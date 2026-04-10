from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import ActivityLog, Invitation, Membership, Organization

User = get_user_model()


class OrganizationSerializer(serializers.ModelSerializer):
    member_count = serializers.SerializerMethodField()

    class Meta:
        model  = Organization
        fields = ["id", "name", "slug", "plan", "intended_plan", "member_count", "created_at"]
        read_only_fields = ["id", "slug", "plan", "intended_plan", "member_count", "created_at"]

    def get_member_count(self, obj):
        return obj.memberships.count()


class OrganizationUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Organization
        fields = ["name"]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Name cannot be blank.")
        return value


class MemberUserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model  = User
        fields = ["id", "email", "first_name", "last_name", "full_name"]

    def get_full_name(self, obj):
        return obj.full_name


class MembershipSerializer(serializers.ModelSerializer):
    user       = MemberUserSerializer(read_only=True)
    invited_by = MemberUserSerializer(read_only=True)

    class Meta:
        model  = Membership
        fields = ["id", "user", "role", "can_approve", "invited_by", "joined_at"]
        read_only_fields = ["id", "user", "invited_by", "joined_at"]


class InvitationSerializer(serializers.ModelSerializer):
    invited_by = MemberUserSerializer(read_only=True)
    is_pending = serializers.BooleanField(read_only=True)

    class Meta:
        model  = Invitation
        fields = ["id", "email", "role", "invited_by", "created_at", "expires_at", "is_pending"]
        read_only_fields = ["id", "invited_by", "created_at", "expires_at", "is_pending"]


class InviteCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role  = serializers.ChoiceField(choices=Invitation.Role.choices, default=Invitation.Role.MEMBER)

    def validate_email(self, value):
        return value.lower().strip()


class PublicInvitationSerializer(serializers.ModelSerializer):
    """Read-only shape for the /invite/<token>/ acceptance page."""
    organization_name = serializers.SerializerMethodField()
    inviter_name      = serializers.SerializerMethodField()
    is_pending        = serializers.BooleanField(read_only=True)
    is_expired        = serializers.BooleanField(read_only=True)

    class Meta:
        model  = Invitation
        fields = ["email", "role", "organization_name", "inviter_name", "is_pending", "is_expired"]

    def get_organization_name(self, obj):
        return obj.organization.name

    def get_inviter_name(self, obj):
        return obj.invited_by.full_name if obj.invited_by else "Ledgix"


class ActivityLogSerializer(serializers.ModelSerializer):
    user_name    = serializers.SerializerMethodField()
    target_name  = serializers.SerializerMethodField()
    invoice_name = serializers.SerializerMethodField()

    class Meta:
        model  = ActivityLog
        fields = ["id", "action", "user_name", "target_name", "invoice_name", "metadata", "created_at"]

    def get_user_name(self, obj):
        return obj.user.full_name or obj.user.email if obj.user else "System"

    def get_target_name(self, obj):
        return obj.target_user.full_name or obj.target_user.email if obj.target_user else None

    def get_invoice_name(self, obj):
        return obj.invoice.original_filename if obj.invoice else None
