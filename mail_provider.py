"""
mail_provider.py — mail backend selector  v1.0
==================================================
Single switchboard so run_ap.py and summary_sender.py don't need to
know or care whether they're talking to Microsoft Graph or Gmail.

Selected via AP_MAIL_PROVIDER in .env: "graph" or "gmail" (default
"graph"). Both backends expose an identical function surface —

    get_all_emails()
    get_unread_emails(top=50)
    download_attachments(email, slot="")
    get_email_by_id(entry_id)
    mark_processed(email)
    mark_exception(email)
    send_html_email(subject, html_body, to, save_to_sent=True)
    SOURCE_FOLDER / PROCESSED_FOLDER / EXCEPTIONS_FOLDER

— so this module just re-exports the right implementation under one
name. Callers do:

    from mail_provider import get_all_emails, send_html_email, ...

instead of importing graph_reader/graph_mailer or gmail_reader/
gmail_mailer directly, and nothing else in the codebase needs to
change when someone flips AP_MAIL_PROVIDER.

To add a third provider (e.g. IMAP/SMTP for a generic mailbox), give
it the same seven names and add one more branch below.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

PROVIDER = os.getenv("AP_MAIL_PROVIDER", "graph").strip().lower()

if PROVIDER == "graph":
    from graph_reader import (
        get_all_emails, get_unread_emails, download_attachments,
        get_email_by_id, mark_processed, mark_exception,
        SOURCE_FOLDER, PROCESSED_FOLDER, EXCEPTIONS_FOLDER,
    )
    from graph_mailer import send_html_email

elif PROVIDER == "gmail":
    from gmail_reader import (
        get_all_emails, get_unread_emails, download_attachments,
        get_email_by_id, mark_processed, mark_exception,
        SOURCE_FOLDER, PROCESSED_FOLDER, EXCEPTIONS_FOLDER,
    )
    from gmail_mailer import send_html_email

else:
    raise RuntimeError(
        f"Unknown AP_MAIL_PROVIDER={PROVIDER!r} in .env — must be "
        f"'graph' or 'gmail'."
    )


def provider_label() -> str:
    """Human-readable label for banners/logs (e.g. 'MICROSOFT GRAPH' / 'GMAIL')."""
    return {"graph": "MICROSOFT GRAPH", "gmail": "GMAIL"}.get(PROVIDER, PROVIDER.upper())
