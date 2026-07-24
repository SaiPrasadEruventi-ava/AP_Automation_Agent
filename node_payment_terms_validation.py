"""
node_payment_terms_validation.py — Node: Payment Terms Validation
==================================================================
Compares PO payment terms vs invoice payment terms.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reconciliation_config import CFG


TERMS_EQUIVALENTS = {
    "net30":  ["net 30", "net30", "30 days", "30days", "n30"],
    "net45":  ["net 45", "net45", "45 days", "45days", "n45"],
    "net60":  ["net 60", "net60", "60 days", "60days", "n60"],
    "net90":  ["net 90", "net90", "90 days", "90days", "n90"],
    "immediate": ["immediate", "cash", "cod", "cash on delivery", "due on receipt"],
    "advance":["advance", "advance payment", "prepaid", "pre-paid"],
}


def _normalise_terms(terms: str) -> str:
    t = terms.lower().strip().replace("-", "").replace("_", "")
    for canonical, variants in TERMS_EQUIVALENTS.items():
        if t in variants or t == canonical:
            return canonical
    return t


def payment_terms_validation_node(state: dict) -> dict:
    """Validate payment terms match between PO and invoice."""
    print("\n" + "="*55)
    print("  PAYMENT TERMS VALIDATION")
    print("="*55)

    po_terms_raw  = state.get("po_data",      {}).get("payment_terms", "")
    inv_terms_raw = state.get("invoice_data", {}).get("payment_terms", "")

    if not po_terms_raw and not inv_terms_raw:
        result = {
            "match":      True,
            "po_terms":   "",
            "invoice_terms": "",
            "reason":     "Payment terms not specified in either document — skipped",
        }
        print(f"  ℹ️  {result['reason']}")
        return _return(state, result, valid=True)

    if not po_terms_raw:
        result = {
            "match":         True,
            "po_terms":      "Not specified",
            "invoice_terms": inv_terms_raw,
            "reason":        "PO has no payment terms — invoice terms accepted",
        }
        print(f"  ℹ️  {result['reason']}")
        return _return(state, result, valid=True)

    po_terms  = _normalise_terms(po_terms_raw)
    inv_terms = _normalise_terms(inv_terms_raw) if inv_terms_raw else po_terms

    match  = (po_terms == inv_terms)
    reason = (
        f"Payment terms match: '{po_terms_raw}'" if match
        else f"Payment terms mismatch: PO='{po_terms_raw}' Invoice='{inv_terms_raw}'"
    )

    result = {
        "match":         match,
        "po_terms":      po_terms_raw,
        "invoice_terms": inv_terms_raw,
        "reason":        reason,
    }

    icon = "✅" if match else "❌"
    print(f"  {icon} PO Terms      : {po_terms_raw or 'Not set'}")
    print(f"     Invoice Terms : {inv_terms_raw or 'Not set'}")
    print(f"     Result        : {reason}")

    return _return(state, result, valid=match)


def _return(state: dict, result: dict, valid: bool) -> dict:
    errors = state.get("errors", [])
    if not valid and CFG.ENFORCE_PAYMENT_TERMS:
        errors = errors + [f"Payment terms: {result['reason']}"]
    return {
        "payment_terms_validation": result,
        "errors":   errors,
        "audit_log":state.get("audit_log", []) + [{
            "node": "payment_terms_validation", "result": valid,
        }],
    }
