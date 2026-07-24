"""
gmail_client.py — retry wrapper for Gmail API calls  v1.0
=============================================================
Google's discovery-based client (googleapiclient) returns request
objects whose .execute() call is what actually hits the network. This
module wraps that .execute() with the same retry-on-rate-limit/server-
error behaviour graph_client.py provides for Microsoft Graph, so
gmail_reader.py and gmail_mailer.py don't have to duplicate it.
"""

import time

_RETRY_STATUS = {403, 429, 500, 502, 503, 504}  # 403 also covers Gmail's
                                                 # rateLimitExceeded / userRateLimitExceeded
_MAX_RETRIES = 5


class GmailApiError(RuntimeError):
    """Raised for any Gmail API failure after retries are exhausted."""


def execute_with_retry(request):
    """
    Call .execute() on a Gmail API request object, retrying on
    rate-limit/server errors with exponential backoff.
    """
    from googleapiclient.errors import HttpError

    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return request.execute()
        except HttpError as e:
            status = e.resp.status if getattr(e, "resp", None) else None
            last_exc = e
            if status in _RETRY_STATUS and attempt < _MAX_RETRIES:
                time.sleep(min(2 ** attempt, 20))
                continue
            raise GmailApiError(f"Gmail API call failed [{status}]: {e}") from e
        except Exception as e:
            last_exc = e
            if attempt < _MAX_RETRIES:
                time.sleep(min(2 ** attempt, 10))
                continue
            raise GmailApiError(f"Gmail API call failed: {e}") from e

    raise GmailApiError(f"Gmail API call failed after {_MAX_RETRIES} attempts: {last_exc}")
