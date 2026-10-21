"""
sap_connector.py — Live SAP RFC connectivity (S/4HANA + ECC)
==============================================================
Low-level SAP access for the AP engine, over the RFC protocol via
`pyrfc` + the SAP NetWeaver RFC SDK 7.50 (see Docs/SAP_CONNECTOR_GUIDE).
Every PO / GRN / supplier-invoice read is a live RFC call — nothing is
cached or mirrored into Redis — so the dashboard and the 3-way match
always see SAP exactly as it is right now.

Two system profiles are supported, selected by SAP_ACTIVE_SYSTEM (or
per call via the `system` argument):

    S4   SAP S/4HANA  — "New IDES S4HANA" in SAP Logon (65.2.76.131,
                        instance 57, SID A4F, client 100)
    ECC  SAP ECC      — Avaali IDES (54.169.73.118, instance 00, SID AVI,
                        client 800), the older SAP Logon 750 system

Each profile reads SAP_<PROFILE>_ASHOST / _SYSNR / _CLIENT / _USER /
_PASSWD / _LANG (and optional _SAPROUTER) from .env. The two systems
differ in a few table names (S/4 keeps pricing in PRCD_ELEMENTS and
material documents in MATDOC; ECC uses KONV and MKPF/MSEG) — those
differences are resolved by erp_sap.py via `is_s4()`, not here.

Requirements (Windows host — the RPA server):
  - SAP NW RFC SDK extracted (e.g. C:\\sap\\nwrfcsdk) with its lib\\
    folder on PATH and SAPNWRFC_HOME set
  - pip install pyrfc==3.3.1 (pinned: every pyrfc release is "yanked"
    on PyPI as unmaintained, but this exact pin still installs and works)
  - network reach to the SAP gateway port 33<sysnr> (3357 for A4F)

`pyrfc` is imported lazily so the rest of the app (mock ERP mode,
dashboard, approval portal) still loads on a machine without the SDK.
"""

import os
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

SYSTEM_LABELS = {
    "S4":  "SAP S/4HANA",
    "ECC": "SAP ECC (SAP Logon 750)",
}

_RFC_WHERE_LINE = 72          # RFC_READ_TABLE OPTIONS rows are CHAR72
_IN_BATCH       = 40          # values per IN (...) list, keeps OPTIONS small


class SapError(Exception):
    """Any SAP-side failure (connection, logon, RFC error, BAPI error)."""


class SapBapiError(SapError):
    """A BAPI ran but reported E/A messages in its RETURN table."""

    def __init__(self, message: str, messages: list = None):
        super().__init__(message)
        self.messages = messages or []


# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

def active_system() -> str:
    return os.getenv("SAP_ACTIVE_SYSTEM", "S4").strip().upper() or "S4"


def available_systems() -> list:
    """Profiles that have at least a host + user configured."""
    return [s for s in SYSTEM_LABELS
            if os.getenv(f"SAP_{s}_ASHOST") and os.getenv(f"SAP_{s}_USER")]


def _cfg(system: str) -> dict:
    system = (system or active_system()).upper()
    p = f"SAP_{system}_"
    missing = [k for k in ("ASHOST", "SYSNR", "CLIENT", "USER", "PASSWD")
               if not os.getenv(p + k)]
    if missing:
        raise SapError(
            f"SAP profile '{system}' is not configured — missing "
            f"{', '.join(p + k for k in missing)} in .env"
        )
    cfg = {
        "ashost": os.getenv(p + "ASHOST"),
        "sysnr":  os.getenv(p + "SYSNR"),
        "client": os.getenv(p + "CLIENT"),
        "user":   os.getenv(p + "USER"),
        "passwd": os.getenv(p + "PASSWD"),
        "lang":   os.getenv(p + "LANG", "EN"),
    }
    if os.getenv(p + "SAPROUTER"):
        cfg["saprouter"] = os.getenv(p + "SAPROUTER")
    return cfg


def public_config(system: str = None) -> dict:
    """Connection parameters safe to show in the UI (no password)."""
    system = (system or active_system()).upper()
    try:
        cfg = _cfg(system)
    except SapError as e:
        return {"system": system, "label": SYSTEM_LABELS.get(system, system),
                "configured": False, "error": str(e)}
    return {
        "system":     system,
        "label":      SYSTEM_LABELS.get(system, system),
        "configured": True,
        "host":       cfg["ashost"],
        "sysnr":      cfg["sysnr"],
        "client":     cfg["client"],
        "user":       cfg["user"],
    }


