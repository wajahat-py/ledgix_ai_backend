from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from .serializers import RegisterSerializer, UserSerializer

User = get_user_model()

_REFRESH_COOKIE = "refresh_token"
_COOKIE_MAX_AGE = 30 * 24 * 60 * 60  # 30 days


def _set_refresh_cookie(response, refresh_token: str) -> None:
    """Attach the refresh token as an HTTP-only cookie to the response."""
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


class LoginView(TokenObtainPairView):
    """
    POST /api/auth/login/
    Extends simplejwt's TokenObtainPairView: same JSON response (access + refresh),
    but also sets the refresh token as an HTTP-only cookie for browser clients.
    NextAuth reads the refresh token from the response body (server-to-server).
    """

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200 and response.data.get("refresh"):
            _set_refresh_cookie(response, response.data["refresh"])
        return response


class TokenRefreshView(APIView):
    """
    POST /api/auth/token/refresh/
    Accepts the refresh token from either:
      - The HTTP-only cookie (browser clients calling Django directly)
      - The request body { "refresh": "..." } (NextAuth server-to-server calls)
    Returns a new access token and resets the cookie's expiry.
    """

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
            token = RefreshToken(refresh_token)
            access = str(token.access_token)
            response = Response({"access": access, "refresh": refresh_token})
            _set_refresh_cookie(response, refresh_token)
            return response
        except TokenError:
            return Response(
                {"detail": "Invalid or expired refresh token."},
                status=status.HTTP_401_UNAUTHORIZED,
            )


class RegisterView(generics.CreateAPIView):
    """
    POST /api/auth/register/
    Body: { email, full_name, password, password2 }
    Returns: { user, access, refresh } — also sets the refresh cookie.
    """

    queryset = User.objects.all()
    permission_classes = [permissions.AllowAny]
    serializer_class = RegisterSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        tokens = get_tokens_for_user(user)
        response = Response(
            {"user": UserSerializer(user).data, **tokens},
            status=status.HTTP_201_CREATED,
        )
        _set_refresh_cookie(response, tokens["refresh"])
        return response


class MeView(generics.RetrieveAPIView):
    """GET /api/auth/me/"""

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user
