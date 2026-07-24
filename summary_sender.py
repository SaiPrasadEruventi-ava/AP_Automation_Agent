"""
summary_sender.py — Sophisticated AP Result Email Generator v3
==============================================================
Produces rich, decision-specific HTML emails that tell the full story
of every invoice processing run so the recipient can act immediately
without opening any source documents.

Design principles:
  • Auto-Approved  → green, full checklist, all 9 validations ticked ✅
  • Manual Review  → amber, scenario banner, per-field diff table,
                     exact mismatch values side-by-side
  • Rejected       → red, same diff table, reviewer comments prominent
  • Duplicate      → purple, original vs duplicate comparison
  • Missing docs   → slate, checklist of what arrived vs what's needed
  • Reviewer HITL  → orange request with same diff detail so reviewer
                     can decide without opening Outlook
"""

import os
import re
import json
from pathlib import Path
from datetime import datetime
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mistralai import ChatMistralAI
from dotenv import load_dotenv

# Load .env relative to THIS file's directory, not the current working
# directory — otherwise running the agent from anywhere other than
# ap_desktop_v2/ (a different shell cwd, a scheduled task, etc.) silently
# fails to find .env and every setting falls back to its hardcoded
# default with no warning. This was the root cause of approval-portal
# links pointing at the wrong host/port with no visible error.
load_dotenv(Path(__file__).resolve().parent / ".env")

RECIPIENT          = os.getenv("AP_NOTIFY_EMAIL", "balakrishna.m@avaali.com")
APPROVAL_PORTAL_URL = os.getenv("AP_APPROVAL_PORTAL_URL", "http://localhost:8000")

if not os.getenv("AP_APPROVAL_PORTAL_URL"):
    print("  ⚠️  AP_APPROVAL_PORTAL_URL not set in .env — review-request "
          f"emails will use the default '{APPROVAL_PORTAL_URL}', which "
          "only works if the reviewer opens the email on the SAME "
          "machine running approval_portal.py. Set AP_APPROVAL_PORTAL_URL "
          "in ap_desktop_v2/.env to your machine's real address "
          "(e.g. http://192.168.1.50:8000) if reviewers use a different PC.")

# ── Scenario classifier ───────────────────────────────────────────────────────
# Maps a set of reason keywords → human-readable scenario label
_SCENARIO_MAP = [
    (["duplicate"],                                          "Duplicate Invoice Detected",       "#7B2D8B"),
    (["damaged", "quality", "rejected", "partial", "short"],"Short / Damaged Delivery",         "#C0392B"),
    (["tax", "gst", "vat", "igst", "cgst", "sgst"],         "Tax Rate Mismatch",                "#1A5276"),
    (["uom", "unit of measure"],                             "Unit of Measure Mismatch",         "#784212"),
    (["payment term", "terms"],                              "Payment Terms Mismatch",           "#145A32"),
    (["freight", "delivery charge"],                         "Freight / Charge Overcharge",      "#1B4F72"),
    (["price", "unit price"],                                "Unit Price Variance",              "#6E2F8C"),
    (["vendor", "po#", "currency", "header"],                "Header Data Mismatch",             "#922B21"),
    (["quantity", "qty"],                                    "Quantity Mismatch",                "#7E5109"),
    (["missing", "incomplete"],                              "Incomplete — Missing Documents",   "#616A6B"),
]

def _detect_scenario(reasons: list, mismatches: list) -> tuple:
    """Return (scenario_label, colour) based on reasons text."""
    combined = " ".join((reasons or []) + (mismatches or [])).lower()
    for keywords, label, colour in _SCENARIO_MAP:
        if any(k in combined for k in keywords):
            return label, colour
    return "General Mismatch — Manual Review", "#E67E22"


# ── LLM helpers ───────────────────────────────────────────────────────────────
def _get_llm():
    return ChatMistralAI(
        model="mistral-small-latest",
        temperature=0.0,
        api_key=os.getenv("MISTRAL_API_KEY", "")
    )

SUMMARY_PROMPT = """You are a professional AP reconciliation assistant.
Write a concise business-friendly summary (4-6 sentences) of this invoice matching result.

Rules:
- State the invoice number, vendor, and PO number in the first sentence.
- Mention what was checked (header, line items, GRN, amounts).
- If manual review: clearly state each specific reason it failed.
- If auto-approved: confirm all checks passed.
- End with exactly one line: "Final Decision: <final_status>."
- Plain English, no markdown bullets, no headers."""

def generate_summary(invoice_data, po_data, grn_data, matching,
                     final_status, reviewer_info=None):
    print("\n" + "="*55)
    print("  GENERATING SUMMARY (Mistral AI)")
    print("="*55)
    context = {
        "invoice":      invoice_data,
        "po":           po_data,
        "grn":          grn_data,
        "matching":     {k: v for k, v in matching.items() if k != "item_results"},
        "final_status": final_status,
        "reviewer":     reviewer_info or {},
    }
    llm = _get_llm()
    resp = llm.invoke([
        SystemMessage(content=SUMMARY_PROMPT),
        HumanMessage(content=f"AP data:\n{json.dumps(context, indent=2)}"),
    ])
    summary = resp.content.strip()
    print(f"\n  {summary}\n")
    return summary


# ── CSS & palette ─────────────────────────────────────────────────────────────
_BASE_CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;margin:0;padding:0;
     background:#f0f2f5;color:#2c3e50}
.wrap{max-width:780px;margin:24px auto;border-radius:12px;
      overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.13)}
