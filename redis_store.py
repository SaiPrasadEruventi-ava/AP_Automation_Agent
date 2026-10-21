"""
redis_store.py — Upstash Redis-backed storage layer (Vercel-ready)
====================================================================
Replaces three pieces of local-disk state that don't survive on
Vercel's read-only, ephemeral serverless filesystem:

    1. erp_mock.py's Excel workbook            -> Redis hashes/strings
    2. pending_reviews.py's JSON-per-token files -> Redis strings + a
                                                     Redis set as an index
    3. duplicate_checker.py's processed_invoices.json -> Redis hash +
                                                     sorted set (for the
                                                     fuzzy date-window
                                                     check)

Connects via the Upstash REST API (https://github.com/upstash/redis-py),
which is what Vercel's "Upstash for Redis" marketplace integration
provisions. This is deliberately NOT a plain TCP redis-py client —
serverless functions spin up/down per request and a REST client avoids
connection-pool exhaustion and cold-start handshake overhead.

Required environment variables (set these in the Vercel project, or in
.env for local dev):

    UPSTASH_REDIS_REST_URL
    UPSTASH_REDIS_REST_TOKEN

(When you add the Upstash integration from the Vercel Marketplace, it
sets both of these automatically on the linked project.)

Local dev fallback
-------------------
If those two env vars are absent, this module falls back to an
in-process dict so `python run_ap.py --mock` etc. still work without
any Redis instance during local development/testing. This fallback is
NOT persistent across process restarts — don't rely on it for anything
beyond quick local testing.
"""

import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

UPSTASH_URL   = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

# Vercel's own "KV" storage product is Upstash Redis under the hood, but
# its Marketplace integration names the env vars differently depending
# on how it was added (KV_REST_API_URL / KV_REST_API_TOKEN, sometimes
# KV_REST_API_READ_ONLY_TOKEN). Accept those as a fallback so this
# module works regardless of which naming Vercel's integration used.
if not UPSTASH_URL:
    UPSTASH_URL = os.getenv("KV_REST_API_URL", "")
if not UPSTASH_TOKEN:
    UPSTASH_TOKEN = os.getenv("KV_REST_API_TOKEN", "")

_KEY_PREFIX = os.getenv("AP_REDIS_KEY_PREFIX", "ap")  # namespacing, in case
                                                       # the Redis instance
                                                       # is shared with other
                                                       # apps/projects.


def _k(*parts: str) -> str:
    return ":".join([_KEY_PREFIX, *parts])


class _InMemoryFallback:
    """Minimal stand-in used only when Upstash isn't configured (local dev)."""

    def __init__(self):
        self._store = {}
        self._zsets = {}
        self._sets = {}

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value):
        self._store[key] = value
        return True

    def delete(self, key):
        existed = key in self._store
        self._store.pop(key, None)
        return 1 if existed else 0

    def hget(self, key, field):
        return self._store.get(key, {}).get(field)

    def hset(self, key, field, value):
        self._store.setdefault(key, {})[field] = value
        return True

    def hgetall(self, key):
        return dict(self._store.get(key, {}))

    def hdel(self, key, field):
        return self._store.get(key, {}).pop(field, None) is not None

    def sadd(self, key, member):
        self._sets.setdefault(key, set()).add(member)
        return True

    def srem(self, key, member):
        self._sets.setdefault(key, set()).discard(member)
        return True

    def smembers(self, key):
        return set(self._sets.get(key, set()))

    def zadd(self, key, scores: dict):
        self._zsets.setdefault(key, {}).update(scores)
        return True

    def zrem(self, key, member):
        self._zsets.get(key, {}).pop(member, None)
        return True

    def zrangebyscore(self, key, min_score, max_score):
        z = self._zsets.get(key, {})
        return [m for m, s in z.items() if min_score <= s <= max_score]


_fallback = _InMemoryFallback()
_client = None
_warned_fallback = False


def _get_client():
    """Lazily build (and cache) the Upstash REST client, or fall back."""
    global _client, _warned_fallback

    if _client is not None:
        return _client

    if not UPSTASH_URL or not UPSTASH_TOKEN:
        if not _warned_fallback:
            print(
                "  ⚠️  UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN not "
                "set — using a non-persistent in-memory store. Set these "
                "env vars (Vercel → Upstash Marketplace integration) before "
                "deploying, or data will not persist across invocations."
            )
            _warned_fallback = True
        _client = _fallback
        return _client

    from upstash_redis import Redis  # pip install upstash-redis

    _client = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)
    return _client


# ─────────────────────────────────────────────────────────────────────
# ERP data (replaces erp_mock.py's Excel workbook)
# ─────────────────────────────────────────────────────────────────────

