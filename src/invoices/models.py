import django.db.models.deletion
from django.conf import settings
from django.db import models


class Invoice(models.Model):
    class Status(models.TextChoices):
        UPLOADED          = "UPLOADED",          "Uploaded"
        PROCESSING        = "PROCESSING",        "Processing"
        PROCESSED         = "PROCESSED",         "Processed"
        PROCESSING_FAILED = "PROCESSING_FAILED", "Processing Failed"
        PENDING_REVIEW    = "PENDING_REVIEW",    "Pending Review"
        APPROVED          = "APPROVED",          "Approved"
        REJECTED          = "REJECTED",          "Rejected"

    user              = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="invoices",
    )
    file              = models.FileField(upload_to="invoices/%Y/%m/")
    original_filename = models.CharField(max_length=255)
    status            = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UPLOADED,
        db_index=True,
    )
    extracted_data    = models.JSONField(null=True, blank=True)
    embedding         = models.JSONField(null=True, blank=True)
    error_message     = models.TextField(blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Invoice {self.id} — {self.original_filename} [{self.status}]"


class DuplicateCheckResult(models.Model):
    class Decision(models.TextChoices):
        DUPLICATE          = "DUPLICATE",          "Duplicate"
        POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE", "Possible Duplicate"
        UNIQUE             = "UNIQUE",             "Unique"

    invoice          = models.OneToOneField(
        Invoice,
        on_delete=models.CASCADE,
        related_name="duplicate_check",
    )
    decision         = models.CharField(max_length=20, choices=Decision.choices)
    best_match       = models.ForeignKey(
        Invoice,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    best_match_score = models.FloatField(null=True, blank=True)
    score_details    = models.JSONField(default=dict)
    checked_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-checked_at"]
