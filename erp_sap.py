"""
erp_sap.py — Live SAP ERP lookup (PO + GRN + supplier invoices)
=================================================================
Builds the exact po_data / grn_data shapes the 3-way match expects
(see erp_mock.py's lookup_po / lookup_grn docstrings) from live SAP
reads — never a cached copy. Used by erp_mock.py whenever
AP_ERP_SOURCE=sap.

Where each field comes from (S/4HANA table names; ECC equivalents in
brackets where they differ):

  PO header      EKKO   vendor (LIFNR), date (BEDAT), currency, payment
                        terms (ZTERM), deletion flag, release status
                        (FRGRL / PROCSTAT), pricing document (KNUMV)
  Vendor         LFA1   name
  Payment terms  T052 + TVZBT  -> "Net 30" style label + SAP text
  PO lines       EKPO   short text (the invoice line description is
                        matched against this), qty, unit, net price,
                        tax code + jurisdiction, deletion / delivery-
                        completed / final-invoice flags
  Units          T006A  internal unit -> commercial unit (ST -> PC)
  Pricing        PRCD_ELEMENTS [KONV]  gross price (PPR0/PBXX/PB00),
                        discounts (DRV1/DRG1/RB00/...), freight
                        (FVA1/FGW1/FQU1/FRB1/...) per line
  Tax rate       A053 (jurisdiction) or A003 (country) + KONP
  PO history     EKBE   goods receipts (VGABE 1) and invoice receipts
                        (VGABE 2) — received / returned / invoiced qty
  GR detail      BAPI_GOODSMVT_GETDETAIL  who posted it, header text,
                        item text and return reason (T157E) -> GRN
                        "condition"
  Invoices       RBKP / RSEG  supplier invoices already in SAP (used by
                        the duplicate check and the demo reset)

All public functions open (or reuse) one RFC connection for the whole
lookup — a full PO + GRN read is ~8 round trips on one logon.
"""

import threading
import time
from datetime import date, datetime

import sap_connector as sap
from sap_connector import alpha, iso_date, num, quote, read_table, read_table_in, strip_alpha

# ── Pricing condition classification (S/4 A17001 and classic RM0000) ──
PRICE_CONDITIONS    = ("PPR0", "PBXX", "PB00", "P000", "P101")
DISCOUNT_CONDITIONS = ("DRV1", "DRG1", "DRN1", "DRQ1", "DRW1", "DL01",
                       "RB00", "RA00", "RA01", "RC00", "HB01", "HA00", "RL01")
FREIGHT_CONDITIONS  = ("FVA1", "FGW1", "FQU1", "FRA1", "FRB1", "FRC1", "FRB2", "FRC2")

# Release-procedure states (EKKO-PROCSTAT, S/4) that mean "not usable yet"
_PROCSTAT_ON_HOLD = {"01": "In Process", "03": "In Release", "04": "Partially Released",
                     "08": "Rejected"}

# Goods-movement types and how they move the received / accepted quantities
_MVT_RECEIVED = {"101": 1, "102": -1, "103": 1, "104": -1}
_MVT_ACCEPTED = {"101": 1, "102": -1, "105": 1, "106": -1, "122": -1, "123": 1,
                 "161": -1, "162": 1}
_MVT_RETURNED = {"122": 1, "123": -1, "124": 1, "125": -1, "161": 1, "162": -1}


def source_label(system: str = None) -> str:
    cfg = sap.public_config(system)
    return f"{cfg.get('label', 'SAP')} · client {cfg.get('client', '?')}"


def _norm_po(po_number: str) -> str:
    return str(po_number or "").strip().upper().replace(" ", "")


# ─────────────────────────────────────────────────────────────────────
# Configuration lookups. Customizing (units, payment terms, tax rates)
# changes rarely, so it is cached for CONFIG_TTL seconds; transactional
# data (EKKO/EKPO/EKBE/pricing/RBKP) is never cached.
# ─────────────────────────────────────────────────────────────────────

CONFIG_TTL = 900
_config_cache = {}
_config_lock = threading.Lock()


def _cached(key, loader):
    now = time.time()
    with _config_lock:
        hit = _config_cache.get(key)
        if hit and now - hit[0] < CONFIG_TTL:
            return hit[1]
    value = loader()
    with _config_lock:
        _config_cache[key] = (now, value)
    return value


