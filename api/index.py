"""
api/index.py — Vercel serverless entry point
================================================
Vercel's Python runtime auto-detects an ASGI app named `app` in this
file and routes all HTTP traffic to it (see vercel.json — every path
is rewritten to /api/index).

This module:
  1. Re-exports the existing FastAPI app from approval_portal.py
     unchanged, so /review/{token}, /approve/{token}, /reject/{token},
     /clarify/{token}, /api/decide, /health all keep working exactly
     as before.
  2. Adds the enterprise dashboard UI (/, /dashboard/pending,
     /dashboard/processed, /dashboard/approved, /dashboard/erp).
  3. Adds POST /api/process — a manually-triggered endpoint that runs
     one pass of the AP pipeline (equivalent to running
     `python run_ap.py` once, without --watch).
  4. Adds GET /api/debug — a diagnostic endpoint confirming Redis
     connectivity without exposing secret values.

Manual trigger auth
--------------------
POST /api/process requires a shared secret so a stranger can't hit
your public URL and kick off mailbox processing. Set AP_TRIGGER_SECRET
in your Vercel project's environment variables. The dashboard's "Run
processing now" button has this baked in server-side so you never
have to paste it — see the docstring on dashboard() below for the
trade-off that comes with that convenience.
"""

import os
import sys
import html as _html
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles default to cp1252, which cannot print the emoji this
# app logs with — reconfigure once at the entry point instead.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from fastapi import Header, HTTPException, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse

from approval_portal import app  # the existing FastAPI app, unchanged
import run_ap


# ═══════════════════════════════════════════════════════════════════
# Design tokens & shared page shell
# ═══════════════════════════════════════════════════════════════════
# Palette: cool paper background, deep navy ink, ledger-teal for
# matched/approved states, amber for pending, crimson for exceptions.
# Data (invoice #s, amounts, statuses) renders in a monospace stack to
# read like a ledger; everything else uses a plain system sans.

_STYLE = """
:root {
  --ink:        #0B1220;
  --ink-soft:   #4B5568;
  --muted:      #8A93A6;
  --paper:      #F4F5F7;
  --surface:    #FFFFFF;
  --border:     #E3E6EB;
  --accent:     #0E6E64;   /* ledger teal — primary action, matched/approved */
  --accent-ink: #08403A;
  --amber:      #B45309;   /* pending */
  --amber-bg:   #FEF3E2;
  --crimson:    #B42318;   /* exception / rejected */
  --crimson-bg: #FDECEC;
  --slate-bg:   #EEF0F3;
  --mono: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
  --sans: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
* { box-sizing: border-box; }
body {
  margin: 0; font-family: var(--sans); background: var(--paper); color: var(--ink);
  -webkit-font-smoothing: antialiased;
}
.shell { max-width: 1240px; margin: 0 auto; padding: 0 24px 80px; }
a { color: inherit; }
td.mono a { color: var(--accent-ink); text-decoration: none; border-bottom: 1px solid transparent; }
td.mono a:hover { border-bottom-color: var(--accent-ink); }

/* ── top bar ─────────────────────────────────────────────────── */
.topbar {
  border-bottom: 1px solid var(--border); background: var(--surface);
}
.topbar-inner {
  max-width: 1240px; margin: 0 auto; padding: 14px 24px;
  display: flex; align-items: center; justify-content: space-between; gap: 24px;
}
.mark { font-family: var(--mono); font-size: 13px; font-weight: 600; letter-spacing: 0.02em;
  color: var(--accent-ink); background: var(--slate-bg); padding: 5px 9px; border-radius: 6px; }
.nav { display: flex; gap: 4px; font-size: 13px; }
.nav a { padding: 8px 12px; border-radius: 7px; text-decoration: none; color: var(--ink-soft); font-weight: 500; }
.nav a.active { background: var(--ink); color: white; }
.nav a:not(.active):hover { background: var(--slate-bg); }

/* ── hero ─────────────────────────────────────────────────────── */
.hero { padding: 44px 0 8px; }
.eyebrow { font-family: var(--mono); font-size: 11.5px; letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--muted); margin: 0 0 10px; }
.hero h1 { font-size: 28px; line-height: 1.2; margin: 0 0 14px; letter-spacing: -0.01em; }
.hero p { font-size: 15px; line-height: 1.6; color: var(--ink-soft); max-width: 720px; margin: 0; }

/* ── validation gate strip (real, ordered pipeline steps) ───────── */
.gates { display: flex; flex-wrap: wrap; gap: 8px; margin: 26px 0 40px; }
.gate {
  display: flex; align-items: center; gap: 7px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 999px; padding: 7px 13px 7px 9px; font-size: 12.5px;
}
.gate .n { font-family: var(--mono); font-size: 11px; color: var(--surface); background: var(--ink);
  border-radius: 999px; width: 18px; height: 18px; display: flex; align-items: center; justify-content: center; }
.gate .label { color: var(--ink-soft); font-weight: 500; }

/* ── stat cards ───────────────────────────────────────────────── */
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 28px; }
.stat {
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 18px 18px 16px; text-decoration: none; color: inherit; display: block;
  transition: border-color .15s, box-shadow .15s; position: relative;
}
.stat:hover { border-color: #C7CCD6; box-shadow: 0 2px 8px rgba(16,24,40,0.06); }
.stat .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 7px; }
.stat .label { font-size: 12px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
.stat .value { font-family: var(--mono); font-size: 30px; font-weight: 600; margin-top: 10px; letter-spacing: -0.02em; }
.stat .go { position: absolute; top: 16px; right: 16px; font-size: 12px; color: var(--muted); }
.stat.ok .dot { background: var(--accent); }
.stat.warn .dot { background: var(--amber); }
.stat.neutral .dot { background: var(--ink-soft); }

/* ── control panel ───────────────────────────────────────────── */
.mode-row { display: flex; gap: 10px; margin-bottom: 16px; }
.mode-btn {
  flex: 1; padding: 13px 18px; font-size: 14px; font-weight: 600; cursor: pointer;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  color: var(--ink-soft); transition: border-color .15s, color .15s, background .15s;
}
.mode-btn:hover { border-color: #C7CCD6; }
.mode-btn.active { background: var(--ink); color: white; border-color: var(--ink); }
.panel {
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 20px 22px; margin-bottom: 36px; display: flex; align-items: center; justify-content: space-between; gap: 20px; flex-wrap: wrap;
}
.panel .txt h2 { font-size: 15px; margin: 0 0 4px; }
.panel .txt p { font-size: 13px; color: var(--muted); margin: 0; }
button.run {
  padding: 11px 20px; font-size: 14px; font-weight: 600; background: var(--ink); color: white;
  border: none; border-radius: 8px; cursor: pointer; white-space: nowrap;
}
button.run:disabled { background: #B8BEC9; cursor: not-allowed; }
.status { font-size: 13px; margin-top: 10px; }
.status.ok { color: var(--accent-ink); }
.status.err { color: var(--crimson); }
.last-run-wrap { margin: -14px 0 22px; padding: 0 2px; }
.last-run { font-size: 13px; color: var(--ink-soft); display: flex; align-items: center; gap: 8px; }
.last-run .pill { text-transform: uppercase; }
pre.result {
  margin-top: 14px; background: var(--ink); color: #D7DEE8; padding: 14px 16px; border-radius: 8px;
  font-family: var(--mono); font-size: 12.5px; white-space: pre-wrap; word-break: break-word; display: none;
}

/* ── section / table ─────────────────────────────────────────── */
.section-head { display: flex; align-items: baseline; justify-content: space-between; margin: 0 0 12px; }
.section-head h2 { font-size: 16px; margin: 0; }
.section-head .count { font-family: var(--mono); font-size: 12.5px; color: var(--muted); }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 11px 16px; border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: none; }
th { font-size: 11px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
  background: var(--slate-bg); }
td.mono, td.amt { font-family: var(--mono); }
.pill { font-family: var(--mono); font-size: 11px; padding: 3px 8px; border-radius: 999px; font-weight: 600; white-space: nowrap; }
.pill.approved { background: #E4F3EF; color: var(--accent-ink); }
.pill.rejected { background: var(--crimson-bg); color: var(--crimson); }
.pill.pending  { background: var(--amber-bg); color: var(--amber); }
.pill.other    { background: var(--slate-bg); color: var(--ink-soft); }
.empty { color: var(--muted); font-style: italic; padding: 28px 16px; text-align: center; }
.back { font-size: 13px; color: var(--muted); text-decoration: none; display: inline-block; margin-bottom: 18px; }
.back:hover { color: var(--ink); }

/* ── confirm modal (replaces native browser confirm()) ─────────── */
.modal-overlay {
  display: none; position: fixed; inset: 0; background: rgba(11,18,32,0.45);
  z-index: 1000; align-items: center; justify-content: center;
}
.modal-overlay.show { display: flex; }
.modal-box {
  background: var(--surface); border-radius: 14px; padding: 26px 28px; max-width: 420px; width: 90%;
  box-shadow: 0 12px 40px rgba(11,18,32,0.25);
}
.modal-box h3 { font-size: 16px; margin: 0 0 10px; color: var(--ink); }
.modal-box p { font-size: 13.5px; color: var(--ink-soft); margin: 0 0 22px; line-height: 1.5; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; }
.modal-actions button {
  padding: 9px 18px; font-size: 13.5px; font-weight: 600; border-radius: 8px; cursor: pointer; border: none;
}
.modal-actions .cancel { background: var(--slate-bg); color: var(--ink-soft); }
.modal-actions .cancel:hover { background: #E3E6EB; }
.modal-actions .confirm { background: var(--crimson); color: white; }
.modal-actions .confirm:hover { background: #96190F; }

.sapbar { display:flex; flex-wrap:wrap; gap:10px 22px; align-items:center; background:var(--surface);
  border:1px solid var(--border); border-radius:12px; padding:14px 18px; margin:18px 0 26px; font-size:13px; }
.sapbar .k { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.05em; display:block; }
.sapbar .v { font-family:var(--mono); font-weight:600; }
.sapdot { width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:6px; background:var(--muted); }
.sapdot.ok { background:var(--accent); } .sapdot.err { background:var(--crimson); }
.erpfilter { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:0 0 22px; }
.erpfilter input, .erpfilter select { padding:9px 11px; border:1px solid var(--border); border-radius:7px;
  font-family:var(--mono); font-size:13px; background:var(--surface); }
.erpfilter button, .btn-sm { padding:8px 13px; border-radius:7px; border:1px solid var(--ink); background:var(--ink);
  color:#fff; font-size:13px; font-weight:600; cursor:pointer; }
.btn-sm.ghost { background:var(--surface); color:var(--ink); border-color:var(--border); }
.btn-sm:disabled { opacity:.5; cursor:wait; }
.tag { font-family:var(--mono); font-size:11px; padding:2px 7px; border-radius:5px; background:var(--slate-bg);
  color:var(--ink-soft); white-space:nowrap; }
.tag.ok { background:#E6F4F1; color:var(--accent-ink); } .tag.warn { background:var(--amber-bg); color:var(--amber); }
.tag.err { background:var(--crimson-bg); color:var(--crimson); }
.scroll { overflow-x:auto; }
td.num { text-align:right; font-family:var(--mono); }
.note { font-size:13px; color:var(--muted); margin:-4px 0 14px; }

/* ── polish: font, sticky bar, softer surfaces ───────────────────── */
body { font-family: "Inter", var(--sans); }
.topbar { position: sticky; top: 0; z-index: 50; background: rgba(255,255,255,.86);
  backdrop-filter: saturate(160%) blur(10px); -webkit-backdrop-filter: saturate(160%) blur(10px); }
.panel, .card, .stat, .sapbar { border-radius: 14px; box-shadow: 0 1px 2px rgba(16,24,40,.04); }
.stat { transition: border-color .15s, box-shadow .15s, transform .15s; }
.stat:hover { transform: translateY(-1px); box-shadow: 0 6px 18px rgba(16,24,40,.08); }
.mode-btn { border-radius: 12px; }
.mode-btn:not(.active):hover { background: var(--slate-bg); }
button.run, .btn-sm { transition: transform .08s, opacity .15s, background .15s; }
button.run:active, .btn-sm:active { transform: scale(.98); }
button.run:hover:not(:disabled) { opacity: .92; }
table tr:hover td { background: #FAFBFC; }
.hero h1 { font-weight: 700; letter-spacing: -0.02em; }

/* ── run report (timeline + checks) ────────────────────────────────── */
.rr { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; margin: 0 0 22px;
  box-shadow: 0 4px 18px rgba(16,24,40,.06); overflow: hidden; animation: rrIn .25s ease-out; }
@keyframes rrIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
.rr-head { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; padding: 16px 20px;
  border-bottom: 1px solid var(--border); }
.rr-head .t { font-weight: 700; font-size: 15px; }
.rr-head .m { font-family: var(--mono); font-size: 12.5px; color: var(--ink-soft); }
.rr-head .close { margin-left: auto; background: none; border: none; font-size: 18px; color: var(--muted); cursor: pointer; }
.badge { font-size: 12px; font-weight: 700; padding: 4px 11px; border-radius: 999px; white-space: nowrap; }
.badge.ok { background: #E4F3EF; color: var(--accent-ink); } .badge.warn { background: var(--amber-bg); color: var(--amber); }
.badge.fail { background: var(--crimson-bg); color: var(--crimson); } .badge.run { background: var(--slate-bg); color: var(--ink-soft); }
.rr-body { display: grid; grid-template-columns: 1.35fr 1fr; gap: 0; }
.rr-steps { padding: 16px 20px; border-right: 1px solid var(--border); }
.rr-side { padding: 16px 20px; }
.rr h4 { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 10px; }
.step { display: grid; grid-template-columns: 22px 1fr; gap: 10px; padding: 7px 0; position: relative; }
.step:not(:last-child)::after { content: ""; position: absolute; left: 10px; top: 28px; bottom: -6px; width: 2px; background: var(--border); }
.step .ic { width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700; color: #fff; background: var(--muted); z-index: 1; }
.step.ok .ic { background: var(--accent); } .step.warn .ic { background: var(--amber); } .step.fail .ic { background: var(--crimson); }
.step.info .ic { background: var(--ink-soft); } .step.pending .ic { background: var(--border); color: var(--muted); }
.step.active .ic { background: var(--ink); animation: pulse 1s infinite; }
@keyframes pulse { 50% { box-shadow: 0 0 0 5px rgba(11,18,32,.12); } }
.step .st { font-weight: 600; font-size: 13.5px; }
.step.pending .st { color: var(--muted); font-weight: 500; }
.step .sd { font-size: 12.5px; color: var(--ink-soft); margin-top: 2px; line-height: 1.45; }
.step .sap { display: inline-block; margin-top: 4px; font-family: var(--mono); font-size: 11.5px; background: #EAF2FF;
  color: #1D4ED8; padding: 2px 7px; border-radius: 5px; }
.chk { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.chk div { font-size: 12.5px; padding: 6px 9px; border-radius: 8px; background: #F1F8F6; color: var(--accent-ink); }
.chk div.f { background: var(--crimson-bg); color: var(--crimson); font-weight: 600; }
.rr-reasons { margin: 14px 0 0; padding: 10px 12px; background: var(--amber-bg); border-radius: 10px; font-size: 12.5px; }
.rr-reasons li { margin: 3px 0; }
.rr-next { margin-top: 14px; padding: 11px 13px; border-radius: 10px; background: var(--slate-bg); font-size: 13px; }
.rr-links { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.rr-links a { font-size: 12.5px; font-weight: 600; text-decoration: none; padding: 7px 11px; border-radius: 8px;
  border: 1px solid var(--border); color: var(--ink); background: var(--surface); }
.rr-links a.primary { background: var(--ink); color: #fff; border-color: var(--ink); }
.casest { font-family: var(--mono); font-size: 11px; padding: 3px 8px; border-radius: 6px; white-space: nowrap;
  background: var(--slate-bg); color: var(--ink-soft); display: inline-block; }
.casest.ok { background: #E4F3EF; color: var(--accent-ink); } .casest.warn { background: var(--amber-bg); color: var(--amber); }
.casest.fail { background: var(--crimson-bg); color: var(--crimson); }
tr.running td { background: #F7FAFF !important; }
.casest { white-space: normal; max-width: 260px; line-height: 1.4; font-family: "Inter", var(--sans); font-size: 12px; font-weight: 500; }
.casest.flash { box-shadow: 0 0 0 3px rgba(29,78,216,.25); transition: box-shadow .3s; }
.demo-table td { vertical-align: middle; }
.demo-table td.mono, .demo-table td.nw { white-space: nowrap; }
.demo-table td.scn { min-width: 300px; }
.demo-table td.scn div { font-size: 12px; color: var(--muted); margin-top: 2px; line-height: 1.4; }

@media (max-width: 900px) { .rr-body { grid-template-columns: 1fr; } .rr-steps { border-right: none; border-bottom: 1px solid var(--border); } }
@media (max-width: 720px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  .nav { display: none; }
}
"""

