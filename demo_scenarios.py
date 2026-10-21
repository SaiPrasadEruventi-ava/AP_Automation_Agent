"""
demo_scenarios.py — SAP-backed demo scenario catalog
=====================================================
One entry per demo case. sap_demo_setup.py turns each entry into REAL
SAP documents (purchase order, goods receipts / returns / PO closure)
and then renders the matching supplier-invoice PDF that references the
real SAP PO number — so every case runs through the live 3-way match
exactly like a vendor's emailed invoice would.

Organisational data (S/4HANA A4F, best-practice US company code 1710):
company code 1710, purchasing org 1710, plant 1710, purchasing group
001, account assignment K (cost center 17101101, G/L 65100000), tax code
I1 = A/P sales tax — SAP derives jurisdiction CA00000000 (6.00%) from the
cost center. Override via SAP_DEMO_* in .env if your client differs.

Each scenario:
    vendor         SAP supplier (LFA1) — invoices print its SAP name
    terms          PO payment terms key (T052)
    lines          (short text, qty, SAP unit, unit price, tax code)
    freight        planned delivery cost on the PO (condition FVA1)
    discount       absolute discount on line 1 (condition DRV1)
    gr             goods receipt plan per line: qty received, item text,
                   and optional QC return (qty, reason code, text)
    close_po       mark lines delivery-completed + final-invoiced
    po_days_ago    PO document date offset (default 0 = setup day)
    invoice        how the vendor's invoice deviates from the PO
    expected       what the AP engine should decide
    checks         which validation(s) should fail (for the smoke test)
"""

import os

ORG = {
    "company_code":   os.getenv("SAP_DEMO_COMPANY_CODE", "1710"),
    "purch_org":      os.getenv("SAP_DEMO_PURCH_ORG", "1710"),
    "purch_group":    os.getenv("SAP_DEMO_PURCH_GROUP", "001"),
    "plant":          os.getenv("SAP_DEMO_PLANT", "1710"),
    "material_group": os.getenv("SAP_DEMO_MATL_GROUP", "YBMM00"),
    "gl_account":     os.getenv("SAP_DEMO_GL_ACCOUNT", "0065100000"),
    "cost_center":    os.getenv("SAP_DEMO_COST_CENTER", "0017101101"),
    "doc_type":       os.getenv("SAP_DEMO_PO_TYPE", "ENB"),   # NB has flexible workflow (stuck "In Release")
    "currency":       os.getenv("SAP_DEMO_CURRENCY", "USD"),
    "bill_to":        "Avaali Solutions — Accounts Payable, 3410 Hillview Ave, Palo Alto CA 94304",
}

TAX_LABEL = "CA Sales Tax"