def _unit_texts(conn, units) -> dict:
    return _cached(("T006A", tuple(sorted(units))), lambda: _unit_texts_live(conn, units))


def _unit_texts_live(conn, units) -> dict:
    rows = read_table_in(conn, "T006A", ["MSEHI", "MSEH3", "MSEHT"], "MSEHI", units,
                         "SPRAS = 'E'")
    return {r["MSEHI"]: (r["MSEH3"] or r["MSEHI"]) for r in rows}


def _payment_terms(conn, zterm: str) -> dict:
    return _cached(("T052", zterm), lambda: _payment_terms_live(conn, zterm))


def _payment_terms_live(conn, zterm: str) -> dict:
    """Payment terms key -> {'code','label','text','net_days'}. The label
    is canonical ("Net 30", "2% 14 Days, Net 30") so it compares cleanly
    with what vendors print on invoices."""
    if not zterm:
        return {"code": "", "label": "", "text": "", "net_days": None}
    t052 = read_table(conn, "T052", ["ZTERM", "ZTAG1", "ZPRZ1", "ZTAG2", "ZPRZ2", "ZTAG3"],
                      f"ZTERM = {quote(zterm)}", max_rows=1)
    text = read_table(conn, "TVZBT", ["VTEXT"],
                      [f"ZTERM = {quote(zterm)}", "SPRAS = 'E'"], max_rows=1)
    label, net_days = "", None
    if t052:
        r = t052[0]
        d1, p1, d2, p2, d3 = (int(num(r["ZTAG1"])), num(r["ZPRZ1"]),
                              int(num(r["ZTAG2"])), num(r["ZPRZ2"]), int(num(r["ZTAG3"])))
        if p1 and d2:
            net_days = d3 or d2
            label = f"{p1:g}% {d1} Days, Net {net_days}"
        elif d1:
            net_days = d1
            label = f"Net {d1}"
        else:
            net_days = 0
            label = "Due on Receipt"
    vtext = text[0]["VTEXT"] if text else ""
    if not label:
        label = vtext or zterm
    return {"code": zterm, "label": label, "text": vtext, "net_days": net_days}


def _tax_rates(conn, country: str, pairs) -> dict:
    out = {}
    for pair in set(pairs):
        out.update(_cached(("TAX", country, pair),
                           lambda pair=pair: _tax_rates_live(conn, country, [pair])))
    return out


def _tax_rates_live(conn, country: str, pairs) -> dict:
    """{(tax_code, jurisdiction): rate_percent} from the tax condition
    records valid today. Jurisdiction-based countries (US: TAXUSJ) keep
    rates per jurisdiction in A053 (JP1I..JP4I levels, summed); others
    keep one rate per tax code in A003."""
    today = date.today().strftime("%Y%m%d")
    rates = {}
    for code, jur in set(pairs):
        if not code:
            continue
        if jur:
            recs = read_table(conn, "A053", ["KSCHL", "KNUMH"], [
                "KAPPL = 'TX'", f"ALAND = {quote(country)}", f"MWSKZ = {quote(code)}",
                f"TXJCD = {quote(jur)}", f"DATBI >= '{today}'", f"DATAB <= '{today}'"])
        else:
            recs = read_table(conn, "A003", ["KSCHL", "KNUMH"], [
                "KAPPL = 'TX'", f"ALAND = {quote(country)}", f"MWSKZ = {quote(code)}"])
        rate = 0.0
        if recs:
            konp = read_table_in(conn, "KONP", ["KNUMH", "KBETR", "KONWA"], "KNUMH",
                                 [r["KNUMH"] for r in recs])
            # Percentage tax conditions are stored per mille (60.00 = 6.0%).
            rate = sum(num(k["KBETR"]) for k in konp if k["KONWA"] == "%") / 10.0
        rates[(code, jur)] = round(rate, 4)
    return rates


def _company_country(conn, bukrs: str) -> str:
    return _cached(("T001", bukrs), lambda: _company_country_live(conn, bukrs))


def _company_country_live(conn, bukrs: str) -> str:
    rows = read_table(conn, "T001", ["LAND1"], f"BUKRS = {quote(bukrs)}", max_rows=1)
    return rows[0]["LAND1"] if rows else ""


def _conditions(conn, knumv: str) -> list:
    if not knumv:
        return []
    table = "PRCD_ELEMENTS" if sap.is_s4(conn) else "KONV"
    rows = read_table(conn, table, _COND_FIELDS, f"KNUMV = {quote(knumv)}")
    return [r for r in rows if not r["KINAK"]]


