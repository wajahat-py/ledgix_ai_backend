from django.urls import path

from .views import InvoiceDetailView, InvoiceListView, InvoiceProcessView, InvoiceUploadView

urlpatterns = [
    path("", InvoiceListView.as_view(), name="invoice-list"),
    path("<int:pk>/", InvoiceDetailView.as_view(), name="invoice-detail"),
    path("<int:pk>/process/", InvoiceProcessView.as_view(), name="invoice-process"),
    path("upload/", InvoiceUploadView.as_view(), name="invoice-upload"),
]
