import logging

from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from organizations.mixins import OrgScopedMixin
from organizations.models import ActivityLog
from organizations.permissions import (
    can_approve, can_delete_any, can_delete_own,
    can_process, can_upload,
)

from .dashboard import compute_dashboard
from .models import DuplicateCheckResult, Invoice, Notification
from .serializers import InvoiceSerializer, NotificationSerializer
from .tasks import check_invoice_duplicates, process_invoice
from .utils import invoice_limit_for_org, monthly_invoice_count

logger = logging.getLogger(__name__)

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/tiff",
}
MAX_FILE_SIZE_BYTES    = 10 * 1024 * 1024  # 10 MB
MAX_FILES_PER_REQUEST  = 10


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_invoice(pk: int, org) -> Invoice | None:
    """Return invoice scoped to org, or None."""
    try:
        return (
            Invoice.objects
            .select_related("duplicate_check", "duplicate_check__best_match")
            .get(pk=pk, organization=org)
        )
    except Invoice.DoesNotExist:
        return None


# ── Invoice list & upload ─────────────────────────────────────────────────────

class InvoiceListView(OrgScopedMixin, APIView):
    """GET /api/invoices/ — all org invoices."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        invoices = (
            Invoice.objects
            .filter(organization=request.org)
            .select_related("duplicate_check", "duplicate_check__best_match",
                            "user", "approved_by", "rejected_by")
            .order_by("-created_at")
        )
        return Response(InvoiceSerializer(invoices, many=True, context={"request": request}).data)


class InvoiceUploadView(OrgScopedMixin, APIView):
    """POST /api/invoices/upload/"""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes     = [MultiPartParser]

    def post(self, request):
        if not can_upload(request.membership):
            return Response(
                {"detail": "Viewers cannot upload invoices."},
                status=status.HTTP_403_FORBIDDEN,
            )

        files = request.FILES.getlist("files")
        if not files:
            return Response(
                {"detail": "No files provided. Send files under the 'files' field."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(files) > MAX_FILES_PER_REQUEST:
            return Response(
                {"detail": f"Maximum {MAX_FILES_PER_REQUEST} files per request."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        current_count = monthly_invoice_count(request.org)
        invoice_limit = invoice_limit_for_org(request.org)
        remaining = None if invoice_limit is None else invoice_limit - current_count
        if invoice_limit is not None and remaining is not None and remaining <= 0:
            return Response(
                {
                    "detail": f"Monthly limit of {invoice_limit} invoices reached. Upgrade to continue.",
                    "code":          "plan_limit_exceeded",
                    "invoice_count": current_count,
                    "invoice_limit": invoice_limit,
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if invoice_limit is not None and remaining is not None and len(files) > remaining:
            return Response(
                {
                    "detail": f"Only {remaining} invoice(s) left this month (limit: {invoice_limit}).",
                    "code":          "plan_limit_exceeded",
                    "invoice_count": current_count,
                    "invoice_limit": invoice_limit,
                    "remaining":     remaining,
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        for f in files:
            if f.content_type not in ALLOWED_MIME_TYPES:
                return Response(
                    {"detail": f"'{f.name}': unsupported type '{f.content_type}'. Accepted: PDF, JPEG, PNG, WebP, TIFF."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            if f.size > MAX_FILE_SIZE_BYTES:
                return Response(
                    {"detail": f"'{f.name}' exceeds the 10 MB limit."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

        created = []
        for f in files:
            inv = Invoice.objects.create(
                user=request.user,
                organization=request.org,
                file=f,
                original_filename=f.name,
            )
            created.append(inv)
            ActivityLog.objects.create(
                organization=request.org,
                user=request.user,
                action=ActivityLog.Action.INVOICE_UPLOADED,
                invoice=inv,
                metadata={"filename": f.name},
            )
            logger.info("Saved invoice %s for org %s by user %s", inv.id, request.org.id, request.user.id)

        return Response(
            InvoiceSerializer(created, many=True, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


# ── Invoice detail ────────────────────────────────────────────────────────────

class InvoiceDetailView(OrgScopedMixin, APIView):
    """GET / PATCH / DELETE /api/invoices/<pk>/"""

    permission_classes = [permissions.IsAuthenticated]

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Invoice.Status.PROCESSED:      {Invoice.Status.PENDING_REVIEW, Invoice.Status.APPROVED, Invoice.Status.REJECTED},
        Invoice.Status.PENDING_REVIEW: {Invoice.Status.APPROVED, Invoice.Status.REJECTED},
    }
    EDITABLE_STATUSES = {Invoice.Status.PROCESSED, Invoice.Status.PENDING_REVIEW}
    APPROVAL_STATUSES = {Invoice.Status.APPROVED, Invoice.Status.REJECTED}

    def get(self, request, pk):
        invoice = _get_invoice(pk, request.org)
        if not invoice:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(InvoiceSerializer(invoice, context={"request": request}).data)

    def patch(self, request, pk):
        try:
            invoice = Invoice.objects.get(pk=pk, organization=request.org)
        except Invoice.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        new_status       = request.data.get("status")
        new_extracted    = request.data.get("extracted_data")
        rejection_reason = request.data.get("rejection_reason", "")

        if new_status is None and new_extracted is None:
            return Response(
                {"detail": "Provide 'status' and/or 'extracted_data'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        update_fields = ["updated_at"]

        if new_status is not None:
            allowed = self.ALLOWED_TRANSITIONS.get(invoice.status, set())
            if new_status not in allowed:
                return Response(
                    {"detail": f"Cannot transition from '{invoice.status}' to '{new_status}'."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

            # Approve / reject requires permission
            if new_status in self.APPROVAL_STATUSES and not can_approve(request.membership):
                return Response(
                    {"detail": "You don't have permission to approve or reject invoices."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            invoice.status = new_status
            update_fields.append("status")

            if new_status == Invoice.Status.APPROVED:
                invoice.approved_by  = request.user
                invoice.reviewed_at  = timezone.now()
                update_fields += ["approved_by", "reviewed_at"]
                ActivityLog.objects.create(
                    organization=request.org,
                    user=request.user,
                    action=ActivityLog.Action.INVOICE_APPROVED,
                    invoice=invoice,
                )
                try:
                    from organizations.email import send_approval_notification
                    send_approval_notification(invoice)
                except Exception:
                    pass

            elif new_status == Invoice.Status.REJECTED:
                invoice.rejected_by      = request.user
                invoice.reviewed_at      = timezone.now()
                invoice.rejection_reason = rejection_reason
                update_fields += ["rejected_by", "reviewed_at", "rejection_reason"]
                ActivityLog.objects.create(
                    organization=request.org,
                    user=request.user,
                    action=ActivityLog.Action.INVOICE_REJECTED,
                    invoice=invoice,
                    metadata={"reason": rejection_reason},
                )
                try:
                    from organizations.email import send_rejection_notification
                    send_rejection_notification(invoice)
                except Exception:
                    pass

        if new_extracted is not None:
            if invoice.status not in self.EDITABLE_STATUSES:
                return Response(
                    {"detail": "Extracted data can only be edited when status is PROCESSED or PENDING_REVIEW."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            if not isinstance(new_extracted, dict):
                return Response(
                    {"detail": "'extracted_data' must be an object."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            invoice.extracted_data = new_extracted
            update_fields.append("extracted_data")

        invoice.save(update_fields=update_fields)
        invoice.refresh_from_db()
        return Response(InvoiceSerializer(invoice, context={"request": request}).data)

    def delete(self, request, pk):
        try:
            invoice = Invoice.objects.get(pk=pk, organization=request.org)
        except Invoice.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        # Permission: admins/owners can delete any; members only their own
        if not can_delete_any(request.membership):
            if not can_delete_own(request.membership) or invoice.user_id != request.user.id:
                return Response(
                    {"detail": "You can only delete your own invoices."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        filename = invoice.original_filename
        try:
            invoice.file.delete(save=False)
        except Exception:
            logger.warning("Could not delete file for invoice %s", pk)

        ActivityLog.objects.create(
            organization=request.org,
            user=request.user,
            action=ActivityLog.Action.INVOICE_DELETED,
            metadata={"filename": filename},
        )
        invoice.delete()
        logger.info("Deleted invoice %s for org %s by user %s", pk, request.org.id, request.user.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Process ───────────────────────────────────────────────────────────────────

class InvoiceProcessView(OrgScopedMixin, APIView):
    """POST /api/invoices/<pk>/process/"""

    permission_classes = [permissions.IsAuthenticated]
    PROCESSABLE        = {Invoice.Status.UPLOADED, Invoice.Status.PROCESSING_FAILED}

    def post(self, request, pk):
        if not can_process(request.membership):
            return Response({"detail": "Viewers cannot trigger processing."}, status=status.HTTP_403_FORBIDDEN)

        invoice = _get_invoice(pk, request.org)
        if not invoice:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if invoice.status not in self.PROCESSABLE:
            return Response(
                {"detail": f"Invoice cannot be processed from status '{invoice.status}'."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        process_invoice.delay(invoice.id)
        logger.info("Enqueued invoice %s for processing by user %s", invoice.id, request.user.id)
        return Response(InvoiceSerializer(invoice, context={"request": request}).data)


# ── Duplicate management ──────────────────────────────────────────────────────

class InvoiceRecheckDuplicatesView(OrgScopedMixin, APIView):
    """POST /api/invoices/<pk>/recheck-duplicates/"""

    permission_classes = [permissions.IsAuthenticated]
    _RECHECKABLE = frozenset({
        Invoice.Status.PROCESSED, Invoice.Status.PENDING_REVIEW,
        Invoice.Status.APPROVED,  Invoice.Status.REJECTED,
    })

    def post(self, request, pk):
        invoice = _get_invoice(pk, request.org)
        if not invoice:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if invoice.status not in self._RECHECKABLE:
            return Response(
                {"detail": "Duplicate check requires a processed invoice."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        check_invoice_duplicates.delay(invoice.id)
        return Response(InvoiceSerializer(invoice, context={"request": request}).data)


class InvoiceDismissDuplicateView(OrgScopedMixin, APIView):
    """POST /api/invoices/<pk>/dismiss-duplicate/"""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        invoice = _get_invoice(pk, request.org)
        if not invoice:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            dup = invoice.duplicate_check
        except DuplicateCheckResult.DoesNotExist:
            return Response({"detail": "No duplicate check result for this invoice."}, status=status.HTTP_404_NOT_FOUND)

        dup.dismissed = bool(request.data.get("dismissed", True))
        dup.save(update_fields=["dismissed"])

        fresh = (
            Invoice.objects
            .select_related("duplicate_check", "duplicate_check__best_match")
            .get(pk=pk)
        )
        return Response(InvoiceSerializer(fresh, context={"request": request}).data)


# ── Dashboard ─────────────────────────────────────────────────────────────────

class DashboardView(OrgScopedMixin, APIView):
    """GET /api/invoices/dashboard/?range=30d"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        range_str = request.query_params.get("range", "30d")
        if range_str not in ("7d", "30d", "90d"):
            range_str = "30d"
        return Response(compute_dashboard(request.org, range_str))


