"""
gmail_reader.py — Gmail Mailbox + Attachment Reader  v1.0
=============================================================
Reads ALL emails carrying the 'ToBeProcessed' Gmail LABEL and exposes
the same public API as graph_reader.py / the old outlook_reader.py, so
run_ap.py (via mail_provider.py) can use whichever backend is
configured without any code changes.

Gmail has no folder-move concept — moving an email in Outlook/Graph
terms is done here by adding the destination label and removing the
source label (and Gmail's own UNREAD label, to mirror "mark as read").
Gmail's "Inbox" is itself just a label (INBOX), so this doesn't touch
Inbox placement at all — an email can carry ToBeProcessed AND still
show up in the Inbox, exactly like it does today. To get invoice
emails labelled 'ToBeProcessed' in the first place, create a Gmail
filter (Settings → Filters and Blocked Addresses → Create a new
filter) that applies that label automatically, or apply it manually.

Public API (identical names/shapes to graph_reader.py)
----------------------------------------------------------
    get_all_emails() -> list[dict]
    get_unread_emails(top=50) -> list[dict]   (alias, top ignored)
    download_attachments(email, slot="") -> list[dict]
    get_email_by_id(entry_id) -> dict
    mark_processed(email) -> None
    mark_exception(email) -> None

Each email dict has: id, subject, sender_email, sender_name,
received_date, body_text, attachment_count, _msg (raw Gmail message
JSON), _namespace (always None — kept only for shape-compatibility
with the other backends).

v3.0 attachment model (same as the other backends)
------------------------------------------------------
The user emails an Invoice PDF (required) plus an optional Supporting
document. PO and GRN are looked up from the mock ERP workbook via the
PO Number on the invoice, never expected as attachments.
"""

import base64
import binascii
import os
from email.utils import parsedate_to_datetime, parseaddr
from pathlib import Path
from datetime import datetime

from gmail_auth import get_gmail_service
from gmail_client import execute_with_retry, GmailApiError

# On Vercel the project directory is read-only — only /tmp is writable
# (and only for the lifetime of that single invocation, wiped on the
# next cold start). VERCEL=1 is set automatically in that environment.
# Locally, "data/" on disk works fine and persists between runs.
DATA_DIR = Path("/tmp/data") if os.getenv("VERCEL") else Path("data")
for d in ["invoices", "supporting", "processed", "exceptions", "notifications"]:
    try:
        (DATA_DIR / d).mkdir(parents=True, exist_ok=True)
    except OSError:
        # Read-only filesystem and not on Vercel (unexpected environment) —
        # don't crash the whole module import over scratch-directory setup.
        pass

SUPPORTED = {".pdf", ".txt", ".xlsx", ".csv", ".docx"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}

# ── Label names (configurable via env — same variable names as the ──
# ── Graph/Outlook backends, since a Gmail label plays the same role  ──
# ── as an Outlook/Graph folder here) ─────────────────────────────────
SOURCE_FOLDER     = os.getenv("AP_SOURCE_FOLDER",     "ToBeProcessed")
PROCESSED_FOLDER  = os.getenv("AP_PROCESSED_FOLDER",  "Processed")
EXCEPTIONS_FOLDER = os.getenv("AP_EXCEPTIONS_FOLDER", "Exceptions")


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _classify(filename: str) -> str:
    """Classify attachment by filename → Invoice / Supporting (same rule as the other backends)."""
    f = filename.lower()
    if any(k in f for k in ["invoice", "inv_", "inv-"]):
        return "Invoice"
    if f.startswith("invoice") or f.startswith("inv"):
        return "Invoice"
    return "Supporting"


def _dest_dir(doc_type: str, slot: str = "") -> Path:
    base = {
        "Invoice":    DATA_DIR / "invoices",
        "Supporting": DATA_DIR / "supporting",
    }.get(doc_type, DATA_DIR)
    if slot:
        base = base / slot
    base.mkdir(parents=True, exist_ok=True)
    return base


def _extract_text(file_path: str) -> str:
    """Extract plain text from PDF or text file (identical to the other backends)."""
    path = Path(file_path)
    if not path.exists():
        return ""
    try:
        if path.suffix.lower() == ".pdf":
            import pdfplumber
            with pdfplumber.open(str(path)) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            print(f"    PDF text extracted: {len(text)} chars from {path.name}")
            return text
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"    WARNING: Could not extract text from {path.name}: {e}")
        return ""


