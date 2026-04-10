"""
Dashboard aggregation logic — all heavy computation lives here so views stay thin.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from .models import DuplicateCheckResult, Invoice

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

STATUS_COLORS: dict[str, str] = {
    Invoice.Status.UPLOADED:          "#6366f1",
    Invoice.Status.PROCESSING:        "#f59e0b",
    Invoice.Status.PROCESSED:         "#3b82f6",
    Invoice.Status.PROCESSING_FAILED: "#ef4444",
    Invoice.Status.PENDING_REVIEW:    "#f97316",
    Invoice.Status.APPROVED:          "#22c55e",
    Invoice.Status.REJECTED:          "#64748b",
}

_MISSING_DATA_FLAGS = [
    "vendor_name", "invoice_date", "total_amount",
    "due_date", "invoice_number",
]

# Ordered list of keys Mindee may use for the invoice total.
_AMOUNT_KEYS = ("total_amount", "total_net", "amount_due", "grand_total", "subtotal")


def _field_value(entry):
    """Unwrap Mindee-style field objects to their raw value."""
    if isinstance(entry, dict):
        return entry.get("value")
    return entry


def _parse_range(range_str: str) -> int:
    """Return number of days for a range string like '7d', '30d', '90d'."""
    mapping = {"7d": 7, "30d": 30, "90d": 90}
    return mapping.get(range_str, 30)


def _safe_amount(invoice: Invoice) -> float:
    """Extract the invoice total from extracted_data.

    Mindee serialises every field as {"value": ..., "confidence": ...}, so we
    must unwrap the dict before converting to float.  We also try several
    common field names because different Mindee models use different keys.
    """
    data = invoice.extracted_data
    if not data:
        return 0.0
    for key in _AMOUNT_KEYS:
        entry = data.get(key)
        if not entry:
            continue
        # Unwrap Mindee's {"value": ..., "confidence": ...} envelope.
        raw = _field_value(entry)
        if raw is None:
            continue
        # Strip currency symbols, spaces, commas — keep digits and dot.
        cleaned = re.sub(r"[^\d.]", "", str(raw))
        try:
            return float(cleaned)
        except (TypeError, ValueError):
            continue
    return 0.0


def _is_missing_data(invoice: Invoice) -> bool:
    """Return True if any key financial fields are absent from extracted_data."""
    if not invoice.extracted_data:
        return True
    return any(
        not invoice.extracted_data.get(field)
        for field in _MISSING_DATA_FLAGS
    )


def _invoice_stub(invoice: Invoice) -> dict:
    vendor = _field_value((invoice.extracted_data or {}).get("vendor_name")) or "—"
    return {
        "id":                invoice.id,
        "original_filename": invoice.original_filename,
        "vendor":            vendor,
        "amount":            _safe_amount(invoice),
        "status":            invoice.status,
        "created_at":        invoice.created_at.isoformat(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Core computation
# ──────────────────────────────────────────────────────────────────────────────

def compute_dashboard(organization, range_str: str = "30d") -> dict:
    days = _parse_range(range_str)
    now  = timezone.now()
    period_start = now - timedelta(days=days)
    prev_start   = period_start - timedelta(days=days)

    user_qs = Invoice.objects.filter(organization=organization)

    # ── Current-period invoices ──────────────────────────────────────────────
    current_qs = user_qs.filter(created_at__gte=period_start)
    prev_qs    = user_qs.filter(created_at__gte=prev_start, created_at__lt=period_start)

    current_list = list(current_qs.select_related("duplicate_check"))
    prev_list    = list(prev_qs)

    current_count  = len(current_list)
    prev_count     = len(prev_list)

    current_amount = sum(_safe_amount(inv) for inv in current_list)
    prev_amount    = sum(_safe_amount(inv) for inv in prev_list)

    def pct_change(curr: float, prev: float) -> float | None:
        if prev == 0:
            return None
        return round((curr - prev) / prev * 100, 1)

    # ── Status counts (current period) ──────────────────────────────────────
    status_counter: dict[str, int] = {}
    for inv in current_list:
        status_counter[inv.status] = status_counter.get(inv.status, 0) + 1

    approved_count  = status_counter.get(Invoice.Status.APPROVED, 0)
    pending_count   = status_counter.get(Invoice.Status.PENDING_REVIEW, 0)
    failed_count    = status_counter.get(Invoice.Status.PROCESSING_FAILED, 0)

    # Approved amount (current period)
    approved_amount = sum(
        _safe_amount(inv) for inv in current_list
        if inv.status == Invoice.Status.APPROVED
    )
    prev_approved_amount = sum(
        _safe_amount(inv) for inv in prev_list
        if inv.status == Invoice.Status.APPROVED
    )

    # Duplicates flagged (all time, not dismissed)
    duplicates_flagged = DuplicateCheckResult.objects.filter(
        invoice__organization=organization,
        decision__in=[DuplicateCheckResult.Decision.DUPLICATE, DuplicateCheckResult.Decision.POSSIBLE_DUPLICATE],
        dismissed=False,
    ).count()

    summary = {
        "total_invoices":       current_count,
        "total_amount":         round(current_amount, 2),
        "approved_count":       approved_count,
        "approved_amount":      round(approved_amount, 2),
        "pending_review_count": pending_count,
        "failed_count":         failed_count,
        "duplicates_flagged":   duplicates_flagged,
        "prev_total_invoices":  prev_count,
        "prev_total_amount":    round(prev_amount, 2),
        "pct_change_invoices":  pct_change(current_count, prev_count),
        "pct_change_amount":    pct_change(current_amount, prev_amount),
        "pct_change_approved":  pct_change(approved_amount, prev_approved_amount),
        "all_time_total":       user_qs.count(),
    }

    # ── Monthly trend (last 12 months) ───────────────────────────────────────
    twelve_months_ago = now - timedelta(days=365)
    trend_invoices = list(
        user_qs
        .filter(created_at__gte=twelve_months_ago)
        .only("id", "status", "extracted_data", "created_at")
    )

    # Build month buckets  {(year, month): {...}}
    month_map: dict[tuple[int, int], dict] = {}
    for inv in trend_invoices:
        key = (inv.created_at.year, inv.created_at.month)
        if key not in month_map:
            month_map[key] = {
                "total_amount":    0.0,
                "approved_amount": 0.0,
                "count":           0,
                "approved_count":  0,
            }
        amt = _safe_amount(inv)
        month_map[key]["total_amount"]    += amt
        month_map[key]["count"]           += 1
        if inv.status == Invoice.Status.APPROVED:
            month_map[key]["approved_amount"] += amt
            month_map[key]["approved_count"]  += 1

    # Fill every calendar month for the last 12, stepping back one month at a
    # time so we never skip or duplicate a month (timedelta(days=30) is wrong
    # because months have 28–31 days).
    cursor = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_slots: list[tuple[int, int, object]] = []
    for _ in range(12):
        month_slots.insert(0, (cursor.year, cursor.month, cursor))
        cursor = (cursor.replace(month=cursor.month - 1) if cursor.month > 1
                  else cursor.replace(year=cursor.year - 1, month=12))

    monthly_trend = []
    for year, month, dt in month_slots:
        key   = (year, month)
        entry = month_map.get(key, {"total_amount": 0.0, "approved_amount": 0.0, "count": 0, "approved_count": 0})
        monthly_trend.append({
            "year":            year,
            "month":           month,
            "label":           dt.strftime("%b %Y"),
            "total_amount":    round(entry["total_amount"], 2),
            "approved_amount": round(entry["approved_amount"], 2),
            "count":           entry["count"],
            "approved_count":  entry["approved_count"],
        })

    # ── Status breakdown (all-time) ──────────────────────────────────────────
    all_counts = (
        user_qs
        .values("status")
        .annotate(count=Count("id"))
    )
    status_breakdown = [
        {
            "status": row["status"],
            "label":  Invoice.Status(row["status"]).label,
            "count":  row["count"],
            "color":  STATUS_COLORS.get(row["status"], "#6366f1"),
        }
        for row in all_counts
        if row["count"] > 0
    ]

    # ── Action center ────────────────────────────────────────────────────────
    def fetch_action_items(filter_q, limit=5) -> list[dict]:
        qs = (
            user_qs
            .filter(filter_q)
            .only("id", "original_filename", "extracted_data", "status", "created_at")
            .order_by("-created_at")[:limit]
        )
        return [_invoice_stub(inv) for inv in qs]

    missing_data_ids = [
        inv.id for inv in
        user_qs.filter(
            status__in=[Invoice.Status.PROCESSED, Invoice.Status.PENDING_REVIEW]
        ).only("id", "extracted_data")
        if _is_missing_data(inv)
    ][:5]

    action_center = {
        "pending_review": fetch_action_items(Q(status=Invoice.Status.PENDING_REVIEW)),
        "failed":         fetch_action_items(Q(status=Invoice.Status.PROCESSING_FAILED)),
        "missing_data":   [
            _invoice_stub(inv) for inv in
            user_qs.filter(id__in=missing_data_ids).only("id", "original_filename", "extracted_data", "status", "created_at")
        ],
    }

    # ── Recent invoices ──────────────────────────────────────────────────────
    recent_invoices = [
        _invoice_stub(inv)
        for inv in user_qs.only("id", "original_filename", "extracted_data", "status", "created_at")[:10]
    ]

    # ── AI Insights ──────────────────────────────────────────────────────────
    insights = _build_insights(
        current_list=current_list,
        prev_list=prev_list,
        current_amount=current_amount,
        prev_amount=prev_amount,
        duplicates_flagged=duplicates_flagged,
        failed_count=failed_count,
        range_str=range_str,
    )

    return {
        "range":            range_str,
        "summary":          summary,
        "monthly_trend":    monthly_trend,
        "status_breakdown": status_breakdown,
        "action_center":    action_center,
        "recent_invoices":  recent_invoices,
        "ai_insights":      insights,
    }


# ──────────────────────────────────────────────────────────────────────────────
# AI Insights builder
# ──────────────────────────────────────────────────────────────────────────────

def _build_insights(
    *,
    current_list: list[Invoice],
    prev_list: list[Invoice],
    current_amount: float,
    prev_amount: float,
    duplicates_flagged: int,
    failed_count: int,
    range_str: str,
) -> list[dict]:
    insights: list[dict] = []

    # 1 — Expense trend
    if prev_amount > 0:
        pct = round((current_amount - prev_amount) / prev_amount * 100, 1)
        if abs(pct) >= 5:
            direction = "increased" if pct > 0 else "decreased"
            severity = "warning" if pct > 20 else ("success" if pct < -5 else "info")
            insights.append({
                "type":     "expense_trend",
                "severity": severity,
                "title":    f"Expenses {direction} by {abs(pct)}%",
                "body":     (
                    f"Total invoiced amount {direction} by {abs(pct)}% "
                    f"compared to the previous {range_str} period."
                ),
            })

    # 2 — Top vendor
    vendor_totals: dict[str, float] = {}
    for inv in current_list:
        vendor = _field_value((inv.extracted_data or {}).get("vendor_name"))
        if vendor:
            vendor_key = str(vendor)
            vendor_totals[vendor_key] = vendor_totals.get(vendor_key, 0) + _safe_amount(inv)
    if vendor_totals:
        top_vendor = max(vendor_totals, key=lambda v: vendor_totals[v])
        top_amount = vendor_totals[top_vendor]
        share = round(top_amount / current_amount * 100, 1) if current_amount else 0
        if share >= 30:
            insights.append({
                "type":     "top_vendor",
                "severity": "info",
                "title":    f"{top_vendor} accounts for {share}% of spend",
                "body":     (
                    f"{top_vendor} is your top vendor this period "
                    f"with ${top_amount:,.2f} ({share}% of total spend)."
                ),
            })

    # 3 — Duplicate warning
    if duplicates_flagged > 0:
        insights.append({
            "type":     "duplicates",
            "severity": "warning",
            "title":    f"{duplicates_flagged} potential duplicate invoice{'s' if duplicates_flagged > 1 else ''} detected",
            "body":     (
                f"{duplicates_flagged} invoice{'s' if duplicates_flagged > 1 else ''} "
                f"{'were' if duplicates_flagged > 1 else 'was'} flagged as possible "
                f"duplicate{'s' if duplicates_flagged > 1 else ''}. Review them to avoid double-payment."
            ),
        })

    # 4 — Unusual amounts (outliers: > mean + 2 * std dev)
    amounts = [_safe_amount(inv) for inv in current_list if _safe_amount(inv) > 0]
    if len(amounts) >= 3:
        mean = sum(amounts) / len(amounts)
        variance = sum((a - mean) ** 2 for a in amounts) / len(amounts)
        std = variance ** 0.5
        threshold = mean + 2 * std
        outliers = [a for a in amounts if a > threshold]
        if outliers:
            insights.append({
                "type":     "unusual_amounts",
                "severity": "warning",
                "title":    f"{len(outliers)} unusually large invoice{'s' if len(outliers) > 1 else ''}",
                "body":     (
                    f"{len(outliers)} invoice{'s' if len(outliers) > 1 else ''} "
                    f"{'are' if len(outliers) > 1 else 'is'} significantly above your average "
                    f"(${mean:,.0f}). Review them for accuracy."
                ),
            })

    # 5 — Processing failures
    if failed_count > 0:
        insights.append({
            "type":     "failed_processing",
            "severity": "error",
            "title":    f"{failed_count} invoice{'s' if failed_count > 1 else ''} failed to process",
            "body":     (
                f"{failed_count} invoice{'s' if failed_count > 1 else ''} could not be "
                f"processed automatically. Reprocess or review them manually."
            ),
        })

    # 6 — Missing data
    missing_count = sum(1 for inv in current_list if _is_missing_data(inv))
    if missing_count > 0:
        insights.append({
            "type":     "missing_data",
            "severity": "info",
            "title":    f"{missing_count} invoice{'s' if missing_count > 1 else ''} missing key fields",
            "body":     (
                f"{missing_count} invoice{'s' if missing_count > 1 else ''} "
                f"{'are' if missing_count > 1 else 'is'} missing fields such as vendor name, "
                f"amount, or date. Complete them for accurate reporting."
            ),
        })

    return insights
