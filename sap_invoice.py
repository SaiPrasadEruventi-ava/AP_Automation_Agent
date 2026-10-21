"""
sap_invoice.py — Supplier-invoice postings in SAP (MIRO via BAPI)
===================================================================
Makes every AP-engine outcome visible in SAP itself:

    Perfect Match          -> invoice POSTED against the PO / GR
                              (BAPI_INCOMINGINVOICE_CREATE1)
    Manual Review          -> invoice PARKED in SAP with the agent's
                              reason in the header text, so AP sees it in
                              MIR4 / MIR6 (BAPI_INCOMINGINVOICE_PARK)
    Reviewer approves      -> the parked invoice is POSTED
                              (BAPI_INCOMINGINVOICE_POST)
    Reviewer rejects       -> the parked invoice is DELETED
                              (BAPI_INCOMINGINVOICE_DELETE)
    Demo reset             -> posted invoice REVERSED, parked deleted
                              (BAPI_INCOMINGINVOICE_CANCEL / _DELETE), so
                              the same invoice number can be run again

Every create is SIMULATED first (HEADERDATA-SIMULATION = 'X' — SAP runs
all checks, builds the accounting document, writes nothing). Only if the
simulation has no E/A message is the real posting made and committed,
on the same connection (a BAPI's LUW commits only on the connection that
opened it). Posting failures roll back and come back as a result with
action "Failed" and SAP's own messages — never a fabricated document
number.

SAP_INVOICE_MODE (.env):
    post      (default) — post / park for real
    simulate  — run SAP's simulation only; nothing is written to SAP
    off       — skip SAP invoice integration entirely

Business validation is NOT repeated here: callers (run_ap.py) only post
once the 3-way match has passed, or a reviewer has explicitly approved.
"""

import os
import re
from datetime import date, datetime

import erp_sap
import sap_connector as sap
from sap_connector import dec, sap_date

FREIGHT_TAX_CODE = os.getenv("SAP_FREIGHT_TAX_CODE", "I0")
REVERSAL_REASON  = os.getenv("SAP_REVERSAL_REASON", "01")


def invoice_mode() -> str:
    return os.getenv("SAP_INVOICE_MODE", "post").strip().lower()


def enabled() -> bool:
    return (os.getenv("AP_ERP_SOURCE", "mock").strip().lower() == "sap"
            and invoice_mode() in ("post", "simulate"))


def _norm(text) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower()).split())


def _result(action, **kw) -> dict:
    return {
        "action":       action,
        "document":     kw.get("document", ""),
        "fiscal_year":  kw.get("fiscal_year", ""),
        "company_code": kw.get("company_code", ""),
        "messages":     kw.get("messages", []),
        "error":        kw.get("error", ""),
        "payment_block": kw.get("payment_block", ""),
        "simulation":   kw.get("simulation", []),
        "system":       erp_sap.source_label(),
        "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode":         invoice_mode(),
    }


def _match_po_line(name: str, lines: list) -> dict:
    key = _norm(name)
    for line in lines:
        if _norm(line["item"]) == key:
            return line
    # tolerate truncation: SAP short text is 40 chars
    for line in lines:
        k = _norm(line["item"])
        if k and (key.startswith(k) or k.startswith(key)):
            return line
    return {}


def _split(total: float, weights: list) -> list:
    """Split `total` across weights, putting the rounding remainder on
    the last share so the parts always add back up exactly."""
    s = sum(weights) or 1.0
    parts = [round(total * w / s, 2) for w in weights]
    if parts:
        parts[-1] = round(total - sum(parts[:-1]), 2)
    return parts


