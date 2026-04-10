import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import PendingRegistration

logger = logging.getLogger(__name__)


@shared_task
def delete_unverified_users() -> dict:
    """
    Clear out pending registrations older than 7 days so emails can be reused.
    """
    cutoff = timezone.now() - timedelta(days=7)
    qs = PendingRegistration.objects.filter(created_at__lt=cutoff)
    count = qs.count()
    qs.delete()
    logger.info("Deleted %d stale pending registration(s)", count)
    return {"deleted": count}