def erp_set_po(po_number: str, po_data: dict) -> None:
    r = _get_client()
    r.hset(_k("erp", "po"), po_number.strip().upper(), json.dumps(po_data))


def erp_get_po(po_number: str) -> dict:
    r = _get_client()
    raw = r.hget(_k("erp", "po"), po_number.strip().upper())
    return json.loads(raw) if raw else {}


def erp_set_grn(po_number: str, grn_data: dict) -> None:
    r = _get_client()
    r.hset(_k("erp", "grn"), po_number.strip().upper(), json.dumps(grn_data))


def erp_get_grn(po_number: str) -> dict:
    r = _get_client()
    raw = r.hget(_k("erp", "grn"), po_number.strip().upper())
    return json.loads(raw) if raw else {}


def erp_list_po_numbers() -> list:
    r = _get_client()
    data = r.hgetall(_k("erp", "po")) or {}
    # upstash-redis returns a flat list [field, value, field, value, ...]
    # for hgetall in some client versions, and a dict in others — normalize.
    if isinstance(data, dict):
        return list(data.keys())
    return [data[i] for i in range(0, len(data), 2)]


# ─────────────────────────────────────────────────────────────────────
# Pending HITL reviews (replaces pending_reviews.py's per-token files)
# ─────────────────────────────────────────────────────────────────────

_PENDING_INDEX = _k("pending_reviews", "index")


def pending_save(token: str, record: dict) -> None:
    r = _get_client()
    r.set(_k("pending_reviews", token), json.dumps(record))
    if record.get("status") == "pending":
        r.sadd(_PENDING_INDEX, token)
    else:
        r.srem(_PENDING_INDEX, token)


def pending_get(token: str) -> dict:
    r = _get_client()
    raw = r.get(_k("pending_reviews", token))
    return json.loads(raw) if raw else {}


def pending_delete(token: str) -> None:
    r = _get_client()
    r.delete(_k("pending_reviews", token))
    r.srem(_PENDING_INDEX, token)


def pending_list_tokens() -> list:
    r = _get_client()
    members = r.smembers(_PENDING_INDEX) or set()
    return list(members)


# ─────────────────────────────────────────────────────────────────────
# Processed-invoice log (replaces duplicate_checker.py's JSON log file)
# ─────────────────────────────────────────────────────────────────────
# Stored as:
#   ap:processed_invoices          hash:   invoice_number -> JSON record
#                                          (last write wins per invoice
#                                          number — matches prior file
#                                          behaviour closely enough for
#                                          exact/tampered-duplicate checks)
#   ap:processed_invoices:by_date  zset:   score = unix ts of
#                                          processed_date, member =
#                                          invoice_number (for the fuzzy
#                                          vendor+amount+date-window check)

_PROCESSED_HASH  = _k("processed_invoices")
_PROCESSED_ZSET  = _k("processed_invoices", "by_date")


def processed_get_all() -> list:
    r = _get_client()
    data = r.hgetall(_PROCESSED_HASH) or {}
    if isinstance(data, dict):
        values = data.values()
    else:
        values = [data[i] for i in range(1, len(data), 2)]
    records = []
    for raw in values:
        try:
            records.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return records


def processed_get(invoice_number: str) -> dict:
    r = _get_client()
    raw = r.hget(_PROCESSED_HASH, invoice_number)
    return json.loads(raw) if raw else {}


def processed_add(record: dict, invoice_date_ts: float = None) -> None:
    r = _get_client()
    invoice_number = record["invoice_number"]
    r.hset(_PROCESSED_HASH, invoice_number, json.dumps(record))
    score = invoice_date_ts if invoice_date_ts is not None else time.time()
    r.zadd(_PROCESSED_ZSET, {invoice_number: score})


def processed_candidates_in_window(min_ts: float, max_ts: float) -> list:
    """Invoice numbers processed within [min_ts, max_ts], for the fuzzy check."""
    r = _get_client()
    members = r.zrangebyscore(_PROCESSED_ZSET, min_ts, max_ts) or []
    return list(members)


def processed_remove(invoice_number: str) -> None:
    """Remove a single invoice from the processed log (both the hash
    and the by-date sorted set) — lets it be reprocessed/resubmitted,
    e.g. to reuse the same test invoice again."""
    r = _get_client()
    r.hdel(_PROCESSED_HASH, invoice_number)
    r.zrem(_PROCESSED_ZSET, invoice_number)


def processed_clear() -> None:
    """Danger: wipes the whole processed-invoice log. Testing use only."""
    r = _get_client()
    for inv in processed_get_all():
        r.hdel(_PROCESSED_HASH, inv["invoice_number"])
        r.zrem(_PROCESSED_ZSET, inv["invoice_number"])


