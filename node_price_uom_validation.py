"""
node_price_uom_validation.py — Node: Unit Price + UOM Validation
=================================================================
Validates unit prices and units of measure per line item.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reconciliation_config import CFG


def _normalise(text: str) -> str:
    return " ".join(str(text).lower().split())


def price_uom_validation_node(state: dict) -> dict:
    """Validate unit price (with tolerance) and UOM match per line."""
    print("\n" + "="*55)
    print("  UNIT PRICE + UOM VALIDATION")
    print("="*55)
    print(f"  Price tolerance: {CFG.PRICE_TOLERANCE_PCT}%")

    invoice_items = state.get("invoice_data", {}).get("items", [])
    po_items      = state.get("po_data",      {}).get("items", [])

    po_idx = {_normalise(i.get("item","")): i for i in po_items}

    item_results = []
    price_ok_all = True
    uom_ok_all   = True

    for inv_item in invoice_items:
        name      = inv_item.get("item", "")
        key       = _normalise(name)
        inv_price = float(inv_item.get("unit_price", 0))
        inv_uom   = str(inv_item.get("uom", inv_item.get("unit", "EA"))).upper().strip()

        po_item   = po_idx.get(key, {})
        po_price  = float(po_item.get("unit_price", 0)) if po_item else 0
        po_uom    = str(po_item.get("uom", po_item.get("unit", "EA"))).upper().strip() if po_item else "EA"

        # Price variance
        if po_price > 0:
            price_var_pct = abs((inv_price - po_price) / po_price * 100)
        else:
            price_var_pct = 0.0

        price_ok = price_var_pct <= CFG.PRICE_TOLERANCE_PCT
        uom_ok   = inv_uom == po_uom

        if not price_ok: price_ok_all = False
        if not uom_ok:   uom_ok_all   = False

        price_icon = "✅" if price_ok else "❌"
        uom_icon   = "✅" if uom_ok   else "❌"
        print(f"  '{name}'")
        print(f"    {price_icon} Price: PO=${po_price} | Inv=${inv_price} | "
              f"Var={price_var_pct:.2f}% ({'OK' if price_ok else 'EXCEEDS TOLERANCE'})")
        print(f"    {uom_icon} UOM:   PO={po_uom} | Inv={inv_uom} | "
              f"{'Match' if uom_ok else 'MISMATCH'}")

        item_results.append({
            "item":             name,
            "po_price":         po_price,
            "invoice_price":    inv_price,
            "price_variance":   round(inv_price - po_price, 4),
            "price_variance_pct": round(price_var_pct, 4),
            "price_ok":         price_ok,
            "po_uom":           po_uom,
            "invoice_uom":      inv_uom,
            "uom_ok":           uom_ok,
        })

    print(f"\n  Price match : {'✅ PASS' if price_ok_all else '❌ FAIL'}")
    print(f"  UOM match   : {'✅ PASS' if uom_ok_all   else '❌ FAIL'}")

    result = {
        "price_match":  price_ok_all,
        "uom_match":    uom_ok_all,
        "item_results": item_results,
    }

    errors = state.get("errors", [])
    if not price_ok_all:
        errors = errors + ["Unit price variance exceeds tolerance"]
    if not uom_ok_all:
        failed_uom = [r["item"] for r in item_results if not r["uom_ok"]]
        errors = errors + [f"UOM mismatch on: {failed_uom}"]

    return {
        "price_uom_match": result,
        "errors":          errors,
        "audit_log":       state.get("audit_log", []) + [{
            "node": "price_uom_validation",
            "price_ok": price_ok_all,
            "uom_ok":   uom_ok_all,
        }],
    }
