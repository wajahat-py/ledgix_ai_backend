"""
Permission helpers for org-scoped operations.
Each function takes a Membership instance and returns a bool.
"""

from __future__ import annotations

from .models import Membership

_RANK = Membership.ROLE_RANK


def can_upload(m: Membership) -> bool:
    return m.role in ("owner", "admin", "member")


def can_process(m: Membership) -> bool:
    return m.role in ("owner", "admin", "member")


def can_approve(m: Membership) -> bool:
    return m.role in ("owner", "admin") or (m.role == "member" and m.can_approve)


def can_delete_any(m: Membership) -> bool:
    """Admins and owners can delete any invoice in the org."""
    return m.role in ("owner", "admin")


def can_delete_own(m: Membership) -> bool:
    """Members can delete only their own invoices."""
    return m.role in ("owner", "admin", "member")


def can_invite(m: Membership) -> bool:
    return m.role in ("owner", "admin")


def can_manage_members(m: Membership) -> bool:
    return m.role in ("owner", "admin")


def can_change_role_to(m: Membership, target_role: str) -> bool:
    """Can `m` assign `target_role` to another user?"""
    if m.role == "owner":
        return target_role in ("admin", "member", "viewer")  # can't assign owner via this path
    if m.role == "admin":
        return _RANK.get(target_role, 0) < _RANK["admin"]
    return False


def can_manage_billing(m: Membership) -> bool:
    return m.role == "owner"


def can_delete_org(m: Membership) -> bool:
    return m.role == "owner"


def can_transfer_ownership(m: Membership) -> bool:
    return m.role == "owner"


def is_payment_required(org) -> bool:
    """Return True if the org has signed up for Pro but hasn't paid yet."""
    from .models import Organization
    return org.intended_plan == Organization.Plan.PRO and org.plan == Organization.Plan.FREE