def _status(header: dict, items: list) -> tuple:
    """(status, detail) in the vocabulary reconciliation_config expects:
    Open / Partially Open / Closed / Cancelled / On Hold."""
    if header.get("LOEKZ"):
        return "Cancelled", "PO header carries a deletion indicator in SAP"
    if header.get("FRGRL") == "X" or header.get("PROCSTAT") in _PROCSTAT_ON_HOLD:
        stage = _PROCSTAT_ON_HOLD.get(header.get("PROCSTAT"), "release not completed")
        return "On Hold", f"PO release strategy not complete ({stage})"
    active = [i for i in items if not i["deleted"]]
    if not active:
        return "Cancelled", "every PO line is marked for deletion in SAP"
    if all(i["delivery_completed"] and i["final_invoice"] for i in active):
        return "Closed", "all lines delivery-completed and final-invoiced in SAP"
    if any(i["quantity_received"] > 0 for i in active):
        return "Partially Open", "goods partly received — PO still open for invoicing"
    return "Open", "released and open in SAP"


# ─────────────────────────────────────────────────────────────────────
# PO history (EKBE) — shared by the PO, GRN and invoice-posting logic
# ─────────────────────────────────────────────────────────────────────

_EKBE_FIELDS = ["EBELP", "VGABE", "GJAHR", "BELNR", "BUZEI", "BWART", "BUDAT", "MENGE",
                "WRBTR", "SHKZG", "XBLNR", "LFGJA", "LFBNR", "LFPOS", "ERNAM"]


def _history(conn, ebeln: str) -> list:
    return _history_rows(read_table(conn, "EKBE", _EKBE_FIELDS,
                                    [f"EBELN = {quote(ebeln)}", "VGABE IN ('1','2')"]))


def _history_rows(rows: list) -> list:
    for r in rows:
        sign = -1 if r["SHKZG"] == "H" else 1
        r["qty"] = num(r["MENGE"])
        r["signed_qty"] = sign * r["qty"]
        r["signed_amount"] = sign * num(r["WRBTR"])
    return rows


def _gr_rows(history: list) -> list:
    return [h for h in history if h["VGABE"] == "1"]


def gr_reference_documents(history: list, ebelp: str) -> list:
    """Open GR reference documents for one PO line, for GR-based invoice
    verification: [{ref_doc, ref_year, ref_item, open_qty}] in posting
    order. A 101 references itself; 102/122 reversals/returns point back
    at it through LFBNR/LFGJA/LFPOS; invoices (VGABE 2) do the same."""
    refs = {}
    for h in history:
        if h["EBELP"] != ebelp:
            continue
        own = not h["LFBNR"] or (h["LFBNR"], h["LFGJA"]) == (h["BELNR"], h["GJAHR"])
        if h["VGABE"] == "1" and h["BWART"] in ("101", "103") and own:
            key = (h["BELNR"], h["GJAHR"], h["BUZEI"])
        else:
            key = (h["LFBNR"], h["LFGJA"], h["LFPOS"])
        if not key[0]:
            continue
        ref = refs.setdefault(key, {"ref_doc": key[0], "ref_year": key[1],
                                    "ref_item": key[2], "open_qty": 0.0,
                                    "budat": h["BUDAT"]})
        if h["VGABE"] == "1":
            if h["BWART"] in ("103", "104", "124", "125"):
                continue  # blocked-stock receipt — not invoiceable until 105
            ref["open_qty"] += h["signed_qty"]
        else:
            ref["open_qty"] -= h["signed_qty"]
    # A 101 posted "against" an earlier GR (LFBNR filled) is rare; anything
    # without its own 101 row simply contributes to the referenced key.
    return sorted(refs.values(), key=lambda r: (r["budat"], r["ref_doc"]))


# ─────────────────────────────────────────────────────────────────────
# PO
# ─────────────────────────────────────────────────────────────────────

_EKKO_FIELDS = ["EBELN", "BUKRS", "BSTYP", "BSART", "LOEKZ", "LIFNR", "ZTERM", "EKORG",
                "EKGRP", "WAERS", "BEDAT", "KNUMV", "FRGRL", "PROCSTAT", "ERNAM"]