# ─────────────────────────────────────────────────────────────────────
# pyrfc plumbing
# ─────────────────────────────────────────────────────────────────────

_patched = False


def _pyrfc():
    """Import pyrfc lazily, and make it tolerate blank packed decimals.

    pyrfc 3.3.1 decodes an empty BCD field ('') with Decimal('') and
    fails the whole call while reading the response — observed on A4F
    for BAPI_PO_CREATE1 and BAPI_GOODSMVT_CREATE, whose returned tables
    leave some BCD fields blank. Treating blank as 0 loses nothing (we
    never read those fields). A subclass, not a function, so pyrfc's own
    isinstance checks keep working; values we SEND must therefore be
    built with dec() below, which uses the same patched class.
    """
    global _patched
    try:
        import pyrfc
        import pyrfc._cyrfc as cyrfc
    except ImportError as e:
        raise SapError(
            "pyrfc / SAP NW RFC SDK not available on this host "
            f"({e}). Install the SDK and `pip install pyrfc==3.3.1`."
        ) from e

    if not _patched:
        base = cyrfc.Decimal
        if not getattr(base, "_blank_tolerant", False):
            class _BlankTolerantDecimal(base):
                _blank_tolerant = True

                def __new__(cls, value="0", context=None):
                    if isinstance(value, str) and not value.strip():
                        value = "0"
                    return base.__new__(cls, value, context)

            cyrfc.Decimal = _BlankTolerantDecimal
        _patched = True
    return pyrfc


def dec(value) -> "object":
    """Build a decimal pyrfc accepts for a BCD (packed) parameter."""
    _pyrfc()
    import pyrfc._cyrfc as cyrfc
    if value is None or value == "":
        value = 0
    return cyrfc.Decimal(str(round(float(value), 6)) if isinstance(value, float) else str(value))


def sap_date(value) -> str:
    """Any date-ish value -> 'YYYYMMDD' (what RFC DATE8 fields take)."""
    if not value:
        return date.today().strftime("%Y%m%d")
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y%m%d")
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d.%m.%Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    return date.today().strftime("%Y%m%d")


def iso_date(raw) -> str:
    """SAP 'YYYYMMDD' (or a date object) -> 'YYYY-MM-DD'; '' for blank/0."""
    if isinstance(raw, (date, datetime)):
        return raw.strftime("%Y-%m-%d")
    s = str(raw or "").strip()
    if len(s) == 8 and s.isdigit() and s != "00000000":
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return ""


def num(raw) -> float:
    """RFC_READ_TABLE numeric text -> float. SAP prints negatives with a
    trailing minus ('12.50-'), and blanks mean 'no value'."""
    s = str(raw or "").strip().replace(",", "")
    if not s:
        return 0.0
    neg = s.endswith("-")
    s = s.rstrip("-")
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return -v if neg else v


def alpha(value: str, length: int = 10) -> str:
    """SAP ALPHA conversion: zero-pad purely numeric keys (vendor,
    document numbers); leave alphanumeric keys (e.g. USSU-VSF01) as-is."""
    s = str(value or "").strip().upper()
    return s.zfill(length) if s.isdigit() else s


def strip_alpha(value: str) -> str:
    s = str(value or "").strip()
    return (s.lstrip("0") or "0") if s.isdigit() else s


# ─────────────────────────────────────────────────────────────────────
# Connections — one live connection per (thread, system), reused while
# alive. pyrfc Connection objects are not thread-safe, and FastAPI runs
# sync endpoints on a thread pool, so connections are thread-local.
# ─────────────────────────────────────────────────────────────────────

_local = threading.local()


def connect(system: str = None):
    """Open a NEW connection (caller closes it)."""
    pyrfc = _pyrfc()
    cfg = _cfg(system)
    try:
        return pyrfc.Connection(**cfg)
    except pyrfc.LogonError as e:
        raise SapError(f"SAP logon failed for user {cfg['user']} "
                       f"(client {cfg['client']}): {e.message}") from e
    except pyrfc.CommunicationError as e:
        # The SDK message is a multi-line NI trace; its ERROR line says it all.
        lines = [l.strip() for l in e.message.splitlines() if l.strip().startswith("ERROR")]
        detail = lines[0][5:].strip() if lines else e.message.strip()[:200]
        raise SapError(f"Cannot reach SAP at {cfg['ashost']} "
                       f"(instance {cfg['sysnr']}): {detail}") from e


