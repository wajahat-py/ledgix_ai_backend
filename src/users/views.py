import logging
import uuid
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.files.base import ContentFile
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from .email import send_contact_sales_email, send_password_reset_email, send_verification_email
from .models import PasswordResetToken, PendingRegistration
from .serializers import (
    ForgotPasswordSerializer,
    ProfileUpdateSerializer,
    RegisterSerializer,
    ResetPasswordSerializer,
    UserSerializer,
)

logger = logging.getLogger(__name__)
User = get_user_model()

DEMO_EMAIL = "demo@ledgix.ai"
DEMO_PASSWORD = "demo123"
DEMO_WORKSPACE_NAME = "Ledgix Demo Workspace"


_DEMO_PDFS_DIR = Path(__file__).resolve().parent.parent / "invoices" / "demo_pdfs"

_FALLBACK_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]/Contents 4 0 R>>endobj\n"
    b"4 0 obj<</Length 55>>stream\nBT /F1 18 Tf 36 72 Td (Ledgix Demo Invoice) Tj ET\nendstream endobj\n"
    b"xref\n0 5\n0000000000 65535 f \n0000000010 00000 n \n0000000053 00000 n \n0000000108 00000 n \n0000000195 00000 n \n"
    b"trailer<</Root 1 0 R/Size 5>>\nstartxref\n300\n%%EOF\n"
)


def _demo_invoice_bytes(filename: str) -> bytes:
    """Return bytes for a named demo PDF from the static demo_pdfs directory.

    Resolution order:
      1. invoices/demo_pdfs/<filename>   — exact match
      2. invoices/demo_pdfs/sample_invoice.pdf — generic fallback
      3. Minimal valid inline PDF        — last resort (no files at all)
    """
    for candidate in (
        _DEMO_PDFS_DIR / filename,
        _DEMO_PDFS_DIR / "sample_invoice.pdf",
    ):
        if candidate.exists():
            return candidate.read_bytes()
    return _FALLBACK_PDF


def _attach_demo_pdf(invoice, filename: str, pdf_bytes: bytes) -> None:
    """Ensure a demo invoice points at a valid PDF file."""
    current_size = 0
    try:
        if invoice.file:
            current_size = invoice.file.size
    except Exception:
        current_size = 0

    if current_size > 1024:
        return

    invoice.file.save(filename, ContentFile(pdf_bytes), save=False)


def _bootstrap_personal_org(user, intended_plan="free") -> None:
    """Create a personal workspace + owner membership for a brand-new user."""
    from organizations.models import Membership, Organization, _unique_slug
    if Membership.objects.filter(user=user).exists():
        return
    name = f"{user.first_name or user.email.split('@')[0]}'s Workspace"
    org  = Organization.objects.create(
        name=name, 
        slug=_unique_slug(name),
        intended_plan=intended_plan
    )
    Membership.objects.create(organization=org, user=user, role=Membership.Role.OWNER)
    logger.info("Created personal workspace '%s' (intended=%s) for %s", name, intended_plan, user.email)


