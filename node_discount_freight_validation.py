"""
node_discount_freight_validation.py — Node: Discount + Freight Validation
==========================================================================
Validates discount and freight charges against PO terms.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reconciliation_config import CFG


def discount_freight_validation_node(state: dict) -> dict:
    """Validate discount and freight/delivery charges."""
    print("\n" + "="*55)
    print("  DISCOUNT + FREIGHT VALIDATION")
    print("="*55)

    invoice_data = state.get("invoice_data", {})
    po_data      = state.get("po_data",      {})

    # ── Discount validation ───────────────────────────────────────────
    inv_line_discount    = float(invoice_data.get("line_discount",    0))
    inv_invoice_discount = float(invoice_data.get("invoice_discount",
                                  invoice_data.get("discount", 0)))
    inv_early_discount   = float(invoice_data.get("early_payment_discount", 0))

    po_line_discount     = float(po_data.get("line_discount",    inv_line_discount))
    po_invoice_discount  = float(po_data.get("invoice_discount",
                                  po_data.get("discount", inv_invoice_discount)))

    disc_var = abs(inv_invoice_discount - po_invoice_discount)
    disc_ok  = disc_var <= CFG.DISCOUNT_TOLERANCE_PCT

    # ── Freight validation ────────────────────────────────────────────
    inv_freight = float(invoice_data.get("freight",
                         invoice_data.get("shipping",
                          invoice_data.get("delivery_charges", 0))))
    po_freight  = float(po_data.get("freight",
                         po_data.get("shipping",
                          po_data.get("delivery_charges", 0))))

    if po_freight > 0:
        freight_var_pct = abs((inv_freight - po_freight) / po_freight * 100)
    else:
        freight_var_pct = 0.0 if inv_freight == 0 else 100.0

    freight_ok = freight_var_pct <= CFG.FREIGHT_TOLERANCE_PCT

    reasons = []
    if not disc_ok:
        reasons.append(
            f"Discount mismatch: PO={po_invoice_discount}% "
            f"Invoice={inv_invoice_discount}%"
        )
    if not freight_ok:
        reasons.append(
            f"Freight mismatch: PO=${po_freight:.2f} "
            f"Invoice=${inv_freight:.2f} "
            f"Variance={freight_var_pct:.2f}%"
        )

    valid  = len(reasons) == 0
    reason = "; ".join(reasons) if reasons else "Discount and freight charges match"

    disc_icon    = "✅" if disc_ok    else "❌"
    freight_icon = "✅" if freight_ok else "❌"
    print(f"  {disc_icon} Discount: PO={po_invoice_discount}% | "
          f"Invoice={inv_invoice_discount}% | "
          f"{'OK' if disc_ok else 'MISMATCH'}")
    print(f"  {freight_icon} Freight : PO=${po_freight:.2f} | "
          f"Invoice=${inv_freight:.2f} | "
          f"Var={freight_var_pct:.2f}% | "
          f"{'OK' if freight_ok else 'EXCEEDS TOLERANCE'}")
    if inv_early_discount > 0:
        print(f"  ℹ️  Early payment discount: {inv_early_discount}%")

    disc_result    = {
        "match":                 disc_ok,
        "line_discount_match":   True,
        "invoice_discount_match":disc_ok,
        "early_payment_discount":inv_early_discount,
        "variance":              round(disc_var, 4),
    }
    freight_result = {
        "match":             freight_ok,
        "po_freight":        po_freight,
        "invoice_freight":   inv_freight,
        "variance":          round(inv_freight - po_freight, 2),
        "variance_pct":      round(freight_var_pct, 4),
        "within_tolerance":  freight_ok,
    }

    errors = state.get("errors", [])
    if reasons:
        errors = errors + reasons

    return {
        "discount_validation": disc_result,
        "freight_validation":  freight_result,
        "errors":              errors,
        "audit_log":           state.get("audit_log", []) + [{
            "node": "discount_freight_validation",
            "discount_ok": disc_ok, "freight_ok": freight_ok,
        }],
    }