def build_payload(invoice_data: dict, po: dict, header_text: str = "",
                  posting_date: str = None) -> dict:
    """
    Translate an extracted invoice + the live SAP PO (with history) into
    BAPI_INCOMINGINVOICE_CREATE1 HEADERDATA / ITEMDATA.
    Returns {"header", "items", "notes"}; raises ValueError when the
    invoice can't be expressed against this PO at all.
    """
    lines = [l for l in po["items"] if not l["deleted"]]
    history = po.get("_history") or []
    inv_items = invoice_data.get("items") or []
    notes = []

    matched = []
    for it in inv_items:
        line = _match_po_line(it.get("item", ""), lines)
        if not line:
            notes.append(f"Invoice line '{it.get('item')}' has no matching PO line — not posted")
            continue
        qty = float(it.get("quantity") or 0)
        amount = round(qty * float(it.get("unit_price") or 0), 2)
        matched.append((line, qty, amount))
    if not matched:
        raise ValueError("No invoice line matches a line on SAP PO "
                         f"{po['po_number']} — nothing to post")

    inv_discount = float(invoice_data.get("discount") or 0)
    discount_parts = _split(inv_discount, [m[2] for m in matched]) if inv_discount else [0.0] * len(matched)

    items, n = [], 0
    for (line, qty, amount), disc in zip(matched, discount_parts):
        net = round(amount - disc, 2)
        base = {
            "PO_NUMBER":  sap.alpha(po["po_number"]),
            "PO_ITEM":    line["po_item"],
            "TAX_CODE":   line["tax_code"],
            "TAXJURCODE": line["tax_jurisdiction"],
            "PO_UNIT":    line["sap_uom"],
        }
        if line["gr_based_iv"]:
            refs = [r for r in erp_sap.gr_reference_documents(history, line["po_item"])
                    if r["open_qty"] > 0]
            if not refs:
                raise ValueError(f"PO line {line['po_item']} ('{line['item']}') is GR-based "
                                 "but has no open goods receipt in SAP to invoice against")
            # Allocate the invoiced quantity to GR documents oldest first;
            # anything beyond what was received lands on the last GR (SAP
            # then flags the over-invoiced quantity and blocks payment).
            alloc, left = [], qty
            for i, r in enumerate(refs):
                take = left if i == len(refs) - 1 else min(left, r["open_qty"])
                if take > 0:
                    alloc.append((r, take))
                    left -= take
            for (r, take), part in zip(alloc, _split(net, [a[1] for a in alloc])):
                n += 1
                items.append({**base, "INVOICE_DOC_ITEM": f"{n:06d}",
                              "REF_DOC": r["ref_doc"], "REF_DOC_YEAR": r["ref_year"],
                              "REF_DOC_IT": r["ref_item"], "QUANTITY": dec(take),
                              "ITEM_AMOUNT": dec(part)})
        else:
            n += 1
            items.append({**base, "INVOICE_DOC_ITEM": f"{n:06d}",
                          "QUANTITY": dec(qty), "ITEM_AMOUNT": dec(net)})

    # ── Freight: settle the PO's planned delivery costs when it has them,
    # otherwise book the invoice freight as unplanned delivery costs ──
    inv_freight = round(float(invoice_data.get("freight") or 0), 2)
    planned = [(l, c) for l, _, _ in matched for c in l.get("freight_conditions", [])
               if c["amount"]]
    del_costs = 0.0
    if inv_freight and planned:
        for (l, c), part in zip(planned, _split(inv_freight, [c["amount"] for _, c in planned])):
            refs = [r for r in erp_sap.gr_reference_documents(history, l["po_item"])
                    if r["open_qty"] > 0] if l["gr_based_iv"] else []
            n += 1
            # SAP (M8/332): identify the planned cost EITHER by condition
            # type OR by step + counter — never both. The type is enough
            # unless the same type occurs twice on this PO line.
            same_type = [x for x in l.get("freight_conditions", []) if x["cond_type"] == c["cond_type"]]
            cond_ref = ({"COND_TYPE": c["cond_type"]} if len(same_type) == 1
                        else {"COND_ST_NO": c["step"], "COND_COUNT": c["counter"][-2:]})
            item = {"INVOICE_DOC_ITEM": f"{n:06d}", "PO_NUMBER": sap.alpha(po["po_number"]),
                    "PO_ITEM": l["po_item"], **cond_ref,
                    "TAX_CODE": FREIGHT_TAX_CODE, "TAXJURCODE": l["tax_jurisdiction"],
                    "ITEM_AMOUNT": dec(part), "QUANTITY": dec(l["quantity_received"] or l["quantity"]),
                    "PO_UNIT": l["sap_uom"]}
            if refs:
                item.update({"REF_DOC": refs[0]["ref_doc"], "REF_DOC_YEAR": refs[0]["ref_year"],
                             "REF_DOC_IT": refs[0]["ref_item"]})
            items.append(item)
    elif inv_freight:
        del_costs = inv_freight
        notes.append(f"Freight ${inv_freight:,.2f} posted as unplanned delivery costs "
                     "(PO has no freight condition)")

    inv_date = sap_date(invoice_data.get("invoice_date"))
    posting_date = posting_date or date.today().strftime("%Y%m%d")
    header = {
        "INVOICE_IND":  "X",
        "DOC_TYPE":     "RE",
        "DOC_DATE":     inv_date,
        "PSTNG_DATE":   posting_date,
        "BLINE_DATE":   inv_date,
        "REF_DOC_NO":   str(invoice_data.get("invoice_number", ""))[:16].upper(),
        "COMP_CODE":    po["company_code"],
        "CURRENCY":     po["currency"],
        "GROSS_AMOUNT": dec(round(float(invoice_data.get("total_amount") or 0), 2)),
        "CALC_TAX_IND": "X",
        "PMNTTRMS":     po.get("payment_terms_code", ""),
        "HEADER_TXT":   (header_text or "AP Agent")[:25],
        "ITEM_TEXT":    f"AP Agent · {invoice_data.get('invoice_number', '')}"[:50],
    }
    if del_costs:
        header.update({"DEL_COSTS": dec(del_costs), "DEL_COSTS_TAXC": FREIGHT_TAX_CODE,
                       "DEL_COSTS_TAXJ": lines[0]["tax_jurisdiction"]})
    return {"header": header, "items": items, "notes": notes}