_GATES = [
    "PO Validity", "GRN Validation", "Invoice Date", "Vendor & Header",
    "Quantity Reconciliation", "Price & UOM", "Line & Total Arithmetic",
    "Tax Rate", "Discount & Freight", "Payment Terms", "Duplicate Check (AP + SAP)",
    "Post / Park in SAP",
]


def _esc(value) -> str:
    """HTML-escape anything before interpolating it — invoice data
    ultimately comes from emailed PDFs, which is untrusted input."""
    return _html.escape(str(value if value is not None else ""))


def _status_pill(status: str) -> str:
    s = (status or "").lower()
    if s in ("perfect match", "approved by reviewer"):
        cls = "approved"
    elif "rejected" in s:
        cls = "rejected"
    elif "pending" in s or "clarification" in s or "review" in s or "incomplete" in s or "error" in s:
        cls = "pending"
    else:
        cls = "other"
    return f'<span class="pill {cls}">{_esc(status)}</span>'


def _sap_tag(action: str, document: str = "") -> str:
    """Pill for what the agent did in SAP with an invoice."""
    if not action:
        return '<span class="tag">—</span>'
    cls = {"Posted": "ok", "Parked": "warn", "Failed": "err", "Deleted": "",
           "Reversed": "", "Skipped": "", "Simulated": ""}.get(action, "")
    return f'<span class="tag {cls}">{_esc(action)}{(" " + _esc(document)) if document else ""}</span>'


def _sap_status_tag(status: str) -> str:
    cls = {"Posted": "ok", "Reversed": "", "Parked": "warn", "Parked & Completed": "warn",
           "Open": "ok", "Partially Open": "ok", "Closed": "err", "Cancelled": "err",
           "On Hold": "warn", "Complete": "ok", "Partial": "warn"}.get(status, "")
    return f'<span class="tag {cls}">{_esc(status)}</span>'


def _erp_mode() -> str:
    import erp_mock
    return erp_mock.erp_source()


def _shell(title: str, active: str, body: str) -> str:
    nav_items = [
        ("/", "Overview"),
        ("/dashboard/pending", "Pending Review"),
        ("/dashboard/processed", "Processed"),
        ("/dashboard/approved", "Approved"),
        ("/dashboard/exceptions", "Exceptions"),
        ("/dashboard/erp", "ERP"),
    ]
    nav_html = "".join(
        f'<a href="{href}" class="{"active" if href == active else ""}">{label}</a>'
        for href, label in nav_items
    )
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{_STYLE}</style>
</head>
<body>
  <div class="topbar"><div class="topbar-inner">
    <span class="mark">AP · 3WM</span>
    <nav class="nav">{nav_html}</nav>
  </div></div>
  <div class="shell">{body}</div>

  <div id="confirmModalOverlay" class="modal-overlay">
    <div class="modal-box">
      <h3 id="confirmModalTitle">Are you sure?</h3>
      <p id="confirmModalMsg"></p>
      <div class="modal-actions">
        <button class="cancel" onclick="_closeConfirmModal(false)">Cancel</button>
        <button class="confirm" onclick="_closeConfirmModal(true)">Confirm</button>
      </div>
    </div>
  </div>
  <script>
    let _confirmModalCallback = null;
    function showConfirmModal(message, onConfirm, title) {{
      document.getElementById('confirmModalTitle').textContent = title || 'Are you sure?';
      document.getElementById('confirmModalMsg').textContent = message;
      document.getElementById('confirmModalOverlay').classList.add('show');
      _confirmModalCallback = onConfirm;
    }}
    function _closeConfirmModal(confirmed) {{
      document.getElementById('confirmModalOverlay').classList.remove('show');
      if (confirmed && _confirmModalCallback) _confirmModalCallback();
      _confirmModalCallback = null;
    }}
  </script>
</body>
</html>"""


def _table_page(title: str, active: str, back_label: str, count: int,
                 head_cells: list, rows_html: str) -> str:
    thead = "".join(f"<th>{h}</th>" for h in head_cells)
    body = f"""
  <a class="back" href="/">← Overview</a>
  <div class="section-head"><h2>{_esc(title)}</h2><span class="count">{count} record(s)</span></div>
  <div class="card">
    <table><tr>{thead}</tr>{rows_html}</table>
  </div>