@contextmanager
def session(system: str = None):
    """
    Yield a live connection for `system`, reusing this thread's cached
    one when it is still alive. A dead/broken connection is dropped and
    reopened once. Use it for a whole unit of work (one PO lookup, one
    dashboard render) so the logon cost is paid at most once.
    """
    system = (system or active_system()).upper()
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}

    conn = cache.get(system)
    if conn is not None:
        try:
            if not conn.alive:
                conn = None
        except Exception:
            conn = None
    if conn is None:
        conn = connect(system)
        cache[system] = conn

    pyrfc = _pyrfc()
    try:
        yield conn
    except (pyrfc.CommunicationError, pyrfc.ExternalRuntimeError):
        # Drop a connection that broke mid-call so the next unit of work
        # starts clean; the error itself still propagates to the caller.
        cache.pop(system, None)
        try:
            conn.close()
        except Exception:
            pass
        raise


@contextmanager
def write_session(system: str = None):
    """
    A brand-new connection for ONE write unit of work (BAPI + commit),
    closed afterwards. Never reuse a session across postings: on A4F the
    second invoice / goods-movement BAPI in the same RFC session dumps with
    ITAB_DUPLICATE_KEY (MM/MRM buffers survive BAPI_TRANSACTION_COMMIT),
    so each write gets its own clean session.
    """
    conn = connect(system)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def close_all() -> None:
    for conn in (getattr(_local, "conns", None) or {}).values():
        try:
            conn.close()
        except Exception:
            pass
    _local.conns = {}


def is_s4(conn) -> bool:
    """S/4HANA (SAP_BASIS >= 7.50 with MATDOC/PRCD_ELEMENTS) vs ECC."""
    attrs = conn.get_connection_attributes()
    key = (attrs.get("sysId"), attrs.get("client"))
    flags = getattr(_local, "flavor", None)
    if flags is None:
        flags = _local.flavor = {}
    if key not in flags:
        try:
            read_table(conn, "PRCD_ELEMENTS", ["KNUMV"], max_rows=1)
            flags[key] = True
        except SapError:
            flags[key] = False
    return flags[key]


# ─────────────────────────────────────────────────────────────────────
# RFC_READ_TABLE helpers
# ─────────────────────────────────────────────────────────────────────

def _where_lines(conditions) -> list:
    """AND-join conditions and wrap into CHAR72 OPTIONS rows, breaking
    only on spaces so no literal is ever split across rows."""
    if not conditions:
        return []
    if isinstance(conditions, str):
        conditions = [conditions]
    text = " AND ".join(f"( {c} )" if " OR " in c.upper() else c
                        for c in conditions if c)
    lines, current = [], ""
    for token in text.split(" "):
        candidate = f"{current} {token}" if current else token
        if len(candidate) > _RFC_WHERE_LINE:
            lines.append({"TEXT": current})
            current = token
        else:
            current = candidate
    if current:
        lines.append({"TEXT": current})
    return lines


def quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def read_table(conn, table: str, fields: list, where=None,
               max_rows: int = 0) -> list:
    """
    Read `fields` from `table` (RFC_READ_TABLE) and return a list of
    dicts {FIELD: stripped string}. `where` is a condition string or a
    list of conditions (ANDed). An empty result is [], never an error.
    """
    pyrfc = _pyrfc()
    try:
        res = conn.call(
            "RFC_READ_TABLE",
            QUERY_TABLE=table,
            DELIMITER="|",
            FIELDS=[{"FIELDNAME": f} for f in fields],
            OPTIONS=_where_lines(where),
            ROWCOUNT=max_rows or 0,
        )
    except pyrfc.ABAPApplicationError as e:
        if e.key == "TABLE_WITHOUT_DATA":
            raise SapError(f"SAP table {table} cannot be read via RFC_READ_TABLE") from e
        if e.key == "FIELD_NOT_VALID":
            raise SapError(f"Field not valid for {table}: {fields}") from e
        if e.key == "NOT_AUTHORIZED":
            raise SapError(f"SAP user not authorized to read table {table}") from e
        raise SapError(f"RFC_READ_TABLE {table} failed: {e.key} {e.message}") from e
    except pyrfc.ABAPRuntimeError as e:
        raise SapError(f"RFC_READ_TABLE {table} failed: {e.message}") from e

    out = []
    for row in res["DATA"]:
        parts = row["WA"].split("|")
        out.append({f: (parts[i].strip() if i < len(parts) else "")
                    for i, f in enumerate(fields)})
    return out


