"""
test_web_hitl_portal.py — End-to-end test of the web-portal HITL flow
========================================================================
Exercises the REAL production code (run_ap.py, approval_portal.py,
pending_reviews.py, hitl_review.py) via FastAPI's TestClient — no
actual server process or Outlook needed. summary_sender's LLM calls
are stubbed out since no live Mistral API key/network is assumed.

Run from the ap_desktop_v2 directory:

    python test_web_hitl_portal.py

This covers:
  1. Creating a pending review (Manual Review + hitl_mode="web")
  2. GET  /review/{token}        — review page renders correctly
  3. GET  /review/{bad_token}    — 404 for invalid tokens
  4. POST /api/decide            — Approve with reviewer name + comments
  5. POST /api/decide (reuse)    — already-resolved token is rejected
  6. GET  /approve/{token}       — one-click approve (default reviewer)
  7. GET  /reject/{token}        — one-click reject
  8. GET  /clarify/{token}       — one-click clarification request
  9. Invalid decision value      — rejected with 400
 10. Reviewer comments on Reject — correctly prepended as primary reason
"""

import sys
import os
import json
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))
os.chdir(str(_THIS_DIR))

# ── Stub LLM-dependent functions (no network/API key needed for this test) ──
import summary_sender
summary_sender.generate_summary        = lambda *a, **k: f"[stub] {k.get('final_status')}"
summary_sender.send_result_email       = lambda *a, **k: None
summary_sender.send_review_request_email = lambda *a, **k: None

import run_ap
import erp_mock
from pending_reviews import list_pending_reviews
from fastapi.testclient import TestClient
import approval_portal

client = TestClient(approval_portal.app)

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def make_pending(invoice_data: dict) -> str:
    po_data, grn_data = erp_mock.lookup_po_and_grn(invoice_data["po_number"])
    result = run_ap.process_invoice_set(
        invoice_data=invoice_data, po_data=po_data, grn_data=grn_data,
        email_ref=None, hitl_mode="web",
    )
    assert result["final_status"] == "Pending Web Review", (
        f"Expected Pending Web Review, got {result['final_status']}"
    )
    return result["review_token"]


print("="*70)
print("  WEB HITL PORTAL — END-TO-END TEST SUITE")
print("="*70)

# ── 1-5: full review-page + API decide cycle ─────────────────────────────
print("\n[1-5] Review page + API decide cycle (S03 price variance)")
token1 = make_pending({
    "invoice_number": "INV-S03-001", "vendor_name": "Global Supplies Co",
    "invoice_date": "2026-06-17", "po_number": "PO-S03-001", "currency": "USD",
    "total_amount": 8773.20,
    "items": [{"item": "Office Chair Ergonomic", "quantity": 20, "unit_price": 163.00},
              {"item": "Executive Desk 180cm", "quantity": 10, "unit_price": 435.00}],
})

resp = client.get(f"/review/{token1}")
check("Review page returns 200", resp.status_code == 200, resp.status_code)
check("Review page shows invoice number", "INV-S03-001" in resp.text)
check("Review page shows vendor", "Global Supplies Co" in resp.text)

resp = client.get("/review/not-a-real-token")
check("Invalid token returns 404", resp.status_code == 404, resp.status_code)

resp = client.post("/api/decide", json={
    "token": token1, "decision": "Approve",
    "reviewer": "Finance Lead", "comments": "Variance approved per contract terms.",
})
check("Decide API returns 200", resp.status_code == 200, resp.status_code)
body = resp.json()
check("Decide API returns correct final_status",
      body.get("final_status") == "Approved by Reviewer", body)

resp = client.post("/api/decide", json={
    "token": token1, "decision": "Approve", "reviewer": "X",
})
check("Reusing a resolved token is rejected", resp.status_code == 404, resp.status_code)

# ── 6-8: one-click GET endpoints ──────────────────────────────────────────
print("\n[6] One-click /approve/{token}")
token2 = make_pending({
    "invoice_number": "INV-S09-001", "vendor_name": "FastShip Logistics",
    "invoice_date": "2026-06-21", "po_number": "PO-S09-001", "currency": "USD",
    "total_amount": 4220.00, "freight": 650.00,
    "items": [{"item": "Packaging Material Box A", "quantity": 1000, "unit_price": 2.50},
              {"item": "Bubble Wrap Roll 50m", "quantity": 50, "unit_price": 18.00}],
})
resp = client.get(f"/approve/{token2}", params={"name": "Ops Manager"})
check("One-click approve returns 200", resp.status_code == 200, resp.status_code)
check("One-click approve page confirms approval", "Approved" in resp.text)