def _simulate(conn, payload: dict) -> tuple:
    res = sap.call(conn, "BAPI_INCOMINGINVOICE_CREATE1",
                   HEADERDATA={**payload["header"], "SIMULATION": "X"},
                   ITEMDATA=payload["items"])
    msgs = sap.bapi_messages(res.get("RETURN", []))
    lines = [{"gl_account": sap.strip_alpha(r.get("GL_ACCOUNT", "")),
              "account_type": r.get("ACCT_TYPE", ""),
              "vendor": sap.strip_alpha(r.get("VENDOR_NO", "")),
              "po_item": r.get("PO_ITEM", ""),
              "tax_code": r.get("TAX_CODE", ""),
              "debit_credit": "Credit" if r.get("DB_CR_IND") == "H" else "Debit",
              "amount": float(r.get("ITEM_AMOUNT", 0) or 0),
              "text": r.get("ITEM_TEXT", "")}
             for r in res.get("SIMULATION_RESULT", [])]
    return msgs, lines


def _errors(msgs: list) -> list:
    return [f"{m['message']} ({m['id']}/{m['number']})" for m in msgs if m["type"] in ("E", "A", "X")]


def _payment_block(conn, doc: str, year: str) -> str:
    rows = sap.read_table(conn, "RBKP", ["ZLSPR"], [f"BELNR = '{doc}'", f"GJAHR = '{year}'"],
                          max_rows=1)
    return rows[0]["ZLSPR"] if rows else ""


def create_invoice(invoice_data: dict, po_number: str, park: bool = False,
                   header_text: str = "") -> dict:
    """
    Post (or park) the supplier invoice in SAP. Simulates first; in
    SAP_INVOICE_MODE=simulate returns after the simulation.
    """
    action = "Parked" if park else "Posted"
    try:
        with sap.write_session() as conn:
            po = erp_sap.po_with_history(conn, po_number)
            if not po:
                return _result("Failed", error=f"PO {po_number} not found in SAP")
            try:
                payload = build_payload(invoice_data, po, header_text,
                                        sap.posting_date(conn, po["company_code"]))
            except ValueError as e:
                return _result("Failed", error=str(e), company_code=po["company_code"])

            sim_msgs, sim_lines = _simulate(conn, payload)
            sim_errors = _errors(sim_msgs)
            if invoice_mode() == "simulate":
                return _result("Simulated" if not sim_errors else "Failed",
                               messages=payload["notes"] + [m["message"] for m in sim_msgs],
                               error="; ".join(sim_errors), simulation=sim_lines,
                               company_code=po["company_code"])
            # A parked document may legitimately be unbalanced or carry a
            # variance (that is why it is parked), so only a posting
            # requires a clean simulation.
            if sim_errors and not park:
                return _result("Failed", error="SAP simulation rejected the invoice: "
                               + "; ".join(sim_errors),
                               messages=payload["notes"], simulation=sim_lines,
                               company_code=po["company_code"])

            fm = "BAPI_INCOMINGINVOICE_PARK" if park else "BAPI_INCOMINGINVOICE_CREATE1"
            res = sap.call(conn, fm, HEADERDATA=payload["header"], ITEMDATA=payload["items"])
            msgs = sap.bapi_messages(res.get("RETURN", []))
            doc = str(res.get("INVOICEDOCNUMBER", "")).strip()
            year = str(res.get("FISCALYEAR", "")).strip()
            errors = _errors(msgs)
            if errors or not doc:
                sap.rollback(conn)
                return _result("Failed", error=f"SAP {action.lower()} failed: "
                               + ("; ".join(errors) or "no document number returned"),
                               messages=payload["notes"], simulation=sim_lines,
                               company_code=po["company_code"])
            sap.commit(conn)
            block = "" if park else _payment_block(conn, doc, year)
            return _result(action, document=doc, fiscal_year=year,
                           company_code=po["company_code"], payment_block=block,
                           messages=payload["notes"] + [m["message"] for m in msgs
                                                        if m["type"] in ("W", "I", "S")],
                           simulation=sim_lines)
    except sap.SapError as e:
        return _result("Failed", error=str(e))


