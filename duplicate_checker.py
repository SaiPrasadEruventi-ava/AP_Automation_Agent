"""
duplicate_checker.py — Processed-Invoice Log (Redis-backed)
=======================================================================
Manages the processed-invoices log so the pipeline can catch duplicate
invoice submissions BEFORE running the 3-way match. Originally backed
by data/processed/processed_invoices.json; now backed by Redis
(redis_store.py) since Vercel's filesystem is read-only/ephemeral.
Public function signatures are unchanged.

Responsibilities:
    - Check if an invoice already exists (exact / tampered / fuzzy match)
    - Add newly processed invoices to the log (call register_invoice()
      after a final decision is reached, regardless of outcome)

Detection rules
---------------
    1. Exact match — same invoice_number already in the log with the
       same amount -> flagged as a straight duplicate resubmission.
    2. Tampered    — same invoice_number already in the log but a
       DIFFERENT amount -> flagged as possible tampering, always
       routed to Manual Review regardless of amount.
    3. Fuzzy match — different invoice_number, but same vendor + same
       amount within a configurable day window -> flagged as a
       suspicious near-duplicate (e.g. vendor re-billing under a new
       invoice number).
"""

from datetime import datetime, timedelta

import redis_store

FUZZY_WINDOW_DAYS = 7


def _normalise(text: str) -> str:
    return str(text or "").lower().strip()


def _parse_amount(amount) -> float:
    try:
        return round(float(str(amount).replace(",", "").replace("$", "")), 2)
    except (ValueError, TypeError):
        return 0.0


def _parse_date(date_str: str) -> datetime:
    try:
        return datetime.strptime(str(date_str).strip(), "%Y-%m-%d")
    except (ValueError, TypeError):
        return datetime.today()


def _sap_invoices(invoice_number: str, vendor_code: str) -> list:
    """Live (posted or parked) supplier invoices in SAP with this
    reference for this vendor — [] when not in SAP mode or unreachable."""
    import erp_mock
    if not erp_mock.using_sap() or not invoice_number:
        return []
    try:
        import erp_sap
        docs = erp_sap.find_supplier_invoices(reference=invoice_number,
                                              vendor=vendor_code or None)
    except Exception as e:  # SAP unreachable must not block processing
        print(f"  ⚠️  SAP duplicate check skipped: {e}")
        return []
    return [d for d in docs if d["is_posted"] or d["is_parked"]]


def _describe_sap(docs: list) -> str:
    return "; ".join(
        f"SAP invoice {d['document']}/{d['fiscal_year']} ({d['status']}, "
        f"{d['currency']} {d['gross_amount']:,.2f}, entered {d['entered_on']})"
        for d in docs)


