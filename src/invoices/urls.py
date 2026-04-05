from django.urls import path

from .views import (
    BulkReprocessFailedView,
    DashboardView,
    InvoiceDetailView,
    InvoiceDismissDuplicateView,
    InvoiceListView,
    InvoiceProcessView,
    InvoiceRecheckDuplicatesView,
    InvoiceUploadView,
    UsageView,
)

urlpatterns = [
    path("", InvoiceListView.as_view(), name="invoice-list"),
    path("usage/", UsageView.as_view(), name="invoice-usage"),
    path("dashboard/", DashboardView.as_view(), name="invoice-dashboard"),
    path("reprocess-failed/", BulkReprocessFailedView.as_view(), name="invoice-reprocess-failed"),
    path("<int:pk>/", InvoiceDetailView.as_view(), name="invoice-detail"),
    path("<int:pk>/process/", InvoiceProcessView.as_view(), name="invoice-process"),
    path("<int:pk>/recheck-duplicates/", InvoiceRecheckDuplicatesView.as_view(), name="invoice-recheck-duplicates"),
    path("<int:pk>/dismiss-duplicate/", InvoiceDismissDuplicateView.as_view(), name="invoice-dismiss-duplicate"),
    path("upload/", InvoiceUploadView.as_view(), name="invoice-upload"),
]