"""
    return _shell(title, active, body)


# ═══════════════════════════════════════════════════════════════════
# Overview / dashboard
# ═══════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """
    Main dashboard: hero, the real validation-gate sequence the engine
    runs every invoice through, four clickable KPI cards (drilling into
    the pages below), and a one-click processing trigger.

    The trigger secret is rendered server-side into the page (read from
    AP_TRIGGER_SECRET) so nothing needs to be typed to use the button.
    Trade-off: anyone who can load this page can read the secret out of
    the page source and call /api/process directly with it — a
    reasonable trade for a private tool only you access, but don't post
    this URL somewhere public if that matters to you.
    """
    import pending_reviews
    import duplicate_checker
    import redis_store

    trigger_secret = os.getenv("AP_TRIGGER_SECRET", "")

    pending    = pending_reviews.list_pending_reviews()
    processed  = duplicate_checker.get_all_processed()
    approved   = [p for p in processed if p.get("status") in
                 ("Perfect Match", "Approved by Reviewer")]
    exceptions = redis_store.exception_get_all()
    demo_html  = _demo_panel_html()
    _RUN_JS = RUN_JS

    gates_html = "".join(
        f'<div class="gate"><span class="n">{i+1:02d}</span><span class="label">{_esc(g)}</span></div>'
        for i, g in enumerate(_GATES)
    )

    body = f"""
  <div class="hero">
    <p class="eyebrow">Accounts Payable · Automation</p>
    <h1>3-Way Match Invoice Processing Engine</h1>
    <p>Reads incoming vendor invoices, pulls the matching Purchase Order and Goods
    Receipt Note live from SAP, and reconciles quantities, pricing, tax, and payment
    terms automatically. Anything within tolerance is posted straight into SAP; anything
    that doesn't is parked in SAP and routed to a human reviewer with the exact reason attached.</p>
  </div>
  <div class="sapbar" id="sapbar"><span><span class="sapdot" id="sapdot"></span>
    <span id="saptext">Checking SAP connection…</span></span></div>

  <div class="gates">{gates_html}</div>

  <div class="stats">
    <a href="/dashboard/pending" class="stat {'warn' if pending else 'ok'}">
      <span class="go">→</span>
      <div><span class="dot"></span><span class="label">Pending Review</span></div>
      <div class="value" id="statPending">{len(pending)}</div>
    </a>
    <a href="/dashboard/processed" class="stat neutral">
      <span class="go">→</span>
      <div><span class="dot"></span><span class="label">Processed Invoices</span></div>
      <div class="value" id="statProcessed">{len(processed)}</div>
    </a>
    <a href="/dashboard/approved" class="stat ok">
      <span class="go">→</span>
      <div><span class="dot"></span><span class="label">Approved Invoices</span></div>
      <div class="value" id="statApproved">{len(approved)}</div>
    </a>
    <a href="/dashboard/exceptions" class="stat {'warn' if exceptions else 'neutral'}">
      <span class="go">→</span>
      <div><span class="dot"></span><span class="label">Exceptions</span></div>
      <div class="value" id="statExceptions">{len(exceptions)}</div>
    </a>
  </div>

  <div class="section-head" style="margin-bottom:10px;"><h2>Process Invoices</h2></div>
  <div class="mode-row">
    <button id="modeMailboxBtn" class="mode-btn active" onclick="showMode('mailbox')">📬 Process Mailbox</button>
    <button id="modeUploadBtn" class="mode-btn" onclick="showMode('upload')">📄 Upload Invoice Manually</button>
    <button id="modeDemoBtn" class="mode-btn" onclick="showMode('demo')">🧪 SAP Demo Cases</button>
  </div>

  <div id="mailboxPanel" class="panel">
    <div class="txt">
      <h2>Run processing now</h2>
      <p>Drains every email currently in the ToBeProcessed folder through the pipeline above.</p>
    </div>
    <div>
      <button id="runBtn" class="run" onclick="runProcess()">Run processing now</button>
      <div id="status" class="status"></div>
    </div>
  </div>
  <div id="result"></div>

  <div id="uploadPanel" class="panel" style="display:none;">
    <div class="txt" style="flex:1;">
      <h2>Upload Invoice Manually</h2>
      <p>Upload an invoice PDF directly — it runs through the exact same pipeline as a mailbox
      submission (extraction, ERP lookup, 3-way match, notification), ending up in Pending Review,
      Processed, Approved, or Exceptions, same as always.</p>
      <input type="file" id="invoiceFile" accept="application/pdf"
        style="display:block; margin:12px 0; font-size:13.5px;">
    </div>
    <div>
      <button id="uploadBtn" class="run" onclick="uploadInvoice()">Upload &amp; Process</button>
      <div id="uploadStatus" class="status"></div>
    </div>
  </div>
  <div id="uploadResult"></div>

  <div id="demoPanel" style="display:none;">
    {demo_html}
  </div>

<script>
  function showMode(mode) {{
    document.getElementById('mailboxPanel').style.display = mode === 'mailbox' ? 'flex' : 'none';
    document.getElementById('uploadPanel').style.display = mode === 'upload' ? 'flex' : 'none';
    document.getElementById('demoPanel').style.display = mode === 'demo' ? 'block' : 'none';
    document.getElementById('modeMailboxBtn').classList.toggle('active', mode === 'mailbox');
    document.getElementById('modeUploadBtn').classList.toggle('active', mode === 'upload');
    document.getElementById('modeDemoBtn').classList.toggle('active', mode === 'demo');
    try {{ localStorage.setItem('apMode', mode); }} catch (e) {{}}
  }}
  try {{ const m = localStorage.getItem('apMode'); if (m) showMode(m); }} catch (e) {{}}

  (async function sapHealth() {{
    const dot = document.getElementById('sapdot'), txt = document.getElementById('saptext');
    try {{
      const t = setTimeout(() => {{ if (txt.textContent.startsWith('Checking')) txt.textContent = 'Still connecting to SAP… (first logon can take a few seconds)'; }}, 6000);
      const r = await fetch('/api/sap/health');
      clearTimeout(t);
      const d = await r.json();
      if (d.erp_source !== 'sap') {{ txt.textContent = 'ERP source: mock data (AP_ERP_SOURCE=' + d.erp_source + ')'; return; }}
      if (d.ok) {{
        dot.className = 'sapdot ok';
        txt.innerHTML = '<b>Live SAP</b> &nbsp;' + d.label + ' · SID <b>' + d.sysid + '</b> · client ' + d.client +
          ' · user ' + d.user + ' · ' + d.flavor + ' ' + d.release + ' · ' + d.latency_ms + ' ms' +
          ' &nbsp;·&nbsp; invoice mode <b>' + d.invoice_mode + '</b>';
      }} else {{
        dot.className = 'sapdot err';
        txt.textContent = 'SAP not reachable — ' + (d.error || 'unknown error');
      }}
    }} catch (e) {{ dot.className = 'sapdot err'; txt.textContent = 'SAP health check failed: ' + e.message; }}
  }})();

  const TRIGGER_SECRET = {trigger_secret!r};
</script>
<script>{_RUN_JS}</script>
"""
    return _shell("AP · 3-Way Match Invoice Processing Engine", "/", body)


@app.get("/dashboard/pending", response_class=HTMLResponse)
async def dashboard_pending():
    import pending_reviews
    pending = sorted(pending_reviews.list_pending_reviews(),
                      key=lambda r: r.get("created_at", ""), reverse=True)
    rows = "".join(
        f"<tr>"
        f"<td class='mono'>{_esc(r.get('invoice_data', {}).get('invoice_number'))}</td>"
        f"<td>{_esc(r.get('invoice_data', {}).get('vendor_name'))}</td>"
        f"<td class='mono'>{_esc(r.get('invoice_data', {}).get('po_number') or r.get('po_data', {}).get('po_number'))}</td>"
        f"<td>{_sap_tag(((r.get('decision_info') or {}).get('sap_posting') or {}).get('action', ''), ((r.get('decision_info') or {}).get('sap_posting') or {}).get('document', ''))}</td>"
        f"<td style='max-width:300px;font-size:12.5px;'>{_esc(((r.get('decision_info') or {}).get('reasons') or [''])[0])}</td>"
        f"<td>{_esc(r.get('created_at'))}</td>"
        f"<td><a href=\"/review/{_esc(r.get('token',''))}\" target=\"_blank\">Open review →</a></td>"
        f"</tr>"
        for r in pending
    ) or '<tr><td colspan="7" class="empty">No invoices currently awaiting review.</td></tr>'
    return _table_page("Pending Review", "/dashboard/pending", "Overview", len(pending),
                        ["Invoice #", "Vendor", "PO #", "SAP", "Reason", "Submitted", ""], rows)


@app.get("/dashboard/processed", response_class=HTMLResponse)
async def dashboard_processed():
    import duplicate_checker
    processed = sorted(duplicate_checker.get_all_processed(),
                        key=lambda p: p.get("processed_date", ""), reverse=True)
    rows = "".join(
        f"<tr>"
        f"<td class='mono'><a href=\"/dashboard/invoice/{_esc(p.get('invoice_number'))}\">{_esc(p.get('invoice_number'))}</a></td>"
        f"<td>{_esc(p.get('vendor_name'))}</td>"
        f"<td class='mono'>{_esc(p.get('po_number'))}</td>"
        f"<td class='amt'>{_esc(p.get('currency'))} {_esc(p.get('amount'))}</td>"
        f"<td>{_status_pill(p.get('status'))}</td>"
        f"<td>{_sap_tag(p.get('sap_action', ''), p.get('sap_document', ''))}</td>"
        f"<td>{_esc(p.get('processed_date'))}</td>"
        f"</tr>"
        for p in processed
    ) or '<tr><td colspan="7" class="empty">No invoices processed yet.</td></tr>'

    trigger_secret = os.getenv("AP_TRIGGER_SECRET", "")
    table_html = f"""
  <a class="back" href="/">← Overview</a>
  <div class="section-head"><h2>All Processed Invoices</h2><span class="count">{len(processed)} record(s)</span></div>
  <div class="card">
    <table><tr><th>Invoice #</th><th>Vendor</th><th>PO #</th><th>Amount</th><th>Status</th><th>SAP</th><th>Date</th></tr>{rows}</table>
  </div>

  <div class="section-head" style="margin-top:40px;"><h2>Testing Tools</h2></div>
  <div class="panel" style="border-color:#F0DAB5; background:#FFFDF8;">
    <div class="txt">
      <h2 style="color:var(--amber);">Reuse a test invoice</h2>
      <p>Resets one invoice everywhere: <b>reverses the posted (or deletes the parked)
      supplier invoice in SAP</b> that this agent created, and removes it from the processed /
      duplicate-check log, exceptions and pending reviews — so the same invoice can be run again.
      For mailbox runs, move the source email back into ToBeProcessed first.</p>
      <div style="margin-top:10px; display:flex; gap:8px;">
        <input id="invNum" placeholder="Invoice number, e.g. INV-S10-001"
          style="padding:9px 11px; border:1px solid var(--border); border-radius:7px; font-family:var(--mono); font-size:13px; width:240px;">
        <button class="run" style="background:var(--amber);" onclick="clearInvoice()">Remove from log</button>
      </div>
      <div id="adminStatus" class="status"></div>
    </div>
  </div>
  <div class="panel" style="border-color:#F3C9C4; background:#FFFAF9;">
    <div class="txt">
      <h2 style="color:var(--crimson);">Clear entire processed log</h2>
      <p>Resets every logged invoice: reverses / deletes each one's SAP invoice created by this
      agent, then wipes the processed log, exceptions and pending reviews. Testing use only.</p>
    </div>
    <button class="run" style="background:var(--crimson);" onclick="clearAllProcessed()">Clear entire log</button>
  </div>

