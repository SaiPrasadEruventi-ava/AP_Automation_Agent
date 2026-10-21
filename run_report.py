"""
run_report.py — Human-readable account of one pipeline run
============================================================
The dashboard used to dump the raw result JSON after a Run / Upload and
reload the page, so nobody could see what the engine actually did. This
module turns a pipeline result (plus the record it persisted — pending
review, invoice detail or exception) into a structured report the UI
renders as a step-by-step timeline:

    invoice received -> PO read live from SAP -> goods receipt read ->
    duplicate check (AP log + SAP) -> 13 validations -> decision ->
    SAP action (posted / parked / skipped) -> where to verify it in SAP

Pure presentation: it never changes state and never calls SAP — every
fact comes from what the pipeline already recorded.
"""

import pending_reviews
import redis_store
import summary_sender


def _money(v, cur="USD"):
    try:
        return f"{cur} {float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v or "")


def _record_for(result: dict) -> dict:
    """The persisted record that holds po/grn/matching for this result."""
    token = result.get("review_token")
    if token:
        rec = pending_reviews.get_pending_review(token)
        if rec:
            return rec
    for key in (result.get("detail_key"), result.get("invoice_number")):
        if key:
            rec = redis_store.invoice_detail_get(key)
            if rec:
                return rec
    return {}


def _step(title, status, detail="", sap=""):
    return {"title": title, "status": status, "detail": detail, "sap": sap}