def _seed_demo_workspace(user) -> None:
    """Create a reusable demo workspace with a few realistic sample invoices."""
    from organizations.models import Membership, Organization, _unique_slug
    from invoices.models import DuplicateCheckResult, Invoice

    membership = (
        Membership.objects
        .select_related("organization")
        .filter(user=user)
        .order_by("joined_at")
        .first()
    )
    if membership:
        org = membership.organization
        if org.name != DEMO_WORKSPACE_NAME:
            org.name = DEMO_WORKSPACE_NAME
            org.save(update_fields=["name"])
    else:
        org = Organization.objects.create(
            name=DEMO_WORKSPACE_NAME,
            slug=_unique_slug(DEMO_WORKSPACE_NAME),
            intended_plan=Organization.Plan.PRO,
            plan=Organization.Plan.PRO,
        )
        Membership.objects.create(
            organization=org,
            user=user,
            role=Membership.Role.OWNER,
            can_approve=True,
        )

    if org.invoices.exists():
        for invoice in org.invoices.all():
            _attach_demo_pdf(invoice, invoice.original_filename, _demo_invoice_bytes(invoice.original_filename))
            if invoice.file:
                invoice.save(update_fields=["file"])
        return

    now = timezone.now()
    demo_specs = [
        {
            "filename": "aws-infrastructure-1047.pdf",
            "status": Invoice.Status.APPROVED,
            "created_at": now - timedelta(days=3),
            "reviewed_at": now - timedelta(days=2, hours=20),
            "approved_by": user,
            "extracted_data": {
                "vendor_name": {"value": "Amazon Web Services", "confidence": 0.98},
                "invoice_number": {"value": "AWS-2023-1047", "confidence": 0.96},
                "invoice_date": {"value": "2026-04-01", "confidence": 0.95},
                "due_date": {"value": "2026-04-15", "confidence": 0.94},
                "total_amount": {"value": "$4,532.12", "confidence": 0.99},
            },
        },
        {
            "filename": "aws-infrastructure-1048.pdf",
            "status": Invoice.Status.PENDING_REVIEW,
            "created_at": now - timedelta(days=2),
            "reviewed_at": None,
            "approved_by": None,
            "extracted_data": {
                "vendor_name": {"value": "Amazon Web Services", "confidence": 0.97},
                "invoice_number": {"value": "AWS-2023-1048", "confidence": 0.94},
                "invoice_date": {"value": "2026-04-02", "confidence": 0.93},
                "due_date": {"value": "2026-04-16", "confidence": 0.92},
                "total_amount": {"value": "$4,532.12", "confidence": 0.98},
            },
        },
        {
            "filename": "stripe-processing-march.pdf",
            "status": Invoice.Status.PROCESSED,
            "created_at": now - timedelta(days=1, hours=8),
            "reviewed_at": None,
            "approved_by": None,
            "extracted_data": {
                "vendor_name": {"value": "Stripe Inc.", "confidence": 0.99},
                "invoice_number": {"value": "STRP-MAR-2026", "confidence": 0.95},
                "invoice_date": {"value": "2026-04-04", "confidence": 0.96},
                "due_date": {"value": "2026-04-18", "confidence": 0.91},
                "total_amount": {"value": "$1,245.00", "confidence": 0.98},
            },
        },
    ]

    invoices_by_filename = {inv.original_filename: inv for inv in org.invoices.all()}

    for spec in demo_specs:
        invoice = invoices_by_filename.get(spec["filename"])
        if invoice is None:
            invoice = Invoice(
                user=user,
                organization=org,
                original_filename=spec["filename"],
                status=spec["status"],
                extracted_data=spec["extracted_data"],
                approved_by=spec["approved_by"],
                reviewed_at=spec["reviewed_at"],
                error_message=spec.get("error_message", ""),
            )
        else:
            invoice.user = user
            invoice.organization = org
            invoice.status = spec["status"]
            invoice.extracted_data = spec["extracted_data"]
            invoice.approved_by = spec["approved_by"]
            invoice.reviewed_at = spec["reviewed_at"]
            invoice.error_message = spec.get("error_message", "")

        _attach_demo_pdf(invoice, spec["filename"], _demo_invoice_bytes(spec["filename"]))
        invoice.save()
        Invoice.objects.filter(pk=invoice.pk).update(
            created_at=spec["created_at"],
            updated_at=spec["created_at"],
            reviewed_at=spec["reviewed_at"],
        )
        invoice.refresh_from_db()
        invoices_by_filename[spec["filename"]] = invoice

    aws_1048 = invoices_by_filename.get("aws-infrastructure-1048.pdf")
    aws_1047 = invoices_by_filename.get("aws-infrastructure-1047.pdf")
    if aws_1048 and aws_1047:
        DuplicateCheckResult.objects.update_or_create(
            invoice=aws_1048,
            defaults={
                "decision": DuplicateCheckResult.Decision.POSSIBLE_DUPLICATE,
                "best_match": aws_1047,
                "best_match_score": 0.91,
                "score_details": {
                    "rule_score": 0.84,
                    "fuzzy_score": 0.87,
                    "embedding_score": 0.72,
                    "final_score": 0.91,
                    "candidates_checked": 4,
                    "candidates_embedded": 2,
                    "llm_verification": {
                        "is_duplicate": True,
                        "confidence": 0.92,
                        "reason": "Vendor, amount, and date all match a previous invoice",
                    },
                    "same_amount": True,
                    "same_vendor": True,
                    "close_date": True,
                },
                "dismissed": False,
            },
        )


