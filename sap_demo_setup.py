"""
sap_demo_setup.py — Create the demo cases in SAP + their invoice PDFs
=======================================================================
For every scenario in demo_scenarios.py this creates REAL documents in
the connected SAP system and then writes the vendor invoice that goes
with it:

    1. Purchase order        BAPI_PO_CREATE1 (+ freight FVA1 / discount
                             DRV1 pricing conditions) -> commit
    2. Goods receipt(s)      BAPI_GOODSMVT_CREATE, mvt 101 (full or
                             partial, with item text)
    3. QC return             BAPI_GOODSMVT_CREATE, mvt 122 with a return
                             reason, referencing the GR (S10)
    4. PO closure            BAPI_PO_CHANGE: delivery completed + final
                             invoice indicators (S12)
    5. Invoice PDF + JSON    demo_invoices/<invoice#>.pdf — references the
                             real SAP PO number and the SAP supplier name;
                             the .json beside it is exactly what the
                             extractor should read from the PDF (used by
                             smoke_test_sap.py to run without the LLM)

The scenario -> SAP document mapping is kept in
data/sap_demo/registry.json, which the dashboard's ERP page uses to list
the demo POs. Re-running is idempotent: a scenario whose PO still exists
in SAP is skipped (use --force to create a fresh set).

Usage
-----
  python sap_demo_setup.py                 create missing cases + PDFs
  python sap_demo_setup.py --only S01,S10  just these scenarios
  python sap_demo_setup.py --force         new POs even if registered
  python sap_demo_setup.py --pdfs          re-render PDFs/JSON only
                                           (re-dates invoices to today)
  python sap_demo_setup.py --list          registry + live SAP status
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import erp_sap
import sap_connector as sap
from demo_scenarios import ORG, SCENARIOS, TAX_LABEL, by_id
from sap_connector import dec

ROOT          = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "data" / "sap_demo" / "registry.json"
INVOICE_DIR   = ROOT / "demo_invoices"

# SAP internal unit -> how a vendor prints it on the invoice
_PRINT_UNIT = {"PAK": "PACK", "ROL": "ROLL", "CS": "CASE", "ST": "PC"}


# ─────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────

def load_registry() -> dict:
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"scenarios": {}}


def save_registry(reg: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    reg["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2), encoding="utf-8")


def registered_po_numbers() -> list:
    reg = load_registry()
    return [v["po_number"] for _, v in sorted(reg.get("scenarios", {}).items())
            if v.get("po_number")]


def scenario_for_po(po_number: str) -> dict:
    for sid, v in load_registry().get("scenarios", {}).items():
        if v.get("po_number") == str(po_number).lstrip("0"):
            return {"id": sid, **v}
    return {}


# ─────────────────────────────────────────────────────────────────────
# SAP document creation
# ─────────────────────────────────────────────────────────────────────

def _bapi_or_raise(res: dict, what: str) -> list:
    msgs = sap.bapi_messages(res.get("RETURN", []))
    errors = [f"{m['message']} ({m['id']}/{m['number']})" for m in msgs
              if m["type"] in ("E", "A", "X")]
    if errors:
        raise sap.SapBapiError(f"{what} failed: " + "; ".join(errors), msgs)
    return msgs


def create_po(conn, scn: dict, testrun: bool = False) -> str:
    doc_date = date.today() - timedelta(days=scn.get("po_days_ago", 0))
    header = {"COMP_CODE": ORG["company_code"], "DOC_TYPE": ORG["doc_type"],
              "VENDOR": scn["vendor"], "PURCH_ORG": ORG["purch_org"],
              "PUR_GROUP": ORG["purch_group"], "CURRENCY": ORG["currency"],
              "PMNTTRMS": scn["terms"], "DOC_DATE": doc_date.strftime("%Y%m%d"),
              "OUR_REF": f"AP demo {scn['id']}"[:12]}
    items, itemsx, acct, acctx, sched, schedx, cond, condx = ([] for _ in range(8))
    deliv = date.today().strftime("%m/%d/%Y")
    for n, (text, qty, unit, price, tax) in enumerate(scn["lines"], 1):
        no = f"{n * 10:05d}"
        items.append({"PO_ITEM": no, "SHORT_TEXT": text[:40], "PLANT": ORG["plant"],
                      "MATL_GROUP": ORG["material_group"], "QUANTITY": dec(qty),
                      "PO_UNIT": unit, "NET_PRICE": dec(price), "PRICE_UNIT": dec(1),
                      "ACCTASSCAT": "K", "TAX_CODE": tax, "GR_IND": "X", "IR_IND": "X",
                      "GR_BASEDIV": "X"})
        itemsx.append({**{k: "X" for k in items[-1]}, "PO_ITEM": no, "PO_ITEMX": "X"})
        acct.append({"PO_ITEM": no, "SERIAL_NO": "01", "GL_ACCOUNT": ORG["gl_account"],
                     "COSTCENTER": ORG["cost_center"]})
        acctx.append({"PO_ITEM": no, "SERIAL_NO": "01", "PO_ITEMX": "X", "SERIAL_NOX": "X",
                      "GL_ACCOUNT": "X", "COSTCENTER": "X"})
        sched.append({"PO_ITEM": no, "SCHED_LINE": "0001", "DELIVERY_DATE": deliv,
                      "QUANTITY": dec(qty)})
        schedx.append({"PO_ITEM": no, "SCHED_LINE": "0001", "PO_ITEMX": "X",
                       "SCHED_LINEX": "X", "DELIVERY_DATE": "X", "QUANTITY": "X"})

    def _cond(itm, ctype, value):
        cond.append({"ITM_NUMBER": f"{itm:06d}", "COND_TYPE": ctype, "COND_VALUE": dec(value),
                     "CURRENCY": ORG["currency"], "CHANGE_ID": "I"})
        condx.append({"ITM_NUMBER": f"{itm:06d}", "ITM_NUMBERX": "X", "COND_TYPE": "X",
                      "COND_VALUE": "X", "CURRENCY": "X", "CHANGE_ID": "X"})

    if scn.get("freight"):
        _cond(10, "FVA1", scn["freight"])
    if scn.get("discount"):
        _cond(10, "DRV1", -abs(scn["discount"]))

    res = sap.call(conn, "BAPI_PO_CREATE1",
                   POHEADER=header, POHEADERX={k: "X" for k in header},
                   POITEM=items, POITEMX=itemsx, POACCOUNT=acct, POACCOUNTX=acctx,
                   POSCHEDULE=sched, POSCHEDULEX=schedx, POCOND=cond, POCONDX=condx,
                   TESTRUN="X" if testrun else "")
    try:
        _bapi_or_raise(res, f"{scn['id']} PO create")
        if testrun:
            return "TESTRUN-OK"
    except sap.SapError:
        sap.rollback(conn)
        raise
    po = str(res.get("EXPPURCHASEORDER", "")).strip()
    if not po:
        sap.rollback(conn)
        raise sap.SapError(f"{scn['id']}: BAPI_PO_CREATE1 returned no PO number")
    sap.commit(conn)
    return po


def _goodsmvt(conn, header: dict, items: list, what: str) -> tuple:
    res = sap.call(conn, "BAPI_GOODSMVT_CREATE", GOODSMVT_HEADER=header,
                   GOODSMVT_CODE={"GM_CODE": "01"}, GOODSMVT_ITEM=items)
    try:
        _bapi_or_raise(res, what)
    except sap.SapError:
        sap.rollback(conn)
        raise
    doc = str(res.get("MATERIALDOCUMENT", "")).strip()
    year = str(res.get("MATDOCUMENTYEAR", "")).strip()
    if not doc:
        sap.rollback(conn)
        raise sap.SapError(f"{what}: no material document returned")
    sap.commit(conn)
    return doc, year


def post_receipts(conn, scn: dict, po: str) -> list:
    """Goods receipt for the scenario, then any QC returns against it."""
    plan = scn.get("gr") or []
    if not plan:
        return []
    today = date.today().strftime("%Y%m%d")
    header = {"PSTNG_DATE": sap.posting_date(conn, ORG["company_code"]), "DOC_DATE": today,
              "REF_DOC_NO": f"DN-{scn['id']}-{po[-4:]}", "HEADER_TXT": f"AP demo {scn['id']}"}
    items, item_po = [], []
    for n, (line, gr) in enumerate(zip(scn["lines"], plan), 1):
        if not gr.get("qty"):
            continue
        it = {"PO_NUMBER": po, "PO_ITEM": f"{n * 10:05d}", "MOVE_TYPE": "101", "MVT_IND": "B",
              "ENTRY_QNT": dec(gr["qty"]), "ENTRY_UOM": line[2]}
        if gr.get("text"):
            it["ITEM_TEXT"] = gr["text"][:50]
        items.append(it)
        item_po.append((n, gr))
    docs = []
    gr_doc, gr_year = _goodsmvt(conn, header, items, f"{scn['id']} goods receipt")
    docs.append({"type": "101", "document": gr_doc, "year": gr_year})

    returns = []
    for pos, (n, gr) in enumerate(item_po, 1):
        if gr.get("return"):
            qty, reason, text = gr["return"]
            returns.append({"PO_NUMBER": po, "PO_ITEM": f"{n * 10:05d}", "MOVE_TYPE": "122",
                            "MVT_IND": "B", "ENTRY_QNT": dec(qty),
                            "ENTRY_UOM": scn["lines"][n - 1][2], "MOVE_REAS": reason,
                            "ITEM_TEXT": text[:50], "REF_DOC_YR": gr_year,
                            "REF_DOC": gr_doc, "REF_DOC_IT": f"{pos:04d}"})
    if returns:
        rdoc, ryear = _goodsmvt(conn, {**header, "HEADER_TXT": f"QC return {scn['id']}"},
                                returns, f"{scn['id']} QC return")
        docs.append({"type": "122", "document": rdoc, "year": ryear})
    return docs


def close_po(conn, po: str, scn: dict) -> None:
    items = [{"PO_ITEM": f"{n * 10:05d}", "NO_MORE_GR": "X", "FINAL_INV": "X"}
             for n in range(1, len(scn["lines"]) + 1)]
    itemsx = [{"PO_ITEM": i["PO_ITEM"], "PO_ITEMX": "X", "NO_MORE_GR": "X", "FINAL_INV": "X"}
              for i in items]
    res = sap.call(conn, "BAPI_PO_CHANGE", PURCHASEORDER=po, POITEM=items, POITEMX=itemsx)
    try:
        _bapi_or_raise(res, f"{scn['id']} PO close")
    except sap.SapError:
        sap.rollback(conn)
        raise
    sap.commit(conn)


def create_scenario(scn: dict, existing_po: str = None) -> dict:
    """Create the PO (unless `existing_po` is given — then only the
    follow-up postings that previously failed are retried)."""
    if existing_po:
        po = existing_po
        print(f"  ↻  {scn['id']}: PO {po} exists — retrying goods movements")
    else:
        with sap.write_session() as conn:
            po = create_po(conn, scn)
        print(f"  ✅ {scn['id']}: PO {po} created")
    # Goods movements on a FRESH RFC session: BAPI_GOODSMVT_CREATE in the
    # same session as BAPI_PO_CREATE1 dumps on A4F (ITAB_DUPLICATE_KEY —
    # the PO save leaves MM buffers behind). See sap.write_session.
    with sap.write_session() as conn:
        try:
            docs = post_receipts(conn, scn, po)
            for d in docs:
                print(f"     mvt {d['type']} → material document {d['document']}/{d['year']}")
            if scn.get("close_po"):
                close_po(conn, po, scn)
                print("     PO closed (delivery completed + final invoice)")
        except sap.SapError as e:
            # Keep the PO in the registry so a re-run doesn't duplicate it;
            # the error tells the operator what to fix (often master data).
            print(f"  ❌ {scn['id']}: follow-up posting failed: {e}")
            return {"po_number": po, "documents": [], "error": str(e)}
    return {"po_number": po, "documents": docs, "error": ""}


# ─────────────────────────────────────────────────────────────────────
# Invoice (data + PDF)
# ─────────────────────────────────────────────────────────────────────

def build_invoice(scn: dict, po: dict, issued: date = None) -> dict:
    """The vendor's invoice for this scenario, derived from the LIVE SAP
    PO (prices, tax rate, freight, discount, terms) plus the scenario's
    deliberate deviations."""
    dev = scn.get("invoice", {})
    issued = (issued or date.today()) + timedelta(days=dev.get("date_offset", 0))
    lines = []
    for n, line in enumerate([l for l in po["items"] if not l["deleted"]]):
        price = dev.get("prices", {}).get(n, line["unit_price"])
        qty = line["quantity"]
        amount = round(qty * price, 2)
        lines.append({
            "item":       line["item"],
            "quantity":   int(qty) if qty == int(qty) else qty,
            "uom":        dev.get("uoms", {}).get(n, _PRINT_UNIT.get(line["sap_uom"], line["uom"])),
            "unit_price": round(price, 2),
            "amount":     dev.get("line_amounts", {}).get(n, amount),
        })
    subtotal = round(sum(l["amount"] for l in lines), 2)
    discount = round(po.get("discount", 0), 2)
    rate = dev.get("tax_rate", po.get("tax_rate") or 0.0)
    tax = round((subtotal - discount) * rate / 100, 2)
    freight = round(dev.get("freight", po.get("freight", 0)), 2)
    total = round(subtotal - discount + tax + freight + dev.get("total_delta", 0), 2)
    tax_label = (f"{TAX_LABEL} @ {rate:.2f}%" if rate else "Sales Tax Exempt @ 0.00%")
    po_no = po["po_number"]
    return {
        "invoice_number": f"INV-{scn['id']}-{po_no[-4:]}",
        "vendor_name":    dev.get("vendor_name", po["vendor_name"]),
        "invoice_date":   issued.strftime("%Y-%m-%d"),
        "po_number":      po_no,
        "currency":       po["currency"],
        "payment_terms":  dev.get("terms", po["payment_terms"]),
        "tax_type":       tax_label,
        "subtotal":       subtotal,
        "tax":            tax,
        "freight":        freight,
        "discount":       discount,
        "total_amount":   total,
        "items":          lines,
    }


def render_pdf(inv: dict, scn: dict, path: Path, vendor_city: str = "") -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ss = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    money = lambda v: f"${v:,.2f}"
    story = [
        Paragraph(f"<b>{inv['vendor_name']}</b>", ss["Title"]),
        Paragraph(vendor_city or "", ss["Normal"]),
        Spacer(1, 6),
        Paragraph("<b>INVOICE</b>", ss["Heading1"]),
    ]
    meta = [
        ["Invoice Number", inv["invoice_number"], "Invoice Date", inv["invoice_date"]],
        ["Vendor Name", inv["vendor_name"], "PO Reference", inv["po_number"]],
        ["Currency", inv["currency"], "Payment Terms", inv["payment_terms"]],
        ["Bill To", Paragraph(ORG["bill_to"], ss["Normal"]), "Tax Type", inv["tax_type"]],
    ]
    t = Table(meta, colWidths=[30 * mm, 60 * mm, 30 * mm, 52 * mm])
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                           ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story += [t, Spacer(1, 10), Paragraph("<b>LINE ITEMS</b>", ss["Heading3"])]

    rows = [["Description", "Qty", "UOM", "Unit Price", "Line Amount"]]
    for l in inv["items"]:
        rows.append([l["item"], f"{l['quantity']:g}", l["uom"], money(l["unit_price"]),
                     money(l["amount"])])
    lt = Table(rows, colWidths=[78 * mm, 18 * mm, 18 * mm, 28 * mm, 30 * mm])
    lt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B1220")),
                            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C7CCD6"))]))
    story += [lt, Spacer(1, 10)]

    totals = [["Subtotal", money(inv["subtotal"])]]
    if inv["discount"]:
        totals.append(["Discount", "-" + money(inv["discount"])])
    totals += [[f"Tax ({inv['tax_type']})", money(inv["tax"])],
               ["Freight / Delivery", money(inv["freight"])],
               ["TOTAL AMOUNT DUE", money(inv["total_amount"])]]
    tt = Table(totals, colWidths=[120 * mm, 52 * mm])
    tt.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"),
                            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                            ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black)]))
    story += [tt, Spacer(1, 18),
              Paragraph(f"<font size=8 color='#8A93A6'>Please quote the invoice number and PO "
                        f"reference on all correspondence. Demo case {scn['id']} — "
                        f"{scn['title']}.</font>", ss["Normal"])]
    doc.build(story)


def write_invoice_files(scn: dict, po_number: str, issued: date = None) -> dict:
    with sap.session() as conn:
        po = erp_sap.po_with_history(conn, po_number)
    if not po:
        raise sap.SapError(f"{scn['id']}: PO {po_number} not found in SAP")
    inv = build_invoice(scn, po, issued)
    INVOICE_DIR.mkdir(exist_ok=True)
    pdf = INVOICE_DIR / f"{inv['invoice_number']}.pdf"
    render_pdf(inv, scn, pdf, po.get("vendor_city", ""))
    (INVOICE_DIR / f"{inv['invoice_number']}.json").write_text(
        json.dumps({"scenario": scn["id"], "expected": scn["expected"],
                    "invoice_data": inv}, indent=2), encoding="utf-8")
    return {"invoice_number": inv["invoice_number"], "pdf": str(pdf.relative_to(ROOT)),
            "total": inv["total_amount"], "vendor_name": inv["vendor_name"]}


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def _po_exists(po_number: str) -> bool:
    try:
        return bool(erp_sap.lookup_po(po_number))
    except sap.SapError:
        return False


def run(only=None, force=False, pdfs_only=False) -> dict:
    reg = load_registry()
    reg.setdefault("scenarios", {})
    reg["system"] = sap.public_config()
    targets = [by_id(s) for s in only] if only else SCENARIOS

    print("=" * 65)
    print(f"  SAP demo setup — {erp_sap.source_label()}  ({len(targets)} scenario(s))")
    print("=" * 65)
    for scn in targets:
        sid = scn["id"]
        entry = reg["scenarios"].get(sid, {})
        if not pdfs_only and (force or not entry.get("po_number")
                              or not _po_exists(entry["po_number"])):
            try:
                entry = {**create_scenario(scn), "title": scn["title"],
                         "expected": scn["expected"], "created_at":
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            except sap.SapError as e:
                print(f"  ❌ {sid}: {e}")
                entry = {**entry, "error": str(e)}
                reg["scenarios"][sid] = entry
                save_registry(reg)
                continue
        elif not pdfs_only and entry.get("error") and not entry.get("documents"):
            entry = {**entry, **create_scenario(scn, existing_po=entry["po_number"])}
        elif not pdfs_only:
            print(f"  ↺  {sid}: PO {entry['po_number']} already in SAP — reusing")
        if entry.get("po_number"):
            try:
                files = write_invoice_files(scn, entry["po_number"])
                entry.update(files)
                print(f"     invoice {files['invoice_number']} → {files['pdf']} "
                      f"(${files['total']:,.2f})")
            except sap.SapError as e:
                print(f"  ❌ {sid}: invoice PDF: {e}")
        reg["scenarios"][sid] = entry
        save_registry(reg)
    print(f"\nRegistry: {REGISTRY_PATH.relative_to(ROOT)}   Invoices: {INVOICE_DIR.name}/")
    return reg


def list_status() -> None:
    reg = load_registry()
    for sid, v in sorted(reg.get("scenarios", {}).items()):
        po = v.get("po_number", "—")
        try:
            p, g = erp_sap.lookup_po_and_grn(po) if po != "—" else ({}, {})
            inv = erp_sap.find_supplier_invoices(po_number=po) if p else []
        except sap.SapError as e:
            print(f"{sid}  PO {po}  SAP error: {e}")
            continue
        live = [f"{d['document']} {d['status']}" for d in inv if d["status"] != "Reversed"]
        print(f"{sid}  PO {po:<11} {p.get('po_status', 'NOT FOUND'):<15} "
              f"GR {g.get('grn_number', '—'):<12} invoice {v.get('invoice_number', '—'):<16} "
              f"SAP invoices: {', '.join(live) or 'none'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated scenario ids, e.g. S01,S10")
    ap.add_argument("--force", action="store_true", help="create new POs even if registered")
    ap.add_argument("--pdfs", action="store_true", help="re-render invoice PDFs/JSON only")
    ap.add_argument("--list", action="store_true", help="show registry with live SAP status")
    a = ap.parse_args()
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    if a.list:
        list_status()
    else:
        run(only=[s.strip() for s in a.only.split(",")] if a.only else None,
            force=a.force, pdfs_only=a.pdfs)
