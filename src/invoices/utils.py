from django.utils import timezone

FREE_PLAN_LIMIT = 50


def monthly_invoice_count(user) -> int:
    """Return the number of invoices the user has created in the current calendar month."""
    from .models import Invoice
    now = timezone.now()
    return Invoice.objects.filter(
        user=user,
        created_at__year=now.year,
        created_at__month=now.month,
    ).count()
