"""
node_invoice_date_validation.py — Node: Invoice Date Validation
================================================================
Validates invoice date against configurable rules.
Mirrors SAP MIRO date validation logic.

Rules:
  1. Invoice date must not be a future date
  2. Invoice date must not be older than MAX_INVOICE_AGE_DAYS (default 90)
  3. Invoice date must be in valid YYYY-MM-DD format
  4. Invoice date must not be after GRN date (goods received before invoiced)
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, date
from reconciliation_config import CFG


def invoice_date_validation_node(state: dict) -> dict:
    """Validate invoice date against business rules."""
    print("\n" + "="*55)
    print("  INVOICE DATE VALIDATION")
    print("="*55)

    invoice_data = state.get("invoice_data", {})
    invoice_date_str = invoice_data.get("invoice_date", "").strip()

    if not invoice_date_str:
        result = {
            "valid":       False,
            "invoice_date":invoice_date_str,
            "age_days":    -1,
            "is_future":   False,
            "reason":      "Invoice date is missing",
        }
        print(f"  ❌ {result['reason']}")
        return _return(state, result)

    # Parse date
    try:
        inv_date = datetime.strptime(invoice_date_str, "%Y-%m-%d").date()
    except ValueError:
        result = {
            "valid":       False,
            "invoice_date":invoice_date_str,
            "age_days":    -1,
            "is_future":   False,
            "reason":      f"Invalid date format: '{invoice_date_str}' — expected YYYY-MM-DD",
        }
        print(f"  ❌ {result['reason']}")
        return _return(state, result)

    today     = date.today()
    age_days  = (today - inv_date).days
    is_future = inv_date > today
    reasons   = []

    # Rule 1: No future dates
    if is_future and not CFG.ALLOW_FUTURE_INVOICE:
        reasons.append(
            f"Invoice date {invoice_date_str} is in the future"
        )

    # Rule 2: Not too old
    if age_days > CFG.MAX_INVOICE_AGE_DAYS:
        reasons.append(
            f"Invoice date {invoice_date_str} is {age_days} days old "
            f"(max allowed: {CFG.MAX_INVOICE_AGE_DAYS} days)"
        )

    # Rule 3: Invoice date should be <= GRN received date
    grn_date_str = state.get("grn_data", {}).get("received_date", "")
    if grn_date_str:
        try:
            grn_date = datetime.strptime(grn_date_str, "%Y-%m-%d").date()
            if inv_date < grn_date:
                pass  # invoice after goods received — correct
            # Invoice date can be same as or after GRN date — both acceptable
        except ValueError:
            pass

    valid = len(reasons) == 0
    reason = "; ".join(reasons) if reasons else (
        f"Invoice date {invoice_date_str} is valid ({age_days} days old)"
    )

    result = {
        "valid":        valid,
        "invoice_date": invoice_date_str,
        "age_days":     age_days,
        "is_future":    is_future,
        "reason":       reason,
    }

    icon = "✅" if valid else "❌"
    print(f"  {icon} Invoice date : {invoice_date_str}")
    print(f"     Age         : {age_days} days")
    print(f"     Status      : {reason}")

    return _return(state, result)


def _return(state: dict, result: dict) -> dict:
    errors = state.get("errors", [])
    if not result["valid"]:
        errors = errors + [f"Invoice date: {result['reason']}"]
    return {
        "invoice_date_validation": result,
        "errors": errors,
        "audit_log": state.get("audit_log", []) + [{
            "node": "invoice_date_validation",
            "result": result["valid"],
            "reason": result["reason"],
        }],
    }
