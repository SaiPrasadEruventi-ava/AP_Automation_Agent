"""
migrate_erp_to_redis.py — One-time ERP workbook -> Redis migration
======================================================================
Run this ONCE, LOCALLY, before your first Vercel deploy:

    pip install pandas openpyxl python-dotenv upstash-redis
    python migrate_erp_to_redis.py

It reads the same 4-sheet workbook erp_mock.py used to read directly
(PO_Header / PO_Lines / GRN_Header / GRN_Lines) and writes each PO/GRN
into Redis in exactly the shape erp_mock.lookup_po()/lookup_grn() used
to return, so nothing downstream (matcher.py, node_*.py, ...) needs to
change.

Requires UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN to be set
(in .env for local runs, or already exported in your shell) — pointing
at the SAME Redis instance your Vercel deployment will use.

pandas/openpyxl are only needed for this one-time migration — they are
NOT in the Vercel deployment's requirements.txt, keeping cold starts
fast.

Re-run any time the workbook changes; it fully overwrites each PO/GRN
key it touches (last run wins).
"""

import os
import sys
from pathlib import Path

import pandas as pd

import redis_store

WORKBOOK_PATH = Path(os.getenv("AP_ERP_WORKBOOK", "data/erp/ERP_Mock_Data.xlsx"))


def migrate() -> None:
    if not WORKBOOK_PATH.exists():
        print(f"❌ Workbook not found: {WORKBOOK_PATH}")
        print("   Set AP_ERP_WORKBOOK in .env to point at your ERP_Mock_Data.xlsx")
        sys.exit(1)

    print(f"Reading {WORKBOOK_PATH} ...")
    xl = pd.ExcelFile(WORKBOOK_PATH)
    po_header  = pd.read_excel(xl, sheet_name="PO_Header")
    po_lines   = pd.read_excel(xl, sheet_name="PO_Lines")
    grn_header = pd.read_excel(xl, sheet_name="GRN_Header")
    grn_lines  = pd.read_excel(xl, sheet_name="GRN_Lines")

    po_count = 0
    for _, r in po_header.iterrows():
        po_number = str(r["PO Number"]).strip().upper()
        item_rows = po_lines[
            po_lines["PO Number"].astype(str).str.strip().str.upper() == po_number
        ]
        items = [
            {
                "item":       str(li["Line Item"]),
                "quantity":   int(li["Quantity"]),
                "unit_price": float(li["Unit Price"]),
                "uom":        str(li.get("UOM", "EA")),
            }
            for _, li in item_rows.iterrows()
        ]
        po_data = {
            "po_number":     str(r["PO Number"]),
            "vendor_name":   str(r["Vendor Name"]),
            "po_date":       str(r["PO Date"])[:10],
            "po_status":     str(r.get("PO Status", "Open")),
            "currency":      str(r.get("Currency", "USD")),
            "payment_terms": str(r.get("Payment Terms", "")),
            "tax_type":      str(r.get("Tax Type", "")),
            "subtotal":      float(r.get("Subtotal", 0)),
            "tax":           float(r.get("Tax", 0)),
            "freight":       float(r.get("Freight", 0)),
            "discount":      float(r.get("Discount", 0)),
            "po_amount":     float(r["PO Amount"]),
            "items":         items,
        }
        redis_store.erp_set_po(po_number, po_data)
        po_count += 1
        print(f"  ✅ PO {po_number} -> Redis ({len(items)} line item(s))")

    grn_count = 0
    for _, r in grn_header.iterrows():
        po_number  = str(r["PO Number"]).strip().upper()
        grn_number = str(r["GRN Number"])
        item_rows  = grn_lines[grn_lines["GRN Number"].astype(str).str.strip() == grn_number]
        items = [
            {
                "item":              str(li["Line Item"]),
                "quantity_ordered":  int(li["Quantity Ordered"]),
                "quantity_received": int(li["Quantity Received"]),
                "quantity_accepted": int(li["Quantity Accepted"]),
                "condition":         str(li.get("Condition", "Good")),
            }
            for _, li in item_rows.iterrows()
        ]
        grn_data = {
            "grn_number":    grn_number,
            "po_number":     str(r["PO Number"]),
            "vendor_name":   str(r["Vendor Name"]),
            "received_date": str(r["Received Date"])[:10],
            "received_by":   str(r.get("Received By", "")),
            "items":         items,
        }
        redis_store.erp_set_grn(po_number, grn_data)
        grn_count += 1
        print(f"  ✅ GRN {grn_number} (PO {po_number}) -> Redis ({len(items)} line item(s))")

    print(f"\nMigrated {po_count} PO(s) and {grn_count} GRN(s) to Redis.")
    print("erp_mock.py will now read from Redis — no workbook needed at runtime.")


if __name__ == "__main__":
    migrate()