_EKPO_FIELDS = ["EBELN", "EBELP", "LOEKZ", "TXZ01", "MATNR", "WERKS", "MENGE", "MEINS",
                "NETPR", "PEINH", "NETWR", "MWSKZ", "TXJCD", "ELIKZ", "EREKZ", "KNTTP", "WEBRE"]
_COND_FIELDS = ["KNUMV", "KPOSN", "STUNR", "ZAEHK", "KSCHL", "KBETR", "KPEIN", "KWERT",
                "KINAK", "KSTAT"]


def _read_po(conn, po_number: str) -> dict:
    ebeln = alpha(_norm_po(po_number))
    hdr = read_table(conn, "EKKO", _EKKO_FIELDS, f"EBELN = {quote(ebeln)}", max_rows=1)
    if not hdr or hdr[0]["BSTYP"] != "F":
        return {}
    h = hdr[0]
    items_raw = read_table(conn, "EKPO", _EKPO_FIELDS, f"EBELN = {quote(ebeln)}")
    vendor = read_table(conn, "LFA1", ["LIFNR", "NAME1", "ORT01", "REGIO"],
                        f"LIFNR = {quote(h['LIFNR'])}", max_rows=1)
    return _assemble_po(conn, h, items_raw, vendor[0] if vendor else {},
                        _conditions(conn, h["KNUMV"]), _history(conn, ebeln))


def read_pos_bulk(conn, po_numbers) -> dict:
    """{po_number: po} for many POs with one query per table (EKKO, EKPO,
    LFA1, pricing, EKBE) instead of one set per PO. pyrfc serialises
    calls (it holds the GIL), so fewer round trips is what makes the ERP
    page fast — not threads."""
    ebelns = [alpha(_norm_po(p)) for p in po_numbers if _norm_po(p)]
    if not ebelns:
        return {}
    heads = {h["EBELN"]: h for h in read_table_in(conn, "EKKO", _EKKO_FIELDS, "EBELN", ebelns)
             if h["BSTYP"] == "F"}
    if not heads:
        return {}
    items = read_table_in(conn, "EKPO", _EKPO_FIELDS, "EBELN", list(heads))
    vendors = {v["LIFNR"]: v for v in read_table_in(
        conn, "LFA1", ["LIFNR", "NAME1", "ORT01", "REGIO"], "LIFNR",
        {h["LIFNR"] for h in heads.values()})}
    knumvs = [h["KNUMV"] for h in heads.values() if h["KNUMV"]]
    table = "PRCD_ELEMENTS" if sap.is_s4(conn) else "KONV"
    conds = [c for c in read_table_in(conn, table, _COND_FIELDS, "KNUMV", knumvs) if not c["KINAK"]]
    hist = _history_rows(read_table_in(conn, "EKBE", ["EBELN"] + _EKBE_FIELDS, "EBELN",
                                       list(heads), "VGABE IN ('1','2')"))
    out = {}
    for ebeln, h in heads.items():
        out[strip_alpha(ebeln)] = _assemble_po(
            conn, h, [i for i in items if i["EBELN"] == ebeln], vendors.get(h["LIFNR"], {}),
            [c for c in conds if c["KNUMV"] == h["KNUMV"]],
            [x for x in hist if x["EBELN"] == ebeln])
    return out


