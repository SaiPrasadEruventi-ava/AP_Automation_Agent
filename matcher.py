"""
matcher.py — 3-Way Matching Engine
====================================
Pure Python matching logic.
Compares Invoice vs PO vs GRN.
Returns detailed match results with variances.
"""


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def _build_index(items: list, key: str = "item") -> dict:
    return {_norm(i.get(key, "")): i for i in items}


def run_three_way_match(invoice_data: dict,
                        po_data: dict,
                        grn_data: dict,
                        variance_threshold: float = 2.0) -> dict:
    """
    Run complete 3-way match: Invoice vs PO vs GRN.

    Args:
        variance_threshold: % tolerance for per-line unit price comparison.
            A line's price is considered a "Match" if it falls within this
            tolerance of the PO price — NOT an exact-cent match. This
            keeps line-level matching consistent with the header-level
            variance tolerance applied in apply_business_rules(), so a
            price difference that's within tolerance doesn't wrongly
            fail the line (and therefore the whole invoice).

    Returns:
        {
            header_match: bool,
            line_match: bool,
            grn_match: bool,
            variance: float,
            variance_percentage: float,
            po_total: float,
            invoice_total: float,
            item_results: list,
            mismatches: list,
        }
    """
    print("\n" + "="*55)
    print("  3-WAY MATCHING ENGINE")
    print("="*55)

    mismatches   = []
    item_results = []

    # ── Header matching ───────────────────────────────────────────────
    inv_vendor = _norm(invoice_data.get("vendor_name", ""))
    po_vendor  = _norm(po_data.get("vendor_name",      ""))
    inv_po_num = invoice_data.get("po_number", "").strip().upper()
    po_num     = po_data.get("po_number",      "").strip().upper()
    inv_cur    = invoice_data.get("currency",  "USD").upper()
    po_cur     = po_data.get("currency",       "USD").upper()

    header_match = True
    if inv_vendor and po_vendor and inv_vendor != po_vendor:
        mismatches.append(
            f"Vendor mismatch: Invoice='{invoice_data.get('vendor_name')}' "
            f"PO='{po_data.get('vendor_name')}'"
        )
        header_match = False
    if inv_po_num and po_num and inv_po_num != po_num:
        mismatches.append(
            f"PO# mismatch: Invoice='{inv_po_num}' PO='{po_num}'"
        )
        header_match = False
    if inv_cur != po_cur:
        mismatches.append(
            f"Currency mismatch: Invoice='{inv_cur}' PO='{po_cur}'"
        )
        header_match = False

    print(f"  Header match  : {'✅ PASS' if header_match else '❌ FAIL'}")
    if not header_match:
        for m in mismatches:
            print(f"    • {m}")

    # ── Line item matching ────────────────────────────────────────────
    inv_items = invoice_data.get("items", [])
    po_items  = po_data.get("items",      [])
    grn_items = grn_data.get("items",     [])

    po_idx  = _build_index(po_items)
    grn_idx = _build_index(grn_items)

    line_match = True

    for inv_item in inv_items:
        name      = inv_item.get("item", "")
        inv_qty   = int(inv_item.get("quantity",   0))
        inv_price = float(inv_item.get("unit_price", 0))
        key       = _norm(name)

        po_item  = po_idx.get(key,  {})
        grn_item = grn_idx.get(key, {})

        po_qty    = int(po_item.get("quantity",          0)) if po_item  else None
        po_price  = float(po_item.get("unit_price",      0)) if po_item  else None
        grn_recv  = int(grn_item.get("quantity_received",0)) if grn_item else None
        grn_acc   = int(grn_item.get("quantity_accepted",0)) if grn_item else None
        condition = grn_item.get("condition","Good")          if grn_item else "Not received"

        # Compare
        # Price tolerance: within variance_threshold% of PO price counts
        # as a match (mirrors the header-level tolerance) — NOT an exact
        # match, so small in-tolerance price differences (e.g. S02) don't
        # wrongly fail the line.
        if po_price is not None and po_price > 0:
            price_diff_pct = abs(inv_price - po_price) / po_price * 100
            price_ok = price_diff_pct <= variance_threshold
        else:
            price_ok = abs(inv_price - (po_price or 0)) < 0.01

        inv_vs_po  = "Match" if po_qty  is not None and inv_qty == po_qty  and price_ok else "Mismatch"
        inv_vs_grn = "Match" if grn_acc is not None and inv_qty == grn_acc else "Mismatch"
        po_vs_grn  = "Match" if grn_recv is not None and po_qty is not None and po_qty == grn_recv else "Mismatch"

        all_match = inv_vs_po == "Match" and inv_vs_grn == "Match" and po_vs_grn == "Match"
        if not all_match:
            line_match = False
            mismatches.append(
                f"Line '{name}': inv_vs_po={inv_vs_po} "
                f"inv_vs_grn={inv_vs_grn} po_vs_grn={po_vs_grn}"
            )

        icon = "✅" if all_match else "❌"
        print(f"  {icon} '{name}': "
              f"inv_vs_po={inv_vs_po} "
              f"inv_vs_grn={inv_vs_grn} "
              f"po_vs_grn={po_vs_grn}")

        item_results.append({
            "item":          name,
            "invoice_qty":   inv_qty,
            "po_qty":        po_qty,
            "grn_received":  grn_recv,
            "grn_accepted":  grn_acc,
            "invoice_price": inv_price,
            "po_price":      po_price,
            "condition":     condition,
            "invoice_vs_po": inv_vs_po,
            "invoice_vs_grn":inv_vs_grn,
            "po_vs_grn":     po_vs_grn,
            "status":        "Matched" if all_match else "Mismatch",
        })

    print(f"  Line match    : {'✅ PASS' if line_match else '❌ FAIL'}")
    grn_match = line_match

    # ── Amount reconciliation ─────────────────────────────────────────
    inv_total = float(invoice_data.get("total_amount", 0))
    po_total  = float(po_data.get("po_amount",         0))
    variance  = round(inv_total - po_total, 2)
    var_pct   = round((variance / po_total * 100), 4) if po_total else 0.0

    print(f"  PO Total      : ${po_total:>12,.2f}")
    print(f"  Invoice Total : ${inv_total:>12,.2f}")
    print(f"  Variance      : ${variance:>12,.2f} ({var_pct:.2f}%)")

    return {
        "header_match":        header_match,
        "line_match":          line_match,
        "grn_match":           grn_match,
        "variance":            variance,
        "variance_percentage": var_pct,
        "po_total":            po_total,
        "invoice_total":       inv_total,
        "item_results":        item_results,
        "mismatches":          mismatches,
    }


