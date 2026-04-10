from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from invoices.views import NotificationListView, NotificationMarkReadView
from organizations.urls import invitation_urlpatterns

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("users.urls")),
    path("api/invoices/", include("invoices.urls")),
    path("api/gmail/", include("gmail_integration.urls")),
    path("api/orgs/", include("organizations.urls")),
    path("api/invitations/", include((invitation_urlpatterns, "invitations"))),
    path("api/notifications/", NotificationListView.as_view(), name="notification-list"),
    path("api/notifications/mark-read/", NotificationMarkReadView.as_view(), name="notification-mark-read"),
    path("api/billing/", include(("billing.urls", "billing"))),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
