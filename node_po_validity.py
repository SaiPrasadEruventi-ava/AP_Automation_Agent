"""
node_po_validity.py — Node: PO Validity Check
===============================================
Verifies the PO is in an eligible status before processing.
Mirrors SAP ME23N / Oracle iProcurement PO status checks.

PO Statuses:
  Open           → eligible for matching
  Partially Open → eligible (some lines still open)
  Closed         → all quantities received — reject invoice
  Cancelled      → PO cancelled — reject invoice
  Expired        → PO past validity date — reject invoice
  On Hold        → PO on hold — route to manual review
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reconciliation_config import CFG


# Mock PO status database — in production connect to ERP
PO_STATUS_DB = {
    "PO1001": {"status": "Open",          "expiry_date": "2025-12-31"},
    "PO1002": {"status": "Open",          "expiry_date": "2025-12-31"},
    "PO1003": {"status": "Open",          "expiry_date": "2025-12-31"},
    "PO2001": {"status": "Open",          "expiry_date": "2025-12-31"},
    "PO9999": {"status": "Closed",        "expiry_date": None},
    "PO8888": {"status": "Cancelled",     "expiry_date": None},
    "PO7777": {"status": "Expired",       "expiry_date": "2024-01-01"},
}


def po_validity_node(state: dict) -> dict:
    """Check PO status is eligible for invoice processing."""
    print("\n" + "="*55)
    print("  PO VALIDITY CHECK")
    print("="*55)

    po_number = (
        state.get("invoice_data", {}).get("po_number", "") or
        state.get("po_data",      {}).get("po_number", "")
    ).strip().upper()

    if not po_number:
        result = {
            "valid":          False,
            "po_status":      "Not Found",
            "po_expiry_date": None,
            "reason":         "PO number is missing from invoice",
        }
        print(f"  ❌ {result['reason']}")
        return _return(state, result)

    po_record = PO_STATUS_DB.get(po_number)

    if not po_record:
        result = {
            "valid":          False,
            "po_status":      "Not Found",
            "po_expiry_date": None,
            "reason":         f"PO {po_number} not found in system",
        }
        print(f"  ❌ {result['reason']}")
        return _return(state, result)

    po_status   = po_record["status"]
    expiry_date = po_record.get("expiry_date")
    valid       = po_status in CFG.ALLOWED_PO_STATUSES

    if valid:
        reason = f"PO {po_number} is {po_status} — eligible for matching"
    else:
        reason = (
            f"PO {po_number} status is '{po_status}' — "
            f"not eligible. Allowed: {list(CFG.ALLOWED_PO_STATUSES)}"
        )

    result = {
        "valid":          valid,
        "po_status":      po_status,
        "po_expiry_date": expiry_date,
        "reason":         reason,
    }

    icon = "✅" if valid else "❌"
    print(f"  {icon} PO Number   : {po_number}")
    print(f"     Status     : {po_status}")
    if expiry_date:
        print(f"     Expiry     : {expiry_date}")
    print(f"     Result     : {reason}")

    return _return(state, result)


def _return(state: dict, result: dict) -> dict:
    errors = state.get("errors", [])
    if not result["valid"]:
        errors = errors + [f"PO validity: {result['reason']}"]
    return {
        "po_validity": result,
        "errors":      errors,
        "audit_log":   state.get("audit_log", []) + [{
            "node": "po_validity", "result": result["valid"],
            "reason": result["reason"],
        }],
    }
