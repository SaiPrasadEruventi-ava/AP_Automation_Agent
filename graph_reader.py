"""
graph_reader.py — Microsoft Graph Mailbox + Attachment Reader  v1.0
======================================================================
Drop-in replacement for outlook_reader.py. Reads ALL emails from the
'ToBeProcessed' folder via the Microsoft Graph REST API instead of
Outlook Desktop COM (pywin32) — so the agent no longer needs Outlook
installed, or even to run on Windows. After processing each email the
agent moves it to either 'Processed' or 'Exceptions'; those sibling
folders are auto-created if missing (same behaviour as the COM
version — 'ToBeProcessed' itself must already exist).

Public API is intentionally identical to outlook_reader.py so callers
(run_ap.py) only need to change their import line:

    from outlook_reader import get_all_emails, download_attachments, ...
    →
    from graph_reader    import get_all_emails, download_attachments, ...

v3.0 attachment model (unchanged from the COM version)
--------------------------------------------------------
The user emails:
  1. Invoice PDF          (required — exactly one)
  2. Supporting document  (optional — packing slip, vendor note, etc.
                           Saved and linked for audit, but NOT matched
                           against.)
Purchase Order and GRN are looked up from the mock ERP workbook
(see erp_mock.py) using the PO Number found on the Invoice — they are
NOT expected as email attachments.

Auth model
-----------
App-only (client-credentials) auth via graph_auth.py — see that
module's docstring and .env.example for the required Azure AD app
registration and permissions (Mail.ReadWrite, Mail.Send).

Object model differences vs. the COM version
-----------------------------------------------
There is no long-lived COM MailItem to hold onto — each email dict's
"_msg" key holds the raw Graph message JSON (a plain dict) instead,
and "_namespace" is unused (kept as None for shape-compatibility).
Every operation (move, mark-read, fetch-by-id) is a fresh, stateless
REST call keyed by the message's Graph `id`, so there's nothing that
can go stale mid-run the way a COM object handle could.
"""

import base64
import os
import re
from pathlib import Path
from datetime import datetime

from graph_client import get, get_all_pages, patch, post, mailbox_path, GraphApiError

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

# ── Folder names (configurable via env) ────────────────────────────────────
SOURCE_FOLDER     = os.getenv("AP_SOURCE_FOLDER",     "ToBeProcessed")
PROCESSED_FOLDER  = os.getenv("AP_PROCESSED_FOLDER",  "Processed")
EXCEPTIONS_FOLDER = os.getenv("AP_EXCEPTIONS_FOLDER", "Exceptions")

# Ask Graph to return message bodies as plain text instead of HTML —
# closest equivalent of COM's msg.Body (vs. msg.HTMLBody).
_TEXT_BODY_HEADER = {"Prefer": 'outlook.body-content-type="text"'}

_MESSAGE_SELECT = (
    "id,subject,from,sender,receivedDateTime,body,hasAttachments,isRead"
)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _classify(filename: str) -> str:
    """Classify attachment by filename → Invoice / Supporting (same rule as COM version)."""
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
    """Extract plain text from PDF or text file (identical to COM version)."""
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


def _find_child_folder(parent_id: str, name: str):
    """Search direct children of a mail folder for one matching `name` (case-insensitive)."""
    children = get_all_pages(
        mailbox_path(f"/mailFolders/{parent_id}/childFolders?$top=100")
    )
    name_lower = name.lower()
    for f in children:
        if f.get("displayName", "").lower() == name_lower:
            return f
    return None


def _get_or_create_subfolder(parent_id: str, name: str) -> dict:
    """Find a named child folder under parent_id, create it if missing."""
    existing = _find_child_folder(parent_id, name)
    if existing:
        return existing
    created = post(
        mailbox_path(f"/mailFolders/{parent_id}/childFolders"),
        json_body={"displayName": name},
    )
    print(f"  📁 Created '{name}' folder")
    return created


