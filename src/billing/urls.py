from django.urls import path
from .views import (
    BillingStatusView,
    BillingPortalView,
    CreateCheckoutSessionView,
    StripeWebhookView,
    VerifyCheckoutSessionView,
)

urlpatterns = [
    path("create-checkout-session/", CreateCheckoutSessionView.as_view(), name="billing-checkout"),
    path("verify-checkout/",         VerifyCheckoutSessionView.as_view(), name="billing-verify"),
    path("status/",                  BillingStatusView.as_view(),          name="billing-status"),
    path("portal/",                  BillingPortalView.as_view(),           name="billing-portal"),
    path("webhook/",                 StripeWebhookView.as_view(),           name="billing-webhook"),
]
