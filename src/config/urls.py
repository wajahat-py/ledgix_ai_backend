from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("users.urls")),
    path("api/invoices/", include("invoices.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