def build(result: dict, source: str = "") -> dict:
    """Report dict for one result returned by run_ap / _process_extracted."""
    rec = _record_for(result)
    inv = rec.get("invoice_data") or {}
    po = rec.get("po_data") or {}
    grn = rec.get("grn_data") or {}
    matching = rec.get("matching") or {}
    decision_info = rec.get("decision_info") or {}
    status = result.get("final_status") or rec.get("final_status") or ""
    reasons = result.get("reasons") or decision_info.get("reasons") or []
    sap = result.get("sap_posting") or decision_info.get("sap_posting") or {}
    cur = inv.get("currency") or po.get("currency") or "USD"
    inv_no = result.get("invoice_number") or inv.get("invoice_number") or "—"
    po_no = result.get("po_number") or inv.get("po_number") or po.get("po_number") or ""
    erp = po.get("erp_source") or "SAP"
    joined = " ".join(reasons).lower()

    steps = []
    # 1 — invoice
    if inv.get("invoice_number"):
        reader = {"llm": "read by the AI reader (Mistral)",
                  "rules": "read by the rule-based reader" + (f" — {inv['_extractor_note']}"
                                                              if inv.get("_extractor_note") else "")
                  }.get(inv.get("_extractor"), "")
        steps.append(_step("Invoice received", "warn" if inv.get("_extractor_note") else "ok",
                           f"{inv_no} · {inv.get('vendor_name', '')} · {_money(inv.get('total_amount'), cur)} · "
                           f"dated {inv.get('invoice_date', '')} · PO ref {po_no}" + (f" — {source}" if source else "")
                           + (f" · {reader}" if reader else "")))
    else:
        steps.append(_step("Invoice received", "fail", reasons[0] if reasons else "Invoice could not be read"))

    # 2 — PO
    if po.get("po_number"):
        steps.append(_step("Purchase order read live from SAP", "ok",
                           f"PO {po['po_number']} · {po.get('vendor_name', '')} ({po.get('vendor_code', '')}) · "
                           f"status {po.get('po_status', '')} · {_money(po.get('po_amount'), cur)} · "
                           f"{len(po.get('items', []))} line(s) · {erp}",
                           f"ME23N → PO {po['po_number']}"))
    elif inv.get("invoice_number"):
        why = next((r for r in reasons if "po" in r.lower() or "erp" in r.lower()), "PO not found in SAP")
        steps.append(_step("Purchase order read live from SAP", "fail", why))

    # 3 — GRN
    if po.get("po_number"):
        if grn:
            items = "; ".join(f"{i['item']}: received {i['quantity_received']:g}, accepted {i['quantity_accepted']:g}"
                              for i in grn.get("items", []))
            steps.append(_step("Goods receipt read from SAP", "ok",
                               f"Material doc {grn.get('grn_number')} · received {grn.get('received_date')} by "
                               f"{grn.get('received_by', '')} · {grn.get('status', '')} — {items}",
                               f"MB03 → {grn.get('grn_number', '').split(',')[0]}"))
        else:
            steps.append(_step("Goods receipt read from SAP", "fail",
                               "No goods receipt posted in SAP for this PO — goods not received yet",
                               f"ME23N → PO {po['po_number']} has no Purchase Order History tab"))

    ran_match = bool(matching.get("item_results") is not None and matching)
    # 4 — duplicate
    if ran_match or "duplicate" in joined:
        dup = next((r for r in reasons if r.startswith("Duplicate Invoice")), "")
        steps.append(_step("Duplicate check — AP log + SAP invoice table (RBKP)",
                           "fail" if dup else "ok",
                           dup or "No earlier invoice with this number in the AP log or in SAP",
                           f"MIR5 → Reference {inv_no}" if dup else ""))

    # 5 — validations
    checks = []
    if ran_match:
        flags = summary_sender.evaluate_checks([], reasons, matching, inv, po)
        checks = [{"name": n, "desc": d, "failed": bool(flags.get(n))} for n, d in summary_sender._ALL_CHECKS]
        failed = [c["name"] for c in checks if c["failed"]]
        steps.append(_step(f"3-way match + {len(checks)} validations",
                           "warn" if failed else "ok",
                           (f"{len(checks) - len(failed)} passed · {len(failed)} failed: {', '.join(failed)}"
                            if failed else f"All {len(checks)} checks passed"),
                           ""))

    # 6 — decision
    tone = ("ok" if status in ("Perfect Match", "Approved by Reviewer") else
            "fail" if status.startswith(("Incomplete", "Error", "Rejected")) else "warn")
    shown = "Manual Review — waiting for a reviewer" if status == "Pending Web Review" else status
    steps.append(_step("Decision", tone, shown))

    # 7 — SAP action
    action = sap.get("action", "")
    doc = sap.get("document", "")
    if action == "Posted":
        steps.append(_step("Supplier invoice POSTED in SAP", "ok",
                           f"Invoice document {doc} / {sap.get('fiscal_year', '')} · company code "
                           f"{sap.get('company_code', '')}"
                           + (f" · payment block {sap['payment_block']}" if sap.get("payment_block") else ""),
                           f"MIR4 → {doc} / {sap.get('fiscal_year', '')}"))
    elif action == "Parked":
        steps.append(_step("Supplier invoice PARKED in SAP (waiting for review)", "warn",
                           f"Parked document {doc} / {sap.get('fiscal_year', '')} — Approve will post it, "
                           f"Reject will delete it",
                           f"MIR4 → {doc} (title shows Parked) · MIR6 → Parked"))
    elif action == "Deleted":
        steps.append(_step("Parked invoice DELETED in SAP", "info", f"Document {doc}", f"MIR6 → {doc} no longer listed"))
    elif action == "Skipped":
        steps.append(_step("Nothing created in SAP", "info", "; ".join(sap.get("messages") or [])))
    elif action == "Simulated":
        steps.append(_step("SAP simulation only (SAP_INVOICE_MODE=simulate)", "info", "Nothing written to SAP"))
    elif action == "Failed":
        steps.append(_step("SAP rejected the posting", "fail", sap.get("error", "")))
    elif status.startswith(("Incomplete", "Error")):
        steps.append(_step("Nothing created in SAP", "info", "Processing stopped before the SAP step"))

    # where it ended up
    where = ("Pending Review" if status == "Pending Web Review" else
             "Exceptions" if status.startswith(("Incomplete", "Error")) else "Processed / Approved")
    steps.append(_step("Recorded on the dashboard", "ok", f"Listed under {where}"))

    links = []
    if result.get("review_token"):
        links.append({"label": "Open review (Approve / Reject)", "href": f"/review/{result['review_token']}", "primary": True})
    if redis_store.invoice_detail_get(result.get("detail_key") or inv_no):
        links.append({"label": "Full validation detail", "href": f"/dashboard/invoice/{result.get('detail_key') or inv_no}"})
    if po_no:
        links.append({"label": f"PO {po_no} live in SAP", "href": f"/dashboard/erp?po={po_no}"})

    if action == "Posted":
        nxt = f"Verify in SAP: MIR4 → invoice document {doc}, fiscal year {sap.get('fiscal_year', '')}."
    elif action == "Parked":
        nxt = (f"Open the review and Approve (SAP posts {doc}) or Reject (SAP deletes {doc}). "
               f"Before deciding you can see it in SAP: MIR4 → {doc}.")
    elif "extraction" in status.lower() or "could not be read" in joined or "could not extract" in joined:
        nxt = ("The PDF could not be read. If the message says HTTP 429 / rate limit, the Mistral account's quota "
               "is used up — wait or use another MISTRAL_API_KEY. Scanned/image PDFs need OCR. "
               "Meanwhile use Run on the demo case, then Reset and try the upload again.")
    elif not grn and po.get("po_number"):
        nxt = "Post the goods receipt in SAP (MIGO, mvt 101) for this PO, then Reset and run again."
    elif status.startswith(("Incomplete", "Error")):
        nxt = "Fix the cause shown above, then Reset and run again."
    elif "duplicate" in joined:
        nxt = f"Expected for a resubmitted invoice. SAP still holds only the original — check MIR5 → Reference {inv_no}."
    else:
        nxt = ""

    return {
        "invoice_number": inv_no,
        "vendor": inv.get("vendor_name", ""),
        "amount": _money(inv.get("total_amount"), cur) if inv.get("total_amount") is not None else "",
        "po_number": po_no,
        "status": shown,
        "tone": tone,
        "steps": steps,
        "checks": checks,
        "reasons": reasons,
        "sap": {k: sap.get(k) for k in ("action", "document", "fiscal_year", "payment_block", "error")} if sap else {},
        "links": links,
        "next": nxt,
    }


def build_reset(out: dict) -> dict:
    """Report for run_ap.reset_invoice()."""
    steps = []
    for r in out.get("sap", []):
        if r["action"] == "Reversed":
            steps.append(_step(f"Posted invoice {r['document']} REVERSED in SAP", "ok",
                               f"Reversal document {r.get('reversal_document', '')}",
                               f"MIR4 → {r['document']} shows the reversal · MIR5 → Reference {out['invoice_number']}"))
        elif r["action"] == "Deleted":
            steps.append(_step(f"Parked invoice {r['document']} DELETED in SAP", "ok", "",
                               "MIR6 → Parked: no longer listed"))
        elif r["action"] == "Failed":
            steps.append(_step(f"SAP undo failed for {r.get('document', '')}", "fail", r.get("error", "")))
    if not out.get("sap"):
        steps.append(_step("Nothing to undo in SAP", "info", "No posted or parked invoice from this agent"))
    removed = out.get("removed") or []
    steps.append(_step("AP logs cleared", "ok", ", ".join(removed) if removed else "nothing was logged"))
    return {"invoice_number": out["invoice_number"], "status": "Reset — ready to run again", "tone": "ok",
            "steps": steps, "checks": [], "reasons": [], "links": [], "sap": {},
            "next": "The case can be run again now."}
