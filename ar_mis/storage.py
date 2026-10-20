"""Append-only SQLite storage for Layers 1-3.

SQLite chosen deliberately: LAN-only, no-cloud-dependency environment
(spec header), single-writer weekly batch job, and it gives the
reconciliation/reporting layers real query capability (Section 4.2's
"isolate which week and which voucher caused the drift" needs to filter
and join, not grep CSVs).

Layer 2 and Layer 3 tables are insert-only from this module's public API:
there is no update_weekly_snapshot() or update_ptp_status() that mutates
an existing row. A correction is a new row for a later week, or in the
case of a PTP status change, a new row in ptp_status_log — the history is
the point.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ar_mis.config import BranchConfig
from ar_mis.models import (
    CreditNoteRegisterRow,
    CustomerMasterRecord,
    FYRolloverSnapshot,
    InvoiceFollowUp,
    LedgerEntry,
    NoteType,
    PartyGrouping,
    PartyTBBalance,
    PreMisAdjustment,
    PTPEntry,
    PTPStatus,
    PTPStatusLogRow,
    ReceiptJournalRegisterRow,
    RegisterBuildExceptionRecord,
    RegisterClassification,
    RegisterRowAuditEntry,
    SalesDNRegisterRow,
    Voucher,
    VoucherType,
    WeeklyMovementRow,
    YtdDebtorVoucherRow,
)
from ar_mis.reconciliation import DriftFinding, DriftFindingRecord

# Schema changes must be additive-only — new tables, or new columns via
# ALTER TABLE ADD COLUMN — never a DROP/recreate of an existing table
# (design doc item 13). SCHEMA_VERSION is the version this code expects;
# MIGRATIONS[N] is the executescript text that brings a database from
# version N-1 to version N, applied in order by Store._run_migrations().
# Once a version has shipped, its MIGRATIONS entry is frozen — the next
# schema change is always a NEW higher-numbered entry, never an edit to
# an existing one, since a real client database may already be sitting
# at that version.
#
# MIGRATIONS[1] is deliberately the *entire* baseline schema, not a diff
# - every future version builds on it. Its CREATE TABLE statements are
# all IF NOT EXISTS, so replaying it is always safe even against a
# database that predates this versioning mechanism entirely (a real
# deployment here started before PRAGMA user_version was ever set, so
# such a database is sitting at SQLite's default user_version of 0
# despite already having this exact table shape — migrating it to
# version 1 must be a no-op, not an error).
SCHEMA_VERSION = 20

MIGRATIONS: dict[int, str] = {
    1: """
CREATE TABLE IF NOT EXISTS branch_master (
    branch_id TEXT PRIMARY KEY,
    branch_name TEXT NOT NULL,
    tally_company_name TEXT NOT NULL,
    tally_host TEXT NOT NULL DEFAULT 'localhost',
    tally_port INTEGER NOT NULL DEFAULT 9000
);

CREATE TABLE IF NOT EXISTS customer_master (
    party_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    party_name TEXT NOT NULL,
    pre_mis_outstanding TEXT NOT NULL,
    last_reconciled_on TEXT,
    last_reconciled_by TEXT,
    PRIMARY KEY (party_id, branch_id)
);

CREATE TABLE IF NOT EXISTS pre_mis_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    party_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    amount TEXT NOT NULL,
    reason TEXT NOT NULL,
    adjusted_by TEXT NOT NULL,
    adjusted_at TEXT NOT NULL,
    FOREIGN KEY (party_id, branch_id) REFERENCES customer_master (party_id, branch_id)
);

CREATE TABLE IF NOT EXISTS weekly_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    party_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    week_ending TEXT NOT NULL,
    opening TEXT NOT NULL,
    sales TEXT NOT NULL,
    credit_notes TEXT NOT NULL,
    debit_notes TEXT NOT NULL,
    receipts TEXT NOT NULL,
    journals TEXT NOT NULL,
    closing_computed TEXT NOT NULL,
    closing_extracted TEXT NOT NULL,
    reconciled INTEGER NOT NULL,
    difference TEXT NOT NULL,
    UNIQUE (party_id, branch_id, week_ending)
);

CREATE TABLE IF NOT EXISTS ptp_register (
    ptp_id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    created_week TEXT NOT NULL,
    promised_amount TEXT NOT NULL,
    promised_date TEXT NOT NULL,
    owner TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ptp_status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ptp_id TEXT NOT NULL,
    week_ending TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (ptp_id) REFERENCES ptp_register (ptp_id)
);

-- Natural-key record of every voucher line that fed a given week's build,
-- for Section 4.2 drift isolation: a fresh YTD pull can be diffed against
-- this table to find exactly which voucher(s) were absent from the
-- original incremental run (e.g. a backdated entry) and which week's
-- date range they land in.
CREATE TABLE IF NOT EXISTS voucher_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id TEXT NOT NULL,
    week_ending TEXT NOT NULL,
    party_ledger_name TEXT NOT NULL,
    voucher_type TEXT NOT NULL,
    voucher_number TEXT NOT NULL,
    voucher_date TEXT NOT NULL,
    flipped_amount TEXT NOT NULL,
    UNIQUE (branch_id, party_ledger_name, voucher_type, voucher_number, voucher_date, flipped_amount)
);
""",
    # Registers design doc (docs/registers_and_reporting_design.md) items
    # 1-4/7-10 — the invoice-level master registers replacing weekly_snapshot
    # as the base of reporting. Row identity per item 2 is Branch + Voucher
    # Number + Party, not bill_allocation_reference/target_doc_no (item 8's
    # scoping note: those are lookup fields, not identity — they can
    # legitimately collide across parties in real Tally data). INSERT OR
    # IGNORE + these UNIQUE constraints is how re-extraction of an
    # overlapping date range stays append-only-safe: a voucher already
    # recorded here is silently skipped rather than duplicated, exactly
    # like the existing voucher_log table's own dedup pattern.
    2: """
CREATE TABLE IF NOT EXISTS sales_dn_register (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    note_type TEXT NOT NULL,
    voucher_number TEXT NOT NULL,
    bill_allocation_reference TEXT NOT NULL,
    party_id TEXT NOT NULL,
    taxable_value TEXT NOT NULL,
    cgst TEXT NOT NULL,
    sgst TEXT NOT NULL,
    igst TEXT NOT NULL,
    invoice_value TEXT NOT NULL,
    due_date TEXT NOT NULL,
    job_id TEXT,
    UNIQUE (branch_id, voucher_number, party_id)
);

-- classification (item 10) starts Current and is the one field this
-- table's append-only-ness doesn't apply to: resolve_credit_note_classification()
-- updates it in place once a human confirms a Pending Review reference as
-- Pre-MIS Adjustment (or Tally itself is corrected and the row is
-- re-extracted under a now-matching reference — never a silent flip back
-- to Current from inside this system).
CREATE TABLE IF NOT EXISTS credit_note_register (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id TEXT NOT NULL,
    cn_date TEXT NOT NULL,
    voucher_number TEXT NOT NULL,
    party_id TEXT NOT NULL,
    cn_amount TEXT NOT NULL,
    bill_allocation_reference TEXT,
    classification TEXT NOT NULL DEFAULT 'Current',
    reason TEXT NOT NULL DEFAULT '',
    UNIQUE (branch_id, voucher_number, party_id)
);

-- One row per bill-allocation line (a Receipt/Journal voucher can apply
-- against several invoices at once — see ar_mis.registers), so identity
-- includes target_doc_no + amount, not just the voucher — mirrors
-- voucher_log's own reasoning for including amount in its dedup key.
-- classification is set and updated per VOUCHER (item 10's invariant:
-- every line of a flagged voucher travels together) — see
-- resolve_receipt_journal_classification(), which updates every row
-- sharing (branch_id, voucher_number, party_id) in one statement, never
-- a single line in isolation.
CREATE TABLE IF NOT EXISTS receipt_journal_register (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id TEXT NOT NULL,
    txn_date TEXT NOT NULL,
    voucher_type TEXT NOT NULL,
    voucher_number TEXT NOT NULL,
    party_id TEXT NOT NULL,
    amount TEXT NOT NULL,
    target_doc_no TEXT,
    classification TEXT NOT NULL DEFAULT 'Current',
    narration TEXT NOT NULL DEFAULT '',
    UNIQUE (branch_id, voucher_number, party_id, target_doc_no, amount)
);

-- Item 7's one deliberately-mutable/upserted row — human-entered PTP and
-- follow-up fields with no other source of truth, keyed to the same
-- (branch, voucher, party) identity as the invoice it follows up on.
CREATE TABLE IF NOT EXISTS invoice_follow_up (
    branch_id TEXT NOT NULL,
    voucher_number TEXT NOT NULL,
    party_id TEXT NOT NULL,
    ptp_date TEXT,
    ptp_amount TEXT,
    next_action TEXT NOT NULL DEFAULT '',
    expected_collection_date TEXT,
    updated_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (branch_id, voucher_number, party_id)
);

-- Item 4 — a frozen record created only by a deliberate year-end rollover
-- action (never automatically). UNIQUE on the transition itself so the
-- same invoice can't be rolled into the same target FY twice by mistake;
-- rolling the same still-open invoice into a LATER FY transition is a
-- separate, legitimate row.
CREATE TABLE IF NOT EXISTS fy_rollover_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id TEXT NOT NULL,
    voucher_number TEXT NOT NULL,
    party_id TEXT NOT NULL,
    from_financial_year TEXT NOT NULL,
    to_financial_year TEXT NOT NULL,
    open_amount_at_rollover TEXT NOT NULL,
    rolled_over_by TEXT NOT NULL,
    rolled_over_at TEXT NOT NULL,
    UNIQUE (branch_id, voucher_number, party_id, to_financial_year)
);
""",
    # PTP Kept Rate (design doc item 13) needs to know when the CURRENT
    # promise was logged to correctly judge "was the promised amount paid
    # since the promise was made" - see InvoiceFollowUp.logged_at's
    # docstring for why a total-ever-collected check would be wrong. This
    # table already shipped in MIGRATIONS[2] without this column, so per
    # this module's own frozen-once-shipped rule it's a new ALTER TABLE
    # here, not an edit to that entry.
    3: """
ALTER TABLE invoice_follow_up ADD COLUMN logged_at TEXT;
""",
    # CustomerMasterRecord (models.py) has carried `grouping` and
    # `credit_period_days` since last night's registers work, but the
    # actual table never gained the columns - a real gap found while
    # wiring registers.build_sales_dn_register_row into the pipeline,
    # which needs credit_period_days to compute Due Date (item 6).
    # credit_period_days defaults to 30, matching CustomerMasterRecord's
    # own default.
    4: """
ALTER TABLE customer_master ADD COLUMN grouping TEXT;
ALTER TABLE customer_master ADD COLUMN credit_period_days INTEGER NOT NULL DEFAULT 30;
""",
    # Client's explicit ask: a viewer of the registers/reports screens
    # needs to see when the underlying data was actually last pulled -
    # not the business week_ending a run covers (an operator could run a
    # backlogged week's extraction well after that week ended), but the
    # real wall-clock moment. One row per successful (PASS-outcome) run.
    5: """
CREATE TABLE IF NOT EXISTS extraction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id TEXT NOT NULL,
    week_ending TEXT NOT NULL,
    extracted_at TEXT NOT NULL
);
""",
    # Design doc item 15's Weekly Movement Register, resolved this session
    # as append-only stored history (Open Item 4): each week's row is
    # computed once, by a Maker explicitly recording the current
    # portfolio position, and kept as-is forever after - never silently
    # recomputed or overwritten, which is exactly what week_ending as the
    # PRIMARY KEY enforces (a second attempt to record the same week fails
    # loudly rather than replacing history). The KPI fields are the same
    # ones ar_mis.dashboard.compute_ar_snapshot already computes; this
    # table only adds the point-in-time persistence that module
    # deliberately doesn't do. dict-shaped fields (by FY, by ageing
    # bucket) are stored as JSON text - there's no need for a normalized
    # table when nothing ever queries into those keys directly, only
    # displays them whole.
    6: """
CREATE TABLE IF NOT EXISTS weekly_movement (
    week_ending TEXT PRIMARY KEY,
    total_ar TEXT NOT NULL,
    open_ar_by_fy_json TEXT NOT NULL,
    pre_mis_outstanding TEXT NOT NULL,
    overdue_ar TEXT NOT NULL,
    overdue_by_bucket_json TEXT NOT NULL,
    dso TEXT,
    collection_efficiency TEXT,
    unapplied_cash TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
""",
    # Section 4.2's YTD drift findings (backdated entries) were being
    # discovered by isolate_drift() but never persisted - shown once on
    # the result page of the run that found them, then gone if nobody
    # acted immediately. Fixed this session, client's explicit ask. The
    # UNIQUE constraint is what makes a still-unresolved finding land
    # once, not spawn a new row every time a later extraction re-detects
    # the same backdated voucher (see DriftFindingRecord's own docstring).
    # `acknowledged` is a human audit note only - it never touches
    # weekly_snapshot or any register; the actual correction mechanism
    # for a backdated entry is separate, deferred work (design doc item 18).
    7: """
CREATE TABLE IF NOT EXISTS drift_finding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id TEXT NOT NULL,
    party_ledger_name TEXT NOT NULL,
    voucher_type TEXT NOT NULL,
    voucher_number TEXT NOT NULL,
    voucher_date TEXT NOT NULL,
    flipped_amount TEXT NOT NULL,
    attributed_week TEXT,
    discovered_at TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    UNIQUE(branch_id, party_ledger_name, voucher_type, voucher_number, voucher_date, flipped_amount)
);
""",
    # The correction mechanism for a backdated entry, client's explicit
    # ask this session: incorporating a finding means replaying its
    # ORIGINAL voucher (all entries - party, revenue, tax, round-off
    # lines) through the normal register-building pipeline under a new
    # correction week, so it needs the full voucher, not just the
    # summary fields already on this table - stored as JSON since
    # nothing ever needs to query into it, only reconstruct it whole.
    # `incorporated` is a distinct, separate signal from `acknowledged`
    # (added in MIGRATIONS[7]) - see DriftFindingRecord's own docstring
    # for why the two are never conflated.
    8: """
