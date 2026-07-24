"""
node_grn_validation.py — Node: GRN Validation
===============================================
Verifies the GRN is valid before 3-way matching.
Rules from SAP MIGO / Oracle Receiving / Dynamics 365 Receiving.

Checks:
  1. GRN exists for the PO number
  2. GRN is in Approved/Complete status
  3. GRN date is before or on invoice date (goods received before billed)
  4. GRN vendor matches invoice vendor
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime
from reconciliation_config import CFG


def po_validity_node(state: dict) -> dict:
    pass  # imported separately


def grn_validation_node(state: dict) -> dict:
    """Validate GRN existence, status, and date."""
    print("\n" + "="*55)
    print("  GRN VALIDATION")
    print("="*55)

    grn_data     = state.get("grn_data",     {})
    invoice_data = state.get("invoice_data", {})

    # Check 1: GRN exists
    grn_exists = bool(grn_data and grn_data.get("grn_number"))
    if not grn_exists:
        result = {
            "valid":         False,
            "grn_exists":    False,
            "grn_approved":  False,
            "grn_date_valid":False,
            "grn_date":      "",
            "reason":        "GRN not found — goods may not have been received yet",
        }
        print(f"  ❌ {result['reason']}")
        return _return(state, result)

    grn_number  = grn_data.get("grn_number",    "")
    grn_status  = grn_data.get("status",         "Complete")
    grn_date_str= grn_data.get("received_date",  "")
    grn_vendor  = grn_data.get("vendor_name",    "").lower().strip()
    inv_vendor  = invoice_data.get("vendor_name","").lower().strip()
    inv_date_str= invoice_data.get("invoice_date","")

    reasons = []

    # Check 2: GRN approved
    valid_statuses = {"complete", "approved", "partial — quality rejection",
                      "over-received", "partial"}
    grn_approved   = grn_status.lower() in valid_statuses or "complete" in grn_status.lower()
    if not grn_approved:
        reasons.append(
            f"GRN {grn_number} status '{grn_status}' is not approved"
        )

    # Check 3: GRN date before invoice date
    grn_date_valid = True
    if grn_date_str and inv_date_str:
        try:
            grn_d = datetime.strptime(grn_date_str, "%Y-%m-%d").date()
            inv_d = datetime.strptime(inv_date_str, "%Y-%m-%d").date()
            if grn_d > inv_d:
                grn_date_valid = False
                reasons.append(
                    f"GRN date {grn_date_str} is after invoice date {inv_date_str} "
                    f"— goods cannot be received after invoice"
                )
        except ValueError:
            pass

    # Check 4: Vendor consistency
    if grn_vendor and inv_vendor and grn_vendor != inv_vendor:
        reasons.append(
            f"GRN vendor '{grn_data.get('vendor_name')}' does not match "
            f"invoice vendor '{invoice_data.get('vendor_name')}'"
        )

    valid  = len(reasons) == 0
    reason = "; ".join(reasons) if reasons else (
        f"GRN {grn_number} is valid — status: {grn_status}, date: {grn_date_str}"
    )

    result = {
        "valid":         valid,
        "grn_exists":    True,
        "grn_approved":  grn_approved,
        "grn_date_valid":grn_date_valid,
        "grn_date":      grn_date_str,
        "reason":        reason,
    }

    icon = "✅" if valid else "❌"
    print(f"  {icon} GRN Number  : {grn_number}")
    print(f"     Status     : {grn_status}")
    print(f"     Date       : {grn_date_str}")
    print(f"     Result     : {reason}")

    return _return(state, result)


def _return(state: dict, result: dict) -> dict:
    errors = state.get("errors", [])
    if not result["valid"]:
        errors = errors + [f"GRN validation: {result['reason']}"]
    return {
        "grn_validation": result,
        "errors":         errors,
        "audit_log":      state.get("audit_log", []) + [{
            "node": "grn_validation", "result": result["valid"],
            "reason": result["reason"],
        }],
    }
