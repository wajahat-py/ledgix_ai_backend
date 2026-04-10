import stripe
from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from organizations.mixins import OrgScopedMixin
from organizations.models import Organization
from organizations.permissions import can_manage_billing

stripe.api_key = settings.STRIPE_SECRET_KEY


def _metadata_value(metadata, key: str) -> str:
    if not metadata:
        return ""
    if isinstance(metadata, dict):
        return str(metadata.get(key, "") or "")
    value = getattr(metadata, key, "")
    return str(value or "")


def _subscription_status(subscription) -> str:
    if not subscription:
        return ""
    return str(getattr(subscription, "status", "") or "")


def _subscription_cancel_at_period_end(subscription) -> bool:
    if not subscription:
        return False
    return bool(getattr(subscription, "cancel_at_period_end", False))


def _subscription_period_end(subscription):
    if not subscription:
        return None
    return getattr(subscription, "current_period_end", None)


def _set_org_plan(org: Organization, *, plan: str, intended_plan: str | None = None, subscription_id: str | None = None) -> None:
    org.plan = plan
    if intended_plan is not None:
        org.intended_plan = intended_plan
    if subscription_id is not None:
        org.stripe_subscription_id = subscription_id
    org.save(update_fields=["plan", "intended_plan", "stripe_subscription_id"])


def _latest_subscription_for_org(org: Organization):
    if not settings.STRIPE_SECRET_KEY or not org.stripe_customer_id:
        return None

    subscription = None

    if org.stripe_subscription_id:
        try:
            subscription = stripe.Subscription.retrieve(org.stripe_subscription_id)
        except stripe.error.InvalidRequestError:
            subscription = None

    if subscription is None:
        subscriptions = stripe.Subscription.list(
            customer=org.stripe_customer_id,
            status="all",
            limit=10,
        )
        data = getattr(subscriptions, "data", []) or []
        active_like_statuses = {"active", "trialing", "past_due", "unpaid", "incomplete"}
        subscription = next(
            (sub for sub in data if _subscription_status(sub) in active_like_statuses),
            data[0] if data else None,
        )

    return subscription


def _sync_org_from_stripe(org: Organization):
    """
    Pull the latest subscription state from Stripe so the UI can reflect
    upgrades/cancellations immediately, even before webhooks arrive.
    """
    if not settings.STRIPE_SECRET_KEY or not org.stripe_customer_id:
        return org, None

    subscription = _latest_subscription_for_org(org)

    status_name = _subscription_status(subscription)
    subscription_id = getattr(subscription, "id", "") if subscription else ""
    cancel_at_period_end = _subscription_cancel_at_period_end(subscription)

    if status_name in {"active", "trialing"}:
        _set_org_plan(
            org,
            plan=Organization.Plan.PRO,
            intended_plan=Organization.Plan.FREE if cancel_at_period_end else Organization.Plan.PRO,
            subscription_id=subscription_id,
        )
    elif status_name in {"past_due", "unpaid", "incomplete"}:
        _set_org_plan(
            org,
            plan=Organization.Plan.FREE,
            intended_plan=Organization.Plan.PRO,
            subscription_id=subscription_id,
        )
    else:
        _set_org_plan(
            org,
            plan=Organization.Plan.FREE,
            intended_plan=Organization.Plan.FREE,
            subscription_id="",
        )

    org.refresh_from_db()
    return org, subscription


