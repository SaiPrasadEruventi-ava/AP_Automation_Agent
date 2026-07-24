"""
gmail_mailer.py — Send email via the Gmail API  v1.0
=========================================================
Gmail equivalent of graph_mailer.py — same public function signature
(send_html_email(subject, html_body, to, save_to_sent=True)) so
mail_provider.py can point either backend's send_html_email at
run_ap.py / summary_sender.py interchangeably.

Builds a standard RFC 2822 MIME message and sends it via
POST users.messages.send, base64url-encoded per the Gmail API's `raw`
field requirement. Uses the same authenticated service
(gmail_auth.get_gmail_service()) as gmail_reader.py, so it sends as
whichever mailbox that auth mode resolves to (the impersonated
mailbox in service_account mode, or whichever account completed the
OAuth consent flow in oauth mode).
"""

import base64
import os
from email.mime.text import MIMEText

from gmail_auth import get_gmail_service
from gmail_client import execute_with_retry, GmailApiError

# Gmail has no equivalent of Graph's "send as a different mailbox" —
# this only works if the address is a verified "Send mail as" alias on
# the authenticated account (Gmail Settings → Accounts → Send mail as).
SEND_AS = os.getenv("AP_GMAIL_SEND_AS", "")


def send_html_email(subject: str, html_body: str, to: str, save_to_sent: bool = True) -> None:
    """
    Send an HTML email via the Gmail API.

    Parameters
    ----------
    subject      : email subject line
    html_body    : full HTML body (bare fragment or full <html>...</html>)
    to           : recipient address, or comma-separated list
    save_to_sent : accepted for interface parity with graph_mailer's
                   send_html_email() — Gmail's users.messages.send
                   always saves a copy to Sent by default, so this is
                   currently a no-op here.

    Raises
    ------
    GmailApiError on failure (after retries). Callers are expected to
    catch this and fall back to saving an HTML file, same as the
    Graph-based path does.
    """
    message = MIMEText(html_body, "html", "utf-8")
    message["To"]      = to
    message["Subject"] = subject
    if SEND_AS:
        message["From"] = SEND_AS

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    try:
        service = get_gmail_service()
    except Exception as e:
        raise GmailApiError(f"Could not build Gmail service: {e}") from e

    execute_with_retry(
        service.users().messages().send(userId="me", body={"raw": raw})
    )
