from django.urls import path

from .views import (
    ContactSalesView,
    ForgotPasswordView,
    GoogleAuthView,
    LoginView,
    MeView,
    RegisterView,
    ResendVerificationView,
    ResetPasswordView,
    TokenRefreshView,
    VerifyEmailView,
)

urlpatterns = [
    path("register/",             RegisterView.as_view(),            name="auth-register"),
    path("login/",                LoginView.as_view(),               name="auth-login"),
    path("token/refresh/",        TokenRefreshView.as_view(),        name="auth-token-refresh"),
    path("me/",                   MeView.as_view(),                  name="auth-me"),
    path("verify-email/",         VerifyEmailView.as_view(),         name="auth-verify-email"),
    path("resend-verification/",  ResendVerificationView.as_view(),  name="auth-resend-verification"),
    path("forgot-password/",      ForgotPasswordView.as_view(),      name="auth-forgot-password"),
    path("reset-password/",       ResetPasswordView.as_view(),       name="auth-reset-password"),
    path("google/",               GoogleAuthView.as_view(),           name="auth-google"),
    path("contact-sales/",        ContactSalesView.as_view(),         name="auth-contact-sales"),
]