def apply_business_rules(matching: dict,
                          variance_threshold: float = 2.0,
                          state: dict = None) -> dict:
    """
    Apply AP business rules to determine final decision.

    Returns:
        {
            "decision": "Perfect Match" | "Manual Review",
            "review_required": bool,
            "reasons": list
        }
    """
    print("\n" + "="*55)
    print("  AP BUSINESS RULES")
    print("="*55)

    reasons      = []
    var_pct      = abs(matching.get("variance_percentage", 0))
    variance     = matching.get("variance", 0)
    hm           = matching.get("header_match", False)
    lm           = matching.get("line_match",   False)
    gm           = matching.get("grn_match",    False)
    mismatches   = matching.get("mismatches",   [])
    item_results = matching.get("item_results", [])
    invoice_data = (state or {}).get("invoice_data", {})
    po_data      = (state or {}).get("po_data", {})

    # ── Duplicate invoice — unconditional, takes priority over all other ──
    # ── checks. A duplicate can otherwise look like a "Perfect Match"     ──
    # ── against its own PO/GRN, so this must run regardless of hm/lm/gm.  ──
    dup_check = (state or {}).get("duplicate_check", {})
    if dup_check.get("is_duplicate"):
        dup_type = dup_check.get("duplicate_type", "Duplicate")
        dup_reason = dup_check.get("duplicate_reason", "")
        reasons.append(f"Duplicate Invoice — {dup_type}: {dup_reason}")
        print("  🟡 Decision: MANUAL REVIEW")
        print(f"    • Duplicate Invoice — {dup_type}: {dup_reason}")
        return {
            "decision":        "Manual Review",
            "review_required": True,
            "reasons":         reasons,
        }

    # ── Header mismatch — derive specific header field that failed ────
    if not hm:
        mismatch_text = " ".join(mismatches).lower()
        if "vendor" in mismatch_text:
            # Extract vendor names from mismatch details
            vendor_detail = next(
                (m for m in mismatches if "vendor" in m.lower()), ""
            )
            reasons.append(f"Vendor Mismatch — {vendor_detail}" if vendor_detail
                           else "Vendor Mismatch — Invoice vendor does not match PO")
        elif "po#" in mismatch_text or "po number" in mismatch_text:
            reasons.append("PO Number Mismatch — Invoice PO# does not match Purchase Order")
        elif "currency" in mismatch_text:
            reasons.append("Currency Mismatch — Invoice currency differs from PO currency")
        else:
            reasons.append("Header Mismatch — Vendor/PO#/Currency discrepancy found")

    # ── Line item / GRN mismatch — classify as price, qty, or short delivery ──
    if not lm or not gm:
        _classify_line_mismatches(reasons, item_results, mismatches, state or {},
                                   variance_threshold)

    # ── UOM mismatch — independent check, line/grn can both "Match" on   ──
    # ── qty+price while UOM still differs (matcher.py doesn't check UOM) ──
    _check_uom_mismatch(reasons, invoice_data, po_data)

    # ── Payment terms mismatch — independent of line/header match ──────
    _check_payment_terms_mismatch(reasons, invoice_data, po_data)

    # ── Tax mismatch — independent of line/header match ─────────────────
    _check_tax_mismatch(reasons, invoice_data, po_data)

    # ── Freight overcharge — independent of line/header match ───────────
    _check_freight_overcharge(reasons, invoice_data, po_data, variance_threshold)

    # ── Amount variance — decomposed by actual cause, not a blanket label ──
    # The old version labelled ANY total-vs-PO variance as "Price Variance
    # — Exceeds Tolerance" even when the real cause was tax, freight, or
    # discount (which already have their own specific checks above). This
    # walks the same total-dollar variance but attributes it to whichever
    # component(s) actually drove it, and skips attribution already
    # covered by a more specific reason already in the list.
    _check_amount_variance_decomposed(
        reasons, invoice_data, po_data, matching, variance_threshold
    )

    if not reasons:
        decision = "Perfect Match"
        print("  🟢 Decision: PERFECT MATCH")
    else:
        decision = "Manual Review"
        print("  🟡 Decision: MANUAL REVIEW")
        for r in reasons:
            print(f"    • {r}")

    return {
        "decision":        decision,
        "review_required": decision == "Manual Review",
        "reasons":         reasons,
    }


