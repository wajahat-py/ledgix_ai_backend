import logging

from rest_framework.exceptions import PermissionDenied

logger = logging.getLogger(__name__)


def get_or_create_personal_org(user):
    """
    Return (org, membership) for a user's primary org.
    Creates a personal workspace if the user has none — this is a safety
    fallback; normally orgs are created at account-creation time.
    """
    from .models import Membership, Organization, _unique_slug

    membership = (
        Membership.objects
        .select_related("organization")
        .filter(user=user)
        .order_by("joined_at")
        .first()
    )
    if membership:
        return membership.organization, membership

    name = f"{user.first_name or user.email.split('@')[0]}'s Workspace"
    org  = Organization.objects.create(name=name, slug=_unique_slug(name))
    membership = Membership.objects.create(
        organization=org,
        user=user,
        role=Membership.Role.OWNER,
    )
    logger.info("Auto-created personal org '%s' for user %s", name, user.email)
    return org, membership


class OrgScopedMixin:
    """
    DRF APIView mixin that attaches request.org and request.membership.

    Reads an optional X-Organization-Id header so multi-org users can
    target a specific workspace.  Falls back to the user's first membership.
    """

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)

        if not request.user or not request.user.is_authenticated:
            return

        from .models import Membership

        org_id_header = request.META.get("HTTP_X_ORGANIZATION_ID")
        qs = Membership.objects.select_related("organization").filter(user=request.user)

        if org_id_header:
            try:
                membership = qs.get(organization_id=int(org_id_header))
            except (Membership.DoesNotExist, ValueError, TypeError):
                raise PermissionDenied("You are not a member of the specified organization.")
        else:
            membership = qs.order_by("joined_at").first()
            if not membership:
                _, membership = get_or_create_personal_org(request.user)
                membership = (
                    Membership.objects
                    .select_related("organization")
                    .get(pk=membership.pk)
                )

        request.org        = membership.organization
        request.membership = membership

        # Enforce payment for Pro-intended orgs that are still on Free.
        # Skip this check for billing views, since they need to create the session.
        from .permissions import is_payment_required
        if is_payment_required(request.org):
            # Allow billing views and logout-related things? 
            # Actually just checking if the view belongs to the billing app.
            if request.resolver_match and request.resolver_match.app_name == "billing":
                return
            
            # If we're here, it's not billing. Block.
            raise PermissionDenied(
                "Your workspace requires a Pro subscription. Please complete your payment."
            )