def read_table_in(conn, table: str, fields: list, key_field: str,
                  values, where=None) -> list:
    """read_table with `key_field IN (values)`, batched so the WHERE
    clause stays small no matter how many keys are requested."""
    values = list(dict.fromkeys(v for v in values if v))
    rows = []
    for i in range(0, len(values), _IN_BATCH):
        batch = values[i:i + _IN_BATCH]
        cond = f"{key_field} IN ( {', '.join(quote(v) for v in batch)} )"  # spaces: WHERE rows wrap at 72 chars on token boundaries
        extra = [where] if isinstance(where, str) else list(where or [])
        rows.extend(read_table(conn, table, fields, [cond, *extra]))
    return rows


# ─────────────────────────────────────────────────────────────────────
# BAPI helpers
# ─────────────────────────────────────────────────────────────────────

def bapi_messages(ret) -> list:
    """Normalise a BAPIRET2 structure/table into [{type,id,number,message}]."""
    rows = ret if isinstance(ret, list) else [ret]
    return [
        {"type": r.get("TYPE", ""), "id": r.get("ID", ""),
         "number": r.get("NUMBER", ""), "message": r.get("MESSAGE", "")}
        for r in rows if r and r.get("TYPE")
    ]


def bapi_errors(ret) -> list:
    return [m["message"] for m in bapi_messages(ret) if m["type"] in ("E", "A", "X")]


def call(conn, function: str, **params) -> dict:
    """Call an RFC function module, mapping pyrfc errors to SapError."""
    pyrfc = _pyrfc()
    try:
        return conn.call(function, **params)
    except (pyrfc.ABAPApplicationError, pyrfc.ABAPRuntimeError) as e:
        raise SapError(f"{function} failed: {e.key} {e.message}") from e


def commit(conn) -> None:
    res = call(conn, "BAPI_TRANSACTION_COMMIT", WAIT="X")
    errors = bapi_errors(res.get("RETURN", {}))
    if errors:
        raise SapBapiError("BAPI_TRANSACTION_COMMIT failed: " + "; ".join(errors))


def rollback(conn) -> None:
    try:
        call(conn, "BAPI_TRANSACTION_ROLLBACK")
    except SapError:
        pass


def posting_date(conn, company_code: str) -> str:
    """
    Posting date ('YYYYMMDD') for goods movements / invoices in this
    company code: today when today's period is open in MM (MARV current
    or previous period), otherwise the last day of the current MM period.

    Observed on A4F: MM periods were left at 2026/04 (previous 2026/03),
    so a posting dated October fails with M7/053. Advancing periods
    (MMPV) is a system-wide close for every user of the client, so the
    connector posts into the open period instead and keeps the real date
    in the document date (BLDAT).
    """
    import calendar
    rows = read_table(conn, "MARV", ["LFGJA", "LFMON", "VMGJA", "VMMON"],
                      f"BUKRS = {quote(company_code)}", max_rows=1)
    today = date.today()
    if not rows:
        return today.strftime("%Y%m%d")
    r = rows[0]
    open_periods = {(int(r["LFGJA"]), int(r["LFMON"])), (int(r["VMGJA"]), int(r["VMMON"]))}
    if (today.year, today.month) in open_periods:
        return today.strftime("%Y%m%d")
    y, m = int(r["LFGJA"]), int(r["LFMON"])
    return date(y, m, calendar.monthrange(y, m)[1]).strftime("%Y%m%d")


# ─────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────

def ping(system: str = None) -> dict:
    """Open a fresh connection and report system info + round-trip time."""
    system = (system or active_system()).upper()
    info = public_config(system)
    if not info.get("configured"):
        return {**info, "ok": False}
    started = time.time()
    try:
        conn = connect(system)
        try:
            attrs = conn.get_connection_attributes()
            si = call(conn, "RFC_SYSTEM_INFO")["RFCSI_EXPORT"]
            return {
                **info,
                "ok":          True,
                "sysid":       attrs.get("sysId", ""),
                "partner_host": attrs.get("partnerHost", ""),
                "release":     si.get("RFCSAPRL", ""),
                "database":    si.get("RFCDBSYS", ""),
                "flavor":      "S/4HANA" if is_s4(conn) else "ECC",
                "latency_ms":  int((time.time() - started) * 1000),
            }
        finally:
            conn.close()
    except SapError as e:
        return {**info, "ok": False, "error": str(e),
                "latency_ms": int((time.time() - started) * 1000)}