def _norm_terms(terms: str) -> str:
    """Normalise payment terms strings for comparison (Net 30 == net30 == 30 days)."""
    t = str(terms or "").lower().strip().replace("-", "").replace("_", "").replace(" ", "")
    equivalents = {
        "net30": ["net30", "30days", "n30"],
        "net45": ["net45", "45days", "n45"],
        "net60": ["net60", "60days", "n60"],
        "net90": ["net90", "90days", "n90"],
        "immediate": ["immediate", "cash", "cod", "cashondelivery", "dueonreceipt"],
        "advance": ["advance", "advancepayment", "prepaid"],
    }
    for canonical, variants in equivalents.items():
        if t in variants or t == canonical:
            return canonical
    return t


def _check_uom_mismatch(reasons: list, invoice_data: dict, po_data: dict) -> None:
    """
    Compare unit of measure per line item between Invoice and PO.
    matcher.py's run_three_way_match() does not check UOM (only qty and
    price), so this independent check exists specifically to catch a
    case like 'Industrial Solvent IPA: Invoice UOM=LTR vs PO UOM=KG'
    where qty and price might otherwise look fine.
    """
    inv_items = invoice_data.get("items", [])
    po_items  = po_data.get("items", [])
    po_idx    = {_norm(i.get("item", "")): i for i in po_items}

    uom_issues = []
    for inv_item in inv_items:
        name    = inv_item.get("item", "")
        key     = _norm(name)
        po_item = po_idx.get(key, {})
        if not po_item:
            continue
        inv_uom = str(inv_item.get("uom", inv_item.get("unit", ""))).upper().strip()
        po_uom  = str(po_item.get("uom", po_item.get("unit", ""))).upper().strip()
        if inv_uom and po_uom and inv_uom != po_uom:
            uom_issues.append(f"'{name}': Invoice UOM '{inv_uom}' vs PO UOM '{po_uom}'")

    if uom_issues:
        for u in uom_issues:
            reasons.append(f"UOM Mismatch — Unit of Measure Discrepancy: {u}")


