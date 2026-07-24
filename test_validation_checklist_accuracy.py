"""
test_validation_checklist_accuracy.py — Regression test for the
Validation Checklist (the 12-item ✅/❌ list in the result emails)
========================================================================
Verifies that each of the 12 checklist items (PO Validity, GRN
Validation, Invoice Date, Vendor/Header, Unit Price, Unit of Measure,
Quantity, Tax Rate & Amount, Freight & Discount, Payment Terms, Line
Amounts, Invoice Total) is marked failed ONLY when that specific
control is actually the cause of Manual Review — never as a side
effect of incidental word overlap with unrelated reason text.

Background: the original _checklist_with_failures() tokenized every
reason string into individual lower-case words and flagged a checklist
item as failed if ANY word in its name appeared ANYWHERE in that pool.
Because reason strings routinely contain "PO $150.00" or
"Invoice $163.00", this caused widespread false positives — e.g. a
pure Vendor Mismatch (S11) would ALSO show "Invoice Date" and
"Invoice Total" as failed, despite neither having anything to do with
the actual problem. Across all 9 review-triggering scenarios, the old
logic produced 14 false positives and 2 false negatives.

This test locks in the fix: each scenario's checklist must show
EXACTLY the expected failing item(s) — no more, no less.

Run from the ap_desktop_v2 directory:

    python test_validation_checklist_accuracy.py
"""

import sys
import os
import re
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
from summary_sender import _checklist_with_failures, _ALL_CHECKS
from invoice_data import INVOICES

VARIANCE_THRESHOLD = 2.0

# Ground truth: exactly which checklist item(s) SHOULD fail per scenario.
# An empty list means a Perfect Match (or, for S05, a duplicate — which
# is its own special-case decision and shouldn't trip any of the normal
# 12 document/line-level checks).
EXPECTED_FAILURES = {
    "S01": [],                       # Perfect Match
    "S02": [],                       # Price variance within tolerance — still Perfect Match
    "S03": ["Unit Price"],
    "S04": ["Quantity"],             # short delivery + damaged goods
    "S05": [],                       # Duplicate Invoice — special case, not a checklist item
    "S06": ["Tax Rate & Amount"],
    "S07": ["Unit of Measure"],
    "S08": ["Payment Terms"],
    "S09": ["Freight & Discount"],
    "S10": ["Quantity"],             # damaged goods / QC rejection
    "S11": ["Vendor / Header"],
}

PASS, FAIL = [], []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def get_checklist_failures(scenario_id: str):
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

    failed_checks = decision_info.get("failed_checks", []) + decision_info["reasons"]
    html = _checklist_with_failures(
        failed_checks, decision_info["reasons"], matching, invoice_data, po_data
    )
    items = re.findall(r"<span class='chk-icon'>(.)</span>.*?<strong>([^<]+)</strong>", html)
    failed = [name for icon, name in items if icon == "❌"]
    return failed, decision_info["reasons"]


print("="*78)
print("  VALIDATION CHECKLIST ACCURACY — REGRESSION TEST")
print("="*78)

for sid, expected in EXPECTED_FAILURES.items():
    print(f"\n[{sid}] Expected failing item(s): {expected or '(none — Perfect Match)'}")
    actual, reasons = get_checklist_failures(sid)

    false_positives = [f for f in actual if f not in expected]
    false_negatives = [f for f in expected if f not in actual]

    check(f"{sid}: exact match — no false positives",
          not false_positives,
          f"unexpectedly failed: {false_positives}")
    check(f"{sid}: exact match — no false negatives",
          not false_negatives,
          f"should have failed but didn't: {false_negatives}")

    # Specific regression guard for the exact bug reported: S11 (vendor
    # mismatch) must NEVER show Invoice Date or Invoice Total as failed.
    if sid == "S11":
        check("S11: 'Invoice Date' is NOT falsely failed",
              "Invoice Date" not in actual)
        check("S11: 'Invoice Total' is NOT falsely failed",
              "Invoice Total" not in actual)
        check("S11: 'PO Validity' is NOT falsely failed",
              "PO Validity" not in actual)

    # Every check name not in `expected` must be passing (✅) — this is
    # the comprehensive version of the assertions above, checked against
    # the full list of all 12 possible checks.
    all_check_names = [name for name, _ in _ALL_CHECKS]
    unexpected_in_actual = set(actual) - set(expected)
    check(f"{sid}: every non-relevant check passes",
          len(unexpected_in_actual) == 0,
          f"wrongly failing: {unexpected_in_actual}")

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
