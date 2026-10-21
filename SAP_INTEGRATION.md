# SAP integration — live ERP for the AP 3-way match

The AP engine reads purchase orders, goods receipts and supplier invoices **live from SAP over RFC**
(no copy in Redis), validates the vendor invoice against them, and writes the outcome **back into SAP**
as a real supplier invoice (MIRO document).

| Agent decision | What happens in SAP |
|---|---|
| Perfect Match | Supplier invoice **posted** against the PO / GR (`BAPI_INCOMINGINVOICE_CREATE1`). |
| Manual Review | Invoice **parked** (visible in MIR4 / MIR6) with header text `AP Agent review`. |
| Reviewer approves | The parked invoice is **posted**. SAP sets a payment block when a variance is outside its tolerance. |
| Reviewer rejects | The parked invoice is **deleted**. |
| Duplicate | Nothing is created in SAP. |
| Demo reset | A posted invoice is **reversed** (`BAPI_INCOMINGINVOICE_CANCEL`) and a parked one is **deleted**. |

Every create is first run as an SAP **simulation**: SAP runs all its checks and writes nothing. The real
posting is made only if the simulation is clean. If SAP refuses a Perfect Match, the invoice goes to
Manual Review with SAP's own message.

## Files

| File | Role |
|---|---|
| `sap_connector.py` | RFC connection profiles (S4 / ECC), `RFC_READ_TABLE` helpers, BAPI and commit helpers, open-MM-period posting date, health ping. |
| `erp_sap.py` | Live PO / GRN / supplier-invoice reads, shaped exactly like the old mock ERP. Also the bulk read behind the ERP page. |
| `sap_invoice.py` | Post, park, post-parked, delete and reverse supplier invoices. Every create is simulated first. |
| `erp_mock.py` | Routes lookups to SAP when `AP_ERP_SOURCE=sap`, and to the Redis mock otherwise. |
| `matcher.py` | 3-way match plus the SAP-driven validations listed below. |
| `demo_scenarios.py` / `sap_demo_setup.py` | The 16 demo cases, and the script that creates them in SAP and renders their invoice PDFs. |
| `smoke_test_sap.py` | End-to-end test of every case against live SAP, including reviewer actions and reset. |

## Where each check gets its SAP data

| # | Check | SAP source |
|---|---|---|
| 1 | PO Validity | EKKO deletion flag, release status (FRGRL / PROCSTAT), EKPO delivery-completed / final-invoice flags |
| 2 | GRN Validation | EKBE goods receipts plus the GR document date (`BAPI_GOODSMVT_GETDETAIL`); GR must be dated on or before the invoice |
| 3 | Invoice Date | Not future-dated, not older than 90 days, not before the PO date (EKKO-BEDAT) |
| 4 | Vendor / PO# / Currency | EKKO-LIFNR and LFA1 name, EKKO-WAERS |
| 5 | Unit Price | Gross price condition PPR0 / PBXX (PRCD_ELEMENTS) |
| 6 | UOM | EKPO-MEINS, shown as its commercial unit from T006A |
| 7 | Quantity | PO quantity vs GR accepted quantity (101 minus 122 returns) minus quantity already invoiced (EKBE) |
| 8 | Tax | Tax code plus jurisdiction, with the rate from condition records (A053 / KONP) |
| 9 | Freight & Discount | Freight condition FVA1 and discount DRV1 on the PO |
| 10 | Payment Terms | EKKO-ZTERM, labelled from T052 (e.g. NT30 shows as "Net 30") |
| 11 | Line Amounts | qty × price = line amount |
| 12 | Invoice Total | subtotal − discount + tax + freight = total |
| 13 | Duplicate | AP processed log **and** SAP's own invoice table RBKP (same reference for the vendor, posted or parked) |

## Running locally or on the RPA server (Windows)

1. Extract the SAP NW RFC SDK 7.50 (`Docs/nwrfc750P_17-70002755.zip`), for example to
   `C:\sap\nwrfcsdk`.
