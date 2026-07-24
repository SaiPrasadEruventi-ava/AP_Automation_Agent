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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Header, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse

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
.shell { max-width: 1080px; margin: 0 auto; padding: 0 24px 80px; }
a { color: inherit; }
td.mono a { color: var(--accent-ink); text-decoration: none; border-bottom: 1px solid transparent; }
td.mono a:hover { border-bottom-color: var(--accent-ink); }

/* ── top bar ─────────────────────────────────────────────────── */
.topbar {
  border-bottom: 1px solid var(--border); background: var(--surface);
}
.topbar-inner {
  max-width: 1080px; margin: 0 auto; padding: 18px 24px;
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

@media (max-width: 720px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  .nav { display: none; }
}
"""

_GATES = [
    "PO Validity", "GRN Validation", "Quantity Reconciliation", "Price & UOM",
    "Line Amount", "Tax Reconciliation", "Discount & Freight",
    "Payment Terms", "Duplicate Check", "Final Reconciliation",
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


def _shell(title: str, active: str, body: str) -> str:
    nav_items = [
        ("/", "Overview"),
        ("/dashboard/upload", "Upload Invoice"),
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
<style>{_STYLE}</style>
</head>
<body>
  <div class="topbar"><div class="topbar-inner">
    <span class="mark">AP · 3WM</span>
    <nav class="nav">{nav_html}</nav>
  </div></div>
  <div class="shell">{body}</div>
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

    gates_html = "".join(
        f'<div class="gate"><span class="n">{i+1:02d}</span><span class="label">{_esc(g)}</span></div>'
        for i, g in enumerate(_GATES)
    )

    body = f"""
  <div class="hero">
    <p class="eyebrow">Accounts Payable · Automation</p>
    <h1>3-Way Match Invoice Processing Engine</h1>
    <p>Reads incoming vendor invoices, pulls the matching Purchase Order and Goods
    Receipt Note from the ERP, and reconciles quantities, pricing, tax, and payment
    terms automatically. Anything within tolerance clears straight through; anything
    that doesn't is routed to a human reviewer with the exact reason attached.</p>
  </div>

  <div class="gates">{gates_html}</div>

  <div class="stats">
    <a href="/dashboard/pending" class="stat {'warn' if pending else 'ok'}">
      <span class="go">→</span>
      <div><span class="dot"></span><span class="label">Pending Review</span></div>
      <div class="value">{len(pending)}</div>
    </a>
    <a href="/dashboard/processed" class="stat neutral">
      <span class="go">→</span>
      <div><span class="dot"></span><span class="label">Processed Invoices</span></div>
      <div class="value">{len(processed)}</div>
    </a>
    <a href="/dashboard/approved" class="stat ok">
      <span class="go">→</span>
      <div><span class="dot"></span><span class="label">Approved Invoices</span></div>
      <div class="value">{len(approved)}</div>
    </a>
    <a href="/dashboard/exceptions" class="stat {'warn' if exceptions else 'neutral'}">
      <span class="go">→</span>
      <div><span class="dot"></span><span class="label">Exceptions</span></div>
      <div class="value">{len(exceptions)}</div>
    </a>
  </div>

  <div class="panel">
    <div class="txt">
      <h2>Run processing now</h2>
      <p>Drains every email currently in the ToBeProcessed folder through the pipeline above.</p>
    </div>
    <div>
      <button id="runBtn" class="run" onclick="runProcess()">Run processing now</button>
      <div id="status" class="status"></div>
    </div>
  </div>
  <pre id="result" class="result"></pre>

<script>
  const TRIGGER_SECRET = {trigger_secret!r};

  async function runProcess() {{
    const btn = document.getElementById('runBtn');
    const statusEl = document.getElementById('status');
    const resultEl = document.getElementById('result');

    if (!TRIGGER_SECRET) {{
      statusEl.textContent = 'AP_TRIGGER_SECRET is not set on the server.';
      statusEl.className = 'status err';
      return;
    }}
    btn.disabled = true;
    statusEl.textContent = 'Running… this can take a little while.';
    statusEl.className = 'status';
    resultEl.style.display = 'none';
    try {{
      const res = await fetch('/api/process', {{ method: 'POST', headers: {{ 'X-Trigger-Secret': TRIGGER_SECRET }} }});
      const text = await res.text();
      let pretty = text;
      try {{ pretty = JSON.stringify(JSON.parse(text), null, 2); }} catch (e) {{}}
      if (res.ok) {{
        statusEl.textContent = 'Done — reloading…';
        statusEl.className = 'status ok';
        resultEl.textContent = pretty; resultEl.style.display = 'block';
        setTimeout(() => location.reload(), 1400);
      }} else {{
        statusEl.textContent = 'Request failed (HTTP ' + res.status + ').';
        statusEl.className = 'status err';
        resultEl.textContent = pretty; resultEl.style.display = 'block';
        btn.disabled = false;
      }}
    }} catch (e) {{
      statusEl.textContent = 'Network error: ' + e.message;
      statusEl.className = 'status err';
      btn.disabled = false;
    }}
  }}
</script>
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
        f"<td>{_esc(r.get('created_at'))}</td>"
        f"<td><a href=\"/review/{_esc(r.get('token',''))}\" target=\"_blank\">Open review →</a></td>"
        f"</tr>"
        for r in pending
    ) or '<tr><td colspan="5" class="empty">No invoices currently awaiting review.</td></tr>'
    return _table_page("Pending Review", "/dashboard/pending", "Overview", len(pending),
                        ["Invoice #", "Vendor", "PO #", "Submitted", ""], rows)


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
        f"<td>{_esc(p.get('processed_date'))}</td>"
        f"</tr>"
        for p in processed
    ) or '<tr><td colspan="6" class="empty">No invoices processed yet.</td></tr>'

    trigger_secret = os.getenv("AP_TRIGGER_SECRET", "")
    table_html = f"""
  <a class="back" href="/">← Overview</a>
  <div class="section-head"><h2>All Processed Invoices</h2><span class="count">{len(processed)} record(s)</span></div>
  <div class="card">
    <table><tr><th>Invoice #</th><th>Vendor</th><th>PO #</th><th>Amount</th><th>Status</th><th>Date</th></tr>{rows}</table>
  </div>

  <div class="section-head" style="margin-top:40px;"><h2>Testing Tools</h2></div>
  <div class="panel" style="border-color:#F0DAB5; background:#FFFDF8;">
    <div class="txt">
      <h2 style="color:var(--amber);">Reuse a test invoice</h2>
      <p>Removes one invoice from the processed/duplicate-check log so it can be
      resubmitted. Move the source email back into ToBeProcessed in Gmail first,
      then run this, then trigger processing from the Overview page.</p>
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
      <p>Wipes every invoice from the processed/duplicate-check log. Testing use only — cannot be undone.</p>
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
      if (res.ok) {{ statusEl.textContent = inv + ' removed — reloading…'; statusEl.className = 'status ok'; setTimeout(() => location.reload(), 900); }}
      else {{ statusEl.textContent = 'Failed (HTTP ' + res.status + ').'; statusEl.className = 'status err'; }}
    }} catch (e) {{ statusEl.textContent = 'Network error: ' + e.message; statusEl.className = 'status err'; }}
  }}

  async function clearAllProcessed() {{
    if (!confirm('This wipes the ENTIRE processed-invoice log. Continue?')) return;
    try {{
      const res = await fetch('/api/admin/clear-all-processed', {{
        method: 'POST', headers: {{ 'X-Trigger-Secret': TRIGGER_SECRET }}
      }});
      const data = await res.json();
      alert(res.ok ? ('Cleared ' + data.cleared + ' invoice(s).') : 'Failed.');
      location.reload();
    }} catch (e) {{ alert('Network error: ' + e.message); }}
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
        f"<td>{_esc(p.get('processed_date'))}</td>"
        f"</tr>"
        for p in approved
    ) or '<tr><td colspan="6" class="empty">No invoices approved yet.</td></tr>'
    return _table_page("Approved Invoices", "/dashboard/approved", "Overview", len(approved),
                        ["Invoice #", "Vendor", "PO #", "Amount", "Status", "Date"], rows)


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
    if (!confirm('This wipes the ENTIRE Exceptions log. Continue?')) return;
    try {{
      const res = await fetch('/api/admin/clear-all-exceptions', {{
        method: 'POST', headers: {{ 'X-Trigger-Secret': TRIGGER_SECRET }}
      }});
      const data = await res.json();
      alert(res.ok ? ('Cleared ' + data.cleared + ' record(s).') : 'Failed.');
      location.reload();
    }} catch (e) {{ alert('Network error: ' + e.message); }}
  }}
</script>
"""
    return _shell("Exceptions", "/dashboard/exceptions", body)