def _assemble_po(conn, h: dict, items_raw: list, vendor: dict, conds: list,
                 history: list) -> dict:
    items_raw = sorted(items_raw, key=lambda r: r["EBELP"])  # storage order != item order
    terms = _payment_terms(conn, h["ZTERM"])
    units = _unit_texts(conn, {i["MEINS"] for i in items_raw})
    country = _company_country(conn, h["BUKRS"])
    rates = _tax_rates(conn, country, [(i["MWSKZ"], i["TXJCD"]) for i in items_raw])

    items = []
    subtotal = discount = freight = tax = 0.0
    for r in items_raw:
        ebelp = r["EBELP"]
        kposn = ebelp.zfill(6)
        line_conds = [c for c in conds if c["KPOSN"] == kposn]
        qty, peinh = num(r["MENGE"]), (num(r["PEINH"]) or 1.0)
        net_price = num(r["NETPR"]) / peinh

        gross = next((c for c in line_conds if c["KSCHL"] in PRICE_CONDITIONS), None)
        unit_price = (num(gross["KBETR"]) / (num(gross["KPEIN"]) or 1.0)) if gross else net_price
        line_discount = -sum(num(c["KWERT"]) for c in line_conds if c["KSCHL"] in DISCOUNT_CONDITIONS)
        line_freight = sum(num(c["KWERT"]) for c in line_conds if c["KSCHL"] in FREIGHT_CONDITIONS)
        freight_conds = [{"cond_type": c["KSCHL"], "step": c["STUNR"], "counter": c["ZAEHK"],
                          "amount": num(c["KWERT"])}
                         for c in line_conds if c["KSCHL"] in FREIGHT_CONDITIONS]
        rate = rates.get((r["MWSKZ"], r["TXJCD"]), 0.0)
        line_gross = round(qty * unit_price, 2)

        line_hist = [x for x in history if x["EBELP"] == ebelp]
        received = sum(x["signed_qty"] for x in line_hist
                       if x["VGABE"] == "1" and x["BWART"] in _MVT_RECEIVED)
        invoiced = sum(x["signed_qty"] for x in line_hist if x["VGABE"] == "2")
        deleted = bool(r["LOEKZ"])

        items.append({
            "item":               r["TXZ01"],
            "po_item":            ebelp,
            "material":           strip_alpha(r["MATNR"]),
            "plant":              r["WERKS"],
            "quantity":           qty,
            "uom":                units.get(r["MEINS"], r["MEINS"]),
            "sap_uom":            r["MEINS"],
            "unit_price":         round(unit_price, 4),
            "net_price":          round(net_price, 4),
            "line_amount":        line_gross,
            "discount":           round(line_discount, 2),
            "freight":            round(line_freight, 2),
            "freight_conditions": freight_conds,
            "tax_code":           r["MWSKZ"],
            "tax_jurisdiction":   r["TXJCD"],
            "tax_rate":           rate,
            "account_assignment": r["KNTTP"],
            "gr_based_iv":        r["WEBRE"] == "X",
            "deleted":            deleted,
            "deletion_flag":      r["LOEKZ"],
            "delivery_completed": r["ELIKZ"] == "X",
            "final_invoice":      r["EREKZ"] == "X",
            "quantity_received":  round(received, 3),
            "quantity_invoiced":  round(invoiced, 3),
        })
        if deleted:
            continue
        subtotal += line_gross
        discount += line_discount
        freight += line_freight
        tax += (line_gross - line_discount) * rate / 100.0

    taxable = [i for i in items if not i["deleted"] and i["tax_code"]]
    tax_rates = sorted({i["tax_rate"] for i in taxable})
    tax_codes = sorted({i["tax_code"] for i in taxable})
    jurisdictions = sorted({i["tax_jurisdiction"] for i in taxable if i["tax_jurisdiction"]})
    tax_rate = tax_rates[0] if len(tax_rates) == 1 else None
    if tax_rate is not None:
        tax_type = (f"Sales Tax @ {tax_rate:.2f}% (SAP {'/'.join(tax_codes)}"
                    f"{' · ' + '/'.join(jurisdictions) if jurisdictions else ''})")
    else:
        tax_type = "Mixed tax rates: " + ", ".join(f"{r:.2f}%" for r in tax_rates)

    status, status_detail = _status(h, items)
    po_amount = round(subtotal - discount + tax + freight, 2)

    return {
        "po_number":          strip_alpha(h["EBELN"]),
        "vendor_code":        strip_alpha(h["LIFNR"]),
        "sap_vendor":         h["LIFNR"],
        "vendor_name":        vendor.get("NAME1", ""),
        "vendor_city":        vendor.get("ORT01", ""),
        "po_date":            iso_date(h["BEDAT"]),
        "po_status":          status,
        "po_status_detail":   status_detail,
        "po_type":            h["BSART"],
        "company_code":       h["BUKRS"],
        "purchasing_org":     h["EKORG"],
        "purchasing_group":   h["EKGRP"],
        "created_by":         h["ERNAM"],
        "currency":           h["WAERS"],
        "payment_terms":      terms["label"],
        "payment_terms_code": terms["code"],
        "payment_terms_text": terms["text"],
        "tax_type":           tax_type,
        "tax_rate":           tax_rate,
        "subtotal":           round(subtotal, 2),
        "discount":           round(discount, 2),
        "tax":                round(tax, 2),
        "freight":            round(freight, 2),
        "po_amount":          po_amount,
        "items":              items,
        "erp_source":         source_label(),
        "fetched_at":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_history":           history,
    }


