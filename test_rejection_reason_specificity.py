"""
test_rejection_reason_specificity.py — Regression test for reason quality
=============================================================================
Verifies that Manual Review reasons are SPECIFIC to their actual cause
(unit price, tax rate, freight, UOM, payment terms, quantity/GRN) rather
than a generic "Price Variance — Exceeds Tolerance" label slapped on
every total-amount mismatch regardless of why the totals differ.

Background: the original implementation had a catch-all check at the
end of apply_business_rules() that fired whenever the aggregate
invoice-vs-PO dollar variance exceeded the tolerance threshold, labelled
unconditionally as "Price Variance — Exceeds Tolerance" — even when the
real cause was a tax rate difference (S06) or a freight overcharge (S09)
that already had their own, more specific reason reported. This test
locks in the fix: each scenario's reasons must be attributable to its
actual cause, with no redundant generic line when a specific one exists.

Run from the ap_desktop_v2 directory:

    python test_rejection_reason_specificity.py
"""

import sys
import os
from pathlib import Path

_THIS_DIR  = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
_TEST_DATA = _REPO_ROOT / "test_scenarios_v3"

sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, str(_TEST_DATA))
os.chdir(str(_THIS_DIR))

import erp_mock
from matcher import run_three_way_match, apply_business_rules
from duplicate_checker import check_duplicate
from invoice_data import INVOICES

VARIANCE_THRESHOLD = 2.0

PASS, FAIL = [], []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def get_reasons(scenario_id: str) -> list:
    invoice_data = INVOICES[scenario_id]
    po_data, grn_data = erp_mock.lookup_po_and_grn(invoice_data["po_number"])
    dup_check = check_duplicate(
        invoice_number=invoice_data.get("invoice_number", ""),
        vendor_name=invoice_data.get("vendor_name", ""),
        amount=invoice_data.get("total_amount", 0),
        invoice_date=invoice_data.get("invoice_date", ""),
    )
    matching = run_three_way_match(invoice_data, po_data, grn_data,
                                    variance_threshold=VARIANCE_THRESHOLD)
    state = {"invoice_data": invoice_data, "po_data": po_data,
             "grn_data": grn_data, "duplicate_check": dup_check}
    decision_info = apply_business_rules(matching, VARIANCE_THRESHOLD, state=state)
    return decision_info["reasons"]


def has_generic_catchall(reasons: list) -> bool:
    """The old, removed phrasing — should never appear again."""
    return any("Exceeds Tolerance" in r and "Tax Mismatch" not in r and
               "Damaged" not in r for r in reasons
               if r.strip().startswith("Price Variance — Exceeds Tolerance"))


print("="*78)
print("  REJECTION REASON SPECIFICITY — REGRESSION TEST")
print("="*78)

print("\n[S03] Unit price variance — reasons must cite the specific line items")
r = get_reasons("S03")
check("Cites 'Office Chair Ergonomic' by name",
      any("Office Chair Ergonomic" in x for x in r))
check("Cites 'Executive Desk 180cm' by name",
      any("Executive Desk 180cm" in x for x in r))
check("No redundant generic catch-all line", not has_generic_catchall(r))
check("Exactly 2 reasons (no third generic line)", len(r) == 2, r)

print("\n[S06] Tax rate mismatch — must be tax-specific, NOT mislabelled as price")
r = get_reasons("S06")
check("Reason mentions 'Tax Mismatch'", any("Tax Mismatch" in x for x in r))
check("Reason cites the actual rates (28.0% vs 18.0%)",
      any("28.0%" in x and "18.0%" in x for x in r))
check("No generic 'Price Variance — Exceeds Tolerance' line", not has_generic_catchall(r))
check("Exactly 1 reason (tax mismatch fully explains the variance)",
      len(r) == 1, r)

print("\n[S07] UOM mismatch — must name both items and both units")
r = get_reasons("S07")
check("Cites 'Industrial Solvent IPA'", any("Industrial Solvent IPA" in x for x in r))
check("Cites 'Acetone Pure Grade'", any("Acetone Pure Grade" in x for x in r))
check("Cites invoice UOM 'LTR' and PO UOM 'KG'",
      any("LTR" in x and "KG" in x for x in r))
check("No generic price-variance line", not has_generic_catchall(r))

print("\n[S08] Payment terms mismatch — must cite both terms")
r = get_reasons("S08")
check("Cites 'Net 15' (invoice) and 'Net 30' (PO)",
      any("Net 15" in x and "Net 30" in x for x in r))
check("No generic price-variance line", not has_generic_catchall(r))
check("Exactly 1 reason", len(r) == 1, r)

print("\n[S09] Freight overcharge — must be freight-specific, no redundant catch-all")
r = get_reasons("S09")
check("Reason mentions 'Freight Overcharge'", any("Freight Overcharge" in x for x in r))
check("Cites actual dollar amounts ($650 vs $150)",
      any("650" in x and "150" in x for x in r))
check("No generic 'Price Variance — Exceeds Tolerance' line", not has_generic_catchall(r))
check("Exactly 1 reason (freight fully explains the variance — this was "
      "the exact bug reported: a redundant generic line used to appear here)",
      len(r) == 1, r)

print("\n[S10] Damaged goods — must cite the specific item and condition")
r = get_reasons("S10")
check("Cites 'ARM Cortex MCU M4'", any("ARM Cortex MCU M4" in x for x in r))
check("Cites the QC/ESD damage condition", any("ESD" in x for x in r))
check("No generic price-variance line", not has_generic_catchall(r))

print("\n[S11] Vendor mismatch — must cite both vendor names")
r = get_reasons("S11")
check("Cites 'Delta Office Supplies'", any("Delta Office Supplies" in x for x in r))
check("Cites 'Sigma Stationery Works'", any("Sigma Stationery Works" in x for x in r))

print("\n[S04] Short delivery + damaged goods — both causes named, no generic noise")
r = get_reasons("S04")
check("Cites short delivery for MCU-32", any("MCU-32" in x and "Short Delivery" in x for x in r))
check("Cites damaged goods for PCB Assembly", any("PCB Assembly" in x and "Damaged" in x for x in r))
check("No generic price-variance line", not has_generic_catchall(r))

print("\n" + "="*78)
print(f"  RESULTS: {len(PASS)} passed, {len(FAIL)} failed")
print("="*78)
if FAIL:
    print("  FAILED:")
    for f in FAIL:
        print(f"    - {f}")
    sys.exit(1)
else:
    print("  ALL TESTS PASSED ✅")
    sys.exit(0)
