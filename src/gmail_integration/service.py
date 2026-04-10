"""
Gmail OAuth2 + API helpers.

OAuth flow:
  1. get_oauth_url(user)       → returns the Google consent URL
  2. exchange_code_and_save()  → called from the callback view; stores tokens
  3. get_gmail_service()       → builds an authenticated Gmail API client,
                                 refreshing the access token if expired
"""
import base64
import hashlib
import logging
import os
import secrets
from datetime import timezone
from email.utils import parsedate_to_datetime

from django.conf import settings
from django.core import signing

logger = logging.getLogger(__name__)

# ── Google OAuth2 scopes ──────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

# ── Attachment filtering ──────────────────────────────────────────────────────

ALLOWED_MIME_TYPES = frozenset({
    "application/pdf",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/tiff",
})

ALLOWED_EXTENSIONS = frozenset({
    ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif",
})

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB

# ── Invoice detection keywords ────────────────────────────────────────────────

_FILENAME_KW = frozenset({
    "invoice", "receipt", "bill", "statement", "inv", "rcpt",
    "billing", "payment", "order", "purchase", "tax", "fee", "subscription",
})

_SUBJECT_KW = frozenset({
    "invoice", "receipt", "bill", "statement", "payment",
    "billing", "order", "purchase", "transaction", "tax", "fee",
})

_SENDER_KW = frozenset({
    "billing", "invoice", "invoices", "receipts", "payment",
    "accounting", "finance", "accounts", "noreply", "no-reply",
    "stripe", "paypal", "square", "support",
})


def _kw_match(text: str, keywords: frozenset[str]) -> bool:
    """Return True if any keyword matches the text as a word or prefix."""
    import re
    t = text.lower()
    for kw in keywords:
        # Avoid short keyword "inv" matching "invitation"
        if kw == "inv":
            if re.search(r"\binv[o\d\W]", t):  # matches "inv-", "inv1", "invoice"
                return True
            continue
        if kw in t:
            return True
    return False


# ── Internal helpers ──────────────────────────────────────────────────────────

def _client_config() -> dict:
    return {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID_EMAIL,
            "client_secret": settings.GOOGLE_CLIENT_SECRET_EMAIL,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.GMAIL_OAUTH_REDIRECT_URI],
        }
    }


def _make_flow():
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        redirect_uri=settings.GMAIL_OAUTH_REDIRECT_URI,
    )