<script>
  const TRIGGER_SECRET = {trigger_secret!r};

  async function clearInvoice() {{
    const inv = document.getElementById('invNum').value.trim();
    const statusEl = document.getElementById('adminStatus');
    if (!inv) {{ statusEl.textContent = 'Enter an invoice number first.'; statusEl.className = 'status err'; return; }}
    statusEl.textContent = 'Removing…'; statusEl.className = 'status';
    try {{
      const res = await fetch('/api/admin/clear-invoice?invoice_number=' + encodeURIComponent(inv), {{
        method: 'POST', headers: {{ 'X-Trigger-Secret': TRIGGER_SECRET }}
      }});
      if (res.ok) {{
        const d = await res.json();
        const sapTxt = (d.sap || []).map(x => x.action + ' ' + (x.document || '') + (x.error ? ' (' + x.error + ')' : '')).join(', ');
        statusEl.textContent = inv + ' reset' + (sapTxt ? ' — SAP: ' + sapTxt : ' — no SAP document to undo') + ' — reloading…';
        statusEl.className = 'status ok'; setTimeout(() => location.reload(), 2200); }}
      else {{ statusEl.textContent = 'Failed (HTTP ' + res.status + ').'; statusEl.className = 'status err'; }}
    }} catch (e) {{ statusEl.textContent = 'Network error: ' + e.message; statusEl.className = 'status err'; }}
  }}

  async function clearAllProcessed() {{
    showConfirmModal(
      'This wipes the ENTIRE processed-invoice log. This cannot be undone.',
      async () => {{
        const statusEl = document.getElementById('adminStatus');
        statusEl.textContent = 'Clearing…'; statusEl.className = 'status';
        try {{
          const res = await fetch('/api/admin/clear-all-processed', {{
            method: 'POST', headers: {{ 'X-Trigger-Secret': TRIGGER_SECRET }}
          }});
          const data = await res.json();
          if (res.ok) {{
            statusEl.textContent = 'Cleared ' + data.cleared + ' invoice(s) — reloading…';
            statusEl.className = 'status ok';
            setTimeout(() => location.reload(), 900);
          }} else {{
            statusEl.textContent = 'Failed (HTTP ' + res.status + ').';
            statusEl.className = 'status err';
          }}
        }} catch (e) {{
          statusEl.textContent = 'Network error: ' + e.message;
          statusEl.className = 'status err';
        }}
      }},
      'Clear entire processed log?'
    );
  }}
</script>
"""
    return _shell("All Processed Invoices", "/dashboard/processed", table_html)


@app.get("/dashboard/approved", response_class=HTMLResponse)
async def dashboard_approved():
    import duplicate_checker
    processed = duplicate_checker.get_all_processed()
    approved = sorted(
        [p for p in processed if p.get("status") in ("Perfect Match", "Approved by Reviewer")],
        key=lambda p: p.get("processed_date", ""), reverse=True
    )
    rows = "".join(
        f"<tr>"
        f"<td class='mono'><a href=\"/dashboard/invoice/{_esc(p.get('invoice_number'))}\">{_esc(p.get('invoice_number'))}</a></td>"
        f"<td>{_esc(p.get('vendor_name'))}</td>"
        f"<td class='mono'>{_esc(p.get('po_number'))}</td>"
        f"<td class='amt'>{_esc(p.get('currency'))} {_esc(p.get('amount'))}</td>"
        f"<td>{_status_pill(p.get('status'))}</td>"
        f"<td>{_sap_tag(p.get('sap_action', ''), p.get('sap_document', ''))}</td>"
        f"<td>{_esc(p.get('processed_date'))}</td>"
        f"</tr>"
        for p in approved
    ) or '<tr><td colspan="7" class="empty">No invoices approved yet.</td></tr>'
    return _table_page("Approved Invoices", "/dashboard/approved", "Overview", len(approved),
                        ["Invoice #", "Vendor", "PO #", "Amount", "Status", "SAP", "Date"], rows)


@app.get("/dashboard/exceptions", response_class=HTMLResponse)
async def dashboard_exceptions():
    """
    Submissions that couldn't be processed at all — no readable invoice,
    PO not found in ERP, extraction failure, or an unexpected pipeline
    error. Separate from Processed/Approved since these aren't final
    dispositions: a corrected resend of the same invoice number should
    still be processable normally.
    """
    import redis_store
    exceptions = sorted(redis_store.exception_get_all(),
                         key=lambda e: e.get("recorded_at", ""), reverse=True)
    rows = "".join(
        f"<tr>"
        f"<td class='mono'><a href=\"/dashboard/invoice/{_esc(e.get('invoice_number'))}\">{_esc(e.get('invoice_number'))}</a></td>"
        f"<td>{_esc(e.get('vendor_name') or '—')}</td>"
        f"<td class='mono'>{_esc(e.get('po_number') or '—')}</td>"
        f"<td>{_esc(e.get('subject'))}</td>"
        f"<td>{_status_pill(e.get('final_status'))}</td>"
        f"<td style='max-width:280px;'>{_esc((e.get('reasons') or [''])[0])}</td>"
        f"<td>{_esc(e.get('recorded_at'))}</td>"
        f"<td><a href=\"#\" onclick=\"removeException('{_esc(e.get('key', e.get('invoice_number')))}'); return false;\" style=\"color:var(--crimson);\">Remove</a></td>"
        f"</tr>"
        for e in exceptions
    ) or '<tr><td colspan="8" class="empty">No exceptions — every submission has processed cleanly.</td></tr>'

    trigger_secret = os.getenv("AP_TRIGGER_SECRET", "")
    body = f"""
  <a class="back" href="/">← Overview</a>
  <div class="section-head"><h2>Exceptions</h2><span class="count">{len(exceptions)} record(s)</span></div>
  <p style="font-size:13px; color:var(--muted); margin:-4px 0 14px;">
    Submissions that couldn't complete processing — click an invoice number for full detail,
    including exactly what was extracted and why it stopped.
  </p>
  <div class="card" style="margin-bottom:24px;">
    <table>
      <tr><th>Invoice #</th><th>Vendor</th><th>PO #</th><th>Email Subject</th><th>Status</th><th>Reason</th><th>Recorded</th><th></th></tr>
      {rows}
    </table>
  </div>

  <div id="excStatus" class="status"></div>
  <div class="panel" style="border-color:#F3C9C4; background:#FFFAF9; margin-top:14px;">
    <div class="txt">
      <h2 style="color:var(--crimson);">Clear all exceptions</h2>
      <p>Wipes every record from the Exceptions log. Testing use only — cannot be undone.</p>
    </div>
    <button class="run" style="background:var(--crimson);" onclick="clearAllExceptions()">Clear all exceptions</button>
  </div>

<script>
  const TRIGGER_SECRET = {trigger_secret!r};

  async function removeException(key) {{
    const statusEl = document.getElementById('excStatus');
    statusEl.textContent = 'Removing…'; statusEl.className = 'status';
    try {{
      const res = await fetch('/api/admin/clear-exception?key=' + encodeURIComponent(key), {{
        method: 'POST', headers: {{ 'X-Trigger-Secret': TRIGGER_SECRET }}
      }});
      if (res.ok) {{ statusEl.textContent = 'Removed — reloading…'; statusEl.className = 'status ok'; setTimeout(() => location.reload(), 700); }}
      else {{ statusEl.textContent = 'Failed (HTTP ' + res.status + ').'; statusEl.className = 'status err'; }}
    }} catch (e) {{ statusEl.textContent = 'Network error: ' + e.message; statusEl.className = 'status err'; }}
  }}

  async function clearAllExceptions() {{
    showConfirmModal(
      'This wipes the ENTIRE Exceptions log. This cannot be undone.',
      async () => {{
        const statusEl = document.getElementById('excStatus');
        statusEl.textContent = 'Clearing…'; statusEl.className = 'status';
        try {{
          const res = await fetch('/api/admin/clear-all-exceptions', {{
            method: 'POST', headers: {{ 'X-Trigger-Secret': TRIGGER_SECRET }}
          }});
          const data = await res.json();
          if (res.ok) {{
            statusEl.textContent = 'Cleared ' + data.cleared + ' record(s) — reloading…';
            statusEl.className = 'status ok';
            setTimeout(() => location.reload(), 900);
          }} else {{
            statusEl.textContent = 'Failed (HTTP ' + res.status + ').';
            statusEl.className = 'status err';
          }}
        }} catch (e) {{
          statusEl.textContent = 'Network error: ' + e.message;
          statusEl.className = 'status err';
        }}
      }},
      'Clear entire Exceptions log?'
    );
  }}
