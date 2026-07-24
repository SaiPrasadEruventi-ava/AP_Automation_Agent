"""
graph_auth.py — Microsoft Graph app-only authentication  v1.0
================================================================
Acquires and caches an OAuth2 access token for Microsoft Graph using
the OAuth2 client-credentials flow (app-only / daemon auth via MSAL).

Why app-only auth
------------------
This agent runs unattended (--watch mode, scheduled task, service),
so there is no interactive user available to complete a login prompt
every time the token expires. App-only auth uses a registered Azure AD
application (client ID + client secret, or a certificate) that has
been granted admin-consented **Application** permissions:

    Mail.ReadWrite   — read/move messages in the target mailbox
    Mail.Send        — send the result / review-request emails

Because there's no signed-in user, every Graph call operates against
an explicit mailbox, addressed by its UPN or object ID:

    GET https://graph.microsoft.com/v1.0/users/{mailbox}/messages

`AP_GRAPH_MAILBOX` (see .env.example) supplies that address.

Required environment variables (see .env.example)
---------------------------------------------------
    AP_GRAPH_TENANT_ID
    AP_GRAPH_CLIENT_ID
    AP_GRAPH_CLIENT_SECRET
    AP_GRAPH_MAILBOX

Token caching
--------------
MSAL's ConfidentialClientApplication already caches tokens in memory
and transparently returns a cached (still-valid) token from
acquire_token_for_client() without hitting the network — we simply
keep one Application instance alive for the process lifetime.
"""

import os
import threading

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_SCOPE = ["https://graph.microsoft.com/.default"]

TENANT_ID     = os.getenv("AP_GRAPH_TENANT_ID", "")
CLIENT_ID     = os.getenv("AP_GRAPH_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("AP_GRAPH_CLIENT_SECRET", "")
MAILBOX       = os.getenv("AP_GRAPH_MAILBOX", "")

_lock = threading.Lock()
_app  = None


class GraphAuthError(RuntimeError):
    """Raised when Graph credentials are missing or a token can't be acquired."""


def _require_config() -> None:
    missing = [
        name for name, val in [
            ("AP_GRAPH_TENANT_ID",     TENANT_ID),
            ("AP_GRAPH_CLIENT_ID",     CLIENT_ID),
            ("AP_GRAPH_CLIENT_SECRET", CLIENT_SECRET),
            ("AP_GRAPH_MAILBOX",       MAILBOX),
        ] if not val
    ]
    if missing:
        raise GraphAuthError(
            "Missing Graph API configuration in .env: " + ", ".join(missing) +
            ". See .env.example for the required Azure AD app registration "
            "values (tenant ID, client ID, client secret) and the target "
            "mailbox address."
        )


def _get_app():
    global _app
    if _app is not None:
        return _app
    with _lock:
        if _app is None:
            _require_config()
            try:
                import msal
            except ImportError as e:
                raise GraphAuthError(
                    "The 'msal' package is required for Graph API auth. "
                    "Install it with: pip install msal"
                ) from e
            _app = msal.ConfidentialClientApplication(
                client_id=CLIENT_ID,
                client_credential=CLIENT_SECRET,
                authority=f"https://login.microsoftonline.com/{TENANT_ID}",
            )
    return _app


def get_access_token() -> str:
    """
    Return a valid Graph access token, acquiring/refreshing as needed.

    MSAL transparently serves a cached token (from its internal cache)
    when one is still valid, and only makes a network call to Azure AD
    when it actually needs a new one — so it's safe to call this before
    every Graph request without worrying about hammering the token
    endpoint.
    """
    app = _get_app()
    result = app.acquire_token_for_client(scopes=_SCOPE)

    if "access_token" not in result:
        err  = result.get("error", "unknown_error")
        desc = result.get("error_description", "No description provided.")
        raise GraphAuthError(f"Could not acquire Graph token: {err} — {desc}")

    return result["access_token"]


def auth_header() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}"}