def _check_payment_terms_mismatch(reasons: list, invoice_data: dict, po_data: dict) -> None:
    """Compare payment terms between Invoice and PO (Net 30 vs Net 45, etc.)."""
    inv_terms_raw = invoice_data.get("payment_terms", "")
    po_terms_raw  = po_data.get("payment_terms", "")

    if not inv_terms_raw or not po_terms_raw:
        return  # nothing to compare

    if _norm_terms(inv_terms_raw) != _norm_terms(po_terms_raw):
        reasons.append(
            f"Payment Terms Mismatch — Invoice: {inv_terms_raw} vs PO: {po_terms_raw}"
        )


def _check_tax_mismatch(reasons: list, invoice_data: dict, po_data: dict) -> None:
    """
    Compare tax type/rate between Invoice and PO
    (e.g. 'IGST @ 28.0%' vs PO's 'IGST @ 18.0%').
    """
    inv_tax = str(invoice_data.get("tax_type", "")).strip()
    po_tax  = str(po_data.get("tax_type", "")).strip()

    if not inv_tax or not po_tax:
        return  # nothing to compare

    if inv_tax.lower() != po_tax.lower():
        reasons.append(
            f"Tax Mismatch — Wrong GST Rate: Invoice {inv_tax} vs PO expects {po_tax}"
        )


def _check_freight_overcharge(reasons: list, invoice_data: dict, po_data: dict,
                               variance_threshold: float) -> None:
    """
    Compare freight charges between Invoice and PO. Flags an overcharge
    when the invoice's freight exceeds the PO's freight by more than the
    configured variance tolerance.
    """
    inv_freight = invoice_data.get("freight")
    po_freight  = po_data.get("freight")

    if inv_freight is None or po_freight is None:
        return  # nothing to compare

    inv_freight = float(inv_freight)
    po_freight  = float(po_freight)

    if po_freight > 0:
        pct = (inv_freight - po_freight) / po_freight * 100
    elif inv_freight > 0:
        pct = 100.0  # PO had no freight at all but invoice charges some
    else:
        pct = 0.0

    if pct > variance_threshold:
        reasons.append(
            f"Freight Overcharge — Invoice: ${inv_freight:,.2f} vs PO: ${po_freight:,.2f} "
            f"({pct:.1f}% over)"
        )