</script>
"""
    return _shell("Exceptions", "/dashboard/exceptions", body)


@app.get("/dashboard/erp", response_class=HTMLResponse)
def dashboard_erp(po: str = "", view: str = "demo", system: str = ""):
    """
    ERP data straight from SAP — every load is a live RFC read (no copy in
    Redis): PO header, PO lines, GRN header, GRN lines and the supplier
    invoices SAP holds against each PO, read with the exact code the 3-way
    match uses. Shows the demo-case POs by default; `?po=` looks up any
    POs, `?view=recent` lists the latest POs in the demo company code.
    (A plain `def`, so FastAPI runs the blocking RFC calls on a worker
    thread instead of the event loop.)
    """
    if _erp_mode() != "sap":
        return _erp_mock_page()

    import erp_sap
    import sap_connector
    from demo_scenarios import ORG

    system = (system or "").upper() or None
    reg = _demo_registry()
    scen_by_po = {v.get("po_number"): k for k, v in reg.items() if v.get("po_number")}
    error = None
    if po.strip():
        po_numbers = [p.strip() for p in re.split(r"[,\s]+", po) if p.strip()]
        view = "search"
    elif view == "recent":
        try:
            po_numbers = erp_sap.recent_po_numbers(company_code=ORG["company_code"], system=system)
        except sap_connector.SapError as e:
            po_numbers, error = [], str(e)
    else:
        po_numbers = [v["po_number"] for _, v in sorted(reg.items()) if v.get("po_number")]

    snap = {"pos": [], "errors": [], "elapsed_ms": 0}
    if po_numbers and not error:
        try:
            snap = erp_sap.dashboard_snapshot(po_numbers, system)
        except sap_connector.SapError as e:
            error = str(e)
    cfg = sap_connector.public_config(system)

    def money(v, cur=""):
        try:
            return f"{cur + ' ' if cur else ''}{float(v):,.2f}"
        except (TypeError, ValueError):
            return _esc(v)

    def qty(v):
        try:
            f = float(v)
            return f"{f:,.0f}" if f == int(f) else f"{f:,.3f}"
        except (TypeError, ValueError):
            return _esc(v)

    po_h, po_l, gr_h, gr_l, inv_rows = [], [], [], [], []
    for entry in snap["pos"]:
        p, g, invs = entry["po"], entry["grn"], entry["invoices"]
        scen = scen_by_po.get(p["po_number"], "")
        scen_tag = f' <span class="tag">{scen}</span>' if scen else ""
        cur = p["currency"]
        po_h.append(
            f"<tr><td class='mono'>{_esc(p['po_number'])}{scen_tag}</td>"
            f"<td>{_esc(p['vendor_name'])}<div class='mono' style='font-size:11px;color:var(--muted)'>{_esc(p['vendor_code'])}</div></td>"
            f"<td>{_esc(p['po_date'])}</td><td>{_sap_status_tag(p['po_status'])}</td>"
            f"<td>{_esc(p['payment_terms'])}<div class='mono' style='font-size:11px;color:var(--muted)'>{_esc(p['payment_terms_code'])}</div></td>"
            f"<td style='font-size:12px'>{_esc(p['tax_type'])}</td>"
            f"<td class='num'>{money(p['subtotal'])}</td><td class='num'>{money(p['discount'])}</td>"
            f"<td class='num'>{money(p['tax'])}</td><td class='num'>{money(p['freight'])}</td>"
            f"<td class='num'><b>{money(p['po_amount'], cur)}</b></td></tr>")
        for i in p["items"]:
            flags = " ".join(t for t, on in (("deleted", i["deleted"]), ("delivered", i["delivery_completed"]),
                                             ("final inv.", i["final_invoice"])) if on)
            po_l.append(
                f"<tr><td class='mono'>{_esc(p['po_number'])}</td><td class='mono'>{_esc(i['po_item'])}</td>"
                f"<td>{_esc(i['item'])}</td><td class='num'>{qty(i['quantity'])}</td><td class='mono'>{_esc(i['uom'])}</td>"
                f"<td class='num'>{money(i['unit_price'])}</td><td class='num'>{money(i['line_amount'])}</td>"
                f"<td class='mono'>{_esc(i['tax_code'])} {i['tax_rate']:.2f}%</td>"
                f"<td class='num'>{qty(i['quantity_received'])}</td><td class='num'>{qty(i['quantity_invoiced'])}</td>"
                f"<td>{'<span class=tag>' + _esc(flags) + '</span>' if flags else ''}</td></tr>")
        if g:
            gr_h.append(
                f"<tr><td class='mono'>{_esc(g['grn_number'])}</td><td class='mono'>{_esc(p['po_number'])}{scen_tag}</td>"
                f"<td>{_esc(g['vendor_name'])}</td><td>{_esc(g['received_date'])}</td><td>{_esc(g.get('posting_date', ''))}</td>"
                f"<td class='mono'>{_esc(g['received_by'])}</td><td class='mono'>{_esc(g.get('delivery_note', ''))}</td>"
                f"<td>{_sap_status_tag(g['status'])}</td></tr>")
            for i in g["items"]:
                gr_l.append(
                    f"<tr><td class='mono'>{_esc(g['grn_number'])}</td><td class='mono'>{_esc(p['po_number'])}</td>"
                    f"<td>{_esc(i['item'])}</td><td class='num'>{qty(i['quantity_ordered'])}</td>"
                    f"<td class='num'>{qty(i['quantity_received'])}</td><td class='num'>{qty(i['quantity_accepted'])}</td>"
                    f"<td class='num'>{qty(i.get('quantity_returned', 0))}</td><td style='font-size:12.5px'>{_esc(i['condition'])}</td></tr>")
        else:
            gr_h.append(f"<tr><td class='mono' style='color:var(--muted)'>— none —</td><td class='mono'>{_esc(p['po_number'])}{scen_tag}</td>"
                        f"<td>{_esc(p['vendor_name'])}</td><td colspan='5' style='color:var(--amber)'>No goods receipt posted in SAP yet</td></tr>")
        for d in sorted(invs, key=lambda d: d["document"], reverse=True):
            inv_rows.append(
                f"<tr><td class='mono'>{_esc(d['document'])}/{_esc(d['fiscal_year'])}</td><td class='mono'>{_esc(p['po_number'])}</td>"
                f"<td class='mono'>{_esc(d['reference'])}</td><td class='num'>{money(d['gross_amount'], d['currency'])}</td>"
                f"<td class='num'>{money(d['tax_amount'])}</td><td>{_sap_status_tag(d['status'])}"
                f"{' <span class=tag>reversed by ' + _esc(d['reversed_by']) + '</span>' if d['reversed_by'] else ''}</td>"
                f"<td class='mono'>{_esc(d['payment_block']) or '—'}</td><td>{_esc(d['entered_on'])}</td>"
                f"<td class='mono'>{_esc(d['entered_by'])}</td><td style='font-size:12px'>{_esc(d['header_text'])}</td></tr>")

    def section(title, headers, rows, empty, note=""):
        thead = "".join(f"<th>{h}</th>" for h in headers)
        body_rows = "".join(rows) or f'<tr><td colspan="{len(headers)}" class="empty">{empty}</td></tr>'
        return (f'<div class="section-head"><h2>{_esc(title)}</h2><span class="count">{len(rows)} row(s)</span></div>'
                + (f'<p class="note">{note}</p>' if note else "")
                + f'<div class="card scroll" style="margin-bottom:34px;"><table><tr>{thead}</tr>{body_rows}</table></div>')

    if error:
        status_html = f'<span><span class="sapdot err"></span>SAP not reachable — {_esc(error)}</span>'
    else:
        status_html = (f'<span><span class="sapdot ok"></span><b>Live SAP</b></span>'
                       f'<span><span class="k">System</span><span class="v">{_esc(cfg.get("label"))}</span></span>'
                       f'<span><span class="k">Host</span><span class="v">{_esc(cfg.get("host"))} / {_esc(cfg.get("sysnr"))}</span></span>'
                       f'<span><span class="k">Client</span><span class="v">{_esc(cfg.get("client"))}</span></span>'
                       f'<span><span class="k">RFC user</span><span class="v">{_esc(cfg.get("user"))}</span></span>'
                       f'<span><span class="k">Read time</span><span class="v">{snap.get("elapsed_ms", 0)} ms</span></span>')
    systems = sap_connector.available_systems()
    sys_select = ""
    if len(systems) > 1:
        opts = "".join(f'<option value="{x}" {"selected" if (system or sap_connector.active_system()) == x else ""}>'
                       f'{_esc(sap_connector.SYSTEM_LABELS.get(x, x))}</option>' for x in systems)
        sys_select = f'<select name="system">{opts}</select>'
    errors_html = "".join(f'<p class="note" style="color:var(--crimson)">{_esc(e)}</p>' for e in snap.get("errors", []))

    body = f"""
  <a class="back" href="/">← Overview</a>
  <div class="section-head"><h2>ERP — live from SAP</h2></div>
  <div class="sapbar">{status_html}</div>
  <form class="erpfilter" method="get" action="/dashboard/erp">
    <input name="po" placeholder="PO number(s), e.g. 4500002217, 4500002226" value="{_esc(po)}" style="width:340px">
    {sys_select}
    <button type="submit">Look up in SAP</button>
    <a class="btn-sm ghost" href="/dashboard/erp" style="text-decoration:none">Demo cases</a>
    <a class="btn-sm ghost" href="/dashboard/erp?view=recent" style="text-decoration:none">Recent POs ({_esc(ORG['company_code'])})</a>
  </form>
  {errors_html}
  {section("PO Header", ["PO Number", "Vendor", "PO Date", "Status", "Payment Terms", "Tax", "Net", "Discount", "Tax Amt", "Freight", "PO Amount"],
           po_h, "No purchase orders to show.", "EKKO + LFA1 + T052 + pricing (PRCD_ELEMENTS) + tax condition records — exactly what validation uses.")}
  {section("PO Line Items", ["PO", "Item", "Description", "Qty", "UOM", "Unit Price", "Line Amount", "Tax", "Received", "Invoiced", "Flags"],
           po_l, "No PO lines.", "EKPO with goods-receipt and invoice quantities from the PO history (EKBE).")}
  {section("GRN Header", ["GRN (Material Doc)", "PO", "Vendor", "Received", "Posted (MM period)", "Received By", "Delivery Note", "Status"],
           gr_h, "No goods receipts.", "Goods receipts (mvt 101) from EKBE + BAPI_GOODSMVT_GETDETAIL.")}
  {section("GRN Line Items", ["GRN", "PO", "Item", "Ordered", "Received", "Accepted", "Returned", "Condition"],
           gr_l, "No GRN lines.", "Accepted = received − QC returns (mvt 122); condition = SAP item text / return reason.")}
  {section("Supplier Invoices in SAP", ["Invoice Doc", "PO", "Reference", "Gross", "Tax", "Status", "Pay. Block", "Entered", "By", "Header Text"],
           inv_rows, "No supplier invoices in SAP for these POs yet.", "RBKP / RSEG — what the agent posted, parked, reversed or deleted (MIRO documents).")}
"""
    return _shell("ERP — live SAP", "/dashboard/erp", body)


def _erp_mock_page() -> str:
    """The original Redis mock view, kept for AP_ERP_SOURCE=mock."""
    import redis_store
    try:
        po_numbers = sorted(redis_store.erp_list_po_numbers())
    except Exception as e:
        return _shell("ERP", "/dashboard/erp", f'<p class="empty">Could not reach Redis: {_esc(e)}</p>')
    rows = []
    for po_num in po_numbers:
        po_data = redis_store.erp_get_po(po_num)
        grn = redis_store.erp_get_grn(po_num)
        rows.append(f"<tr><td class='mono'>{_esc(po_num)}</td><td>{_esc(po_data.get('vendor_name'))}</td>"
                    f"<td class='amt'>{_esc(po_data.get('po_amount'))}</td><td class='mono'>{_esc(grn.get('grn_number', '—'))}</td></tr>")
    body = (f'<a class="back" href="/">← Overview</a><div class="section-head"><h2>ERP (mock data)</h2></div>'
            f'<p class="note">AP_ERP_SOURCE=mock — set AP_ERP_SOURCE=sap to read live from SAP.</p>'
            f'<div class="card"><table><tr><th>PO</th><th>Vendor</th><th>Amount</th><th>GRN</th></tr>{"".join(rows)}</table></div>')
    return _shell("ERP (mock)", "/dashboard/erp", body)


# ═══════════════════════════════════════════════════════════════════
# Diagnostics + manual trigger (API)
# ═══════════════════════════════════════════════════════════════════

@app.get("/dashboard/invoice/{invoice_number}", response_class=HTMLResponse)
async def dashboard_invoice_detail(invoice_number: str):
    """
    Shows the exact same rich HTML template used in the "AP Invoice
    Processing Result" email (checklist, line-item diff table, amount
    panel, reasons panel — via summary_sender.py's _build_full_email),
    regenerated from the persisted detail record and rendered inside
    an isolated iframe so its own stylesheet doesn't clash with the
    dashboard's.
    """
    import redis_store
    from summary_sender import _build_full_email

    detail = redis_store.invoice_detail_get(invoice_number)

    if not detail:
        body = f"""
  <a class="back" href="/">← Overview</a>
  <div class="section-head"><h2>Invoice {_esc(invoice_number)}</h2></div>
  <div class="card"><p class="empty" style="padding:0;">
    No detailed validation record found for this invoice — it may have been
    processed before this detail view was added, or the record has expired.
  </p></div>
