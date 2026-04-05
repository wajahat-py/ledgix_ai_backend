import logging
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
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
        if not id_token_str:
            return Response(
                {"detail": "id_token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            idinfo = google_id_token.verify_oauth2_token(
                id_token_str,
                google_requests.Request(),
                django_settings.GOOGLE_CLIENT_ID,
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
            logger.info("Created new Google user: %s", email)
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

        tokens   = get_tokens_for_user(user)
        response = Response({
            "access":  tokens["access"],
            "refresh": tokens["refresh"],
            "user":    UserSerializer(user).data,
        })
        _set_refresh_cookie(response, tokens["refresh"])
        return response


class LoginView(TokenObtainPairView):
    """POST /api/auth/login/"""

    def post(self, request, *args, **kwargs):
        email = request.data.get("email", "").lower().strip()
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
            # Email/password (or linked) account: silently succeed so we don't
            # reveal that this email is registered.
            return _SUCCESS

        parts      = data["full_name"].strip().split(" ", 1)
        first_name = parts[0]
        last_name  = parts[1] if len(parts) > 1 else ""

        try:
            pending = PendingRegistration.objects.get(email=email)
            # Resend only if the cooldown has elapsed.
            if timezone.now() - pending.last_sent_at >= _RESEND_COOLDOWN:
                pending.token        = uuid.uuid4()
                pending.last_sent_at = timezone.now()
                pending.save(update_fields=["token", "last_sent_at"])
                send_verification_email(pending)
        except PendingRegistration.DoesNotExist:
            pending = PendingRegistration.objects.create(
                email        = email,
                first_name   = first_name,
                last_name    = last_name,
                company_name = data.get("company_name", ""),
                password     = make_password(data["password"]),
            )
            send_verification_email(pending)

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
        pending.delete()

        logger.info("Account created after email verification: %s", user.email)
        return Response({"detail": "Email verified. You can now sign in."})


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
        send_verification_email(pending)

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


class MeView(APIView):
    """GET /api/auth/me/  — PATCH /api/auth/me/"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)

    def patch(self, request):
        serializer = ProfileUpdateSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data)