def _b64url_decode(data: str) -> bytes:
    """Gmail returns base64url without padding — pad it back out before decoding."""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _header(headers: list, name: str) -> str:
    name_lower = name.lower()
    for h in headers or []:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _walk_parts(part: dict, text_chunks: list, attachments: list) -> None:
    """
    Recursively walk a Gmail message payload's MIME tree, collecting
    plain-text body chunks and attachment descriptors.

    A "part" is an attachment if it has a filename AND either an
    attachmentId (Gmail stores it separately — the common case) or
    inline base64 data (small attachments Gmail sometimes inlines
    directly). Anything else with a text/plain mimeType contributes to
    the body text. Everything else is recursed into (multipart/*).
    """
    mime_type = part.get("mimeType", "")
    filename  = part.get("filename", "")
    body      = part.get("body", {}) or {}

    if filename and (body.get("attachmentId") or body.get("data")):
        attachments.append({
            "filename":     filename,
            "mimeType":     mime_type,
            "attachmentId": body.get("attachmentId"),
            "inline_data":  body.get("data") if not body.get("attachmentId") else None,
        })
        return

    if mime_type == "text/plain" and body.get("data"):
        try:
            text_chunks.append(_b64url_decode(body["data"]).decode("utf-8", errors="ignore"))
        except (binascii.Error, ValueError):
            pass
        return

    for sub_part in part.get("parts", []) or []:
        _walk_parts(sub_part, text_chunks, attachments)