"""
        return _shell(f"Invoice {invoice_number}", "", body)

    inv    = detail.get("invoice_data", {})
    po     = detail.get("po_data", {})
    grn    = detail.get("grn_data", {})
    match  = detail.get("matching", {})
    dec    = detail.get("decision_info", {})
    rev    = detail.get("reviewer_info", {}) or {}
    status = detail.get("final_status", "")
    threshold = detail.get("variance_threshold", 2.0)

    email_html = _build_full_email(
        inv, po, grn, match, status,
        detail.get("summary", ""), dec, rev,
        variance_threshold=threshold,
    )
    full_doc = f"<html><head><meta charset='utf-8'></head><body>{email_html}</body></html>"

    reviewer_html = ""
    if rev.get("name") or rev.get("comments"):
        reviewer_html = f"""
  <div class="section-head"><h2>Reviewer Decision</h2></div>
  <div class="card" style="margin-bottom:24px;">
    <p style="font-size:13.5px; margin:0 0 6px;"><b>Reviewer:</b> {_esc(rev.get('name', '—'))}</p>
    <p style="font-size:13.5px; margin:0;"><b>Comments:</b> {_esc(rev.get('comments', '—'))}</p>
  </div>"""

    body = f"""
  <a class="back" href="/">← Overview</a>
  <div class="section-head">
    <h2>Invoice {_esc(inv.get('invoice_number', invoice_number))}</h2>
    {_status_pill(status)}
  </div>
  <p style="font-size:13px; color:var(--muted); margin:-6px 0 18px;">Recorded {_esc(detail.get('recorded_at', '—'))}</p>
{reviewer_html}
  <div class="card" style="padding:0; overflow:hidden;">
    <iframe id="resultFrame" srcdoc="{_esc(full_doc)}"
      style="width:100%; border:none; display:block; min-height:600px;"
      onload="this.style.height = (this.contentWindow.document.body.scrollHeight + 40) + 'px';">
    </iframe>
  </div>
