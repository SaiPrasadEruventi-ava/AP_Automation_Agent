"""
erp_mock.py — Mock ERP Lookup (PO + GRN via Redis)
====================================================
Originally this read PO/GRN data from an Excel workbook on local disk.
On Vercel the filesystem is read-only (aside from ephemeral /tmp), so
this now reads from Redis via redis_store.py instead.

One-time migration: run `python migrate_erp_to_redis.py` locally
(with the original ERP_Mock_Data.xlsx present) to load the workbook's
contents into Redis before deploying. After that, this module needs no
local file at all.

Public functions (lookup_po, lookup_grn, lookup_po_and_grn,
list_available_pos) keep the EXACT same signatures/return shapes as
before, so matcher.py, node_*.py, summary_sender.py etc. need ZERO
changes.
"""

import redis_store


def _norm_po(po_number: str) -> str:
    return str(po_number or "").strip().upper()


def lookup_po(po_number: str) -> dict:
    """
    Look up a Purchase Order by PO Number from Redis.

    Returns a dict shaped exactly like document_extractor.extract_po():
        {
          "po_number": str, "vendor_name": str, "po_date": str,
          "currency": str, "po_amount": float,
          "items": [{"item": str, "quantity": int, "unit_price": float}],
        }
    Returns {} if not found.
    """
    print(f"  🔎 ERP lookup: PO {po_number}")
    key = _norm_po(po_number)
    po_data = redis_store.erp_get_po(key)

    if not po_data:
        print(f"    ⚠️  No PO found in ERP for '{po_number}'")
        return {}

    print(f"    ✅ Found: {po_data.get('po_number')} | "
          f"{po_data.get('vendor_name')} | "
          f"${po_data.get('po_amount', 0):,.2f} | "
          f"{len(po_data.get('items', []))} line item(s)")
    return po_data


def lookup_grn(po_number: str) -> dict:
    """
    Look up the Goods Receipt Note associated with a PO Number from Redis.

    Returns a dict shaped exactly like document_extractor.extract_grn():
        {
          "grn_number": str, "po_number": str, "vendor_name": str,
          "received_date": str, "received_by": str,
          "items": [{"item": str, "quantity_ordered": int,
                     "quantity_received": int, "quantity_accepted": int,
                     "condition": str}],
        }
    Returns {} if no GRN has been recorded yet for that PO.
    """
    print(f"  🔎 ERP lookup: GRN for PO {po_number}")
    key = _norm_po(po_number)
    grn_data = redis_store.erp_get_grn(key)

    if not grn_data:
        print(f"    ⚠️  No GRN found in ERP for PO '{po_number}' "
              f"— goods may not have been received yet")
        return {}

    print(f"    ✅ Found: {grn_data.get('grn_number')} | "
          f"received {grn_data.get('received_date')} | "
          f"{len(grn_data.get('items', []))} line item(s)")
    return grn_data


def lookup_po_and_grn(po_number: str) -> tuple:
    """Convenience wrapper — returns (po_data, grn_data) in one call."""
    po_data  = lookup_po(po_number)
    grn_data = lookup_grn(po_number) if po_data else {}
    return po_data, grn_data


def list_available_pos() -> list:
    """Return all PO numbers currently in the mock ERP — useful for diagnostics."""
    return redis_store.erp_list_po_numbers()
