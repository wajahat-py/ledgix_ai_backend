from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from invoices.views import NotificationListView, NotificationMarkReadView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("users.urls")),
    path("api/invoices/", include("invoices.urls")),
    path("api/gmail/", include("gmail_integration.urls")),
    path("api/notifications/", NotificationListView.as_view(), name="notification-list"),
    path("api/notifications/mark-read/", NotificationMarkReadView.as_view(), name="notification-mark-read"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
