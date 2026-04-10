import logging
import math
import re
from datetime import date

from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer
from django.conf import settings

from .models import DuplicateCheckResult, Invoice, Notification
from .serializers import InvoiceSerializer

logger = logging.getLogger(__name__)

_PROCESSED_STATUSES = frozenset({
    Invoice.Status.PROCESSED,
    Invoice.Status.PENDING_REVIEW,
    Invoice.Status.APPROVED,
    Invoice.Status.REJECTED,
})

# ── serialization helper ──────────────────────────────────────────────────────

def _serialize_raw_field(raw_field):
    """
    Recursively serialize a raw Mindee V2 field dict (from the API JSON) to a
    JSON-safe structure.

    Mindee returns three field shapes:
      - Simple:  {"value": <scalar|null>, "raw_value": <str|null>, "confidence": ..., "locations": ...}
      - List:    {"items": [...], "confidence": ...}
      - Object:  {"fields": {...}, "confidence": ...}

    When `value` is null but `raw_value` is not, we use raw_value so the
    extracted text is never silently dropped.
    """
    if raw_field is None:
        return None
    if not isinstance(raw_field, dict):
        # Scalar that somehow escaped wrapping — return as-is.
        return raw_field

    # ── List field ────────────────────────────────────────────────────────────
    if "items" in raw_field:
        items = [_serialize_raw_field(item) for item in raw_field["items"]]
        result: dict = {"items": items}
        if raw_field.get("confidence") is not None:
            result["confidence"] = raw_field["confidence"]
        return result

    # ── Object field (nested sub-fields) ─────────────────────────────────────
    if "fields" in raw_field:
        data: dict = {}
        for k, v in raw_field["fields"].items():
            serialized = _serialize_raw_field(v)
            if serialized == "[object Object]":
                continue
            if isinstance(serialized, dict) and serialized.get("value") == "[object Object]":
                serialized = {kk: vv for kk, vv in serialized.items() if kk != "value"}
            data[k] = serialized
        if raw_field.get("confidence") is not None:
            data["confidence"] = raw_field["confidence"]
        return data

    # ── Simple field ──────────────────────────────────────────────────────────
    if "value" in raw_field:
        value = raw_field.get("value")
        raw_value = raw_field.get("raw_value")

        if value == "[object Object]":
            value = None
        elif isinstance(value, date):
            value = value.isoformat()

        # raw_value is the verbatim text the model read from the document.
        # Fall back to it when the typed value is null so we never discard
        # text that was clearly present on the invoice.
        display_value = value if value is not None else raw_value

        result = {}
        if display_value is not None and display_value != "[object Object]":
            result["value"] = display_value
        if raw_field.get("confidence") is not None:
            result["confidence"] = raw_field["confidence"]
        return result

    # Unknown shape — store as string to avoid silent data loss.
    return str(raw_field)


# ── WebSocket push ────────────────────────────────────────────────────────────

def _push_update(invoice: Invoice) -> None:
    """Send a real-time status update to the user's WebSocket group."""
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    invoice_with_rels = (
        Invoice.objects
        .select_related("duplicate_check", "duplicate_check__best_match")
        .get(pk=invoice.pk)
    )
    group = f"invoices_{invoice.user_id}"
    async_to_sync(channel_layer.group_send)(
        group,
        {
            "type": "invoice.update",
            "data": InvoiceSerializer(invoice_with_rels).data,
        },
    )


# ── notification helpers ──────────────────────────────────────────────────────

def _push_notification(notif: Notification) -> None:
    """Push a new notification over the user's WebSocket channel."""
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    group = f"invoices_{notif.user_id}"
    async_to_sync(channel_layer.group_send)(
        group,
        {
            "type": "notification.new",
            "data": {
                "id": notif.id,
                "kind": notif.kind,
                "title": notif.title,
                "body": notif.body,
                "invoice_id": notif.invoice_id,
                "is_read": notif.is_read,
                "created_at": notif.created_at.isoformat(),
            },
        },
    )


def _create_notification(
    user_id: int,
    kind: str,
    title: str,
    body: str = "",
    invoice: "Invoice | None" = None,
) -> Notification:
    notif = Notification.objects.create(
        user_id=user_id,
        kind=kind,
        title=title,
        body=body,
        invoice=invoice,
    )
    _push_notification(notif)
    return notif