# ─────────────────────────────────────────────────────────────────────
# GRN — built from the PO's goods-receipt history
# ─────────────────────────────────────────────────────────────────────

def _gr_details(conn, docs) -> dict:
    """{(mblnr, mjahr): {'header': {...}, 'items': {zeile: {...}}}} via
    BAPI_GOODSMVT_GETDETAIL (MATDOC itself is not RFC_READ_TABLE-able)."""
    out = {}
    reasons = {}
    for mblnr, mjahr in docs:
        try:
            res = sap.call(conn, "BAPI_GOODSMVT_GETDETAIL",
                           MATERIALDOCUMENT=mblnr, MATDOCUMENTYEAR=mjahr)
        except sap.SapError:
            continue
        h = res.get("GOODSMVT_HEADER", {})
        items = {}
        for it in res.get("GOODSMVT_ITEMS", []):
            items[str(it.get("MATDOC_ITM", "")).zfill(4)] = {
                "text":   it.get("ITEM_TEXT", "").strip(),
                "reason": str(it.get("MOVE_REAS", "") or "").strip(),
                "bwart":  it.get("MOVE_TYPE", ""),
            }
            if items[str(it.get("MATDOC_ITM", "")).zfill(4)]["reason"].strip("0"):
                reasons[(it.get("MOVE_TYPE", ""), str(it.get("MOVE_REAS")).zfill(4))] = None
        out[(mblnr, mjahr)] = {
            "doc_date":    iso_date(h.get("DOC_DATE", "")),
            "user":        h.get("USERNAME", ""),
            "header_text": h.get("HEADER_TXT", "").strip(),
            "delivery_note": h.get("REF_DOC_NO", "").strip(),
            "items":       items,
        }
    if reasons:
        rows = read_table_in(conn, "T157E", ["BWART", "GRUND", "GRTXT"], "BWART",
                             {k[0] for k in reasons}, "SPRAS = 'E'")
        texts = {(r["BWART"], r["GRUND"]): r["GRTXT"] for r in rows}
        for d in out.values():
            for it in d["items"].values():
                if it["reason"].strip("0"):
                    it["reason_text"] = texts.get((it["bwart"], it["reason"].zfill(4)), "")
    return out


def _build_grn(conn, po: dict) -> dict:
    history = po.get("_history") or []
    grs = _gr_rows(history)
    if not grs:
        return {}

    details = _gr_details(conn, {(g["BELNR"], g["GJAHR"]) for g in grs})
    items = []
    for line in po["items"]:
        if line["deleted"]:
            continue
        rows = [g for g in grs if g["EBELP"] == line["po_item"]]
        received = sum(_MVT_RECEIVED.get(g["BWART"], 0) * g["qty"] for g in rows)
        accepted = sum(_MVT_ACCEPTED.get(g["BWART"], 0) * g["qty"] for g in rows)
        returned = sum(_MVT_RETURNED.get(g["BWART"], 0) * g["qty"] for g in rows)

        notes = []
        for g in rows:
            d = details.get((g["BELNR"], g["GJAHR"]), {})
            it = d.get("items", {}).get(g["BUZEI"].zfill(4), {})
            if g["BWART"] in _MVT_RETURNED and g["signed_qty"]:
                why = it.get("reason_text") or "returned to vendor"
                notes.append(f"{g['qty']:g} {line['uom']} returned ({why})"
                             + (f" — {it['text']}" if it.get("text") else ""))
            elif it.get("text"):
                notes.append(it["text"])
        if received <= 0 and not rows:
            condition = "Not received"
        elif notes:
            condition = "; ".join(dict.fromkeys(notes))
        elif received < line["quantity"]:
            condition = f"Partial — {line['quantity'] - received:g} {line['uom']} outstanding"
        else:
            condition = "Good"

        items.append({
            "item":              line["item"],
            "po_item":           line["po_item"],
            "uom":               line["uom"],
            "quantity_ordered":  line["quantity"],
            "quantity_received": round(received, 3),
            "quantity_accepted": round(accepted, 3),
            "quantity_returned": round(returned, 3),
            "condition":         condition,
        })

    active_receipts = [g for g in grs if g["BWART"] in ("101", "103")]
    reversed_docs = {g["LFBNR"] for g in grs if g["BWART"] in ("102", "104")}
    live = [g for g in active_receipts if g["BELNR"] not in reversed_docs] or active_receipts
    if all(i["quantity_received"] <= 0 for i in items):
        return {}  # every receipt reversed — nothing has actually been received

    docs = list(dict.fromkeys(g["BELNR"] for g in live))
    first = details.get((live[0]["BELNR"], live[0]["GJAHR"]), {})
    # The GR document date is when the goods physically arrived; the
    # posting date can sit in an earlier open MM period.
    received_dates = sorted(
        (details.get((g["BELNR"], g["GJAHR"]), {}).get("doc_date") or iso_date(g["BUDAT"]))
        for g in live)
    complete = all(i["quantity_received"] >= i["quantity_ordered"] for i in items)
    return {
        "grn_number":    ", ".join(docs),
        "grn_documents": [{"material_document": g["BELNR"], "year": g["GJAHR"],
                           "po_item": g["EBELP"], "movement_type": g["BWART"],
                           "quantity": g["signed_qty"], "posting_date": iso_date(g["BUDAT"]),
                           "delivery_note": g["XBLNR"]}
                          for g in grs],
        "po_number":     po["po_number"],
        "vendor_name":   po["vendor_name"],
        "received_date": received_dates[-1],
        "first_received_date": received_dates[0],
        "posting_date":  iso_date(max(g["BUDAT"] for g in live)),
        "received_by":   first.get("user") or live[0]["ERNAM"],
        "delivery_note": first.get("delivery_note", ""),
        "status":        "Complete" if complete else "Partial",
        "items":         items,
        "erp_source":    po.get("erp_source", ""),
    }


