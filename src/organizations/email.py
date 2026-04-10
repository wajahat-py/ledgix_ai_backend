import logging

import resend
from django.conf import settings

logger = logging.getLogger(__name__)


def send_invitation_email(invitation) -> None:
    resend.api_key = settings.RESEND_API_KEY
    invite_url   = f"{settings.FRONTEND_URL}/invite/{invitation.token}"
    org_name     = invitation.organization.name
    inviter      = invitation.invited_by
    inviter_name = inviter.full_name if inviter else "Your team"
    role_label   = invitation.get_role_display()

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                max-width:480px;margin:0 auto;padding:32px 24px;
                background:#0f172a;color:#e2e8f0;border-radius:12px;">
      <h2 style="font-size:22px;font-weight:700;margin:0 0 8px;color:#ffffff;">
        You're invited to join {org_name}
      </h2>
      <p style="margin:0 0 24px;color:#94a3b8;line-height:1.6;">
        {inviter_name} has invited you to join
        <strong style="color:#e2e8f0;">{org_name}</strong> on Ledgix as a
        <strong style="color:#e2e8f0;">{role_label}</strong>.
      </p>
      <a href="{invite_url}"
         style="display:inline-block;padding:14px 28px;background:#6366f1;
                color:#ffffff;text-decoration:none;border-radius:8px;
                font-weight:600;font-size:15px;">
        Accept invitation
      </a>
      <p style="margin:24px 0 0;font-size:13px;color:#64748b;line-height:1.5;">
        This invitation expires in 7 days. If you don't have a Ledgix account yet,
        you'll be able to create one when you accept.
      </p>
      <p style="margin:12px 0 0;font-size:12px;color:#475569;">
        Or copy this URL:<br/>
        <a href="{invite_url}" style="color:#818cf8;word-break:break-all;">{invite_url}</a>
      </p>
    </div>
    """

    params: resend.Emails.SendParams = {
        "from":    settings.RESEND_FROM_EMAIL,
        "to":      [invitation.email],
        "subject": f"You're invited to join {org_name} on Ledgix",
        "html":    html,
    }
    try:
        resend.Emails.send(params)
        logger.info("Invitation email sent to %s for org %s", invitation.email, org_name)
    except Exception as exc:
        logger.error("Failed to send invitation email to %s: %s", invitation.email, exc)
        raise


def send_approval_notification(invoice) -> None:
    resend.api_key = settings.RESEND_API_KEY
    uploader      = invoice.user
    approver      = invoice.approved_by
    approver_name = approver.full_name if approver else "A team member"

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                max-width:480px;margin:0 auto;padding:32px 24px;
                background:#0f172a;color:#e2e8f0;border-radius:12px;">
      <h2 style="font-size:22px;font-weight:700;margin:0 0 8px;color:#22c55e;">
        Invoice Approved ✓
      </h2>
      <p style="margin:0 0 16px;color:#94a3b8;line-height:1.6;">
        Hi {uploader.first_name},<br/>
        <strong style="color:#e2e8f0;">{invoice.original_filename}</strong>
        has been approved by {approver_name}.
      </p>
      <p style="font-size:13px;color:#64748b;">Log into Ledgix to view the details.</p>
    </div>
    """

    params: resend.Emails.SendParams = {
        "from":    settings.RESEND_FROM_EMAIL,
        "to":      [uploader.email],
        "subject": f"Invoice approved: {invoice.original_filename}",
        "html":    html,
    }
    try:
        resend.Emails.send(params)
    except Exception as exc:
        logger.error("Failed to send approval notification: %s", exc)


def send_rejection_notification(invoice) -> None:
    resend.api_key = settings.RESEND_API_KEY
    uploader      = invoice.user
    rejector      = invoice.rejected_by
    rejector_name = rejector.full_name if rejector else "A team member"
    reason        = invoice.rejection_reason or "No reason provided."

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                max-width:480px;margin:0 auto;padding:32px 24px;
                background:#0f172a;color:#e2e8f0;border-radius:12px;">
      <h2 style="font-size:22px;font-weight:700;margin:0 0 8px;color:#ef4444;">
        Invoice Rejected
      </h2>
      <p style="margin:0 0 16px;color:#94a3b8;line-height:1.6;">
        Hi {uploader.first_name},<br/>
        <strong style="color:#e2e8f0;">{invoice.original_filename}</strong>
        was rejected by {rejector_name}.
      </p>
      <div style="background:#1e293b;border-radius:8px;padding:16px;margin-bottom:16px;">
        <p style="font-size:12px;text-transform:uppercase;letter-spacing:.06em;
                  color:#64748b;margin:0 0 8px;">Reason</p>
        <p style="margin:0;color:#e2e8f0;font-size:14px;line-height:1.6;">{reason}</p>
      </div>
      <p style="font-size:13px;color:#64748b;">Log into Ledgix to review and resubmit.</p>
    </div>
    """

    params: resend.Emails.SendParams = {
        "from":    settings.RESEND_FROM_EMAIL,
        "to":      [uploader.email],
        "subject": f"Invoice rejected: {invoice.original_filename}",
        "html":    html,
    }
    try:
        resend.Emails.send(params)
    except Exception as exc:
        logger.error("Failed to send rejection notification: %s", exc)