def _get_source_folder() -> dict:
    """
    Locate the 'ToBeProcessed' folder and return its Graph folder object.

    Search order (mirrors the COM version):
      1. Child of the well-known 'inbox' folder
      2. Top-level folder (sibling of Inbox at the mailbox root)

    Raises RuntimeError with a clear message if not found.
    """
    inbox = get(mailbox_path("/mailFolders/inbox"))
    found = _find_child_folder(inbox["id"], SOURCE_FOLDER)
    if found:
        print(f"  📂 Found '{SOURCE_FOLDER}' under Inbox")
        found["_parent_id"] = inbox["id"]
        return found

    top_level = get_all_pages(mailbox_path("/mailFolders?$top=100"))
    src_lower = SOURCE_FOLDER.lower()
    for f in top_level:
        if f.get("displayName", "").lower() == src_lower:
            print(f"  📂 Found '{SOURCE_FOLDER}' at mailbox root")
            f["_parent_id"] = None
            return f

    raise RuntimeError(
        f"Folder '{SOURCE_FOLDER}' not found.\n"
        f"Please create a folder named '{SOURCE_FOLDER}' in the mailbox "
        f"(as a subfolder of Inbox or at the mailbox root) and move "
        f"invoice emails into it before running this agent."
    )


def _sender(msg: dict) -> tuple:
    frm = (msg.get("from") or msg.get("sender") or {}).get("emailAddress", {})
    return frm.get("address", ""), frm.get("name", "")


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_all_emails() -> list:
    """
    Return ALL emails currently sitting in the 'ToBeProcessed' folder,
    oldest-first, via Microsoft Graph.

    Returns
    -------
    list of dict, each containing:
        id, subject, sender_email, sender_name, received_date,
        body_text, attachment_count, _msg (raw Graph message dict),
        _namespace (always None — kept for shape-compatibility with
        the old COM-based outlook_reader.py)
    """
    print(f"\n📧 Connecting to Microsoft Graph...")
    print(f"  Source folder : '{SOURCE_FOLDER}'")

    try:
        folder = _get_source_folder()
    except RuntimeError as e:
        print(f"  ❌ {e}")
        raise
    except GraphApiError as e:
        print(f"  ❌ Cannot reach Microsoft Graph: {e}")
        raise

    path = mailbox_path(
        f"/mailFolders/{folder['id']}/messages"
        f"?$select={_MESSAGE_SELECT}&$orderby=receivedDateTime asc&$top=100"
    )
    raw_messages = get_all_pages(path, headers=_TEXT_BODY_HEADER)

    emails = []
    for msg in raw_messages:
        try:
            sender_email, sender_name = _sender(msg)
            received = msg.get("receivedDateTime", "")
            try:
                received = (
                    datetime.fromisoformat(received.replace("Z", "+00:00"))
                    .strftime("%Y-%m-%d %H:%M:%S")
                )
            except Exception:
                received = str(datetime.now())

            emails.append({
                "id":               msg["id"],
                "subject":          msg.get("subject") or "",
                "sender_email":     sender_email,
                "sender_name":      sender_name,
                "received_date":    received,
                "body_text":        (msg.get("body") or {}).get("content", ""),
                "attachment_count": 1 if msg.get("hasAttachments") else 0,
                "_msg":             msg,
                "_namespace":       None,
            })
        except Exception as e:
            print(f"  ⚠️  Skipping one email: {e}")

    # hasAttachments doesn't give an exact count — fetch the real count
    # for messages that have at least one, so downstream logging matches
    # what download_attachments() will actually find.
    for e in emails:
        if e["attachment_count"]:
            try:
                atts = get(mailbox_path(f"/messages/{e['id']}/attachments?$select=id"))
                e["attachment_count"] = len(atts.get("value", []))
            except GraphApiError:
                pass  # keep the boolean-derived count (1) if this fails

    total = len(emails)
    print(f"  Found {total} email(s) in '{SOURCE_FOLDER}'")
    for i, e in enumerate(emails, 1):
        unread = " [UNREAD]" if not e["_msg"].get("isRead", True) else ""
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
    via Graph, decoding the base64 `contentBytes` Graph returns for
    fileAttachments.

    Parameters
    ----------
    email : dict   — as returned by get_all_emails()
    slot  : str    — optional sub-folder suffix so multiple emails being
                     processed together don't overwrite each other's files.

    Returns list of attachment dicts with extracted text.
    """
    msg_id = email["id"]
    print(f"\n📎 Downloading attachment(s)...")

    try:
        atts = get_all_pages(mailbox_path(f"/messages/{msg_id}/attachments"))
    except GraphApiError as e:
        print(f"  ❌ Could not list attachments: {e}")
        return []

    saved = []
    for att in atts:
        try:
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                print(f"  ⏭  Skipping non-file attachment: {att.get('name')}")
                continue

            fname = att.get("name", "unnamed")
            ext   = Path(fname).suffix.lower()

            if ext in IMAGE_EXT:
                print(f"  ⏭  Skipping image  : {fname}")
                continue
            if ext not in SUPPORTED:
                print(f"  ⏭  Skipping unsupported: {fname}")
                continue

            content_b64 = att.get("contentBytes")
            if not content_b64:
                print(f"  ⏭  Skipping {fname}: no inline content (contentBytes missing)")
                continue

            doc_type  = _classify(fname)
            dest_dir  = _dest_dir(doc_type, slot)
            save_path = dest_dir / fname

            save_path.write_bytes(base64.b64decode(content_b64))
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
            print(f"  ❌ Error saving attachment {att.get('name','?')}: {e}")

    print(f"  Saved {len(saved)} document(s)")
    return saved


def get_email_by_id(entry_id: str) -> dict:
    """
    Re-fetch a single email by its Graph message id, returning the same
    shape get_all_emails() produces.

    Used by run_ap.resume_after_review() — when a web-portal decision
    arrives, possibly minutes or hours after the original
    get_all_emails() snapshot, we re-resolve the message fresh so
    mark_processed()/mark_exception() can still move it.

    Returns {} if the item can't be found (e.g. it was manually moved
    or deleted) — callers should treat that as "skip moving the email,
    but still finish the rest of the pipeline."
    """
    if not entry_id:
        return {}
    try:
        msg = get(
            mailbox_path(f"/messages/{entry_id}?$select={_MESSAGE_SELECT}"),
            headers=_TEXT_BODY_HEADER,
        )
        sender_email, sender_name = _sender(msg)
        received = msg.get("receivedDateTime", "")
        try:
            received = (
                datetime.fromisoformat(received.replace("Z", "+00:00"))
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        except Exception:
            pass

        return {
            "id":            msg["id"],
            "subject":       msg.get("subject") or "",
            "sender_email":  sender_email,
            "sender_name":   sender_name,
            "received_date": received,
            "_msg":          msg,
            "_namespace":    None,
        }
    except Exception as e:
        print(f"  ⚠️  Could not re-fetch email {entry_id[:12]}...: {e}")
        return {}


def mark_processed(email: dict) -> None:
    """
    Mark email as read and move it from 'ToBeProcessed' to 'Processed'.
    The Processed folder is auto-created as a sibling of ToBeProcessed.
    """
    msg_id = email["id"]

    try:
        patch(mailbox_path(f"/messages/{msg_id}"), {"isRead": True})
        print(f"  ✅ Marked as read")
    except GraphApiError as e:
        print(f"  ⚠️  Could not mark as read: {e}")

    try:
        source = _get_source_folder()
        parent_id = source.get("_parent_id")
        # A top-level ToBeProcessed folder has no parent to nest siblings
        # under except the mailbox root — use msFolderRoot in that case.
        container_id = parent_id or "msgfolderroot"
        processed = _get_or_create_subfolder(container_id, PROCESSED_FOLDER)
        post(mailbox_path(f"/messages/{msg_id}/move"),
             json_body={"destinationId": processed["id"]})
        print(f"  📁 Moved to '{PROCESSED_FOLDER}'")
    except Exception as e:
        print(f"  ⚠️  Could not move to '{PROCESSED_FOLDER}': {e}")


def mark_exception(email: dict) -> None:
    """
    Move email from 'ToBeProcessed' to 'Exceptions'.
    The Exceptions folder is auto-created as a sibling of ToBeProcessed.
    """
    msg_id = email["id"]

    try:
        source = _get_source_folder()
        parent_id = source.get("_parent_id")
        container_id = parent_id or "msgfolderroot"
        exceptions = _get_or_create_subfolder(container_id, EXCEPTIONS_FOLDER)
        post(mailbox_path(f"/messages/{msg_id}/move"),
             json_body={"destinationId": exceptions["id"]})
        print(f"  📁 Moved to '{EXCEPTIONS_FOLDER}'")
    except Exception as e:
        print(f"  ⚠️  Could not move to '{EXCEPTIONS_FOLDER}': {e}")
