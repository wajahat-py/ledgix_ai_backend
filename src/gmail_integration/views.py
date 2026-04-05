import logging

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from invoices.utils import FREE_PLAN_LIMIT, monthly_invoice_count
from .models import GmailIntegration, GmailSyncedMessage
from .serializers import GmailStatusSerializer, GmailSyncedMessageSerializer
from .service import exchange_code_and_save, get_oauth_url, get_gmail_service, revoke_token

logger = logging.getLogger(__name__)


class GmailAuthView(APIView):
    """
    GET /api/gmail/auth/
    Returns a Google OAuth2 consent URL.  The frontend should navigate
    (or redirect) the user to that URL to begin the auth flow.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        auth_url = get_oauth_url(request.user)
        return Response({"auth_url": auth_url})


class GmailCallbackView(APIView):
    """
    GET /api/gmail/callback/
    Handles the redirect from Google after the user grants (or denies) access.
    This endpoint is unauthenticated — user identity is encoded in the *state*
    parameter signed by Django's cryptographic signing module.
    """

    permission_classes = []
    authentication_classes = []

    def get(self, request):
        frontend_url = settings.FRONTEND_URL
        error        = request.query_params.get("error")
        code         = request.query_params.get("code")
        state        = request.query_params.get("state")

        if error:
            logger.warning("Gmail OAuth denied by user: %s", error)
            return redirect(f"{frontend_url}/email?gmail=error&reason={error}")

        if not code or not state:
            return redirect(f"{frontend_url}/email?gmail=error&reason=missing_params")

        try:
            integration = exchange_code_and_save(code, state)
        except ValueError as exc:
            logger.warning("Gmail callback state error: %s", exc)
            return redirect(f"{frontend_url}/email?gmail=error&reason=invalid_state")
        except Exception as exc:
            logger.exception("Gmail callback unexpected error: %s", exc)
            return redirect(f"{frontend_url}/email?gmail=error&reason=server_error")

        # Kick off the initial inbox scan and register the push watch
        from .tasks import setup_watch_for_user, sync_gmail_invoices
        sync_gmail_invoices.delay(integration.user_id)
        setup_watch_for_user.delay(integration.user_id)

        return redirect(f"{frontend_url}/email?gmail=connected")


class GmailStatusView(APIView):
    """
    GET /api/gmail/status/
    Returns the connection status and recent imports for the authenticated user.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            integration = GmailIntegration.objects.get(user=request.user)
        except GmailIntegration.DoesNotExist:
            return Response({"connected": False})

        data = GmailStatusSerializer(integration).data
        data["connected"] = integration.is_active
        return Response(data)


class GmailSyncView(APIView):
    """
    POST /api/gmail/sync/
    Manually trigger a Gmail inbox scan for the authenticated user.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            integration = GmailIntegration.objects.get(
                user=request.user, is_active=True
            )
        except GmailIntegration.DoesNotExist:
            return Response(
                {"detail": "Gmail is not connected."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if monthly_invoice_count(request.user) >= FREE_PLAN_LIMIT:
            return Response(
                {
                    "detail": f"You've reached your monthly limit of {FREE_PLAN_LIMIT} invoices. Upgrade to sync more.",
                    "code": "plan_limit_exceeded",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        from .tasks import sync_gmail_invoices
        sync_gmail_invoices.delay(integration.user_id)
        return Response({"detail": "Sync started. New invoices will appear shortly."})


class GmailDisconnectView(APIView):
    """
    DELETE /api/gmail/disconnect/
    Revokes the OAuth token and removes the integration record.
    """

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request):
        try:
            integration = GmailIntegration.objects.get(user=request.user)
        except GmailIntegration.DoesNotExist:
            return Response(
                {"detail": "Gmail is not connected."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from .service import stop_gmail_watch
        try:
            service = get_gmail_service(integration)
            stop_gmail_watch(service)
        except Exception:
            pass  # Best-effort — proceed with deletion regardless

        revoke_token(integration.access_token)
        integration.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class GmailMessageDetailView(APIView):
    """
    GET /api/gmail/message/<message_id>/
    Returns the subject, sender, date and plain-text body of one email.
    Only accessible for messages that belong to the authenticated user.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, message_id):
        try:
            integration = GmailIntegration.objects.get(user=request.user, is_active=True)
        except GmailIntegration.DoesNotExist:
            return Response({"detail": "Gmail not connected."}, status=status.HTTP_400_BAD_REQUEST)

        if not GmailSyncedMessage.objects.filter(integration=integration, message_id=message_id).exists():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        from .service import extract_message_body, get_message_header
        service = get_gmail_service(integration)
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()

        return Response({
            "subject": get_message_header(msg, "Subject"),
            "from":    get_message_header(msg, "From"),
            "date":    get_message_header(msg, "Date"),
            "body":    extract_message_body(msg),
        })


