"""
graph_client.py — thin Microsoft Graph REST helper  v1.0
===========================================================
Shared HTTP plumbing used by graph_reader.py and graph_mailer.py so
neither module has to duplicate auth-header injection, retry-on-429,
or error-message formatting.

Every function returns parsed JSON (dict) for JSON responses, or the
raw `requests.Response` for endpoints (like /content) that return
binary/opaque bodies.
"""

import time

import requests

from graph_auth import GRAPH_BASE, MAILBOX, auth_header, GraphAuthError

_MAX_RETRIES  = 4
_RETRY_STATUS = {429, 502, 503, 504}


class GraphApiError(RuntimeError):
    """Raised for any non-2xx Graph response after retries are exhausted."""


def _url(path: str) -> str:
    """
    Build a full Graph URL from a path fragment.

    `path` may already be an absolute URL (Graph returns full URLs in
    @odata.nextLink and in some action responses) — in that case it's
    returned unchanged.
    """
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{GRAPH_BASE}{path}"


def _request(method: str, path: str, **kwargs) -> requests.Response:
    url = _url(path)
    last_exc = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            headers = kwargs.pop("headers", {})
            headers.update(auth_header())
            resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        except requests.RequestException as e:
            last_exc = e
            time.sleep(min(2 ** attempt, 10))
            continue

        if resp.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES:
            retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
            time.sleep(min(retry_after, 15))
            continue

        if not resp.ok:
            try:
                detail = resp.json().get("error", {}).get("message", resp.text)
            except Exception:
                detail = resp.text
            raise GraphApiError(
                f"Graph {method} {path} failed [{resp.status_code}]: {detail}"
            )

        return resp

    raise GraphApiError(f"Graph {method} {path} failed after {_MAX_RETRIES} attempts: {last_exc}")


def get(path: str, **kwargs) -> dict:
    resp = _request("GET", path, **kwargs)
    return resp.json() if resp.content else {}


def get_all_pages(path: str, **kwargs) -> list:
    """Follow @odata.nextLink until exhausted; return the concatenated `value` lists."""
    items = []
    next_path = path
    first = True
    while next_path:
        data = get(next_path, **(kwargs if first else {}))
        items.extend(data.get("value", []))
        next_path = data.get("@odata.nextLink")
        first = False
    return items


def post(path: str, json_body: dict = None, **kwargs) -> dict:
    resp = _request("POST", path, json=json_body, **kwargs)
    return resp.json() if resp.content else {}


def patch(path: str, json_body: dict) -> dict:
    resp = _request("PATCH", path, json=json_body)
    return resp.json() if resp.content else {}


def mailbox_path(suffix: str) -> str:
    """Build a /users/{mailbox}/... path for the configured target mailbox."""
    if not MAILBOX:
        raise GraphAuthError(
            "AP_GRAPH_MAILBOX is not set in .env — Graph API calls need to "
            "know which mailbox to operate on (app-only auth has no signed-in user)."
        )
    return f"/users/{MAILBOX}{suffix}"