def _ensure_demo_user() -> None:
    """Guarantee that the public demo account and workspace exist."""
    user, created = User.objects.get_or_create(
        email=DEMO_EMAIL,
        defaults={
            "username": DEMO_EMAIL,
            "first_name": "Demo",
            "last_name": "User",
            "company_name": "Ledgix",
            "auth_provider": User.AuthProvider.EMAIL,
        },
    )
    if created or not user.check_password(DEMO_PASSWORD):
        user.username = DEMO_EMAIL
        user.first_name = "Demo"
        user.last_name = "User"
        user.company_name = "Ledgix"
        user.auth_provider = User.AuthProvider.EMAIL
        user.set_password(DEMO_PASSWORD)
        user.save()

    _seed_demo_workspace(user)


_REFRESH_COOKIE = "refresh_token"
_COOKIE_MAX_AGE = 30 * 24 * 60 * 60  # 30 days
_RESEND_COOLDOWN = timedelta(seconds=60)
_TOKEN_EXPIRY    = timedelta(hours=24)


def _set_refresh_cookie(response, refresh_token: str) -> None:
    from django.conf import settings
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=refresh_token,
        httponly=True,
        secure=not settings.DEBUG,
        samesite="Lax",
        max_age=_COOKIE_MAX_AGE,
        path="/api/auth/",
    )


