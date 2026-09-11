"""Core data model.

Mirrors the spec's Section 3 architecture:
  Layer 1 - static customer master (+ one-time Pre-MIS Outstanding)
  Layer 2 - append-only weekly snapshot (party-level movement bridge)
  Layer 3 - append-only action log (PTP commitments + status)

Extraction-side types (Voucher, LedgerEntry) are the raw shape a Tally XML
response is parsed into, before sign-flip and roll-forward are applied.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum


class VoucherType(str, Enum):
    SALES = "Sales"
    CREDIT_NOTE = "Credit Note"
    DEBIT_NOTE = "Debit Note"
    RECEIPT = "Receipt"
    JOURNAL = "Journal"


class PTPStatus(str, Enum):
    ACTIVE = "Active"
    KEPT = "Kept"
    BROKEN = "Broken"


@dataclass(frozen=True)
class LedgerEntry:
    """One bill-wise allocation line from BILLALLOCATIONS.LIST (or a plain
    ledger entry when no bill allocation exists), already carrying the raw
    Tally-signed amount. Sign-flip is applied downstream in ar_mis.sign, not
    here — this type is the untouched extraction result.
    """

    party_ledger_name: str
    amount_as_extracted: Decimal  # Tally sign convention: Dr negative, Cr positive
    bill_name: str | None = None


@dataclass(frozen=True)
class Voucher:
    voucher_type: VoucherType
    voucher_date: date
    voucher_number: str
    branch_id: str
    entries: list[LedgerEntry] = field(default_factory=list)


@dataclass(frozen=True)
class Party:
    party_id: str  # stable identifier used for cross-branch consolidation
    party_name: str
    branch_id: str


@dataclass(frozen=True)
class CustomerMasterRecord:
    """Layer 1. `pre_mis_outstanding` is the fixed baseline (Section 2.5) —
    set once, changed only via a logged PreMisAdjustment, never by the
    weekly extraction/ingestion path.
    """

    party_id: str
    party_name: str
    branch_id: str
    pre_mis_outstanding: Decimal


@dataclass(frozen=True)
class PreMisAdjustment:
    """An explicit, logged, one-off change to a party's Pre-MIS Outstanding
    baseline (e.g. legacy-balance write-off or collection). Never produced
    by the weekly pipeline — only by a deliberate manual action.
    """

    party_id: str
    branch_id: str
    amount: Decimal  # signed delta applied to pre_mis_outstanding
    reason: str
    adjusted_by: str
    adjusted_at: date


@dataclass(frozen=True)
class WeeklySnapshotRow:
    """Layer 2. One row per party per branch per week. All amounts are
    post-sign-flip. `closing_extracted` is the YTD Trial Balance closing
    balance pulled directly from Tally for this party; `closing_computed`
    is Opening + Sales + CN + DN + Receipts + Journals. Section 4.1
    compares the two — this row carries both so the reconciliation result
    is reproducible from stored data alone, not recomputed silently later.
    """

    party_id: str
    branch_id: str
    week_ending: date
    opening: Decimal
    sales: Decimal
    credit_notes: Decimal
    debit_notes: Decimal
    receipts: Decimal
    journals: Decimal
    closing_computed: Decimal
    closing_extracted: Decimal
    reconciled: bool
    difference: Decimal


@dataclass(frozen=True)
class PTPEntry:
    """Layer 3 creation record — immutable once logged."""

    ptp_id: str
    party_id: str
    branch_id: str
    created_week: date
    promised_amount: Decimal
    promised_date: date
    owner: str
    notes: str = ""


@dataclass(frozen=True)
class PTPStatusLogRow:
    """Layer 3 append-only status history for a PTPEntry. The current
    status for a PTP as of a given week is the latest row for that
    ptp_id with week_ending <= that week.
    """

    ptp_id: str
    week_ending: date
    status: PTPStatus
    updated_by: str
    notes: str = ""
