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
from datetime import date
from decimal import Decimal
from pathlib import Path

from ar_mis.config import BranchConfig
from ar_mis.models import (
    CustomerMasterRecord,
    PreMisAdjustment,
    PTPEntry,
    PTPStatus,
    PTPStatusLogRow,
    WeeklySnapshotRow,
)

SCHEMA = """
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
"""


class Store:
    def __init__(self, db_path: str):
        # sqlite3.connect() fails with "unable to open database file" if
        # the parent directory doesn't exist yet - true for a fresh
        # checkout, since an empty `data/` dir can't be committed to git.
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

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
        touch pre_mis_outstanding for a party that already exists — that
        field only moves via record_pre_mis_adjustment. Safe to call every
        week for master-data sync (name changes etc.) without risking the
        one field that must never move through this path.
        """
        existing = self.conn.execute(
            "SELECT pre_mis_outstanding FROM customer_master WHERE party_id=? AND branch_id=?",
            (record.party_id, record.branch_id),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO customer_master (party_id, branch_id, party_name, pre_mis_outstanding)"
                " VALUES (?, ?, ?, ?)",
                (record.party_id, record.branch_id, record.party_name, str(record.pre_mis_outstanding)),
            )
        else:
            self.conn.execute(
                "UPDATE customer_master SET party_name=? WHERE party_id=? AND branch_id=?",
                (record.party_name, record.party_id, record.branch_id),
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

    def all_week_endings(self) -> list[date]:
        cur = self.conn.execute("SELECT DISTINCT week_ending FROM weekly_snapshot ORDER BY week_ending")
        return [date.fromisoformat(row[0]) for row in cur.fetchall()]

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