2. Set `SAPNWRFC_HOME=C:\sap\nwrfcsdk` and add `C:\sap\nwrfcsdk\lib` to `PATH`.
3. Install the VC++ 2013 x64 runtime (see the SDK guide in `Docs/`).
4. Set up Python and the dependencies, from the project folder:
   ```powershell
   uv venv --python 3.12 .venv
   uv pip install --python .venv\Scripts\python.exe -r requirements-local.txt
   ```
5. Copy `.env.example` to `.env`, then fill in `SAP_S4_USER` / `SAP_S4_PASSWD`, `AP_TRIGGER_SECRET`,
   `MISTRAL_API_KEY` (PDF extraction), Upstash Redis and the mail settings.
6. Start the app:
   ```powershell
   uv run --no-project uvicorn api.index:app --host 0.0.0.0 --port 8000
   ```
7. Open http://localhost:8000.

The RFC gateway is port **33‹sysnr›**, which is **3357** for A4F. It must be reachable from the host.

## Demo cases

```powershell
.venv\Scripts\python.exe sap_demo_setup.py          # create POs/GRs in SAP + invoice PDFs (idempotent)
.venv\Scripts\python.exe sap_demo_setup.py --list   # live status of every case
.venv\Scripts\python.exe sap_demo_setup.py --pdfs   # re-render the PDFs only (invoice dates move to today)
.venv\Scripts\python.exe smoke_test_sap.py          # run all cases end-to-end, then reset
```

In the dashboard, open **Overview → 🧪 SAP Demo Cases**. Each case has:

- **Run**: processes the invoice fields through the full pipeline (no LLM needed);
- the invoice PDF, to upload under *Upload Invoice Manually* or to email to the mailbox;
- **Reset**: reverses or deletes the agent's SAP document and clears the logs, so the case can be demoed again.

**Reset all demo cases** does every case at once.

| Case | PO (A4F) | Scenario | Expected |
|---|---|---|---|
| S01 | 4500002217 | Perfect match | Posted in SAP |
| S02 | 4500002218 | Price +1.7% (within the 2% tolerance) | Posted in SAP |
| S03 | 4500002219 | Price +8.7% | Parked → Manual Review |
| S04 | 4500002220 | Partial GR (450/500, 180/200) | Parked → Manual Review |
| S05 | 4500002221 | Submit twice | 1st posted; 2nd flagged as duplicate (AP log + SAP RBKP) |
| S06 | 4500002222 | Tax 8.25% vs SAP 6% | Parked → Manual Review |
| S07 | 4500002223 | UOM L vs KG | Parked → Manual Review |
| S08 | 4500002224 | Net 15 vs NT30 | Parked → Manual Review |
| S09 | 4500002225 | Freight $650 vs FVA1 $150 | Parked → Manual Review |
| S10 | 4500002226 | 50 of 500 returned (mvt 122, reason "Damaged") | Parked → Manual Review |
| S11 | 4500002227 | Invoice from a different vendor | Parked → Manual Review |
| S12 | 4500002228 | PO closed in SAP | Parked → Manual Review (PO Validity) |
| S13 | 4500002229 | No GR posted | Incomplete — listed under Exceptions |
| S14 | 4500002230 | Invoice dated +30 days | Parked → Manual Review (Invoice Date) |
| S15 | 4500002231 | Line amount and total arithmetic wrong | Parked → Manual Review |
| S16 | 4500002232 | GR dated after the invoice | Parked → Manual Review (GRN Validation) |

## A4F specifics found while building this

- **The PO type is `ENB`, not `NB`.** `NB` has flexible workflow switched on, and its POs stay "In Release"
  with no approver, so goods receipts fail with ME/390.
- **MM posting periods are still 2026/04.** Goods movements and invoices are posted into the open period,
  and the real date goes into the document date. Running MMPV would advance periods for every user of
  the client, so it is left to Basis.
- **One fresh RFC session per write.** A second goods-movement or invoice BAPI in the same session dumps
  with `ITAB_DUPLICATE_KEY`.
- **Planned freight lines.** These are identified by condition type *or* by step plus counter, never
  both (M8/332).
- **Use the IP.** The gateway rejects the old hostname `as4hsap01`; the server is now `as4fsap01`.
- **The ECC profile is wired but not verified.** The `RPA` user is rejected on AVI client 800, so ECC
  needs its own credentials.
