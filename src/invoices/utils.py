from django.utils import timezone

FREE_PLAN_LIMIT = 50
PRO_PLAN_LIMIT = 500

PLAN_LIMITS: dict[str, int | None] = {
    "free": FREE_PLAN_LIMIT,
    "pro": PRO_PLAN_LIMIT,
    "business": None,
}


def monthly_invoice_count(organization) -> int:
    """Count invoices for an org in the current calendar month."""
    from .models import Invoice

    now = timezone.now()
    return Invoice.objects.filter(
        organization=organization,
        created_at__year=now.year,
        created_at__month=now.month,
    ).count()


def invoice_limit_for_plan(plan: str) -> int | None:
    return PLAN_LIMITS.get(plan, FREE_PLAN_LIMIT)


def invoice_limit_for_org(organization) -> int | None:
    return invoice_limit_for_plan(getattr(organization, "plan", "free"))


def remaining_invoice_capacity(organization) -> int | None:
    limit = invoice_limit_for_org(organization)
    if limit is None:
        return None
    return max(limit - monthly_invoice_count(organization), 0)