"""
    return _shell(f"Invoice {invoice_number}", "", body)


@app.get("/api/debug")
async def debug_redis(x_trigger_secret: str = Header(default="")):
    """
    Diagnostic-only endpoint: reports whether this deployment can see
    real Redis config and what it finds there — WITHOUT exposing any
    actual secret values.
    """
    expected = os.getenv("AP_TRIGGER_SECRET", "")
    if not expected or x_trigger_secret != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Trigger-Secret")

    import redis_store

    url_set   = bool(os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL"))
    token_set = bool(os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN"))

    try:
        po_numbers = redis_store.erp_list_po_numbers()
    except Exception as e:
        po_numbers = [f"ERROR: {e}"]

    try:
        gmail_token_present = bool(redis_store.gmail_token_get())
    except Exception as e:
        gmail_token_present = f"ERROR: {e}"

    return {
        "UPSTASH_REDIS_REST_URL_set":   url_set,
        "UPSTASH_REDIS_REST_TOKEN_set": token_set,
        "po_count_in_redis":            len(po_numbers) if isinstance(po_numbers, list) and not (po_numbers and str(po_numbers[0]).startswith("ERROR")) else po_numbers,
        "gmail_token_cached":           gmail_token_present,
    }


@app.get("/api/cron/process")
async def cron_process(authorization: str = Header(default="")):
    """
    Called automatically by Vercel Cron on the schedule set in
    vercel.json's "crons" array (currently every 5 minutes). Runs the
    exact same pipeline pass as POST /api/process.

    Secured differently from the manual endpoint: Vercel Cron makes a
    GET request and — as long as you've set a CRON_SECRET env var —
    automatically attaches "Authorization: Bearer <CRON_SECRET>" to it.
    That means only Vercel's own scheduler (which knows the secret
    because you told Vercel what it is) can trigger this path; nobody
    else calling this URL directly will have that header.

    Set CRON_SECRET in Vercel → Settings → Environment Variables to
    enable this. If it's unset, this endpoint refuses all requests
    (fail closed).
    """
    expected = os.getenv("CRON_SECRET", "")
    if not expected or authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    results = await run_in_threadpool(run_ap.process_all_emails, "web")

    import redis_store
    redis_store.last_run_set("Cron", len(results))

    return {
        "processed_count": len(results),
        "results": results,
    }


@app.post("/api/admin/clear-invoice")
async def clear_invoice(invoice_number: str, x_trigger_secret: str = Header(default="")):
    """
    Remove one invoice from the processed/duplicate-check log so the
    same test invoice can be resubmitted and reprocessed from scratch.
    Does NOT touch your actual Gmail mailbox — if the source email
    already moved out of ToBeProcessed, you still need to move/relabel
    it back there yourself before re-running /api/process.
    """
    expected = os.getenv("AP_TRIGGER_SECRET", "")
    if not expected or x_trigger_secret != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Trigger-Secret")

    import run_report
    out = await run_in_threadpool(run_ap.reset_invoice, invoice_number.strip())
    return {**out, "report": run_report.build_reset(out)}


@app.post("/api/admin/clear-all-processed")
async def clear_all_processed(x_trigger_secret: str = Header(default="")):
    """Danger: wipes the ENTIRE processed-invoice log. Testing use only."""
    expected = os.getenv("AP_TRIGGER_SECRET", "")
    if not expected or x_trigger_secret != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Trigger-Secret")

    return await run_in_threadpool(_reset_everything)


@app.post("/api/admin/clear-exception")
async def clear_exception(key: str, x_trigger_secret: str = Header(default="")):
    """Remove a single record from the Exceptions log."""
    expected = os.getenv("AP_TRIGGER_SECRET", "")
    if not expected or x_trigger_secret != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Trigger-Secret")

    import redis_store
    redis_store.exception_remove(key)
    return {"removed": key}


@app.post("/api/admin/clear-all-exceptions")
async def clear_all_exceptions(x_trigger_secret: str = Header(default="")):
    """Danger: wipes the ENTIRE Exceptions log. Testing use only."""
    expected = os.getenv("AP_TRIGGER_SECRET", "")
    if not expected or x_trigger_secret != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Trigger-Secret")

    import redis_store
    count = redis_store.exception_clear_all()
    return {"cleared": count}


@app.post("/api/upload-invoice")
async def upload_invoice(file: UploadFile = File(...), x_trigger_secret: str = Header(default="")):
    result = await _upload_invoice(file, x_trigger_secret)
    return await run_in_threadpool(_with_report, result, f"extracted from uploaded PDF {file.filename}")


async def _upload_invoice(file: UploadFile, x_trigger_secret: str):
    """
    Accepts a directly-uploaded invoice PDF and runs it through the
    IDENTICAL pipeline a real mailbox submission gets — same extraction,
    same ERP lookup, same 3-way match, same notification/persistence,
    same exception handling. The only difference is there's no source
    email to move afterward (email_ref=None), which process_invoice_set
    already supports — this is the same code path run_mock() uses for
    local testing.
    """
    expected = os.getenv("AP_TRIGGER_SECRET", "")
    if not expected or x_trigger_secret != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Trigger-Secret")

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    raw = await file.read()

    import io
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        text = ""
        print(f"  ⚠️  Could not extract text from uploaded PDF: {e}")

    subject = f"Manual Upload — {file.filename}"
    synthetic_email = {"subject": subject}

    if not text:
        result = run_ap._finalize_exception(
            synthetic_email, subject, "Incomplete — Missing Documents",
            [f"Could not extract any text from the uploaded file '{file.filename}'. "
             "It may be a scanned image with no selectable text, or corrupted."],
        )
        return result

    from document_extractor import extract_all_documents
    attachments = [{"doc_type": "Invoice", "extracted_text": text, "file_name": file.filename}]
    try:
        extracted = await run_in_threadpool(extract_all_documents, attachments)
    except Exception as e:
        return run_ap._finalize_exception(
            synthetic_email, subject, "Error — Extraction failure",
            [f"Invoice could not be read/extracted: {e}"])
    invoice_data = extracted.get("invoice_data", {})
    return await run_in_threadpool(_process_extracted, invoice_data, subject)


def _process_extracted(invoice_data: dict, subject: str) -> dict:
    """ERP lookup + full pipeline for an already-extracted invoice — the
    shared tail of a manual upload and a demo-case run."""
    synthetic_email = {"subject": subject}

    if not invoice_data or not invoice_data.get("invoice_number"):
        result = run_ap._finalize_exception(
            synthetic_email, subject, "Incomplete — Missing Documents",
            ["Invoice fields could not be extracted from the uploaded PDF. "
             "Please check the file and try again."],
            invoice_data=invoice_data,
        )
        return result

    from erp_mock import lookup_po_and_grn
    po_number = invoice_data.get("po_number", "")
    try:
        po_data, grn_data = lookup_po_and_grn(po_number)
    except Exception as e:  # SAP down / logon failure — record, don't 500
        return run_ap._finalize_exception(
            synthetic_email, subject, "Error — ERP unavailable",
            [f"Could not read PO '{po_number}' from the ERP: {e}"],
            invoice_data=invoice_data, po_number=po_number)

    if not po_data:
        result = run_ap._finalize_exception(
            synthetic_email, subject, "Incomplete — PO Not Found",
            [f"PO Number '{po_number}' not found in ERP system. Cannot proceed with "
             "3-way match. Please verify the PO number or confirm it has been "
             "created in the ERP."],
            invoice_data=invoice_data, po_number=po_number,
        )
        return result

    try:
        result = run_ap.process_invoice_set(
            invoice_data=invoice_data, po_data=po_data, grn_data=grn_data,
            email_ref=None, hitl_mode="web", supporting_docs=[],
        )
        result["subject"] = subject
        return result
    except Exception as e:
        return run_ap._finalize_exception(
            synthetic_email, subject, "Error — Pipeline failure",
            [f"Unexpected error while processing this invoice: {e}"],
            invoice_data=invoice_data, po_number=po_number,
        )


@app.post("/api/process")
async def trigger_processing(x_trigger_secret: str = Header(default="")):
    """
    Run one pass of the AP pipeline: drains every email currently in
    the ToBeProcessed folder, exactly like `python run_ap.py` (no
    --watch — this endpoint does one pass and returns).
    """
    expected = os.getenv("AP_TRIGGER_SECRET", "")
    if not expected or x_trigger_secret != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Trigger-Secret")

    results = await run_in_threadpool(run_ap.process_all_emails, "web")

    import redis_store
    redis_store.last_run_set("Manual", len(results))

    results = [await run_in_threadpool(_with_report, r, "from the mailbox") for r in results]
    return {
        "processed_count": len(results),
        "results": results,
    }



# ═══════════════════════════════════════════════════════════════════
# SAP: health, live lookups, demo cases, reset
# ═══════════════════════════════════════════════════════════════════

def _require_secret(x_trigger_secret: str) -> None:
    expected = os.getenv("AP_TRIGGER_SECRET", "")
    if not expected or x_trigger_secret != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Trigger-Secret")


@app.get("/api/sap/health")
def sap_health(system: str = None):
    """Live RFC ping of the configured SAP system (no secret values returned)."""
    import sap_connector
    import sap_invoice
    if _erp_mode() != "sap":
        return {"erp_source": _erp_mode(), "ok": False}
    return {**sap_connector.ping(system), "erp_source": "sap",
            "invoice_mode": sap_invoice.invoice_mode(),
            "systems": sap_connector.available_systems()}


@app.get("/api/sap/po/{po_number}")
def sap_po(po_number: str, system: str = None):
    """The exact PO + GRN data the 3-way match would use, live from SAP."""
    import erp_sap
    import sap_connector
    try:
        po, grn = erp_sap.lookup_po_and_grn(po_number, system)
        invoices = erp_sap.find_supplier_invoices(po_number=po_number, system=system) if po else []
    except sap_connector.SapError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not po:
        raise HTTPException(status_code=404, detail=f"PO {po_number} not found in SAP")
    return {"po": po, "grn": grn, "supplier_invoices": invoices}


def _demo_registry() -> dict:
    try:
        import sap_demo_setup
        return sap_demo_setup.load_registry().get("scenarios", {})
    except Exception:
        return {}


def _case_states() -> dict:
    """{invoice_number: (label, tone)} in plain language — where each
    invoice stands right now, in the AP engine AND in SAP."""
    import duplicate_checker
    import pending_reviews
    import redis_store

    def doc(sap):
        return f" {sap.get('document')}" if sap.get("document") else ""

    out = {}
    for e in redis_store.exception_get_all():
        why = (e.get("reasons") or [""])[0].lower()
        short = ("no goods receipt in SAP" if "grn" in why or "goods receipt" in why else
                 "PO not found in SAP" if "not found" in why else e.get("final_status", ""))
        out[e.get("invoice_number")] = (f"Exception — {short} · nothing created in SAP", "fail")

    for p in duplicate_checker.get_all_processed():
        st, act = p.get("status", ""), p.get("sap_action", "")
        d = f" {p.get('sap_document')}" if p.get("sap_document") else ""
        if act == "Posted" and st == "Perfect Match":
            label, tone = f"Posted in SAP{d}", "ok"
        elif act == "Posted":
            label, tone = f"Approved · posted in SAP{d}", "ok"
        elif act == "Deleted":
            label, tone = f"Rejected · parked doc{d} deleted in SAP", "fail"
        elif st == "Pending Clarification":
            label, tone = f"Clarification requested · still parked in SAP{d}", "warn"
        elif act == "Failed":
            label, tone = f"{st} · SAP posting failed", "fail"
        else:
            label, tone = st, "ok" if st in ("Perfect Match", "Approved by Reviewer") else "warn"
        out[p.get("invoice_number")] = (label, tone)

    for r in pending_reviews.list_pending_reviews():
        n = (r.get("invoice_data") or {}).get("invoice_number")
        sap = (r.get("decision_info") or {}).get("sap_posting") or {}
        dup = (r.get("dup_check") or {}).get("is_duplicate")
        if dup:
            prev = out.get(n, ("", ""))[0]
            if "uplicate" in prev:
                continue  # several resubmissions — one label is enough
            label = (f"{prev} · resubmission caught as DUPLICATE (waiting for review)" if prev
                     else "Duplicate — already in SAP · nothing new created · waiting for review")
            out[n] = (label, "warn")
        elif sap.get("action") == "Parked":
            out[n] = (f"Parked in SAP{doc(sap)} · waiting for review", "warn")
        elif sap.get("action") == "Failed":
            out[n] = ("SAP refused the invoice · waiting for review", "fail")
        else:
            out[n] = ("Waiting for review", "warn")
    return out


def _demo_panel_html() -> str:
    from demo_scenarios import SCENARIOS
    reg = _demo_registry()
    if not reg:
        return ('<div class="panel"><div class="txt"><h2>SAP demo cases</h2><p>No demo cases set up '
                'yet — run <code>python sap_demo_setup.py</code> on the server to create them in SAP.'
                '</p></div></div>')
    # No SAP call while rendering — the page must load instantly even when
    # SAP is slow or unreachable. SAP-side statuses ("already posted in SAP")
    # arrive a moment later through the /api/demo/status poll.
    states = _case_states()
    rows = ""
    for scn in SCENARIOS:
        e = reg.get(scn["id"], {})
        if not e.get("po_number"):
            continue
        inv = e.get("invoice_number", "")
        exp = scn["expected"] + (f" → then {scn['expected_second']} on resubmit" if scn.get("expected_second") else "")
        label, tone = states.get(inv, ("Not run yet", ""))
        done = "1" if inv in states else "0"
        rows += (
            f"<tr id='row-{scn['id']}'><td class='mono'><b>{scn['id']}</b></td>"
            f"<td class='scn'><b>{_esc(scn['title'])}</b><div>{_esc(scn.get('story', ''))}</div></td>"
            f"<td class='mono'><a href='/dashboard/erp?po={_esc(e['po_number'])}'>{_esc(e['po_number'])}</a></td>"
            f"<td class='mono'><a href='/demo/invoices/{_esc(inv)}.pdf' target='_blank'>{_esc(inv)}</a></td>"
            f"<td style='font-size:12.5px;min-width:130px'>{_esc(exp)}</td>"
            f"<td class='nw'><span class='casest {tone}' id='st-{scn['id']}' data-done='{done}'>{_esc(label)}</span></td>"
            f"<td style='white-space:nowrap'><button class='btn-sm' onclick=\"runCase('{scn['id']}','{_esc(inv)}',this)\">Run</button> "
            f"<button class='btn-sm ghost' onclick=\"resetCase('{scn['id']}','{_esc(inv)}',this)\">Reset</button></td></tr>"
        )
    return f"""
  <div class="panel" style="display:block;">
    <div class="txt">
      <h2>SAP demo cases — ready to run</h2>
      <p>Each case is a real purchase order + goods receipt in SAP with a matching vendor invoice PDF
      (click the invoice # to download it — upload it under <i>Upload Invoice Manually</i> or email it to
      the mailbox). <b>Run</b> processes the same invoice fields directly (no LLM needed) through the
      identical pipeline and shows every step it took. <b>Reset</b> reverses/deletes what the agent created
      in SAP and clears the logs so the case can be demoed again.</p>
    </div>
    <div id="demoResult" style="margin-top:16px;"></div>
    <div class="scroll" style="margin-top:6px;"><table class="demo-table">
      <tr><th>Case</th><th>Scenario</th><th>SAP PO</th><th>Invoice PDF</th><th>Expected</th><th>Current status</th><th></th></tr>
      {rows}
    </table></div>
    <div style="margin-top:16px; display:flex; gap:10px; align-items:center;">
      <button class="run" style="background:var(--crimson);" onclick="resetAll(this)">Reset all demo cases</button>
      <span style="font-size:12.5px;color:var(--muted)">Reverses posted / deletes parked SAP invoices created by the agent and clears every log.</span>
    </div>
  </div>"""


@app.get("/demo/invoices/{name}")
def demo_invoice_file(name: str):
    import sap_demo_setup
    if not re.fullmatch(r"INV-S\d{2}-\d{4}\.(pdf|json)", name):
        raise HTTPException(status_code=404, detail="Not found")
    path = sap_demo_setup.INVOICE_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(path), filename=name)


@app.post("/api/demo/run/{scenario_id}")
def demo_run(scenario_id: str, x_trigger_secret: str = Header(default="")):
    """Process a demo case's invoice through the full pipeline (same as
    an upload, with the fields as printed on its PDF — no LLM call)."""
    import json as _json
    import sap_demo_setup
    _require_secret(x_trigger_secret)
    entry = _demo_registry().get(scenario_id.upper())
    if not entry or not entry.get("invoice_number"):
        raise HTTPException(status_code=404, detail=f"Demo case {scenario_id} not set up")
    path = sap_demo_setup.INVOICE_DIR / f"{entry['invoice_number']}.json"
    invoice_data = _json.loads(path.read_text(encoding="utf-8"))["invoice_data"]
    result = _process_extracted(invoice_data, f"Demo case {scenario_id.upper()} — {entry['invoice_number']}")
    return _with_report(result, "fields as printed on the demo invoice PDF")


def _reset_everything() -> dict:
    """Reset every invoice the AP side knows about (processed log,
    exceptions, pending reviews) plus all demo-case invoices."""
    import duplicate_checker
    import pending_reviews
    import redis_store
    numbers = {p.get("invoice_number") for p in duplicate_checker.get_all_processed()}
    numbers |= {e.get("invoice_number") for e in redis_store.exception_get_all()}
    numbers |= {(r.get("invoice_data") or {}).get("invoice_number")
                for r in pending_reviews.list_pending_reviews()}
    numbers |= {e.get("invoice_number") for e in _demo_registry().values()}
    numbers.discard(None)
    numbers.discard("unknown")
    import run_report
    results = [run_ap.reset_invoice(n) for n in sorted(numbers)]
    duplicate_checker.clear_log()
    redis_store.exception_clear_all()
    steps = [s for r in results for s in run_report.build_reset(r)["steps"] if "SAP" in s["title"] and "Nothing" not in s["title"]]
    steps.append({"title": "All AP logs cleared", "status": "ok",
                  "detail": f"{len(results)} invoice(s) reset — processed log, exceptions and pending reviews are empty", "sap": ""})
    report = {"invoice_number": "All demo cases", "status": "Reset complete", "tone": "ok", "steps": steps,
              "checks": [], "reasons": [], "links": [], "sap": {},
              "next": "Every case can be run again. In SAP, MIR5 by reference shows each reversal."}
    return {"cleared": len(results), "results": results, "report": report}


@app.post("/api/admin/reset-demo")
async def reset_demo(x_trigger_secret: str = Header(default="")):
    _require_secret(x_trigger_secret)
    return await run_in_threadpool(_reset_everything)



def _with_report(result: dict, source: str = "") -> dict:
    import run_report
    try:
        return {**result, "report": run_report.build(result, source)}
    except Exception as e:  # never lose the result because the explanation failed
        print(f"  ⚠️  run report failed: {e}")
        return result


@app.get("/api/stats")
def stats():
    import duplicate_checker
    import pending_reviews
    import redis_store
    processed = duplicate_checker.get_all_processed()
    return {
        "pending":    len(pending_reviews.list_pending_reviews()),
        "processed":  len(processed),
        "approved":   len([p for p in processed if p.get("status") in ("Perfect Match", "Approved by Reviewer")]),
        "exceptions": len(redis_store.exception_get_all()),
    }


RUN_JS = r"""
const RUN_STEPS = ['Reading the invoice', 'Reading the purchase order live from SAP', 'Reading the goods receipt from SAP',
  'Duplicate check — AP log + SAP', 'Running the 3-way match and validations', 'Deciding', 'Posting / parking in SAP',
  'Recording the result'];