# ─────────────────────────────────────────────────────────────────────
# Public API (same contract as erp_mock.py)
# ─────────────────────────────────────────────────────────────────────

def _public(po: dict) -> dict:
    return {k: v for k, v in po.items() if not k.startswith("_")}


def lookup_po_and_grn(po_number: str, system: str = None) -> tuple:
    """(po_data, grn_data) for one PO, read live from SAP on one logon."""
    if not _norm_po(po_number):
        return {}, {}
    with sap.session(system) as conn:
        po = _read_po(conn, po_number)
        if not po:
            return {}, {}
        grn = _build_grn(conn, po)
        return _public(po), grn


def lookup_po(po_number: str, system: str = None) -> dict:
    return lookup_po_and_grn(po_number, system)[0]


def lookup_grn(po_number: str, system: str = None) -> dict:
    return lookup_po_and_grn(po_number, system)[1]


def po_with_history(conn, po_number: str) -> dict:
    """Internal: PO incl. raw EKBE history (for invoice posting)."""
    return _read_po(conn, po_number)


def recent_po_numbers(created_by: str = None, company_code: str = None,
                      days: int = 60, limit: int = 25, system: str = None) -> list:
    """Most recent standard POs, newest first — for the ERP dashboard."""
    since = date.fromordinal(date.today().toordinal() - days).strftime("%Y%m%d")
    where = ["BSTYP = 'F'", f"BEDAT >= '{since}'"]
    if created_by:
        where.append(f"ERNAM = {quote(created_by)}")
    if company_code:
        where.append(f"BUKRS = {quote(company_code)}")
    with sap.session(system) as conn:
        rows = read_table(conn, "EKKO", ["EBELN", "BEDAT"], where)
    rows.sort(key=lambda r: (r["BEDAT"], r["EBELN"]), reverse=True)
    return [strip_alpha(r["EBELN"]) for r in rows[:limit]]


def find_supplier_invoices(reference: str = None, vendor: str = None,
                           po_number: str = None, system: str = None) -> list:
    """
    Supplier invoices (MIRO documents) in SAP matching an invoice
    reference (RBKP-XBLNR), optionally restricted to a vendor, or every
    invoice posted against a PO (via RSEG). Includes parked and reversed
    documents, flagged so callers can decide what counts.
    """
    with sap.session(system) as conn:
        return _find_invoices(conn, reference, vendor, po_number)


_RBSTAT = {"5": "Posted", "A": "Parked", "B": "Parked & Completed", "C": "Parked & Held",
           "D": "Entered & Held", "E": "Parked & Released", "1": "Saved",
           "2": "Deleted", "3": "Being processed in background"}


_RBKP_FIELDS = ["BELNR", "GJAHR", "BUKRS", "BLDAT", "BUDAT", "XBLNR", "LIFNR", "RMWWR",
                "WMWST1", "WAERS", "RBSTAT", "STBLG", "STJAH", "ZLSPR", "USNAM", "CPUDT", "BKTXT"]