# ─────────────────────────────────────────────────────────────────────
# Gmail OAuth refresh token (replaces gmail_auth.py's local token file)
# ─────────────────────────────────────────────────────────────────────

_GMAIL_TOKEN_KEY = _k("auth", "gmail_token")


def gmail_token_get() -> str:
    r = _get_client()
    return r.get(_GMAIL_TOKEN_KEY) or ""


def gmail_token_set(token_json: str) -> None:
    r = _get_client()
    r.set(_GMAIL_TOKEN_KEY, token_json)


# ─────────────────────────────────────────────────────────────────────
# Last-run tracking (which trigger fired last: Cron vs Manual)
# ─────────────────────────────────────────────────────────────────────
# Vercel functions are stateless between invocations, so "was this run
# triggered by the 5-minute Cron schedule or by clicking the dashboard
# button" isn't something the process itself remembers — it has to be
# written to Redis so the dashboard can read it back afterward.

_LAST_RUN_KEY = _k("last_run")


def last_run_set(source: str, processed_count: int) -> None:
    r = _get_client()
    r.set(_LAST_RUN_KEY, json.dumps({
        "source": source,  # "Cron" or "Manual"
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "processed_count": processed_count,
    }))


def last_run_get() -> dict:
    r = _get_client()
    raw = r.get(_LAST_RUN_KEY)
    return json.loads(raw) if raw else {}


# ─────────────────────────────────────────────────────────────────────
# Full invoice validation detail (replaces "only in the email" — lets
# the dashboard show the same reasons/gate results after the fact)
# ─────────────────────────────────────────────────────────────────────

def invoice_detail_set(invoice_number: str, detail: dict) -> None:
    r = _get_client()
    r.set(_k("invoice_detail", invoice_number), json.dumps(detail))


def invoice_detail_get(invoice_number: str) -> dict:
    r = _get_client()
    raw = r.get(_k("invoice_detail", invoice_number))
    return json.loads(raw) if raw else {}


# ─────────────────────────────────────────────────────────────────────
# Exceptions log — submissions that couldn't be processed at all
# (no readable invoice, PO not found in ERP, extraction failure, etc.)
# =======================================================================
# Deliberately SEPARATE from the processed-invoice log: an exception is
# not a final disposition. If the vendor resends a corrected invoice
# with the same invoice number, it should still be processable —
# nothing here should make duplicate_checker treat it as "already
# processed."
# ─────────────────────────────────────────────────────────────────────

_EXCEPTIONS_HASH = _k("exceptions")
_EXCEPTIONS_ZSET = _k("exceptions", "by_date")


def exception_add(key: str, record: dict) -> None:
    r = _get_client()
    r.hset(_EXCEPTIONS_HASH, key, json.dumps(record))
    r.zadd(_EXCEPTIONS_ZSET, {key: time.time()})


def exception_get_all() -> list:
    r = _get_client()
    data = r.hgetall(_EXCEPTIONS_HASH) or {}
    if isinstance(data, dict):
        values = data.values()
    else:
        values = [data[i] for i in range(1, len(data), 2)]
    records = []
    for raw in values:
        try:
            records.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return records


def exception_remove(key: str) -> None:
    r = _get_client()
    r.hdel(_EXCEPTIONS_HASH, key)
    r.zrem(_EXCEPTIONS_ZSET, key)


def exception_clear_all() -> int:
    r = _get_client()
    data = r.hgetall(_EXCEPTIONS_HASH) or {}
    keys = list(data.keys()) if isinstance(data, dict) else [data[i] for i in range(0, len(data), 2)]
    for key in keys:
        r.hdel(_EXCEPTIONS_HASH, key)
        r.zrem(_EXCEPTIONS_ZSET, key)
    return len(keys)



# ─────────────────────────────────────────────────────────────────────
# Lookups used by the demo reset (run_ap.reset_invoice)
# ─────────────────────────────────────────────────────────────────────

def invoice_detail_delete(invoice_number: str) -> None:
    r = _get_client()
    r.delete(_k("invoice_detail", invoice_number))


def exception_keys_for_invoice(invoice_number: str) -> list:
    return [e.get("key") for e in exception_get_all()
            if e.get("invoice_number") == invoice_number and e.get("key")]


def pending_tokens_for_invoice(invoice_number: str) -> list:
    tokens = []
    for token in pending_list_tokens():
        rec = pending_get(token)
        if (rec.get("invoice_data") or {}).get("invoice_number") == invoice_number:
            tokens.append(token)
    return tokens
