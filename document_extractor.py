"""
document_extractor.py — LLM Document Field Extractor  v3.0
==============================================================
Uses Mistral to extract structured fields from PDF/TXT content.

v3.0 change
-----------
Only the Invoice is extracted via LLM now. Purchase Order and Goods
Receipt Note data come from the mock ERP workbook (see erp_mock.py),
looked up by the PO Number found on the invoice. extract_po() and
extract_grn() are kept in this module (unused in the main pipeline)
only so any legacy caller / test script that imports them still works.

Returns clean Python dicts — no N/A, no null values.
"""

import os
import json
import re
from pathlib import Path
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mistralai import ChatMistralAI
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")


def _get_llm():
    api_key = os.getenv("MISTRAL_API_KEY", "")
    if not api_key:
        raise EnvironmentError("MISTRAL_API_KEY not set in .env")
    return ChatMistralAI(model="mistral-small-latest", temperature=0.0, api_key=api_key)


def _call_llm(system_prompt: str, text: str) -> dict:
    """Call Mistral and parse JSON response."""
    llm = _get_llm()
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"Document text:\n\n{text[:4000]}"),
    ]
    response = llm.invoke(messages)
    raw      = response.content.strip()
    clean    = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        print(f"    ⚠️  JSON parse error. Raw: {raw[:200]}")
        return {}


INVOICE_PROMPT = """Extract Invoice fields from the document text.
Return ONLY valid JSON — no markdown, no explanation.

Required JSON schema:
{
  "invoice_number": "string",
  "vendor_name":    "string",
  "invoice_date":   "YYYY-MM-DD",
  "po_number":      "string",
  "currency":       "USD",
  "payment_terms":  "string",
  "tax_type":       "string",
  "subtotal":       0.00,
  "tax":            0.00,
  "freight":        0.00,
  "discount":       0.00,
  "total_amount":   0.00,
  "items": [
    {"item": "string", "quantity": 0, "unit_price": 0.00, "uom": "string"}
  ]
}

Rules:
- Convert all dates to YYYY-MM-DD format
- subtotal, tax, freight, discount, total_amount must all be numbers (not strings)
- tax_type is the label as printed, e.g. "IGST @ 18.0%", "GST @ 10.0%", "VAT @ 12.0%"
  — capture it verbatim, do not normalise or reformat it
- payment_terms is the label as printed, e.g. "Net 30", "Net 45", "Due on Receipt"
- uom is the unit of measure per line item as printed, e.g. "EA", "KG", "LTR", "BOX"
  — if not shown for a line, use "EA"
- If a numeric field is not found use 0; if a text field is not found use ""
- items array must have at least one entry"""

PO_PROMPT = """Extract Purchase Order fields from the document text.
Return ONLY valid JSON — no markdown, no explanation.

Required JSON schema:
{
  "po_number":   "string",
  "vendor_name": "string",
  "po_date":     "YYYY-MM-DD",
  "currency":    "USD",
  "po_amount":   0.00,
  "items": [
    {"item": "string", "quantity": 0, "unit_price": 0.00}
  ]
}"""

GRN_PROMPT = """Extract Goods Receipt Note fields from the document text.
Return ONLY valid JSON — no markdown, no explanation.

Required JSON schema:
{
  "grn_number":    "string",
  "po_number":     "string",
  "vendor_name":   "string",
  "received_date": "YYYY-MM-DD",
  "received_by":   "string",
  "items": [
    {
      "item":               "string",
      "quantity_ordered":   0,
      "quantity_received":  0,
      "quantity_accepted":  0,
      "condition":          "Good"
    }
  ]
}"""


def extract_invoice(text: str) -> dict:
    print("  🔍 Extracting Invoice fields...")
    data = _call_llm(INVOICE_PROMPT, text)
    print(f"    Invoice: {data.get('invoice_number','')} | "
          f"{data.get('vendor_name','')} | ${data.get('total_amount',0)}")
    print(f"      Subtotal=${data.get('subtotal',0)} Tax=${data.get('tax',0)} "
          f"({data.get('tax_type','—')}) Freight=${data.get('freight',0)} "
          f"Discount=${data.get('discount',0)} Terms={data.get('payment_terms','—')}")
    return data


def extract_po(text: str) -> dict:
    print("  🔍 Extracting PO fields...")
    data = _call_llm(PO_PROMPT, text)
    print(f"    PO: {data.get('po_number','')} | "
          f"{data.get('vendor_name','')} | ${data.get('po_amount',0)}")
    return data


def extract_grn(text: str) -> dict:
    print("  🔍 Extracting GRN fields...")
    data = _call_llm(GRN_PROMPT, text)
    print(f"    GRN: {data.get('grn_number','')} | "
          f"PO: {data.get('po_number','')} | "
          f"Received: {data.get('received_date','')}")
    return data


def extract_all_documents(attachments: list) -> dict:
    """
    Extract fields from the Invoice attachment only.

    PO and GRN are NOT extracted here anymore — they are looked up from
    the mock ERP workbook (erp_mock.lookup_po_and_grn) using the PO
    Number found on the invoice. This function's job is now just:
    find the Invoice attachment, run it through the LLM extractor, and
    pass through any "Supporting" attachment as raw reference material
    (file name + first ~500 chars of text) for the audit trail/summary
    — it is never matched against.

    Args:
        attachments: list of dicts with doc_type and extracted_text
                     (doc_type is "Invoice" or "Supporting")

    Returns:
        {
          "invoice_data": {...},
          "supporting_docs": [{"file_name": str, "preview": str}, ...]
        }
    """
    result = {"invoice_data": {}, "supporting_docs": []}

    print("\n" + "="*55)
    print("  DOCUMENT EXTRACTION (LLM)")
    print("="*55)

    invoice_found = False

    for att in attachments:
        doc_type = att.get("doc_type", att.get("document_type", "Unknown"))
        text     = att.get("extracted_text", "")
        fname    = att.get("file_name", "")

        if not text:
            print(f"  ⚠️  No text in {fname} — skipping")
            continue

        print(f"\n  Processing [{doc_type}] {fname} ({len(text)} chars)")

        if doc_type == "Invoice" and not invoice_found:
            result["invoice_data"] = extract_invoice(text)
            invoice_found = True
        elif doc_type == "Invoice" and invoice_found:
            # More than one invoice-looking attachment — keep the first,
            # log the rest as supporting so nothing is silently dropped.
            print(f"  ⚠️  Multiple Invoice-like attachments — "
                  f"'{fname}' kept as Supporting reference only")
            result["supporting_docs"].append(
                {"file_name": fname, "preview": text[:500]}
            )
        else:
            # Supporting document — store a preview only, never parsed
            print(f"  📎 Supporting document — stored for reference, not matched")
            result["supporting_docs"].append(
                {"file_name": fname, "preview": text[:500]}
            )

    if not invoice_found:
        print("  ❌ No Invoice attachment found among the supplied documents")

    return result