def _doc_call(fm: str, action: str, document: str, fiscal_year: str, **extra) -> dict:
    """Run a document-level invoice BAPI + commit. A callable `extra`
    value is resolved with the live connection first (posting dates)."""
    if invoice_mode() == "simulate":
        return _result("Simulated", document=document, fiscal_year=fiscal_year,
                       messages=[f"SAP_INVOICE_MODE=simulate — {fm} not executed"])
    try:
        with sap.write_session() as conn:
            extra = {k: (v(conn) if callable(v) else v) for k, v in extra.items()}
            res = sap.call(conn, fm, INVOICEDOCNUMBER=document, FISCALYEAR=fiscal_year, **extra)
            msgs = sap.bapi_messages(res.get("RETURN", []))
            errors = _errors(msgs)
            if errors:
                sap.rollback(conn)
                return _result("Failed", document=document, fiscal_year=fiscal_year,
                               error=f"{fm}: " + "; ".join(errors))
            sap.commit(conn)
            out = _result(action, document=document, fiscal_year=fiscal_year,
                          messages=[m["message"] for m in msgs])
            if fm == "BAPI_INCOMINGINVOICE_CANCEL":
                out["reversal_document"] = str(res.get("INVOICEDOCNUMBER_REVERSAL", "")).strip()
            if fm == "BAPI_INCOMINGINVOICE_POST":
                out["payment_block"] = _payment_block(conn, document, fiscal_year)
            return out
    except sap.SapError as e:
        return _result("Failed", document=document, fiscal_year=fiscal_year, error=str(e))


def _company_of(conn, document: str, fiscal_year: str) -> str:
    rows = sap.read_table(conn, "RBKP", ["BUKRS"],
                          [f"BELNR = '{document}'", f"GJAHR = '{fiscal_year}'"], max_rows=1)
    return rows[0]["BUKRS"] if rows else ""


def post_parked(document: str, fiscal_year: str) -> dict:
    return _doc_call("BAPI_INCOMINGINVOICE_POST", "Posted", document, fiscal_year)


def delete_parked(document: str, fiscal_year: str) -> dict:
    return _doc_call("BAPI_INCOMINGINVOICE_DELETE", "Deleted", document, fiscal_year)


def reverse_posted(document: str, fiscal_year: str) -> dict:
    return _doc_call("BAPI_INCOMINGINVOICE_CANCEL", "Reversed", document, fiscal_year,
                     REASONREVERSAL=REVERSAL_REASON,
                     POSTINGDATE=lambda conn: sap.posting_date(conn, _company_of(conn, document, fiscal_year)))


def undo_invoice(reference: str, vendor: str = None) -> list:
    """
    Demo reset: find every live SAP invoice carrying this reference
    (RBKP-XBLNR) — optionally for one vendor — and reverse the posted
    ones / delete the parked ones, so the invoice number can be
    processed again from scratch. Returns one result per document.
    """
    try:
        docs = erp_sap.find_supplier_invoices(reference=reference, vendor=vendor)
    except sap.SapError as e:
        return [_result("Failed", error=str(e))]
    out = []
    # Only ever touch documents this agent created (header text set by
    # create_invoice) — never a same-numbered invoice someone keyed in MIRO.
    docs = [d for d in docs if (d.get("header_text") or "").startswith("AP Agent")]
    for d in docs:
        if d["is_posted"]:
            out.append(reverse_posted(d["document"], d["fiscal_year"]))
        elif d["is_parked"]:
            out.append(delete_parked(d["document"], d["fiscal_year"]))
    return out