class GmailAttachmentProxyView(APIView):
    """
    GET /api/gmail/attachment/?mid=<message_id>&aid=<attachment_id>
    Streams the attachment bytes back to the browser with the correct
    Content-Type so the frontend can create a blob URL for preview.
    Falls back to Gmail API download if the invoice file is unavailable.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        import mimetypes
        from .service import download_attachment

        message_id    = request.query_params.get("mid", "")
        attachment_id = request.query_params.get("aid", "")

        if not message_id or not attachment_id:
            return Response({"detail": "mid and aid are required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            synced = GmailSyncedMessage.objects.select_related(
                "integration", "invoice"
            ).get(
                integration__user=request.user,
                message_id=message_id,
                attachment_id=attachment_id,
            )
        except GmailSyncedMessage.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        # Serve from the already-stored invoice file when possible
        if synced.invoice_id and synced.invoice.file:
            try:
                with synced.invoice.file.open("rb") as fh:
                    data = fh.read()
                content_type = (
                    mimetypes.guess_type(synced.attachment_filename)[0]
                    or "application/octet-stream"
                )
                resp = HttpResponse(data, content_type=content_type)
                resp["Content-Disposition"] = f'inline; filename="{synced.attachment_filename}"'
                return resp
            except Exception:
                pass  # Fall through to Gmail API

        # Download fresh from Gmail
        service = get_gmail_service(synced.integration)
        data = download_attachment(service, message_id, attachment_id)
        content_type = (
            mimetypes.guess_type(synced.attachment_filename)[0]
            or "application/octet-stream"
        )
        resp = HttpResponse(data, content_type=content_type)
        resp["Content-Disposition"] = f'inline; filename="{synced.attachment_filename}"'
        return resp


class GmailRetryView(APIView):
    """
    POST /api/gmail/retry/<int:synced_message_id>/
    Re-attempts invoice creation for a synced message where invoice_detected
    is True but the invoice record was never linked (download/creation failed).
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, synced_message_id):
        from django.core.files.base import ContentFile
        from invoices.models import Invoice
        from invoices.tasks import process_invoice as enqueue_processing
        from .service import download_attachment

        try:
            synced = GmailSyncedMessage.objects.select_related("integration").get(
                id=synced_message_id,
                integration__user=request.user,
            )
        except GmailSyncedMessage.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if synced.invoice_id:
            return Response({"detail": "Invoice already linked."}, status=status.HTTP_400_BAD_REQUEST)

        if monthly_invoice_count(request.user) >= FREE_PLAN_LIMIT:
            return Response(
                {
                    "detail": f"You've reached your monthly limit of {FREE_PLAN_LIMIT} invoices. Upgrade to import more.",
                    "code": "plan_limit_exceeded",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            service = get_gmail_service(synced.integration)
            data    = download_attachment(service, synced.message_id, synced.attachment_id)
        except Exception as exc:
            logger.error("Retry download failed for synced_msg %s: %s", synced_message_id, exc)
            return Response({"detail": "Could not download attachment."}, status=status.HTTP_502_BAD_GATEWAY)

        try:
            invoice = Invoice.objects.create(
                user=request.user,
                file=ContentFile(data, name=synced.attachment_filename),
                original_filename=synced.attachment_filename,
                status=Invoice.Status.UPLOADED,
            )
            enqueue_processing.delay(invoice.id)
            synced.invoice          = invoice
            synced.invoice_detected = True
            synced.save(update_fields=["invoice", "invoice_detected"])
        except Exception as exc:
            logger.error("Retry invoice creation failed for synced_msg %s: %s", synced_message_id, exc)
            return Response({"detail": "Could not create invoice."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(GmailSyncedMessageSerializer(synced).data, status=status.HTTP_200_OK)


class GmailWatchView(APIView):
    """
    GET  /api/gmail/watch/  — returns current watch status (expiry, topic).
    POST /api/gmail/watch/  — registers / refreshes the push watch immediately
                              and returns the raw API response so you can see
                              errors without waiting for a Celery task.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            integration = GmailIntegration.objects.get(user=request.user, is_active=True)
        except GmailIntegration.DoesNotExist:
            return Response({"detail": "Gmail not connected."}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "watch_expiry": integration.watch_expiry,
            "topic":        settings.GMAIL_PUBSUB_TOPIC or None,
        })

    def post(self, request):
        from datetime import datetime, timezone as dt_timezone
        from .service import setup_gmail_watch

        try:
            integration = GmailIntegration.objects.get(user=request.user, is_active=True)
        except GmailIntegration.DoesNotExist:
            return Response({"detail": "Gmail not connected."}, status=status.HTTP_400_BAD_REQUEST)

        topic = settings.GMAIL_PUBSUB_TOPIC
        if not topic:
            return Response(
                {"detail": "GMAIL_PUBSUB_TOPIC is not configured on the server."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        try:
            service = get_gmail_service(integration)
            result  = setup_gmail_watch(service, topic)
        except Exception as exc:
            logger.error("GmailWatchView: watch registration failed: %s", exc)
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        expiry_ms = int(result.get("expiration", 0))
        if expiry_ms:
            integration.watch_expiry = datetime.fromtimestamp(expiry_ms / 1000, tz=dt_timezone.utc)
            integration.save(update_fields=["watch_expiry"])

        return Response({
            "ok":          True,
            "topic":       topic,
            "history_id":  result.get("historyId"),
            "watch_expiry": integration.watch_expiry,
            "raw":         result,
        })


class GmailPubSubView(APIView):
    """
    POST /api/gmail/pubsub/
    Receives Google Cloud Pub/Sub push messages for Gmail push notifications.
    No authentication — Google signs delivery via HTTPS and the subscription
    URL is kept private.  We validate the message structure and queue a sync.

    Pub/Sub message shape:
      {
        "message": {
          "data": "<base64-encoded JSON>",   # {"emailAddress": "...", "historyId": "..."}
          "messageId": "...",
          "publishTime": "..."
        },
        "subscription": "..."
      }
    """

    permission_classes = []
    authentication_classes = []

    def post(self, request):
        import base64
        import json

        body = request.data
        message = body.get("message", {})
        raw_data = message.get("data", "")

        if not raw_data:
            # Pub/Sub requires a 2xx response even for malformed messages,
            # otherwise it retries indefinitely.
            logger.warning("GmailPubSubView: empty data field in Pub/Sub message")
            return Response(status=status.HTTP_204_NO_CONTENT)

        try:
            padded = raw_data + "=" * (-len(raw_data) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        except Exception as exc:
            logger.warning("GmailPubSubView: could not decode message data: %s", exc)
            return Response(status=status.HTTP_204_NO_CONTENT)

        email_address = payload.get("emailAddress", "")
        history_id    = payload.get("historyId", "")

        logger.info(
            "GmailPubSubView: notification for %s historyId=%s",
            email_address, history_id,
        )

        if not email_address:
            return Response(status=status.HTTP_204_NO_CONTENT)

        try:
            integration = GmailIntegration.objects.get(
                gmail_address=email_address, is_active=True
            )
        except GmailIntegration.DoesNotExist:
            logger.warning(
                "GmailPubSubView: no active integration for %s", email_address
            )
            return Response(status=status.HTTP_204_NO_CONTENT)

        from .tasks import sync_gmail_invoices
        sync_gmail_invoices.delay(integration.user_id)

        return Response(status=status.HTTP_204_NO_CONTENT)