def check_duplicate(invoice_number: str, vendor_name: str,
                     amount, invoice_date: str, vendor_code: str = None) -> dict:
    """
    Check if an invoice is a duplicate against the processed log.

    Returns:
        {
            "is_duplicate":     bool,
            "duplicate_type":   "Exact" | "Tampered" | "Fuzzy" | None,
            "duplicate_reason": str,
            "original_invoice": str | None,
        }
    """
    print("\n" + "="*55)
    print("  DUPLICATE INVOICE CHECK")
    print("="*55)

    inv_amount = _parse_amount(amount)
    inv_date   = _parse_date(invoice_date)
    sap_docs   = _sap_invoices(invoice_number, vendor_code)

    # Rule 1 / 2 — exact-number lookup (O(1) via the Redis hash)
    existing = redis_store.processed_get(invoice_number)
    if existing:
        rec_amount = _parse_amount(existing.get("amount", "0"))
        if abs(rec_amount - inv_amount) < 0.01:
            reason = (
                f"Invoice {invoice_number} was already processed on "
                f"{existing.get('processed_date')} with status "
                f"'{existing.get('status')}'"
                + (f" — and it already exists in SAP: {_describe_sap(sap_docs)}" if sap_docs else "")
            )
            print(f"  🔁 EXACT DUPLICATE: {reason}")
            return {
                "is_duplicate":     True,
                "duplicate_type":   "Exact",
                "duplicate_reason": reason,
                "original_invoice": existing.get("invoice_number"),
            }
        else:
            reason = (
                f"Invoice {invoice_number} exists in log with amount "
                f"${rec_amount:,.2f} but current invoice shows "
                f"${inv_amount:,.2f} — possible tampering"
            )
            print(f"  ⚠️  TAMPERED RESUBMISSION: {reason}")
            return {
                "is_duplicate":     True,
                "duplicate_type":   "Tampered",
                "duplicate_reason": reason,
                "original_invoice": existing.get("invoice_number"),
            }

    # Rule 1b — the ERP itself already holds this vendor invoice (posted
    # or parked), e.g. entered directly in MIRO or by an earlier run
    # whose AP log was cleared.
    if sap_docs:
        reason = (f"Invoice {invoice_number} already exists in SAP for this vendor: "
                  f"{_describe_sap(sap_docs)}")
        print(f"  🔁 SAP DUPLICATE: {reason}")
        return {
            "is_duplicate":     True,
            "duplicate_type":   "SAP " + sap_docs[0]["status"],
            "duplicate_reason": reason,
            "original_invoice": invoice_number,
        }

    # Rule 3 — fuzzy: same vendor + same amount within the day window.
    # Query the by-date sorted set for candidates in range, then check
    # vendor/amount on each (cheap — the window is narrow).
    window_start = (inv_date - timedelta(days=FUZZY_WINDOW_DAYS)).timestamp()
    window_end   = (inv_date + timedelta(days=FUZZY_WINDOW_DAYS)).timestamp()
    candidates   = redis_store.processed_candidates_in_window(window_start, window_end)

    for candidate_number in candidates:
        if _normalise(candidate_number) == _normalise(invoice_number):
            continue  # already handled by the exact-match check above
        record = redis_store.processed_get(candidate_number)
        if not record:
            continue
        rec_vendor = _normalise(record.get("vendor_name", ""))
        rec_amount = _parse_amount(record.get("amount", "0"))
        rec_date   = _parse_date(record.get("processed_date", ""))
        days_diff  = abs((inv_date - rec_date).days)

        if (_normalise(vendor_name) == rec_vendor
                and abs(rec_amount - inv_amount) < 0.01
                and days_diff <= FUZZY_WINDOW_DAYS
                and rec_vendor):
            reason = (
                f"Suspicious: vendor '{vendor_name}' submitted "
                f"${inv_amount:,.2f} within {days_diff} day(s) of "
                f"previously processed invoice {record.get('invoice_number')}"
            )
            print(f"  🔁 FUZZY DUPLICATE: {reason}")
            return {
                "is_duplicate":     True,
                "duplicate_type":   "Fuzzy",
                "duplicate_reason": reason,
                "original_invoice": record.get("invoice_number"),
            }

    print(f"  ✅ No duplicate found for {invoice_number}")
    return {
        "is_duplicate":     False,
        "duplicate_type":   None,
        "duplicate_reason": "",
        "original_invoice": None,
    }


def register_invoice(invoice_number: str, vendor_name: str,
                      po_number: str, amount, currency: str,
                      invoice_date: str, status: str,
                      sap_posting: dict = None) -> None:
    """Add a processed invoice to the log so future submissions can be checked against it."""
    sap_posting = sap_posting or {}
    record = {
        "sap_action":      sap_posting.get("action", ""),
        "sap_document":    sap_posting.get("document", ""),
        "sap_fiscal_year": sap_posting.get("fiscal_year", ""),
        "invoice_number":  invoice_number,
        "vendor_name":     vendor_name,
        "po_number":       po_number,
        "amount":          str(amount),
        "currency":        currency,
        "processed_date":  invoice_date,
        "status":          status,
    }
    redis_store.processed_add(record, invoice_date_ts=_parse_date(invoice_date).timestamp())
    print(f"  📝 Registered {invoice_number} in duplicate-check log (status: {status})")


def get_all_processed() -> list:
    return redis_store.processed_get_all()


def remove_invoice(invoice_number: str) -> None:
    """Remove one invoice from the processed log so it can be resubmitted
    and reprocessed from scratch — for reusing the same test invoice."""
    redis_store.processed_remove(invoice_number)
    print(f"  🗑️  Removed {invoice_number} from duplicate-check log — it can be reprocessed now.")


def clear_log() -> None:
    redis_store.processed_clear()
    print("  Invoice log cleared.")
