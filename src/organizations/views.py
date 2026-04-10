import logging
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .email import send_invitation_email
from .mixins import OrgScopedMixin, get_or_create_personal_org
from .models import ActivityLog, Invitation, Membership, Organization, _unique_slug
from .permissions import (
    can_change_role_to, can_delete_org, can_invite,
    can_manage_members, can_transfer_ownership,
)
from .serializers import (
    ActivityLogSerializer, InvitationSerializer, InviteCreateSerializer,
    MembershipSerializer, OrganizationSerializer, OrganizationUpdateSerializer,
    PublicInvitationSerializer,
)

logger = logging.getLogger(__name__)
User = get_user_model()


# ── helpers ───────────────────────────────────────────────────────────────────

def _org_response(org: Organization, membership: Membership) -> dict:
    data         = OrganizationSerializer(org).data
    data["role"] = membership.role
    return data


def _check_seat_limit(org: Organization) -> bool:
    """Return True if the org is at or over its seat + pending-invite limit."""
    limit = Organization.SEAT_LIMITS.get(org.plan)
    if limit is None:
        return False  # unlimited
    current = org.memberships.count()
    pending = Invitation.objects.filter(
        organization=org, accepted_at__isnull=True, expires_at__gt=timezone.now()
    ).count()
    return (current + pending) >= limit


# ── Organization ──────────────────────────────────────────────────────────────

class OrganizationListView(OrgScopedMixin, APIView):
    """GET /api/orgs/  — all orgs the user belongs to."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        memberships = (
            Membership.objects
            .select_related("organization")
            .filter(user=request.user)
            .order_by("joined_at")
        )
        result = []
        for m in memberships:
            d              = OrganizationSerializer(m.organization).data
            d["role"]      = m.role
            d["is_current"] = m.organization.id == request.org.id
            result.append(d)
        return Response(result)


class OrganizationDetailView(OrgScopedMixin, APIView):
    """GET / PATCH / DELETE /api/orgs/<org_id>/"""

    permission_classes = [permissions.IsAuthenticated]

    def _get(self, request, org_id):
        try:
            return Membership.objects.select_related("organization").get(
                user=request.user, organization_id=org_id
            )
        except Membership.DoesNotExist:
            return None

    def get(self, request, org_id):
        m = self._get(request, org_id)
        if not m:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        data                = OrganizationSerializer(m.organization).data
        data["role"]        = m.role
        data["can_approve"] = m.can_approve
        return Response(data)

    def patch(self, request, org_id):
        m = self._get(request, org_id)
        if not m:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not can_manage_members(m):
            return Response(
                {"detail": "Only owners and admins can update workspace settings."},
                status=status.HTTP_403_FORBIDDEN,
            )
        s = OrganizationUpdateSerializer(m.organization, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        s.save()
        return Response(OrganizationSerializer(m.organization).data)

    def delete(self, request, org_id):
        m = self._get(request, org_id)
        if not m:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not can_delete_org(m):
            return Response(
                {"detail": "Only the owner can delete the workspace."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if m.organization.memberships.count() > 1:
            return Response(
                {"detail": "Remove all other members before deleting the workspace."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        m.organization.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Members ───────────────────────────────────────────────────────────────────

class MemberListView(OrgScopedMixin, APIView):
    """GET /api/orgs/<org_id>/members/"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, org_id):
        if not Membership.objects.filter(user=request.user, organization_id=org_id).exists():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        members = (
            Membership.objects
            .select_related("user", "invited_by")
            .filter(organization_id=org_id)
            .order_by("joined_at")
        )
        return Response(MembershipSerializer(members, many=True).data)


