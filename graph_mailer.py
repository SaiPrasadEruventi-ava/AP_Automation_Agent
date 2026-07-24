"""
graph_mailer.py — Send email via Microsoft Graph  v1.0
==========================================================
Drop-in replacement for the win32com "outlook.CreateItem(0) ... .Send()"
pattern that was scattered across summary_sender.py and run_ap.py.

Uses POST /users/{mailbox}/sendMail — the same app-only Graph app
registration used by graph_reader.py (requires the Mail.Send
Application permission, admin-consented).

The mailbox that SENDS the mail is the one configured via
AP_GRAPH_MAILBOX (same as the reader) unless AP_GRAPH_SEND_AS is set
to a different address — useful if invoices should appear to come
from a shared "ap-noreply@yourco.com" mailbox while a different
mailbox holds the ToBeProcessed folder being polled.
"""

import os

from graph_client import post, mailbox_path, GraphApiError
from graph_auth import MAILBOX

SEND_AS_MAILBOX = os.getenv("AP_GRAPH_SEND_AS", MAILBOX)


def _send_mail_path() -> str:
    if not SEND_AS_MAILBOX:
        raise GraphApiError(
            "No sending mailbox configured — set AP_GRAPH_MAILBOX (and, "
            "optionally, AP_GRAPH_SEND_AS to send from a different address) "
            "in .env."
        )
    return f"/users/{SEND_AS_MAILBOX}/sendMail"


def send_html_email(subject: str, html_body: str, to: str, save_to_sent: bool = True) -> None:
    """
    Send an HTML email via Microsoft Graph.

    Parameters
    ----------
    subject      : email subject line
    html_body    : full HTML body (already wrapped in <html>...</html> or
                   a bare fragment — Graph accepts either)
    to           : recipient address, or comma-separated list of addresses
    save_to_sent : whether Graph should save a copy to Sent Items
                   (maps to the API's saveToSentItems flag)

    Raises
    ------
    GraphApiError on any non-2xx response from Graph (after retries).
    Callers are expected to catch this and fall back to saving an HTML
    file, same as the old code did when win32com/Outlook wasn't available.
    """
    recipients = [
        {"emailAddress": {"address": addr.strip()}}
        for addr in to.split(",") if addr.strip()
    ]
    if not recipients:
        raise GraphApiError(f"No valid recipient address(es) in: {to!r}")

    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": html_body,
            },
            "toRecipients": recipients,
        },
        "saveToSentItems": save_to_sent,
    }

    # sendMail returns 202 Accepted with an empty body on success.
    post(_send_mail_path(), json_body=payload)
