"""
node_line_amount_validation.py — Line Amount + Invoice Total Validation
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reconciliation_config import CFG


def line_amount_validation_node(state: dict) -> dict:
    print("\n" + "="*55)
    print("  LINE AMOUNT + INVOICE TOTAL VALIDATION")
    print("="*55)

    invoice_data  = state.get("invoice_data", {})
    invoice_items = invoice_data.get("items", [])
    item_results  = []
    lines_ok      = True

    for item in invoice_items:
        name       = item.get("item", "")
        qty        = float(item.get("quantity", 0))
        unit_price = float(item.get("unit_price", 0))
        line_amt   = float(item.get("line_amount", item.get("amount", qty * unit_price)))
        calculated = round(qty * unit_price, 2)
        variance   = round(line_amt - calculated, 2)
        ok         = abs(variance) <= 0.01

        if not ok:
            lines_ok = False

        icon = "✅" if ok else "❌"
        print(f"  {icon} '{name}': {qty} x ${unit_price} = ${calculated:.2f} | Invoice=${line_amt:.2f} | Var=${variance:.2f}")

        item_results.append({
            "item": name, "qty": qty, "unit_price": unit_price,
            "calculated_amount": calculated, "invoice_amount": line_amt,
            "variance": variance, "ok": ok,
        })

    # Invoice total check
    subtotal  = sum(float(i.get("line_amount", float(i.get("quantity",0)) * float(i.get("unit_price",0)))) for i in invoice_items)
    tax       = float(invoice_data.get("tax_amount", invoice_data.get("tax", 0)))
    freight   = float(invoice_data.get("freight", invoice_data.get("shipping", 0)))
    discount  = float(invoice_data.get("discount", invoice_data.get("invoice_discount", 0)))
    inv_total = float(invoice_data.get("total_amount", 0))

    calc_total    = round(subtotal + tax + freight - discount, 2)
    total_var     = round(inv_total - calc_total, 2)
    total_var_pct = abs(total_var / inv_total * 100) if inv_total > 0 else 0.0
    total_ok      = total_var_pct <= CFG.AMOUNT_TOLERANCE_PCT

    icon = "✅" if total_ok else "❌"
    print(f"\n  {icon} Total: subtotal=${subtotal:.2f} + tax=${tax:.2f} + freight=${freight:.2f} - discount=${discount:.2f}")
    print(f"     Calculated=${calc_total:.2f} | Invoice=${inv_total:.2f} | Var={total_var_pct:.2f}%")

    total_check = {
        "subtotal": round(subtotal,2), "tax": tax, "freight": freight,
        "discount": discount, "calculated_total": calc_total,
        "invoice_total": inv_total, "variance": total_var,
        "variance_pct": round(total_var_pct,4), "ok": total_ok,
    }

    result = {
        "match": lines_ok and total_ok,
        "lines_match": lines_ok,
        "item_results": item_results,
        "invoice_total_check": total_check,
    }

    errors = state.get("errors", [])
    if not lines_ok:
        failed = [r["item"] for r in item_results if not r["ok"]]
        errors = errors + [f"Line amount error on: {failed}"]
    if not total_ok:
        errors = errors + [f"Invoice total mismatch: calc=${calc_total:.2f} invoice=${inv_total:.2f}"]

    return {
        "line_amount_validation": result,
        "errors": errors,
        "audit_log": state.get("audit_log", []) + [{"node": "line_amount_validation", "ok": lines_ok and total_ok}],
    }