def get_tokens_for_user(user):
    refresh = RefreshToken.for_user(user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


class GoogleAuthView(APIView):
    """
    POST /api/auth/google/
    Body: { "id_token": "<Google ID token>" }

    Verifies the token with Google, then:
      • Email not in DB          → create User (auth_provider=google)
      • Email exists (any)       → link google_id if not already linked, log in
    Returns Django JWT tokens + user info.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        from django.conf import settings as django_settings
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_requests

        id_token_str = request.data.get("id_token", "").strip()
        # Try request body first, then fallback to cookie
        plan = request.data.get("plan") or request.COOKIES.get("intended_plan", "free")
        
        if not id_token_str:
            return Response(
                {"detail": "id_token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            idinfo = google_id_token.verify_oauth2_token(
                id_token_str,
                google_requests.Request(),
                django_settings.GOOGLE_CLIENT_ID_AUTH,
            )
        except ValueError as exc:
            logger.warning("Google token verification failed: %s", exc)
            return Response(
                {"detail": "Invalid or expired Google token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email     = idinfo["email"].lower().strip()
        google_id = idinfo["sub"]
        name      = idinfo.get("name", "")

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            # New user — create directly (Google already verified the email)
            parts      = name.strip().split(" ", 1)
            first_name = parts[0] or email.split("@")[0]
            last_name  = parts[1] if len(parts) > 1 else ""

            user = User(
                username      = email,
                email         = email,
                first_name    = first_name,
                last_name     = last_name,
                auth_provider = User.AuthProvider.GOOGLE,
                google_id     = google_id,
            )
            user.set_unusable_password()
            user.save()
            _bootstrap_personal_org(user, intended_plan=plan)
            logger.info("Created new Google user: %s (intended=%s)", email, plan)
        else:
            # Existing user — link Google if not already linked
            if user.google_id is None:
                user.google_id = google_id
                user.save(update_fields=["google_id"])
                logger.info("Linked Google to existing account: %s", email)
            elif user.google_id != google_id:
                # Same email, different Google account — reject
                return Response(
                    {"detail": "This email is already linked to a different Google account."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        tokens = get_tokens_for_user(user)
        response = Response({
            "access":  tokens["access"],
            "refresh": tokens["refresh"],
            "user":    UserSerializer(user).data,
        })
        _set_refresh_cookie(response, tokens["refresh"])
        response.delete_cookie("intended_plan")
        return response


class LoginView(TokenObtainPairView):
    """POST /api/auth/login/"""

    def post(self, request, *args, **kwargs):
        email = request.data.get("email", "").lower().strip()
        password = request.data.get("password", "")

        if email == DEMO_EMAIL and password == DEMO_PASSWORD:
            _ensure_demo_user()

        try:
            user = User.objects.get(email=email)
            # Google-only accounts have no usable password — tell the frontend
            # explicitly so it can guide the user to "Continue with Google".
            if not user.has_usable_password() and user.google_id:
                return Response(
                    {
                        "detail": "This account uses Google sign-in. Please continue with Google.",
                        "code":   "google_only",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except User.DoesNotExist:
            pass

        response = super().post(request, *args, **kwargs)
        if response.status_code == 200 and response.data.get("refresh"):
            _set_refresh_cookie(response, response.data["refresh"])
        return response


class TokenRefreshView(APIView):
    """POST /api/auth/token/refresh/"""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        refresh_token = (
            request.COOKIES.get(_REFRESH_COOKIE) or request.data.get("refresh")
        )
        if not refresh_token:
            return Response(
                {"detail": "Refresh token not provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            token  = RefreshToken(refresh_token)
            access = str(token.access_token)
            response = Response({"access": access, "refresh": refresh_token})
            _set_refresh_cookie(response, refresh_token)
            return response
        except TokenError:
            return Response(
                {"detail": "Invalid or expired refresh token."},
                status=status.HTTP_401_UNAUTHORIZED,
            )


class RegisterView(APIView):
    """
    POST /api/auth/register/
    Body: { email, full_name, company_name, password, password2 }

    Writes a PendingRegistration and sends a verification email.
    The User record is NOT created until the email is verified.
    Always returns 201 with the same message — never reveals whether an
    email is already registered.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data  = serializer.validated_data
        email = data["email"]

        _SUCCESS = Response(
            {
                "detail": "Check your inbox to verify your email address.",
                "requires_verification": True,
            },
            status=status.HTTP_201_CREATED,
        )

        # Email already belongs to a verified account
        if User.objects.filter(email=email).exists():
            existing = User.objects.get(email=email)
            # Google-only accounts: tell the frontend to redirect to Google sign-in.
            if not existing.has_usable_password() and existing.google_id:
                return Response(
                    {
                        "detail": "This email is already registered via Google. Please sign in with Google.",
                        "code":   "google_account",
                    },
                    status=status.HTTP_200_OK,
                )
            # Existing email/password account — tell the user explicitly.
            return Response(
                {
                    "detail": "An account with this email already exists. Please sign in.",
                    "code":   "email_exists",
                },
                status=status.HTTP_200_OK,
            )

        parts      = data["full_name"].strip().split(" ", 1)
        first_name = parts[0]
        last_name  = parts[1] if len(parts) > 1 else ""

        try:
            pending = PendingRegistration.objects.get(email=email)
            # Resend only if the cooldown has elapsed.
            if timezone.now() - pending.last_sent_at >= _RESEND_COOLDOWN:
                pending.token        = uuid.uuid4()
                pending.last_sent_at = timezone.now()
                pending.plan         = data.get("plan", "free")
                pending.save(update_fields=["token", "last_sent_at", "plan"])
                try:
                    send_verification_email(pending)
                except Exception as exc:
                    logger.error("Failed to resend verification email to %s: %s", email, exc)
                    return Response(
                        {"detail": "We couldn't send the verification email. Please try again in a moment."},
                        status=status.HTTP_502_BAD_GATEWAY,
                    )
        except PendingRegistration.DoesNotExist:
            pending = PendingRegistration.objects.create(
                email        = email,
                first_name   = first_name,
                last_name    = last_name,
                company_name = data.get("company_name", ""),
                password     = make_password(data["password"]),
                plan         = data.get("plan", "free"),
            )
            try:
                send_verification_email(pending)
            except Exception as exc:
                logger.error("Failed to send verification email to %s: %s", email, exc)
                pending.delete()
                return Response(
                    {"detail": "We couldn't send the verification email. Please try again in a moment."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

        return _SUCCESS


class VerifyEmailView(APIView):
    """GET /api/auth/verify-email/?token=<uuid>"""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        raw_token = request.query_params.get("token", "")
        try:
            token = uuid.UUID(raw_token)
        except ValueError:
            return Response(
                {"detail": "Invalid verification link."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            pending = PendingRegistration.objects.get(token=token)
        except PendingRegistration.DoesNotExist:
            return Response(
                {"detail": "This verification link is invalid or has already been used."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if timezone.now() - pending.created_at > _TOKEN_EXPIRY:
            return Response(
                {"detail": "This verification link has expired. Please sign up again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Guard against a race where the email was taken between signup and verify.
        if User.objects.filter(email=pending.email).exists():
            pending.delete()
            return Response(
                {"detail": "An account with this email already exists. Please sign in."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.create(
            username     = pending.email,
            email        = pending.email,
            password     = pending.password,  # already hashed
            first_name   = pending.first_name,
            last_name    = pending.last_name,
            company_name = pending.company_name,
        )
        plan = pending.plan
        pending.delete()

        # Bootstrap the user's personal workspace
        _bootstrap_personal_org(user, intended_plan=plan)

        logger.info("Account created after email verification: %s", user.email)
        return Response({"detail": "Email verified. You can now sign in.", "plan": plan})


class ResendVerificationView(APIView):
    """
    POST /api/auth/resend-verification/
    Body: { "email": "..." }

    Always returns 200 — never reveals whether the email is registered.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = request.data.get("email", "").lower().strip()

        _GENERIC = Response(
            {"detail": "If this email is pending verification, a new link has been sent."}
        )

        try:
            pending = PendingRegistration.objects.get(email=email)
        except PendingRegistration.DoesNotExist:
            return _GENERIC

        if timezone.now() - pending.last_sent_at < _RESEND_COOLDOWN:
            return Response(
                {"detail": "Please wait a moment before requesting another link."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        pending.token        = uuid.uuid4()
        pending.last_sent_at = timezone.now()
        pending.save(update_fields=["token", "last_sent_at"])
        try:
            send_verification_email(pending)
        except Exception as exc:
            logger.error("Failed to resend verification email to %s: %s", email, exc)
            return Response(
                {"detail": "We couldn't send the verification email. Please try again in a moment."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response({"detail": "Verification email sent. Please check your inbox."})


class ForgotPasswordView(APIView):
    """
    POST /api/auth/forgot-password/
    Body: { "email": "..." }

    Always returns 200 with the same message — never reveals whether an
    account exists.  If one does, deletes any stale tokens, creates a fresh
    one, and emails a reset link.  Rate-limited to one email per 60 seconds
    per address.
    """

    permission_classes = [permissions.AllowAny]

    _COOLDOWN = timedelta(seconds=60)
    _RESPONSE = {"detail": "If an account with that email exists, a reset link has been sent."}

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response(self._RESPONSE)

        # Rate-limit: skip if a token was sent in the last 60 s
        latest = (
            PasswordResetToken.objects
            .filter(user=user)
            .order_by("-created_at")
            .first()
        )
        if latest and timezone.now() - latest.created_at < self._COOLDOWN:
            return Response(self._RESPONSE)

        # Delete all old tokens, create a fresh one
        PasswordResetToken.objects.filter(user=user).delete()
        reset_token = PasswordResetToken.objects.create(user=user)
        send_password_reset_email(user, reset_token.token)

        return Response(self._RESPONSE)


class ResetPasswordView(APIView):
    """
    POST /api/auth/reset-password/
    Body: { "token": "<uuid>", "password": "...", "password2": "..." }

    Validates the token, enforces a 1-hour expiry, sets the new password,
    then deletes all reset tokens for that user so the link can't be reused.
    """

    permission_classes = [permissions.AllowAny]

    _EXPIRY = timedelta(hours=1)

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token_uuid = serializer.validated_data["token"]
        new_password = serializer.validated_data["password"]

        try:
            reset_token = PasswordResetToken.objects.select_related("user").get(token=token_uuid)
        except PasswordResetToken.DoesNotExist:
            return Response(
                {"detail": "This reset link is invalid or has already been used."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if timezone.now() - reset_token.created_at > self._EXPIRY:
            reset_token.delete()
            return Response(
                {"detail": "This reset link has expired. Please request a new one."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = reset_token.user
        user.set_password(new_password)
        user.save(update_fields=["password"])

        # Consume all tokens for this user
        PasswordResetToken.objects.filter(user=user).delete()

        logger.info("Password reset completed for user %s", user.email)
        return Response({"detail": "Password reset successfully. You can now sign in."})


class ContactSalesView(APIView):
    """
    POST /api/auth/contact-sales/
    Body: { name, email, company?, message? }
    Forwards a sales enquiry email.  No auth required — also used from the
    public pricing page.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        name    = request.data.get("name",    "").strip()
        email   = request.data.get("email",   "").strip()
        company = request.data.get("company", "").strip()
        message = request.data.get("message", "").strip()

        if not name:
            return Response({"detail": "Name is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not email or "@" not in email:
            return Response({"detail": "A valid email is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            send_contact_sales_email(name, email, company, message)
        except Exception:
            return Response(
                {"detail": "Could not send your message. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response({"detail": "Message sent. We'll be in touch soon."})


def _me_response(user, organization_id: int | None = None) -> dict:
    """Build the /me/ response dict including org + membership info."""
    from organizations.models import Membership, Organization
    from organizations.serializers import OrganizationSerializer

    data = UserSerializer(user).data

    memberships = (
        Membership.objects
        .select_related("organization")
        .filter(user=user)
        .order_by("joined_at")
    )

    primary = None
    if organization_id is not None:
        primary = memberships.filter(organization_id=organization_id).first()

    # Primary org (explicit selection first, otherwise first joined)
    if primary is None:
        primary = memberships.first()
    if primary:
        org_data = OrganizationSerializer(primary.organization).data
        org_data["role"]        = primary.role
        org_data["can_approve"] = primary.can_approve
        data["organization"] = org_data
        data["membership"]   = {
            "id":          primary.id,
            "role":        primary.role,
            "can_approve": primary.can_approve,
        }
    else:
        data["organization"] = None
        data["membership"]   = None

    # All orgs list (for org-switcher)
    data["organizations"] = [
        {
            "id":   m.organization.id,
            "name": m.organization.name,
            "plan": m.organization.plan,
            "intended_plan": m.organization.intended_plan,
            "role": m.role,
        }
        for m in memberships
    ]
    return data


class MeView(APIView):
    """GET /api/auth/me/  — PATCH /api/auth/me/"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        org_id = request.META.get("HTTP_X_ORGANIZATION_ID")
        try:
            organization_id = int(org_id) if org_id is not None else None
        except (TypeError, ValueError):
            organization_id = None
        return Response(_me_response(request.user, organization_id=organization_id))

    def patch(self, request):
        serializer = ProfileUpdateSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(_me_response(user))
