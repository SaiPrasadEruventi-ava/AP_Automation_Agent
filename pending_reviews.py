"""
pending_reviews.py — Pending HITL Review Store (Redis-backed)
==============================================================
When an invoice needs Manual Review AND the web-portal HITL mode is
active, the pipeline can't block waiting for an HTTP click the way it
blocks on input() in CLI mode. Instead it:

  1. Generates a unique approval token
  2. Persists everything needed to RESUME the pipeline later
     (invoice/po/grn data, matching, decision_info, email_ref info)
     to Redis under that token
  3. Sends the reviewer an email with clickable links containing the
     token: {AP_APPROVAL_PORTAL_URL}/review/{token}

When the reviewer clicks Approve/Reject/Clarify (with their name and
comments) on the web portal, approval_portal.py loads the pending
record back via this module, and run_ap.py's resume_after_review()
finishes the pipeline (summary, result email, log registration,
mailbox move) exactly as if process_hitl_review() had returned that
decision synchronously.

Records are removed once resolved — this store only ever holds
invoices that are CURRENTLY awaiting a decision. Originally this wrote
one JSON file per token to data/pending_reviews/; Vercel's filesystem
is read-only/ephemeral, so this now persists via Redis
(redis_store.py) instead. Public function signatures are unchanged.
"""

import secrets
from datetime import datetime

import redis_store


def generate_token() -> str:
    """URL-safe, unguessable token identifying one pending review."""
    return secrets.token_urlsafe(24)


def create_pending_review(invoice_data: dict, po_data: dict, grn_data: dict,
                           matching: dict, decision_info: dict,
                           supporting_docs: list = None,
                           email_entry_id: str = None) -> str:
    """
    Persist a pending review and return its token.

    email_entry_id: the Outlook EntryID string (NOT the live COM object —
    that can't be serialized/survive a process restart) so the resume
    step can still move the source email to Processed/Exceptions later
    if Outlook is available at resume time.
    """
    token = generate_token()

    record = {
        "token":           token,
        "created_at":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status":          "pending",   # pending | resolved
        "invoice_data":    invoice_data,
        "po_data":         po_data,
        "grn_data":        grn_data,
        "matching":        matching,
        "decision_info":   decision_info,
        "supporting_docs": supporting_docs or [],
        "email_entry_id":  email_entry_id,
    }

    redis_store.pending_save(token, record)
    print(f"  📝 Pending review created: token={token[:12]}... "
          f"(invoice {invoice_data.get('invoice_number')})")
    return token


def get_pending_review(token: str) -> dict:
    """
    Load a pending review by token. Returns {} if not found
    (bad/expired token, or already resolved and cleaned up).
    """
    return redis_store.pending_get(token)


def is_pending(token: str) -> bool:
    record = get_pending_review(token)
    return bool(record) and record.get("status") == "pending"


def mark_resolved_and_remove(token: str) -> None:
    """
    Delete the pending record once a decision has been processed.
    Idempotent — safe to call even if already removed (e.g. a reviewer
    double-clicking a link, or two tabs open on the same review page).
    """
    redis_store.pending_delete(token)
    print(f"  🗑️  Pending review resolved & removed: token={token[:12]}...")


def list_pending_reviews() -> list:
    """Return all currently-pending review records — useful for a dashboard."""
    records = []
    for token in redis_store.pending_list_tokens():
        record = redis_store.pending_get(token)
        if record and record.get("status") == "pending":
            records.append(record)
    records.sort(key=lambda r: r.get("created_at", ""))
    return records
