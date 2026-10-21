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

Live SAP
---------
With AP_ERP_SOURCE=sap (.env) every lookup is routed to erp_sap.py and
read live from SAP over RFC on each call — no Redis copy, no cache. The
Redis mock below remains for AP_ERP_SOURCE=mock (local testing without
SAP access, and the original test_*.py scripts).
"""

import os

import redis_store


def erp_source() -> str:
    return os.getenv("AP_ERP_SOURCE", "mock").strip().lower()


def using_sap() -> bool:
    return erp_source() == "sap"


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
    if using_sap():
        return lookup_po_and_grn(po_number)[0]
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
    if using_sap():
        return lookup_po_and_grn(po_number)[1]
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
    """Convenience wrapper — returns (po_data, grn_data) in one call.
    In SAP mode both come from one live RFC session."""
    if using_sap():
        import erp_sap
        print(f"  🔎 SAP lookup ({erp_sap.source_label()}): PO {po_number}")
        po_data, grn_data = erp_sap.lookup_po_and_grn(po_number)
        if not po_data:
            print(f"    ⚠️  PO '{po_number}' not found in SAP")
        else:
            print(f"    ✅ PO {po_data['po_number']} | {po_data['vendor_name']} | "
                  f"{po_data['currency']} {po_data['po_amount']:,.2f} | "
                  f"status {po_data['po_status']} | {len(po_data['items'])} line(s)")
            print(f"    {'✅ GR ' + grn_data['grn_number'] + ' | received ' + grn_data['received_date'] if grn_data else '⚠️  No goods receipt posted in SAP yet'}")
        return po_data, grn_data
    po_data  = lookup_po(po_number)
    grn_data = lookup_grn(po_number) if po_data else {}
    return po_data, grn_data


def list_available_pos() -> list:
    """Return all PO numbers currently in the mock ERP — useful for diagnostics."""
    if using_sap():
        import erp_sap
        return erp_sap.recent_po_numbers()
    return redis_store.erp_list_po_numbers()