ALTER TABLE drift_finding ADD COLUMN voucher_json TEXT;
ALTER TABLE drift_finding ADD COLUMN incorporated INTEGER NOT NULL DEFAULT 0;
ALTER TABLE drift_finding ADD COLUMN incorporated_by TEXT;
ALTER TABLE drift_finding ADD COLUMN incorporated_at TEXT;
ALTER TABLE drift_finding ADD COLUMN incorporated_week_ending TEXT;
""",
    # Client's explicit reversal this session: extraction no longer snaps
    # to a Monday-Sunday calendar grid (see ar_mis.config's own docstring
    # for the real incident that forced this) - a run's own start date can
    # no longer be recovered by subtracting 6 days from week_ending, so it
    # has to be stored explicitly. Every row that predates this column
    # was, without exception, saved back when the fixed-7-day-week
    # assumption still held everywhere in this codebase, so backfilling
    # via that same subtraction is exact for existing data, not a guess -
    # it only stops being derivable for rows saved from here on, which is
    # exactly why they're the ones that now store it directly.
    9: """
ALTER TABLE weekly_snapshot ADD COLUMN period_start TEXT;
UPDATE weekly_snapshot SET period_start = date(week_ending, '-6 days') WHERE period_start IS NULL;
""",
    # A "vouchers not added to a register" exception (registers.
    # RegisterBuildExceptions) was previously shown once on the result
    # page of the run that found it, then gone - the exact same gap
    # DriftFindingRecord (MIGRATIONS[7]) fixed for backdated entries,
    # now fixed here too, client's explicit ask this session: a review
    # workflow needs something durable to review. `status` is a human
    # audit trail only ("reviewed_no_action"/"resolved_via_catchup"),
    # like `acknowledged` on drift_finding - it never touches
    # weekly_snapshot or any register itself; the actual fix for a
    # "resolved_via_catchup" case is the separate Catch Up a Party run,
    # this table only records that a human made that call and why. No
    # UNIQUE constraint (unlike drift_finding): each SAVE run logs its
    # own truthful snapshot of that run's own exceptions, and a given
    # voucher_number is only ever seen in the one run whose date range
    # covers it, so duplicates across runs aren't the real risk here.
    10: """
CREATE TABLE IF NOT EXISTS register_build_exception (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id TEXT NOT NULL,
    week_ending TEXT NOT NULL,
    voucher_number TEXT NOT NULL,
    party_ledger_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    reviewed_by TEXT,
    reviewed_at TEXT,
    reviewed_note TEXT
);
""",
    # Client's explicit ask: a reference credit limit per party, editable
    # from Customer Master, defaulting to Rs 1,00,00,000 (one crore) -
    # purely informational in this secondary reporting layer (it has no
    # power to block a sale in Tally, the actual system of record), but
    # a starting point for flagging a party whose exposure has grown
    # past what was approved for them.
    11: """
ALTER TABLE customer_master ADD COLUMN credit_limit TEXT NOT NULL DEFAULT '10000000.00';
""",
    # A reviewer couldn't tell "no leg touches a tracked debtor" on a
    # Journal (mundane - a freight vendor payment) from the identical
    # message on a Credit Note (worth a second look), since both
    # builders share that exact wording. Existing rows get '' - true
    # gap for anything logged before this migration, never guessed at.
    12: """
ALTER TABLE register_build_exception ADD COLUMN voucher_type TEXT NOT NULL DEFAULT '';
""",
    # Client's own later call: a free-text "who made this edit" box was
    # pure friction on a single-operator Maker role with no real login
    # behind it (InvoiceFollowUp.updated_by is now a fixed "Maker"
    # literal, never typed) - "when" has to carry the full audit signal
    # alone from here. logged_at only moves when the PTP promise itself
    # changes (see its own docstring); this is the genuine "row touched"
    # timestamp, stamped on every single save regardless of which field
    # changed.
    13: """
ALTER TABLE invoice_follow_up ADD COLUMN updated_at TEXT;
""",
    # Section 4.2's "TB vs Registers" cross-check (docs/registers_and_
    # reporting_design.md) - an independent second read of what Tally
    # itself says touched a Sundry Debtor (TallyClient.fetch_sundry_
    # debtor_vouchers, scoped at the source, never the whole company's
    # vouchers filtered down afterward), persisted on its own so it can be
    # compared against the three master registers rather than trusted as
    # a one-off in-memory check. UNIQUE includes as_of (not just branch +
    # voucher + party) because a later run's pull for a different
    # reporting date is a legitimately separate snapshot, not a duplicate
    # of an earlier one - only re-running the SAME as_of date (e.g. a
    # chunk-boundary overlap) should be silently deduplicated.
    14: """
CREATE TABLE IF NOT EXISTS ytd_debtor_voucher (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    voucher_date TEXT NOT NULL,
    voucher_type TEXT NOT NULL,
    raw_voucher_type_name TEXT NOT NULL,
    voucher_number TEXT NOT NULL,
    party_id TEXT NOT NULL,
    amount TEXT NOT NULL,
    UNIQUE (branch_id, as_of, voucher_number, party_id)
);
""",
    # Phase 6b's Delete/Modify overlay on the three master registers used
    # to live here (register_row_correction table, read back via
    # ar_mis.register_corrections.effective_corrections). The client's
    # later, explicit redesign this session replaced that overlay with
    # real row mutation + register_row_audit (see migration 19 below);
    # this table is kept, unread, as permanent audit history of every
    # correction it ever recorded - never dropped, never migrated.
    15: """
CREATE TABLE IF NOT EXISTS register_row_correction (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id TEXT NOT NULL,
    voucher_type TEXT NOT NULL,
    voucher_number TEXT NOT NULL,
    party_id TEXT NOT NULL,
    action TEXT NOT NULL,
    corrected_amount TEXT,
    reason TEXT NOT NULL,
    corrected_by TEXT NOT NULL,
    corrected_at TEXT NOT NULL
);
""",
    # Branch-specific voucher type name mapping (client's own ask: every
    # one of this app's five AR-relevant categories - Sales, Debit Note,
    # Credit Note, Receipt, Journal - was matched against a single global,
    # hardcoded keyword list (ar_mis.parsers._CATEGORY_KEYWORDS), which is
    # exactly how a client's "Proforma Sales" voucher type (containing the
    # substring "sales") got miscategorized as a real Sale. A plain
    # config table, not an audit log like register_row_correction above -
    # replaced wholesale on save, same as every other Branch Master field.
    # No UNIQUE beyond the natural key: a branch is free to list the same
    # alias under only one category at a time, enforced by the Store
    # method that writes this table (delete-then-insert per branch), not
    # by a DB constraint.
    16: """
CREATE TABLE IF NOT EXISTS voucher_type_alias (
    branch_id TEXT NOT NULL,
    category TEXT NOT NULL,
    alias_name TEXT NOT NULL,
    PRIMARY KEY (branch_id, category, alias_name)
);
""",
    # Client's explicit call this session: weekly_snapshot's per-week,
    # per-party accumulation was never needed for TB Cross-Check, AR
    # Snapshot, or Ageing Matrix to do their actual jobs - every one of
    # them only ever needed Tally's CURRENT stated closing per party, not
    # a growing history of past ones, because the registers (already
    # append-only, already date-stamped per voucher) can reconstruct any
    # past position on demand (ar_mis.rollforward.
    # compute_party_closings_as_of) without anything having been saved
    # for that purpose. party_tb_balance replaces it: one row per party,
    # upserted (never appended) on every extraction - the single current
    # reading, nothing else. weekly_snapshot itself is deliberately NOT
    # dropped here (this app's own never-discard-data principle) - it
    # simply stops being written or read by any code from this version
    # on, and whatever history already exists in it on a live database
    # stays on disk, untouched, for anyone who needs to look at it
    # directly.
    #
    # extraction_log gains its own period_start for the same reason
    # weekly_snapshot once needed it (Branch Master's coverage summary,
    # delete_branch_week's audit trail) - backfilled here from whichever
    # weekly_snapshot row shared that run's (branch_id, week_ending),
    # since every party in one run recorded the identical period_start.
    17: """
CREATE TABLE IF NOT EXISTS party_tb_balance (
    party_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    closing_extracted TEXT NOT NULL,
    PRIMARY KEY (party_id, branch_id)
);

ALTER TABLE extraction_log ADD COLUMN period_start TEXT;

UPDATE extraction_log
SET period_start = (
    SELECT w.period_start FROM weekly_snapshot w
    WHERE w.branch_id = extraction_log.branch_id AND w.week_ending = extraction_log.week_ending
    LIMIT 1
)
WHERE period_start IS NULL;

INSERT OR IGNORE INTO party_tb_balance (party_id, branch_id, as_of, closing_extracted)
SELECT w.party_id, w.branch_id, w.week_ending, w.closing_extracted
FROM weekly_snapshot w
WHERE w.week_ending = (
    SELECT MAX(w2.week_ending) FROM weekly_snapshot w2
    WHERE w2.party_id = w.party_id AND w2.branch_id = w.branch_id
);
""",
    # Client's explicit call: TB Cross-Check's Opening must be the figure
    # originally fed in, frozen forever - never the living, adjustment-
    # updated customer_master.pre_mis_outstanding that AR Snapshot and the
    # Pre-MIS Adjustment Register still correctly rely on. This column is
    # that frozen figure. Only the column is added here (SQL) - its
    # backfill for existing parties needs real Decimal arithmetic against
    # the full pre_mis_adjustments history (original = current minus every
    # adjustment ever applied), which is precision-risky to do as raw SQL
    # over TEXT-stored decimals, so Store._backfill_original_pre_mis_
    # outstanding does it in Python immediately after this migration runs.
    18: """
ALTER TABLE customer_master ADD COLUMN original_pre_mis_outstanding TEXT;
""",
    # Client's explicit redesign this session: the old register_row_
    # correction overlay (migration 15) - "stop counting this row, as if
    # it never existed, for computation purposes" - never actually
    # touched the register itself, which left a Maker able to mark a bad
    # voucher "resolved" on Check 1 while it kept sitting in the real
    # Sales & DN Register, Ageing, DSO, everywhere else, unchanged. That
    # table is NOT dropped (never-discard-data) but nothing writes to it
    # from this version on - register_row_audit replaces its actual job.
    #
    # Edit and Delete now mutate the real register row - a genuine UPDATE
    # or DELETE against sales_dn_register/credit_note_register/
    # receipt_journal_register, the one and only place every screen reads
    # from, so nothing downstream can forget to apply a correction ever
    # again. The audit trail moves here instead: `before`/`after` are
    # each a JSON list of the affected row(s) own full field set (a list,
    # not a single object, so one voucher-wide action on a multi-line
    # Receipt/Journal voucher is still exactly one audit entry) - enough
    # to reconstruct exactly what existed before an action and undo it
    # later. `row_id` is the specific register row a Maker actually
    # selected to start the action, kept for traceability even when the
    # action ended up voucher-wide. `undone_of` is set only on an UNDO
    # entry, naming the earlier entry it reverses - Undo is itself a new,
    # logged action, never a silent rewind.
    19: """
