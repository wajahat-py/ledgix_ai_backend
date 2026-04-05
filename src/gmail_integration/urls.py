from django.urls import path, re_path

from .views import (
    GmailAttachmentProxyView,
    GmailAuthView,
    GmailCallbackView,
    GmailDisconnectView,
    GmailMessageDetailView,
    GmailPubSubView,
    GmailRetryView,
    GmailStatusView,
    GmailSyncView,
    GmailWatchView,
)

urlpatterns = [
    path("auth/",                             GmailAuthView.as_view(),            name="gmail-auth"),
    path("callback/",                         GmailCallbackView.as_view(),        name="gmail-callback"),
    path("status/",                           GmailStatusView.as_view(),          name="gmail-status"),
    path("sync/",                             GmailSyncView.as_view(),            name="gmail-sync"),
    path("disconnect/",                       GmailDisconnectView.as_view(),      name="gmail-disconnect"),
    path("message/<str:message_id>/",         GmailMessageDetailView.as_view(),   name="gmail-message-detail"),
    path("attachment/",                       GmailAttachmentProxyView.as_view(), name="gmail-attachment"),
    path("retry/<int:synced_message_id>/",    GmailRetryView.as_view(),           name="gmail-retry"),
    path("watch/",                            GmailWatchView.as_view(),           name="gmail-watch"),
    # Google Pub/Sub doesn't follow 301 redirects, so match both with and
    # without a trailing slash to avoid Django's APPEND_SLASH redirect.
    re_path(r"^pubsub/?$",                    GmailPubSubView.as_view(),          name="gmail-pubsub"),
]
