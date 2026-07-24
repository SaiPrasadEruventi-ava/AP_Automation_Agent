"""
reconciliation_config.py — Configurable Tolerance Rules
=========================================================
All thresholds in one place. Change here — applies everywhere.
Mirrors SAP MIRO / Oracle AP / Dynamics 365 tolerance settings.
"""

from dataclasses import dataclass
from pathlib import Path
import json


@dataclass
class ReconciliationConfig:

    # ── Amount tolerances (percentage) ────────────────────────────────
    PRICE_TOLERANCE_PCT:    float = 2.0   # unit price variance allowed
    AMOUNT_TOLERANCE_PCT:   float = 2.0   # invoice total vs PO total
    TAX_TOLERANCE_PCT:      float = 1.0   # tax amount variance
    FREIGHT_TOLERANCE_PCT:  float = 5.0   # freight/delivery charges
    DISCOUNT_TOLERANCE_PCT: float = 0.5   # discount variance

    # ── Quantity tolerances (percentage) ──────────────────────────────
    QUANTITY_TOLERANCE_PCT: float = 0.0   # strict by default (SAP standard)
    ALLOW_OVER_DELIVERY:    bool  = False  # GRN qty > PO qty

    # ── Date rules ────────────────────────────────────────────────────
    MAX_INVOICE_AGE_DAYS:   int   = 90    # reject invoices older than this
    ALLOW_FUTURE_INVOICE:   bool  = False # reject future-dated invoices

    # ── PO rules ──────────────────────────────────────────────────────
    ALLOWED_PO_STATUSES:    tuple = ("Open", "Partially Open")

    # ── Duplicate detection window ────────────────────────────────────
    DUPLICATE_WINDOW_DAYS:  int   = 365   # check last N days

    # ── Tax types supported ───────────────────────────────────────────
    SUPPORTED_TAX_TYPES:    tuple = ("GST", "VAT", "IGST", "CGST", "SGST",
                                     "Sales Tax", "Service Tax")

    # ── Payment terms ─────────────────────────────────────────────────
    ENFORCE_PAYMENT_TERMS:  bool  = True  # mismatch → manual review

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_json(cls, path: str) -> "ReconciliationConfig":
        """Load config from a JSON file for environment-specific overrides."""
        cfg = cls()
        if Path(path).exists():
            with open(path) as f:
                overrides = json.load(f)
            for k, v in overrides.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        return cfg


# Module-level default instance — import this everywhere
CFG = ReconciliationConfig()