def _build_credentials(integration):
    from google.oauth2.credentials import Credentials
    return Credentials(
        token=integration.access_token,
        refresh_token=integration.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID_EMAIL,
        client_secret=settings.GOOGLE_CLIENT_SECRET_EMAIL,
        scopes=SCOPES,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def get_oauth_url(user) -> str:
    """Return the Google consent URL for the given user.

    We build the URL manually rather than delegating to requests_oauthlib's
    PKCE helper because its behaviour (and whether code_verifier ends up in the
    query string) differs across library versions.  Building it ourselves gives
    us exact control: only code_challenge goes in the auth URL; code_verifier
    is stored in the signed state and sent only in the token-exchange request.
    """
    from urllib.parse import urlencode

    # Generate PKCE pair
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(96)).rstrip(b"=").decode("ascii")
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")

    state = signing.dumps(
        {"user_id": user.id, "cv": code_verifier},
        salt="gmail-oauth",
    )

    params = {
        "client_id":              settings.GOOGLE_CLIENT_ID_EMAIL,
        "redirect_uri":           settings.GMAIL_OAUTH_REDIRECT_URI,
        "response_type":          "code",
        "scope":                  " ".join(SCOPES),
        "access_type":            "offline",
        "include_granted_scopes": "true",
        "state":                  state,
        "prompt":                 "consent",
        "code_challenge":         code_challenge,
        "code_challenge_method":  "S256",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


def exchange_code_and_save(code: str, state: str):
    """
    Exchange an OAuth code for credentials, fetch the Gmail address,
    and persist a GmailIntegration record for the user.
    """
    from googleapiclient.discovery import build
    from .models import GmailIntegration

    try:
        data = signing.loads(state, salt="gmail-oauth", max_age=600)
    except signing.BadSignature:
        raise ValueError("Invalid or expired OAuth state.")

    user_id       = data["user_id"]
    code_verifier = data.get("cv")   # PKCE verifier stored during get_oauth_url()

    # google_auth_oauthlib rejects http:// redirect URIs unless this env var is
    # set.  Safe here — the user already consented on Google's servers and we're
    # just exchanging a one-time code over loopback.  In production the redirect
    # URI uses https:// and this branch is never reached.
    if settings.DEBUG:
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

    logger.debug("exchange_code_and_save: user_id=%s, starting token exchange", user_id)

    try:
        flow = _make_flow()
        # Pass the PKCE verifier so Google can verify it against the challenge it
        # received in the authorization request.
        flow.fetch_token(code=code, code_verifier=code_verifier)
        creds = flow.credentials
    except Exception as exc:
        logger.error(
            "exchange_code_and_save: token exchange failed for user %s — %s: %s",
            user_id, type(exc).__name__, exc,
        )
        raise

    logger.debug("exchange_code_and_save: token exchange OK, fetching userinfo")

    try:
        oauth2_service = build("oauth2", "v2", credentials=creds)
        user_info = oauth2_service.userinfo().get().execute()
        gmail_address = user_info["email"]
    except Exception as exc:
        logger.error(
            "exchange_code_and_save: userinfo fetch failed for user %s — %s: %s",
            user_id, type(exc).__name__, exc,
        )
        raise

    logger.debug("exchange_code_and_save: got gmail_address=%s", gmail_address)

    expiry = None
    if creds.expiry:
        expiry = creds.expiry
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

    # Google only returns a refresh_token on the *first* grant (or after the
    # user explicitly revokes and re-grants access).  Never overwrite an existing
    # valid refresh_token with None — that would break all future token refreshes.
    defaults: dict = {
        "gmail_address": gmail_address,
        "access_token":  creds.token,
        "token_expiry":  expiry,
        "is_active":     True,
    }
    if creds.refresh_token:
        defaults["refresh_token"] = creds.refresh_token

    integration, _ = GmailIntegration.objects.update_or_create(
        user_id=user_id,
        defaults=defaults,
    )
    return integration


def get_gmail_service(integration):
    """
    Return an authenticated Gmail API service resource.
    Refreshes the access token automatically if it has expired.
    """
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from googleapiclient.discovery import build

    creds = _build_credentials(integration)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        integration.access_token = creds.token
        if creds.expiry:
            expiry = creds.expiry
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            integration.token_expiry = expiry
        integration.save(update_fields=["access_token", "token_expiry"])

    return build("gmail", "v1", credentials=creds)


def setup_gmail_watch(service, topic_name: str) -> dict:
    """
    Register (or re-register) a Gmail push-notification watch so Google sends
    Pub/Sub messages whenever the INBOX changes.

    Returns the raw API response which includes 'historyId' and 'expiration'
    (Unix epoch in milliseconds).  The watch lasts at most 7 days; call this
    again before expiry to keep it active.
    """
    return service.users().watch(
        userId="me",
        body={
            "topicName":           topic_name,
            "labelIds":            ["INBOX"],
            "labelFilterBehavior": "INCLUDE",
        },
    ).execute()


def stop_gmail_watch(service) -> None:
    """Best-effort — stop Gmail push notifications for the authenticated user."""
    try:
        service.users().stop(userId="me").execute()
    except Exception:
        pass


def revoke_token(access_token: str) -> None:
    """Best-effort token revocation — errors are silently ignored."""
    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({"token": access_token}).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/revoke",
            data=data,
            headers={"Content-type": "application/x-www-form-urlencoded"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


# ── Gmail message helpers ─────────────────────────────────────────────────────

def get_message_header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def parse_email_date(date_str: str):
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def collect_attachment_parts(payload: dict) -> list:
    """Recursively collect every part that carries an attachment."""
    parts = []
    if payload.get("filename") and payload.get("body", {}).get("attachmentId"):
        parts.append(payload)
    for child in payload.get("parts", []):
        parts.extend(collect_attachment_parts(child))
    return parts


def is_likely_invoice(filename: str, mime_type: str, subject: str, sender: str) -> bool:
    """
    Heuristic scorer — True when the attachment is probably an invoice.

    Scoring:
      +2  PDF attachment (stronger file-type signal)
      +1  Image attachment
      +3  filename contains an invoice keyword
      +2  email subject contains an invoice keyword
      +1  sender address contains a billing-style keyword

    Threshold: score >= 3
    """
    if mime_type not in ALLOWED_MIME_TYPES:
        if os.path.splitext(filename)[1].lower() not in ALLOWED_EXTENSIONS:
            return False

    score = 0
    fn  = filename.lower()
    sub = subject.lower()
    snd = sender.lower()

    if mime_type == "application/pdf":
        score += 2
    elif mime_type.startswith("image/"):
        score += 1

    if _kw_match(fn, _FILENAME_KW):
        score += 3

    if _kw_match(sub, _SUBJECT_KW):
        score += 2

    if _kw_match(snd, _SENDER_KW):
        score += 1

    return score >= 3


def get_profile(service) -> dict:
    """Return the Gmail profile (contains current historyId)."""
    return service.users().getProfile(userId="me").execute()


def list_messages(service, max_results: int = 200) -> list:
    """Return up to *max_results* Gmail message stubs with attachments (full scan)."""
    query = "has:attachment newer_than:180d"
    messages = []
    page_token = None

    while len(messages) < max_results:
        kwargs = {
            "userId": "me",
            "q": query,
            "maxResults": min(100, max_results - len(messages)),
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.users().messages().list(**kwargs).execute()
        messages.extend(response.get("messages", []))

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return messages


def list_new_message_ids_since(service, history_id: str) -> list[str] | None:
    """
    Return the IDs of messages added to the inbox since *history_id*.

    Returns None when the history_id is too old (GmailAPI returns 404 or 400),
    which signals the caller to fall back to a full list_messages() scan.
    """
    message_ids: list[str] = []
    page_token = None

    try:
        while True:
            kwargs: dict = {
                "userId":         "me",
                "startHistoryId": history_id,
                "historyTypes":   ["messageAdded"],
            }
            if page_token:
                kwargs["pageToken"] = page_token

            response = service.users().history().list(**kwargs).execute()

            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg_id = added.get("message", {}).get("id")
                    if msg_id and msg_id not in message_ids:
                        message_ids.append(msg_id)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    except Exception as exc:
        logger.warning(
            "Gmail history.list failed for history_id=%s (%s) — will do full scan",
            history_id, exc,
        )
        return None

    return message_ids


def _find_body_part(part: dict, mime_type: str) -> str:
    """Recursively find and base64-decode a specific MIME type part."""
    if part.get("mimeType") == mime_type:
        data = part.get("body", {}).get("data", "")
        if data:
            padded = data + "=" * (-len(data) % 4)
            try:
                return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
            except Exception:
                return ""
    for child in part.get("parts", []):
        result = _find_body_part(child, mime_type)
        if result:
            return result
    return ""


def extract_message_body(msg: dict) -> str:
    """Return a readable text preview of a Gmail message (≤ 4 000 chars)."""
    import re
    payload = msg.get("payload", {})
    text = _find_body_part(payload, "text/plain")
    if not text:
        html = _find_body_part(payload, "text/html")
        if html:
            # Strip tags and collapse whitespace
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"[ \t]{2,}", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:4000]


def download_attachment(service, message_id: str, attachment_id: str) -> bytes:
    attachment = (
        service.users().messages().attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    # Gmail uses URL-safe base64; pad to a multiple of 4 before decoding
    data = attachment["data"]
    data += "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data)
