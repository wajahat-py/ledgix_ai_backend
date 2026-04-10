from django.urls import path

from .views import (
    AcceptInvitationView,
    ActivityLogView,
    InvitationDetailView,
    InvitationListView,
    MemberDetailView,
    MemberListView,
    OrganizationDetailView,
    OrganizationListView,
    PublicInvitationView,
    ResendInvitationView,
    TransferOwnershipView,
)

urlpatterns = [
    # Org list / current
    path("",                          OrganizationListView.as_view(),    name="org-list"),
    # Org detail
    path("<int:org_id>/",             OrganizationDetailView.as_view(),  name="org-detail"),
    # Members
    path("<int:org_id>/members/",                           MemberListView.as_view(),         name="org-member-list"),
    path("<int:org_id>/members/<int:membership_id>/",       MemberDetailView.as_view(),       name="org-member-detail"),
    path("<int:org_id>/transfer-ownership/",                TransferOwnershipView.as_view(),  name="org-transfer-ownership"),
    # Invitations
    path("<int:org_id>/invitations/",                       InvitationListView.as_view(),     name="org-invitation-list"),
    path("<int:org_id>/invitations/<int:invitation_id>/",   InvitationDetailView.as_view(),   name="org-invitation-detail"),
    path("<int:org_id>/invitations/<int:invitation_id>/resend/", ResendInvitationView.as_view(), name="org-invitation-resend"),
    # Activity log
    path("<int:org_id>/activity/",    ActivityLogView.as_view(),         name="org-activity"),
]

# Public invitation endpoints (no org_id in path — token is self-contained)
invitation_urlpatterns = [
    path("<str:token>/",         PublicInvitationView.as_view(),  name="invitation-detail"),
    path("<str:token>/accept/",  AcceptInvitationView.as_view(),  name="invitation-accept"),
]
