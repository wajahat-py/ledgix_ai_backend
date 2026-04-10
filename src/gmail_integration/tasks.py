import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def sync_gmail_invoices(self, user_id: int) -> dict:
    """
    Scan the user's Gmail inbox for invoice attachments, create Invoice
    records for newly-detected ones, and enqueue AI extraction.

    Each (message_id, attachment_id) pair is tracked in GmailSyncedMessage
    so re-runs are idempotent — already-processed attachments are skipped.
    """
    from django.core.files.base import ContentFile

    from invoices.models import Invoice
    from invoices.tasks import process_invoice as enqueue_processing
    from invoices.utils import invoice_limit_for_org, monthly_invoice_count
    from organizations.mixins import get_or_create_personal_org

    from .models import GmailIntegration, GmailSyncedMessage
    from .service import (
        MAX_ATTACHMENT_BYTES,
        collect_attachment_parts,
        download_attachment,
        get_gmail_service,
        get_message_header,
        get_profile,
        is_likely_invoice,
        list_messages,
        list_new_message_ids_since,
        parse_email_date,
    )

    try:
        integration = GmailIntegration.objects.get(user_id=user_id, is_active=True)
    except GmailIntegration.DoesNotExist:
        logger.warning("sync_gmail_invoices: no active integration for user %s", user_id)
        return {"error": "No active Gmail integration"}

    org, _ = get_or_create_personal_org(integration.user)
    invoice_limit = invoice_limit_for_org(org)

    try:
        service = get_gmail_service(integration)

        # ── Incremental sync via history API ─────────────────────────────────
        # On the first run (no history_id stored) we do a full scan.
        # On subsequent runs we ask Gmail for only the messages added since the
        # last sync, making it cheap enough to poll every few minutes.
        if integration.history_id:
            new_ids = list_new_message_ids_since(service, integration.history_id)
            if new_ids is None:
                # history_id expired — fall back to full scan
                message_stubs = list_messages(service, max_results=200)
            else:
                message_stubs = [{"id": mid} for mid in new_ids]
        else:
            message_stubs = list_messages(service, max_results=200)

    except Exception as exc:
        logger.exception("sync_gmail_invoices: failed to list messages for user %s", user_id)
        raise self.retry(exc=exc)

    results = {
        "invoices_created": 0,
        "attachments_scanned": 0,
        "skipped_already_seen": 0,
        "skipped_not_invoice": 0,
        "errors": [],
    }

    for stub in message_stubs:
        message_id = stub["id"]

        try:
            msg = service.users().messages().get(
                userId="me", id=message_id, format="full"
            ).execute()
        except Exception as exc:
            logger.warning("Could not fetch message %s: %s", message_id, exc)
            continue

        subject    = get_message_header(msg, "Subject")
        sender     = get_message_header(msg, "From")
        received_at = parse_email_date(get_message_header(msg, "Date"))

        for part in collect_attachment_parts(msg.get("payload", {})):
            filename      = part.get("filename", "")
            mime_type     = part.get("mimeType", "")
            attachment_id = part.get("body", {}).get("attachmentId", "")
            size          = part.get("body", {}).get("size", 0)

            if not filename or not attachment_id:
                continue

            # Idempotency: skip if we have already handled this attachment
            synced_msg, created = GmailSyncedMessage.objects.get_or_create(
                integration=integration,
                message_id=message_id,
                attachment_id=attachment_id,
                defaults={
                    "subject":             subject[:500],
                    "sender":              sender[:500],
                    "received_at":         received_at,
                    "attachment_filename": filename[:255],
                    "invoice_detected":    False,
                },
            )

            if not created:
                results["skipped_already_seen"] += 1
                continue

            results["attachments_scanned"] += 1

            # Skip oversized attachments
            if size > MAX_ATTACHMENT_BYTES:
                results["skipped_not_invoice"] += 1
                continue

            # Heuristic check
            if not is_likely_invoice(filename, mime_type, subject, sender):
                results["skipped_not_invoice"] += 1
                continue

            # Download the attachment bytes first — if this fails we leave
            # invoice_detected=False so the record doesn't appear as stuck.
            try:
                data = download_attachment(service, message_id, attachment_id)
            except Exception as exc:
                logger.error("Download failed for attachment %s: %s", attachment_id, exc)
                results["errors"].append(f"download:{attachment_id}: {exc}")
                continue

            # Check plan limit *before* creating the invoice.
            # If at limit, we delete the synced_msg record and BREAK.
            # This "pauses" the sync for this user until they upgrade or the 
            # next month starts, without updating the history_id.
            if invoice_limit is not None and monthly_invoice_count(org) >= invoice_limit:
                results["errors"].append("plan_limit_exceeded")
                logger.info(
                    "Gmail sync user %s: plan limit %s reached. Pausing sync.",
                    user_id, invoice_limit
                )
                synced_msg.delete()
                # Stop processing this user for now. We skip the history_id 
                # update at the end by returning early.
                return results

            # Create the Invoice record, then mark as detected only on success.
            try:
                from invoices.tasks import _push_update
                invoice = Invoice.objects.create(
                    user_id=user_id,
                    organization=org,
                    file=ContentFile(data, name=filename),
                    original_filename=filename,
                    status=Invoice.Status.UPLOADED,
                )
                # Push immediately so the frontend shows the new row before
                # Celery even picks up the processing task.
                _push_update(invoice)
                enqueue_processing.delay(invoice.id)

                synced_msg.invoice_detected = True
                synced_msg.invoice = invoice
                synced_msg.save(update_fields=["invoice_detected", "invoice"])

                results["invoices_created"] += 1
                logger.info(
                    "Created invoice %s from Gmail message %s (%s)",
                    invoice.id, message_id, filename,
                )
            except Exception as exc:
                logger.error(
                    "Invoice creation failed for attachment %s: %s", attachment_id, exc
                )
                results["errors"].append(f"create:{attachment_id}: {exc}")

    # Persist the current history ID so the next run is incremental.
    try:
        profile = get_profile(service)
        integration.history_id = str(profile.get("historyId", ""))
    except Exception:
        pass  # Non-fatal — next run will fall back to full scan

    integration.last_synced_at = timezone.now()
    integration.save(update_fields=["last_synced_at", "history_id"])

    logger.info(
        "Gmail sync user=%s created=%d scanned=%d skipped=%d errors=%d",
        user_id,
        results["invoices_created"],
        results["attachments_scanned"],
        results["skipped_already_seen"] + results["skipped_not_invoice"],
        len(results["errors"]),
    )
    return results


