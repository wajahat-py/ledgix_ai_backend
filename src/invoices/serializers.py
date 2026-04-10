from rest_framework import serializers

from .models import DuplicateCheckResult, Invoice, Notification


class DuplicateCheckResultSerializer(serializers.ModelSerializer):
    best_match_filename = serializers.SerializerMethodField()

    class Meta:
        model = DuplicateCheckResult
        fields = [
            "decision",
            "best_match",
            "best_match_filename",
            "best_match_score",
            "score_details",
            "dismissed",
            "checked_at",
        ]

    def get_best_match_filename(self, obj):
        return obj.best_match.original_filename if obj.best_match else None


class InvoiceSerializer(serializers.ModelSerializer):
    file_url         = serializers.SerializerMethodField()
    duplicate_check  = DuplicateCheckResultSerializer(read_only=True)
    uploaded_by_name  = serializers.SerializerMethodField()
    approved_by_name  = serializers.SerializerMethodField()
    rejected_by_name  = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            "id",
            "original_filename",
            "file_url",
            "status",
            "extracted_data",
            "duplicate_check",
            "error_message",
            # audit trail
            "uploaded_by_name",
            "approved_by_name",
            "rejected_by_name",
            "reviewed_at",
            "rejection_reason",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_file_url(self, obj):
        if not obj.file:
            return None
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(obj.file.url)
        # Celery tasks call this serializer without an HTTP request context.
        # Fall back to an explicit base URL so the file_url is never silently
        # dropped in WebSocket pushes.
        from django.conf import settings as django_settings
        base = getattr(django_settings, "BACKEND_URL", "http://localhost:8000").rstrip("/")
        return f"{base}{obj.file.url}"

    def get_uploaded_by_name(self, obj):
        return obj.user.full_name or obj.user.email

    def get_approved_by_name(self, obj):
        if obj.approved_by:
            return obj.approved_by.full_name or obj.approved_by.email
        return None

    def get_rejected_by_name(self, obj):
        if obj.rejected_by:
            return obj.rejected_by.full_name or obj.rejected_by.email
        return None


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "kind", "title", "body", "invoice_id", "is_read", "created_at"]
        read_only_fields = fields
