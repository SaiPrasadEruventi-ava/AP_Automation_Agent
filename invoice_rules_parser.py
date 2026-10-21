"""
invoice_rules_parser.py — Rule-based invoice reader (no LLM)
=============================================================
Fallback for document_extractor.extract_invoice() when the LLM is not
available (observed: Mistral answering every call with 429 "Rate limit
exceeded" once the account's quota is used up). It reads the text of a
clearly labelled invoice PDF — the demo invoices produced by
sap_demo_setup.py and the original test-scenario invoices — and returns
the same dict shape as the LLM prompt (INVOICE_PROMPT).

It is deliberately strict: it only returns data when it finds an
invoice number, a PO reference, at least one line item and a total, so a
document it cannot really read is reported as unreadable instead of
being half-parsed.
"""

import re

_LABELS = ["Invoice Number", "Invoice Date", "Vendor Name", "PO Reference", "Purchase Order",
           "Currency", "Payment Terms", "Tax Type", "Bill To"]
_LABEL_RE = "|".join(re.escape(l) for l in _LABELS)
_MONEY = r"-?\$?\s?-?[\d,]+\.\d{2}"


def _num(s) -> float:
    s = str(s or "").replace("$", "").replace(",", "").replace(" ", "")
    neg = s.startswith("-") or s.endswith("-")
    s = s.strip("-")
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return -v if neg else v


def _field(text: str, label: str) -> str:
    """Value after `label` (optionally followed by ':'), up to the next
    known label on the same line or the end of the line."""
    m = re.search(rf"{re.escape(label)}\s*:?\s*(.+?)(?=\s+(?:{_LABEL_RE})\b|$)", text, re.M)
    return m.group(1).strip() if m else ""


_DOLLARS = r"-?\$\s?-?[\d,]+\.\d{2}"   # amounts always carry $, rates like 6.00% never do


def _amount(text: str, *labels) -> float:
    for label in labels:
        # exactly one $ amount on the line: item lines ("Freight Forwarding
        # Service 1 EA $2,500.00 $2,500.00") carry two and must not match
        m = re.search(rf"^{label}[^\n$]*?({_DOLLARS})[^\n$]*$", text, re.M | re.I)
        if m:
            return abs(_num(m.group(1)))
    return 0.0


# "Office Chair Ergonomic 20 EA $163.00 $3,260.00"   (desc qty uom price amount)
_LINE_A = re.compile(rf"^(?P<item>.+?)\s+(?P<qty>[\d,]+(?:\.\d+)?)\s+(?P<uom>[A-Za-z]{{1,6}})\s+"
                     rf"(?P<price>\$[\d,]+\.\d{{2}})\s+(?P<amount>\$[\d,]+\.\d{{2}})\s*$")
# "ARM Cortex MCU M4 500 $15.00 EA $7,500.00"        (desc qty price uom amount)
_LINE_B = re.compile(rf"^(?P<item>.+?)\s+(?P<qty>[\d,]+(?:\.\d+)?)\s+(?P<price>\$[\d,]+\.\d{{2}})\s+"
                     rf"(?P<uom>[A-Za-z]{{1,6}})\s+(?P<amount>\$[\d,]+\.\d{{2}})\s*$")


def _items(text: str) -> list:
    items = []
    for raw in text.splitlines():
        line = raw.strip()
        m = _LINE_A.match(line) or _LINE_B.match(line)
        if not m:
            continue
        qty = _num(m.group("qty"))
        items.append({
            "item":       m.group("item").strip(),
            "quantity":   int(qty) if qty == int(qty) else qty,
            "unit_price": _num(m.group("price")),
            "uom":        m.group("uom").upper(),
            "amount":     _num(m.group("amount")),
        })
    return items


def parse_invoice(text: str) -> dict:
    """Invoice fields from PDF text, or {} if this is not a layout the
    rules can read with confidence."""
    text = text.replace("—", "—")
    inv_no = _field(text, "Invoice Number")
    po = _field(text, "PO Reference") or _field(text, "Purchase Order")
    items = _items(text)
    total = _amount(text, r"TOTAL AMOUNT DUE", r"TOTAL AMOUNT", r"Total Due", r"Total")
    if not (inv_no and po and items and total):
        return {}
    date = _field(text, "Invoice Date")
    m = re.search(r"\d{4}-\d{2}-\d{2}", date)
    return {
        "invoice_number": inv_no.split()[0],
        "vendor_name":    _field(text, "Vendor Name"),
        "invoice_date":   m.group(0) if m else date,
        "po_number":      po.split()[0],
        "currency":       (_field(text, "Currency").split() or ["USD"])[0],
        "payment_terms":  _field(text, "Payment Terms"),
        "tax_type":       _field(text, "Tax Type"),
        "subtotal":       _amount(text, r"Subtotal"),
        "tax":            _amount(text, r"Tax\b"),
        "freight":        _amount(text, r"Freight"),
        "discount":       _amount(text, r"Discount"),
        "total_amount":   total,
        "items":          items,
    }