def _check_amount_variance_decomposed(reasons: list, invoice_data: dict,
                                       po_data: dict, matching: dict,
                                       variance_threshold: float) -> None:
    """
    The total invoice-vs-PO dollar variance (matching['variance']) can be
    caused by unit price differences, tax rate differences, freight
    differences, or discount differences — often in combination. Rather
    than emitting one blanket "Price Variance — Exceeds Tolerance" label
    regardless of cause (the old behaviour), this decomposes the total
    variance into its components and reports ONLY the ones that actually
    contributed beyond tolerance, each with its own dollar/percent
    attribution — and ONLY if not already explained by a more specific
    check that ran earlier in apply_business_rules.

    Tax is compared by RATE, not raw dollar amount: if the invoice and PO
    apply the same tax rate, a higher invoice subtotal naturally produces
    a higher tax dollar amount too — that's a consequence of the price
    variance already reported, not an independent tax problem. Only a
    genuine rate difference (e.g. 28% vs 18%) is tax's own contribution.
    """
    inv_total = matching.get("invoice_total", 0) or 0
    po_total  = matching.get("po_total", 0) or 0
    var_pct   = abs(matching.get("variance_percentage", 0) or 0)

    if var_pct <= variance_threshold or po_total == 0:
        return  # within tolerance overall — nothing to attribute

    already_has = lambda *keywords: any(
        all(k.lower() in r.lower() for k in kw) if isinstance(kw, tuple) else kw.lower() in r.lower()
        for r in reasons for kw in keywords
    )

    # ── Compute invoice-side subtotal (explicit field, else derived from items) ──
    inv_items    = invoice_data.get("items", [])
    derived_sub  = sum(
        float(i.get("quantity", 0)) * float(i.get("unit_price", 0))
        for i in inv_items
    )
    inv_subtotal = float(invoice_data.get("subtotal") or derived_sub or 0)
    po_subtotal  = float(po_data.get("subtotal", 0) or 0)

    inv_tax      = invoice_data.get("tax")
    po_tax       = float(po_data.get("tax", 0) or 0)
    inv_freight  = invoice_data.get("freight")
    po_freight   = float(po_data.get("freight", 0) or 0)
    inv_discount = invoice_data.get("discount")
    po_discount  = float(po_data.get("discount", 0) or 0)

    # accounted_for tracks every component that's been explained — either
    # by a NEW reason this function is about to add, or by a check that
    # already ran earlier. Only if NOTHING ends up accounted for do we
    # fall back to a generic total-mismatch line.
    accounted_for = []

    # ── Subtotal / unit-price-driven variance ──────────────────────────
    sub_delta = 0.0
    if po_subtotal > 0:
        sub_delta     = inv_subtotal - po_subtotal
        sub_delta_pct = abs(sub_delta) / po_subtotal * 100
        if sub_delta_pct > variance_threshold:
            if already_has("Unit Price Mismatch", "Vendor Mismatch", "Short Delivery",
                            "Damaged Goods", "Quantity Discrepancy"):
                accounted_for.append("subtotal")  # explained elsewhere, no new line needed
            else:
                reasons.append(
                    f"Price Variance — Line Item Subtotal: Invoice ${inv_subtotal:,.2f} "
                    f"vs PO ${po_subtotal:,.2f} "
                    f"(${abs(sub_delta):,.2f} {'higher' if sub_delta > 0 else 'lower'} than PO, "
                    f"{sub_delta_pct:.1f}% variance)"
                )
                accounted_for.append("subtotal")

    # ── Tax-driven variance — compared by RATE, not raw dollar amount ──
    if inv_tax is not None and po_subtotal > 0 and inv_subtotal > 0:
        inv_tax_f    = float(inv_tax or 0)
        inv_tax_rate = (inv_tax_f / inv_subtotal * 100) if inv_subtotal else 0
        po_tax_rate  = (po_tax / po_subtotal * 100) if po_subtotal else 0
        rate_delta   = abs(inv_tax_rate - po_tax_rate)

        if rate_delta > 0.5:  # genuine rate difference (not rounding noise)
            if already_has("Tax Mismatch"):
                accounted_for.append("tax")  # already reported with full detail
            else:
                tax_delta = inv_tax_f - (inv_subtotal * po_tax_rate / 100)
                reasons.append(
                    f"Price Variance — Tax Rate: Invoice applied {inv_tax_rate:.1f}% "
                    f"vs PO's {po_tax_rate:.1f}% (${abs(tax_delta):,.2f} "
                    f"{'higher' if tax_delta > 0 else 'lower'} than PO rate would yield)"
                )
                accounted_for.append("tax")
        elif abs(inv_tax_f - po_tax) > 0.01:
            # Same rate, different dollar amount — purely a downstream
            # consequence of the subtotal variance already explained
            # above. Not a separate cause; just note it's accounted for.
            accounted_for.append("tax-via-subtotal")

    # ── Freight-driven variance ─────────────────────────────────────────
    if inv_freight is not None:
        inv_freight_f = float(inv_freight or 0)
        freight_delta = inv_freight_f - po_freight
        if abs(freight_delta) > 0.01:
            if already_has("Freight Overcharge"):
                accounted_for.append("freight")  # already reported with full detail
            else:
                pct = (abs(freight_delta) / po_freight * 100) if po_freight > 0 else 100.0
                if pct > variance_threshold:
                    reasons.append(
                        f"Price Variance — Freight: Invoice ${inv_freight_f:,.2f} vs PO "
                        f"${po_freight:,.2f} (${abs(freight_delta):,.2f} "
                        f"{'higher' if freight_delta > 0 else 'lower'} than PO)"
                    )
                    accounted_for.append("freight")

    # ── Discount-driven variance ────────────────────────────────────────
    if inv_discount is not None:
        inv_discount_f = float(inv_discount or 0)
        discount_delta = po_discount - inv_discount_f  # positive = invoice under-discounted
        if abs(discount_delta) > 0.01:
            reasons.append(
                f"Price Variance — Discount: Invoice applied ${inv_discount_f:,.2f} "
                f"discount vs PO terms of ${po_discount:,.2f} "
                f"(${abs(discount_delta):,.2f} {'less' if discount_delta > 0 else 'more'} "
                f"discount applied than expected)"
            )
            accounted_for.append("discount")

    if not accounted_for:
        # Variance exists and exceeds tolerance, but none of subtotal/tax/
        # freight/discount individually exceed it and nothing else
        # explains it either (e.g. rounding accumulation, or fields the
        # invoice simply didn't report). Keep a clearly-scoped fallback
        # rather than silently dropping a real, unexplained variance.
        reasons.append(
            f"Price Variance — Total Amount Mismatch: Invoice ${inv_total:,.2f} "
            f"vs PO ${po_total:,.2f} ({var_pct:.2f}% variance, cause not "
            f"individually attributable from extracted fields)"
        )
    # else: every contributing component is now accounted for — either by
    # a new reason just added, or by a more specific reason that already
    # existed before this function ran (e.g. tax mismatch, unit price
    # mismatch, freight overcharge) — no generic catch-all needed.


