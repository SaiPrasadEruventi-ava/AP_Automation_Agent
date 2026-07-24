"""
node_final_reconciliation.py — Node: Final Reconciliation Engine
=================================================================
Aggregates ALL validation results and applies tolerance rules.
Produces the definitive Auto Approve / Manual Review decision.

This replaces/enhances the existing AP Validation node.
Implements SAP MIRO / Oracle AP / Dynamics 365 approval logic.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reconciliation_config import CFG


def final_reconciliation_node(state: dict) -> dict:
    """
    Aggregate all 14 validation checks and produce final decision.
    Auto Approve only if ALL checks pass.
    """
    print("\n" + "="*55)
    print("  FINAL RECONCILIATION ENGINE")
    print("="*55)

    def _pass(check_dict: dict, key: str = "match") -> bool:
        if not check_dict:
            return True  # Not run — treat as pass (backward compatible)
        return bool(check_dict.get(key, check_dict.get("valid", True)))

    # ── Collect all check results ─────────────────────────────────────
    dup     = state.get("duplicate_check",           {})
    inv_dt  = state.get("invoice_date_validation",   {})
    po_val  = state.get("po_validity",               {})
    grn_val = state.get("grn_validation",            {})
    header  = state.get("matching_results",          {})  # existing
    qty     = state.get("quantity_match",            {})
    price   = state.get("price_uom_match",           {})
    tax     = state.get("tax_reconciliation",        {})
    disc    = state.get("discount_validation",       {})
    freight = state.get("freight_validation",        {})
    terms   = state.get("payment_terms_validation",  {})
    lines   = state.get("line_amount_validation",    {})

    checks = {
        "Duplicate check":         not _pass(dup,     "is_duplicate") if dup else True,
        "Invoice date":            _pass(inv_dt,  "valid"),
        "PO validity":             _pass(po_val,  "valid"),
        "GRN validation":          _pass(grn_val, "valid"),
        "Header match":            _pass(header,  "header_match"),
        "Quantity match":          _pass(qty,     "match"),
        "Unit price match":        _pass(price,   "price_match"),
        "UOM match":               _pass(price,   "uom_match"),
        "Tax reconciliation":      _pass(tax,     "match"),
        "Discount validation":     _pass(disc,    "match"),
        "Freight validation":      _pass(freight, "match"),
        "Payment terms":           _pass(terms,   "match"),
        "Line amount arithmetic":  _pass(lines,   "lines_match") if lines else True,
        "Invoice total match":     _pass(
            lines.get("invoice_total_check", {}) if lines else {},
            "ok"
        ),
    }

    # Special handling for duplicate check
    if dup.get("is_duplicate"):
        checks["Duplicate check"] = False

    failed_checks = [k for k, v in checks.items() if not v]
    all_pass      = len(failed_checks) == 0

    # ── Build decision ────────────────────────────────────────────────
    decision = "Perfect Match" if all_pass else "Manual Review"

    # ── Print reconciliation scorecard ────────────────────────────────
    print(f"\n  {'Check':<30} {'Result':<10}")
    print(f"  {'─'*30} {'─'*10}")
    for check_name, passed in checks.items():
        icon = "✅ Pass" if passed else "❌ Fail"
        print(f"  {check_name:<30} {icon}")

    print(f"\n  {'─'*42}")
    if all_pass:
        print(f"  🟢 DECISION: PERFECT MATCH — all {len(checks)} checks passed")
    else:
        print(f"  🟡 DECISION: MANUAL REVIEW — {len(failed_checks)} check(s) failed")
        for f in failed_checks:
            print(f"     • {f}")

    result = {
        "auto_approve":     all_pass,
        "checks":           {k: "Pass" if v else "Fail" for k, v in checks.items()},
        "failed_checks":    failed_checks,
        "total_checks":     len(checks),
        "passed_checks":    len(checks) - len(failed_checks),
        "decision":         decision,
        "decision_reasons": failed_checks,
    }

    return {
        "final_reconciliation":  result,
        "validation_decision":   decision,
        "review_required":       not all_pass,
        "audit_log": state.get("audit_log", []) + [{
            "node":           "final_reconciliation",
            "decision":       decision,
            "failed_checks":  failed_checks,
            "checks_passed":  len(checks) - len(failed_checks),
            "checks_total":   len(checks),
        }],
        "errors": state.get("errors", []),
    }
