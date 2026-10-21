"""
run_ap.py — AP Automation Agent v3.2 — Multi-Provider Mail (Graph / Gmail)
=======================================================
Processes ALL emails in the 'ToBeProcessed' folder in one run.
Each email is processed independently; a failure on one never
blocks the others.  After processing, each email is moved to
'Processed' (perfect match) or 'Exceptions' (everything else).
A batch summary is printed and emailed at the end.

v3.0 — Invoice + ERP-mock input model
---------------------------------------
The user now emails ONLY an Invoice PDF (required) plus an optional
Supporting document (not matched against). Purchase Order and Goods
Receipt Note data are looked up from the mock ERP workbook
(data/erp/ERP_Mock_Data.xlsx via erp_mock.py) using the PO Number on
the invoice — they are no longer expected as email attachments.

Usage
-----
  python run_ap.py                      process every email in ToBeProcessed
  python run_ap.py --watch              poll every 30 s and drain the folder each time
  python run_ap.py --test               test mail provider connection + list waiting emails
  python run_ap.py --mock               run the S01 + S03 scenarios via ERP lookup (no mailbox needed)
  python run_ap.py --mock --variance    mock with the price-variance invoice (S03) only
  python run_ap.py --no-hitl            skip interactive HITL (send review email only)
  python run_ap.py --hitl-mode web      Manual Review sends clickable Approve/Reject/
                                         Clarify links — requires approval_portal.py
                                         to be running (python approval_portal.py)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows consoles default to cp1252, which cannot print the emoji this
# app logs with — reconfigure once at the entry point instead.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import argparse
import json
import re
import time
import traceback
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# Resolve .env relative to this file's directory, not the current working
# directory — run_ap.py is meant to be runnable from anywhere (a different
# shell cwd, a scheduled task, --watch as a background service, etc.).
load_dotenv(Path(__file__).resolve().parent / ".env")

RECIPIENT          = os.getenv("AP_NOTIFY_EMAIL",           "balakrishna.m@avaali.com")
REVIEWER_EMAIL     = os.getenv("AP_REVIEWER_EMAIL",         "balakrishna.m@avaali.com")
VARIANCE_THRESHOLD = float(os.getenv("VARIANCE_THRESHOLD_PCT", "2.0"))
POLL_INTERVAL      = int(os.getenv("POLL_INTERVAL_SECONDS",    "30"))
SOURCE_FOLDER      = os.getenv("AP_SOURCE_FOLDER",           "ToBeProcessed")


# ── Mock data (LEGACY — kept for reference, no longer used by run_mock()) ──
# run_mock() now exercises the real ERP-lookup flow via erp_mock.py.
# This dict embeds its own PO/GRN data inline, which doesn't reflect the
# v3.0 architecture (PO/GRN come from the ERP workbook, not hardcoded
# alongside the invoice). Left here only in case any external script
# still imports MOCK_SETS directly.

MOCK_SETS = {
    "perfect_match": {
        "invoice": {
            "invoice_number": "INV-M001", "vendor_name": "ABC Industries",
            "invoice_date": "2026-06-20", "po_number": "PO-M001",
            "currency": "USD", "total_amount": 15825.00,
            "subtotal": 14250.00, "tax_amount": 1425.00,
            "freight": 150.00, "discount": 0,
            "tax_type": "GST @ 10%", "payment_terms": "Net 30",
            "items": [
                {"item": "Dell Laptop XPS-15",      "quantity": 10, "unit_price": 1200.00},
                {"item": "Samsung Monitor 27-inch",  "quantity":  5, "unit_price":  450.00},
            ],
        },
        "po": {
            "po_number": "PO-M001", "vendor_name": "ABC Industries",
            "po_date": "2026-06-10", "currency": "USD", "po_amount": 15825.00,
            "items": [
                {"item": "Dell Laptop XPS-15",      "quantity": 10, "unit_price": 1200.00},
                {"item": "Samsung Monitor 27-inch",  "quantity":  5, "unit_price":  450.00},
            ],
        },
        "grn": {
            "grn_number": "GRN-M001", "po_number": "PO-M001",
            "vendor_name": "ABC Industries", "received_date": "2026-06-18",
            "received_by": "Balakrishna M",
            "items": [
                {"item": "Dell Laptop XPS-15",     "quantity_ordered": 10,
                 "quantity_received": 10, "quantity_accepted": 10, "condition": "Good"},
                {"item": "Samsung Monitor 27-inch", "quantity_ordered":  5,
                 "quantity_received":  5, "quantity_accepted":  5, "condition": "Good"},
            ],
        },
    },
    "price_variance": {
        "invoice": {
            "invoice_number": "INV-M002", "vendor_name": "Global Supplies Co",
            "invoice_date": "2026-06-17", "po_number": "PO-M002",
            "currency": "USD", "total_amount": 8773.20,
            "subtotal": 7610.00, "tax_amount": 913.20,
            "freight": 250.00, "discount": 0,
            "tax_type": "VAT @ 12%", "payment_terms": "Net 30",
            "items": [
                {"item": "Office Chair Ergonomic", "quantity": 20, "unit_price": 163.00},
                {"item": "Executive Desk 180cm",   "quantity": 10, "unit_price": 435.00},
            ],
        },
        "po": {
            "po_number": "PO-M002", "vendor_name": "Global Supplies Co",
            "po_date": "2026-06-05", "currency": "USD", "po_amount": 8250.00,
            "items": [
                {"item": "Office Chair Ergonomic", "quantity": 20, "unit_price": 150.00},
                {"item": "Executive Desk 180cm",   "quantity": 10, "unit_price": 400.00},
            ],
        },
        "grn": {
            "grn_number": "GRN-M002", "po_number": "PO-M002",
            "vendor_name": "Global Supplies Co", "received_date": "2026-06-16",
            "received_by": "Raj K",
            "items": [
                {"item": "Office Chair Ergonomic", "quantity_ordered": 20,
                 "quantity_received": 20, "quantity_accepted": 20, "condition": "Good"},
                {"item": "Executive Desk 180cm",   "quantity_ordered": 10,
                 "quantity_received": 10, "quantity_accepted": 10, "condition": "Good"},
            ],
        },
    },
}


# ── Single-invoice pipeline ───────────────────────────────────────────────────

def process_invoice_set(invoice_data:    dict,
                         po_data:         dict,
                         grn_data:        dict,
                         email_ref:       dict = None,
                         hitl_mode             = True,
                         supporting_docs: list = None) -> dict:
    """
    Run the complete AP pipeline for ONE invoice.

    v3.0 input model
    -----------------
    invoice_data     — extracted via LLM from the emailed Invoice PDF.
    po_data/grn_data — looked up from the mock ERP workbook (erp_mock.py)
                       using the PO Number on the invoice. They are NO
                       LONGER extracted from emailed PDFs.
    supporting_docs  — optional list of {"file_name", "preview"} dicts
                       for any non-Invoice attachment the user sent
                       (packing slip, vendor note, etc.). Stored for
                       audit/reference only — never matched against.

    hitl_mode — controls how Manual Review is resolved when triggered:
        "interactive" / True  — blocks the run, prompts in the terminal
        "email_only"  / False — sends the review email, returns
                                 immediately with "Manual Review
                                 Required" (no automatic reply parsing)
        "web"                 — sends the review email with clickable
                                 Approve/Reject/Clarify portal links,
                                 PERSISTS the pending state to disk via
                                 pending_reviews.py, and returns
                                 immediately with "Pending Web Review".
                                 The pipeline resumes later — when the
                                 reviewer clicks a link — via
                                 resume_after_review() below, called by
                                 approval_portal.py.

    Steps
    -----
    1. Validate Invoice/PO/GRN are all present (PO/GRN absence now means
       "not found in ERP" rather than "not attached to email")
    2. 3-Way Match (header, line items, GRN)
    3. Apply business rules → Perfect Match or Manual Review
    4. HITL review if required (interactive CLI, email-only, or web portal)
    5. Generate LLM summary
    6. Send rich result email
    7. Move source email: Processed (approved) or Exceptions (everything else)

    Returns a result dict with keys:
        invoice_number, vendor_name, po_number, final_status, reasons, …
    """
    from matcher            import run_three_way_match, apply_business_rules
    from hitl_review        import process_hitl_review
    from summary_sender     import (generate_summary, send_result_email,
                                    send_review_request_email)

    supporting_docs = supporting_docs or []

    # ── 1. Validate presence ──────────────────────────────────────────
    missing = []
    if not invoice_data: missing.append("Invoice")
    if not po_data:       missing.append("PO (not found in ERP)")
    if not grn_data:
        missing.append(
            f"GRN (no goods receipt posted in {po_data.get('erp_source') or 'ERP'} for PO "
            f"{po_data.get('po_number', '')} — goods not received yet)" if po_data
            else "GRN (not found in ERP — goods may not be received yet)")

    if missing:
        print(f"\n  ⚠️  Missing documents: {missing}")
        partial_match = {
            "header_match": False, "line_match": False, "grn_match": False,
            "variance": 0, "variance_percentage": 0,
            "po_total": 0, "invoice_total": 0,
            "item_results": [], "mismatches": [f"Missing: {', '.join(missing)}"],
        }
        partial_decision = {
            "decision": "Incomplete — Missing Documents",
            "review_required": True,
            "reasons": [
                f"Missing: {', '.join(missing)}. "
                "Please confirm the PO/GRN exist in the ERP system, or "
                "resend the invoice with the correct PO Number."
            ],
        }
        summary = (
            f"Invoice processing could not complete. "
            f"Missing: {', '.join(missing)}. "
            f"Final Decision: Incomplete — Missing Documents."
        )
        send_result_email(
            invoice_data       = invoice_data or {},
            po_data            = po_data or {},
            grn_data           = grn_data or {},
            matching           = partial_match,
            final_status       = "Incomplete — Missing Documents",
            summary            = summary,
            decision_info      = partial_decision,
            recipient          = RECIPIENT,
            variance_threshold = VARIANCE_THRESHOLD,
        )
        _persist_exception_record(
            invoice_data or {}, "Incomplete — Missing Documents",
            partial_decision["reasons"], po_number=(po_data or {}).get("po_number", ""),
            subject=(email_ref or {}).get("subject", "Manual upload"),
            po_data=po_data or {}, grn_data=grn_data or {}, summary=summary,
        )
        if email_ref:
            from mail_provider import mark_exception
            mark_exception(email_ref)
        return {"final_status": "Incomplete — Missing Documents", "missing": missing,
                "reasons": partial_decision["reasons"],
                "invoice_number": (invoice_data or {}).get("invoice_number", "unknown")}


    # ── 1b. Duplicate check ───────────────────────────────────────────
    from duplicate_checker import check_duplicate
    dup_check = check_duplicate(
        invoice_number = invoice_data.get("invoice_number", ""),
        vendor_name    = invoice_data.get("vendor_name", ""),
        amount         = invoice_data.get("total_amount", 0),
        invoice_date   = invoice_data.get("invoice_date", ""),
        vendor_code    = po_data.get("sap_vendor", ""),
    )

    # ── 2. 3-Way Matching ─────────────────────────────────────────────
    matching      = run_three_way_match(invoice_data, po_data, grn_data,
                                         variance_threshold=VARIANCE_THRESHOLD)
    # Pass a state dict so apply_business_rules can derive scenario-specific reasons
    _match_state  = {
        "invoice_data":    invoice_data,
        "po_data":         po_data,
        "grn_data":        grn_data,
        "duplicate_check": dup_check,
    }
    decision_info = apply_business_rules(matching, VARIANCE_THRESHOLD, state=_match_state)
    # ── 2b. Reflect the decision in SAP: post a clean invoice, park one
    # that needs review (may downgrade a Perfect Match SAP refuses) ────
    _sap_sync_decision(invoice_data, po_data, decision_info, dup_check)
    final_status  = decision_info["decision"]
    reviewer_info = None


    # ── Normalise hitl_mode: accept legacy bool or new string modes ────
    if hitl_mode is True:
        hitl_mode = "interactive"
    elif hitl_mode is False:
        hitl_mode = "email_only"
    # else: already a string mode — "interactive" | "email_only" | "web"

    # ── 3. HITL ───────────────────────────────────────────────────────
    if decision_info["review_required"]:
        if hitl_mode == "web":
            # Web-portal mode does NOT block here — it persists state to
            # disk and returns immediately. The pipeline resumes later
            # via resume_after_review() when the reviewer clicks a link.
            from pending_reviews import create_pending_review
            email_entry_id = email_ref.get("id") if email_ref else None
            token = create_pending_review(
                invoice_data, po_data, grn_data, matching, decision_info,
                supporting_docs=supporting_docs,
                email_entry_id=email_entry_id,
                dup_check=dup_check,
            )
            send_review_request_email(
                invoice_data  = invoice_data,
                matching      = matching,
                decision_info = decision_info,
                recipient     = REVIEWER_EMAIL,
                token         = token,
            )
            print(f"\n  ⏸  Awaiting web-portal decision — review email sent "
                  f"to {REVIEWER_EMAIL}")
            print(f"     Review link: {os.getenv('AP_APPROVAL_PORTAL_URL','http://localhost:8000')}/review/{token}")
            return {
                "invoice_number":  invoice_data.get("invoice_number"),
                "vendor_name":     invoice_data.get("vendor_name"),
                "po_number":       po_data.get("po_number"),
                "final_status":   "Pending Web Review",
                "reasons":         decision_info.get("reasons", []),
                "review_token":    token,
                "sap_posting":     decision_info.get("sap_posting"),
                "subject":         None,  # filled in by caller if applicable
            }

        send_review_request_email(
            invoice_data  = invoice_data,
            matching      = matching,
            decision_info = decision_info,
            recipient     = REVIEWER_EMAIL,
        )
        if hitl_mode == "interactive":
            review        = process_hitl_review(
                invoice_data, po_data, grn_data, matching, decision_info
            )
            final_status  = review["final_status"]
            reviewer_info = review
            _sap_apply_review(invoice_data, po_data, decision_info, final_status,
                              dup_check.get("is_duplicate", False))
        else:  # "email_only"
            final_status = "Manual Review Required"
            print(f"\n  Review request sent to {REVIEWER_EMAIL}")

    # ── 4. Summary + email ────────────────────────────────────────────
    summary, result = _finalize_decision(
        invoice_data, po_data, grn_data, matching, decision_info,
        final_status, reviewer_info, dup_check, email_ref, supporting_docs,
    )
    return result


def _finalize_decision(invoice_data: dict, po_data: dict, grn_data: dict,
                        matching: dict, decision_info: dict,
                        final_status: str, reviewer_info: dict,
                        dup_check: dict, email_ref: dict,
                        supporting_docs: list) -> tuple:
    """
    Steps 4-7 of the pipeline, factored out so both the synchronous
    CLI/email_only path (process_invoice_set, immediately after the
    decision is known) and the asynchronous web-portal resume path
    (resume_after_review, called later when a reviewer clicks a link)
    can share identical finishing logic:

        4. Generate LLM summary
        5. Send rich result email
        6. Register in duplicate-check log
        7. Move source email (Processed / Exceptions)

    Returns (summary, result_dict).
    """
    from summary_sender import generate_summary, send_result_email

    summary = generate_summary(
        invoice_data  = invoice_data,
        po_data       = po_data,
        grn_data      = grn_data,
        matching      = matching,
        final_status  = final_status,
        reviewer_info = reviewer_info,
    )
    send_result_email(
        invoice_data       = invoice_data,
        po_data            = po_data,
        grn_data           = grn_data,
        matching           = matching,
        final_status       = final_status,
        summary            = summary,
        decision_info      = decision_info,
        reviewer_info      = reviewer_info,
        recipient          = RECIPIENT,
        variance_threshold = VARIANCE_THRESHOLD,
    )

    # ── Persist full validation detail — same content as the email, but
    # kept around so the dashboard's Processed/Approved pages can show
    # the exact reasons and gate-by-gate results after the fact ────────
    from datetime import datetime as _dt
    import redis_store
    redis_store.invoice_detail_set(invoice_data.get("invoice_number", ""), {
        "invoice_data":  invoice_data,
        "po_data":       po_data,
        "grn_data":      grn_data,
        "matching":      matching,
        "decision_info": decision_info,
        "final_status":  final_status,
        "reviewer_info": reviewer_info or {},
        "summary":       summary,
        "variance_threshold": VARIANCE_THRESHOLD,
        "recorded_at":   _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

    # ── Register in duplicate-check log ────────────────────────────────
    if not dup_check.get("is_duplicate"):
        from duplicate_checker import register_invoice
        register_invoice(
            invoice_number = invoice_data.get("invoice_number", ""),
            vendor_name    = invoice_data.get("vendor_name", ""),
            po_number      = po_data.get("po_number", ""),
            amount         = invoice_data.get("total_amount", 0),
            currency       = invoice_data.get("currency", "USD"),
            invoice_date   = invoice_data.get("invoice_date", ""),
            status         = final_status,
            sap_posting    = decision_info.get("sap_posting"),
        )

    # ── Move source email ───────────────────────────────────────────────
    if email_ref:
        from mail_provider import mark_processed, mark_exception
        if final_status in {"Perfect Match", "Approved by Reviewer"}:
            mark_processed(email_ref)
        else:
            mark_exception(email_ref)

    # ── Return result ────────────────────────────────────────────────────
    result = {
        "invoice_number":  invoice_data.get("invoice_number"),
        "vendor_name":     invoice_data.get("vendor_name"),
        "po_number":       po_data.get("po_number"),
        "grn_number":      grn_data.get("grn_number"),
        "invoice_amount":  invoice_data.get("total_amount"),
        "po_amount":       po_data.get("po_amount"),
        "variance":        matching.get("variance"),
        "variance_pct":    matching.get("variance_percentage"),
        "header_match":    matching.get("header_match"),
        "line_match":      matching.get("line_match"),
        "grn_match":       matching.get("grn_match"),
        "final_status":    final_status,
        "reasons":         (
            # If reviewer rejected, prepend their notes as the primary reason
            ([f"Rejected by Reviewer — {reviewer_info.get('notes','')}"]
             if (reviewer_info and final_status == "Rejected by Reviewer"
                 and reviewer_info.get("notes"))
             else [])
            + decision_info.get("reasons", [])
        ),
        "reviewer":        reviewer_info.get("reviewer") if reviewer_info else None,
        "sap_posting":     decision_info.get("sap_posting"),
        "summary":         summary,
        "supporting_docs": [d["file_name"] for d in (supporting_docs or [])],
    }

    print("\n" + "="*60)
    print("  RESULT")
    print("="*60)
    print(json.dumps({k: v for k, v in result.items() if k != "summary"}, indent=2))
    print("="*60)
    return summary, result


def resume_after_review(token: str, decision: str, reviewer: str,
                         comments: str = "") -> dict:
    """
    Resume a pipeline that was suspended awaiting a web-portal decision.

    Called by approval_portal.py when a reviewer clicks Approve / Reject /
    Request Clarification (optionally with their name + comments). Loads
    the pending state persisted by create_pending_review(), records the
    decision via hitl_review.record_web_decision(), runs the same
    finishing logic (_finalize_decision) that the synchronous paths use,
    and removes the pending record so the link can't be reused.

    decision must be one of: "Approve", "Reject", "Request Clarification"
    — validated by the caller (approval_portal.py) before this is called.

    Returns the same result dict shape as process_invoice_set(), or
    {"error": "..."} if the token is invalid/already resolved.
    """
    from pending_reviews import get_pending_review, mark_resolved_and_remove
    from hitl_review     import record_web_decision

    record = get_pending_review(token)
    if not record or record.get("status") != "pending":
        return {"error": "This review link is invalid or has already been used."}

    invoice_data    = record["invoice_data"]
    po_data         = record["po_data"]
    grn_data        = record["grn_data"]
    matching        = record["matching"]
    decision_info   = record["decision_info"]
    supporting_docs = record.get("supporting_docs", [])
    email_entry_id  = record.get("email_entry_id")

    review        = record_web_decision(decision, reviewer, comments)
    final_status  = review["final_status"]
    reviewer_info = review

    # Duplicate-check info isn't persisted separately — re-derive the
    # minimal shape _finalize_decision needs from decision_info's reasons
    # (a duplicate would already be reflected there; this only affects
    # whether we re-register in the dup log, which is harmless either way).
    dup_check = record.get("dup_check") or {"is_duplicate": False}
    _sap_apply_review(invoice_data, po_data, decision_info, final_status,
                      dup_check.get("is_duplicate", False))

    # Reconstruct a minimal email_ref so _finalize_decision can still
    # move the source email, if Graph is reachable at resume time.
    email_ref = {"id": email_entry_id} if email_entry_id else None
    if email_ref:
        from mail_provider import get_email_by_id
        live_email = get_email_by_id(email_entry_id)
        email_ref = live_email if live_email else None

    summary, result = _finalize_decision(
        invoice_data, po_data, grn_data, matching, decision_info,
        final_status, reviewer_info, dup_check, email_ref, supporting_docs,
    )

    mark_resolved_and_remove(token)
    return result


# ── Per-email orchestrator ────────────────────────────────────────────────────

def _finalize_exception(email: dict, subject: str, final_status: str,
                         reasons: list, invoice_data: dict = None,
                         po_number: str = "") -> dict:
    """
    Shared finishing logic for every "couldn't process this" exit path
    in _process_one_email_obj (bad attachment, extraction failure, PO
    not found in ERP, unexpected pipeline error, ...).

    Every one of these used to just call mark_exception(email) and
    return a result dict — which moved the email in the mailbox but
    never notified anyone WHY, and never left any record an operator
    could look up later. This fixes both: it sends the same kind of
    result email a successfully-processed invoice gets (routed to the
    "couldn't complete" template), and persists a record to the
    Exceptions log in Redis so it shows up in the dashboard's
    Exceptions section with full detail.
    """
    invoice_data = invoice_data or {}
    inv_num = invoice_data.get("invoice_number") or "unknown"

    from summary_sender import send_result_email

    print(f"  📧 Sending exception notification for: {inv_num}")
    try:
        send_result_email(
            invoice_data       = invoice_data,
            po_data            = {},
            grn_data           = {},
            matching           = {},
            final_status       = final_status,
            summary            = reasons[0] if reasons else "This submission could not be processed.",
            decision_info      = {"reasons": reasons},
            reviewer_info      = None,
            recipient          = RECIPIENT,
            variance_threshold = VARIANCE_THRESHOLD,
        )
    except Exception as e:
        print(f"  ⚠️  Could not send exception notification email: {e}")

    # Unique key even when no invoice number could be extracted at all —
    # falls back to a timestamp + subject slug so nothing overwrites
    # another record.
    key = inv_num if inv_num != "unknown" else (
        "EXC-" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:20]
    )

    _persist_exception_record(invoice_data, final_status, reasons, po_number=po_number,
                              subject=subject, key=key)

    return {
        "invoice_number": inv_num,
        "vendor_name":    invoice_data.get("vendor_name", ""),
        "po_number":      po_number,
        "final_status":   final_status,
        "reasons":        reasons,
        "subject":        subject,
        "detail_key":     key,
    }


def _process_one_email_obj(email: dict, index: int, total: int,
                            hitl_mode: bool) -> dict:
    """
    Process a single email dict (already fetched via the configured mail provider).
    Returns a result dict with at minimum keys: invoice_number, final_status.
    Never raises — all exceptions are caught and the email is moved to Exceptions.
    """
    from mail_provider       import download_attachments, mark_exception
    from document_extractor import extract_all_documents

    subject  = email["subject"]
    sender   = f"{email['sender_name']} <{email['sender_email']}>"
    received = email["received_date"]
    n_att    = email["attachment_count"]

    print(f"\n{'═'*65}")
    print(f"  Email {index}/{total}")
    print(f"{'═'*65}")
    print(f"  Subject    : {subject}")
    print(f"  From       : {sender}")
    print(f"  Received   : {received}")
    print(f"  Attachments: {n_att}")

    # Use a per-email slot so attachment files don't collide
    ts_slot = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:20]
    safe_subj = re.sub(r"[^\w-]", "_", subject[:30])
    slot = f"{ts_slot}_{safe_subj}"

    try:
        attachments = download_attachments(email, slot=slot)
    except Exception as e:
        print(f"  ❌ Attachment error: {e}")
        mark_exception(email)
        return _finalize_exception(email, subject, "Error — Attachment failure",
                                    [f"Could not download attachments: {e}"])

    if not attachments:
        print("  ❌ No supported attachments — moving to Exceptions")
        mark_exception(email)
        return _finalize_exception(email, subject, "Incomplete — No Attachments",
                                    ["No supported attachments found on this email. "
                                     "Please resend with the Invoice PDF attached."])

    types_found = {a["doc_type"] for a in attachments}
    print(f"\n  Document types found: {types_found}")

    if "Invoice" not in types_found:
        print("  ❌ No Invoice attachment found — moving to Exceptions")
        mark_exception(email)
        return _finalize_exception(email, subject, "Incomplete — Missing Documents",
                                    ["No Invoice attachment found. Please resend the "
                                     "email with the Invoice PDF attached."])

    try:
        extracted       = extract_all_documents(attachments)
        invoice_data     = extracted.get("invoice_data", {})
        supporting_docs  = extracted.get("supporting_docs", [])
    except Exception as e:
        print(f"  ❌ Extraction error: {e}")
        traceback.print_exc()
        mark_exception(email)
        return _finalize_exception(email, subject, "Error — Extraction failure",
                                    [f"Invoice could not be read/extracted: {e}"])

    if not invoice_data or not invoice_data.get("invoice_number"):
        print("  ❌ Could not extract Invoice fields — moving to Exceptions")
        mark_exception(email)
        return _finalize_exception(email, subject, "Incomplete — Missing Documents",
                                    ["Invoice attachment could not be read/parsed. "
                                     "Please check the file and resend."],
                                    invoice_data=invoice_data)

    # ── ERP lookup — PO and GRN no longer come from email attachments ──
    from erp_mock import lookup_po_and_grn
    po_number = invoice_data.get("po_number", "")
    po_data, grn_data = lookup_po_and_grn(po_number)

    print(f"\n  Extraction:")
    print(f"    Invoice : {invoice_data.get('invoice_number','NOT FOUND')}")
    print(f"    PO      : {po_data.get('po_number','NOT FOUND IN ERP')}")
    print(f"    GRN     : {grn_data.get('grn_number','NOT FOUND IN ERP')}")
    if supporting_docs:
        print(f"    Supporting docs: {[d['file_name'] for d in supporting_docs]}")

    if not po_data:
        print(f"  ❌ PO '{po_number}' not found in ERP — moving to Exceptions")
        mark_exception(email)
        return _finalize_exception(
            email, subject, "Incomplete — PO Not Found",
            [f"PO Number '{po_number}' not found in ERP system. "
             "Cannot proceed with 3-way match. Please verify the "
             "PO number on the invoice or confirm the PO has been "
             "created in the ERP."],
            invoice_data=invoice_data, po_number=po_number,
        )

    try:
        result = process_invoice_set(
            invoice_data     = invoice_data,
            po_data          = po_data,
            grn_data         = grn_data,
            email_ref        = email,
            hitl_mode        = hitl_mode,
            supporting_docs  = supporting_docs,
        )
        result["subject"] = subject
        return result
    except Exception as e:
        print(f"\n  ❌ Pipeline error: {e}")
        traceback.print_exc()
        mark_exception(email)
        return _finalize_exception(
            email, subject, "Error — Pipeline failure",
            [f"Unexpected error while processing this invoice: {e}"],
            invoice_data=invoice_data, po_number=po_number,
        )


# ── Batch processing — the new core ──────────────────────────────────────────

def process_all_emails(hitl_mode: bool = True) -> list:
    """
    Fetch ALL emails from 'ToBeProcessed' and process them one by one.

    The folder is snapshotted before any processing starts so that
    Move() calls during processing don't corrupt the iteration.

    Returns a list of result dicts (one per email).
    """
    from mail_provider import get_all_emails

    emails = get_all_emails()
    if not emails:
        print(f"\n  ✅ Nothing to do — '{SOURCE_FOLDER}' is empty.")
        return []

    total   = len(emails)
    results = []

    print(f"\n  📋 Processing {total} email(s) from '{SOURCE_FOLDER}'")
    print(f"  {'─'*60}")

    for idx, email in enumerate(emails, 1):
        result = _process_one_email_obj(email, idx, total, hitl_mode)
        results.append(result)

    _print_batch_summary(results)
    _send_batch_summary_email(results)
    return results


def _print_batch_summary(results: list) -> None:
    """Print a concise table of all processed emails to stdout."""
    if not results:
        return

    approved = [r for r in results if r.get("final_status","") in
                {"Perfect Match", "Approved by Reviewer"}]
    review   = [r for r in results if "Review" in r.get("final_status","") or
                "Clarification" in r.get("final_status","") or
                "Rejected" in r.get("final_status","")]
    errors   = [r for r in results if "Error" in r.get("final_status","") or
                "Incomplete" in r.get("final_status","") or
                "Missing" in r.get("final_status","")]

    print("\n\n" + "█"*65)
    print("  BATCH PROCESSING COMPLETE")
    print("█"*65)
    print(f"\n  Total processed : {len(results)}")
    print(f"  ✅ Perfect Match : {len(approved)}")
    print(f"  ⚠️  Manual Review : {len(review)}")
    print(f"  ❌ Errors/Missing: {len(errors)}")
    print(f"\n  {'Invoice':<18} {'Vendor':<22} {'Status':<30} {'Folder'}")
    print(f"  {'─'*18} {'─'*22} {'─'*30} {'─'*12}")

    for r in results:
        inv    = str(r.get("invoice_number") or r.get("subject","?"))[:17]
        vendor = str(r.get("vendor_name") or "")[:21]
        status = str(r.get("final_status","?"))[:29]
        folder = ("Processed"  if r.get("final_status") in {"Perfect Match","Approved by Reviewer"}
                  else "Exceptions")
        icon   = ("✅" if folder == "Processed" else "⚠️ " if "Review" in status else "❌")
        print(f"  {inv:<18} {vendor:<22} {icon} {status:<28} → {folder}")

    print("█"*65)


def _send_batch_summary_email(results: list) -> None:
    """
    Send a single batch-summary email after all invoices are processed.
    Only sent when more than one email was processed (no point for a single run).
    """
    if len(results) <= 1:
        return   # single-invoice runs already have a full detail email

    from summary_sender import _BASE_CSS

    approved = [r for r in results if r.get("final_status","") in
                {"Perfect Match","Approved by Reviewer"}]
    review   = [r for r in results if "Review" in r.get("final_status","") or
                "Rejected" in r.get("final_status","") or
                "Clarification" in r.get("final_status","")]
    errors   = [r for r in results if "Error" in r.get("final_status","") or
                "Incomplete" in r.get("final_status","") or
                "Missing" in r.get("final_status","")]

    def _pill(text, color, bg):
        return (f"<span style='background:{bg};color:{color};font-size:11px;"
                f"font-weight:700;padding:3px 10px;border-radius:20px'>{text}</span>")

    rows = ""
    for r in results:
        status = str(r.get("final_status","?"))
        if status in {"Perfect Match","Approved by Reviewer"}:
            pill = _pill(status, "#1e8449", "#d5f5e3")
            folder = "Processed"
        elif "Error" in status or "Missing" in status or "Incomplete" in status:
            pill   = _pill(status, "#922b21", "#fdedec")
            folder = "Exceptions"
        else:
            pill   = _pill(status, "#784212", "#fef9e7")
            folder = "Exceptions"

        inv    = r.get("invoice_number") or r.get("subject","—")
        vendor = r.get("vendor_name","—")
        amt    = r.get("invoice_amount")
        amt_s  = f"${float(amt):,.2f}" if amt else "—"
        reasons_s = "; ".join(r.get("reasons",[]))[:80] or "—"

        rows += (
            f"<tr>"
            f"<td style='padding:9px 12px;font-weight:600'>{inv}</td>"
            f"<td style='padding:9px 12px'>{vendor}</td>"
            f"<td style='padding:9px 12px;text-align:right'>{amt_s}</td>"
            f"<td style='padding:9px 12px'>{pill}</td>"
            f"<td style='padding:9px 12px;font-size:12px;color:#888'>{reasons_s}</td>"
            f"<td style='padding:9px 12px;color:#555'>{folder}</td>"
            f"</tr>"
        )

    body = (
        f"<style>{_BASE_CSS}</style>"
        "<div class='wrap'>"
        "<div class='topbar' style='background:#2d4a7a'></div>"
        "<div class='hdr' style='background:#2d4a7a'>"
        "<h1>📊 AP Batch Processing Summary</h1>"
        f"<p>{len(results)} invoice(s) processed · "
        f"{datetime.now().strftime('%d %b %Y %H:%M')}</p>"
        "</div>"
        "<div class='body'>"

        # Stat tiles
        "<div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:24px'>"
        + f"<div style='text-align:center;background:#f0fdf4;border-radius:8px;padding:16px'>"
          f"<p style='margin:0 0 4px;font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px'>Perfect Match</p>"
          f"<p style='font-size:32px;font-weight:700;color:#1e8449;margin:0'>{len(approved)}</p></div>"
        + f"<div style='text-align:center;background:#fef9e7;border-radius:8px;padding:16px'>"
          f"<p style='margin:0 0 4px;font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px'>Manual Review</p>"
          f"<p style='font-size:32px;font-weight:700;color:#b7950b;margin:0'>{len(review)}</p></div>"
        + f"<div style='text-align:center;background:#fdedec;border-radius:8px;padding:16px'>"
          f"<p style='margin:0 0 4px;font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px'>Errors / Incomplete</p>"
          f"<p style='font-size:32px;font-weight:700;color:#922b21;margin:0'>{len(errors)}</p></div>"
        + "</div>"

        # Detail table
        + "<div class='section'>"
          "<p class='section-title'>Invoice Detail</p>"
          "<table style='width:100%;border-collapse:collapse;font-size:13px'>"
          "<thead><tr style='background:#f8f9fa'>"
          "<th style='padding:9px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#555;border-bottom:2px solid #e0e0e0'>Invoice #</th>"
          "<th style='padding:9px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#555;border-bottom:2px solid #e0e0e0'>Vendor</th>"
          "<th style='padding:9px 12px;text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#555;border-bottom:2px solid #e0e0e0'>Amount</th>"
          "<th style='padding:9px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#555;border-bottom:2px solid #e0e0e0'>Status</th>"
          "<th style='padding:9px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#555;border-bottom:2px solid #e0e0e0'>Reason</th>"
          "<th style='padding:9px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#555;border-bottom:2px solid #e0e0e0'>Folder</th>"
          "</tr></thead>"
          f"<tbody>{rows}</tbody>"
          "</table></div>"

        + "<p style='font-size:12px;color:#aaa;margin-top:16px;text-align:center'>"
          "Each invoice has a full detail email. This is the batch summary only.</p>"

        + f"<div class='footer'>"
          f"AP Automation Agent &nbsp;·&nbsp; {datetime.now().strftime('%d %b %Y %H:%M')}"
          f" &nbsp;·&nbsp; Source: '{SOURCE_FOLDER}' folder"
          f"</div>"
        + "</div></div>"
    )

    subject = (f"📊 AP Batch Summary — {len(results)} processed "
               f"({len(approved)} approved, {len(review)} review, {len(errors)} errors)"
               f" — {datetime.now().strftime('%d %b %Y %H:%M')}")

    print(f"\n  Sending batch summary to: {RECIPIENT}")
    try:
        from mail_provider import send_html_email, provider_label
        send_html_email(
            subject   = subject,
            html_body = f"<html><head></head><body>{body}</body></html>",
            to        = RECIPIENT,
        )
        print(f"  ✅ Batch summary sent via {provider_label()}")
    except Exception as e:
        print(f"  ⚠️  Batch summary email send failed: {e}")
        try:
            out_dir = Path("/tmp/data/notifications") if os.getenv("VERCEL") else Path("data/notifications")
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            p  = out_dir / f"batch_summary_{ts}.html"
            p.write_text(
                f"<!-- TO: {RECIPIENT} -->\n<!-- SUBJECT: {subject} -->\n"
                f"<html><head></head><body>{body}</body></html>",
                encoding="utf-8"
            )
            print(f"  ℹ️  Saved a local copy instead: {p}")
        except OSError:
            print("  ⚠️  Could not save a local fallback copy either (read-only filesystem) — summary was only logged above.")
        print(f"  ⚠️  Mail send unavailable ({e}) — saved: {p.name}")


# ── Mock mode ─────────────────────────────────────────────────────────────────

def run_mock(variance: bool = False, hitl_mode: bool = True) -> list:
    """
    Run with built-in mock data — no mailbox needed.

    v3.0: exercises the real Invoice + ERP-mock flow (erp_mock.py +
    data/erp/ERP_Mock_Data.xlsx) across all 11 scenarios bundled with
    the agent, instead of the old hardcoded 2-invoice MOCK_SETS dict.
    Each "invoice" here is exactly what the LLM would have extracted
    from the corresponding Invoice PDF in test_scenarios_v3/ — PO and
    GRN are looked up live from the ERP workbook, just like production.
    """
    print("\n" + "="*65)
    print("  MOCK MODE — 11 built-in scenarios via ERP lookup (no mailbox needed)")
    print("="*65)

    from erp_mock import lookup_po_and_grn

    # Mirrors test_scenarios_v3/ — kept here so --mock works standalone
    # without needing the PDF files on disk.
    mock_invoices = {
        "S01": {"invoice_number": "INV-S01-001", "vendor_name": "ABC Industries",
                "invoice_date": "2026-06-19", "po_number": "PO-S01-001",
                "currency": "USD", "total_amount": 15825.00,
                "items": [{"item": "Dell Laptop XPS-15", "quantity": 10, "unit_price": 1200.00},
                          {"item": "Samsung Monitor 27-inch", "quantity": 5, "unit_price": 450.00}]},
        "S03": {"invoice_number": "INV-S03-001", "vendor_name": "Global Supplies Co",
                "invoice_date": "2026-06-17", "po_number": "PO-S03-001",
                "currency": "USD", "total_amount": 8773.20,
                "items": [{"item": "Office Chair Ergonomic", "quantity": 20, "unit_price": 163.00},
                          {"item": "Executive Desk 180cm", "quantity": 10, "unit_price": 435.00}]},
    }

    sets_to_run = (["S01", "S03"] if not variance else ["S03"])

    results = []
    total   = len(sets_to_run)
    for idx, sid in enumerate(sets_to_run, 1):
        invoice_data = mock_invoices[sid]
        print(f"\n{'═'*65}")
        print(f"  Mock invoice {idx}/{total}: {invoice_data['invoice_number']} "
              f"(scenario {sid})")
        print(f"{'═'*65}")

        po_data, grn_data = lookup_po_and_grn(invoice_data["po_number"])

        result = process_invoice_set(
            invoice_data = invoice_data,
            po_data      = po_data,
            grn_data     = grn_data,
            email_ref    = None,
            hitl_mode    = hitl_mode,
        )
        result["subject"] = invoice_data["invoice_number"]
        results.append(result)

    _print_batch_summary(results)
    _send_batch_summary_email(results)
    return results


# ── Test connection ────────────────────────────────────────────────────────────

def test_connection() -> None:
    """Test the configured mail provider connection and list emails waiting in ToBeProcessed."""
    from mail_provider import get_all_emails
    from mail_provider import provider_label
    print(f"\nTesting {provider_label()} connection...")
    print(f"  Source folder : '{SOURCE_FOLDER}'")
    try:
        emails = get_all_emails()
        print(f"\n  ✅ Connected — {len(emails)} email(s) in '{SOURCE_FOLDER}'")
        if emails:
            print(f"\n  Waiting to be processed:")
            for e in emails:
                att = e["attachment_count"]
                print(f"    [{e['received_date'][:10]}] {e['subject'][:55]}  "
                      f"({att} attachment{'s' if att != 1 else ''})")
        else:
            print(f"\n  Tip: Place invoice emails in '{SOURCE_FOLDER}' folder in the mailbox.")
        print(f"\n{provider_label()} integration is working.")
    except Exception as ex:
        print(f"  ❌ {ex}")
        print("  Make sure the mail provider credentials in .env are correct and (for Graph) admin-consented / (for Gmail) domain-delegated or OAuth-authorized.")


# ── CLI ────────────────────────────────────────────────────────────────────────

def _check_portal_reachable(portal_url: str) -> None:
    """
    Live connectivity check against the approval portal's /health
    endpoint, run once at startup when --hitl-mode web is selected.

    This exists because a misconfigured or not-yet-started portal
    otherwise fails completely silently: review emails still send fine
    (the link is just text), and nothing breaks until a reviewer
    actually clicks — at which point it just looks like "the link
    doesn't work" with no indication of why. This check surfaces that
    immediately, before any invoices are processed.
    """
    import urllib.request
    import urllib.error

    health_url = portal_url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(health_url, timeout=3) as resp:
            if resp.status == 200:
                print(f"  ✅ Approval portal is reachable at {portal_url}")
            else:
                print(f"  ⚠️  Approval portal responded with status "
                      f"{resp.status} at {health_url} — check it's running "
                      f"correctly.")
    except urllib.error.URLError as e:
        print(f"  ❌ Cannot reach approval portal at {health_url}: {e.reason}")
        print(f"     Review-request emails will still be sent, but the "
              f"links inside them will NOT work until the portal is "
              f"running. Start it with: python approval_portal.py")
    except Exception as e:
        print(f"  ⚠️  Could not verify approval portal reachability: {e}")


def parse_args():
    p = argparse.ArgumentParser(
        description="AP Automation Agent v3.0 — processes ALL emails in ToBeProcessed"
    )
    p.add_argument("--watch",    action="store_true",
                   help="Continuous mode — poll every POLL_INTERVAL_SECONDS")
    p.add_argument("--test",     action="store_true",
                   help="Test the configured mail provider connection and list waiting emails")
    p.add_argument("--mock",     action="store_true",
                   help="Run with built-in mock invoices (no mailbox needed)")
    p.add_argument("--variance", action="store_true",
                   help="(mock) Use price-variance invoice set")
    p.add_argument("--no-hitl",  action="store_true",
                   help="[legacy alias for --hitl-mode email_only] Skip interactive "
                        "HITL — send review email only")
    p.add_argument("--hitl-mode", choices=["interactive", "email_only", "web"],
                   default=None,
                   help="How Manual Review is resolved: 'interactive' prompts in "
                        "this terminal, 'email_only' just sends the review email, "
                        "'web' sends clickable Approve/Reject/Clarify links and "
                        "requires approval_portal.py to be running. "
                        "Overrides --no-hitl if both are given.")
    return p.parse_args()


def main():
    args = parse_args()

    if args.hitl_mode:
        hitl = args.hitl_mode
    else:
        hitl = "email_only" if args.no_hitl else "interactive"

    hitl_label = {
        "interactive": "ON (interactive CLI)",
        "email_only":  "OFF (email only)",
        "web":         "WEB PORTAL (clickable approve/reject/clarify links)",
    }.get(hitl, hitl)

    print("\n" + "="*65)
    from mail_provider import provider_label
    print(f"  AP AUTOMATION AGENT v3.2 — MAIL PROVIDER: {provider_label()}")
    print("="*65)
    print(f"  Source folder : '{SOURCE_FOLDER}'  (all emails processed)")
    print(f"  Recipient     : {RECIPIENT}")
    print(f"  Reviewer      : {REVIEWER_EMAIL}")
    print(f"  Threshold     : {VARIANCE_THRESHOLD}%")
    print(f"  HITL mode     : {hitl_label}")
    if hitl == "web":
        portal_url = os.getenv("AP_APPROVAL_PORTAL_URL", "http://localhost:8000")
        print(f"  Portal URL    : {portal_url}")
        if "localhost" in portal_url or "127.0.0.1" in portal_url:
            print(f"  ⚠️  Using 'localhost' — review links will only work "
                  f"if the reviewer opens the email on THIS machine. Set "
                  f"AP_APPROVAL_PORTAL_URL in .env to this machine's real "
                  f"network address if reviewers use a different PC.")
        _check_portal_reachable(portal_url)
    print("="*65)

    if args.test:
        test_connection()

    elif args.mock:
        run_mock(variance=args.variance, hitl_mode=hitl)

    elif args.watch:
        print(f"\n  Watch mode — polling every {POLL_INTERVAL}s")
        print(f"  Folder '{SOURCE_FOLDER}' is drained on each cycle")
        print("  Press Ctrl+C to stop\n")
        while True:
            try:
                process_all_emails(hitl_mode=hitl)
            except KeyboardInterrupt:
                print("\n  Stopped.")
                break
            except Exception as e:
                print(f"  Cycle error: {e}")

                traceback.print_exc()
            print(f"\n  Sleeping {POLL_INTERVAL}s …\n")
            time.sleep(POLL_INTERVAL)

    else:
        # Default: drain the folder once
        process_all_emails(hitl_mode=hitl)


if __name__ == "__main__":
    main()



# ── SAP reflection helpers ────────────────────────────────────────────────────

def _persist_exception_record(invoice_data: dict, final_status: str, reasons: list,
                              po_number: str = "", subject: str = "", key: str = None,
                              po_data: dict = None, grn_data: dict = None,
                              summary: str = None) -> None:
    """Exceptions-page record + invoice detail for a submission that could
    not complete (shared by _finalize_exception and the missing-PO/GRN
    path of process_invoice_set)."""
    import redis_store
    inv_num = invoice_data.get("invoice_number") or "unknown"
    key = key or (inv_num if inv_num != "unknown"
                  else "EXC-" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:20])
    recorded = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        redis_store.exception_add(key, {
            "key":            key,
            "invoice_number": inv_num,
            "vendor_name":    invoice_data.get("vendor_name", ""),
            "po_number":      po_number,
            "subject":        subject,
            "final_status":   final_status,
            "reasons":        reasons,
            "recorded_at":    recorded,
        })
        # Also store under invoice_detail so the same rich detail page
        # (/dashboard/invoice/{key}) works for exceptions too.
        redis_store.invoice_detail_set(key, {
            "invoice_data":  invoice_data,
            "po_data":       po_data or {},
            "grn_data":      grn_data or {},
            "matching":      {},
            "decision_info": {"reasons": reasons},
            "final_status":  final_status,
            "reviewer_info": {},
            "summary":       summary or (reasons[0] if reasons else
                                         "This submission could not be processed."),
            "variance_threshold": VARIANCE_THRESHOLD,
            "recorded_at":   recorded,
        })
    except Exception as e:
        print(f"  ⚠️  Could not persist exception record to Redis: {e}")


def _sap_sync_decision(invoice_data: dict, po_data: dict, decision_info: dict,
                       dup_check: dict) -> None:
    """
    Make the agent's decision visible in SAP (no-op unless
    AP_ERP_SOURCE=sap and SAP_INVOICE_MODE is post/simulate):

      Perfect Match -> post the supplier invoice. If SAP itself refuses
                       it, the invoice is routed to Manual Review with
                       SAP's message and parked instead.
      Manual Review -> park the invoice in SAP so AP sees it in MIR4.
      Duplicate     -> nothing is created in SAP.

    The outcome is stored as decision_info["sap_posting"] so it travels
    with the pending review, the result email and the dashboard detail.
    """
    import sap_invoice
    if not sap_invoice.enabled() or not po_data.get("po_number"):
        return
    po_number = po_data["po_number"]
    print("\n" + "=" * 55)
    print(f"  SAP INVOICE ({sap_invoice.invoice_mode().upper()} mode)")
    print("=" * 55)

    if dup_check.get("is_duplicate"):
        decision_info["sap_posting"] = sap_invoice._result(
            "Skipped", messages=["Duplicate invoice — nothing created in SAP"])
        print("  ⏭  Duplicate — not created in SAP")
        return

    if decision_info.get("decision") == "Perfect Match":
        res = sap_invoice.create_invoice(invoice_data, po_number, park=False,
                                         header_text="AP Agent auto-post")
        if res["action"] in ("Posted", "Simulated"):
            decision_info["sap_posting"] = res
            print(f"  ✅ SAP: {res['action']} {res.get('document', '')} "
                  f"{('payment block ' + res['payment_block']) if res.get('payment_block') else ''}")
            return
        print(f"  ❌ SAP refused the posting: {res['error']}")
        decision_info["decision"] = "Manual Review"
        decision_info["review_required"] = True
        decision_info.setdefault("reasons", []).append(f"SAP Posting Failed — {res['error']}")

    if decision_info.get("review_required"):
        res = sap_invoice.create_invoice(invoice_data, po_number, park=True,
                                         header_text="AP Agent review")
        decision_info["sap_posting"] = res
        print(f"  {'🅿️ ' if res['action'] == 'Parked' else '⚠️ '} SAP: {res['action']} "
              f"{res.get('document', '')} {res.get('error', '')}")


def _sap_apply_review(invoice_data: dict, po_data: dict, decision_info: dict,
                      final_status: str, is_duplicate: bool) -> None:
    """Carry a reviewer's decision into SAP: Approve posts the parked
    invoice (or posts fresh if nothing was parked), Reject deletes the
    parked invoice, Clarification leaves it parked."""
    import sap_invoice
    if not sap_invoice.enabled():
        return
    current = decision_info.get("sap_posting") or {}
    parked = current.get("action") == "Parked" and current.get("document")

    if final_status == "Approved by Reviewer":
        if is_duplicate:
            res = sap_invoice._result("Skipped", messages=[
                "Approved, but this is a duplicate of an invoice already processed — "
                "not posted to SAP again"])
        elif parked:
            res = sap_invoice.post_parked(current["document"], current["fiscal_year"])
        else:
            res = sap_invoice.create_invoice(invoice_data, po_data.get("po_number", ""),
                                             park=False, header_text="AP Agent approved")
    elif final_status == "Rejected by Reviewer" and parked:
        res = sap_invoice.delete_parked(current["document"], current["fiscal_year"])
    else:
        return

    if current:
        decision_info.setdefault("sap_history", []).append(current)
    decision_info["sap_posting"] = res
    print(f"  🏦 SAP after review: {res['action']} {res.get('document', '')} "
          f"{res.get('error', '')}")


def reset_invoice(invoice_number: str) -> dict:
    """
    Demo reset for one invoice number: undo what the agent created in SAP
    (reverse posted / delete parked supplier invoices carrying this
    reference) and remove every AP-side trace — processed log, detail
    record, exception, pending review — so it can be run again.
    """
    import duplicate_checker
    import redis_store
    import sap_invoice

    out = {"invoice_number": invoice_number, "sap": [], "removed": []}
    if sap_invoice.enabled():
        out["sap"] = sap_invoice.undo_invoice(invoice_number)
    if redis_store.processed_get(invoice_number):
        duplicate_checker.remove_invoice(invoice_number)
        out["removed"].append("processed log")
    if redis_store.invoice_detail_get(invoice_number):
        redis_store.invoice_detail_delete(invoice_number)
        out["removed"].append("detail")
    for key in redis_store.exception_keys_for_invoice(invoice_number):
        redis_store.exception_remove(key)
        redis_store.invoice_detail_delete(key)
        out["removed"].append("exception")
    for token in redis_store.pending_tokens_for_invoice(invoice_number):
        redis_store.pending_delete(token)
        out["removed"].append("pending review")
    return out
