import logging
from datetime import timedelta

from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils import timezone

logger = logging.getLogger(__name__)
User = get_user_model()


@shared_task
def delete_unverified_users() -> dict:
    """
    Periodic task — delete unverified accounts older than 7 days.
    This keeps the DB clean and lets someone re-register with the same
    email after an abandoned signup.
    """
    cutoff = timezone.now() - timedelta(days=7)
    qs = User.objects.filter(is_email_verified=False, date_joined__lt=cutoff)
    count = qs.count()
    qs.delete()
    logger.info("Deleted %d stale unverified user(s)", count)
    return {"deleted": count}