@shared_task
def setup_watch_for_user(user_id: int) -> None:
    """
    Register (or refresh) a Gmail push-notification watch for one user so
    Google sends Pub/Sub messages whenever new mail arrives in their INBOX.
    """
    from datetime import datetime, timezone as dt_timezone

    from django.conf import settings

    from .models import GmailIntegration
    from .service import get_gmail_service, setup_gmail_watch

    try:
        integration = GmailIntegration.objects.get(user_id=user_id, is_active=True)
    except GmailIntegration.DoesNotExist:
        logger.warning("setup_watch_for_user: no active integration for user %s", user_id)
        return

    topic = getattr(settings, "GMAIL_PUBSUB_TOPIC", "")
    if not topic:
        logger.warning("setup_watch_for_user: GMAIL_PUBSUB_TOPIC not configured")
        return

    try:
        service = get_gmail_service(integration)
        result  = setup_gmail_watch(service, topic)
        expiry_ms = int(result.get("expiration", 0))
        if expiry_ms:
            integration.watch_expiry = datetime.fromtimestamp(expiry_ms / 1000, tz=dt_timezone.utc)
            integration.save(update_fields=["watch_expiry"])
        logger.info("Gmail watch registered for user %s (expires %s)", user_id, integration.watch_expiry)
    except Exception as exc:
        logger.error("setup_watch_for_user failed for user %s: %s", user_id, exc)


@shared_task
def renew_expiring_watches() -> None:
    """
    Periodic task — renew Gmail push watches that expire within the next 24 h.
    Gmail watches last at most 7 days; this keeps them perpetually active.
    """
    from datetime import datetime, timedelta, timezone as dt_timezone

    from django.conf import settings

    from .models import GmailIntegration
    from .service import get_gmail_service, setup_gmail_watch

    topic = getattr(settings, "GMAIL_PUBSUB_TOPIC", "")
    if not topic:
        return

    cutoff = timezone.now() + timedelta(hours=24)
    # Also include integrations where the watch was never registered (NULL)
    from django.db.models import Q
    qs = GmailIntegration.objects.filter(
        is_active=True
    ).filter(Q(watch_expiry__isnull=True) | Q(watch_expiry__lt=cutoff))

    for integration in qs:
        try:
            service   = get_gmail_service(integration)
            result    = setup_gmail_watch(service, topic)
            expiry_ms = int(result.get("expiration", 0))
            if expiry_ms:
                integration.watch_expiry = datetime.fromtimestamp(expiry_ms / 1000, tz=dt_timezone.utc)
                integration.save(update_fields=["watch_expiry"])
            logger.info("Renewed Gmail watch for user %s", integration.user_id)
        except Exception as exc:
            logger.error("renew watch failed for user %s: %s", integration.user_id, exc)


@shared_task
def sync_all_active_integrations() -> None:
    """
    Periodic task — enqueue sync_gmail_invoices for every active integration.
    Run the beat worker alongside Celery to use this:
      celery -A config beat --loglevel=info
    """
    from django.conf import settings
    from .models import GmailIntegration

    integrations = list(GmailIntegration.objects.filter(is_active=True))
    for integration in integrations:
        sync_gmail_invoices.delay(integration.user_id)
        # Register the push watch if it has never been set up for this integration
        if not integration.watch_expiry and getattr(settings, "GMAIL_PUBSUB_TOPIC", ""):
            setup_watch_for_user.delay(integration.user_id)

    logger.info("Enqueued Gmail sync for %d integration(s)", len(integrations))
