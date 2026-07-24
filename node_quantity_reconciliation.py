"""
node_quantity_reconciliation.py — Node: Quantity Reconciliation
================================================================
Deep comparison of quantities across all 3 documents.
Identifies: Exact Match, Partial Delivery, Over Delivery, Under Delivery.

SAP equivalent: MIRO → Quantity tab comparison
Oracle equivalent: AP Invoice Lines → PO Receipts matching
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reconciliation_config import CFG


def _normalise(text: str) -> str:
    return " ".join(str(text).lower().split())


def quantity_reconciliation_node(state: dict) -> dict:
    """Compare Invoice qty vs PO qty vs GRN qty per line item."""
    print("\n" + "="*55)
    print("  QUANTITY RECONCILIATION")
    print("="*55)

    invoice_items = state.get("invoice_data", {}).get("items", [])
    po_items      = state.get("po_data",      {}).get("items", [])
    grn_items     = state.get("grn_data",     {}).get("items", [])

    po_idx  = {_normalise(i.get("item","")): i for i in po_items}
    grn_idx = {_normalise(i.get("item","")): i for i in grn_items}

    item_results = []
    all_match    = True

    for inv_item in invoice_items:
        name    = inv_item.get("item", "")
        key     = _normalise(name)
        inv_qty = int(inv_item.get("quantity", 0))

        po_item  = po_idx.get(key,  {})
        grn_item = grn_idx.get(key, {})

        po_qty   = int(po_item.get("quantity",          0)) if po_item  else 0
        grn_recv = int(grn_item.get("quantity_received", 0)) if grn_item else 0
        grn_acc  = int(grn_item.get("quantity_accepted", 0)) if grn_item else 0

        # Determine delivery status
        if grn_recv == po_qty:
            delivery_status = "Exact Match"
        elif grn_recv < po_qty:
            delivery_status = "Partial Delivery"
        elif grn_recv > po_qty and not CFG.ALLOW_OVER_DELIVERY:
            delivery_status = "Over Delivery"
        else:
            delivery_status = "Exact Match"

        # Rule 1: Invoice qty must not exceed GRN accepted qty
        inv_exceeds_grn = inv_qty > grn_acc if grn_acc else False

        # Rule 2: Invoice qty must not exceed PO qty
        inv_exceeds_po  = inv_qty > po_qty if po_qty else False

        variance = inv_qty - grn_acc if grn_acc else inv_qty - po_qty
        item_ok  = not inv_exceeds_grn and not inv_exceeds_po

        if not item_ok:
            all_match = False

        icon = "✅" if item_ok else "❌"
        print(f"  {icon} '{name}'")
        print(f"       PO qty: {po_qty} | GRN received: {grn_recv} | "
              f"GRN accepted: {grn_acc} | Invoice qty: {inv_qty}")
        print(f"       Delivery: {delivery_status} | "
              f"Invoice vs GRN: {'OK' if not inv_exceeds_grn else 'OVER-INVOICED'}")

        item_results.append({
            "item":             name,
            "po_qty":           po_qty,
            "grn_qty_received": grn_recv,
            "grn_qty_accepted": grn_acc,
            "invoice_qty":      inv_qty,
            "variance":         variance,
            "delivery_status":  delivery_status,
            "invoice_vs_grn":   "Over-Invoiced" if inv_exceeds_grn else "OK",
            "invoice_vs_po":    "Exceeds PO"    if inv_exceeds_po  else "OK",
            "ok":               item_ok,
        })

    overall = "✅ PASS" if all_match else "❌ FAIL"
    print(f"\n  Quantity reconciliation: {overall}")

    result = {
        "match":          all_match,
        "item_results":   item_results,
        "overall_status": "Pass" if all_match else "Fail",
    }

    errors = state.get("errors", [])
    if not all_match:
        failed = [r["item"] for r in item_results if not r["ok"]]
        errors = errors + [f"Quantity mismatch on: {failed}"]

    return {
        "quantity_match": result,
        "errors":         errors,
        "audit_log":      state.get("audit_log", []) + [{
            "node": "quantity_reconciliation",
            "result": all_match,
        }],
    }