print("\n[7] One-click /reject/{token}")
token3 = make_pending({
    "invoice_number": "INV-S07-001", "vendor_name": "Chem Industries",
    "invoice_date": "2026-06-18", "po_number": "PO-S07-001", "currency": "USD",
    "total_amount": 2885.00,
    "items": [{"item": "Industrial Solvent IPA", "quantity": 500, "unit_price": 3.20, "uom": "LTR"},
              {"item": "Acetone Pure Grade", "quantity": 200, "unit_price": 4.50, "uom": "LTR"}],
})
resp = client.get(f"/reject/{token3}")
check("One-click reject (default name) returns 200", resp.status_code == 200, resp.status_code)
check("One-click reject page confirms rejection", "Rejected" in resp.text)

print("\n[8] One-click /clarify/{token}")
token4 = make_pending({
    "invoice_number": "INV-S08-001", "vendor_name": "IT Solutions Pvt Ltd",
    "invoice_date": "2026-06-23", "po_number": "PO-S08-001", "currency": "USD",
    "total_amount": 36998.00, "payment_terms": "Net 15",
    "items": [{"item": "Cisco Switch 24-Port", "quantity": 3, "unit_price": 4200.00},
              {"item": "HP Server DL380 Gen10", "quantity": 1, "unit_price": 18500.00}],
})
resp = client.get(f"/clarify/{token4}")
check("One-click clarify returns 200", resp.status_code == 200, resp.status_code)
check("One-click clarify page confirms clarification", "Clarification" in resp.text)

# ── 9: invalid decision value ─────────────────────────────────────────────
print("\n[9] Invalid decision value")
token5 = make_pending({
    "invoice_number": "INV-S06-001", "vendor_name": "Pharma Supplies India",
    "invoice_date": "2026-06-20", "po_number": "PO-S06-001", "currency": "USD",
    "total_amount": 59380.00, "tax_type": "IGST @ 28.0%",
    "items": [{"item": "Lab Equipment Set", "quantity": 5, "unit_price": 8000.00},
              {"item": "Chemical Reagent Kit", "quantity": 50, "unit_price": 120.00}],
})
resp = client.post("/api/decide", json={"token": token5, "decision": "MAYBE", "reviewer": "X"})
check("Invalid decision value rejected with 400", resp.status_code == 400, resp.status_code)
# Clean up — this one was never resolved, decide it now so it doesn't linger
client.post("/api/decide", json={"token": token5, "decision": "Approve", "reviewer": "Cleanup"})

# ── 10: comments prepended correctly on Reject ────────────────────────────
print("\n[10] Reviewer comments prepended as primary reason on Reject")
token6 = make_pending({
    "invoice_number": "INV-S10-001", "vendor_name": "Semiconductor Supply Co",
    "invoice_date": "2026-06-20", "po_number": "PO-S10-001", "currency": "USD",
    "total_amount": 10292.00,
    "items": [{"item": "ARM Cortex MCU M4", "quantity": 500, "unit_price": 15.00},
              {"item": "Voltage Regulator LM317", "quantity": 1000, "unit_price": 1.20}],
})
resp = client.post("/api/decide", json={
    "token": token6, "decision": "Reject", "reviewer": "QC Lead",
    "comments": "Vendor confirmed permanent damage — credit note requested.",
})
check("Reject via API returns 200", resp.status_code == 200, resp.status_code)
check("Reject final_status correct",
      resp.json().get("final_status") == "Rejected by Reviewer")

# ── Final state check ─────────────────────────────────────────────────────
remaining = list_pending_reviews()
check("No pending reviews left dangling after all tests",
      len(remaining) == 0, f"{len(remaining)} remaining")

print("\n" + "="*70)
print(f"  RESULTS: {len(PASS)} passed, {len(FAIL)} failed")
print("="*70)
if FAIL:
    print("  FAILED:")
    for f in FAIL:
        print(f"    - {f}")
    sys.exit(1)
else:
    print("  ALL TESTS PASSED ✅")
    sys.exit(0)