class CreateCheckoutSessionView(OrgScopedMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        org = request.org
        membership = request.membership

        if not can_manage_billing(membership):
            return Response(
                {"detail": "Only the workspace owner can manage billing."},
                status=403,
            )

        if org.plan == Organization.Plan.PRO:
            return Response({"detail": "Already on the Pro plan."}, status=400)

        if not settings.STRIPE_SECRET_KEY:
            return Response(
                {"detail": "Stripe is not configured on the server."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            if not org.stripe_customer_id:
                customer = stripe.Customer.create(
                    email=request.user.email,
                    name=org.name,
                    metadata={"org_id": str(org.id)},
                )
                org.stripe_customer_id = customer.id
                org.save(update_fields=["stripe_customer_id"])

            price_id = self._resolve_price_id(settings.STRIPE_PRO_PRICE_ID)

            session = stripe.checkout.Session.create(
                customer=org.stripe_customer_id,
                mode="subscription",
                line_items=[{"price": price_id, "quantity": 1}],
                payment_method_collection="always",
                success_url=(
                    f"{settings.FRONTEND_URL}/billing/success"
                    "?session_id={CHECKOUT_SESSION_ID}"
                ),
                cancel_url=f"{settings.FRONTEND_URL}/pricing",
                metadata={"org_id": str(org.id)},
            )
        except stripe.error.StripeError as exc:
            return Response({"detail": str(exc.user_message or exc)}, status=502)
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({"url": session.url})

    @staticmethod
    def _resolve_price_id(price_id: str) -> str:
        if not price_id:
            raise ValueError(
                "Stripe price is not configured. Set DEV_STRIPE_PRO_PRICE_ID to a price_... or prod_... value."
            )
        if not price_id.startswith("prod_"):
            return price_id
        product = stripe.Product.retrieve(price_id)
        default_price = getattr(product, "default_price", None)
        if not default_price:
            raise ValueError(
                f"Stripe product {price_id} has no default_price set. "
                "Create a price in the Stripe dashboard and set it as the product's default, "
                "or update DEV_STRIPE_PRO_PRICE_ID to a price_... ID."
            )
        return default_price if isinstance(default_price, str) else default_price["id"]


class BillingPortalView(OrgScopedMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        org = request.org
        membership = request.membership

        if not can_manage_billing(membership):
            return Response(
                {"detail": "Only the workspace owner can manage billing."},
                status=403,
            )

        if not org.stripe_customer_id:
            return Response({"detail": "No active subscription found."}, status=400)

        try:
            session = stripe.billing_portal.Session.create(
                customer=org.stripe_customer_id,
                return_url=f"{settings.FRONTEND_URL}/settings/workspace",
            )
        except stripe.error.StripeError as exc:
            return Response({"detail": str(exc.user_message or exc)}, status=502)

        return Response({"url": session.url})


class BillingStatusView(OrgScopedMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        org = request.org
        subscription = None

        try:
            if org.stripe_customer_id:
                org, subscription = _sync_org_from_stripe(org)
        except stripe.error.StripeError as exc:
            return Response({"detail": str(exc.user_message or exc)}, status=502)

        return Response({
            "plan": org.plan,
            "intended_plan": org.intended_plan,
            "has_customer": bool(org.stripe_customer_id),
            "has_subscription": bool(org.stripe_subscription_id),
            "cancel_at_period_end": _subscription_cancel_at_period_end(subscription),
            "current_period_end": _subscription_period_end(subscription),
        })


class VerifyCheckoutSessionView(OrgScopedMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        session_id = request.data.get("session_id", "")
        if not session_id:
            return Response({"detail": "session_id is required."}, status=400)

        try:
            session = stripe.checkout.Session.retrieve(session_id)
        except stripe.error.StripeError as exc:
            return Response({"detail": str(exc.user_message or exc)}, status=502)

        payment_status = getattr(session, "payment_status", None)
        if getattr(session, "status", None) != "complete" or payment_status not in {"paid", "no_payment_required"}:
            return Response({"detail": "Payment not yet confirmed.", "plan": "free"})

        org = request.org
        metadata = getattr(session, "metadata", None)
        meta_org_id = _metadata_value(metadata, "org_id")
        if str(org.id) != meta_org_id:
            return Response({"detail": "Session does not belong to this workspace."}, status=403)

        org.plan = Organization.Plan.PRO
        org.intended_plan = Organization.Plan.PRO
        org.stripe_customer_id = getattr(session, "customer", None) or org.stripe_customer_id
        org.stripe_subscription_id = getattr(session, "subscription", None) or org.stripe_subscription_id
        org.save(update_fields=["plan", "intended_plan", "stripe_customer_id", "stripe_subscription_id"])

        return Response({"plan": "pro"})


class StripeWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        webhook_secret = settings.STRIPE_WEBHOOK_SECRET

        if webhook_secret:
            try:
                event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
            except (ValueError, stripe.error.SignatureVerificationError):
                return Response({"detail": "Invalid signature."}, status=400)
        else:
            import json
            try:
                event = json.loads(payload)
            except (ValueError, KeyError):
                return Response({"detail": "Invalid payload."}, status=400)

        event_type = getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else None)
        data = getattr(event, "data", None) or (event.get("data") if isinstance(event, dict) else None)
        data_obj = getattr(data, "object", None) or (data.get("object") if isinstance(data, dict) else {})

        if event_type == "checkout.session.completed":
            self._handle_checkout_completed(data_obj)
        elif event_type == "customer.subscription.updated":
            self._handle_subscription_updated(data_obj)
        elif event_type == "customer.subscription.deleted":
            self._handle_subscription_deleted(data_obj)

        return Response({"received": True})

    def _handle_checkout_completed(self, session) -> None:
        metadata = getattr(session, "metadata", None)
        org_id = _metadata_value(metadata, "org_id")
        if not org_id:
            return
        try:
            org = Organization.objects.get(id=org_id)
        except Organization.DoesNotExist:
            return

        org.plan = Organization.Plan.PRO
        org.intended_plan = Organization.Plan.PRO
        org.stripe_customer_id = getattr(session, "customer", None) or org.stripe_customer_id
        org.stripe_subscription_id = getattr(session, "subscription", None) or org.stripe_subscription_id
        org.save(update_fields=["plan", "intended_plan", "stripe_customer_id", "stripe_subscription_id"])

    def _handle_subscription_updated(self, subscription) -> None:
        customer_id = getattr(subscription, "customer", None)
        subscription_status = _subscription_status(subscription)
        cancel_at_period_end = _subscription_cancel_at_period_end(subscription)
        if not customer_id:
            return
        try:
            org = Organization.objects.get(stripe_customer_id=customer_id)
        except Organization.DoesNotExist:
            return

        if subscription_status in {"active", "trialing"}:
            org.plan = Organization.Plan.PRO
            org.intended_plan = Organization.Plan.FREE if cancel_at_period_end else Organization.Plan.PRO
            org.stripe_subscription_id = getattr(subscription, "id", None)
            org.save(update_fields=["plan", "intended_plan", "stripe_subscription_id"])
        elif subscription_status in {"past_due", "unpaid", "incomplete", "incomplete_expired"}:
            org.plan = Organization.Plan.FREE
            org.intended_plan = Organization.Plan.PRO
            org.stripe_subscription_id = getattr(subscription, "id", None) or org.stripe_subscription_id
            org.save(update_fields=["plan", "intended_plan", "stripe_subscription_id"])
        elif subscription_status in {"canceled"}:
            org.plan = Organization.Plan.FREE
            org.intended_plan = Organization.Plan.FREE
            org.stripe_subscription_id = ""
            org.save(update_fields=["plan", "intended_plan", "stripe_subscription_id"])

    def _handle_subscription_deleted(self, subscription) -> None:
        customer_id = getattr(subscription, "customer", None)
        if not customer_id:
            return
        try:
            org = Organization.objects.get(stripe_customer_id=customer_id)
        except Organization.DoesNotExist:
            return

        org.plan = Organization.Plan.FREE
        org.intended_plan = Organization.Plan.FREE
        org.stripe_subscription_id = ""
        org.save(update_fields=["plan", "intended_plan", "stripe_subscription_id"])