# ── normalization helpers ─────────────────────────────────────────────────────

_BUSINESS_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|gmbh|plc|pty|sa|sas|srl|bv|nv|ag)\b\.?$",
    re.IGNORECASE,
)


def _get_field_value(data: dict, *keys: str) -> str | None:
    """Return the first non-empty value found under any of the given keys."""
    if not data:
        return None
    for key in keys:
        entry = data.get(key)
        if not entry:
            continue
        if isinstance(entry, dict):
            val = entry.get("value")
            if val is not None and val != "" and val != "[object Object]":
                return str(val)
        elif isinstance(entry, str) and entry:
            return entry
    return None


def _normalize_vendor(raw: str | None) -> str:
    if not raw:
        return ""
    text = raw.lower().strip()
    text = _BUSINESS_SUFFIX_RE.sub("", text).strip().rstrip(",").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _normalize_text(raw: str | None) -> str:
    if not raw:
        return ""
    return re.sub(r"\s+", " ", raw.lower().strip())


def _normalize_amount(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(raw))
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def _normalize_date(raw: str | None) -> str:
    """Return an ISO date string (YYYY-MM-DD) or empty string."""
    if not raw:
        return ""
    raw = raw.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", raw)
    if m:
        month, day, year = m.group(1), m.group(2), m.group(3)
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    return raw


def _extract_normalized_fields(invoice: Invoice) -> dict:
    data = invoice.extracted_data or {}
    vendor_raw       = _get_field_value(data, "supplier_name", "vendor_name", "seller_name", "company_name")
    inv_number_raw   = _get_field_value(data, "invoice_number", "invoice_id", "reference_number", "invoice_no")
    amount_raw       = _get_field_value(data, "total_amount", "total_net", "amount_due", "grand_total", "subtotal")
    date_raw         = _get_field_value(data, "date", "invoice_date", "issue_date")
    return {
        "vendor":         _normalize_vendor(vendor_raw),
        "invoice_number": _normalize_text(inv_number_raw),
        "amount":         _normalize_amount(amount_raw),
        "date":           _normalize_date(date_raw),
    }


# ── scoring ───────────────────────────────────────────────────────────────────

def _rule_score(a: dict, b: dict) -> float:
    score = 0.0
    if a["invoice_number"] and b["invoice_number"] and a["invoice_number"] == b["invoice_number"]:
        score += 0.5
    if a["vendor"] and b["vendor"] and a["vendor"] == b["vendor"]:
        score += 0.2
    if a["amount"] is not None and b["amount"] is not None and a["amount"] == b["amount"]:
        score += 0.2
    if a["date"] and b["date"] and a["date"] == b["date"]:
        score += 0.1
    return score


def _fuzzy_score(a: dict, b: dict) -> float:
    from rapidfuzz import fuzz

    def ratio(x: str, y: str) -> float:
        if not x or not y:
            return 0.0
        return fuzz.token_sort_ratio(x, y) / 100.0

    def amount_similarity(x: float | None, y: float | None) -> float:
        if x is None or y is None:
            return 0.0
        if x == 0 and y == 0:
            return 1.0
        if x == 0 or y == 0:
            return 0.0
        diff = abs(x - y) / max(abs(x), abs(y))
        return max(0.0, 1.0 - diff * 10)

    return (
        0.35 * ratio(a["invoice_number"], b["invoice_number"])
        + 0.35 * ratio(a["vendor"], b["vendor"])
        + 0.20 * amount_similarity(a["amount"], b["amount"])
        + 0.10 * ratio(a["date"], b["date"])
    )


