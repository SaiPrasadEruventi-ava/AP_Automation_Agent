"""
node_tax_reconciliation.py — Node: Tax Reconciliation
=======================================================
Validates GST/VAT/IGST/CGST/SGST/Sales Tax.
Handles both simple (single rate) and split tax (CGST+SGST for India GST).
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reconciliation_config import CFG


def tax_reconciliation_node(state: dict) -> dict:
    """Validate tax type, rate, and amount against PO terms."""
    print("\n" + "="*55)
    print("  TAX RECONCILIATION")
    print("="*55)
    print(f"  Tax tolerance: {CFG.TAX_TOLERANCE_PCT}%")

    invoice_data = state.get("invoice_data", {})
    po_data      = state.get("po_data",      {})

    inv_subtotal   = float(invoice_data.get("subtotal",   invoice_data.get("total_amount", 0)))
    inv_tax_rate   = float(invoice_data.get("tax_rate",   invoice_data.get("tax_percentage", 0)))
    inv_tax_amount = float(invoice_data.get("tax_amount", invoice_data.get("tax", 0)))
    inv_tax_type   = str(invoice_data.get("tax_type",     invoice_data.get("tax_type_code", "GST")))
    inv_cgst       = float(invoice_data.get("cgst", 0))
    inv_sgst       = float(invoice_data.get("sgst", 0))
    inv_igst       = float(invoice_data.get("igst", 0))

    po_tax_rate    = float(po_data.get("tax_rate",   po_data.get("tax_percentage", inv_tax_rate)))
    po_tax_type    = str(po_data.get("tax_type",     po_data.get("tax_type_code",  inv_tax_type)))

    reasons  = []

    # Rule 1: Tax type must match
    if inv_tax_type.upper() != po_tax_type.upper():
        reasons.append(
            f"Tax type mismatch: Invoice='{inv_tax_type}' PO='{po_tax_type}'"
        )

    # Rule 2: Calculate expected tax amount
    taxable_amount   = inv_subtotal if inv_subtotal > 0 else float(
        invoice_data.get("total_amount", 0)
    )
    expected_tax_amt = round(taxable_amount * inv_tax_rate / 100, 2)

    # Rule 3: Invoice tax amount vs calculated
    if expected_tax_amt > 0:
        tax_var_pct = abs((inv_tax_amount - expected_tax_amt) / expected_tax_amt * 100)
    else:
        tax_var_pct = 0.0

    tax_amount_ok = tax_var_pct <= CFG.TAX_TOLERANCE_PCT

    if not tax_amount_ok:
        reasons.append(
            f"Tax amount mismatch: Expected=${expected_tax_amt:.2f} "
            f"Invoice=${inv_tax_amount:.2f} Variance={tax_var_pct:.2f}%"
        )

    # Rule 4: For India GST — CGST + SGST must equal total tax
    split_tax_ok = True
    if inv_cgst > 0 or inv_sgst > 0:
        split_total = inv_cgst + inv_sgst
        if abs(split_total - inv_tax_amount) > 0.01:
            split_tax_ok = False
            reasons.append(
                f"CGST ({inv_cgst}) + SGST ({inv_sgst}) = {split_total} "
                f"does not equal tax amount {inv_tax_amount}"
            )

    valid  = len(reasons) == 0
    reason = "; ".join(reasons) if reasons else (
        f"Tax {inv_tax_type} @ {inv_tax_rate}% = ${inv_tax_amount:.2f} — valid"
    )

    result = {
        "match":               valid,
        "tax_type":            inv_tax_type,
        "taxable_amount":      taxable_amount,
        "expected_tax_rate":   po_tax_rate,
        "invoice_tax_rate":    inv_tax_rate,
        "expected_tax_amount": expected_tax_amt,
        "invoice_tax_amount":  inv_tax_amount,
        "variance":            round(inv_tax_amount - expected_tax_amt, 2),
        "variance_pct":        round(tax_var_pct, 4),
        "cgst":                {"amount": inv_cgst},
        "sgst":                {"amount": inv_sgst},
        "igst":                {"amount": inv_igst},
        "reason":              reason,
    }

    icon = "✅" if valid else "❌"
    print(f"  {icon} Tax type    : {inv_tax_type}")
    print(f"     Tax rate   : {inv_tax_rate}%")
    print(f"     Expected   : ${expected_tax_amt:.2f}")
    print(f"     Invoice    : ${inv_tax_amount:.2f}")
    print(f"     Variance   : {tax_var_pct:.2f}%")
    if inv_cgst > 0 or inv_sgst > 0:
        print(f"     CGST/SGST  : {inv_cgst}/{inv_sgst}")
    print(f"     Result     : {reason}")

    errors = state.get("errors", [])
    if not valid:
        errors = errors + [f"Tax reconciliation: {reason}"]

    return {
        "tax_reconciliation": result,
        "errors":             errors,
        "audit_log":          state.get("audit_log", []) + [{
            "node": "tax_reconciliation", "result": valid,
        }],
    }