.topbar{height:6px}
.hdr{padding:28px 32px 20px;color:#fff}
.hdr h1{margin:0 0 4px;font-size:22px;font-weight:700;letter-spacing:-.3px}
.hdr p{margin:0;font-size:13px;opacity:.88}
.body{background:#fff;padding:28px 32px}
.summary-box{border-radius:8px;padding:16px 20px;margin-bottom:24px;
             font-size:14px;line-height:1.75;border-left:5px solid}
.section{margin-bottom:24px}
.section-title{font-size:12px;font-weight:700;letter-spacing:.8px;
               text-transform:uppercase;color:#7f8c8d;margin:0 0 10px;
               padding-bottom:6px;border-bottom:1px solid #ecf0f1}
table.data{width:100%;border-collapse:collapse;font-size:13px}
table.data td,table.data th{padding:9px 12px;text-align:left;
  border-bottom:1px solid #f0f0f0}
table.data th{background:#f8f9fa;color:#555;font-weight:600;
              font-size:11px;text-transform:uppercase;letter-spacing:.5px}
table.data tr:last-child td{border-bottom:none}
.pill{display:inline-block;font-size:11px;font-weight:700;padding:3px 10px;
      border-radius:20px;letter-spacing:.3px}
.pill-pass{background:#d5f5e3;color:#1e8449}
.pill-fail{background:#fdedec;color:#cb4335}
.pill-warn{background:#fef9e7;color:#b7950b}
.pill-info{background:#eaf4fb;color:#1a5276}
.pill-dup {background:#f5eef8;color:#6c3483}
.checklist{list-style:none;margin:0;padding:0}
.checklist li{padding:8px 0;border-bottom:1px solid #f0f0f0;
              font-size:13px;display:flex;align-items:flex-start;gap:10px}
.checklist li:last-child{border-bottom:none}
.chk-icon{font-size:16px;min-width:20px;margin-top:1px}
.diff-tbl{width:100%;border-collapse:collapse;font-size:13px}
.diff-tbl th{background:#f8f9fa;padding:9px 12px;text-align:left;
             font-size:11px;text-transform:uppercase;letter-spacing:.5px;
             color:#555;font-weight:600;border-bottom:2px solid #e0e0e0}
.diff-tbl td{padding:9px 12px;border-bottom:1px solid #f5f5f5;vertical-align:top}
.diff-tbl tr:last-child td{border-bottom:none}
.val-match{color:#1e8449;font-weight:600}
.val-miss {color:#cb4335;font-weight:600}
.val-warn {color:#b7950b;font-weight:600}
.val-neutral{color:#2c3e50}
.arrow{color:#aaa;margin:0 6px;font-size:11px}
.mismatch-row{background:#fff8f8}
.match-row{background:#f9fffa}
.decision-banner{border-radius:8px;padding:18px 22px;margin-top:24px;
                 display:flex;align-items:center;gap:14px}
.decision-icon{font-size:32px;min-width:42px}
.decision-text h3{margin:0 0 3px;font-size:16px;font-weight:700}
.decision-text p{margin:0;font-size:13px;opacity:.85}
.footer{background:#f8f9fa;padding:16px 32px;font-size:11px;
        color:#95a5a6;border-top:1px solid #ecf0f1;text-align:center}
.doc-card{border:1px solid #e8e8e8;border-radius:8px;padding:14px 16px;
          margin-bottom:10px}
.doc-card-title{font-size:11px;font-weight:700;letter-spacing:.6px;
                text-transform:uppercase;color:#888;margin:0 0 8px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.three-col{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
.kv{display:flex;justify-content:space-between;font-size:13px;
    padding:3px 0;border-bottom:1px dotted #f0f0f0}
.kv:last-child{border-bottom:none}
.kv-label{color:#888;font-weight:500}
.kv-value{font-weight:600;color:#2c3e50;text-align:right;max-width:60%}
.scenario-badge{display:inline-flex;align-items:center;gap:8px;
               border-radius:6px;padding:10px 16px;font-size:13px;
               font-weight:600;margin-bottom:20px}
.amount-highlight{font-size:26px;font-weight:700;letter-spacing:-.5px}
.amount-sub{font-size:12px;opacity:.7;margin-top:2px}
.delta-positive{color:#cb4335}
.delta-negative{color:#1e8449}
.tag{font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;
     letter-spacing:.4px;text-transform:uppercase;vertical-align:middle;
     margin-left:4px}
"""


# ── Component builders ────────────────────────────────────────────────────────

def _pill(text, kind="pass"):
    cls = {"pass":"pill-pass","fail":"pill-fail","warn":"pill-warn",
           "info":"pill-info","dup":"pill-dup"}.get(kind,"pill-info")
    return f"<span class='pill {cls}'>{text}</span>"

def _kv(label, value, bold_value=True):
    v = f"<strong>{value}</strong>" if bold_value else value
    return f"<div class='kv'><span class='kv-label'>{label}</span><span class='kv-value'>{v}</span></div>"

def _section(title, content):
    return (f"<div class='section'>"
            f"<p class='section-title'>{title}</p>"
            f"{content}"
            f"</div>")

def _doc_card(title, lines):
    inner = "".join(_kv(k, v) for k, v in lines)
    return f"<div class='doc-card'><p class='doc-card-title'>{title}</p>{inner}</div>"

def _fmt_amt(v):
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return str(v) if v else "—"

def _fmt_qty(v):
    return str(int(v)) if v is not None else "—"

def _pct_pill(pct, threshold=2.0):
    pct = abs(pct)
    if pct == 0:
        return _pill("0.00%  No variance", "pass")
    elif pct <= threshold:
        return _pill(f"{pct:.2f}%  Within tolerance", "warn")
    else:
        return _pill(f"{pct:.2f}%  Exceeds threshold", "fail")


# ── 9-check approval checklist ────────────────────────────────────────────────

_ALL_CHECKS = [
    ("PO Validity",        "Purchase Order is Open / Partially Open and not expired"),
    ("GRN Validation",     "Goods Receipt Note is approved and date precedes invoice"),
    ("Invoice Date",       "Invoice date is not future-dated or older than 90 days"),
    ("Vendor / Header",    "Vendor name, PO reference and currency all match"),
    ("Unit Price",         "Each line unit price within ±2% of PO agreed price"),
    ("Unit of Measure",    "Line UOM matches PO (EA, KG, LTR, BOX …)"),
    ("Quantity",           "Invoice qty = PO ordered qty = GRN accepted qty"),
    ("Tax Rate & Amount",  "GST/VAT/IGST rate and computed tax amount match PO"),
    ("Freight & Discount", "Freight ≤5% variance, discount matches PO terms"),
    ("Payment Terms",      "Payment terms (Net 30/45/60) match PO"),
    ("Line Amounts",       "Qty × Unit price = line total (arithmetic check)"),
    ("Invoice Total",      "Sum of lines + tax + freight − discount = invoice total"),
]

def _checklist_approved():
    """Full green checklist for auto-approved invoices."""
    items = ""
    for name, desc in _ALL_CHECKS:
        items += (
            f"<li>"
            f"<span class='chk-icon'>✅</span>"
            f"<span><strong>{name}</strong><br>"
            f"<span style='color:#666;font-size:12px'>{desc}</span></span>"
            f"</li>"
        )
    return f"<ul class='checklist'>{items}</ul>"

def _checklist_with_failures(failed_checks: list, reasons: list,
                              matching: dict = None,
                              invoice_data: dict = None,
                              po_data: dict = None) -> str:
    """
    Checklist where failed items are red; passed items are green.

    Determines each check's pass/fail status via EXPLICIT, deterministic
    rules — never by tokenizing reason text into individual words and
    looking for incidental overlaps. The old approach matched on bare
    words like "po" or "invoice", which appear constantly inside dollar
    amounts and labels ("PO $150.00", "Invoice $163.00") that have
    nothing to do with PO Validity or Invoice Date/Total — causing
    unrelated checks to light up red for scenarios that never touched
    them (e.g. a pure Vendor Mismatch wrongly showing Invoice Date and
    Invoice Total as failed).

    Each of the 12 checklist items is mapped to:
      (a) one or more REASON PREFIXES that, if any reason in `reasons`
          starts with one, mean that check failed — these prefixes are
          the consistent labels matcher.py's apply_business_rules()
          always uses ("Vendor Mismatch —", "Tax Mismatch —", etc.)
      (b) where reason-prefix matching alone is ambiguous (e.g.
          Quantity vs GRN Validation both stem from item-level
          mismatches), a structured-data check against `matching`'s
          item_results / header_match / line_match / grn_match.

    `matching`, `invoice_data`, `po_data` are optional for backward
    compatibility with any old caller that doesn't pass them — in that
    case the structured-data checks are skipped and only reason-prefix
    matching is used.
    """
    matching     = matching or {}
    invoice_data = invoice_data or {}
    po_data      = po_data or {}
    all_reasons  = [str(r) for r in (failed_checks + reasons)]

    def _has_prefix(*prefixes) -> bool:
        return any(
            r.strip().lower().startswith(p.lower())
            for r in all_reasons for p in prefixes
        )

    def _contains_phrase(*phrases) -> bool:
        """For checks with no dedicated prefix — substring match on a
        specific multi-word phrase, NOT individual words, to avoid the
        original bug's word-collision problem."""
        return any(
            p.lower() in r.lower() for r in all_reasons for p in phrases
        )

    item_results = matching.get("item_results", [])

    # ── Quantity check: invoice qty vs PO qty vs GRN accepted qty ───────
    # Distinct from GRN Validation (which is about the GRN document
    # itself being approved/dated correctly, not quantity reconciliation)
    # and distinct from Unit Price (item_results["invoice_vs_po"] is a
    # combined flag that also flips to "Mismatch" on a pure price
    # difference — so quantity must be checked from the raw qty fields
    # directly, not that conflated flag, to avoid a price-only variance
    # wrongly lighting up the Quantity check too).
    def _qty_differs(i: dict) -> bool:
        inv_qty  = i.get("invoice_qty")
        po_qty   = i.get("po_qty")
        grn_acc  = i.get("grn_accepted")
        if inv_qty is not None and po_qty is not None and inv_qty != po_qty:
            return True
        if inv_qty is not None and grn_acc is not None and inv_qty != grn_acc:
            return True
        return False

    qty_mismatch = any(_qty_differs(i) for i in item_results) or _has_prefix(
        "Short Delivery —", "Damaged Goods —"
    )

    # ── GRN Validation: the GRN document itself (approval/date), NOT  ──
    # ── quantity reconciliation — only fails on an explicit GRN-doc    ──
    # ── level problem (missing GRN, GRN dated after invoice, etc.)     ──
    grn_doc_invalid = _contains_phrase(
        "grn not found", "grn date", "grn vendor", "status is not approved",
    )

    # ── Invoice Date: future-dated or >90 days old — its own dedicated ──
    # ── check elsewhere in the codebase (node_invoice_date_validation), ──
    # ── never inferred from reason text in this pipeline today.        ──
    invoice_date_invalid = _contains_phrase(
        "invoice date", "future-dated", "older than 90 days",
    ) and not _has_prefix("Vendor Mismatch —", "Tax Mismatch —")
    # The trailing guard above is defensive: even a deliberate phrase
    # match shouldn't fire if the SAME reason is actually about vendor/
    # tax (defends against a future reason string that happens to also
    # mention "invoice date" in passing, e.g. as context).

    # ── Invoice Total: line items + tax + freight − discount = total —  ──
    # ── only fails on a genuine arithmetic reconciliation problem,      ──
    # ── never merely because some OTHER reason mentions a dollar       ──
    # ── amount that happens to be labelled "Invoice $123.45".          ──
    invoice_total_invalid = _contains_phrase(
        "total amount mismatch", "invoice total", "arithmetic",
    ) and _has_prefix("Price Variance — Total Amount Mismatch")

    checks_failed = {
        "PO Validity":        _contains_phrase(
            "po not found", "po status", "po expired", "not open",
        ),
        "GRN Validation":     grn_doc_invalid,
        "Invoice Date":       invoice_date_invalid,
        "Vendor / Header":    _has_prefix(
            "Vendor Mismatch —", "PO Number Mismatch —", "Currency Mismatch —",
            "Header Mismatch —",
        ),
        "Unit Price":         _has_prefix("Price Variance — Unit Price Mismatch"),
        "Unit of Measure":    _has_prefix("UOM Mismatch —"),
        "Quantity":           qty_mismatch,
        "Tax Rate & Amount":  _has_prefix("Tax Mismatch —", "Price Variance — Tax Rate"),
        "Freight & Discount": _has_prefix("Freight Overcharge —", "Price Variance — Freight",
                                           "Price Variance — Discount"),
        "Payment Terms":      _has_prefix("Payment Terms Mismatch —"),
        "Line Amounts":       _has_prefix("Price Variance — Line Item Subtotal")
                               and not qty_mismatch,
        "Invoice Total":      invoice_total_invalid,
    }

    items = ""
    for name, desc in _ALL_CHECKS:
        failed = checks_failed.get(name, False)
        icon   = "❌" if failed else "✅"
        style  = "color:#cb4335" if failed else "color:#1e8449"
        items += (
            f"<li>"
            f"<span class='chk-icon'>{icon}</span>"
            f"<span style='{style}'><strong>{name}</strong></span>"
            f"<span style='color:#666;font-size:12px;margin-left:4px'>— {desc}</span>"
            f"</li>"
        )
    return f"<ul class='checklist'>{items}</ul>"


# ── Line-item diff table ───────────────────────────────────────────────────────

def _line_item_diff_table(item_results: list) -> str:
    """
    Rich diff table showing Invoice / PO / GRN values side by side.
    Each mismatched cell is highlighted with the exact delta.
    """
    if not item_results:
        return "<p style='color:#aaa;font-size:13px'>No line items found.</p>"

    rows = ""
    for item in item_results:
        is_match  = item.get("status") == "Matched"
        row_class = "match-row" if is_match else "mismatch-row"
        status_pill = _pill("Matched", "pass") if is_match else _pill("Mismatch", "fail")

        inv_qty  = item.get("invoice_qty")
        po_qty   = item.get("po_qty")
        grn_acc  = item.get("grn_accepted")
        grn_recv = item.get("grn_received")
        inv_p    = item.get("invoice_price")
        po_p     = item.get("po_price")
        cond     = item.get("condition", "Good")

        # Qty cell
        qty_issues = []
        if po_qty  is not None and inv_qty != po_qty:
            qty_issues.append(f"PO ordered: {_fmt_qty(po_qty)}")
        if grn_acc is not None and inv_qty != grn_acc:
            qty_issues.append(f"GRN accepted: {_fmt_qty(grn_acc)}")
        if grn_recv is not None and po_qty is not None and po_qty != grn_recv:
            qty_issues.append(f"GRN received: {_fmt_qty(grn_recv)}")

        qty_html = f"<span class='val-neutral'>{_fmt_qty(inv_qty)}</span>"
        if qty_issues:
            qty_html = (
                f"<span class='val-miss'>{_fmt_qty(inv_qty)}</span>"
                f"<br><span style='font-size:11px;color:#c0392b'>"
                + "<br>".join(f"≠ {q}" for q in qty_issues)
                + "</span>"
            )

        # Price cell
        if inv_p is not None and po_p is not None and abs(inv_p - po_p) > 0.01:
            diff_pct = ((inv_p - po_p) / po_p * 100) if po_p else 0
            sign     = "+" if diff_pct > 0 else ""
            price_html = (
                f"<span class='val-miss'>{_fmt_amt(inv_p)}</span>"
                f"<br><span style='font-size:11px;color:#c0392b'>"
                f"PO: {_fmt_amt(po_p)}"
                f"<span class='tag' style='background:#fde8e8;color:#c0392b'>"
                f"{sign}{diff_pct:.1f}%</span></span>"
            )
        else:
            price_html = f"<span class='val-match'>{_fmt_amt(inv_p)}</span>"

        # Condition cell
        if cond.lower() not in ["good", "ok", "acceptable", ""]:
            cond_html = f"<span class='val-miss'>{cond}</span>"
        else:
            cond_html = f"<span class='val-match'>{cond}</span>"

        # GRN status
        if item.get("invoice_vs_grn") == "Match" and item.get("po_vs_grn") == "Match":
            grn_status = _pill("Matched", "pass")
        else:
            grn_status = _pill("Mismatch", "fail")

        rows += (
            f"<tr class='{row_class}'>"
            f"<td><strong>{item.get('item','')}</strong></td>"
            f"<td>{qty_html}</td>"
            f"<td>{price_html}</td>"
            f"<td>{cond_html}</td>"
            f"<td>{grn_status}</td>"
            f"<td>{status_pill}</td>"
            f"</tr>"
        )

    return (
        "<table class='diff-tbl'>"
        "<thead><tr>"
        "<th style='width:28%'>Line Item</th>"
        "<th>Qty (Invoice)</th>"
        "<th>Unit Price (Invoice)</th>"
        "<th>Condition</th>"
        "<th>GRN Match</th>"
        "<th>Status</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


# ── Header diff section ────────────────────────────────────────────────────────

def _header_diff_section(invoice_data, po_data, grn_data, matching):
    """
    Side-by-side header comparison across all 3 documents.
    Highlights mismatches in red.
    """
    def _cell(inv_val, po_val, label=""):
        match = str(inv_val).strip().lower() == str(po_val).strip().lower()
        cls_i = "val-match" if match else "val-miss"
        cls_p = "val-match" if match else "val-miss"
        status = "✅" if match else "❌"
        return (
            f"<tr{'  class=\"mismatch-row\"' if not match else ''}>"
            f"<td style='color:#888;font-size:12px;font-weight:600;width:22%'>{label}</td>"
            f"<td><span class='{cls_i}'>{inv_val or '—'}</span></td>"
            f"<td><span class='{cls_p}'>{po_val  or '—'}</span></td>"
            f"<td style='color:#888'>{grn_data.get('vendor_name','—') if label=='Vendor' else '—'}</td>"
            f"<td style='text-align:center'>{status}</td>"
            f"</tr>"
        )

    vendor_match = (
        str(invoice_data.get('vendor_name','')).strip().lower() ==
        str(po_data.get('vendor_name','')).strip().lower()
    )
    po_match = (
        str(invoice_data.get('po_number','')).strip().upper() ==
        str(po_data.get('po_number','')).strip().upper()
    )
    cur_match = (
        str(invoice_data.get('currency','USD')).upper() ==
        str(po_data.get('currency','USD')).upper()
    )

    def _row(label, inv_v, po_v, grn_v="—", match=True):
        cls = "" if match else " class='mismatch-row'"
        ci  = "val-match" if match else "val-miss"
        cp  = "val-match" if match else "val-miss"
        icon = "✅" if match else "❌"
        return (
            f"<tr{cls}>"
            f"<td style='color:#888;font-size:12px;font-weight:600;width:22%'>{label}</td>"
            f"<td><span class='{ci}'>{inv_v}</span></td>"
            f"<td><span class='{cp}'>{po_v}</span></td>"
            f"<td><span style='color:#555'>{grn_v}</span></td>"
            f"<td style='text-align:center'>{icon}</td>"
            f"</tr>"
        )

    rows = (
        _row("Vendor",    invoice_data.get('vendor_name','—'),
                          po_data.get('vendor_name','—'),
                          grn_data.get('vendor_name','—'), vendor_match)
        + _row("PO Number", invoice_data.get('po_number','—'),
                             po_data.get('po_number','—'),
                             grn_data.get('po_number','—'), po_match)
        + _row("Currency",  invoice_data.get('currency','USD'),
                             po_data.get('currency','USD'), "—", cur_match)
        + _row("Date",      invoice_data.get('invoice_date','—'),
                             po_data.get('po_date','—'),
                             grn_data.get('received_date','—'), True)
    )

    return (
        "<table class='diff-tbl'>"
        "<thead><tr>"
        "<th>Field</th><th>Invoice</th><th>Purchase Order</th>"
        "<th>GRN</th><th>Match</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


# ── Amount reconciliation panel ────────────────────────────────────────────────

def _amount_panel(invoice_data, po_data, matching, variance_threshold=2.0):
    inv_total = float(invoice_data.get("total_amount", 0))
    po_total  = float(po_data.get("po_amount", 0))
    variance  = matching.get("variance", 0)
    var_pct   = abs(matching.get("variance_percentage", 0))

    subtotal = float(invoice_data.get("subtotal", 0))
    tax      = float(invoice_data.get("tax_amount", invoice_data.get("tax", 0)))
    freight  = float(invoice_data.get("freight", 0))
    discount = float(invoice_data.get("discount", 0))

    delta_style = "delta-positive" if variance > 0 else "delta-negative"
    sign        = "+" if variance > 0 else ""
    var_pill    = _pct_pill(var_pct, variance_threshold)

    breakdown = ""
    if subtotal:
        breakdown = (
            "<div style='background:#f8f9fa;border-radius:6px;padding:12px 14px;"
            "font-size:13px;margin-top:10px'>"
            "<p style='margin:0 0 8px;font-size:11px;font-weight:700;"
            "text-transform:uppercase;letter-spacing:.5px;color:#888'>"
            "Invoice Amount Breakdown</p>"
            + _kv("Subtotal", _fmt_amt(subtotal))
            + _kv("Tax", _fmt_amt(tax))
            + (_kv("Freight", _fmt_amt(freight)) if freight else "")
            + (_kv("Discount", f"− {_fmt_amt(discount)}") if discount else "")
            + f"<div class='kv' style='border-top:1px solid #ddd;margin-top:4px;padding-top:6px'>"
            f"<span class='kv-label' style='font-weight:700'>Invoice Total</span>"
            f"<span class='kv-value'><strong>{_fmt_amt(inv_total)}</strong></span></div>"
            "</div>"
        )

    return (
        "<div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:8px'>"
        + f"<div style='text-align:center;background:#f0fdf4;border-radius:8px;padding:16px'>"
          f"<p style='margin:0 0 4px;font-size:11px;color:#888;text-transform:uppercase;"
          f"letter-spacing:.5px'>PO Amount</p>"
          f"<p class='amount-highlight' style='color:#1e8449;margin:0'>{_fmt_amt(po_total)}</p>"
          f"</div>"
        + f"<div style='text-align:center;background:#fef9f0;border-radius:8px;padding:16px'>"
          f"<p style='margin:0 0 4px;font-size:11px;color:#888;text-transform:uppercase;"
          f"letter-spacing:.5px'>Invoice Amount</p>"
          f"<p class='amount-highlight' style='color:#e67e22;margin:0'>{_fmt_amt(inv_total)}</p>"
          f"</div>"
        + f"<div style='text-align:center;background:{'#fff5f5' if var_pct>variance_threshold else '#f0f9ff'};"
          f"border-radius:8px;padding:16px'>"
          f"<p style='margin:0 0 4px;font-size:11px;color:#888;text-transform:uppercase;"
          f"letter-spacing:.5px'>Variance</p>"
          f"<p class='amount-highlight {delta_style}' style='margin:0'>"
          f"{sign}{_fmt_amt(variance)}</p>"
          f"<p style='margin:4px 0 0;font-size:12px'>{var_pill}</p>"
          f"</div>"
        + "</div>"
        + breakdown
    )


# ── Mismatch reasons detail panel ─────────────────────────────────────────────

def _reasons_panel(reasons, mismatches):
    all_issues = list(dict.fromkeys((reasons or []) + (mismatches or [])))
    if not all_issues:
        return ""

    rows = ""
    for issue in all_issues:
        il = issue.lower()
        if any(k in il for k in ["duplicate"]):
            cat, cat_bg, cat_fg = "Duplicate", "#f5eef8", "#6c3483"
        elif any(k in il for k in ["vendor", "po#", "currency", "header"]):
            cat, cat_bg, cat_fg = "Header", "#fdebd0", "#784212"
        elif any(k in il for k in ["price", "unit price"]):
            cat, cat_bg, cat_fg = "Price", "#f5eef8", "#6c3483"
        elif any(k in il for k in ["uom", "unit of measure"]):
            cat, cat_bg, cat_fg = "UOM", "#eaf4fb", "#1a5276"
        elif any(k in il for k in ["qty", "quantity"]):
            cat, cat_bg, cat_fg = "Quantity", "#fef9e7", "#7e5109"
        elif any(k in il for k in ["tax", "gst", "vat", "igst"]):
            cat, cat_bg, cat_fg = "Tax", "#eaf4fb", "#1a5276"
        elif any(k in il for k in ["freight", "delivery"]):
            cat, cat_bg, cat_fg = "Freight", "#e8f8f5", "#1e8449"
        elif any(k in il for k in ["payment", "terms"]):
            cat, cat_bg, cat_fg = "Terms", "#f0f3ff", "#2c3e90"
        elif any(k in il for k in ["date", "age", "future", "stale"]):
            cat, cat_bg, cat_fg = "Date", "#fdfefe", "#616a6b"
        elif any(k in il for k in ["grn", "goods receipt", "damaged", "quality"]):
            cat, cat_bg, cat_fg = "GRN/Quality", "#fff5f5", "#c0392b"
        elif any(k in il for k in ["missing", "incomplete"]):
            cat, cat_bg, cat_fg = "Missing Doc", "#ecf0f1", "#5d6d7e"
        else:
            cat, cat_bg, cat_fg = "Other", "#f8f9fa", "#555"

        badge = (
            f"<span style='background:{cat_bg};color:{cat_fg};font-size:10px;"
            f"font-weight:700;padding:2px 8px;border-radius:4px;white-space:nowrap'>"
            f"{cat}</span>"
        )
        rows += (
            f"<tr>"
            f"<td style='padding:9px 12px;vertical-align:middle;width:16%'>{badge}</td>"
            f"<td style='padding:9px 12px;font-size:13px;color:#c0392b;font-weight:500'>"
            f"{issue}</td>"
            f"</tr>"
        )

    return (
        "<div style='border:1.5px solid #f5c6cb;border-radius:8px;overflow:hidden;"
        "margin-bottom:16px'>"
        "<div style='background:#c0392b;color:#fff;padding:10px 14px;"
        "font-size:12px;font-weight:700;letter-spacing:.5px;text-transform:uppercase'>"
        "⚠  Mismatch Details — Action Required</div>"
        "<table style='width:100%;border-collapse:collapse'>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        "</div>"
    )


# ── Scenario banner ────────────────────────────────────────────────────────────

def _scenario_banner(scenario_label, colour):
    icons = {
        "Duplicate Invoice Detected":      "🔁",
        "Short / Damaged Delivery":        "📦",
        "Tax Rate Mismatch":               "🧾",
        "Unit of Measure Mismatch":        "📐",
        "Payment Terms Mismatch":          "📅",
        "Freight / Charge Overcharge":     "🚚",
        "Unit Price Variance":             "💰",
        "Header Data Mismatch":            "🏷",
        "Quantity Mismatch":               "🔢",
        "Incomplete — Missing Documents":  "📎",
        "General Mismatch — Manual Review":"⚠️",
    }
    icon = icons.get(scenario_label, "⚠️")
    return (
        f"<div class='scenario-badge' style='background:{colour}18;border:1.5px solid {colour}44;color:{colour}'>"
        f"<span style='font-size:20px'>{icon}</span>"
        f"<span>Scenario Detected: <strong>{scenario_label}</strong></span>"
        f"</div>"
    )


def _review_action_block_with_token(token: str, portal_url: str) -> str:
    """
    Clickable Approve/Reject/Clarify links + a "Review with comments" link
    to the full web portal page. The one-click links use a default
    reviewer name of "AP Reviewer" with no comments — the portal page is
    the way to supply your name and comments before deciding.
    """
    review_url   = f"{portal_url}/review/{token}"
    approve_url  = f"{portal_url}/approve/{token}"
    reject_url   = f"{portal_url}/reject/{token}"
    clarify_url  = f"{portal_url}/clarify/{token}"

    btn = lambda href, label, bg: (
        f"<a href='{href}' style='display:inline-block;background:{bg};color:#fff;"
        f"padding:10px 22px;border-radius:6px;font-weight:700;font-size:13px;"
        f"text-decoration:none;margin:4px'>{label}</a>"
    )

    return (
        "<div style='background:#eaf4fb;border:1.5px solid #2D4A7A;border-radius:8px;"
        "padding:18px 20px;margin-top:16px;text-align:center'>"
        "<p style='margin:0 0 14px;font-weight:700;font-size:14px;color:#2D4A7A'>"
        "👉 Click below to record your decision</p>"
        f"<a href='{review_url}' style='display:inline-block;background:#2D4A7A;color:#fff;"
        f"padding:12px 28px;border-radius:6px;font-weight:700;font-size:14px;"
        f"text-decoration:none;margin-bottom:12px'>📋 Open Review Page (enter name &amp; comments)</a>"
        "<p style='margin:14px 0 8px;font-size:12px;color:#777'>"
        "Or use a one-click link (recorded as \"AP Reviewer\", no comments):</p>"
        "<div>"
        + btn(approve_url,  "✅ Approve",              "#27AE60")
        + btn(reject_url,   "❌ Reject",                "#c0392b")
        + btn(clarify_url,  "🔼 Request Clarification", "#f39c12")
        + "</div>"
        "<p style='margin:12px 0 0;font-size:11px;color:#999'>"
        "Links are single-use — once a decision is recorded, "
        "the link no longer applies.</p>"
        "</div>"
    )


def _review_action_block_reply_only() -> str:
    """
    Fallback when no portal token is available (web portal not in use
    for this run) — the original reply-with-keyword instructions.
    """
    return (
        "<div style='background:#e8f4e8;border:1.5px solid #27AE60;border-radius:8px;"
        "padding:16px 20px;margin-top:16px'>"
        "<p style='margin:0 0 10px;font-weight:700;font-size:14px'>"
        "📨 Reply to this email with your decision:</p>"
        "<div style='display:flex;gap:10px;flex-wrap:wrap'>"
        "<span style='background:#27AE60;color:#fff;padding:6px 16px;"
        "border-radius:4px;font-weight:700;font-size:13px'>APPROVE</span>"
        "<span style='background:#c0392b;color:#fff;padding:6px 16px;"
        "border-radius:4px;font-weight:700;font-size:13px'>REJECT</span>"
        "<span style='background:#f39c12;color:#fff;padding:6px 16px;"
        "border-radius:4px;font-weight:700;font-size:13px'>CLARIFICATION</span>"
        "</div>"
        "<p style='margin:10px 0 0;font-size:12px;color:#555'>"
        "Add your comments on a new line after the keyword.</p>"
        "</div>"
    )


# ── Master email builders ─────────────────────────────────────────────────────

def _email_approved(invoice_data, po_data, grn_data, matching, summary,
                    reviewer_info, variance_threshold):
    """Full green email for Auto-Approved or Reviewer-Approved invoices."""
    inv_num = invoice_data.get("invoice_number", "N/A")
    vendor  = invoice_data.get("vendor_name",    "N/A")
    po_num  = po_data.get("po_number",           "N/A")
    grn_num = grn_data.get("grn_number",         "N/A")
    colour  = "#1e8449" if not reviewer_info else "#2980b9"
    label   = "Perfect Match ✓" if not reviewer_info else "Approved by Reviewer ✓"
    icon    = "✅"

    doc_cards = (
        "<div class='three-col'>"
        + _doc_card("📄 Invoice",  [
            ("Number",  inv_num),
            ("Date",    invoice_data.get("invoice_date","—")),
            ("Vendor",  vendor),
            ("Amount",  _fmt_amt(invoice_data.get("total_amount",0))),
            ("Tax",     invoice_data.get("tax_type","—")),
            ("Terms",   invoice_data.get("payment_terms","—")),
          ])
        + _doc_card("📋 Purchase Order", [
            ("PO Number", po_num),
            ("PO Date",   po_data.get("po_date","—")),
            ("Vendor",    po_data.get("vendor_name","—")),
            ("PO Amount", _fmt_amt(po_data.get("po_amount",0))),
            ("Currency",  po_data.get("currency","—")),
          ])
        + _doc_card("📦 GRN", [
            ("GRN Number",  grn_num),
            ("Received By", grn_data.get("received_by","—")),
            ("Recv Date",   grn_data.get("received_date","—")),
            ("PO Ref",      grn_data.get("po_number","—")),
          ])
        + "</div>"
    )

    reviewer_section = ""
    if reviewer_info:
        reviewer_section = _section("👤 Reviewer Details",
            "<table class='data'>"
            f"<tr><td style='width:35%;color:#888'>Reviewer</td>"
            f"<td><strong>{reviewer_info.get('reviewer','')}</strong></td></tr>"
            f"<tr><td style='color:#888'>Decision</td>"
            f"<td>{reviewer_info.get('decision','')}</td></tr>"
            f"<tr><td style='color:#888'>Comments</td>"
            f"<td>{reviewer_info.get('notes','') or 'None'}</td></tr>"
            f"<tr><td style='color:#888'>Reviewed At</td>"
            f"<td>{reviewer_info.get('timestamp','')}</td></tr>"
            "</table>"
        )

    body = (
        f"<style>{_BASE_CSS}</style>"
        "<div class='wrap'>"
        f"<div class='topbar' style='background:{colour}'></div>"
        f"<div class='hdr' style='background:{colour}'>"
        f"<h1>{icon} AP Invoice Processing Result</h1>"
        f"<p>{label} &nbsp;·&nbsp; Invoice {inv_num} &nbsp;·&nbsp; {vendor}</p>"
        "</div>"
        "<div class='body'>"

        # Summary
        f"<div class='summary-box' style='background:#f0fdf4;border-color:{colour}'>"
        f"{summary}"
        "</div>"

        # Document cards
        + _section("📑 Document Reference", doc_cards)

        # Amount panel
        + _section("💵 Amount Reconciliation",
                   _amount_panel(invoice_data, po_data, matching, variance_threshold))

        # All checks green
        + _section("✅ All Validation Checks Passed", _checklist_approved())

        # Line items — all matched
        + _section("📦 Line Items (3-Way Match)",
                   _line_item_diff_table(matching.get("item_results", [])))

        + reviewer_section

        # Decision banner
        + f"<div class='decision-banner' style='background:#f0fdf4;border:2px solid {colour}'>"
          f"<span class='decision-icon'>{icon}</span>"
          f"<div class='decision-text'>"
          f"<h3 style='color:{colour}'>{label}</h3>"
          f"<p>Invoice cleared for payment processing. No manual intervention required.</p>"
          f"</div></div>"

        + f"<div class='footer'>"
          f"AP Automation Agent &nbsp;·&nbsp; {datetime.now().strftime('%d %b %Y %H:%M')}"
          f" &nbsp;·&nbsp; Email moved to <strong>Processed</strong> folder"
          f"</div>"
        + "</div></div>"
    )
    return body


def _email_review_or_rejected(invoice_data, po_data, grn_data, matching,
                               summary, decision_info, reviewer_info,
                               final_status, variance_threshold):
    """Detailed email for Manual Review / Rejected / Pending Clarification."""
    inv_num  = invoice_data.get("invoice_number", "N/A")
    vendor   = invoice_data.get("vendor_name",    "N/A")
    po_num   = po_data.get("po_number",           "N/A")
    grn_num  = grn_data.get("grn_number",         "N/A")
    reasons  = decision_info.get("reasons",    [])
    mismatches = matching.get("mismatches",    [])

    scenario_label, scenario_colour = _detect_scenario(reasons, mismatches)
    failed_checks = decision_info.get("failed_checks", []) + reasons

    if final_status == "Rejected by Reviewer":
        colour, icon, label = "#c0392b", "❌", "Rejected by Reviewer"
    elif final_status == "Pending Clarification":
        colour, icon, label = "#f39c12", "⏳", "Pending Clarification"
    else:
        colour, icon, label = "#e67e22", "⚠️", "Manual Review Required"

    doc_cards = (
        "<div class='three-col'>"
        + _doc_card("📄 Invoice", [
            ("Number",  inv_num),
            ("Date",    invoice_data.get("invoice_date","—")),
            ("Vendor",  vendor),
            ("Amount",  _fmt_amt(invoice_data.get("total_amount",0))),
            ("Tax",     invoice_data.get("tax_type","—")),
            ("Terms",   invoice_data.get("payment_terms","—")),
          ])
        + _doc_card("📋 Purchase Order", [
            ("PO Number", po_num),
            ("PO Date",   po_data.get("po_date","—")),
            ("Vendor",    po_data.get("vendor_name","—")),
            ("PO Amount", _fmt_amt(po_data.get("po_amount",0))),
            ("Currency",  po_data.get("currency","—")),
          ])
        + _doc_card("📦 GRN", [
            ("GRN Number",  grn_num),
            ("Received By", grn_data.get("received_by","—")),
            ("Recv Date",   grn_data.get("received_date","—")),
            ("PO Ref",      grn_data.get("po_number","—")),
          ])
        + "</div>"
    )

    reviewer_section = ""
    if reviewer_info:
        reviewer_section = _section("👤 Reviewer Details",
            "<table class='data'>"
            f"<tr><td style='width:35%;color:#888'>Reviewer</td>"
            f"<td><strong>{reviewer_info.get('reviewer','')}</strong></td></tr>"
            f"<tr><td style='color:#888'>Decision</td>"
            f"<td>{reviewer_info.get('decision','')}</td></tr>"
            f"<tr><td style='color:#888'>Comments</td>"
            f"<td>{reviewer_info.get('notes','') or 'None'}</td></tr>"
            f"<tr><td style='color:#888'>Reviewed At</td>"
            f"<td>{reviewer_info.get('timestamp','')}</td></tr>"
            "</table>"
        )

    folder_note = "Exceptions" if final_status != "Approved by Reviewer" else "Processed"

    body = (
        f"<style>{_BASE_CSS}</style>"
        "<div class='wrap'>"
        f"<div class='topbar' style='background:{colour}'></div>"
        f"<div class='hdr' style='background:{colour}'>"
        f"<h1>{icon} AP Invoice Processing Result</h1>"
        f"<p>{label} &nbsp;·&nbsp; Invoice {inv_num} &nbsp;·&nbsp; {vendor}</p>"
        "</div>"
        "<div class='body'>"

        # Scenario banner
        + _scenario_banner(scenario_label, scenario_colour)

        # Summary
        + f"<div class='summary-box' style='background:#fff8f4;border-color:{colour}'>"
        + f"{summary}"
        + "</div>"

        # Mismatch reasons
        + _reasons_panel(reasons, mismatches)

        # Document cards
        + _section("📑 Document Reference", doc_cards)

        # Header diff
        + _section("🔍 Header Comparison (Invoice vs PO vs GRN)",
                   _header_diff_section(invoice_data, po_data, grn_data, matching))

        # Amount panel
        + _section("💵 Amount Reconciliation",
                   _amount_panel(invoice_data, po_data, matching, variance_threshold))

        # Validation checklist (with fails highlighted)
        + _section("📋 Validation Checklist",
                   _checklist_with_failures(failed_checks, reasons, matching,
                                             invoice_data, po_data))

        # Line items diff
        + _section("📦 Line Item Diff (Invoice vs PO vs GRN)",
                   _line_item_diff_table(matching.get("item_results", [])))

        + reviewer_section

        # Decision banner
        + f"<div class='decision-banner' style='background:#fff8f0;border:2px solid {colour}'>"
          f"<span class='decision-icon'>{icon}</span>"
          f"<div class='decision-text'>"
          f"<h3 style='color:{colour}'>{label}</h3>"
          f"<p>Discrepancies found. Please review the details above and take action.</p>"
          f"</div></div>"

        + f"<div class='footer'>"
          f"AP Automation Agent &nbsp;·&nbsp; {datetime.now().strftime('%d %b %Y %H:%M')}"
          f" &nbsp;·&nbsp; Email moved to <strong>{folder_note}</strong> folder"
          f"</div>"
        + "</div></div>"
    )
    return body


def _email_missing_docs(invoice_data, po_data, grn_data, matching,
                         summary, decision_info):
    """
    Email for submissions that couldn't be processed — no readable
    Invoice attachment, extraction failure, or the invoice's PO Number
    wasn't found in the ERP. NOTE: in the current architecture only the
    Invoice PDF is expected as an email attachment; PO and GRN data
    come from the ERP lookup by PO Number, not from attachments — this
    template must not tell vendors to attach PO/GRN PDFs.
    """
    reasons = decision_info.get("reasons", []) or ["Could not process this submission — see details above."]
    colour  = "#616a6b"

    inv_num = invoice_data.get("invoice_number", "")
    vendor  = invoice_data.get("vendor_name", "")
    po_num  = (po_data or {}).get("po_number") or invoice_data.get("po_number", "")

    known_lines = ""
    if inv_num:
        known_lines += f"<tr><td style='width:35%;color:#888'>Invoice Number</td><td><strong>{inv_num}</strong></td></tr>"
    if vendor:
        known_lines += f"<tr><td style='color:#888'>Vendor</td><td>{vendor}</td></tr>"
    if po_num:
        known_lines += f"<tr><td style='color:#888'>PO Number (as read from invoice)</td><td>{po_num}</td></tr>"

    known_section = ""
    if known_lines:
        known_section = _section("📄 What We Could Read", f"<table class='data'>{known_lines}</table>")

    reasons_items = "".join(f"<li>{r}</li>" for r in reasons)

    body = (
        f"<style>{_BASE_CSS}</style>"
        "<div class='wrap'>"
        f"<div class='topbar' style='background:{colour}'></div>"
        f"<div class='hdr' style='background:{colour}'>"
        "<h1>❗ AP Invoice Processing — Could Not Complete</h1>"
        "<p>This submission needs attention before it can be matched and processed</p>"
        "</div>"
        "<div class='body'>"

        f"<div class='summary-box' style='background:#f8f9fa;border-color:{colour}'>"
        f"{summary}"
        "</div>"

        + known_section

        + _section("⚠️ Why This Couldn't Proceed",
            f"<ul class='checklist'>{reasons_items}</ul>")

        + _section("ℹ️ How to Fix This",
            "<div style='background:#eaf4fb;border-radius:8px;padding:16px;font-size:13px;line-height:1.8'>"
            "<ul style='margin:0;padding-left:20px;color:#555'>"
            "<li>Only the <strong>Invoice PDF</strong> needs to be attached — "
            "Purchase Order and Goods Receipt data are looked up automatically "
            "from the ERP using the PO Number printed on the invoice.</li>"
            "<li>If the reason above mentions a PO Number not found: verify the "
            "PO Number on the invoice is correct and that the PO has already "
            "been created in the ERP.</li>"
            "<li>If the reason above mentions the invoice being unreadable: "
            "resend the email with a clear, non-scanned Invoice PDF attached.</li>"
            "<li>Once corrected, simply resend the email — it will be "
            "picked up on the next processing run.</li>"
            "</ul>"
            "</div>"
        )

        + f"<div class='decision-banner' style='background:#f8f9fa;border:2px solid {colour}'>"
          "<span class='decision-icon'>❗</span>"
          "<div class='decision-text'>"
          f"<h3 style='color:{colour}'>Incomplete — Action Needed</h3>"
          f"<p>{reasons[0] if reasons else 'See details above.'}</p>"
          "</div></div>"

        + f"<div class='footer'>"
          f"AP Automation Agent &nbsp;·&nbsp; {datetime.now().strftime('%d %b %Y %H:%M')}"
          " &nbsp;·&nbsp; Email moved to <strong>Exceptions</strong> folder"
          "</div>"
        + "</div></div>"
    )
    return body


# ── Subject builder ───────────────────────────────────────────────────────────

def _build_subject(final_status, inv_num, vendor, matching, decision_info):
    reasons = decision_info.get("reasons", []) if decision_info else []
    mismatches = matching.get("mismatches", []) if matching else []
    scenario_label, _ = _detect_scenario(reasons, mismatches)

    prefixes = {
        "Perfect Match":                  f"✅ Perfect Match",
        "Approved by Reviewer":           f"✅ Approved",
        "Rejected by Reviewer":           f"❌ Rejected",
        "Pending Clarification":          f"⏳ Clarification Needed",
        "Manual Review Required":         f"⚠️  Manual Review",
        "Incomplete — Missing Documents": f"❗ Incomplete",
    }
    prefix = prefixes.get(final_status)
    if prefix is None:
        if final_status.startswith("Incomplete") or final_status.startswith("Error"):
            prefix = "❗ Incomplete"
        else:
            prefix = "📋 AP Result"

    if final_status in ("Perfect Match", "Approved by Reviewer"):
        return f"{prefix} — INV {inv_num} | {vendor}"
    else:
        return f"{prefix} — INV {inv_num} | {vendor} | {scenario_label}"


# ── Public API ────────────────────────────────────────────────────────────────

def _build_full_email(invoice_data, po_data, grn_data, matching,
                      final_status, summary, decision_info=None,
                      reviewer_info=None, variance_threshold=2.0):
    """Route to the correct email template and return HTML body."""
    if final_status in ("Perfect Match", "Approved by Reviewer"):
        return _email_approved(invoice_data, po_data, grn_data, matching,
                               summary, reviewer_info, variance_threshold)
    elif final_status.startswith("Incomplete") or final_status.startswith("Error"):
        # Covers every "couldn't complete processing" case — missing/unreadable
        # invoice, extraction failure, PO not found in ERP, pipeline errors,
        # etc. — not just the one exact original string. New failure modes
        # introduced later automatically get this template instead of being
        # silently unhandled.
        return _email_missing_docs(invoice_data, po_data, grn_data, matching,
                                   summary, decision_info or {})
    else:
        return _email_review_or_rejected(invoice_data, po_data, grn_data, matching,
                                         summary, decision_info or {}, reviewer_info,
                                         final_status, variance_threshold)


def send_result_email(invoice_data, po_data, grn_data, matching,
                      final_status, summary, decision_info=None,
                      reviewer_info=None, recipient=None,
                      variance_threshold=2.0):
    recipient = recipient or RECIPIENT
    inv_num   = (invoice_data or {}).get("invoice_number", "unknown")
    vendor    = (invoice_data or {}).get("vendor_name", "")
    subject   = _build_subject(final_status, inv_num, vendor,
                               matching or {}, decision_info)
    body      = _build_full_email(invoice_data or {}, po_data or {},
                                  grn_data or {}, matching or {},
                                  final_status, summary, decision_info,
                                  reviewer_info, variance_threshold)

    print("\n" + "="*55)
    print("  SENDING RESULT EMAIL")
    print("="*55)
    print(f"  To      : {recipient}")
    print(f"  Subject : {subject}")

    try:
        from mail_provider import send_html_email, provider_label
        send_html_email(
            subject   = subject,
            html_body = f"<html><head></head><body>{body}</body></html>",
            to        = recipient,
        )
        print(f"  ✅ Email sent via {provider_label()}")
        return True
    except ImportError:
        print("  ⚠️  mail_provider not available — saving HTML file")
    except Exception as e:
        print(f"  ⚠️  Mail send error: {e} — saving HTML file")

    try:
        out_dir = Path("/tmp/data/notifications") if os.getenv("VERCEL") else Path("data/notifications")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^\w-]", "_", inv_num)
        path = out_dir / f"result_{ts}_{safe}.html"
        path.write_text(
            f"<!-- TO: {recipient} -->\n<!-- SUBJECT: {subject} -->\n"
            f"<html><head></head><body>{body}</body></html>",
            encoding="utf-8"
        )
        print(f"  ✅ HTML saved: {path.name}")
    except OSError:
        print("  ⚠️  Could not save a local fallback copy (read-only filesystem) — result email was only logged above.")
    return True


def send_review_request_email(invoice_data, matching, decision_info,
                               recipient=None, token=None):
    """
    HITL reviewer request — includes full diff so reviewer can decide
    from email alone without opening any source documents.

    token: when provided, the email includes clickable one-click
    Approve/Reject/Clarify links (http://.../approve/{token} etc.) and
    a "Review with comments" link to the full web portal page —
    pointing at APPROVAL_PORTAL_URL. When omitted, falls back to the
    old "reply to this email with a keyword" instructions (no portal
    running / web mode not in use).
    """
    recipient  = recipient or RECIPIENT
    inv_num    = invoice_data.get("invoice_number", "N/A")
    vendor     = invoice_data.get("vendor_name",    "N/A")
    amount     = float(invoice_data.get("total_amount", 0))
    reasons    = decision_info.get("reasons",    [])
    mismatches = matching.get("mismatches",      [])
    scenario_label, scenario_colour = _detect_scenario(reasons, mismatches)

    subject = f"⚠️  AP Review Required — INV {inv_num} | {vendor} | {scenario_label}"

    reason_rows = ""
    for r in (reasons or mismatches or ["Please review the invoice"]):
        reason_rows += (
            f"<li style='margin-bottom:6px;color:#555'>{r}</li>"
        )

    item_diff = _line_item_diff_table(matching.get("item_results", []))
    reasons_block = _reasons_panel(reasons, mismatches)

    body = (
        f"<style>{_BASE_CSS}</style>"
        "<div class='wrap'>"
        "<div class='topbar' style='background:#e67e22'></div>"
        "<div class='hdr' style='background:#e67e22'>"
        "<h1>⚠️  AP Manual Review Required</h1>"
        f"<p>Your approval is needed for Invoice {inv_num} from {vendor}</p>"
        "</div>"
        "<div class='body'>"

        + _scenario_banner(scenario_label, scenario_colour)

        + "<div style='display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px'>"
          f"<div style='background:#fff8f0;border-radius:8px;padding:14px'>"
          f"<p style='margin:0 0 8px;font-size:11px;text-transform:uppercase;"
          f"letter-spacing:.5px;color:#888;font-weight:700'>Invoice Details</p>"
          + _kv("Invoice #", inv_num)
          + _kv("Vendor", vendor)
          + _kv("Invoice Amount", _fmt_amt(amount))
          + _kv("PO Reference", invoice_data.get("po_number","—"))
          + _kv("Invoice Date", invoice_data.get("invoice_date","—"))
          + "</div>"
          f"<div style='background:#fff8f0;border-radius:8px;padding:14px'>"
          f"<p style='margin:0 0 8px;font-size:11px;text-transform:uppercase;"
          f"letter-spacing:.5px;color:#888;font-weight:700'>Variance Summary</p>"
          + _kv("PO Amount", _fmt_amt(matching.get("po_total",0)))
          + _kv("Invoice Amount", _fmt_amt(matching.get("invoice_total",0)))
          + _kv("Variance $", _fmt_amt(matching.get("variance",0)))
          + _kv("Variance %", f"{abs(matching.get('variance_percentage',0)):.2f}%")
          + _kv("Header Match", "✅ Yes" if matching.get("header_match") else "❌ No")
          + "</div>"
          "</div>"

        + reasons_block

        + _section("📦 Line Item Detail", item_diff)

        + (_review_action_block_with_token(token, APPROVAL_PORTAL_URL)
           if token else _review_action_block_reply_only())

        + f"<div class='footer'>"
          f"AP Automation Agent &nbsp;·&nbsp; {datetime.now().strftime('%d %b %Y %H:%M')}"
          "</div>"
        + "</div></div>"
    )

    print(f"\n  Sending review request to: {recipient}")

    try:
        from mail_provider import send_html_email, provider_label
        send_html_email(
            subject   = subject,
            html_body = f"<html><head></head><body>{body}</body></html>",
            to        = recipient,
        )
        print(f"  ✅ Review request sent via {provider_label()}")
        return True
    except Exception as e:
        print(f"  ⚠️  Could not send via mail provider: {e}")
        try:
            out_dir = Path("/tmp/data/notifications") if os.getenv("VERCEL") else Path("data/notifications")
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            print("  ⚠️  Could not save a local fallback copy either (read-only filesystem) — review request was only logged above.")
            return False
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^\w-]", "_", inv_num)
        path = out_dir / f"review_request_{ts}_{safe}.html"
        path.write_text(
            f"<!-- TO: {recipient} -->\n<!-- SUBJECT: {subject} -->\n"
            f"<html><head></head><body>{body}</body></html>",
            encoding="utf-8"
        )
        print(f"  ✅ Review request saved: {path.name}")
        return True
