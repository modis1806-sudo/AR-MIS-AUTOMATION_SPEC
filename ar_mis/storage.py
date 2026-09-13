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

import sqlite3
from collections.abc import Iterable
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ar_mis.config import BranchConfig
from ar_mis.models import (
    CreditNoteRegisterRow,
    CustomerMasterRecord,
    FYRolloverSnapshot,
    InvoiceFollowUp,
    NoteType,
    PartyGrouping,
    PreMisAdjustment,
    PTPEntry,
    PTPStatus,
    PTPStatusLogRow,
    ReceiptJournalRegisterRow,
    RegisterClassification,
    SalesDNRegisterRow,
    WeeklySnapshotRow,
)

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
SCHEMA_VERSION = 5

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
        for version in range(current_version + 1, SCHEMA_VERSION + 1):
            self.conn.executescript(MIGRATIONS[version])
            self.conn.execute(f"PRAGMA user_version = {version}")
        self.conn.commit()

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

    # ---- Layer 1: customer master ---------------------------------

    def upsert_customer_master(self, record: CustomerMasterRecord) -> None:
        """Create-or-refresh party reference data. Explicitly does NOT
        touch pre_mis_outstanding, grouping, or credit_period_days for a
        party that already exists — those fields only move via
        record_pre_mis_adjustment, set_party_grouping, and
        set_credit_period_days respectively (item 7's editable-fields
        list). Safe to call every week for master-data sync (name
        changes etc.) without risking any of the fields that must never
        move through this path. A brand-new party gets `record`'s own
        grouping/credit_period_days (None/30 by default) as its starting
        point.
        """
        existing = self.conn.execute(
            "SELECT pre_mis_outstanding FROM customer_master WHERE party_id=? AND branch_id=?",
            (record.party_id, record.branch_id),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO customer_master"
                " (party_id, branch_id, party_name, pre_mis_outstanding, grouping, credit_period_days)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.party_id,
                    record.branch_id,
                    record.party_name,
                    str(record.pre_mis_outstanding),
                    record.grouping.value if record.grouping else None,
                    record.credit_period_days,
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

    def customer_master_exists(self, party_id: str, branch_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM customer_master WHERE party_id=? AND branch_id=?", (party_id, branch_id)
        ).fetchone()
        return row is not None

    def has_weekly_snapshots(self, party_id: str, branch_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM weekly_snapshot WHERE party_id=? AND branch_id=? LIMIT 1",
            (party_id, branch_id),
        ).fetchone()
        return row is not None

    def force_overwrite_pre_mis_outstanding(self, party_id: str, branch_id: str, new_amount: Decimal) -> None:
        """Corrects a bad *initial* Layer 1 load - before the weekly
        pipeline has ever produced a snapshot for this party. Raises if
        any weekly_snapshot row already exists: at that point the only
        sanctioned path is record_pre_mis_adjustment() (an explicit,
        logged, reasoned adjustment), never a silent bulk overwrite of
        the seed. This is the one place besides record_pre_mis_adjustment
        that can move pre_mis_outstanding after creation, and it refuses
        to run once the field it protects has actually been relied upon.
        """
        if self.has_weekly_snapshots(party_id, branch_id):
            raise ValueError(
                f"{party_id}/{branch_id} already has weekly snapshot rows on record - "
                "use record_pre_mis_adjustment() instead of overwriting the seed."
            )
        if not self.customer_master_exists(party_id, branch_id):
            raise ValueError(f"No customer_master record for {party_id}/{branch_id} to overwrite")
        self.conn.execute(
            "UPDATE customer_master SET pre_mis_outstanding=? WHERE party_id=? AND branch_id=?",
            (str(new_amount), party_id, branch_id),
        )
        self.conn.commit()

    def get_latest_closing(self, party_id: str, branch_id: str) -> Decimal | None:
        """Most recent weekly_snapshot.closing_computed for this party/
        branch, or None if no prior week exists yet. This - not
        pre_mis_outstanding - is the correct Opening Balance for every
        week after the first: Section 2.5 fixes the Pre-MIS Outstanding
        baseline itself against weekly recalculation, it does not mean
        every week's roll-forward re-seeds from that one figure. Week 1
        opens from Pre-MIS Outstanding; every week after opens from the
        prior week's computed closing, exactly like a standard roll-
        forward bridge.
        """
        row = self.conn.execute(
            "SELECT closing_computed FROM weekly_snapshot WHERE party_id=? AND branch_id=?"
            " ORDER BY week_ending DESC LIMIT 1",
            (party_id, branch_id),
        ).fetchone()
        return Decimal(row[0]) if row else None

    def get_opening_balance(self, party_id: str, branch_id: str) -> Decimal:
        row = self.conn.execute(
            "SELECT pre_mis_outstanding FROM customer_master WHERE party_id=? AND branch_id=?",
            (party_id, branch_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"No customer_master record for {party_id}/{branch_id}")
        return Decimal(row[0])

    # ---- Layer 2: weekly snapshot (append-only) --------------------

    def append_weekly_snapshot(self, row: WeeklySnapshotRow) -> None:
        d = asdict(row)
        self.conn.execute(
            "INSERT INTO weekly_snapshot "
            "(party_id, branch_id, week_ending, opening, sales, credit_notes, debit_notes,"
            " receipts, journals, closing_computed, closing_extracted, reconciled, difference)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                d["party_id"],
                d["branch_id"],
                d["week_ending"].isoformat(),
                str(d["opening"]),
                str(d["sales"]),
                str(d["credit_notes"]),
                str(d["debit_notes"]),
                str(d["receipts"]),
                str(d["journals"]),
                str(d["closing_computed"]),
                str(d["closing_extracted"]),
                int(d["reconciled"]),
                str(d["difference"]),
            ),
        )
        self.conn.commit()

    def weekly_snapshots_for_week(self, week_ending: date) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute(
            "SELECT * FROM weekly_snapshot WHERE week_ending=?", (week_ending.isoformat(),)
        )
        return cur.fetchall()

    def append_weekly_snapshots(self, rows: Iterable[WeeklySnapshotRow]) -> None:
        for row in rows:
            self.append_weekly_snapshot(row)

    def has_weekly_snapshot_for_branch_week(self, branch_id: str, week_ending: date) -> bool:
        """True if this branch/week already has any recorded data, from
        either live extraction or a manual upload - the two converge on
        the same append-only weekly_snapshot table, so this is the one
        check that enforces "first one in wins, no silent overwrite"
        regardless of which path got there first.
        """
        row = self.conn.execute(
            "SELECT 1 FROM weekly_snapshot WHERE branch_id=? AND week_ending=? LIMIT 1",
            (branch_id, week_ending.isoformat()),
        ).fetchone()
        return row is not None

    def all_week_endings(self) -> list[date]:
        cur = self.conn.execute("SELECT DISTINCT week_ending FROM weekly_snapshot ORDER BY week_ending")
        return [date.fromisoformat(row[0]) for row in cur.fetchall()]

    def latest_weekly_snapshot_closing_total(self) -> Decimal | None:
        """Summed closing_computed across every party for the most recent
        week_ending on record - the AR Snapshot dashboard's Rounding
        Difference baseline (ar_mis.dashboard). None when no weekly_snapshot
        rows exist yet, not zero - there's nothing to compare against.
        """
        week_endings = self.all_week_endings()
        if not week_endings:
            return None
        latest = week_endings[-1]
        rows = self.weekly_snapshots_for_week(latest)
        return sum((Decimal(r["closing_computed"]) for r in rows), Decimal("0.00"))

    def weekly_snapshots_for_party(self, party_id: str, branch_id: str) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute(
            "SELECT * FROM weekly_snapshot WHERE party_id=? AND branch_id=? ORDER BY week_ending",
            (party_id, branch_id),
        )
        return cur.fetchall()

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
            " party_id, taxable_value, cgst, sgst, igst, invoice_value, due_date, job_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        self.conn.commit()

    def all_sales_dn_rows(self, branch_id: str | None = None) -> list[SalesDNRegisterRow]:
        self.conn.row_factory = sqlite3.Row
        if branch_id is None:
            cur = self.conn.execute("SELECT * FROM sales_dn_register ORDER BY invoice_date")
        else:
            cur = self.conn.execute(
                "SELECT * FROM sales_dn_register WHERE branch_id=? ORDER BY invoice_date", (branch_id,)
            )
        return [
            SalesDNRegisterRow(
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
            )
            for r in cur.fetchall()
        ]

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
            )
            for r in cur.fetchall()
        ]

    def resolve_receipt_journal_classification(
        self, branch_id: str, voucher_number: str, party_id: str, classification: RegisterClassification
    ) -> None:
        """Item 10's voucher-level invariant: updates EVERY line of this
        voucher in one statement, never a single line in isolation — a
        split classification within one voucher would recreate the exact
        phantom-imbalance bug this design is meant to close (see
        ar_mis.registers.build_receipt_journal_register_rows).
        """
        cur = self.conn.execute(
            "UPDATE receipt_journal_register SET classification=?"
            " WHERE branch_id=? AND voucher_number=? AND party_id=?",
            (classification.value, branch_id, voucher_number, party_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"No receipt_journal_register rows for {branch_id}/{voucher_number}/{party_id}")
        self.conn.commit()

    # ---- Extraction log (freshness indicator for Registers/Reports viewers) --

    def record_extraction_run(self, branch_id: str, week_ending: date, extracted_at: datetime) -> None:
        """One row per successful (PASS-outcome) extraction run - the real
        wall-clock moment data was pulled, not `week_ending` (the business
        period a run covers - an operator can run a backlogged week's
        extraction well after that week actually ended). This is what a
        Registers/Reports viewer needs to judge the age of what they're
        looking at.
        """
        self.conn.execute(
            "INSERT INTO extraction_log (branch_id, week_ending, extracted_at) VALUES (?, ?, ?)",
            (branch_id, week_ending.isoformat(), extracted_at.isoformat()),
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
            " next_action, expected_collection_date, updated_by, logged_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(branch_id, voucher_number, party_id) DO UPDATE SET"
            " ptp_date=excluded.ptp_date, ptp_amount=excluded.ptp_amount,"
            " next_action=excluded.next_action,"
            " expected_collection_date=excluded.expected_collection_date,"
            " updated_by=excluded.updated_by,"
            " logged_at=excluded.logged_at",
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
