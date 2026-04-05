import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    Custom User model where email is the unique identifier for authentication
    instead of username. Only exists after email has been verified.

    One email = one account.  auth_provider records *how* the account was
    first created; google_id is set whenever Google is linked (at signup or
    later).  A user may have both a usable password and a google_id.
    """

    class AuthProvider(models.TextChoices):
        EMAIL  = "email",  "Email"
        GOOGLE = "google", "Google"

    email        = models.EmailField(unique=True)
    first_name   = models.CharField(max_length=150, blank=False)
    last_name    = models.CharField(max_length=150, blank=True)
    company_name = models.CharField(max_length=255, blank=True)

    auth_provider = models.CharField(
        max_length=20,
        choices=AuthProvider.choices,
        default=AuthProvider.EMAIL,
    )
    # Populated on Google sign-in/link.  NULL = not linked.
    google_id = models.CharField(max_length=255, null=True, blank=True, unique=True)

    USERNAME_FIELD  = "email"
    REQUIRED_FIELDS = ["username", "first_name"]

    def __str__(self):
        return self.email

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def has_google(self) -> bool:
        return self.google_id is not None

    @property
    def has_password(self) -> bool:
        return self.has_usable_password()


class PasswordResetToken(models.Model):
    """
    Single-use token for password reset. Expires after 1 hour.
    All tokens for a user are deleted once any one of them is consumed.
    """
    user       = models.ForeignKey(
        "User", on_delete=models.CASCADE, related_name="password_reset_tokens"
    )
    token      = models.UUIDField(default=uuid.uuid4, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"PasswordResetToken({self.user.email})"


class PendingRegistration(models.Model):
    """
    Holds signup data until the user verifies their email.
    Promoted to a real User on verification, then deleted.
    """
    email        = models.EmailField(unique=True)
    first_name   = models.CharField(max_length=150)
    last_name    = models.CharField(max_length=150, blank=True)
    company_name = models.CharField(max_length=255, blank=True)
    password     = models.CharField(max_length=128)  # already hashed
    token        = models.UUIDField(default=uuid.uuid4, unique=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"PendingRegistration({self.email})"
