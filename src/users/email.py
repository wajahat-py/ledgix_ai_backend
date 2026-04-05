import logging

import resend
from django.conf import settings

logger = logging.getLogger(__name__)


def send_contact_sales_email(name: str, email: str, company: str, message: str) -> None:
    """Forward a Contact Sales form submission to the sales inbox."""
    resend.api_key = settings.RESEND_API_KEY

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                max-width:560px;margin:0 auto;padding:32px 24px;
                background:#0f172a;color:#e2e8f0;border-radius:12px;">
      <h2 style="font-size:20px;font-weight:700;margin:0 0 20px;color:#ffffff;">
        New Sales Enquiry — Ledgix
      </h2>

      <table style="width:100%;border-collapse:collapse;">
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #1e293b;width:110px;
                     font-size:12px;text-transform:uppercase;letter-spacing:.06em;
                     color:#64748b;">Name</td>
          <td style="padding:10px 0;border-bottom:1px solid #1e293b;
                     font-size:14px;color:#e2e8f0;">{name}</td>
        </tr>
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #1e293b;
                     font-size:12px;text-transform:uppercase;letter-spacing:.06em;
                     color:#64748b;">Email</td>
          <td style="padding:10px 0;border-bottom:1px solid #1e293b;
                     font-size:14px;color:#e2e8f0;">
            <a href="mailto:{email}" style="color:#818cf8;text-decoration:none;">{email}</a>
          </td>
        </tr>
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #1e293b;
                     font-size:12px;text-transform:uppercase;letter-spacing:.06em;
                     color:#64748b;">Company</td>
          <td style="padding:10px 0;border-bottom:1px solid #1e293b;
                     font-size:14px;color:#e2e8f0;">{company or "—"}</td>
        </tr>
      </table>

      <div style="margin-top:20px;">
        <p style="font-size:12px;text-transform:uppercase;letter-spacing:.06em;
                  color:#64748b;margin:0 0 8px;">Message</p>
        <p style="font-size:14px;color:#e2e8f0;line-height:1.6;margin:0;
                  white-space:pre-wrap;">{message or "—"}</p>
      </div>
    </div>
    """

    params: resend.Emails.SendParams = {
        "from":     settings.RESEND_FROM_EMAIL,
        "to":       ["wajahathassan699@gmail.com"],
        "reply_to": email,
        "subject":  f"Sales enquiry from {name} ({company or email})",
        "html":     html,
    }

    try:
        resend.Emails.send(params)
        logger.info("Contact Sales email forwarded from %s", email)
    except Exception as exc:
        logger.error("Failed to send Contact Sales email from %s: %s", email, exc)
        raise


def send_password_reset_email(user, token) -> None:
    """
    Send a password-reset link to `user`.  `token` is a UUID (or string).
    Logs and swallows on send failure.
    """
    reset_url = f"{settings.FRONTEND_URL}/reset-password?token={token}"

    resend.api_key = settings.RESEND_API_KEY

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                max-width:480px;margin:0 auto;padding:32px 24px;
                background:#0f172a;color:#e2e8f0;border-radius:12px;">
      <h2 style="font-size:22px;font-weight:700;margin:0 0 8px;color:#ffffff;">
        Reset your password
      </h2>
      <p style="margin:0 0 24px;color:#94a3b8;line-height:1.6;">
        Hi {user.first_name},<br/>
        We received a request to reset your Ledgix password. Click the button
        below to choose a new one.
      </p>
      <a href="{reset_url}"
         style="display:inline-block;padding:14px 28px;background:#6366f1;
                color:#ffffff;text-decoration:none;border-radius:8px;
                font-weight:600;font-size:15px;">
        Reset my password
      </a>
      <p style="margin:24px 0 0;font-size:13px;color:#64748b;line-height:1.5;">
        This link expires in <strong style="color:#94a3b8;">1 hour</strong>.
        If you didn't request a password reset you can safely ignore this email —
        your password won't change.
      </p>
      <p style="margin:12px 0 0;font-size:12px;color:#475569;">
        Or copy this URL:<br/>
        <a href="{reset_url}" style="color:#818cf8;word-break:break-all;">{reset_url}</a>
      </p>
    </div>
    """

    params: resend.Emails.SendParams = {
        "from": settings.RESEND_FROM_EMAIL,
        "to": [user.email],
        "subject": "Reset your Ledgix password",
        "html": html,
    }

    try:
        resend.Emails.send(params)
        logger.info("Password reset email sent to %s", user.email)
    except Exception as exc:
        logger.error("Failed to send password reset email to %s: %s", user.email, exc)


def send_verification_email(pending) -> None:
    """
    Send a verification email for a PendingRegistration.
    Logs and swallows on send failure so the caller is never crashed by a
    transient email error.
    """
    verify_url = f"{settings.FRONTEND_URL}/verify-email?token={pending.token}"

    resend.api_key = settings.RESEND_API_KEY

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                max-width:480px;margin:0 auto;padding:32px 24px;
                background:#0f172a;color:#e2e8f0;border-radius:12px;">
      <h2 style="font-size:22px;font-weight:700;margin:0 0 8px;color:#ffffff;">
        Verify your email
      </h2>
      <p style="margin:0 0 24px;color:#94a3b8;line-height:1.6;">
        Hi {pending.first_name},<br/>
        Click the button below to verify your email and activate your Ledgix account.
      </p>
      <a href="{verify_url}"
         style="display:inline-block;padding:14px 28px;background:#6366f1;
                color:#ffffff;text-decoration:none;border-radius:8px;
                font-weight:600;font-size:15px;">
        Verify my email
      </a>
      <p style="margin:24px 0 0;font-size:13px;color:#64748b;line-height:1.5;">
        This link expires in 24 hours. If you didn't create a Ledgix account
        you can safely ignore this email.
      </p>
      <p style="margin:12px 0 0;font-size:12px;color:#475569;">
        Or copy this URL:<br/>
        <a href="{verify_url}" style="color:#818cf8;word-break:break-all;">{verify_url}</a>
      </p>
    </div>
    """

    params: resend.Emails.SendParams = {
        "from": settings.RESEND_FROM_EMAIL,
        "to": [pending.email],
        "subject": "Verify your Ledgix email",
        "html": html,
    }

    try:
        resend.Emails.send(params)
        logger.info("Verification email sent to %s", pending.email)
    except Exception as exc:
        logger.error("Failed to send verification email to %s: %s", pending.email, exc)
