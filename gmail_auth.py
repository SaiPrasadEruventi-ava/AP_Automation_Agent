"""
gmail_auth.py — Gmail API authentication  v1.0
=================================================
Builds an authorized Gmail API service client. Two auth modes are
supported, selected via AP_GMAIL_AUTH_MODE in .env:

"service_account"  (recommended for unattended/production use —
                    requires Google Workspace, NOT plain consumer Gmail)
------------------------------------------------------------------------
A service account JSON key with **domain-wide delegation**,
impersonating the mailbox in AP_GMAIL_MAILBOX. No human needs to be
present at run time — same operating model as graph_auth.py's
client-credentials flow.

Setup:
  1. Google Cloud Console → IAM & Admin → Service Accounts → Create.
  2. Create a JSON key for it, download → AP_GMAIL_SERVICE_ACCOUNT_FILE.
  3. Note the service account's numeric "Client ID" (Details tab).
  4. Google Workspace Admin Console → Security → API Controls →
     Domain-wide Delegation → Add new. Client ID = step 3. Scopes:
         https://www.googleapis.com/auth/gmail.modify
         https://www.googleapis.com/auth/gmail.send
     (a Workspace super admin must do this step)
  5. Set AP_GMAIL_MAILBOX to the address the agent should act as.

"oauth"  (works with a personal/non-Workspace Gmail account, or any
          time domain-wide delegation isn't available)
------------------------------------------------------------------------
A one-time interactive browser consent flow using a Desktop-app OAuth
client (client secret JSON from Google Cloud Console). Produces a
refresh token cached to disk (AP_GMAIL_TOKEN_FILE) and silently
refreshed on every subsequent run — no browser needed again unless the
token is revoked or deleted.

Setup:
  1. Google Cloud Console → APIs & Services → Credentials →
     Create Credentials → OAuth client ID → Application type: Desktop app.
  2. Download the JSON → AP_GMAIL_OAUTH_CLIENT_FILE.
  3. First run of the agent opens a browser for you to grant consent;
     after that, gmail_token.json (or your configured path) is reused.

Either mode requires the Gmail API to be enabled on the Google Cloud
project (APIs & Services → Library → Gmail API → Enable).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

AUTH_MODE          = os.getenv("AP_GMAIL_AUTH_MODE", "oauth").strip().lower()
MAILBOX            = os.getenv("AP_GMAIL_MAILBOX", "")
SA_KEY_FILE        = os.getenv("AP_GMAIL_SERVICE_ACCOUNT_FILE", "")
OAUTH_CLIENT_FILE  = os.getenv("AP_GMAIL_OAUTH_CLIENT_FILE", "gmail_oauth_client.json")
TOKEN_FILE         = os.getenv("AP_GMAIL_TOKEN_FILE", "gmail_token.json")

# gmail.modify covers read + label add/remove (our "move" equivalent);
# gmail.send covers sending. Both are requested so one credential
# handles the whole pipeline.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


class GmailAuthError(RuntimeError):
    """Raised when Gmail credentials are missing/invalid or a token can't be acquired."""


_service = None


def _build_service_account_creds():
    from google.oauth2 import service_account

    if not SA_KEY_FILE or not Path(SA_KEY_FILE).exists():
        raise GmailAuthError(
            f"AP_GMAIL_SERVICE_ACCOUNT_FILE not set or file not found: "
            f"{SA_KEY_FILE!r}. Download the service account JSON key from "
            f"Google Cloud Console → IAM & Admin → Service Accounts, and "
            f"point this variable at it."
        )
    if not MAILBOX:
        raise GmailAuthError(
            "AP_GMAIL_MAILBOX is required in service_account mode — the "
            "service account impersonates this mailbox via domain-wide "
            "delegation."
        )
    creds = service_account.Credentials.from_service_account_file(
        SA_KEY_FILE, scopes=SCOPES
    )
    return creds.with_subject(MAILBOX)


def _build_oauth_creds():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    import json as _json

    import redis_store

    creds = None

    # Prefer Redis (works on Vercel's read-only/ephemeral filesystem);
    # fall back to a local file for local dev/first-time setup.
    token_json = redis_store.gmail_token_get()
    token_path = Path(TOKEN_FILE)
    if token_json:
        creds = Credentials.from_authorized_user_info(_json.loads(token_json), SCOPES)
    elif token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(OAUTH_CLIENT_FILE).exists():
                raise GmailAuthError(
                    f"OAuth client file not found: {OAUTH_CLIENT_FILE!r}. "
                    f"Download it from Google Cloud Console → APIs & Services "
                    f"→ Credentials → (your Desktop app OAuth client) → "
                    f"Download JSON, and set AP_GMAIL_OAUTH_CLIENT_FILE to "
                    f"its path. NOTE: the interactive browser consent below "
                    f"only works when run locally — do this once on your "
                    f"machine; it caches the token to Redis, and Vercel "
                    f"will reuse it from there without ever needing a "
                    f"browser."
                )
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CLIENT_FILE, SCOPES)
            print("\n  🔐 No cached Gmail token found — opening browser for one-time consent...")
            creds = flow.run_local_server(port=0)

        # Persist to both Redis (source of truth on Vercel) and the local
        # file (handy for local dev/debugging) whenever we have a fresh
        # or refreshed token.
        redis_store.gmail_token_set(creds.to_json())
        try:
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except OSError:
            pass  # read-only filesystem (e.g. Vercel) — Redis already has it
        print("  ✅ Gmail token cached to Redis")

    return creds


def get_gmail_service():
    """
    Return a cached, authorized Gmail API service client
    (googleapiclient.discovery.Resource), building it on first call.
    """
    global _service
    if _service is not None:
        return _service

    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise GmailAuthError(
            "Required packages missing. Install with: "
            "pip install google-api-python-client google-auth google-auth-oauthlib"
        ) from e

    if AUTH_MODE == "service_account":
        creds = _build_service_account_creds()
    elif AUTH_MODE == "oauth":
        creds = _build_oauth_creds()
    else:
        raise GmailAuthError(
            f"Unknown AP_GMAIL_AUTH_MODE={AUTH_MODE!r} in .env — must be "
            f"'service_account' or 'oauth'."
        )

    _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service
