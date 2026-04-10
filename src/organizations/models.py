import uuid

from django.conf import settings
from django.db import models
from django.utils.text import slugify


def _unique_slug(name: str) -> str:
    """Generate a unique slug for an organization name."""
    base = slugify(name) or "workspace"
    slug = base
    i = 1
    while Organization.objects.filter(slug=slug).exists():
        slug = f"{base}-{i}"
        i += 1
    return slug


class Organization(models.Model):
    class Plan(models.TextChoices):
        FREE     = "free",     "Free"
        PRO      = "pro",      "Pro"
        BUSINESS = "business", "Business"

    # Maximum members per plan (None = unlimited)
    SEAT_LIMITS: dict[str, int | None] = {"free": 1, "pro": 5, "business": None}

    name                    = models.CharField(max_length=255)
    slug                    = models.SlugField(unique=True, max_length=255)
    plan                    = models.CharField(max_length=20, choices=Plan.choices, default=Plan.FREE)
    intended_plan           = models.CharField(max_length=20, choices=Plan.choices, default=Plan.FREE)
    stripe_customer_id      = models.CharField(max_length=255, blank=True)
    stripe_subscription_id  = models.CharField(max_length=255, blank=True)
    created_at              = models.DateTimeField(auto_now_add=True)
    updated_at              = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER  = "owner",  "Owner"
        ADMIN  = "admin",  "Admin"
        MEMBER = "member", "Member"
        VIEWER = "viewer", "Viewer"

    ROLE_RANK: dict[str, int] = {"owner": 4, "admin": 3, "member": 2, "viewer": 1}

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    user         = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    role         = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    # Extra permission override: members with this flag can approve/reject invoices
    can_approve  = models.BooleanField(default=False)
    invited_by   = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    joined_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("organization", "user")]
        ordering        = ["joined_at"]

    def __str__(self) -> str:
        return f"{self.user.email} — {self.role} @ {self.organization.name}"


class Invitation(models.Model):
    class Role(models.TextChoices):
        ADMIN  = "admin",  "Admin"
        MEMBER = "member", "Member"
        VIEWER = "viewer", "Viewer"

    _EXPIRY_DAYS = 7

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="invitations")
    email        = models.EmailField()
    role         = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    token        = models.UUIDField(default=uuid.uuid4, unique=True)
    invited_by   = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    created_at   = models.DateTimeField(auto_now_add=True)
    expires_at   = models.DateTimeField()
    accepted_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        # One pending invite per email per org
        unique_together = [("organization", "email")]

    def __str__(self) -> str:
        return f"Invite {self.email} → {self.organization.name} ({self.role})"

    @property
    def is_expired(self) -> bool:
        from django.utils import timezone
        return timezone.now() > self.expires_at

    @property
    def is_pending(self) -> bool:
        return self.accepted_at is None and not self.is_expired


class ActivityLog(models.Model):
    class Action(models.TextChoices):
        INVOICE_UPLOADED  = "INVOICE_UPLOADED",  "Invoice Uploaded"
        INVOICE_PROCESSED = "INVOICE_PROCESSED", "Invoice Processed"
        INVOICE_APPROVED  = "INVOICE_APPROVED",  "Invoice Approved"
        INVOICE_REJECTED  = "INVOICE_REJECTED",  "Invoice Rejected"
        INVOICE_DELETED   = "INVOICE_DELETED",   "Invoice Deleted"
        MEMBER_INVITED    = "MEMBER_INVITED",    "Member Invited"
        MEMBER_JOINED     = "MEMBER_JOINED",     "Member Joined"
        MEMBER_REMOVED    = "MEMBER_REMOVED",    "Member Removed"
        ROLE_CHANGED      = "ROLE_CHANGED",      "Role Changed"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="activity_logs")
    user         = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    action      = models.CharField(max_length=30, choices=Action.choices)
    # Optional references — SET_NULL so logs survive deletions
    invoice     = models.ForeignKey(
        "invoices.Invoice",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    metadata    = models.JSONField(default=dict)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action} by {self.user} @ {self.organization}"
