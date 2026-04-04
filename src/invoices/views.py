import logging

from rest_framework import permissions, status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Invoice
from .serializers import InvoiceSerializer
from .tasks import process_invoice

logger = logging.getLogger(__name__)

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/tiff",
}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_FILES_PER_REQUEST = 10


class InvoiceListView(APIView):
    """GET /api/invoices/ — returns all invoices for the authenticated user."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        invoices = Invoice.objects.filter(user=request.user).order_by("-created_at")
        return Response(InvoiceSerializer(invoices, many=True, context={"request": request}).data)


class InvoiceDetailView(APIView):
    """
    GET    /api/invoices/<pk>/ — single invoice owned by the authenticated user.
    PATCH  /api/invoices/<pk>/ — update status and/or extracted_data.
    DELETE /api/invoices/<pk>/ — permanently delete the invoice and its file.
    """

    permission_classes = [permissions.IsAuthenticated]

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Invoice.Status.PROCESSED:      {Invoice.Status.PENDING_REVIEW, Invoice.Status.APPROVED, Invoice.Status.REJECTED},
        Invoice.Status.PENDING_REVIEW: {Invoice.Status.APPROVED, Invoice.Status.REJECTED},
    }

    # Statuses where a user may edit the AI-extracted field values.
    EDITABLE_STATUSES = {Invoice.Status.PROCESSED, Invoice.Status.PENDING_REVIEW}

    def get(self, request, pk):
        try:
            invoice = Invoice.objects.get(pk=pk, user=request.user)
        except Invoice.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(InvoiceSerializer(invoice, context={"request": request}).data)

    def patch(self, request, pk):
        try:
            invoice = Invoice.objects.get(pk=pk, user=request.user)
        except Invoice.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        new_status = request.data.get("status")
        new_extracted_data = request.data.get("extracted_data")

        if new_status is None and new_extracted_data is None:
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
            invoice.status = new_status
            update_fields.append("status")

        if new_extracted_data is not None:
            if invoice.status not in self.EDITABLE_STATUSES:
                return Response(
                    {"detail": "Extracted data can only be edited when status is PROCESSED or PENDING_REVIEW."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            if not isinstance(new_extracted_data, dict):
                return Response(
                    {"detail": "'extracted_data' must be an object."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            invoice.extracted_data = new_extracted_data
            update_fields.append("extracted_data")

        invoice.save(update_fields=update_fields)
        return Response(InvoiceSerializer(invoice, context={"request": request}).data)

    def delete(self, request, pk):
        try:
            invoice = Invoice.objects.get(pk=pk, user=request.user)
        except Invoice.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        # Best-effort file removal; log but don't abort if the file is already gone.
        try:
            invoice.file.delete(save=False)
        except Exception:
            logger.warning("Could not delete file for invoice %s — record will still be removed", pk)

        invoice.delete()
        logger.info("Deleted invoice %s for user %s", pk, request.user.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class InvoiceProcessView(APIView):
    """POST /api/invoices/<pk>/process/ — enqueue AI extraction for an uploaded invoice."""

    permission_classes = [permissions.IsAuthenticated]

    PROCESSABLE = {Invoice.Status.UPLOADED, Invoice.Status.PROCESSING_FAILED}

    def post(self, request, pk):
        try:
            invoice = Invoice.objects.get(pk=pk, user=request.user)
        except Invoice.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if invoice.status not in self.PROCESSABLE:
            return Response(
                {"detail": f"Invoice cannot be processed from status '{invoice.status}'."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        process_invoice.delay(invoice.id)
        logger.info("Enqueued invoice %s for processing by user %s", invoice.id, request.user.id)
        # Return the invoice still in UPLOADED state; the task will push PROCESSING via WebSocket
        return Response(InvoiceSerializer(invoice, context={"request": request}).data)


class InvoiceUploadView(APIView):
    """
    POST /api/invoices/upload/
    Saves 1–10 invoice files and returns their records (status=UPLOADED).
    Processing is not started automatically — call /process/ to trigger it.
    """

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
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

        for f in files:
            if f.content_type not in ALLOWED_MIME_TYPES:
                return Response(
                    {"detail": f"'{f.name}': unsupported file type '{f.content_type}'. "
                               f"Accepted: PDF, JPEG, PNG, WebP, TIFF."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            if f.size > MAX_FILE_SIZE_BYTES:
                return Response(
                    {"detail": f"'{f.name}' exceeds the 10 MB limit."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

        invoices = []
        for f in files:
            invoice = Invoice.objects.create(
                user=request.user,
                file=f,
                original_filename=f.name,
            )
            invoices.append(invoice)
            logger.info("Saved invoice %s for user %s", invoice.id, request.user.id)

        return Response(
            InvoiceSerializer(invoices, many=True, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )
