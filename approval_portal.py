"""
approval_portal.py — FastAPI HITL Approval Portal (ap_desktop_v2)
======================================================================
Gives the reviewer an actual clickable button experience: the review
request email contains links to this local web server. Clicking
"Open Review Page" shows invoice details plus a reviewer-name field,
a comments box, and three buttons (Approve / Reject / Request
Clarification). The one-click links in the email (/approve/{token}
etc.) skip straight to recording a decision with no name/comments.

Architecture
------------
This does NOT use a database or LangGraph. It works directly against
ap_desktop_v2's existing file-based design:

    pending_reviews.py        — persists/loads suspended invoices by token
    run_ap.resume_after_review() — finishes the pipeline once a decision
                                    arrives (summary, result email, log
                                    registration, mailbox move)

Start the server (from the ap_desktop_v2 directory):

    python -m uvicorn approval_portal:app --host 0.0.0.0 --port 8000 --reload

Or just:

    python approval_portal.py

The portal URL the emails point to is controlled by AP_APPROVAL_PORTAL_URL
in .env (defaults to http://localhost:8000) — set this to wherever the
server is actually reachable from the reviewer's browser.
"""

import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from pending_reviews import get_pending_review, is_pending, list_pending_reviews

app = FastAPI(
    title="AP Invoice Approval Portal",
    description="Human-in-the-Loop approval interface for the AP Automation Agent",
    version="1.0.0",
)

VALID_DECISIONS = {"Approve", "Reject", "Request Clarification"}


class ApprovalRequest(BaseModel):
    token:    str
    decision: str
    reviewer: str
    comments: Optional[str] = ""


def _get_pending_or_404(token: str) -> dict:
    record = get_pending_review(token)
    if not record:
        raise HTTPException(
            status_code=404,
            detail="This review link is invalid or has already been used."
        )
    if record.get("status") != "pending":
        raise HTTPException(
            status_code=409,
            detail="This invoice has already been reviewed."
        )
    return record


def _process_decision(token: str, decision: str,
                       reviewer: str = "AP Reviewer", comments: str = "") -> dict:
    """Shared handler for all decision endpoints — validates, then resumes the pipeline."""
    if decision not in VALID_DECISIONS:
        raise HTTPException(status_code=400, detail=f"Invalid decision: {decision}")

    _get_pending_or_404(token)  # 404/409 before doing any work

    # Import here (not at module load) so this module can be imported by
    # tests without requiring the full run_ap.py dependency chain to be
    # ready (e.g. Outlook/pywin32 not installed in a test environment).
    import run_ap
    result = run_ap.resume_after_review(token, decision, reviewer, comments)

    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])

    return result


# ── Web pages ──────────────────────────────────────────────────────────────

@app.get("/review/{token}", response_class=HTMLResponse)
async def review_page(token: str):
    """Full review page — invoice details, reviewer name, comments, 3 buttons."""
    record = _get_pending_or_404(token)

    invoice  = record["invoice_data"]
    po       = record["po_data"]
    matching = record["matching"]
    decision_info = record["decision_info"]

    inv_num   = invoice.get("invoice_number", "—")
    vendor    = invoice.get("vendor_name", "—")
    po_num    = po.get("po_number", "—")
    amount    = float(invoice.get("total_amount", 0) or 0)
    po_amount = float(po.get("po_amount", 0) or 0)
    var_pct   = matching.get("variance_percentage", 0) or 0
    reasons   = decision_info.get("reasons", [])

    reasons_html = "".join(
        f"<li style='margin-bottom:6px'>{r}</li>" for r in reasons
    ) or "<li>No specific reasons recorded.</li>"

    sap = decision_info.get("sap_posting") or {}
    if sap.get("action") == "Parked":
        sap_html = (f"<div class='sapbox'><strong>🏦 In SAP:</strong> this invoice is <b>parked</b> as document "
                    f"<code>{sap.get('document')}</code> / {sap.get('fiscal_year')} (check it: MIR4 → {sap.get('document')})."
                    f"<ul><li><b>Approve</b> → SAP <b>posts</b> document {sap.get('document')}</li>"
                    f"<li><b>Reject</b> → SAP <b>deletes</b> the parked document</li>"
                    f"<li><b>Request Clarification</b> → it stays parked in SAP</li></ul></div>")
    elif sap.get("action") == "Skipped":
        sap_html = ("<div class='sapbox'><strong>🏦 In SAP:</strong> nothing was created (duplicate). "
                    "Approving will <b>not</b> post it again.</div>")
    elif sap.get("action") == "Failed":
        sap_html = (f"<div class='sapbox'><strong>🏦 In SAP:</strong> SAP rejected the invoice — {sap.get('error', '')}. "
                    "Approving will try to post it once more.</div>")
    else:
        sap_html = ""

    return HTMLResponse(f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>AP Invoice Review — {inv_num}</title>
  <style>
    body{{font-family:Arial,Helvetica,sans-serif;max-width:680px;margin:40px auto;
         padding:0 20px;color:#2c3e50;background:#f4f6f8}}
    h1{{color:#2D4A7A;font-size:22px;border-bottom:2px solid #2D4A7A;padding-bottom:10px}}
    .card{{background:#fff;border:1px solid #e0e0e0;border-radius:10px;
          padding:22px;margin:18px 0;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
    table{{width:100%;border-collapse:collapse;font-size:14px}}
    td{{padding:7px 10px}} tr:nth-child(even){{background:#f8f9fa}}
    .lbl{{font-weight:600;color:#666;width:40%}}
    .reasons{{background:#fff8f0;border-left:4px solid #f39c12;border-radius:6px;
             padding:14px 18px;margin-top:10px}}
    .reasons ul{{margin:6px 0 0;padding-left:20px;font-size:13px;color:#555}}
    label{{font-weight:600;display:block;margin-bottom:6px;font-size:14px}}
    input[type=text]{{width:100%;padding:10px;border:1px solid #ccc;border-radius:6px;
                      font-size:14px;margin-bottom:16px;box-sizing:border-box}}
    textarea{{width:100%;padding:10px;border:1px solid #ccc;border-radius:6px;
             font-size:14px;resize:vertical;box-sizing:border-box;min-height:70px}}
    .btn-row{{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}}
    .btn{{flex:1;min-width:150px;padding:14px;border:none;border-radius:8px;color:#fff;
         font-size:14px;font-weight:700;cursor:pointer}}
    .approve{{background:#27AE60}} .reject{{background:#E74C3C}} .clarify{{background:#F39C12}}
    .btn:hover{{opacity:.9}} .btn:disabled{{opacity:.5;cursor:not-allowed}}
    .result{{display:none;text-align:center;padding:30px}}
    .err{{color:#c0392b;font-size:13px;margin-top:10px;text-align:center}}
    .sapbox{{background:#eef4ff;border-left:4px solid #1d4ed8;border-radius:6px;padding:12px 16px;margin-top:12px;font-size:13.5px}}
    .sapbox ul{{margin:6px 0 0;padding-left:20px}} code{{background:#e2e8f0;padding:1px 5px;border-radius:4px}}
    .busy{{text-align:center;color:#555;font-size:13px;margin-top:12px;display:none}}
  </style>
</head>
<body>
  <h1>📋 Invoice Review Required</h1>

  <div class="card">
    <table>
      <tr><td class="lbl">Invoice Number</td><td><strong>{inv_num}</strong></td></tr>
      <tr><td class="lbl">Vendor</td><td>{vendor}</td></tr>
      <tr><td class="lbl">PO Number</td><td>{po_num}</td></tr>
      <tr><td class="lbl">Invoice Amount</td><td>${amount:,.2f}</td></tr>
      <tr><td class="lbl">PO Amount</td><td>${po_amount:,.2f}</td></tr>
      <tr><td class="lbl">Variance</td><td>{var_pct:.2f}%</td></tr>
    </table>
    <div class="reasons">
      <strong>⚠️ Why this needs review:</strong>
      <ul>{reasons_html}</ul>
    </div>
    {sap_html}
  </div>

  <div class="card" id="decision-form">
    <label for="reviewer">Your name</label>
    <input type="text" id="reviewer" placeholder="Enter your full name">

    <label for="comments">Comments (optional)</label>
    <textarea id="comments" placeholder="Add any notes for the audit trail..."></textarea>

    <div class="btn-row">
      <button class="btn approve" onclick="decide('Approve')">✅ Approve</button>
      <button class="btn reject"  onclick="decide('Reject')">❌ Reject</button>
      <button class="btn clarify" onclick="decide('Request Clarification')">🔼 Request Clarification</button>
    </div>
    <div class="err" id="err"></div>
    <div class="busy" id="busy">⏳ Recording your decision and updating SAP…</div>
  </div>

  <div class="card result" id="done">
    <h2 id="done-title" style="margin-top:0"></h2>
    <p id="done-body" style="color:#555"></p>
    <p id="done-sap" class="sapbox" style="display:none;text-align:left"></p>
    <p><a href="/dashboard/pending" style="color:#1d4ed8">← Back to the dashboard</a></p>
  </div>

  <script>
    async function decide(decision) {{
      const reviewer = document.getElementById('reviewer').value.trim();
      const comments = document.getElementById('comments').value.trim();
      const errEl = document.getElementById('err');
      if (!reviewer) {{
        errEl.textContent = 'Please enter your name before submitting a decision.';
        return;
      }}
      errEl.textContent = '';
      document.querySelectorAll('.btn').forEach(b => b.disabled = true);
      document.getElementById('busy').style.display = 'block';

      try {{
        const res = await fetch('/api/decide', {{
          method: 'POST',
          headers: {{'Content-Type':'application/json'}},
          body: JSON.stringify({{token:'{token}', decision, reviewer, comments}})
        }});
        const data = await res.json();
        if (res.ok) {{
          const titles = {{
            'Approve': '✅ Invoice Approved',
            'Reject': '❌ Invoice Rejected',
            'Request Clarification': '🔼 Clarification Requested'
          }};
          document.getElementById('decision-form').style.display = 'none';
          document.getElementById('done-title').textContent = titles[decision] || 'Decision Recorded';
          document.getElementById('done-body').textContent =
            `Invoice ${{data.invoice_number || ''}} — recorded by ${{reviewer}}.`;
          const sap = data.sap_posting || {{}};
          const sapMsg = {{
            'Posted':  `SAP: invoice document ${{sap.document}} is now POSTED` + (sap.payment_block ? ` (payment block ${{sap.payment_block}})` : '') + ` — verify in MIR4 → ${{sap.document}}.`,
            'Deleted': `SAP: parked document ${{sap.document}} has been DELETED — it no longer appears in MIR6.`,
            'Parked':  `SAP: document ${{sap.document}} stays PARKED until clarified.`,
            'Skipped': `SAP: nothing posted — ${{(sap.messages || []).join(' ')}}`,
            'Failed':  `SAP could not complete the action: ${{sap.error}} — the document stays as it was; finish it in SAP (MIR4).`,
          }}[sap.action];
          if (sapMsg) {{ const el = document.getElementById('done-sap'); el.textContent = sapMsg; el.style.display = 'block'; }}
          document.getElementById('done').style.display = 'block';
        }} else {{
          document.getElementById('busy').style.display = 'none';
          errEl.textContent = data.detail || 'Error submitting decision. Please try again.';
          document.querySelectorAll('.btn').forEach(b => b.disabled = false);
        }}
      }} catch (e) {{
        errEl.textContent = 'Network error — please try again.';
        document.querySelectorAll('.btn').forEach(b => b.disabled = false);
      }}
    }}
  </script>
</body>
</html>""")


# ── API ──────────────────────────────────────────────────────────────────

@app.post("/api/decide")
def api_decide(request: ApprovalRequest):  # sync: resuming posts to SAP (blocking RFC)
    """JSON endpoint used by the review page's form submission."""
    result = _process_decision(
        request.token, request.decision,
        request.reviewer or "AP Reviewer", request.comments or ""
    )
    return {
        "status":         "success",
        "invoice_number": result.get("invoice_number"),
        "final_status":   result.get("final_status"),
        "sap_posting":    result.get("sap_posting") or {},
    }


@app.get("/approve/{token}")
def quick_approve(token: str, name: str = "AP Reviewer"):
    """One-click approve link from the email — no comments, default reviewer name."""
    result = _process_decision(token, "Approve", name)
    return _one_click_result_page("✅ Invoice Approved", "#27AE60", result)


@app.get("/reject/{token}")
def quick_reject(token: str, name: str = "AP Reviewer"):
    """One-click reject link from the email."""
    result = _process_decision(token, "Reject", name)
    return _one_click_result_page("❌ Invoice Rejected", "#E74C3C", result)


@app.get("/clarify/{token}")
def quick_clarify(token: str, name: str = "AP Reviewer"):
    """One-click clarification-request link from the email."""
    result = _process_decision(token, "Request Clarification", name)
    return _one_click_result_page("🔼 Clarification Requested", "#F39C12", result)


def _sap_line(sap: dict) -> str:
    a, d = sap.get("action"), sap.get("document", "")
    return {"Posted": f"SAP: invoice {d} posted (MIR4 → {d}).",
            "Deleted": f"SAP: parked invoice {d} deleted.",
            "Failed": f"SAP could not complete: {sap.get('error', '')}"}.get(a, "")


def _one_click_result_page(title: str, colour: str, result: dict) -> HTMLResponse:
    inv_num = result.get("invoice_number", "—")
    return HTMLResponse(f"""
<html><body style="font-family:Arial,sans-serif;max-width:480px;margin:80px auto;
text-align:center;color:#2c3e50">
  <h2 style="color:{colour}">{title}</h2>
  <p>Invoice <strong>{inv_num}</strong> has been recorded.</p>
  <p>{_sap_line(result.get("sap_posting") or {})}</p>
  <p style="color:#999;font-size:13px">You may close this window.</p>
</body></html>""")


# ── Diagnostics ────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    from datetime import datetime
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/pending-reviews")
async def pending_reviews():
    """List all invoices currently awaiting a web-portal decision."""
    portal_url = os.getenv("AP_APPROVAL_PORTAL_URL", "http://localhost:8000")
    records = list_pending_reviews()
    return [
        {
            "token":          r["token"][:12] + "...",
            "invoice_number": r["invoice_data"].get("invoice_number"),
            "vendor_name":    r["invoice_data"].get("vendor_name"),
            "created_at":     r.get("created_at"),
            "review_url":     f"{portal_url}/review/{r['token']}",
        }
        for r in records
    ]


if __name__ == "__main__":
    import uvicorn
    port       = int(os.getenv("AP_APPROVAL_PORTAL_PORT", "8000"))
    portal_url = os.getenv("AP_APPROVAL_PORTAL_URL", "http://localhost:8000")

    print("\n" + "="*65)
    print("  🌐 AP APPROVAL PORTAL")
    print("="*65)
    print(f"  Binding to    : http://0.0.0.0:{port}  (reachable on any "
          f"network interface)")
    print(f"  Links in email point to : {portal_url}")
    print(f"  Pending reviews list    : http://localhost:{port}/pending-reviews")

    try:
        from urllib.parse import urlparse
        configured_port = urlparse(portal_url).port or 80
        if configured_port != port:
            print(f"\n  ⚠️  MISMATCH: AP_APPROVAL_PORTAL_URL uses port "
                  f"{configured_port} but this server is binding to port "
                  f"{port}. Email links will fail to connect. Fix "
                  f"AP_APPROVAL_PORTAL_URL or AP_APPROVAL_PORTAL_PORT in .env "
                  f"so they agree.")
    except Exception:
        pass

    if "localhost" in portal_url or "127.0.0.1" in portal_url:
        print(f"\n  ℹ️  AP_APPROVAL_PORTAL_URL uses 'localhost' — links will "
              f"ONLY work if the reviewer opens the email on THIS machine. "
              f"If your reviewer uses a different PC, set "
              f"AP_APPROVAL_PORTAL_URL in .env to this machine's real "
              f"network address, e.g. http://<this-machine's-IP>:{port}")

    print("="*65)
    uvicorn.run(app, host="0.0.0.0", port=port)