def _classify_line_mismatches(reasons: list, item_results: list,
                               mismatches: list, state: dict,
                               variance_threshold: float = 2.0) -> None:
    """
    Inspect item-level match results and append specific, scenario-driven
    rejection reasons (price variance, short delivery, UOM mismatch,
    damaged goods, duplicate, etc.).

    Price differences within variance_threshold% are NOT reported as a
    mismatch here — they're in-tolerance and already reflected (if the
    aggregate invoice-vs-PO variance exceeds threshold) by the separate
    "Price Variance — Exceeds Tolerance" reason in apply_business_rules.
    This keeps line-level and header-level tolerance consistent.
    """
    grn_data     = state.get("grn_data",     {})
    invoice_data = state.get("invoice_data", {})

    price_mismatches = []
    qty_mismatches   = []
    uom_mismatches   = []
    damaged_items    = []

    for item in item_results:
        name        = item.get("item", "?")
        inv_qty     = item.get("invoice_qty")
        grn_acc     = item.get("grn_accepted")
        inv_price   = item.get("invoice_price")
        po_price    = item.get("po_price")
        condition   = item.get("condition", "Good")
        inv_vs_po   = item.get("invoice_vs_po",  "Match")
        inv_vs_grn  = item.get("invoice_vs_grn", "Match")

        # Price mismatch — only report if the per-line variance EXCEEDS
        # the configured tolerance (consistent with run_three_way_match's
        # line-level matching and the header-level threshold check).
        if (inv_vs_po == "Mismatch" and inv_price is not None
                and po_price is not None and po_price > 0):
            pct = abs(inv_price - po_price) / po_price * 100
            if pct > variance_threshold:
                price_mismatches.append(
                    f"'{name}': Invoice ${inv_price:.2f} vs PO ${po_price:.2f}"
                    f" ({pct:.1f}% variance)"
                )

        # Quantity / GRN short delivery
        if inv_vs_grn == "Mismatch" and inv_qty is not None and grn_acc is not None:
            short = inv_qty - grn_acc
            # Check if damaged / QC rejection is noted in GRN
            condition_lower = condition.lower()
            if any(k in condition_lower for k in
                   ("damage", "defect", "reject", "qc", "esd", "broken", "failed")):
                damaged_items.append(
                    f"'{name}': {short} unit(s) failed QC — {condition}"
                )
            else:
                qty_mismatches.append(
                    f"'{name}': Invoiced {inv_qty} but GRN accepted {grn_acc}"
                    + (f" ({short} unit(s) in transit/short)" if short > 0 else "")
                )

    # Check UOM mismatch via raw mismatches text and node state
    price_uom = state.get("price_uom_match", {})
    if price_uom and not price_uom.get("uom_match", True):
        uom_details = price_uom.get("uom_details", [])
        for u in uom_details:
            if not u.get("match", True):
                uom_mismatches.append(
                    f"'{u.get('item','?')}': Invoice UOM '{u.get('invoice_uom','')}'"
                    f" vs PO UOM '{u.get('po_uom','')}'"
                )

    # Check payment terms mismatch
    terms_check = state.get("payment_terms_validation", {})
    if terms_check and not terms_check.get("match", True):
        inv_terms = terms_check.get("invoice_terms", "")
        po_terms  = terms_check.get("po_terms", "")
        reasons.append(
            f"Payment Terms Mismatch — Invoice: {inv_terms} vs PO: {po_terms}"
        )

    # Check freight overcharge
    freight_check = state.get("freight_validation", {})
    if freight_check and not freight_check.get("match", True):
        inv_freight = freight_check.get("invoice_freight", "")
        po_freight  = freight_check.get("po_freight", "")
        reasons.append(
            f"Freight Overcharge — Invoice: ${inv_freight} vs PO: ${po_freight}"
        )

    # Check tax mismatch
    tax_check = state.get("tax_reconciliation", {})
    if tax_check and not tax_check.get("match", True):
        inv_tax  = tax_check.get("invoice_tax_type", "")
        po_tax   = tax_check.get("po_tax_type", "")
        inv_rate = tax_check.get("invoice_rate", "")
        po_rate  = tax_check.get("po_rate", "")
        reasons.append(
            f"Tax Mismatch — Wrong GST Rate: Invoice {inv_tax or inv_rate}"
            f" vs PO expects {po_tax or po_rate}"
        )

    # Append classified reasons
    if damaged_items:
        for d in damaged_items:
            reasons.append(f"Damaged Goods — Quality Rejection: {d}")
    if qty_mismatches:
        for q in qty_mismatches:
            reasons.append(f"Short Delivery — Partial GRN: {q}")
    if price_mismatches:
        for p in price_mismatches:
            reasons.append(f"Price Variance — Unit Price Mismatch: {p}")
    if uom_mismatches:
        for u in uom_mismatches:
            reasons.append(f"UOM Mismatch — Unit of Measure Discrepancy: {u}")

    # Fallback if nothing classified above but line/grn still failed
    if not any([damaged_items, qty_mismatches, price_mismatches,
                uom_mismatches]) and (not reasons or all(
                    r.startswith("Payment") or r.startswith("Freight")
                    or r.startswith("Tax") for r in reasons)):
        # Use raw mismatch strings as a last resort
        for m in mismatches:
            ml = m.lower()
            if "vendor" in ml:
                if not any("Vendor" in r for r in reasons):
                    reasons.append(f"Vendor Mismatch — {m}")
            elif "quantity" in ml or "qty" in ml:
                if not any("Short Delivery" in r or "Damaged" in r for r in reasons):
                    reasons.append(f"Quantity Discrepancy — {m}")
            else:
                reasons.append(f"Line Mismatch — {m}")
