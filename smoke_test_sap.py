"""
smoke_test_sap.py — End-to-end smoke test against live SAP
============================================================
Runs every demo scenario (demo_scenarios.py / data/sap_demo/registry.json,
created by sap_demo_setup.py) through the REAL pipeline:

    invoice -> live SAP PO/GRN lookup -> 3-way match + validations ->
    decision -> SAP posting / parking -> reviewer decision -> SAP

and asserts, per scenario:
  - the decision (Perfect Match / Pending Web Review / Incomplete)
  - exactly which validation checks fail (summary_sender.evaluate_checks)
  - what happened in SAP (Posted / Parked / Skipped) and that SAP's own
    invoice table (RBKP) agrees
  - the invoice PDF really carries the invoice #, PO # and total

Then it exercises the reviewer path (approve -> SAP posts the parked
invoice; reject -> SAP deletes it), proves the demo is repeatable
(reset -> same invoice posts again), and finally resets everything so
the demo starts clean.

Invoice fields come from demo_invoices/<invoice>.json (exactly what the
PDF says) so the test doesn't need the LLM; pass --llm to extract from
the PDFs with Mistral instead (needs MISTRAL_API_KEY).

Usage
-----
  python smoke_test_sap.py                 everything, then reset
  python smoke_test_sap.py --only S01,S05  selected scenarios
  python smoke_test_sap.py --keep          leave SAP documents + logs in place
  python smoke_test_sap.py --llm           extract invoices from the PDFs
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("AP_ERP_SOURCE", "sap")

import run_ap  # noqa: E402  (configures UTF-8 console output)
import erp_mock  # noqa: E402
import erp_sap  # noqa: E402
import pending_reviews  # noqa: E402
import summary_sender  # noqa: E402
from demo_scenarios import SCENARIOS, by_id  # noqa: E402
from sap_demo_setup import INVOICE_DIR, load_registry  # noqa: E402

REVIEW_ACTIONS = {"S03": "Approve", "S06": "Reject"}

results = []


def _check(cond: bool, label: str, detail: str = "") -> bool:
    results[-1]["checks"].append((cond, label, detail))
    print(f"     {'✅' if cond else '❌'} {label}" + (f" — {detail}" if detail and not cond else ""))
    return cond


def _load_invoice(inv_number: str, use_llm: bool) -> dict:
    if use_llm:
        import pdfplumber
        from document_extractor import extract_all_documents
        with pdfplumber.open(INVOICE_DIR / f"{inv_number}.pdf") as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        return extract_all_documents([{"doc_type": "Invoice", "extracted_text": text,
                                       "file_name": f"{inv_number}.pdf"}])["invoice_data"]
    return json.loads((INVOICE_DIR / f"{inv_number}.json").read_text(encoding="utf-8"))["invoice_data"]


def _pdf_ok(inv: dict) -> tuple:
    import pdfplumber
    path = INVOICE_DIR / f"{inv['invoice_number']}.pdf"
    if not path.exists():
        return False, "PDF missing"
    with pdfplumber.open(path) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    need = [inv["invoice_number"], inv["po_number"], f"{inv['total_amount']:,.2f}"]
    missing = [n for n in need if n not in text]
    return not missing, f"not in PDF text: {missing}" if missing else ""


def _failed_checks(result: dict) -> list:
    """Which checklist items fail, from the persisted pending/detail record."""
    reasons = result.get("reasons") or []
    matching, inv, po = {}, {}, {}
    token = result.get("review_token")
    if token:
        rec = pending_reviews.get_pending_review(token)
        matching, inv, po = rec.get("matching", {}), rec.get("invoice_data", {}), rec.get("po_data", {})
    flags = summary_sender.evaluate_checks([], reasons, matching, inv, po)
    return sorted(k for k, v in flags.items() if v)


def _sap_docs(inv_number: str) -> list:
    return [d for d in erp_sap.find_supplier_invoices(reference=inv_number)
            if d["is_posted"] or d["is_parked"]]


def run_case(scn: dict, inv: dict, expected: str, expected_checks: list, label: str) -> dict:
    results.append({"scenario": label, "invoice": inv["invoice_number"], "checks": []})
    print(f"\n── {label}: {scn['title']}  ({inv['invoice_number']} → PO {inv['po_number']})")
    started = time.time()
    po_data, grn_data = erp_mock.lookup_po_and_grn(inv["po_number"])
    _check(bool(po_data), "PO read live from SAP", f"PO {inv['po_number']} not found")
    if not po_data:
        return {}
    result = run_ap.process_invoice_set(invoice_data=inv, po_data=po_data, grn_data=grn_data,
                                        email_ref=None, hitl_mode="web")
    elapsed = time.time() - started
    status = result.get("final_status")
    sap = result.get("sap_posting") or {}
    want_status = {"Manual Review": "Pending Web Review"}.get(expected, expected)
    _check(status == want_status, f"decision = {want_status}", f"got {status}: {result.get('reasons')}")

    failed = _failed_checks(result) if status != "Perfect Match" else []
    if expected != "Incomplete — Missing Documents":
        _check(set(expected_checks) <= set(failed) and (expected_checks or not failed),
               f"failing checks = {expected_checks or 'none'}", f"got {failed}")
    else:
        _check(any("GRN" in r for r in result.get("reasons", [])), "reason names the missing GRN",
               str(result.get("reasons")))

    if expected == "Perfect Match":
        _check(sap.get("action") == "Posted", "SAP: invoice POSTED", f"{sap.get('action')} {sap.get('error')}")
    elif expected == "Manual Review" and "Duplicate Check" not in expected_checks:
        _check(sap.get("action") == "Parked", "SAP: invoice PARKED for review",
               f"{sap.get('action')} {sap.get('error')}")
    elif "Duplicate Check" in expected_checks:
        _check(sap.get("action") == "Skipped", "SAP: duplicate NOT re-created", str(sap.get("action")))

    if sap.get("document"):
        live = {d["document"]: d["status"] for d in _sap_docs(inv["invoice_number"])}
        _check(sap["document"] in live, f"SAP RBKP shows {sap['document']} as {live.get(sap['document'])}",
               f"live docs {live}")
    results[-1]["status"] = status
    results[-1]["sap"] = f"{sap.get('action', '—')} {sap.get('document', '')}".strip()
    results[-1]["failed"] = failed
    results[-1]["seconds"] = round(elapsed, 1)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--llm", action="store_true")
    a = ap.parse_args()

    reg = load_registry().get("scenarios", {})
    targets = [by_id(s.strip()) for s in a.only.split(",")] if a.only else SCENARIOS
    print("=" * 70)
    print(f"  SAP SMOKE TEST — {erp_sap.source_label()} — {len(targets)} scenario(s)")
    print(f"  SAP_INVOICE_MODE={os.getenv('SAP_INVOICE_MODE', 'post')}")
    print("=" * 70)

    invoices = {}
    for scn in targets:
        entry = reg.get(scn["id"], {})
        if not entry.get("invoice_number"):
            print(f"\n⚠️  {scn['id']}: not set up — run sap_demo_setup.py first")
            continue
        inv_no = entry["invoice_number"]
        reset = run_ap.reset_invoice(inv_no)          # clean slate (SAP + logs)
        if reset["sap"]:
            print(f"   (reset {inv_no}: {[r['action'] + ' ' + r['document'] for r in reset['sap']]})")
        inv = _load_invoice(inv_no, a.llm)
        invoices[scn["id"]] = inv
        ok, why = _pdf_ok(inv)
        result = run_case(scn, inv, scn["expected"], scn["checks"], scn["id"])
        _check(ok, "invoice PDF carries invoice #, PO #, total", why)

        if scn.get("expected_second"):  # S05: submit the same invoice again
            run_case(scn, inv, scn["expected_second"], scn["checks_second"], scn["id"] + "-resubmit")

        action = REVIEW_ACTIONS.get(scn["id"])
        if action and result.get("review_token"):
            results.append({"scenario": f"{scn['id']}-review", "invoice": inv_no, "checks": []})
            print(f"\n── {scn['id']}-review: reviewer clicks {action}")
            parked = (result.get("sap_posting") or {}).get("document")
            res = run_ap.resume_after_review(result["review_token"], action, "Smoke Test Reviewer",
                                             "automated smoke test")
            sap = res.get("sap_posting") or {}
            want = "Posted" if action == "Approve" else "Deleted"
            _check(sap.get("action") == want, f"SAP: parked {parked} {want.upper()}",
                   f"{sap.get('action')} {sap.get('error')}")
            live = {d["document"]: d["status"] for d in _sap_docs(inv_no)}
            if want == "Posted":
                _check(live.get(parked) == "Posted", f"RBKP status of {parked} = Posted"
                       + (f" (payment block '{sap.get('payment_block')}')" if sap.get("payment_block") else ""),
                       str(live))
            else:
                _check(parked not in live, f"{parked} no longer live in SAP", str(live))
            results[-1]["status"] = res.get("final_status")
            results[-1]["sap"] = f"{sap.get('action', '—')} {sap.get('document', '')}"

    # ── Repeatability: reset S01 and run it again ──────────────────────
    if "S01" in invoices:
        results.append({"scenario": "S01-reset", "invoice": invoices["S01"]["invoice_number"], "checks": []})
        print("\n── S01-reset: reverse in SAP + clear logs, then process the same invoice again")
        reset = run_ap.reset_invoice(invoices["S01"]["invoice_number"])
        rev = [r for r in reset["sap"] if r["action"] == "Reversed"]
        _check(bool(rev), "SAP: posted invoice REVERSED",
               str([(r['action'], r.get('error')) for r in reset["sap"]]))
        _check(not _sap_docs(invoices["S01"]["invoice_number"]), "no live SAP invoice left")
        results[-1]["status"] = "Reset"
        results[-1]["sap"] = f"Reversed {rev[0].get('reversal_document', '')}" if rev else "—"
        run_case(by_id("S01"), invoices["S01"], "Perfect Match", [], "S01-repeat")

    # ── Final reset so the demo starts clean ────────────────────────────
    if not a.keep:
        print("\n── Final reset (reverse/delete everything the test created in SAP)")
        for sid, inv in invoices.items():
            r = run_ap.reset_invoice(inv["invoice_number"])
            done = [f"{x['action']} {x['document']}" + (f" ({x['error']})" if x.get("error") else "")
                    for x in r["sap"]]
            if done:
                print(f"   {sid} {inv['invoice_number']}: {', '.join(done)}")
            left = _sap_docs(inv["invoice_number"])
            if left:
                print(f"   ❌ {sid}: still live in SAP: {[d['document'] for d in left]}")

    # ── Summary ─────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"  {'CASE':<14}{'INVOICE':<16}{'DECISION':<32}{'SAP':<20}RESULT")
    print("=" * 70)
    passed = 0
    for r in results:
        ok = all(c[0] for c in r["checks"])
        passed += ok
        print(f"  {r['scenario']:<14}{r['invoice']:<16}{str(r.get('status', '—'))[:31]:<32}"
              f"{r.get('sap', '—')[:19]:<20}{'PASS' if ok else 'FAIL'}")
    print("=" * 70)
    print(f"  {passed}/{len(results)} cases passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