@app.get("/dashboard/erp", response_class=HTMLResponse)
async def dashboard_erp():
    import redis_store
    try:
        po_numbers = sorted(redis_store.erp_list_po_numbers())
        error = None
    except Exception as e:
        po_numbers = []
        error = str(e)

    po_header_rows, po_line_rows = [], []
    grn_header_rows, grn_line_rows = [], []

    if not error:
        for po_num in po_numbers:
            po_data = redis_store.erp_get_po(po_num)
            if po_data:
                po_header_rows.append((
                    po_data.get("po_number", po_num),
                    po_data.get("vendor_name", ""),
                    po_data.get("po_date", ""),
                    po_data.get("currency", ""),
                    po_data.get("po_amount", ""),
                ))
            for item in po_data.get("items", []):
                po_line_rows.append((
                    po_data.get("po_number", po_num),
                    po_data.get("vendor_name", ""),
                    item.get("item", ""),
                    item.get("quantity", ""),
                    item.get("unit_price", ""),
                ))

            grn_data = redis_store.erp_get_grn(po_num)
            if grn_data:
                grn_header_rows.append((
                    grn_data.get("grn_number", ""),
                    grn_data.get("po_number", po_num),
                    grn_data.get("vendor_name", ""),
                    grn_data.get("received_date", ""),
                    grn_data.get("received_by", ""),
                ))
            for item in grn_data.get("items", []):
                grn_line_rows.append((
                    grn_data.get("grn_number", ""),
                    grn_data.get("po_number", po_num),
                    item.get("item", ""),
                    item.get("quantity_ordered", ""),
                    item.get("quantity_received", ""),
                    item.get("quantity_accepted", ""),
                    item.get("condition", ""),
                ))

    def _cap(rows):
        return rows[:10], len(rows)

    po_header_top, po_header_total = _cap(po_header_rows)
    po_line_top, po_line_total = _cap(po_line_rows)
    grn_header_top, grn_header_total = _cap(grn_header_rows)
    grn_line_top, grn_line_total = _cap(grn_line_rows)

    def _err_row(colspan):
        return f'<tr><td colspan="{colspan}" class="empty">Could not reach Redis: {_esc(error)}</td></tr>'

    if error:
        po_header_html = _err_row(5)
        po_line_html   = _err_row(5)
        grn_header_html = _err_row(5)
        grn_line_html   = _err_row(7)
    else:
        po_header_html = "".join(
            f"<tr><td class='mono'>{_esc(a)}</td><td>{_esc(b)}</td><td>{_esc(c)}</td>"
            f"<td>{_esc(d)}</td><td class='amt'>{_esc(e)}</td></tr>"
            for a, b, c, d, e in po_header_top
        ) or '<tr><td colspan="5" class="empty">No PO headers loaded yet — run migrate_erp_to_redis.py.</td></tr>'

        po_line_html = "".join(
            f"<tr><td class='mono'>{_esc(a)}</td><td>{_esc(b)}</td><td>{_esc(c)}</td>"
            f"<td class='mono'>{_esc(d)}</td><td class='amt'>{_esc(e)}</td></tr>"
            for a, b, c, d, e in po_line_top
        ) or '<tr><td colspan="5" class="empty">No PO line items loaded yet — run migrate_erp_to_redis.py.</td></tr>'

        grn_header_html = "".join(
            f"<tr><td class='mono'>{_esc(a)}</td><td class='mono'>{_esc(b)}</td><td>{_esc(c)}</td>"
            f"<td>{_esc(d)}</td><td>{_esc(e)}</td></tr>"
            for a, b, c, d, e in grn_header_top
        ) or '<tr><td colspan="5" class="empty">No GRN headers loaded yet — run migrate_erp_to_redis.py.</td></tr>'

        grn_line_html = "".join(
            f"<tr><td class='mono'>{_esc(a)}</td><td class='mono'>{_esc(b)}</td><td>{_esc(c)}</td>"
            f"<td class='mono'>{_esc(d)}</td><td class='mono'>{_esc(e)}</td><td class='mono'>{_esc(f)}</td><td>{_esc(g)}</td></tr>"
            for a, b, c, d, e, f, g in grn_line_top
        ) or '<tr><td colspan="7" class="empty">No GRN line items loaded yet — run migrate_erp_to_redis.py.</td></tr>'

    readonly_note = ('Read-only sample — PO/GRN data is managed via '
                      '<code>migrate_erp_to_redis.py</code>, not editable here.')

    def _section(title, count_shown, count_total, headers, rows_html):
        thead = "".join(f"<th>{h}</th>" for h in headers)
        return f"""
  <div class="section-head"><h2>{_esc(title)}</h2>
    <span class="count">showing {count_shown} of {count_total}</span></div>
  <p style="font-size:13px; color:var(--muted); margin:-4px 0 14px;">{readonly_note}</p>
  <div class="card" style="margin-bottom:36px;">
    <table><tr>{thead}</tr>{rows_html}</table>
  </div>"""

    body = f"""
  <a class="back" href="/">← Overview</a>
{_section("PO Header", len(po_header_top), po_header_total,
           ["PO Number", "Vendor", "PO Date", "Currency", "PO Amount"], po_header_html)}
{_section("PO Line Items", len(po_line_top), po_line_total,
           ["PO Number", "Vendor", "Item", "Qty", "Unit Price"], po_line_html)}
{_section("GRN Header", len(grn_header_top), grn_header_total,
           ["GRN Number", "PO Number", "Vendor", "Received Date", "Received By"], grn_header_html)}
{_section("GRN Line Items", len(grn_line_top), grn_line_total,
           ["GRN Number", "PO Number", "Item", "Ordered", "Received", "Accepted", "Condition"], grn_line_html)}
"""
    return _shell("ERP PO/GRN Data Records", "/dashboard/erp", body)


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

    results = run_ap.process_all_emails(hitl_mode="web")

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

    import duplicate_checker
    duplicate_checker.remove_invoice(invoice_number)
    return {"removed": invoice_number}