# ── Bulk reprocess ────────────────────────────────────────────────────────────

class BulkReprocessFailedView(OrgScopedMixin, APIView):
    """POST /api/invoices/reprocess-failed/"""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not can_process(request.membership):
            return Response({"detail": "Insufficient permissions."}, status=status.HTTP_403_FORBIDDEN)

        failed = Invoice.objects.filter(
            organization=request.org,
            status=Invoice.Status.PROCESSING_FAILED,
        )
        count = failed.count()
        if count == 0:
            return Response({"detail": "No failed invoices to reprocess.", "queued": 0})

        for inv in failed:
            process_invoice.delay(inv.id)

        return Response({"detail": f"Queued {count} invoice(s) for reprocessing.", "queued": count})


# ── Notifications ─────────────────────────────────────────────────────────────

class NotificationListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs           = Notification.objects.filter(user=request.user)[:20]
        unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
        return Response({
            "results":      NotificationSerializer(qs, many=True).data,
            "unread_count": unread_count,
        })


class NotificationMarkReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({"status": "ok"})


# ── Usage ─────────────────────────────────────────────────────────────────────

class UsageView(OrgScopedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        count = monthly_invoice_count(request.org)
        limit = invoice_limit_for_org(request.org)
        return Response({
            "invoice_count": count,
            "invoice_limit": limit,
            "remaining": None if limit is None else max(limit - count, 0),
            "plan":          request.org.plan,
        })
