"""
hitl_review.py — Human-in-the-Loop Review
==========================================
Capability (b): When matching finds discrepancies,
pause the workflow and prompt for human decision.

Three modes:
  1. Interactive CLI    — reviewer types in terminal (blocks the run)
  2. Email reply        — agent sends review request email,
                           reviewer replies APPROVE/REJECT/CLARIFICATION
                           (not currently polled automatically)
  3. Web portal         — agent sends review request email with
                           clickable Approve/Reject/Clarify links.
                           Reviewer enters their name + comments on a
                           small local web page (approval_portal.py).
                           This mode does NOT block the run — see
                           pending_reviews.py + run_ap.resume_after_review().
"""

import os
from datetime import datetime


DECISION_TO_FINAL_STATUS = {
    "Approve":               "Approved by Reviewer",
    "Reject":                "Rejected by Reviewer",
    "Request Clarification": "Pending Clarification",
}

FINAL_STATUS_ICONS = {
    "Approved by Reviewer":  "✅",
    "Rejected by Reviewer":  "❌",
    "Pending Clarification": "🔼",
}


def record_web_decision(decision: str, reviewer: str, comments: str = "") -> dict:
    """
    Build the same shape of result dict that get_human_decision_interactive()
    produces, but from a web-portal submission instead of a terminal prompt.

    decision must already be one of: "Approve", "Reject", "Request Clarification"
    (the API layer in approval_portal.py is responsible for validating this
    before calling here).
    """
    reviewer  = (reviewer or "").strip() or "Web Portal Reviewer"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    final_status = DECISION_TO_FINAL_STATUS.get(decision, "Manual Review Required")
    icon = FINAL_STATUS_ICONS.get(final_status, "📋")
    print(f"\n  {icon} Web portal decision recorded: {final_status} "
          f"by {reviewer} at {timestamp}")

    return {
        "final_status":  final_status,
        "reviewer":      reviewer,
        "decision":      decision,
        "notes":         (comments or "").strip(),
        "timestamp":     timestamp,
        "review_method": "web_portal",
    }


def show_review_panel(invoice_data: dict,
                      po_data: dict,
                      grn_data: dict,
                      matching: dict,
                      decision_info: dict) -> None:
    """Display a clear review panel for the human approver."""

    inv_num  = invoice_data.get("invoice_number", "N/A")
    vendor   = invoice_data.get("vendor_name",    "N/A")
    po_num   = po_data.get("po_number",           "N/A")
    grn_num  = grn_data.get("grn_number",         "N/A")
    amount   = float(invoice_data.get("total_amount", 0))
    po_amt   = float(po_data.get("po_amount",      0))
    var_pct  = matching.get("variance_percentage", 0)
    variance = matching.get("variance",            0)
    reasons  = decision_info.get("reasons",        [])

    print("\n" + "█"*60)
    print("  ⏸  MANUAL REVIEW REQUIRED — HUMAN APPROVAL NEEDED")
    print("█"*60)
    print(f"\n  📄 INVOICE DETAILS")
    print(f"  {'─'*55}")
    print(f"  Invoice #   : {inv_num}")
    print(f"  Vendor      : {vendor}")
    print(f"  PO Number   : {po_num}")
    print(f"  GRN Number  : {grn_num}")
    print(f"  Inv Amount  : ${amount:,.2f}")
    print(f"  PO Amount   : ${po_amt:,.2f}")
    print(f"  Variance    : ${variance:,.2f} ({var_pct:.2f}%)")

    print(f"\n  ⚠️  REASONS FOR REVIEW")
    print(f"  {'─'*55}")
    for r in reasons:
        print(f"  • {r}")

    # Show mismatched line items
    mismatched = [i for i in matching.get("item_results", [])
                  if i.get("status") != "Matched"]
    if mismatched:
        print(f"\n  📦 MISMATCHED ITEMS")
        print(f"  {'─'*55}")
        for item in mismatched:
            print(f"  Item       : {item.get('item')}")
            print(f"  Invoice qty: {item.get('invoice_qty')} | "
                  f"PO qty: {item.get('po_qty')} | "
                  f"GRN accepted: {item.get('grn_accepted')}")
            print(f"  Status     : {item.get('invoice_vs_po')} / "
                  f"{item.get('invoice_vs_grn')} / {item.get('po_vs_grn')}")
            print()

    print("█"*60)


def get_human_decision_interactive() -> dict:
    """
    Prompt reviewer for decision in terminal.
    Returns decision dict.
    """
    print("\n  Enter your details to complete the review:")
    print()

    reviewer = input("  Your name (reviewer): ").strip()
    if not reviewer:
        reviewer = "AP Reviewer"

    print()
    print("  Available decisions:")
    print("    approve      → Approve and process for payment")
    print("    reject       → Reject invoice — return to vendor")
    print("    clarify      → Request clarification from vendor")
    print()

    decision_map = {
        "approve":       "Approve",
        "approved":      "Approve",
        "reject":        "Reject",
        "rejected":      "Reject",
        "clarify":       "Request Clarification",
        "clarification": "Request Clarification",
    }

    while True:
        raw = input("  Your decision [approve/reject/clarify]: ").strip().lower()
        if raw in decision_map:
            decision = decision_map[raw]
            break
        print(f"  ❌ Invalid. Please type: approve, reject, or clarify")

    notes = input("  Comments/notes (optional — press Enter to skip): ").strip()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n  ✅ Decision recorded: {decision} by {reviewer} at {timestamp}")

    return {
        "decision":        decision,
        "reviewer":        reviewer,
        "notes":           notes,
        "timestamp":       timestamp,
        "review_method":   "interactive_cli",
    }


def process_hitl_review(invoice_data: dict,
                         po_data: dict,
                         grn_data: dict,
                         matching: dict,
                         decision_info: dict) -> dict:
    """
    Full HITL review flow:
    1. Show review panel
    2. Get human decision
    3. Return updated decision

    Returns:
        {
            "final_status": str,
            "reviewer": str,
            "decision": str,
            "notes": str,
            "timestamp": str,
        }
    """
    show_review_panel(invoice_data, po_data, grn_data, matching, decision_info)
    review = get_human_decision_interactive()

    decision     = review["decision"]
    final_status = DECISION_TO_FINAL_STATUS.get(decision, "Manual Review Required")
    icon         = FINAL_STATUS_ICONS.get(final_status, "📋")
    print(f"\n  {icon} Final Status: {final_status}")

    return {
        "final_status": final_status,
        "reviewer":     review["reviewer"],
        "decision":     decision,
        "notes":        review["notes"],
        "timestamp":    review["timestamp"],
    }