@app.post("/api/admin/clear-all-processed")
async def clear_all_processed(x_trigger_secret: str = Header(default="")):
    """Danger: wipes the ENTIRE processed-invoice log. Testing use only."""
    expected = os.getenv("AP_TRIGGER_SECRET", "")
    if not expected or x_trigger_secret != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Trigger-Secret")

    import duplicate_checker
    count = len(duplicate_checker.get_all_processed())
    duplicate_checker.clear_log()
    return {"cleared": count}


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
    extracted = extract_all_documents(attachments)
    invoice_data = extracted.get("invoice_data", {})

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
    po_data, grn_data = lookup_po_and_grn(po_number)

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


@app.get("/dashboard/upload", response_class=HTMLResponse)
async def dashboard_upload():
    """
    Upload a test/real invoice PDF directly — no need to email it to the
    mailbox first. Runs through the exact same pipeline as a mailbox
    submission, ending up in Pending Review, Processed, Approved, or
    Exceptions exactly like any other invoice.
    """
    trigger_secret = os.getenv("AP_TRIGGER_SECRET", "")
    body = f"""
  <a class="back" href="/">← Overview</a>
  <div class="section-head"><h2>Upload Invoice</h2></div>
  <p style="font-size:13px; color:var(--muted); margin:-4px 0 18px;">
    Upload an invoice PDF directly to test the agent — it runs through the exact same
    pipeline as a mailbox submission (extraction, ERP lookup, 3-way match, notification).
    The result lands in Pending Review, Processed, Approved, or Exceptions, same as always.
  </p>

  <div class="card">
    <input type="file" id="invoiceFile" accept="application/pdf"
      style="display:block; margin-bottom:14px; font-size:13.5px;">
    <button id="uploadBtn" class="run" onclick="uploadInvoice()">Upload & Process</button>
    <div id="uploadStatus" class="status"></div>
  </div>
  <pre id="uploadResult" class="result"></pre>

<script>
  const TRIGGER_SECRET = {trigger_secret!r};

  async function uploadInvoice() {{
    const fileInput = document.getElementById('invoiceFile');
    const btn = document.getElementById('uploadBtn');
    const statusEl = document.getElementById('uploadStatus');
    const resultEl = document.getElementById('uploadResult');

    if (!fileInput.files.length) {{
      statusEl.textContent = 'Choose a PDF file first.';
      statusEl.className = 'status err';
      return;
    }}
    if (!TRIGGER_SECRET) {{
      statusEl.textContent = 'AP_TRIGGER_SECRET is not set on the server.';
      statusEl.className = 'status err';
      return;
    }}

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);

    btn.disabled = true;
    statusEl.textContent = 'Uploading & processing… this can take a little while.';
    statusEl.className = 'status';
    resultEl.style.display = 'none';

    try {{
      const res = await fetch('/api/upload-invoice', {{
        method: 'POST',
        headers: {{ 'X-Trigger-Secret': TRIGGER_SECRET }},
        body: formData,
      }});
      const text = await res.text();
      let pretty = text;
      try {{ pretty = JSON.stringify(JSON.parse(text), null, 2); }} catch (e) {{}}

      if (res.ok) {{
        statusEl.textContent = 'Done.';
        statusEl.className = 'status ok';
      }} else {{
        statusEl.textContent = 'Request failed (HTTP ' + res.status + ').';
        statusEl.className = 'status err';
      }}
      resultEl.textContent = pretty;
      resultEl.style.display = 'block';
    }} catch (e) {{
      statusEl.textContent = 'Network error: ' + e.message;
      statusEl.className = 'status err';
    }} finally {{
      btn.disabled = false;
    }}
  }}
</script>
"""
    return _shell("Upload Invoice", "/dashboard/upload", body)


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

    results = run_ap.process_all_emails(hitl_mode="web")

    import redis_store
    redis_store.last_run_set("Manual", len(results))

    return {
        "processed_count": len(results),
        "results": results,
    }
