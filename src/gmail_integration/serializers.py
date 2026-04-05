from rest_framework import serializers

from .models import GmailIntegration, GmailSyncedMessage


class GmailSyncedMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = GmailSyncedMessage
        fields = [
            "id",
            "message_id",
            "attachment_id",
            "subject",
            "sender",
            "received_at",
            "attachment_filename",
            "invoice_id",
            "invoice_detected",
            "synced_at",
        ]


class GmailStatusSerializer(serializers.ModelSerializer):
    recent_imports = serializers.SerializerMethodField()

    class Meta:
        model = GmailIntegration
        fields = [
            "gmail_address",
            "is_active",
            "last_synced_at",
            "created_at",
            "recent_imports",
        ]

    def get_recent_imports(self, obj):
        qs = obj.synced_messages.filter(invoice_detected=True).order_by("-synced_at")[:30]
        return GmailSyncedMessageSerializer(qs, many=True).data
