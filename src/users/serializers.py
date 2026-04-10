from django.contrib.auth import get_user_model
from rest_framework import serializers

User = get_user_model()


class RegisterSerializer(serializers.Serializer):
    email        = serializers.EmailField(required=True)
    full_name    = serializers.CharField(required=True)
    company_name = serializers.CharField(required=False, allow_blank=True, default="")
    password     = serializers.CharField(write_only=True, required=True, min_length=8, style={"input_type": "password"})
    password2    = serializers.CharField(write_only=True, required=True, style={"input_type": "password"})
    plan         = serializers.CharField(required=False, default="free")

    def validate_email(self, value):
        return value.lower().strip()

    def validate(self, attrs):
        if attrs["password"] != attrs["password2"]:
            raise serializers.ValidationError({"password": "Passwords do not match."})
        return attrs


class UserSerializer(serializers.ModelSerializer):
    full_name    = serializers.SerializerMethodField()
    has_google   = serializers.SerializerMethodField()
    has_password = serializers.SerializerMethodField()

    class Meta:
        model  = User
        fields = (
            "id", "email", "first_name", "last_name", "full_name",
            "company_name", "auth_provider", "has_google", "has_password",
        )

    def get_full_name(self, obj):
        return obj.full_name

    def get_has_google(self, obj):
        return obj.has_google

    def get_has_password(self, obj):
        return obj.has_password


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)

    def validate_email(self, value):
        return value.lower().strip()


class ResetPasswordSerializer(serializers.Serializer):
    token     = serializers.UUIDField(required=True)
    password  = serializers.CharField(write_only=True, required=True, min_length=8)
    password2 = serializers.CharField(write_only=True, required=True)

    def validate(self, attrs):
        if attrs["password"] != attrs["password2"]:
            raise serializers.ValidationError({"password": "Passwords do not match."})
        return attrs


class ProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model  = User
        fields = ("first_name", "last_name", "company_name")
        extra_kwargs = {
            "first_name":   {"required": False},
            "last_name":    {"required": False},
            "company_name": {"required": False},
        }