def _cosine_similarity(vec_a: list, vec_b: list) -> float:
    dot   = sum(a * b for a, b in zip(vec_a, vec_b))
    mag_a = math.sqrt(sum(x * x for x in vec_a))
    mag_b = math.sqrt(sum(x * x for x in vec_b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _make_embedding_text(fields: dict, invoice: Invoice) -> str:
    parts = [invoice.original_filename]
    if fields["vendor"]:
        parts.append(fields["vendor"])
    if fields["invoice_number"]:
        parts.append(fields["invoice_number"])
    if fields["amount"] is not None:
        parts.append(str(fields["amount"]))
    if fields["date"]:
        parts.append(fields["date"])
    return " | ".join(parts)


# ── Celery tasks ──────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def process_invoice(self, invoice_id: int) -> None:
    try:
        invoice = Invoice.objects.get(id=invoice_id)
    except Invoice.DoesNotExist:
        logger.error("process_invoice: Invoice %s not found", invoice_id)
        return

    invoice.status = Invoice.Status.PROCESSING
    invoice.save(update_fields=["status", "updated_at"])
    _push_update(invoice)

    try:
        from mindee import ClientV2, InferenceParameters, InferenceResponse, PathInput

        client = ClientV2(settings.MINDEE_V2_API_KEY)
        params = InferenceParameters(model_id=settings.MINDEE_MODEL_ID, confidence=True)
        input_source = PathInput(invoice.file.path)
        response = client.enqueue_and_get_result(InferenceResponse, input_source, params)

        # Use the raw API JSON directly so raw_value (the verbatim text the
        # model read) is available as a fallback when the typed value is null.
        raw_fields = response._raw_http.get("inference", {}).get("result", {}).get("fields", {})
        extracted = {
            key: _serialize_raw_field(raw_field)
            for key, raw_field in raw_fields.items()
        }

        invoice.status = Invoice.Status.PROCESSED
        invoice.extracted_data = extracted
        invoice.error_message = ""
        invoice.save(update_fields=["status", "extracted_data", "error_message", "updated_at"])
        _push_update(invoice)
        logger.info("Invoice %s processed successfully", invoice_id)

        # Build a human-readable notification title from extracted fields.
        vendor = _get_field_value(extracted, "supplier_name", "vendor_name", "seller_name", "company_name")
        amount_raw = _get_field_value(extracted, "total_amount", "amount_due", "grand_total", "subtotal")
        amount = _normalize_amount(amount_raw)
        if vendor and amount is not None:
            notif_title = f"Invoice processed \u2013 ${amount:,.2f} from {vendor}"
        elif amount is not None:
            notif_title = f"Invoice processed \u2013 ${amount:,.2f}"
        elif vendor:
            notif_title = f"Invoice processed from {vendor}"
        else:
            notif_title = f"Invoice processed \u2013 {invoice.original_filename}"
        _create_notification(invoice.user_id, Notification.Kind.INVOICE_PROCESSED, notif_title, invoice=invoice)

        check_invoice_duplicates.delay(invoice_id)

    except Exception as exc:
        logger.exception("Invoice %s processing failed: %s", invoice_id, exc)
        invoice.status = Invoice.Status.PROCESSING_FAILED
        invoice.error_message = str(exc)
        invoice.save(update_fields=["status", "error_message", "updated_at"])
        _push_update(invoice)
        if self.request.retries >= self.max_retries:
            _create_notification(
                invoice.user_id,
                Notification.Kind.INVOICE_FAILED,
                f"Failed to extract \u2013 {invoice.original_filename}",
                body=str(exc)[:200],
                invoice=invoice,
            )
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=1, default_retry_delay=15)
def check_invoice_duplicates(self, invoice_id: int) -> None:
    try:
        invoice = Invoice.objects.get(id=invoice_id)
    except Invoice.DoesNotExist:
        logger.error("check_invoice_duplicates: Invoice %s not found", invoice_id)
        return

    candidates = list(
        Invoice.objects
        .filter(user=invoice.user, status__in=_PROCESSED_STATUSES)
        .exclude(pk=invoice_id)
    )

    if not candidates:
        DuplicateCheckResult.objects.update_or_create(
            invoice=invoice,
            defaults={
                "decision": DuplicateCheckResult.Decision.UNIQUE,
                "best_match": None,
                "best_match_score": None,
                "score_details": {
                    "rule_score": 0.0,
                    "fuzzy_score": 0.0,
                    "embedding_score": None,
                    "final_score": 0.0,
                    "candidates_checked": 0,
                    "candidates_embedded": 0,
                },
            },
        )
        _push_update(invoice)
        return

    current_fields   = _extract_normalized_fields(invoice)
    candidate_fields = {c.pk: _extract_normalized_fields(c) for c in candidates}

    rule_scores  = {c.pk: _rule_score(current_fields, candidate_fields[c.pk]) for c in candidates}
    fuzzy_scores = {c.pk: _fuzzy_score(current_fields, candidate_fields[c.pk]) for c in candidates}

    embed_candidates = [
        c for c in candidates
        if (rule_scores[c.pk] + fuzzy_scores[c.pk]) / 2 >= 0.15
    ]

    embedding_scores: dict = {}
    candidates_embedded = 0

    if embed_candidates and settings.OPENAI_API_KEY:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=settings.OPENAI_API_KEY)

            needs_embed: list = []
            if invoice.embedding is None:
                needs_embed.append(invoice)
            for c in embed_candidates:
                if c.embedding is None:
                    needs_embed.append(c)

            if needs_embed:
                texts = [_make_embedding_text(_extract_normalized_fields(inv), inv) for inv in needs_embed]
                response = client.embeddings.create(model="text-embedding-3-small", input=texts)
                for inv, item in zip(needs_embed, response.data):
                    inv.embedding = item.embedding
                    inv.save(update_fields=["embedding"])

            current_embedding = invoice.embedding
            if current_embedding:
                for c in embed_candidates:
                    if c.embedding:
                        embedding_scores[c.pk] = _cosine_similarity(current_embedding, c.embedding)
                        candidates_embedded += 1

        except Exception as exc:
            logger.warning(
                "Embedding computation failed for invoice %s, falling back to rule+fuzzy: %s",
                invoice_id, exc,
            )

    use_embedding = bool(embedding_scores)
    final_scores: dict = {}
    for c in candidates:
        r = rule_scores[c.pk]
        f = fuzzy_scores[c.pk]
        e = embedding_scores.get(c.pk)
        if use_embedding and e is not None:
            final_scores[c.pk] = 0.35 * r + 0.30 * f + 0.35 * e
        else:
            final_scores[c.pk] = 0.50 * r + 0.50 * f

    best_id    = max(final_scores, key=lambda pk: final_scores[pk])
    best_score = final_scores[best_id]
    best_candidate = next(c for c in candidates if c.pk == best_id)

    # ── LLM verification ───────────────────────────────────────────────────
    llm_decision = None
    if 0.40 <= best_score < 0.90 and settings.OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=settings.OPENAI_API_KEY)
            
            prompt = (
                "You are an expert invoice auditor. Compare these two invoices and decide if they are duplicates.\n"
                "Invoices are considered duplicates if they are for the same transaction (same vendor, same amount, same date, and usually same invoice number).\n\n"
                f"NEW INVOICE:\n{current_fields}\n\n"
                f"EXISTING INVOICE MATCH:\n{candidate_fields[best_id]}\n\n"
                "Respond ONLY with a JSON object: {\"is_duplicate\": true/false, \"confidence\": 0.0-1.0, \"reason\": \"string\"}"
            )
            
            chat_response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            import json
            result = json.loads(chat_response.choices[0].message.content)
            llm_decision = result
            
            # Adjust best_score based on LLM feedback
            if result.get("is_duplicate"):
                # If LLM is confident it's a duplicate, boost score
                best_score = max(best_score, 0.85 * result.get("confidence", 1.0))
            else:
                # If LLM says it's not a duplicate, lower score
                best_score = min(best_score, 1.0 - (0.60 * result.get("confidence", 1.0)))
                
        except Exception as exc:
            logger.warning("LLM verification failed for invoice %s: %s", invoice_id, exc)

    if best_score >= 0.85:
        decision = DuplicateCheckResult.Decision.DUPLICATE
    elif best_score >= 0.55:
        decision = DuplicateCheckResult.Decision.POSSIBLE_DUPLICATE
    else:
        decision = DuplicateCheckResult.Decision.UNIQUE

    best_match       = best_candidate if decision != DuplicateCheckResult.Decision.UNIQUE else None
    best_match_score = best_score     if decision != DuplicateCheckResult.Decision.UNIQUE else None

    DuplicateCheckResult.objects.update_or_create(
        invoice=invoice,
        defaults={
            "decision":         decision,
            "best_match":       best_match,
            "best_match_score": best_match_score,
            "score_details": {
                "rule_score":          rule_scores[best_id],
                "fuzzy_score":         fuzzy_scores[best_id],
                "embedding_score":     embedding_scores.get(best_id),
                "final_score":         best_score,
                "llm_verification":    llm_decision,
                "candidates_checked":  len(candidates),
                "candidates_embedded": candidates_embedded,
            },
        },
    )

    _push_update(invoice)
    logger.info(
        "Duplicate check for invoice %s: %s (score=%.3f, candidates=%d)",
        invoice_id, decision, best_score, len(candidates),
    )