def _find_invoices(conn, reference=None, vendor=None, po_number=None) -> list:
    fields = _RBKP_FIELDS
    where = []
    if reference:
        where.append(f"XBLNR = {quote(str(reference).strip().upper()[:16])}")
    if vendor:
        where.append(f"LIFNR = {quote(alpha(vendor))}")
    if po_number:
        links = read_table(conn, "RSEG", ["BELNR", "GJAHR"],
                           f"EBELN = {quote(alpha(_norm_po(po_number)))}")
        keys = {(r["BELNR"], r["GJAHR"]) for r in links}
        if not keys:
            return []
        rows = read_table_in(conn, "RBKP", fields, "BELNR", {k[0] for k in keys}, where)
        rows = [r for r in rows if (r["BELNR"], r["GJAHR"]) in keys]
    elif where:
        rows = read_table(conn, "RBKP", fields, where)
    else:
        return []
    return _rbkp_rows(rows)


def _rbkp_rows(rows: list) -> list:
    out = []
    for r in rows:
        status = _RBSTAT.get(r["RBSTAT"], r["RBSTAT"])
        reversed_ = bool(r["STBLG"])
        out.append({
            "document":      r["BELNR"],
            "fiscal_year":   r["GJAHR"],
            "company_code":  r["BUKRS"],
            "reference":     r["XBLNR"],
            "vendor":        strip_alpha(r["LIFNR"]),
            "gross_amount":  num(r["RMWWR"]),
            "tax_amount":    num(r["WMWST1"]),
            "currency":      r["WAERS"],
            "doc_date":      iso_date(r["BLDAT"]),
            "posting_date":  iso_date(r["BUDAT"]),
            "entered_on":    iso_date(r["CPUDT"]),
            "entered_by":    r["USNAM"],
            "status":        "Reversed" if reversed_ else status,
            "rbstat":        r["RBSTAT"],
            "reversed_by":   r["STBLG"],
            "payment_block": r["ZLSPR"],
            "header_text":   r["BKTXT"],
            "is_parked":     r["RBSTAT"] in ("A", "B", "C", "D", "E") and not reversed_,
            "is_posted":     r["RBSTAT"] == "5" and not reversed_,
        })
    out.sort(key=lambda d: (d["fiscal_year"], d["document"]))
    return out


# ─────────────────────────────────────────────────────────────────────
# Dashboard bulk read — PO header/lines + GRN header/lines + invoices for
# a list of POs, on a single logon
# ─────────────────────────────────────────────────────────────────────

def dashboard_snapshot(po_numbers: list, system: str = None) -> dict:
    """Live read of several POs for the ERP page — same assembly code the
    3-way match uses, so the page shows exactly what validation sees."""
    started = time.time()
    result = {"pos": [], "errors": []}
    with sap.session(system) as conn:
        pos = read_pos_bulk(conn, po_numbers)
        invoices = _find_invoices_bulk(conn, list(pos))
        for po_number in po_numbers:
            key = strip_alpha(_norm_po(po_number))
            po = pos.get(key)
            if not po:
                result["errors"].append(f"PO {po_number} not found in SAP")
                continue
            try:
                grn = _build_grn(conn, po)
            except sap.SapError as e:
                grn = {}
                result["errors"].append(f"PO {po_number} GR detail: {e}")
            result["pos"].append({"po": _public(po), "grn": grn,
                                  "invoices": invoices.get(key, [])})
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


def _find_invoices_bulk(conn, po_numbers: list) -> dict:
    """{po_number: [supplier invoices]} via RSEG -> RBKP in two queries."""
    if not po_numbers:
        return {}
    links = read_table_in(conn, "RSEG", ["BELNR", "GJAHR", "EBELN"], "EBELN",
                          [alpha(p) for p in po_numbers])
    by_doc = {}
    for l in links:
        by_doc.setdefault((l["BELNR"], l["GJAHR"]), set()).add(strip_alpha(l["EBELN"]))
    if not by_doc:
        return {}
    docs = _rbkp_rows(read_table_in(conn, "RBKP", _RBKP_FIELDS, "BELNR", {k[0] for k in by_doc}))
    out = {}
    for d in docs:
        for po in sorted(by_doc.get((d["document"], d["fiscal_year"]), ())):
            out.setdefault(po, []).append(d)
    return out