class MemberDetailView(OrgScopedMixin, APIView):
    """PATCH / DELETE /api/orgs/<org_id>/members/<membership_id>/"""

    permission_classes = [permissions.IsAuthenticated]

    def _get(self, request, org_id, mid):
        """Return (my_membership, target_membership) or (None, None)."""
        try:
            my = Membership.objects.get(user=request.user, organization_id=org_id)
        except Membership.DoesNotExist:
            return None, None
        try:
            target = Membership.objects.select_related("user", "organization").get(
                id=mid, organization_id=org_id
            )
        except Membership.DoesNotExist:
            return None, None
        return my, target

    def patch(self, request, org_id, membership_id):
        my, target = self._get(request, org_id, membership_id)
        if my is None or target is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not can_manage_members(my):
            return Response({"detail": "Insufficient permissions."}, status=status.HTTP_403_FORBIDDEN)
        if target.user_id == request.user.id:
            return Response(
                {"detail": "Use the transfer-ownership endpoint to change your own role."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        new_role    = request.data.get("role")
        can_approve = request.data.get("can_approve")

        if new_role is not None and not can_change_role_to(my, new_role):
            return Response(
                {"detail": f"You cannot assign the role '{new_role}'."},
                status=status.HTTP_403_FORBIDDEN,
            )

        update_fields = []
        old_role = target.role
        if new_role is not None:
            target.role = new_role
            update_fields.append("role")
        if can_approve is not None:
            target.can_approve = bool(can_approve)
            update_fields.append("can_approve")

        if update_fields:
            target.save(update_fields=update_fields)
            ActivityLog.objects.create(
                organization=target.organization,
                user=request.user,
                action=ActivityLog.Action.ROLE_CHANGED,
                target_user=target.user,
                metadata={"old_role": old_role, "new_role": target.role},
            )

        target.refresh_from_db()
        return Response(MembershipSerializer(target).data)

    def delete(self, request, org_id, membership_id):
        my, target = self._get(request, org_id, membership_id)
        if my is None or target is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        # Allow self-removal (leaving the org)
        if target.user_id == request.user.id:
            if target.role == "owner" and target.organization.memberships.filter(role="owner").count() == 1:
                return Response(
                    {"detail": "Transfer ownership before leaving the workspace."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            target.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        if not can_manage_members(my):
            return Response({"detail": "Insufficient permissions."}, status=status.HTTP_403_FORBIDDEN)
        if target.role == "owner":
            return Response({"detail": "Cannot remove the owner."}, status=status.HTTP_400_BAD_REQUEST)

        ActivityLog.objects.create(
            organization=target.organization,
            user=request.user,
            action=ActivityLog.Action.MEMBER_REMOVED,
            target_user=target.user,
            metadata={"email": target.user.email, "role": target.role},
        )
        target.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TransferOwnershipView(OrgScopedMixin, APIView):
    """POST /api/orgs/<org_id>/transfer-ownership/  Body: {new_owner_membership_id}"""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, org_id):
        try:
            my = Membership.objects.get(user=request.user, organization_id=org_id)
        except Membership.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if not can_transfer_ownership(my):
            return Response({"detail": "Only the owner can transfer ownership."}, status=status.HTTP_403_FORBIDDEN)

        new_owner_id = request.data.get("new_owner_membership_id")
        if not new_owner_id:
            return Response({"detail": "new_owner_membership_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            target = Membership.objects.select_related("user").get(
                id=new_owner_id, organization_id=org_id
            )
        except Membership.DoesNotExist:
            return Response({"detail": "Target member not found."}, status=status.HTTP_404_NOT_FOUND)

        if target.user_id == request.user.id:
            return Response({"detail": "You are already the owner."}, status=status.HTTP_400_BAD_REQUEST)

        # Demote current owner → admin, promote target → owner
        my.role = Membership.Role.ADMIN
        my.save(update_fields=["role"])
        target.role = Membership.Role.OWNER
        target.save(update_fields=["role"])

        ActivityLog.objects.create(
            organization=my.organization,
            user=request.user,
            action=ActivityLog.Action.ROLE_CHANGED,
            target_user=target.user,
            metadata={"transfer": True, "new_owner": target.user.email},
        )
        return Response({"detail": f"Ownership transferred to {target.user.email}."})


# ── Invitations ───────────────────────────────────────────────────────────────

class InvitationListView(OrgScopedMixin, APIView):
    """GET / POST /api/orgs/<org_id>/invitations/"""

    permission_classes = [permissions.IsAuthenticated]

    def _my(self, request, org_id):
        try:
            return Membership.objects.select_related("organization").get(
                user=request.user, organization_id=org_id
            )
        except Membership.DoesNotExist:
            return None

    def get(self, request, org_id):
        my = self._my(request, org_id)
        if not my:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not can_manage_members(my):
            return Response({"detail": "Insufficient permissions."}, status=status.HTTP_403_FORBIDDEN)

        invitations = Invitation.objects.filter(
            organization_id=org_id,
            accepted_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).order_by("-created_at")
        return Response(InvitationSerializer(invitations, many=True).data)

    def post(self, request, org_id):
        my = self._my(request, org_id)
        if not my:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not can_invite(my):
            return Response(
                {"detail": "Only owners and admins can invite members."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if _check_seat_limit(my.organization):
            limit = Organization.SEAT_LIMITS.get(my.organization.plan)
            return Response(
                {
                    "detail": f"Your {my.organization.plan} plan allows {limit} seat(s). Upgrade to invite more members.",
                    "code":   "seat_limit_exceeded",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        s = InviteCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        email = s.validated_data["email"]
        role  = s.validated_data["role"]

        # Already a member?
        try:
            existing_user = User.objects.get(email=email)
            if Membership.objects.filter(organization=my.organization, user=existing_user).exists():
                return Response(
                    {"detail": f"{email} is already a member of this workspace."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except User.DoesNotExist:
            pass

        expires_at = timezone.now() + timedelta(days=Invitation._EXPIRY_DAYS)

        # Upsert: refresh an expired/existing invite for the same email
        invitation, created = Invitation.objects.get_or_create(
            organization=my.organization,
            email=email,
            defaults={
                "role":       role,
                "invited_by": request.user,
                "expires_at": expires_at,
            },
        )
        if not created:
            invitation.role       = role
            invitation.token      = uuid.uuid4()
            invitation.expires_at = expires_at
            invitation.invited_by = request.user
            invitation.save(update_fields=["role", "token", "expires_at", "invited_by"])

        try:
            send_invitation_email(invitation)
        except Exception:
            pass  # Don't fail the request if email fails

        ActivityLog.objects.create(
            organization=my.organization,
            user=request.user,
            action=ActivityLog.Action.MEMBER_INVITED,
            metadata={"email": email, "role": role},
        )
        return Response(InvitationSerializer(invitation).data, status=status.HTTP_201_CREATED)


class InvitationDetailView(OrgScopedMixin, APIView):
    """DELETE /api/orgs/<org_id>/invitations/<inv_id>/"""

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, org_id, invitation_id):
        try:
            my = Membership.objects.get(user=request.user, organization_id=org_id)
        except Membership.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if not can_manage_members(my):
            return Response({"detail": "Insufficient permissions."}, status=status.HTTP_403_FORBIDDEN)

        try:
            inv = Invitation.objects.get(id=invitation_id, organization_id=org_id)
        except Invitation.DoesNotExist:
            return Response({"detail": "Invitation not found."}, status=status.HTTP_404_NOT_FOUND)

        inv.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ResendInvitationView(OrgScopedMixin, APIView):
    """POST /api/orgs/<org_id>/invitations/<inv_id>/resend/"""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, org_id, invitation_id):
        try:
            my = Membership.objects.get(user=request.user, organization_id=org_id)
        except Membership.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if not can_manage_members(my):
            return Response({"detail": "Insufficient permissions."}, status=status.HTTP_403_FORBIDDEN)

        try:
            inv = Invitation.objects.get(id=invitation_id, organization_id=org_id)
        except Invitation.DoesNotExist:
            return Response({"detail": "Invitation not found."}, status=status.HTTP_404_NOT_FOUND)

        inv.token      = uuid.uuid4()
        inv.expires_at = timezone.now() + timedelta(days=Invitation._EXPIRY_DAYS)
        inv.save(update_fields=["token", "expires_at"])

        try:
            send_invitation_email(inv)
        except Exception:
            return Response(
                {"detail": "Failed to send email. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(InvitationSerializer(inv).data)


# ── Public invitation acceptance ──────────────────────────────────────────────

class PublicInvitationView(APIView):
    """GET /api/invitations/<token>/  — public; used to render acceptance page."""

    permission_classes = [permissions.AllowAny]

    def get(self, request, token):
        try:
            inv = Invitation.objects.select_related("organization", "invited_by").get(token=token)
        except (Invitation.DoesNotExist, ValueError):
            return Response({"detail": "Invalid invitation link."}, status=status.HTTP_404_NOT_FOUND)
        return Response(PublicInvitationSerializer(inv).data)


class AcceptInvitationView(APIView):
    """POST /api/invitations/<token>/accept/  — must be authenticated."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, token):
        try:
            inv = Invitation.objects.select_related("organization").get(token=token)
        except Invitation.DoesNotExist:
            return Response({"detail": "Invalid invitation link."}, status=status.HTTP_404_NOT_FOUND)

        if inv.is_expired:
            return Response({"detail": "This invitation has expired."}, status=status.HTTP_400_BAD_REQUEST)

        if inv.accepted_at is not None:
            return Response({"detail": "This invitation has already been used."}, status=status.HTTP_400_BAD_REQUEST)

        if request.user.email.lower() != inv.email.lower():
            return Response(
                {"detail": f"This invitation was sent to {inv.email}. Please sign in with that email."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if Membership.objects.filter(organization=inv.organization, user=request.user).exists():
            return Response(
                {"detail": "You are already a member of this workspace."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        Membership.objects.create(
            organization=inv.organization,
            user=request.user,
            role=inv.role,
            invited_by=inv.invited_by,
        )

        inv.accepted_at = timezone.now()
        inv.save(update_fields=["accepted_at"])

        ActivityLog.objects.create(
            organization=inv.organization,
            user=request.user,
            action=ActivityLog.Action.MEMBER_JOINED,
            metadata={
                "role":       inv.role,
                "invited_by": inv.invited_by.email if inv.invited_by else "",
            },
        )
        return Response(
            {
                "detail": f"You've joined {inv.organization.name}.",
                "org_id": inv.organization.id,
                "org_name": inv.organization.name,
            }
        )


# ── Activity log ──────────────────────────────────────────────────────────────

class ActivityLogView(OrgScopedMixin, APIView):
    """GET /api/orgs/<org_id>/activity/"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, org_id):
        try:
            my = Membership.objects.get(user=request.user, organization_id=org_id)
        except Membership.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if not can_manage_members(my):
            return Response({"detail": "Insufficient permissions."}, status=status.HTTP_403_FORBIDDEN)

        logs = (
            ActivityLog.objects
            .select_related("user", "target_user", "invoice")
            .filter(organization_id=org_id)[:50]
        )
        return Response(ActivityLogSerializer(logs, many=True).data)
