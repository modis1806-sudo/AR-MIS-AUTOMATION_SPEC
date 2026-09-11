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

from ar_mis.models import (
    CustomerMasterRecord,
    PreMisAdjustment,
    PTPEntry,
    PTPStatus,
    PTPStatusLogRow,
    WeeklySnapshotRow,
)

SCHEMA = """
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
"""


class Store:
    def __init__(self, db_path: str):
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

    def current_ptp_status(self, ptp_id: str, as_of_week: date) -> PTPStatus | None:
        row = self.conn.execute(
            "SELECT status FROM ptp_status_log WHERE ptp_id=? AND week_ending<=?"
            " ORDER BY week_ending DESC, id DESC LIMIT 1",
            (ptp_id, as_of_week.isoformat()),
        ).fetchone()
        return PTPStatus(row[0]) if row else None