const ICON = {ok: '✓', warn: '!', fail: '✕', info: 'i', pending: '', active: ''};
function esc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function progressCard(el, title, steps) {
  let i = 0;
  const draw = () => {
    el.innerHTML = `<div class="rr"><div class="rr-head"><span class="badge run">Running</span>
      <span class="t">${esc(title)}</span><span class="m">live SAP calls — usually 5–15 s</span></div>
      <div class="rr-steps">${steps.map((s, k) => `<div class="step ${k < i ? 'ok' : k === i ? 'active' : 'pending'}">
        <span class="ic">${k < i ? '✓' : ''}</span><div><div class="st">${esc(s)}${k === i ? '…' : ''}</div></div></div>`).join('')}</div></div>`;
  };
  draw();
  const t = setInterval(() => { if (i < steps.length - 1) { i++; draw(); } }, 1100);
  el.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  return () => clearInterval(t);
}

function reportCard(rep) {
  const steps = (rep.steps || []).map(s => `<div class="step ${s.status}"><span class="ic">${ICON[s.status] || ''}</span>
    <div><div class="st">${esc(s.title)}</div>${s.detail ? `<div class="sd">${esc(s.detail)}</div>` : ''}
    ${s.sap ? `<span class="sap">SAP: ${esc(s.sap)}</span>` : ''}</div></div>`).join('');
  const checks = (rep.checks || []).length ? `<h4>Validations</h4><div class="chk">${rep.checks.map(c =>
    `<div class="${c.failed ? 'f' : ''}" title="${esc(c.desc)}">${c.failed ? '✕' : '✓'} ${esc(c.name)}</div>`).join('')}</div>` : '';
  const reasons = (rep.reasons || []).length ? `<ul class="rr-reasons">${rep.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : '';
  const links = (rep.links || []).length ? `<div class="rr-links">${rep.links.map(l =>
    `<a class="${l.primary ? 'primary' : ''}" href="${esc(l.href)}" target="${l.primary ? '_blank' : '_self'}">${esc(l.label)} →</a>`).join('')}</div>` : '';
  const side = checks + reasons + (rep.next ? `<div class="rr-next"><b>Next:</b> ${esc(rep.next)}</div>` : '') + links;
  return `<div class="rr"><div class="rr-head"><span class="badge ${rep.tone}">${esc(rep.status)}</span>
    <span class="t">${esc(rep.invoice_number)}</span>
    <span class="m">${esc([rep.vendor, rep.amount, rep.po_number ? 'PO ' + rep.po_number : ''].filter(Boolean).join(' · '))}</span>
    <button class="close" title="Close" onclick="this.closest('.rr').remove()">×</button></div>
    <div class="rr-body" style="${side ? '' : 'grid-template-columns:1fr'}"><div class="rr-steps"><h4>What the engine did</h4>${steps}</div>
    ${side ? `<div class="rr-side">${side}</div>` : ''}</div></div>`;
}

function errorCard(msg) {
  return `<div class="rr"><div class="rr-head"><span class="badge fail">Failed</span><span class="t">${esc(msg)}</span>
    <button class="close" onclick="this.closest('.rr').remove()">×</button></div></div>`;
}

async function postJSON(url, opts) {
  const res = await fetch(url, Object.assign({method: 'POST', headers: {'X-Trigger-Secret': TRIGGER_SECRET}}, opts || {}));
  let data = null; try { data = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error((data && data.detail) || ('HTTP ' + res.status));
  return data;
}

async function refreshStats() {
  try {
    const d = await (await fetch('/api/stats')).json();
    [['statPending', d.pending], ['statProcessed', d.processed], ['statApproved', d.approved], ['statExceptions', d.exceptions]]
      .forEach(([id, v]) => { const el = document.getElementById(id); if (el) el.textContent = v; });
  } catch (e) {}
}

async function refreshCaseStatus() {
  if (!document.getElementById('demoPanel')) return;
  try {
    const d = await (await fetch('/api/demo/status')).json();
    for (const [sid, st] of Object.entries(d)) {
      const el = document.getElementById('st-' + sid); if (!el) continue;
      if (el.textContent !== st.label) { el.textContent = st.label; el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 900); }
      el.className = 'casest ' + (st.tone || '') + (el.classList.contains('flash') ? ' flash' : '');
      el.dataset.done = st.done ? '1' : '0';
    }
  } catch (e) {}
}
function setCaseStatus() { refreshCaseStatus(); }
// Approvals / rejections happen in the review tab — keep this page current.
setInterval(() => { if (!document.hidden) { refreshCaseStatus(); refreshStats(); } }, 5000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { refreshCaseStatus(); refreshStats(); } });
refreshCaseStatus();  // first fill right after the page loads (the page itself renders without SAP calls)
refreshCaseStatus();  // first fill right after the page loads

function lockButtons(row, on) { if (row) row.querySelectorAll('button').forEach(b => b.disabled = on); }

async function runCase(sid, inv, btn) {
  const st = document.getElementById('st-' + sid);
  const go = async () => {
    const row = document.getElementById('row-' + sid), box = document.getElementById('demoResult');
    lockButtons(row, true); row && row.classList.add('running');
    const stop = progressCard(box, `${sid} · ${inv}`, RUN_STEPS);
    try {
      const d = await postJSON('/api/demo/run/' + sid);
      stop(); box.innerHTML = d.report ? reportCard(d.report) : errorCard('No report returned');
      if (d.report) setCaseStatus(sid, d.report);
    } catch (e) { stop(); box.innerHTML = errorCard(e.message); }
    lockButtons(row, false); row && row.classList.remove('running'); refreshStats();
  };
  if (st && st.dataset.done === '1' && sid !== 'S05')
    showConfirmModal(`${inv} was already processed (${st.textContent}). Running it again will be caught as a DUPLICATE. ` +
      `Click Reset first to demo the case from scratch — or confirm to run it anyway.`, go, 'Run again?');
  else go();
}

async function resetCase(sid, inv, btn) {
  const row = document.getElementById('row-' + sid), box = document.getElementById('demoResult');
  lockButtons(row, true);
  const stop = progressCard(box, `Reset ${sid} · ${inv}`, ['Finding this invoice in SAP (RBKP)', 'Reversing posted / deleting parked document', 'Clearing AP logs']);
  try { const d = await postJSON('/api/admin/clear-invoice?invoice_number=' + encodeURIComponent(inv));
        stop(); box.innerHTML = reportCard(d.report); setCaseStatus(sid, null, true); }
  catch (e) { stop(); box.innerHTML = errorCard(e.message); }
  lockButtons(row, false); refreshStats();
}

function resetAll(btn) {
  showConfirmModal('Reverse/delete every SAP invoice the agent created for the demo cases and clear all logs?', async () => {
    const box = document.getElementById('demoResult'); btn.disabled = true;
    const stop = progressCard(box, 'Reset all demo cases', ['Finding agent invoices in SAP', 'Reversing posted / deleting parked documents', 'Clearing AP logs']);
    try { const d = await postJSON('/api/admin/reset-demo'); stop(); box.innerHTML = reportCard(d.report);
          refreshCaseStatus(); }
    catch (e) { stop(); box.innerHTML = errorCard(e.message); }
    btn.disabled = false; refreshStats();
  }, 'Reset all demo cases?');
}

async function uploadInvoice() {
  const f = document.getElementById('invoiceFile'), box = document.getElementById('uploadResult'), btn = document.getElementById('uploadBtn');
  if (!f.files.length) { box.innerHTML = errorCard('Choose a PDF file first.'); return; }
  const fd = new FormData(); fd.append('file', f.files[0]); btn.disabled = true;
  const stop = progressCard(box, f.files[0].name, ['Extracting invoice fields from the PDF (AI)'].concat(RUN_STEPS.slice(1)));
  try { const d = await postJSON('/api/upload-invoice', {body: fd}); stop();
        box.innerHTML = d.report ? reportCard(d.report) : errorCard(d.final_status || 'No report returned'); }
  catch (e) { stop(); box.innerHTML = errorCard(e.message); }
  btn.disabled = false; refreshStats();
}

async function runProcess() {
  const box = document.getElementById('result'), btn = document.getElementById('runBtn'); btn.disabled = true;
  const stop = progressCard(box, 'Mailbox — ToBeProcessed folder', ['Reading the mailbox'].concat(RUN_STEPS));
  try { const d = await postJSON('/api/process'); stop();
        box.innerHTML = (d.results || []).length ? d.results.map(r => r.report ? reportCard(r.report) : errorCard(r.final_status)).join('')
          : `<div class="rr"><div class="rr-head"><span class="badge ok">Done</span><span class="t">No emails waiting in ToBeProcessed</span></div></div>`; }
  catch (e) { stop(); box.innerHTML = errorCard(e.message); }
  btn.disabled = false; refreshStats();
}
"""



@app.get("/api/demo/status")
def demo_status():
    """Plain-language status of every demo case (polled by the dashboard)."""
    states = _case_states()
    reg = _demo_registry()
    in_sap = _demo_invoices_in_sap([e.get("invoice_number") for e in reg.values()])
    out = {}
    for sid, e in reg.items():
        inv = e.get("invoice_number")
        if inv not in states and inv in in_sap:
            # SAP still holds an agent document the app no longer knows about
            # (e.g. server restarted) — say so, otherwise Run would surprise
            # everyone with a duplicate.
            d = in_sap[inv]
            states[inv] = (f"Already {d['status'].lower()} in SAP {d['document']} (earlier run) — "
                           "click Reset before demoing", "warn")
        label, tone = states.get(inv, ("Not run yet", ""))
        out[sid] = {"label": label, "tone": tone, "done": inv in states}
    return out


def _demo_invoices_in_sap(numbers: list) -> dict:
    """{invoice_number: live agent document} — one RBKP read for all cases."""
    if _erp_mode() != "sap":
        return {}
    try:
        import erp_sap
        import sap_connector
        with sap_connector.session() as conn:
            rows = sap_connector.read_table_in(conn, "RBKP", erp_sap._RBKP_FIELDS, "XBLNR",
                                               [n for n in numbers if n])
        docs = [d for d in erp_sap._rbkp_rows(rows)
                if (d["is_posted"] or d["is_parked"]) and d["header_text"].startswith("AP Agent")]
        return {d["reference"]: d for d in docs}
    except Exception as e:
        print(f"  ⚠️  demo status: SAP check skipped ({e})")
        return {}