SCENARIOS = [
    {
        "id": "S01", "title": "Perfect Match — Auto Post",
        "vendor": "USSU-VSF01", "terms": "NT30",
        "lines": [("Dell Laptop XPS-15", 10, "EA", 1200.00, "I1"),
                  ("Samsung Monitor 27-inch", 5, "EA", 450.00, "I1")],
        "freight": 150.00,
        "gr": [{"qty": 10}, {"qty": 5}],
        "invoice": {},
        "expected": "Perfect Match", "checks": [],
        "story": "Invoice, PO and GRN agree on every field. The agent posts the "
                 "supplier invoice in SAP automatically (MIRO document).",
    },
    {
        "id": "S02", "title": "Price Within Tolerance — Auto Post",
        "vendor": "USSU-VSF02", "terms": "NT45",
        "lines": [("Steel Rods Grade A", 200, "KG", 25.00, "I1"),
                  ("Welding Machine WM-500", 2, "EA", 1500.00, "I1")],
        "freight": 320.00,
        "gr": [{"qty": 200}, {"qty": 2}],
        "invoice": {"prices": {1: 1525.00}},
        "expected": "Perfect Match", "checks": [],
        "story": "Welding machine billed at $1,525 vs PO $1,500 (+1.7%) — inside the 2% "
                 "tolerance, so it still auto-posts in SAP.",
    },
    {
        "id": "S03", "title": "Price Variance — Exceeds Tolerance",
        "vendor": "USSU-VSF50", "terms": "NT30",
        "lines": [("Office Chair Ergonomic", 20, "EA", 150.00, "I1"),
                  ("Executive Desk 180cm", 10, "EA", 400.00, "I1")],
        "freight": 250.00,
        "gr": [{"qty": 20}, {"qty": 10}],
        "invoice": {"prices": {0: 163.00, 1: 435.00}},
        "expected": "Manual Review", "checks": ["Unit Price"],
        "story": "Both lines billed ~8.7% above the SAP PO price. Parked in SAP for review.",
    },
    {
        "id": "S04", "title": "Short Delivery — Partial GRN",
        "vendor": "USSU-VSF51", "terms": "NT60",
        "lines": [("Microcontroller MCU-32", 500, "EA", 12.50, "I1"),
                  ("PCB Assembly Board", 200, "EA", 35.00, "I1")],
        "freight": 180.00,
        "gr": [{"qty": 450, "text": "50 units still in transit"},
               {"qty": 180, "text": "20 units not delivered"}],
        "invoice": {},
        "expected": "Manual Review", "checks": ["Quantity"],
        "story": "SAP shows only 450/500 and 180/200 received, but the vendor bills the "
                 "full PO quantity.",
    },
    {
        "id": "S05", "title": "Duplicate Invoice",
        "vendor": "USSU-VSF52", "terms": "NT30",
        "lines": [("Freight Forwarding Service", 1, "EA", 2500.00, "I0"),
                  ("Customs Clearance Fee", 1, "EA", 400.00, "I0")],
        "freight": 0.0,
        "gr": [{"qty": 1}, {"qty": 1}],
        "invoice": {},
        "expected": "Perfect Match", "expected_second": "Manual Review",
        "checks": [], "checks_second": ["Duplicate Check"],
        "story": "Submit it TWICE. The first run posts in SAP; the second is caught as a "
                 "duplicate — by the AP log and by SAP's own invoice table (RBKP).",
    },
    {
        "id": "S06", "title": "Tax Rate Mismatch",
        "vendor": "USSU-VSF01", "terms": "NT45",
        "lines": [("Lab Equipment Set", 5, "EA", 8000.00, "I1"),
                  ("Chemical Reagent Kit", 50, "PAK", 120.00, "I1")],
        "freight": 500.00,
        "gr": [{"qty": 5}, {"qty": 50}],
        "invoice": {"tax_rate": 8.25},
        "expected": "Manual Review", "checks": ["Tax Rate & Amount"],
        "story": "Vendor charges 8.25% sales tax; SAP's tax code I1 for jurisdiction "
                 "CA00000000 is 6.00%.",
    },
    {
        "id": "S07", "title": "Unit of Measure Mismatch",
        "vendor": "USSU-VSF02", "terms": "NT30",
        "lines": [("Industrial Solvent IPA", 500, "KG", 3.20, "I1"),
                  ("Acetone Pure Grade", 200, "KG", 4.50, "I1")],
        "freight": 85.00,
        "gr": [{"qty": 500}, {"qty": 200}],
        "invoice": {"uoms": {0: "L"}},
        "expected": "Manual Review", "checks": ["Unit of Measure"],
        "story": "IPA is billed in litres; the SAP PO orders it in KG.",
    },
    {
        "id": "S08", "title": "Payment Terms Mismatch",
        "vendor": "USSU-VSF50", "terms": "NT30",
        "lines": [("Cisco Switch 24-Port", 3, "EA", 4200.00, "I1"),
                  ("HP Server DL380 Gen10", 1, "EA", 18500.00, "I1")],
        "freight": 300.00,
        "gr": [{"qty": 3}, {"qty": 1}],
        "invoice": {"terms": "Net 15"},
        "expected": "Manual Review", "checks": ["Payment Terms"],
        "story": "Invoice demands Net 15; the SAP PO carries NT30 (Net 30).",
    },
    {
        "id": "S09", "title": "Freight Overcharge",
        "vendor": "USSU-VSF51", "terms": "NT30",
        "lines": [("Packaging Material Box A", 1000, "EA", 2.50, "I1"),
                  ("Bubble Wrap Roll 50m", 50, "ROL", 18.00, "I1")],
        "freight": 150.00,
        "gr": [{"qty": 1000}, {"qty": 50}],
        "invoice": {"freight": 650.00},
        "expected": "Manual Review", "checks": ["Freight & Discount"],
        "story": "PO freight condition (FVA1) is $150; the invoice charges $650.",
    },
    {
        "id": "S10", "title": "Damaged Goods — QC Return",
        "vendor": "USSU-VSF52", "terms": "NT30",
        "lines": [("ARM Cortex MCU M4", 500, "EA", 15.00, "I1"),
                  ("Voltage Regulator LM317", 1000, "EA", 1.20, "I1")],
        "freight": 200.00, "discount": 174.00,
        "gr": [{"qty": 500, "return": (50, "0003", "ESD damage — failed QC")},
               {"qty": 1000}],
        "invoice": {},
        "expected": "Manual Review", "checks": ["Quantity"],
        "story": "500 MCUs received, 50 returned to vendor in SAP (mvt 122, reason "
                 "Damaged). The invoice still bills all 500.",
    },
    {
        "id": "S11", "title": "Vendor Mismatch",
        "vendor": "USSU-VSF01", "terms": "NT30",
        "lines": [("A4 Copy Paper Box of 5 Reams", 100, "CS", 28.50, "I1"),
                  ("Whiteboard Marker Set 12 Pack", 50, "PAK", 8.00, "I1"),
                  ("Filing Cabinet 4-Drawer", 5, "EA", 320.00, "I1")],
        "freight": 120.00,
        "gr": [{"qty": 100}, {"qty": 50}, {"qty": 5}],
        "invoice": {"vendor_name": "Delta Office Supplies"},
        "expected": "Manual Review", "checks": ["Vendor / Header"],
        "story": "Invoice is raised by 'Delta Office Supplies' against a PO issued to a "
                 "different SAP supplier.",
    },
    {
        "id": "S12", "title": "PO Closed in SAP",
        "vendor": "USSU-VSF02", "terms": "NT30",
        "lines": [("Ergonomic Keyboard K400", 25, "EA", 45.00, "I1")],
        "freight": 0.0,
        "gr": [{"qty": 25}],
        "close_po": True,
        "invoice": {},
        "expected": "Manual Review", "checks": ["PO Validity"],
        "story": "Buyer has closed the PO in SAP (delivery completed + final invoice "
                 "indicators set) — no further invoices allowed.",
    },
    {
        "id": "S13", "title": "GRN Missing — Goods Not Received",
        "vendor": "USSU-VSF50", "terms": "NT30",
        "lines": [("Projector Epson EB-X51", 4, "EA", 650.00, "I1")],
        "freight": 40.00,
        "gr": [],
        "invoice": {},
        "expected": "Incomplete — Missing Documents", "checks": ["GRN Validation"],
        "story": "PO is open in SAP but no goods receipt has been posted yet.",
    },
    {
        "id": "S14", "title": "Future-Dated Invoice",
        "vendor": "USSU-VSF51", "terms": "NT30",
        "lines": [("Network Cable Cat6 305m", 12, "ROL", 95.00, "I1")],
        "freight": 60.00,
        "gr": [{"qty": 12}],
        "invoice": {"date_offset": 30},
        "expected": "Manual Review", "checks": ["Invoice Date"],
        "story": "Invoice is dated 30 days in the future.",
    },
    {
        "id": "S15", "title": "Arithmetic Error on Invoice",
        "vendor": "USSU-VSF52", "terms": "NT30",
        "lines": [("Safety Helmet Class E", 40, "EA", 32.00, "I1"),
                  ("Hi-Vis Safety Vest", 40, "EA", 14.00, "I1")],
        "freight": 50.00,
        "gr": [{"qty": 40}, {"qty": 40}],
        "invoice": {"line_amounts": {0: 1380.00}, "total_delta": 100.00},
        "expected": "Manual Review", "checks": ["Line Amounts", "Invoice Total"],
        "story": "Line 1 prints $1,380 for 40 × $32 (= $1,280) and the grand total is "
                 "overstated by $100.",
    },
    {
        "id": "S16", "title": "Goods Received After Invoice Date",
        "vendor": "USSU-VSF01", "terms": "NT30",
        "lines": [("Conference Table 8-Seater", 2, "EA", 1150.00, "I1")],
        "freight": 90.00,
        "gr": [{"qty": 2}],
        "po_days_ago": 20,
        "invoice": {"date_offset": -10},
        "expected": "Manual Review", "checks": ["GRN Validation"],
        "story": "Invoice dated 10 days ago, but SAP shows the goods receipt was only "
                 "posted today — billed before delivery.",
    },
]


def by_id(sid: str) -> dict:
    return next(s for s in SCENARIOS if s["id"] == sid.upper())
