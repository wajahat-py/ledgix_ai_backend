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
        if obj.best_match:
            return obj.best_match.original_filename
        return None


class InvoiceSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()
    duplicate_check = DuplicateCheckResultSerializer(read_only=True)

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
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_file_url(self, obj):
        request = self.context.get("request")
        if obj.file and request:
            return request.build_absolute_uri(obj.file.url)
        return None


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "kind", "title", "body", "invoice_id", "is_read", "created_at"]
        read_only_fields = fields