def _parse_message(msg: dict) -> dict:
    """Turn a raw Gmail message resource (format='full') into our common email dict shape."""
    payload = msg.get("payload", {}) or {}
    headers = payload.get("headers", [])

    subject     = _header(headers, "Subject")
    from_header = _header(headers, "From")
    sender_name, sender_email = parseaddr(from_header)

    date_header = _header(headers, "Date")
    try:
        received = parsedate_to_datetime(date_header).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        # Fall back to Gmail's internalDate (epoch millis, always present)
        try:
            received = datetime.fromtimestamp(
                int(msg.get("internalDate", 0)) / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            received = str(datetime.now())

    text_chunks, attachments = [], []
    _walk_parts(payload, text_chunks, attachments)

    return {
        "id":               msg["id"],
        "subject":          subject,
        "sender_email":     sender_email,
        "sender_name":      sender_name or sender_email,
        "received_date":    received,
        "body_text":        "\n".join(text_chunks),
        "attachment_count": len(attachments),
        "_msg":             msg,
        "_namespace":       None,
        "_internal_date":   int(msg.get("internalDate", 0)),
    }


def _get_label_map(service) -> dict:
    resp = execute_with_retry(service.users().labels().list(userId="me"))
    return {l["name"]: l["id"] for l in resp.get("labels", [])}


def _get_or_create_label(service, name: str, label_map: dict = None) -> str:
    """Find a label's id by name, creating it if it doesn't exist yet."""
    label_map = label_map if label_map is not None else _get_label_map(service)
    if name in label_map:
        return label_map[name]
    created = execute_with_retry(
        service.users().labels().create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
    )
    print(f"  📁 Created '{name}' label")
    return created["id"]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_all_emails() -> list:
    """
    Return ALL emails currently carrying the 'ToBeProcessed' label,
    oldest-first, via the Gmail API.
    """
    print(f"\n📧 Connecting to Gmail...")
    print(f"  Source label  : '{SOURCE_FOLDER}'")

    service = get_gmail_service()

    try:
        label_map = _get_label_map(service)
    except GmailApiError as e:
        print(f"  ❌ Cannot reach Gmail API: {e}")
        raise

    if SOURCE_FOLDER not in label_map:
        raise RuntimeError(
            f"Label '{SOURCE_FOLDER}' not found.\n"
            f"Please create a Gmail label named '{SOURCE_FOLDER}' (Gmail "
            f"sidebar → 'Create new label', or Settings → Labels) and apply "
            f"it to invoice emails — manually, or via a filter — before "
            f"running this agent."
        )
    source_label_id = label_map[SOURCE_FOLDER]

    stubs = []
    page_token = None
    while True:
        resp = execute_with_retry(
            service.users().messages().list(
                userId="me", labelIds=[source_label_id],
                maxResults=100, pageToken=page_token,
            )
        )
        stubs.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    emails = []
    for stub in stubs:
        try:
            msg = execute_with_retry(
                service.users().messages().get(userId="me", id=stub["id"], format="full")
            )
            emails.append(_parse_message(msg))
        except Exception as e:
            print(f"  ⚠️  Skipping one email ({stub.get('id','?')}): {e}")

    emails.sort(key=lambda e: e["_internal_date"])  # oldest-first, like the other backends

    total = len(emails)
    print(f"  Found {total} email(s) with label '{SOURCE_FOLDER}'")
    for i, e in enumerate(emails, 1):
        unread = " [UNREAD]" if "UNREAD" in e["_msg"].get("labelIds", []) else ""
        print(f"    {i:>2}. [{e['received_date'][:10]}] "
              f"{e['subject'][:55]}{unread}")

    return emails


# Keep backward-compatible alias used by test_connection()
def get_unread_emails(top: int = 50) -> list:
    """Alias for get_all_emails() — 'top' is ignored (all are returned)."""
    return get_all_emails()


def download_attachments(email: dict, slot: str = "") -> list:
    """
    Download all supported (non-image) attachments from a single email
    via Gmail, decoding either the inline base64 data or a separate
    attachments.get() call, depending on how Gmail stored it.
    """
    service = get_gmail_service()
    msg_id  = email["id"]
    print(f"\n📎 Downloading attachment(s)...")

    try:
        msg = execute_with_retry(
            service.users().messages().get(userId="me", id=msg_id, format="full")
        )
    except GmailApiError as e:
        print(f"  ❌ Could not fetch message: {e}")
        return []

    text_chunks, attachments = [], []
    _walk_parts(msg.get("payload", {}), text_chunks, attachments)

    saved = []
    for att in attachments:
        try:
            fname = att["filename"]
            ext   = Path(fname).suffix.lower()

            if ext in IMAGE_EXT:
                print(f"  ⏭  Skipping image  : {fname}")
                continue
            if ext not in SUPPORTED:
                print(f"  ⏭  Skipping unsupported: {fname}")
                continue

            if att.get("attachmentId"):
                data = execute_with_retry(
                    service.users().messages().attachments().get(
                        userId="me", messageId=msg_id, id=att["attachmentId"]
                    )
                )
                content = _b64url_decode(data["data"])
            elif att.get("inline_data"):
                content = _b64url_decode(att["inline_data"])
            else:
                print(f"  ⏭  Skipping {fname}: no retrievable content")
                continue

            doc_type  = _classify(fname)
            dest_dir  = _dest_dir(doc_type, slot)
            save_path = dest_dir / fname

            save_path.write_bytes(content)
            print(f"  ✅ [{doc_type}] {fname} → {dest_dir}")

            text = _extract_text(str(save_path))

            saved.append({
                "file_path":      str(save_path),
                "file_name":      fname,
                "doc_type":       doc_type,
                "document_type":  doc_type,
                "extracted_text": text,
                "confidence":     0.95,
            })

        except Exception as e:
            print(f"  ❌ Error saving attachment {att.get('filename','?')}: {e}")

    print(f"  Saved {len(saved)} document(s)")
    return saved


def get_email_by_id(entry_id: str) -> dict:
    """
    Re-fetch a single email by its Gmail message id, returning the
    same shape get_all_emails() produces.

    Used by run_ap.resume_after_review() to re-resolve a message that
    may have been fetched minutes/hours earlier, before moving it.
    Returns {} if the message can't be found.
    """
    if not entry_id:
        return {}
    try:
        service = get_gmail_service()
        msg = execute_with_retry(
            service.users().messages().get(userId="me", id=entry_id, format="full")
        )
        return _parse_message(msg)
    except Exception as e:
        print(f"  ⚠️  Could not re-fetch email {entry_id[:12]}...: {e}")
        return {}


def mark_processed(email: dict) -> None:
    """
    Mark email as read and move it from 'ToBeProcessed' to 'Processed'
    — in Gmail terms: remove UNREAD + the ToBeProcessed label, add the
    Processed label (auto-created if it doesn't exist yet).
    """
    service = get_gmail_service()
    msg_id  = email["id"]

    try:
        label_map    = _get_label_map(service)
        processed_id = _get_or_create_label(service, PROCESSED_FOLDER, label_map)
        remove_ids   = ["UNREAD"]
        if SOURCE_FOLDER in label_map:
            remove_ids.append(label_map[SOURCE_FOLDER])

        execute_with_retry(
            service.users().messages().modify(
                userId="me", id=msg_id,
                body={"removeLabelIds": remove_ids, "addLabelIds": [processed_id]},
            )
        )
        print(f"  ✅ Marked as read")
        print(f"  📁 Moved to '{PROCESSED_FOLDER}'")
    except Exception as e:
        print(f"  ⚠️  Could not move to '{PROCESSED_FOLDER}': {e}")


def mark_exception(email: dict) -> None:
    """
    Move email from 'ToBeProcessed' to 'Exceptions' — remove the
    ToBeProcessed label, add the Exceptions label (auto-created if
    missing). Read/unread state is left untouched, matching the
    original COM behaviour of mark_exception().
    """
    service = get_gmail_service()
    msg_id  = email["id"]

    try:
        label_map     = _get_label_map(service)
        exceptions_id = _get_or_create_label(service, EXCEPTIONS_FOLDER, label_map)
        remove_ids    = []
        if SOURCE_FOLDER in label_map:
            remove_ids.append(label_map[SOURCE_FOLDER])

        execute_with_retry(
            service.users().messages().modify(
                userId="me", id=msg_id,
                body={"removeLabelIds": remove_ids, "addLabelIds": [exceptions_id]},
            )
        )
        print(f"  📁 Moved to '{EXCEPTIONS_FOLDER}'")
    except Exception as e:
        print(f"  ⚠️  Could not move to '{EXCEPTIONS_FOLDER}': {e}")