CREATE TABLE IF NOT EXISTS register_row_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    register TEXT NOT NULL,
    action TEXT NOT NULL,
    row_id INTEGER NOT NULL,
    branch_id TEXT NOT NULL,
    voucher_number TEXT NOT NULL,
    party_id TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    reason TEXT NOT NULL,
    performed_by TEXT NOT NULL,
    performed_at TEXT NOT NULL,
    undone_of INTEGER
);
""",
    # Real, pre-existing bug surfaced (not caused) by this session's
    # Sales & DN Edit expansion: SalesDNRegisterRow.round_off has had a
    # model field and a UI column since the field was added, but
    # append_sales_dn_row never wrote it and all_sales_dn_rows never read
    # it back - every row ever written to storage silently lost its real
    # Round Off and read back as the dataclass default (0.00) regardless
    # of what the voucher actually carried. A row written before this
    # migration has no way to recover its true original Round Off (it
    # was never captured anywhere, not even transiently) - NULL here
    # means exactly that "unknown, treat as 0.00 same as it always
    # displayed", read back via COALESCE at the Python layer, never
    # backfilled with a guessed number.
    20: "ALTER TABLE sales_dn_register ADD COLUMN round_off TEXT;",
}


class Store:
    def __init__(self, db_path: str):
        # sqlite3.connect() fails with "unable to open database file" if
        # the parent directory doesn't exist yet - true for a fresh
        # checkout, since an empty `data/` dir can't be committed to git.
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        # Checked before connect() - sqlite3.connect() itself creates the
        # file if missing, which would make this check always see "exists"
        # afterwards and wrongly treat a brand-new database as one that
        # needs backing up (design doc item 13: only an existing database
        # being migrated needs a backup - there's nothing to protect in a
        # database that has no data yet).
        db_file = Path(db_path)
        is_new_db = db_path == ":memory:" or not db_file.exists() or db_file.stat().st_size == 0
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._run_migrations(db_path, is_new_db)

    def _run_migrations(self, db_path: str, is_new_db: bool) -> None:
        """Design doc item 13: schema version-sticker migration, run fully
        automatically at startup whenever PRAGMA user_version is behind
        this code's SCHEMA_VERSION - no separate button, no step to
        remember. A backup is taken first, unconditionally, whenever an
        EXISTING database (is_new_db False) is behind - even if the
        specific versions being applied turn out to be no-op CREATE TABLE
        IF NOT EXISTS statements against a database that already has that
        shape, per the design doc's "regardless" on this point. A brand
        new database just created for this run skips the backup (nothing
        to protect yet) and walks every version from 1 up in one go, which
        leaves it at the exact same shape a migrated database converges
        on.
        """
        current_version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version >= SCHEMA_VERSION:
            return
        if not is_new_db:
            self._backup_before_migration(db_path, current_version)
        needs_pre_mis_backfill = current_version < 18 <= SCHEMA_VERSION
        for version in range(current_version + 1, SCHEMA_VERSION + 1):
            self.conn.executescript(MIGRATIONS[version])
            self.conn.execute(f"PRAGMA user_version = {version}")
        if needs_pre_mis_backfill:
            self._backfill_original_pre_mis_outstanding()
        self.conn.commit()

    def _backfill_original_pre_mis_outstanding(self) -> None:
        """One-time, run only the first time a database crosses into
        schema version 18 (see MIGRATIONS[18]'s own comment for why this
        isn't plain SQL). For every existing party: original = current
        pre_mis_outstanding minus every adjustment ever recorded against
        them - exact, not a guess, since record_pre_mis_adjustment's own
        math (new_balance = old + amount) makes this invertible. A party
        with no adjustments on record gets original == current, correctly
        (nothing has ever moved it). A brand-new party created after this
        migration never needs this path - upsert_customer_master sets
        both columns equal at creation time directly.
        """
        self.conn.row_factory = sqlite3.Row
        parties = self.conn.execute(
            "SELECT party_id, branch_id, pre_mis_outstanding FROM customer_master"
        ).fetchall()
        adjustment_totals: dict[tuple[str, str], Decimal] = {}
        for row in self.conn.execute("SELECT party_id, branch_id, amount FROM pre_mis_adjustments"):
            key = (row["party_id"], row["branch_id"])
            adjustment_totals[key] = adjustment_totals.get(key, Decimal("0.00")) + Decimal(row["amount"])
        for party in parties:
            key = (party["party_id"], party["branch_id"])
            original = Decimal(party["pre_mis_outstanding"]) - adjustment_totals.get(key, Decimal("0.00"))
            self.conn.execute(
                "UPDATE customer_master SET original_pre_mis_outstanding=? WHERE party_id=? AND branch_id=?",
                (str(original), party["party_id"], party["branch_id"]),
            )

    def _backup_before_migration(self, db_path: str, from_version: int) -> None:
        """Uses SQLite's own backup API rather than a raw file copy, so
        the backup is crash-consistent regardless of this connection's
        journal/WAL state - this runs before anything in the migration
        itself has been written, so `self.conn` is exactly the pre-
        migration database.
        """
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup_path = f"{db_path}.backup-v{from_version}-{timestamp}.db"
        backup_conn = sqlite3.connect(backup_path)
        try:
            self.conn.backup(backup_conn)
        finally:
            backup_conn.close()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- Branch Master (extraction config, user-editable) ----------
    #
    # This is deliberately a database table, not a hardcoded Python list
    # (ar_mis/config.py used to carry a placeholder BRANCHES list) - the
    # user configuring which branches exist and how to reach their Tally
    # instance is master-data entry, exactly like Layer 1's customer
    # master, not something that should require editing source code.

    def list_branches(self) -> list[BranchConfig]:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute("SELECT * FROM branch_master ORDER BY branch_name")
        return [
            BranchConfig(
                branch_id=row["branch_id"],
                branch_name=row["branch_name"],
                tally_company_name=row["tally_company_name"],
                tally_host=row["tally_host"],
                tally_port=row["tally_port"],
            )
            for row in cur.fetchall()
        ]

    def get_branch(self, branch_id: str) -> BranchConfig | None:
        self.conn.row_factory = sqlite3.Row
        row = self.conn.execute("SELECT * FROM branch_master WHERE branch_id=?", (branch_id,)).fetchone()
        if row is None:
            return None
        return BranchConfig(
            branch_id=row["branch_id"],
            branch_name=row["branch_name"],
            tally_company_name=row["tally_company_name"],
            tally_host=row["tally_host"],
            tally_port=row["tally_port"],
        )

    def upsert_branch(self, branch: BranchConfig) -> None:
        self.conn.execute(
            "INSERT INTO branch_master (branch_id, branch_name, tally_company_name, tally_host, tally_port)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(branch_id) DO UPDATE SET"
            " branch_name=excluded.branch_name, tally_company_name=excluded.tally_company_name,"
            " tally_host=excluded.tally_host, tally_port=excluded.tally_port",
            (branch.branch_id, branch.branch_name, branch.tally_company_name, branch.tally_host, branch.tally_port),
        )
        self.conn.commit()

    def delete_branch(self, branch_id: str) -> None:
        self.conn.execute("DELETE FROM branch_master WHERE branch_id=?", (branch_id,))
        self.conn.commit()

    # ---- Voucher Type Aliases (per-branch, user-editable) -----------
    #
    # Client's own ask: ar_mis.parsers.categorize_voucher_type's five
    # categories (Sales, Debit Note, Credit Note, Receipt, Journal) were
    # matched against one global, hardcoded keyword list - no way for a
    # Maker to tell this app what a DIFFERENT client's Tally actually
    # calls its own voucher types without editing source code. An empty
    # result for a branch means "not configured yet" - ar_mis.parsers'
    # own exact-match-when-configured, keyword-fallback-otherwise logic
    # treats that as "use the global keyword list", so every branch
    # already in production keeps working unchanged until someone
    # explicitly sets this up.

    def voucher_type_aliases(self, branch_id: str) -> dict[str, list[str]]:
        cur = self.conn.execute(
            "SELECT category, alias_name FROM voucher_type_alias WHERE branch_id=? ORDER BY category, alias_name",
            (branch_id,),
        )
        aliases: dict[str, list[str]] = {}
        for category, alias_name in cur.fetchall():
            aliases.setdefault(category, []).append(alias_name)
        return aliases

    def set_voucher_type_aliases(self, branch_id: str, aliases: dict[str, list[str]]) -> None:
        """Replaces every alias this branch has on record - a plain config
        table (see its own CREATE TABLE comment), not an audit log, so a
        wholesale replace on save is correct here, unlike register_row_
        correction's append-only history.
        """
        self.conn.execute("DELETE FROM voucher_type_alias WHERE branch_id=?", (branch_id,))
        for category, names in aliases.items():
            for name in names:
                stripped = name.strip()
                if not stripped:
                    continue
                self.conn.execute(
                    "INSERT OR IGNORE INTO voucher_type_alias (branch_id, category, alias_name) VALUES (?, ?, ?)",
                    (branch_id, category, stripped),
                )
        self.conn.commit()

    # ---- Layer 1: customer master ---------------------------------

    def upsert_customer_master(self, record: CustomerMasterRecord) -> None:
        """Create-or-refresh party reference data. Explicitly does NOT
        touch pre_mis_outstanding, grouping, credit_period_days, or
        credit_limit for a party that already exists — those fields only
        move via record_pre_mis_adjustment, set_party_grouping,
        set_credit_period_days, and set_credit_limit respectively (item
        7's editable-fields list). Safe to call every week for
        master-data sync (name changes etc.) without risking any of the
        fields that must never move through this path. A brand-new party
        gets `record`'s own grouping/credit_period_days/credit_limit
        (None/30/one crore by default) as its starting point.
        """
        existing = self.conn.execute(
            "SELECT pre_mis_outstanding FROM customer_master WHERE party_id=? AND branch_id=?",
            (record.party_id, record.branch_id),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO customer_master"
                " (party_id, branch_id, party_name, pre_mis_outstanding, original_pre_mis_outstanding,"
                " grouping, credit_period_days, credit_limit)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.party_id,
                    record.branch_id,
                    record.party_name,
                    str(record.pre_mis_outstanding),
                    str(record.pre_mis_outstanding),
                    record.grouping.value if record.grouping else None,
                    record.credit_period_days,
                    str(record.credit_limit),
                ),
            )
        else:
            self.conn.execute(
                "UPDATE customer_master SET party_name=? WHERE party_id=? AND branch_id=?",
                (record.party_name, record.party_id, record.branch_id),
            )
        self.conn.commit()

    def get_customer_master(self, party_id: str, branch_id: str) -> CustomerMasterRecord | None:
        self.conn.row_factory = sqlite3.Row
        r = self.conn.execute(
            "SELECT * FROM customer_master WHERE party_id=? AND branch_id=?", (party_id, branch_id)
        ).fetchone()
        if r is None:
            return None
        return CustomerMasterRecord(
            party_id=r["party_id"],
            party_name=r["party_name"],
            branch_id=r["branch_id"],
            pre_mis_outstanding=Decimal(r["pre_mis_outstanding"]),
            grouping=PartyGrouping(r["grouping"]) if r["grouping"] else None,
            credit_period_days=r["credit_period_days"],
            credit_limit=Decimal(r["credit_limit"]),
        )

    def all_customer_masters(self) -> list[CustomerMasterRecord]:
        """Every customer_master row as a fully-typed CustomerMasterRecord
        (Decimal, PartyGrouping) - unlike all_customer_master_records(),
        which returns raw sqlite3.Row for the Customer Master webapp page's
        own dict-style template access. Needed wherever a caller (the AR
        Snapshot dashboard) needs real .pre_mis_outstanding/.grouping
        values to compute with, not just display.
        """
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute("SELECT * FROM customer_master")
        return [
            CustomerMasterRecord(
                party_id=r["party_id"],
                party_name=r["party_name"],
                branch_id=r["branch_id"],
                pre_mis_outstanding=Decimal(r["pre_mis_outstanding"]),
                grouping=PartyGrouping(r["grouping"]) if r["grouping"] else None,
                credit_period_days=r["credit_period_days"],
                credit_limit=Decimal(r["credit_limit"]),
            )
            for r in cur.fetchall()
        ]

    def set_credit_period_days(self, party_id: str, branch_id: str, credit_period_days: int) -> None:
        """Item 6/7: the one sanctioned way to change a party's credit
        period. Per item 6, this only affects invoices built AFTER this
        call — Due Date is computed once at invoice-build time
        (registers.compute_due_date) and never retroactively recalculated,
        so changing this has no effect on any SalesDNRegisterRow already
        stored.
        """
        if not self.customer_master_exists(party_id, branch_id):
            raise ValueError(f"No customer_master record for {party_id}/{branch_id}")
        self.conn.execute(
            "UPDATE customer_master SET credit_period_days=? WHERE party_id=? AND branch_id=?",
            (credit_period_days, party_id, branch_id),
        )
        self.conn.commit()

    def set_credit_limit(self, party_id: str, branch_id: str, credit_limit: Decimal) -> None:
        """Client's explicit ask: the one sanctioned way to change a
        party's reference credit limit (default Rs 1,00,00,000). Purely
        informational in this secondary reporting layer - it cannot and
        does not enforce anything in Tally, the actual system of record.
        """
        if not self.customer_master_exists(party_id, branch_id):
            raise ValueError(f"No customer_master record for {party_id}/{branch_id}")
        self.conn.execute(
            "UPDATE customer_master SET credit_limit=? WHERE party_id=? AND branch_id=?",
            (str(credit_limit), party_id, branch_id),
        )
        self.conn.commit()

    def set_party_grouping(self, party_id: str, branch_id: str, grouping: PartyGrouping) -> None:
        """Item 5/7: the one sanctioned way to classify a party as Sundry
        Debtor or Related Party."""
        if not self.customer_master_exists(party_id, branch_id):
            raise ValueError(f"No customer_master record for {party_id}/{branch_id}")
        self.conn.execute(
            "UPDATE customer_master SET grouping=? WHERE party_id=? AND branch_id=?",
            (grouping.value, party_id, branch_id),
        )
        self.conn.commit()

    def mark_reconciled(self, party_id: str, branch_id: str, reconciled_on: date, reconciled_by: str) -> None:
        """Records that a human on the AR team personally reviewed this
        party - separate from, and a complement to, the automated
        zero-tolerance check in reconciliation.py. The automated check
        catches numeric mismatches; it cannot catch "this looks off" or
        "someone should double-check this classification". Tracking who
        and when puts a name against the human review, not just a
        checkbox nobody is accountable for.
        """
        if not self.customer_master_exists(party_id, branch_id):
            raise ValueError(f"No customer_master record for {party_id}/{branch_id}")
        self.conn.execute(
            "UPDATE customer_master SET last_reconciled_on=?, last_reconciled_by=?"
            " WHERE party_id=? AND branch_id=?",
            (reconciled_on.isoformat(), reconciled_by, party_id, branch_id),
        )
        self.conn.commit()

    def record_pre_mis_adjustment(self, adj: PreMisAdjustment) -> None:
        """The only sanctioned way to move a party's Pre-MIS Outstanding
        baseline. Logs the adjustment and applies it to customer_master
        in the same transaction.
        """
        cur = self.conn.execute(
            "SELECT pre_mis_outstanding FROM customer_master WHERE party_id=? AND branch_id=?",
            (adj.party_id, adj.branch_id),
        ).fetchone()
        if cur is None:
            raise ValueError(f"No customer_master record for {adj.party_id}/{adj.branch_id}")
        new_balance = Decimal(cur[0]) + adj.amount
        self.conn.execute(
            "INSERT INTO pre_mis_adjustments (party_id, branch_id, amount, reason, adjusted_by, adjusted_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (adj.party_id, adj.branch_id, str(adj.amount), adj.reason, adj.adjusted_by, adj.adjusted_at.isoformat()),
        )
        self.conn.execute(
            "UPDATE customer_master SET pre_mis_outstanding=? WHERE party_id=? AND branch_id=?",
            (str(new_balance), adj.party_id, adj.branch_id),
        )
        self.conn.commit()

    def all_pre_mis_adjustments(self) -> list[PreMisAdjustment]:
        """Client's own later ask: the audited log record_pre_mis_adjustment
        already writes had nothing reading it back - a Maker could see
        today's Pre-MIS Outstanding figure but not how it got there.
        Oldest first, so a per-party running balance can be built up in
        order by the caller (ar_mis.pre_mis_register).
        """
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute(
            "SELECT * FROM pre_mis_adjustments ORDER BY adjusted_at ASC, adjustment_id ASC"
        )
        return [
            PreMisAdjustment(
                party_id=r["party_id"],
                branch_id=r["branch_id"],
                amount=Decimal(r["amount"]),
                reason=r["reason"],
                adjusted_by=r["adjusted_by"],
                adjusted_at=date.fromisoformat(r["adjusted_at"]),
            )
            for r in cur.fetchall()
        ]

    def customer_master_exists(self, party_id: str, branch_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM customer_master WHERE party_id=? AND branch_id=?", (party_id, branch_id)
        ).fetchone()
        return row is not None

    def has_tb_balance_on_record(self, party_id: str, branch_id: str) -> bool:
        """True once this party has ever been through a real extraction
        run - party_tb_balance only ever gets a row for a party the
        moment process_branch_data first runs for them, same timing
        guarantee the old weekly_snapshot-based check relied on.
        """
        row = self.conn.execute(
            "SELECT 1 FROM party_tb_balance WHERE party_id=? AND branch_id=?",
            (party_id, branch_id),
        ).fetchone()
        return row is not None

    def force_overwrite_pre_mis_outstanding(self, party_id: str, branch_id: str, new_amount: Decimal) -> None:
        """Corrects a bad *initial* Layer 1 load - before the weekly
        pipeline has ever actually run for this party. Raises once a
        real extraction has (has_tb_balance_on_record): at that point the
        only sanctioned path is record_pre_mis_adjustment() (an explicit,
        logged, reasoned adjustment), never a silent bulk overwrite of
        the seed. This is the one place besides record_pre_mis_adjustment
        that can move pre_mis_outstanding after creation, and it refuses
        to run once the field it protects has actually been relied upon.

        Moves original_pre_mis_outstanding together with pre_mis_
        outstanding - at this point in a party's life the two have never
        diverged (no adjustment has ever been possible, since none can
        be recorded before a real extraction exists either), so "the
        seed" and "the original seed" are still the same fact being
        corrected.
        """
        if self.has_tb_balance_on_record(party_id, branch_id):
            raise ValueError(
                f"{party_id}/{branch_id} has already been through a real extraction run - "
                "use record_pre_mis_adjustment() instead of overwriting the seed."
            )
        if not self.customer_master_exists(party_id, branch_id):
            raise ValueError(f"No customer_master record for {party_id}/{branch_id} to overwrite")
        self.conn.execute(
            "UPDATE customer_master SET pre_mis_outstanding=?, original_pre_mis_outstanding=?"
            " WHERE party_id=? AND branch_id=?",
            (str(new_amount), str(new_amount), party_id, branch_id),
        )
        self.conn.commit()

    def get_opening_balance(self, party_id: str, branch_id: str) -> Decimal:
        row = self.conn.execute(
            "SELECT pre_mis_outstanding FROM customer_master WHERE party_id=? AND branch_id=?",
            (party_id, branch_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"No customer_master record for {party_id}/{branch_id}")
        return Decimal(row[0])

    def get_original_pre_mis_outstanding(self, party_id: str, branch_id: str) -> Decimal:
        """Client's explicit call this session: TB Cross-Check's Opening
        must be exactly what was originally fed in, frozen forever - this
        is that figure, never touched by record_pre_mis_adjustment (see
        this column's own migration comment). get_opening_balance above
        is deliberately a separate method, not reused here: it reads the
        living, adjustment-updated figure AR Snapshot and the Pre-MIS
        Adjustment Register still correctly depend on, which is a
        different fact for a different purpose.
        """
        row = self.conn.execute(
            "SELECT original_pre_mis_outstanding FROM customer_master WHERE party_id=? AND branch_id=?",
            (party_id, branch_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"No customer_master record for {party_id}/{branch_id}")
        return Decimal(row[0])

    # ---- Layer 2: party_tb_balance (upsert, never appended) ---------
    # Replaces weekly_snapshot's actual job (client's explicit call this
    # session) - one current reading per party, overwritten every
    # extraction, never a growing per-week history. See PartyTBBalance's
    # own docstring in ar_mis.models for why no history is kept here.

    def upsert_party_tb_balance(self, party_id: str, branch_id: str, as_of: date, closing_extracted: Decimal) -> None:
        self.conn.execute(
            "INSERT INTO party_tb_balance (party_id, branch_id, as_of, closing_extracted) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(party_id, branch_id) DO UPDATE SET as_of=excluded.as_of,"
            " closing_extracted=excluded.closing_extracted",
            (party_id, branch_id, as_of.isoformat(), str(closing_extracted)),
        )
        self.conn.commit()

    def replace_party_tb_balances_for_branch(
        self, branch_id: str, as_of: date, closing_extracted: dict[str, Decimal]
    ) -> None:
        """Client's explicit decision this session: party_tb_balance stops
        being upsert-forever for a branch's own extraction runs. The old
        behavior left a party's TB reading frozen forever the moment
        Tally stopped reporting them as a Sundry Debtor at all (the
        client's own real case: a party reclassified to Sundry Creditor
        kept showing in TB Cross-Check, compared against a frozen, ever-
        more-stale reading, forever "mismatched" with nothing left to
        actually reconcile). `closing_extracted` is always a
        comprehensive read of Tally's current Sundry Debtors group -
        confirmed true at every real call site (live Test & Save
        Extraction and Catch-Up both call TallyClient.fetch_ytd_sundry_
        debtors with no party filter; Manual Upload parses a full
        Trial Balance export) - so replacing this branch's entire
        party_tb_balance table with exactly what this run's own pull
        reported is safe: a party still genuinely a Sundry Debtor
        reappears in every run's own comprehensive pull; a party that
        doesn't is no longer one, and dropping its stale reading along
        with it is correct, not data loss - the registers themselves
        (append-only, untouched by this) remain the permanent record of
        what actually happened while that party was tracked.

        Direct Store-level seeding for a single party outside a real
        extraction run (test fixtures, etc.) should keep using
        upsert_party_tb_balance instead - this method is specifically
        process_branch_data's own call, replacing its former per-party
        upsert loop.
        """
        self.conn.execute("DELETE FROM party_tb_balance WHERE branch_id=?", (branch_id,))
        self.conn.executemany(
            "INSERT INTO party_tb_balance (party_id, branch_id, as_of, closing_extracted) VALUES (?, ?, ?, ?)",
            [
                (party_id, branch_id, as_of.isoformat(), str(amount))
                for party_id, amount in closing_extracted.items()
            ],
        )
        self.conn.commit()

    def all_party_tb_balances(self, branch_id: str | None = None) -> dict[tuple[str, str], "PartyTBBalance"]:
        """Every party's current Tally reading, keyed (party_id,
        branch_id) - the live input to TB Cross-Check, AR Snapshot's
        Total AR, and Ageing Matrix's Tally-closing column alike. Always
        the single latest reading; there is nothing else to filter by a
        date, since nothing older is kept.
        """
        self.conn.row_factory = sqlite3.Row
        if branch_id is None:
            cur = self.conn.execute("SELECT * FROM party_tb_balance")
        else:
            cur = self.conn.execute("SELECT * FROM party_tb_balance WHERE branch_id=?", (branch_id,))
        return {
            (r["party_id"], r["branch_id"]): PartyTBBalance(
                party_id=r["party_id"], branch_id=r["branch_id"],
                as_of=date.fromisoformat(r["as_of"]), closing_extracted=Decimal(r["closing_extracted"]),
            )
            for r in cur.fetchall()
        }

    def has_extraction_for_branch_week(self, branch_id: str, week_ending: date) -> bool:
        """True if this branch/week already has a recorded extraction
        run, from either live extraction or a manual upload - the two
        converge on the same extraction_log table, so this is the one
        check that enforces "first one in wins, no silent overwrite"
        regardless of which path got there first. Replaces the old
        weekly_snapshot-based has_weekly_snapshot_for_branch_week:
        extraction_log already gets exactly one row per run regardless
        of how many parties it touched, so it's the more direct source,
        not just a substitute.
        """
        row = self.conn.execute(
            "SELECT 1 FROM extraction_log WHERE branch_id=? AND week_ending=? LIMIT 1",
            (branch_id, week_ending.isoformat()),
        ).fetchone()
        return row is not None

    def all_week_endings(self) -> list[date]:
        cur = self.conn.execute("SELECT DISTINCT week_ending FROM extraction_log ORDER BY week_ending")
        return [date.fromisoformat(row[0]) for row in cur.fetchall()]

    def week_endings_for_branch(self, branch_id: str) -> list[date]:
        """Every week_ending this branch has a recorded extraction run
        for, oldest first - the authoritative "what's already in the
        system" list Branch Master shows, and what delete_branch_week
        checks against to enforce its latest-week-only guard below.
        """
        cur = self.conn.execute(
            "SELECT DISTINCT week_ending FROM extraction_log WHERE branch_id=? ORDER BY week_ending",
            (branch_id,),
        )
        return [date.fromisoformat(row[0]) for row in cur.fetchall()]

    def branch_data_summary(self, branch_id: str) -> dict | None:
        """Coverage summary for the Branch Master screen: the earliest
        and latest date already on record for this branch, and how many
        separate runs make up that history - so an operator can see
        what's already been extracted before running another extraction,
        without having to open a register and hunt for it. None if
        nothing has been recorded for this branch yet.
        """
        row = self.conn.execute(
            "SELECT MIN(period_start), MAX(week_ending), COUNT(DISTINCT week_ending)"
            " FROM extraction_log WHERE branch_id=?",
            (branch_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return {
            "earliest_week_start": date.fromisoformat(row[0]),
            "latest_week_end": date.fromisoformat(row[1]),
            "week_count": row[2],
        }

    def delete_branch_week(self, branch_id: str, week_ending: date) -> None:
        """Deletes every row this system wrote for one branch's one
        extraction run - the client's explicit ask: a mistaken extraction
        currently can't be undone at all, so a genuine error (wrong
        company loaded, wrong range typed) is stuck in the system forever
        with no way to reextract cleanly.

        Deliberately restricted to the branch's OWN latest recorded run,
        not any arbitrary past one. Opening Balance and Closing (Workings)
        are no longer anything stored at all - ar_mis.rollforward.
        compute_party_closings_as_of recomputes both fresh from the
        registers every time anyone needs them, for any date, so there
        is no stale frozen figure to strand by deleting an earlier run.
        The real remaining risk is party_tb_balance, which IS a stored
        current reading (deliberately, see its own docstring): deleting
        an earlier run's reading out from under a later one already on
        record for the same party would be fine (the later reading would
        simply stand), but deleting the LATEST run must also roll
        party_tb_balance back - leaving it pointing at a now-deleted
        run's Tally figure would silently claim a "current" state that
        no longer has a supporting extraction behind it. Restricting
        this to the branch's own latest run keeps that rollback exact:
        every party_tb_balance row this run touched has as_of ==
        week_ending, by construction, so deleting rows with that exact
        (branch_id, as_of) match is never a guess.

        sales_dn_register/credit_note_register/receipt_journal_register
        carry no week_ending column of their own (see their own CREATE
        TABLE comments) - identity there is branch + voucher + party, not
        a batch/run id. Rows belonging to this run are found via
        voucher_log instead of a derived date range: voucher_log is
        itself reliably scoped by (branch_id, week_ending) with no such
        dependency, so reading back the exact (voucher_type,
        voucher_number, party_ledger_name) triples this run logged there
        - before that table's own rows are deleted below - gives an exact
        identity match rather than a guessed range. This replaces an
        earlier version that derived [period_start, week_ending] from
        this run's own weekly_snapshot row: if period_start was ever
        missing or wrong for a run (confirmed live - a real branch's
        September extraction), that BETWEEN match silently deleted zero
        register rows while the weekly_snapshot row still vanished
        cleanly, leaving orphaned register rows with nothing anywhere
        indicating it happened. Any invoice_follow_up row for an invoice
        being deleted goes with it - a human's PTP/next-action note about
        an invoice this system is about to forget has nothing left to
        attach to.

        Deliberately does NOT touch weekly_movement: design doc item 15's
        Weekly Movement Register is its own frozen, explicitly-recorded
        history (never silently recomputed - see its own CREATE TABLE
        comment), so a week already recorded there stays exactly as
        recorded; re-extracting after this delete does not retroactively
        fix it - re-record that sheet separately if it's now stale.
        """
        weeks = self.week_endings_for_branch(branch_id)
        if not weeks or week_ending != weeks[-1]:
            raise ValueError(
                f"Only the most recently recorded week for '{branch_id}' can be deleted - "
                "deleting an earlier run while a later one is still on record would leave "
                "that later run's party_tb_balance readings pointing at data that no "
                "longer supports them."
            )
        w_end_s = week_ending.isoformat()

        logged_vouchers = self.conn.execute(
            "SELECT voucher_type, voucher_number, party_ledger_name FROM voucher_log"
            " WHERE branch_id=? AND week_ending=?",
            (branch_id, w_end_s),
        ).fetchall()
        sales_dn_identities = {(vn, p) for vt, vn, p in logged_vouchers if vt in ("Sales", "Debit Note")}
        credit_note_identities = {(vn, p) for vt, vn, p in logged_vouchers if vt == "Credit Note"}
        receipt_journal_identities = {(vn, p) for vt, vn, p in logged_vouchers if vt in ("Receipt", "Journal")}

        for voucher_number, party_id in sales_dn_identities:
            self.conn.execute(
                "DELETE FROM invoice_follow_up WHERE branch_id=? AND voucher_number=? AND party_id=?",
                (branch_id, voucher_number, party_id),
            )
            self.conn.execute(
                "DELETE FROM sales_dn_register WHERE branch_id=? AND voucher_number=? AND party_id=?",
                (branch_id, voucher_number, party_id),
            )
        for voucher_number, party_id in credit_note_identities:
            self.conn.execute(
                "DELETE FROM credit_note_register WHERE branch_id=? AND voucher_number=? AND party_id=?",
                (branch_id, voucher_number, party_id),
            )
        for voucher_number, party_id in receipt_journal_identities:
            self.conn.execute(
                "DELETE FROM receipt_journal_register WHERE branch_id=? AND voucher_number=? AND party_id=?",
                (branch_id, voucher_number, party_id),
            )

        self.conn.execute(
            "DELETE FROM voucher_log WHERE branch_id=? AND week_ending=?", (branch_id, w_end_s)
        )
        self.conn.execute(
            "DELETE FROM extraction_log WHERE branch_id=? AND week_ending=?", (branch_id, w_end_s)
        )
        self.conn.execute(
            "DELETE FROM drift_finding WHERE branch_id=? AND attributed_week=?", (branch_id, w_end_s)
        )
        # Legacy cleanup only: no code writes weekly_snapshot any more,
        # so this is a no-op for any run saved after that change - kept
        # so deleting a pre-existing legacy row still works cleanly for
        # a database migrated from before it.
        self.conn.execute(
            "DELETE FROM weekly_snapshot WHERE branch_id=? AND week_ending=?", (branch_id, w_end_s)
        )
        # party_tb_balance rollback (see this method's own docstring):
        # every party this run touched has as_of == week_ending exactly,
        # since this is always the branch's own latest run.
        self.conn.execute(
            "DELETE FROM party_tb_balance WHERE branch_id=? AND as_of=?", (branch_id, w_end_s)
        )
        self.conn.commit()

    def all_customer_master_records(self) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute("SELECT * FROM customer_master ORDER BY party_name")
        return cur.fetchall()

    # ---- Layer 3: PTP register + status log (append-only) ---------

    def create_ptp_entry(self, entry: PTPEntry) -> None:
        self.conn.execute(
            "INSERT INTO ptp_register (ptp_id, party_id, branch_id, created_week,"
            " promised_amount, promised_date, owner, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.ptp_id,
                entry.party_id,
                entry.branch_id,
                entry.created_week.isoformat(),
                str(entry.promised_amount),
                entry.promised_date.isoformat(),
                entry.owner,
                entry.notes,
            ),
        )
        self.conn.commit()

    def all_ptp_entries(self) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute("SELECT * FROM ptp_register ORDER BY promised_date")
        return cur.fetchall()

    def log_ptp_status(self, log_row: PTPStatusLogRow) -> None:
        self.conn.execute(
            "INSERT INTO ptp_status_log (ptp_id, week_ending, status, updated_by, notes)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                log_row.ptp_id,
                log_row.week_ending.isoformat(),
                log_row.status.value,
                log_row.updated_by,
                log_row.notes,
            ),
        )
        self.conn.commit()

    # ---- Voucher log (append-only, supports 4.2 drift isolation) --

    def append_voucher_log_entry(
        self,
        branch_id: str,
        week_ending: date,
        party_ledger_name: str,
        voucher_type: str,
        voucher_number: str,
        voucher_date: date,
        flipped_amount: Decimal,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO voucher_log "
            "(branch_id, week_ending, party_ledger_name, voucher_type, voucher_number, voucher_date, flipped_amount)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                branch_id,
                week_ending.isoformat(),
                party_ledger_name,
                voucher_type,
                voucher_number,
                voucher_date.isoformat(),
                str(flipped_amount),
            ),
        )
        self.conn.commit()

    def logged_voucher_keys(self, branch_id: str, party_ledger_name: str) -> set[tuple[str, str, str, str]]:
        """Natural keys (voucher_type, voucher_number, voucher_date,
        flipped_amount) of every voucher line ever logged for this party/
        branch across all prior weekly runs - the ground truth a fresh
        YTD pull is diffed against to isolate drift.
        """
        cur = self.conn.execute(
            "SELECT voucher_type, voucher_number, voucher_date, flipped_amount FROM voucher_log"
            " WHERE branch_id=? AND party_ledger_name=?",
            (branch_id, party_ledger_name),
        )
        return {tuple(row) for row in cur.fetchall()}

    def current_ptp_status(self, ptp_id: str, as_of_week: date) -> PTPStatus | None:
        row = self.conn.execute(
            "SELECT status FROM ptp_status_log WHERE ptp_id=? AND week_ending<=?"
            " ORDER BY week_ending DESC, id DESC LIMIT 1",
            (ptp_id, as_of_week.isoformat()),
        ).fetchone()
        return PTPStatus(row[0]) if row else None

    # ---- Registers (append-only invoice-level, replace weekly_snapshot
    # as the base of reporting — docs/registers_and_reporting_design.md) --
    #
    # INSERT OR IGNORE against each table's UNIQUE constraint (see
    # MIGRATIONS[2]) is what keeps re-extraction of an overlapping date
    # range append-only-safe: a voucher already recorded is silently
    # skipped, never duplicated.

    def append_sales_dn_row(self, row: SalesDNRegisterRow) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO sales_dn_register"
            " (branch_id, invoice_date, note_type, voucher_number, bill_allocation_reference,"
            " party_id, taxable_value, cgst, sgst, igst, invoice_value, due_date, job_id, round_off)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.branch_id,
                row.invoice_date.isoformat(),
                row.note_type.value,
                row.voucher_number,
                row.bill_allocation_reference,
                row.party_id,
                str(row.taxable_value),
                str(row.cgst),
                str(row.sgst),
                str(row.igst),
                str(row.invoice_value),
                row.due_date.isoformat(),
                row.job_id,
                str(row.round_off),
            ),
        )
        self.conn.commit()

    def _sales_dn_row_from_row(self, r) -> SalesDNRegisterRow:
        # round_off is NULL for any row written before migration 20 (see
        # its own comment) - its true original value was never captured
        # anywhere to recover, so this reads back exactly what it always
        # displayed as: 0.00.
        round_off_raw = r["round_off"]
        return SalesDNRegisterRow(
            branch_id=r["branch_id"],
            invoice_date=date.fromisoformat(r["invoice_date"]),
            note_type=NoteType(r["note_type"]),
            voucher_number=r["voucher_number"],
            bill_allocation_reference=r["bill_allocation_reference"],
            party_id=r["party_id"],
            taxable_value=Decimal(r["taxable_value"]),
            cgst=Decimal(r["cgst"]),
            sgst=Decimal(r["sgst"]),
            igst=Decimal(r["igst"]),
            invoice_value=Decimal(r["invoice_value"]),
            due_date=date.fromisoformat(r["due_date"]),
            job_id=r["job_id"],
            round_off=Decimal(round_off_raw) if round_off_raw is not None else Decimal("0.00"),
            id=r["id"],
        )

    def all_sales_dn_rows(self, branch_id: str | None = None) -> list[SalesDNRegisterRow]:
        self.conn.row_factory = sqlite3.Row
        if branch_id is None:
            cur = self.conn.execute("SELECT * FROM sales_dn_register ORDER BY invoice_date")
        else:
            cur = self.conn.execute(
                "SELECT * FROM sales_dn_register WHERE branch_id=? ORDER BY invoice_date", (branch_id,)
            )
        return [self._sales_dn_row_from_row(r) for r in cur.fetchall()]

    def get_sales_dn_row_by_id(self, row_id: int) -> SalesDNRegisterRow | None:
        """A single row by its own real id - the Edit form's own target,
        needed to pre-fill each editable field with its current value and
        to diff a submission against (only a field that actually changed
        gets written - see edit_sales_dn_row)."""
        self.conn.row_factory = sqlite3.Row
        r = self.conn.execute("SELECT * FROM sales_dn_register WHERE id=?", (row_id,)).fetchone()
        return self._sales_dn_row_from_row(r) if r is not None else None

    def append_credit_note_row(self, row: CreditNoteRegisterRow) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO credit_note_register"
            " (branch_id, cn_date, voucher_number, party_id, cn_amount,"
            " bill_allocation_reference, classification, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.branch_id,
                row.cn_date.isoformat(),
                row.voucher_number,
                row.party_id,
                str(row.cn_amount),
                row.bill_allocation_reference,
                row.classification.value,
                row.reason,
            ),
        )
        self.conn.commit()

    def all_credit_note_rows(self, branch_id: str | None = None) -> list[CreditNoteRegisterRow]:
        self.conn.row_factory = sqlite3.Row
        if branch_id is None:
            cur = self.conn.execute("SELECT * FROM credit_note_register ORDER BY cn_date")
        else:
            cur = self.conn.execute(
                "SELECT * FROM credit_note_register WHERE branch_id=? ORDER BY cn_date", (branch_id,)
            )
        return [
            CreditNoteRegisterRow(
                branch_id=r["branch_id"],
                cn_date=date.fromisoformat(r["cn_date"]),
                voucher_number=r["voucher_number"],
                party_id=r["party_id"],
                cn_amount=Decimal(r["cn_amount"]),
                bill_allocation_reference=r["bill_allocation_reference"],
                classification=RegisterClassification(r["classification"]),
                reason=r["reason"],
                id=r["id"],
            )
            for r in cur.fetchall()
        ]

    def resolve_credit_note_classification(
        self, branch_id: str, voucher_number: str, party_id: str, classification: RegisterClassification, reason: str
    ) -> None:
        """Item 10's human resolution of a Pending Review reference — never
        called automatically. Confirming Pre-MIS Adjustment here is only
        the register-side classification flip; the caller is separately
        responsible for logging the actual balance correction via
        record_pre_mis_adjustment(), since that is the one sanctioned path
        to move a party's Pre-MIS Outstanding baseline.
        """
        cur = self.conn.execute(
            "UPDATE credit_note_register SET classification=?, reason=?"
            " WHERE branch_id=? AND voucher_number=? AND party_id=?",
            (classification.value, reason, branch_id, voucher_number, party_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"No credit_note_register row for {branch_id}/{voucher_number}/{party_id}")
        self.conn.commit()

    def append_ytd_debtor_voucher_rows(self, rows: Iterable[YtdDebtorVoucherRow]) -> None:
        for row in rows:
            self.conn.execute(
                "INSERT OR IGNORE INTO ytd_debtor_voucher"
                " (branch_id, as_of, voucher_date, voucher_type, raw_voucher_type_name,"
                " voucher_number, party_id, amount)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.branch_id,
                    row.as_of.isoformat(),
                    row.voucher_date.isoformat(),
                    row.voucher_type,
                    row.raw_voucher_type_name,
                    row.voucher_number,
                    row.party_id,
                    str(row.amount),
                ),
            )
        self.conn.commit()

    def all_ytd_debtor_voucher_rows(
        self, branch_id: str | None = None, as_of: date | None = None
    ) -> list[YtdDebtorVoucherRow]:
        self.conn.row_factory = sqlite3.Row
        clauses = []
        params: list[str] = []
        if branch_id is not None:
            clauses.append("branch_id=?")
            params.append(branch_id)
        if as_of is not None:
            clauses.append("as_of=?")
            params.append(as_of.isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cur = self.conn.execute(f"SELECT * FROM ytd_debtor_voucher{where} ORDER BY voucher_date", params)
        return [
            YtdDebtorVoucherRow(
                branch_id=r["branch_id"],
                as_of=date.fromisoformat(r["as_of"]),
                voucher_date=date.fromisoformat(r["voucher_date"]),
                voucher_type=r["voucher_type"],
                raw_voucher_type_name=r["raw_voucher_type_name"],
                voucher_number=r["voucher_number"],
                party_id=r["party_id"],
                amount=Decimal(r["amount"]),
            )
            for r in cur.fetchall()
        ]

    def append_receipt_journal_rows(self, rows: Iterable[ReceiptJournalRegisterRow]) -> None:
        for row in rows:
            self.conn.execute(
                "INSERT OR IGNORE INTO receipt_journal_register"
                " (branch_id, txn_date, voucher_type, voucher_number, party_id,"
                " amount, target_doc_no, classification, narration)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.branch_id,
                    row.txn_date.isoformat(),
                    row.voucher_type,
                    row.voucher_number,
                    row.party_id,
                    str(row.amount),
                    row.target_doc_no,
                    row.classification.value,
                    row.narration,
                ),
            )
        self.conn.commit()

    def all_receipt_journal_rows(self, branch_id: str | None = None) -> list[ReceiptJournalRegisterRow]:
        self.conn.row_factory = sqlite3.Row
        if branch_id is None:
            cur = self.conn.execute("SELECT * FROM receipt_journal_register ORDER BY txn_date")
        else:
            cur = self.conn.execute(
                "SELECT * FROM receipt_journal_register WHERE branch_id=? ORDER BY txn_date", (branch_id,)
            )
        return [
            ReceiptJournalRegisterRow(
                branch_id=r["branch_id"],
                txn_date=date.fromisoformat(r["txn_date"]),
                voucher_type=r["voucher_type"],
                voucher_number=r["voucher_number"],
                party_id=r["party_id"],
                amount=Decimal(r["amount"]),
                target_doc_no=r["target_doc_no"],
                classification=RegisterClassification(r["classification"]),
                narration=r["narration"],
                id=r["id"],
            )
            for r in cur.fetchall()
        ]

    def promote_receipt_journal_row_to_matched(self, row_id: int) -> None:
        """Auto-recheck's own row-level promotion (registers.find_pending_
        review_rows_now_resolvable), NOT a human's own Resolve action -
        touches ONLY the one row whose own reference now resolves to a
        tracked invoice. Unlike resolve_receipt_journal_classification
        below (which deliberately still updates the WHOLE voucher/party
        group, a human's own judgment call), two referenced lines of the
        same voucher can now resolve independently of each other (client's
        own live-testing catch), so sweeping a still-unresolved sibling
        into Matched here would be a brand new, worse bug.
        """
        cur = self.conn.execute(
            "UPDATE receipt_journal_register SET classification=? WHERE id=?",
            (RegisterClassification.CURRENT.value, row_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"No receipt_journal_register row with id={row_id}")
        self.conn.commit()

    def resolve_receipt_journal_classification(
        self, branch_id: str, voucher_number: str, party_id: str, classification: RegisterClassification
    ) -> None:
        """Item 10's voucher-level invariant: updates EVERY line of this
        voucher in one statement, never a single line in isolation — a
        split classification within one voucher would recreate the exact
        phantom-imbalance bug this design is meant to close (see
        ar_mis.registers.build_receipt_journal_register_rows). This is a
        human's own Resolve/Revert judgment call - the narrower, row-level
        promote_receipt_journal_row_to_matched above is for the automatic
        recheck only, which deliberately does NOT share this method's
        whole-group scope any more.
        """
        cur = self.conn.execute(
            "UPDATE receipt_journal_register SET classification=?"
            " WHERE branch_id=? AND voucher_number=? AND party_id=?",
            (classification.value, branch_id, voucher_number, party_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"No receipt_journal_register rows for {branch_id}/{voucher_number}/{party_id}")
        self.conn.commit()

    # ---- Register row Edit/Delete/Undo (client's explicit redesign this
    # session, replacing register_row_correction's overlay) - real
    # mutation of the actual register row, audited in register_row_audit.

    _SALES_DN_AUDIT_COLUMNS = [
        "branch_id", "invoice_date", "note_type", "voucher_number", "bill_allocation_reference",
        "party_id", "taxable_value", "cgst", "sgst", "igst", "invoice_value", "due_date", "job_id", "round_off",
    ]
    _CREDIT_NOTE_AUDIT_COLUMNS = [
        "branch_id", "cn_date", "voucher_number", "party_id", "cn_amount",
        "bill_allocation_reference", "classification", "reason",
    ]
    _RECEIPT_JOURNAL_AUDIT_COLUMNS = [
        "branch_id", "txn_date", "voucher_type", "voucher_number", "party_id",
        "amount", "target_doc_no", "classification", "narration",
    ]
    _AUDIT_TABLE_BY_REGISTER = {
        "sales_dn": "sales_dn_register", "credit_note": "credit_note_register", "receipt_journal": "receipt_journal_register",
    }
    _AUDIT_COLUMNS_BY_REGISTER = {
        "sales_dn": _SALES_DN_AUDIT_COLUMNS, "credit_note": _CREDIT_NOTE_AUDIT_COLUMNS, "receipt_journal": _RECEIPT_JOURNAL_AUDIT_COLUMNS,
    }

    # Sales & DN's own editable field whitelist (client's call: money/
    # derived fields plus Due Date and Job ID - never the identity fields
    # Voucher No./Party/Branch/Bill Allocation Reference/Invoice Date/
    # Note Type, since changing those has cascade implications - a CN or
    # Receipt already matched to this row's old bill reference going
    # silently stale - that edit_sales_dn_row doesn't handle).
    _SALES_DN_EDITABLE_COLUMNS = [
        "taxable_value", "cgst", "sgst", "igst", "round_off", "invoice_value", "due_date", "job_id",
    ]

    def _fetch_row_dict(self, table: str, row_id: int) -> dict | None:
        self.conn.row_factory = sqlite3.Row
        r = self.conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
        return dict(r) if r is not None else None

    def _record_register_audit(
        self, register: str, action: str, row_id: int, branch_id: str, voucher_number: str, party_id: str,
        before: list[dict] | None, after: list[dict] | None, reason: str, performed_by: str, performed_at: datetime,
        undone_of: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO register_row_audit (register, action, row_id, branch_id, voucher_number, party_id,"
            " before_json, after_json, reason, performed_by, performed_at, undone_of)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                register, action, row_id, branch_id, voucher_number, party_id,
                json.dumps(before) if before is not None else None,
                json.dumps(after) if after is not None else None,
                reason, performed_by, performed_at.isoformat(), undone_of,
            ),
        )
        return cur.lastrowid

    def edit_sales_dn_row(
        self, row_id: int, reason: str, edited_by: str, edited_at: datetime,
        **changes: Decimal | date | str | None,
    ) -> int:
        """Client's own later call (point 4/live-testing): editing only
        Invoice Value left the real problem - an extraction that wrongly
        captured only a discount line as the revenue item - impossible to
        actually fix, since the Taxable Value/GST breakdown stayed wrong
        underneath it even once the total was patched. `changes` is a
        dict of column -> new value, restricted to _SALES_DN_EDITABLE_
        COLUMNS, built by the caller as a diff against this row's own
        current values (so only a field the Maker actually changed is
        written - nothing silently reset by submitting an unmodified
        field alongside a changed one). Raises ValueError for an unknown
        column or an empty `changes` (nothing to update).
        """
        before = self._fetch_row_dict("sales_dn_register", row_id)
        if before is None:
            raise ValueError(f"No sales_dn_register row with id={row_id}")
        unknown = set(changes) - set(self._SALES_DN_EDITABLE_COLUMNS)
        if unknown:
            raise ValueError(f"Not an editable field: {', '.join(sorted(unknown))}")
        if not changes:
            raise ValueError("Nothing to update - every field matches its current value.")

        def _serialize(col: str, value) -> str | None:
            if col == "due_date":
                return value.isoformat()
            if col == "job_id":
                return value
            return str(value)

        serialized = {col: _serialize(col, value) for col, value in changes.items()}
        set_clause = ", ".join(f"{col}=?" for col in serialized)
        self.conn.execute(
            f"UPDATE sales_dn_register SET {set_clause} WHERE id=?", (*serialized.values(), row_id)
        )
        after = dict(before, **serialized)
        audit_id = self._record_register_audit(
            "sales_dn", "EDIT", row_id, before["branch_id"], before["voucher_number"], before["party_id"],
            [before], [after], reason, edited_by, edited_at,
        )
        self.conn.commit()
        return audit_id

    def delete_sales_dn_row(self, row_id: int, reason: str, deleted_by: str, deleted_at: datetime) -> int:
        """Deletes the real row. Cascades, same as Store.delete_branch_week
        already does for the same reasons: this invoice's own Invoice
        Follow-Up note goes with it (nothing left for a human's PTP/next-
        action note to attach to), and any Credit Note/Receipt/Journal
        that had matched this invoice's own bill_allocation_reference is
        unmatched (cleared back to on-account/unapplied) rather than left
        pointing at a row that no longer exists. Undo restores the
        invoice row itself; it does NOT restore the Follow-Up note or
        re-match whatever was unmatched - a deliberate limit, not an
        oversight, since reconstructing those exactly would need far more
        bookkeeping for a genuinely rare case (undoing a delete that also
        had cascade effects).
        """
        before = self._fetch_row_dict("sales_dn_register", row_id)
        if before is None:
            raise ValueError(f"No sales_dn_register row with id={row_id}")
        branch_id, voucher_number, party_id = before["branch_id"], before["voucher_number"], before["party_id"]
        bill_ref = before["bill_allocation_reference"]

        self.conn.execute(
            "DELETE FROM invoice_follow_up WHERE branch_id=? AND voucher_number=? AND party_id=?",
            (branch_id, voucher_number, party_id),
        )
        self.conn.execute(
            "UPDATE credit_note_register SET bill_allocation_reference=NULL"
            " WHERE branch_id=? AND party_id=? AND bill_allocation_reference=?",
            (branch_id, party_id, bill_ref),
        )
        self.conn.execute(
            "UPDATE receipt_journal_register SET target_doc_no=NULL"
            " WHERE branch_id=? AND party_id=? AND target_doc_no=?",
            (branch_id, party_id, bill_ref),
        )
        self.conn.execute("DELETE FROM sales_dn_register WHERE id=?", (row_id,))

        audit_id = self._record_register_audit(
            "sales_dn", "DELETE", row_id, branch_id, voucher_number, party_id,
            [before], None, reason, deleted_by, deleted_at,
        )
        self.conn.commit()
        return audit_id

    def edit_credit_note_row(
        self, row_id: int, new_cn_amount: Decimal, reason: str, edited_by: str, edited_at: datetime
    ) -> int:
        before = self._fetch_row_dict("credit_note_register", row_id)
        if before is None:
            raise ValueError(f"No credit_note_register row with id={row_id}")
        self.conn.execute("UPDATE credit_note_register SET cn_amount=? WHERE id=?", (str(new_cn_amount), row_id))
        after = dict(before, cn_amount=str(new_cn_amount))
        audit_id = self._record_register_audit(
            "credit_note", "EDIT", row_id, before["branch_id"], before["voucher_number"], before["party_id"],
            [before], [after], reason, edited_by, edited_at,
        )
        self.conn.commit()
        return audit_id

    def delete_credit_note_row(self, row_id: int, reason: str, deleted_by: str, deleted_at: datetime) -> int:
        before = self._fetch_row_dict("credit_note_register", row_id)
        if before is None:
            raise ValueError(f"No credit_note_register row with id={row_id}")
        self.conn.execute("DELETE FROM credit_note_register WHERE id=?", (row_id,))
        audit_id = self._record_register_audit(
            "credit_note", "DELETE", row_id, before["branch_id"], before["voucher_number"], before["party_id"],
            [before], None, reason, deleted_by, deleted_at,
        )
        self.conn.commit()
        return audit_id

    def edit_receipt_journal_row(
        self, row_id: int, new_amount: Decimal, reason: str, edited_by: str, edited_at: datetime, whole_voucher: bool
    ) -> int:
        """`whole_voucher` is the Maker's own explicit choice (client's
        call: never a hardcoded default) for a voucher spanning several
        bill-allocation lines. True collapses every line of the voucher
        into one, carrying `new_amount` - the first line's own txn_date/
        target_doc_no/classification/narration are kept, the rest
        dropped, same convention the old Correct Amount action always
        used. False edits only the one selected line.
        """
        anchor = self._fetch_row_dict("receipt_journal_register", row_id)
        if anchor is None:
            raise ValueError(f"No receipt_journal_register row with id={row_id}")
        branch_id, voucher_number, party_id = anchor["branch_id"], anchor["voucher_number"], anchor["party_id"]

        if whole_voucher:
            self.conn.row_factory = sqlite3.Row
            before = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT * FROM receipt_journal_register WHERE branch_id=? AND voucher_number=? AND party_id=?",
                    (branch_id, voucher_number, party_id),
                ).fetchall()
            ]
            self.conn.execute(
                "DELETE FROM receipt_journal_register WHERE branch_id=? AND voucher_number=? AND party_id=?",
                (branch_id, voucher_number, party_id),
            )
            new_row = dict(before[0], amount=str(new_amount))
            cols = self._RECEIPT_JOURNAL_AUDIT_COLUMNS
            self.conn.execute(
                f"INSERT INTO receipt_journal_register ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                tuple(new_row[c] for c in cols),
            )
            after = [new_row]
        else:
            self.conn.execute("UPDATE receipt_journal_register SET amount=? WHERE id=?", (str(new_amount), row_id))
            before = [anchor]
            after = [dict(anchor, amount=str(new_amount))]

        audit_id = self._record_register_audit(
            "receipt_journal", "EDIT", row_id, branch_id, voucher_number, party_id,
            before, after, reason, edited_by, edited_at,
        )
        self.conn.commit()
        return audit_id

    def delete_receipt_journal_row(
        self, row_id: int, reason: str, deleted_by: str, deleted_at: datetime, whole_voucher: bool
    ) -> int:
        anchor = self._fetch_row_dict("receipt_journal_register", row_id)
        if anchor is None:
            raise ValueError(f"No receipt_journal_register row with id={row_id}")
        branch_id, voucher_number, party_id = anchor["branch_id"], anchor["voucher_number"], anchor["party_id"]

        if whole_voucher:
            self.conn.row_factory = sqlite3.Row
            before = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT * FROM receipt_journal_register WHERE branch_id=? AND voucher_number=? AND party_id=?",
                    (branch_id, voucher_number, party_id),
                ).fetchall()
            ]
            self.conn.execute(
                "DELETE FROM receipt_journal_register WHERE branch_id=? AND voucher_number=? AND party_id=?",
                (branch_id, voucher_number, party_id),
            )
        else:
            before = [anchor]
            self.conn.execute("DELETE FROM receipt_journal_register WHERE id=?", (row_id,))

        audit_id = self._record_register_audit(
            "receipt_journal", "DELETE", row_id, branch_id, voucher_number, party_id,
            before, None, reason, deleted_by, deleted_at,
        )
        self.conn.commit()
        return audit_id

    def get_register_row_audit(self, audit_id: int) -> RegisterRowAuditEntry | None:
        self.conn.row_factory = sqlite3.Row
        r = self.conn.execute("SELECT * FROM register_row_audit WHERE id=?", (audit_id,)).fetchone()
        return self._register_row_audit_from_row(r) if r is not None else None

    def all_register_row_audit(self, branch_id: str | None = None) -> list[RegisterRowAuditEntry]:
        self.conn.row_factory = sqlite3.Row
        if branch_id is None:
            cur = self.conn.execute("SELECT * FROM register_row_audit ORDER BY performed_at DESC, id DESC")
        else:
            cur = self.conn.execute(
                "SELECT * FROM register_row_audit WHERE branch_id=? ORDER BY performed_at DESC, id DESC",
                (branch_id,),
            )
        return [self._register_row_audit_from_row(r) for r in cur.fetchall()]

    def _register_row_audit_from_row(self, r) -> RegisterRowAuditEntry:
        return RegisterRowAuditEntry(
            id=r["id"], register=r["register"], action=r["action"], row_id=r["row_id"],
            branch_id=r["branch_id"], voucher_number=r["voucher_number"], party_id=r["party_id"],
            before=json.loads(r["before_json"]) if r["before_json"] else None,
            after=json.loads(r["after_json"]) if r["after_json"] else None,
            reason=r["reason"], performed_by=r["performed_by"],
            performed_at=datetime.fromisoformat(r["performed_at"]), undone_of=r["undone_of"],
        )

    def undo_register_row_audit(self, audit_id: int, undone_by: str, undone_at: datetime) -> int:
        """Restores exactly what `audit_id`'s own `before` captured -
        always reverting to that specific action's own prior state, even
        if something else has since changed the row again (ordinary,
        expected "undo this one action" semantics, not "undo everything
        since"). Refuses to undo an UNDO (make a new correction instead,
        don't chase your own tail) or to undo the same entry twice.
        """
        entry = self.get_register_row_audit(audit_id)
        if entry is None:
            raise ValueError(f"No register_row_audit entry with id={audit_id}")
        if entry.action == "UNDO":
            raise ValueError("Cannot undo an Undo - make a new correction instead.")
        if self.conn.execute("SELECT 1 FROM register_row_audit WHERE undone_of=? LIMIT 1", (audit_id,)).fetchone():
            raise ValueError(f"Audit entry #{audit_id} has already been undone.")

        table = self._AUDIT_TABLE_BY_REGISTER[entry.register]
        cols = self._AUDIT_COLUMNS_BY_REGISTER[entry.register]

        if entry.action == "DELETE":
            for row in entry.before:
                self.conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                    tuple(row[c] for c in cols),
                )
        elif entry.action == "EDIT":
            if entry.register == "receipt_journal" and len(entry.before) > 1:
                # A whole-voucher edit collapsed several lines into one -
                # undo removes that single collapsed line and restores
                # every original line.
                self.conn.execute(
                    "DELETE FROM receipt_journal_register WHERE branch_id=? AND voucher_number=? AND party_id=?",
                    (entry.branch_id, entry.voucher_number, entry.party_id),
                )
                for row in entry.before:
                    self.conn.execute(
                        f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                        tuple(row[c] for c in cols),
                    )
            else:
                # Full-row restore (every audited column, not just the
                # one amount column) - needed now that edit_sales_dn_row
                # can change several fields in one action, and correct
                # unconditionally for the single-amount-field CN/Receipt/
                # Journal edits too (restoring the whole row is a strict
                # superset of restoring one column).
                restore_cols = [c for c in cols if c != "id"]
                for row in entry.before:
                    if self._fetch_row_dict(table, row["id"]) is None:
                        raise ValueError(
                            f"Cannot undo: {table} row {row['id']} no longer exists "
                            "(deleted or collapsed by a later action)."
                        )
                    set_clause = ", ".join(f"{c}=?" for c in restore_cols)
                    self.conn.execute(
                        f"UPDATE {table} SET {set_clause} WHERE id=?",
                        (*[row[c] for c in restore_cols], row["id"]),
                    )
        else:
            raise ValueError(f"Unknown audit action: {entry.action}")

        new_audit_id = self._record_register_audit(
            entry.register, "UNDO", entry.row_id, entry.branch_id, entry.voucher_number, entry.party_id,
            entry.after, entry.before, f"Undo of audit #{audit_id}: {entry.reason}", undone_by, undone_at,
            undone_of=audit_id,
        )
        self.conn.commit()
        return new_audit_id

    # ---- Extraction log (freshness indicator for Registers/Reports viewers) --

    def record_extraction_run(
        self, branch_id: str, week_ending: date, extracted_at: datetime, period_start: date | None = None
    ) -> None:
        """One row per successful (PASS-outcome) extraction run - the real
        wall-clock moment data was pulled, not `week_ending` (the business
        period a run covers - an operator can run a backlogged week's
        extraction well after that week actually ended). This is what a
        Registers/Reports viewer needs to judge the age of what they're
        looking at.

        `period_start` (added alongside party_tb_balance, replacing
        weekly_snapshot's own copy of the same fact) is this run's own
        start date - branch_data_summary's "earliest_week_start" needs
        it; nothing else does, now that register-row cleanup
        (Store.delete_branch_week) is keyed off voucher_log identities
        rather than a derived date range.
        """
        self.conn.execute(
            "INSERT INTO extraction_log (branch_id, week_ending, extracted_at, period_start) VALUES (?, ?, ?, ?)",
            (branch_id, week_ending.isoformat(), extracted_at.isoformat(),
             period_start.isoformat() if period_start else None),
        )
        self.conn.commit()

    def last_extraction_at(self, branch_id: str | None = None) -> datetime | None:
        """Most recent extraction_log timestamp - across every branch when
        `branch_id` is omitted (the Registers/Reports screens show data
        consolidated across branches, so their freshness indicator is the
        oldest-possible truth: "everything you see is at least this
        fresh"). None if nothing has ever been extracted successfully.
        """
        if branch_id is None:
            row = self.conn.execute("SELECT MAX(extracted_at) FROM extraction_log").fetchone()
        else:
            row = self.conn.execute(
                "SELECT MAX(extracted_at) FROM extraction_log WHERE branch_id=?", (branch_id,)
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    # ---- Weekly Movement Register (design doc item 15, append-only) ----

    def record_weekly_movement(self, row: WeeklyMovementRow) -> None:
        """Records one week's portfolio-wide KPI snapshot, once. The
        week_ending PRIMARY KEY means a second attempt for the same week
        raises sqlite3.IntegrityError rather than silently overwriting -
        append-only history is the entire point (see WeeklyMovementRow's
        docstring); a genuine correction is the caller's problem to solve
        some other way, not this method's to paper over.
        """
        self.conn.execute(
            "INSERT INTO weekly_movement (week_ending, total_ar, open_ar_by_fy_json,"
            " pre_mis_outstanding, overdue_ar, overdue_by_bucket_json, dso,"
            " collection_efficiency, unapplied_cash, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.week_ending.isoformat(),
                str(row.total_ar),
                json.dumps({k: str(v) for k, v in row.open_ar_by_fy.items()}),
                str(row.pre_mis_outstanding),
                str(row.overdue_ar),
                json.dumps({k: str(v) for k, v in row.overdue_by_bucket.items()}),
                str(row.dso) if row.dso is not None else None,
                str(row.collection_efficiency) if row.collection_efficiency is not None else None,
                str(row.unapplied_cash),
                row.recorded_at.isoformat(),
            ),
        )
        self.conn.commit()

    def has_weekly_movement_for_week(self, week_ending: date) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM weekly_movement WHERE week_ending=? LIMIT 1", (week_ending.isoformat(),)
        ).fetchone()
        return row is not None

    def all_weekly_movement_rows(self) -> list[WeeklyMovementRow]:
        """Every recorded week, oldest first - the Weekly Movement Register
        report reads this list top to bottom and computes week-over-week
        trends by comparing each row to the one before it.
        """
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute("SELECT * FROM weekly_movement ORDER BY week_ending ASC")
        rows = []
        for r in cur.fetchall():
            rows.append(
                WeeklyMovementRow(
                    week_ending=date.fromisoformat(r["week_ending"]),
                    recorded_at=datetime.fromisoformat(r["recorded_at"]),
                    total_ar=Decimal(r["total_ar"]),
                    open_ar_by_fy={k: Decimal(v) for k, v in json.loads(r["open_ar_by_fy_json"]).items()},
                    pre_mis_outstanding=Decimal(r["pre_mis_outstanding"]),
                    overdue_ar=Decimal(r["overdue_ar"]),
                    overdue_by_bucket={k: Decimal(v) for k, v in json.loads(r["overdue_by_bucket_json"]).items()},
                    dso=Decimal(r["dso"]) if r["dso"] is not None else None,
                    collection_efficiency=Decimal(r["collection_efficiency"]) if r["collection_efficiency"] is not None else None,
                    unapplied_cash=Decimal(r["unapplied_cash"]),
                )
            )
        return rows

    # ---- Drift findings (Section 4.2, backdated entries - persisted) --

    @staticmethod
    def _serialize_voucher(voucher: Voucher) -> str:
        return json.dumps(
            {
                "voucher_type": voucher.voucher_type.value,
                "voucher_date": voucher.voucher_date.isoformat(),
                "voucher_number": voucher.voucher_number,
                "branch_id": voucher.branch_id,
                "party_ledger_name": voucher.party_ledger_name,
                "raw_voucher_type_name": voucher.raw_voucher_type_name,
                "entries": [
                    {
                        "party_ledger_name": e.party_ledger_name,
                        "amount_as_extracted": str(e.amount_as_extracted),
                        "bill_name": e.bill_name,
                        "bill_type": e.bill_type,
                    }
                    for e in voucher.entries
                ],
            }
        )

    @staticmethod
    def _deserialize_voucher(raw: str) -> Voucher:
        d = json.loads(raw)
        return Voucher(
            voucher_type=VoucherType(d["voucher_type"]),
            voucher_date=date.fromisoformat(d["voucher_date"]),
            voucher_number=d["voucher_number"],
            branch_id=d["branch_id"],
            party_ledger_name=d["party_ledger_name"],
            raw_voucher_type_name=d.get("raw_voucher_type_name", ""),
            entries=[
                LedgerEntry(
                    party_ledger_name=e["party_ledger_name"],
                    amount_as_extracted=Decimal(e["amount_as_extracted"]),
                    bill_name=e.get("bill_name"),
                    bill_type=e.get("bill_type"),
                )
                for e in d["entries"]
            ],
        )

    def record_drift_findings(self, branch_id: str, findings: list[DriftFinding], discovered_at: datetime) -> int:
        """Persists each finding once - a finding already on record (same
        branch/party/voucher identity, enforced by the drift_finding
        table's UNIQUE constraint) is left untouched via INSERT OR IGNORE,
        since isolate_drift re-detects the same still-unresolved backdated
        voucher on every subsequent extraction until it's actually
        incorporated; recording it again every run would bury a genuinely
        new finding under duplicates of an old one. Returns how many of
        `findings` were genuinely new. The finding's full original
        voucher is stored too (see DriftFinding's own docstring) so it
        can later be replayed by ar_mis.drift_correction without a second
        live Tally pull.
        """
        new_count = 0
        for f in findings:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO drift_finding (branch_id, party_ledger_name, voucher_type,"
                " voucher_number, voucher_date, flipped_amount, attributed_week, discovered_at,"
                " acknowledged, voucher_json, incorporated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0)",
                (
                    branch_id, f.party_ledger_name, f.voucher_type, f.voucher_number,
                    f.voucher_date.isoformat(), str(f.flipped_amount),
                    f.attributed_week.isoformat() if f.attributed_week else None,
                    discovered_at.isoformat(), self._serialize_voucher(f.voucher),
                ),
            )
            if cur.rowcount:
                new_count += 1
        self.conn.commit()
        return new_count

    def _drift_finding_record_from_row(self, r: sqlite3.Row) -> DriftFindingRecord:
        return DriftFindingRecord(
            id=r["id"],
            branch_id=r["branch_id"],
            finding=DriftFinding(
                party_ledger_name=r["party_ledger_name"],
                voucher_type=r["voucher_type"],
                voucher_number=r["voucher_number"],
                voucher_date=date.fromisoformat(r["voucher_date"]),
                flipped_amount=Decimal(r["flipped_amount"]),
                attributed_week=date.fromisoformat(r["attributed_week"]) if r["attributed_week"] else None,
                voucher=self._deserialize_voucher(r["voucher_json"]),
            ),
            discovered_at=datetime.fromisoformat(r["discovered_at"]),
            acknowledged=bool(r["acknowledged"]),
            acknowledged_by=r["acknowledged_by"],
            acknowledged_at=datetime.fromisoformat(r["acknowledged_at"]) if r["acknowledged_at"] else None,
            incorporated=bool(r["incorporated"]),
            incorporated_by=r["incorporated_by"],
            incorporated_at=datetime.fromisoformat(r["incorporated_at"]) if r["incorporated_at"] else None,
            incorporated_week_ending=date.fromisoformat(r["incorporated_week_ending"]) if r["incorporated_week_ending"] else None,
        )

    def all_drift_findings(self) -> list[DriftFindingRecord]:
        """Every persisted finding, most recently discovered first."""
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute("SELECT * FROM drift_finding ORDER BY discovered_at DESC, id DESC")
        return [self._drift_finding_record_from_row(r) for r in cur.fetchall()]

    def get_drift_finding(self, finding_id: int) -> DriftFindingRecord | None:
        self.conn.row_factory = sqlite3.Row
        row = self.conn.execute("SELECT * FROM drift_finding WHERE id=?", (finding_id,)).fetchone()
        return self._drift_finding_record_from_row(row) if row else None

    def mark_drift_finding_incorporated(
        self, finding_id: int, incorporated_by: str, incorporated_at: datetime, week_ending: date | None = None
    ) -> None:
        """Records that a finding's voucher has been built into its
        register - see ar_mis.drift_correction.incorporate_drift_finding,
        the only caller. This flag says the missing voucher is now in the
        system; it does NOT claim the party now reconciles clean going
        forward - that's the TB Reconciliation Cross-Check sheet's own,
        separate signal.

        `week_ending` is a leftover column from this mechanism's earlier,
        weekly_snapshot-touching design (see drift_correction's own
        module docstring for why that was dropped) - always None from
        the current caller, kept nullable rather than dropped outright so
        a pre-existing row incorporated under the old design still reads
        back its own real correction week.
        """
        self.conn.execute(
            "UPDATE drift_finding SET incorporated=1, incorporated_by=?, incorporated_at=?,"
            " incorporated_week_ending=? WHERE id=?",
            (incorporated_by, incorporated_at.isoformat(), week_ending.isoformat() if week_ending else None, finding_id),
        )
        self.conn.commit()

    def append_register_build_exceptions(
        self, branch_id: str, week_ending: date, exceptions: list[tuple[str, str, str, str]], logged_at: datetime
    ) -> None:
        """Persists this run's own "vouchers not added to a register"
        exceptions (registers.RegisterBuildExceptions.unattributable_party
        - (voucher_number, party_ledger_name, voucher_type, reason)
        4-tuples) so they can be reviewed later instead of vanishing the
        moment the result page is closed. Called once per SAVE run (both
        live extraction and manual upload share process_branch_data, so
        both get this for free) - always a plain append, never checked
        against what's already on record, since a given voucher_number
        only ever falls inside the one run whose date range covers it.
        """
        for voucher_number, party_ledger_name, voucher_type, reason in exceptions:
            self.conn.execute(
                "INSERT INTO register_build_exception"
                " (branch_id, week_ending, voucher_number, party_ledger_name, voucher_type, reason, logged_at, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
                (branch_id, week_ending.isoformat(), voucher_number, party_ledger_name, voucher_type, reason, logged_at.isoformat()),
            )
        self.conn.commit()

    def _register_build_exception_from_row(self, r: sqlite3.Row) -> RegisterBuildExceptionRecord:
        return RegisterBuildExceptionRecord(
            id=r["id"],
            branch_id=r["branch_id"],
            week_ending=date.fromisoformat(r["week_ending"]),
            voucher_number=r["voucher_number"],
            party_ledger_name=r["party_ledger_name"],
            voucher_type=r["voucher_type"],
            reason=r["reason"],
            logged_at=datetime.fromisoformat(r["logged_at"]),
            status=r["status"],
            reviewed_by=r["reviewed_by"],
            reviewed_at=datetime.fromisoformat(r["reviewed_at"]) if r["reviewed_at"] else None,
            reviewed_note=r["reviewed_note"],
        )

    def all_register_build_exceptions(self) -> list[RegisterBuildExceptionRecord]:
        """Every persisted exception, open ones first (most actionable),
        most recently logged first within each group."""
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute(
            "SELECT * FROM register_build_exception ORDER BY (status != 'open'), logged_at DESC, id DESC"
        )
        return [self._register_build_exception_from_row(r) for r in cur.fetchall()]

    def review_register_build_exception(
        self, exception_id: int, status: str, reviewed_by: str, reviewed_at: datetime, reviewed_note: str
    ) -> None:
        """Records a human's disposition of one exception - an audit
        trail only (exactly like drift_finding's `acknowledged`), never
        itself touching weekly_snapshot or any register. `status` must be
        "reviewed_no_action" or "resolved_via_catchup" (never "open" -
        there is no un-reviewing an exception here, matching every other
        review marker in this system).
        """
        self.conn.execute(
            "UPDATE register_build_exception SET status=?, reviewed_by=?, reviewed_at=?, reviewed_note=?"
            " WHERE id=?",
            (status, reviewed_by, reviewed_at.isoformat(), reviewed_note, exception_id),
        )
        self.conn.commit()

    # ---- Invoice Follow-Up (item 7 — the one mutable/upserted register row) --

    def upsert_invoice_follow_up(self, follow_up: InvoiceFollowUp, today: date) -> None:
        """`logged_at` is computed here, never taken from `follow_up` as
        given - it must only move when the (ptp_date, ptp_amount) PROMISE
        itself changes, not on every edit (e.g. updating next_action
        alone must leave it untouched), since the PTP Kept Rate
        calculation (ar_mis.registers) depends on it marking exactly when
        the current promise started, not merely "row last touched". `today`
        is threaded in by the caller rather than read from the wall clock
        here, matching how every other as-of/point-in-time value in this
        codebase is passed explicitly rather than resolved internally.
        """
        existing = self.get_invoice_follow_up(follow_up.branch_id, follow_up.voucher_number, follow_up.party_id)
        promise_cleared = follow_up.ptp_date is None and follow_up.ptp_amount is None
        promise_changed = existing is None or (existing.ptp_date, existing.ptp_amount) != (
            follow_up.ptp_date,
            follow_up.ptp_amount,
        )
        if promise_cleared:
            logged_at = None
        elif promise_changed:
            logged_at = today
        else:
            logged_at = existing.logged_at

        self.conn.execute(
            "INSERT INTO invoice_follow_up"
            " (branch_id, voucher_number, party_id, ptp_date, ptp_amount,"
            " next_action, expected_collection_date, updated_by, logged_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(branch_id, voucher_number, party_id) DO UPDATE SET"
            " ptp_date=excluded.ptp_date, ptp_amount=excluded.ptp_amount,"
            " next_action=excluded.next_action,"
            " expected_collection_date=excluded.expected_collection_date,"
            " updated_by=excluded.updated_by,"
            " logged_at=excluded.logged_at,"
            " updated_at=excluded.updated_at",
            (
                follow_up.branch_id,
                follow_up.voucher_number,
                follow_up.party_id,
                follow_up.ptp_date.isoformat() if follow_up.ptp_date else None,
                str(follow_up.ptp_amount) if follow_up.ptp_amount is not None else None,
                follow_up.next_action,
                follow_up.expected_collection_date.isoformat() if follow_up.expected_collection_date else None,
                follow_up.updated_by,
                logged_at.isoformat() if logged_at else None,
                today.isoformat(),
            ),
        )
        self.conn.commit()

    def get_invoice_follow_up(self, branch_id: str, voucher_number: str, party_id: str) -> InvoiceFollowUp | None:
        self.conn.row_factory = sqlite3.Row
        r = self.conn.execute(
            "SELECT * FROM invoice_follow_up WHERE branch_id=? AND voucher_number=? AND party_id=?",
            (branch_id, voucher_number, party_id),
        ).fetchone()
        if r is None:
            return None
        return InvoiceFollowUp(
            branch_id=r["branch_id"],
            voucher_number=r["voucher_number"],
            party_id=r["party_id"],
            ptp_date=date.fromisoformat(r["ptp_date"]) if r["ptp_date"] else None,
            ptp_amount=Decimal(r["ptp_amount"]) if r["ptp_amount"] is not None else None,
            next_action=r["next_action"],
            expected_collection_date=(
                date.fromisoformat(r["expected_collection_date"]) if r["expected_collection_date"] else None
            ),
            updated_by=r["updated_by"],
            logged_at=date.fromisoformat(r["logged_at"]) if r["logged_at"] else None,
            updated_at=date.fromisoformat(r["updated_at"]) if r["updated_at"] else None,
        )

    def all_invoice_follow_ups(self, branch_id: str | None = None) -> list[InvoiceFollowUp]:
        """Every logged follow-up - the input compute_ptp_kept_rate needs
        (design doc item 13), rather than looking one invoice up at a
        time.
        """
        self.conn.row_factory = sqlite3.Row
        if branch_id is None:
            cur = self.conn.execute("SELECT * FROM invoice_follow_up")
        else:
            cur = self.conn.execute("SELECT * FROM invoice_follow_up WHERE branch_id=?", (branch_id,))
        return [
            InvoiceFollowUp(
                branch_id=r["branch_id"],
                voucher_number=r["voucher_number"],
                party_id=r["party_id"],
                ptp_date=date.fromisoformat(r["ptp_date"]) if r["ptp_date"] else None,
                ptp_amount=Decimal(r["ptp_amount"]) if r["ptp_amount"] is not None else None,
                next_action=r["next_action"],
                expected_collection_date=(
                    date.fromisoformat(r["expected_collection_date"]) if r["expected_collection_date"] else None
                ),
                updated_by=r["updated_by"],
                logged_at=date.fromisoformat(r["logged_at"]) if r["logged_at"] else None,
                updated_at=date.fromisoformat(r["updated_at"]) if r["updated_at"] else None,
            )
            for r in cur.fetchall()
        ]

    # ---- FY rollover snapshot (item 4 — append-only, deliberate-action only) --

    def append_fy_rollover_snapshot(self, snapshot: FYRolloverSnapshot) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO fy_rollover_snapshot"
            " (branch_id, voucher_number, party_id, from_financial_year, to_financial_year,"
            " open_amount_at_rollover, rolled_over_by, rolled_over_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.branch_id,
                snapshot.voucher_number,
                snapshot.party_id,
                snapshot.from_financial_year,
                snapshot.to_financial_year,
                str(snapshot.open_amount_at_rollover),
                snapshot.rolled_over_by,
                snapshot.rolled_over_at.isoformat(),
            ),
        )
        self.conn.commit()

    def fy_rollover_snapshots_for_invoice(
        self, branch_id: str, voucher_number: str, party_id: str
    ) -> list[FYRolloverSnapshot]:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute(
            "SELECT * FROM fy_rollover_snapshot WHERE branch_id=? AND voucher_number=? AND party_id=?"
            " ORDER BY to_financial_year",
            (branch_id, voucher_number, party_id),
        )
        return [
            FYRolloverSnapshot(
                branch_id=r["branch_id"],
                voucher_number=r["voucher_number"],
                party_id=r["party_id"],
                from_financial_year=r["from_financial_year"],
                to_financial_year=r["to_financial_year"],
                open_amount_at_rollover=Decimal(r["open_amount_at_rollover"]),
                rolled_over_by=r["rolled_over_by"],
                rolled_over_at=date.fromisoformat(r["rolled_over_at"]),
            )
            for r in cur.fetchall()
        ]
