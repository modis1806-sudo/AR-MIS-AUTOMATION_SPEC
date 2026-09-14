"""One-time Layer 1 seeding: loads each party's Pre-MIS Outstanding
baseline (Section 2.5) from a CSV prepared before this pipeline goes
live for a branch.

This is the ONLY bulk-load path for that baseline, and it is guarded to
match Section 2.5's "never re-pulled or recalculated weekly, must not
move except through explicit, logged adjustment":

  - A party not yet in customer_master: loaded as given.
  - A party already loaded with the SAME balance: harmless no-op refresh
    of party_name only (the normal case when re-running this script
    against an updated master list).
  - A party already loaded with a DIFFERENT balance, and the weekly
    pipeline has already produced at least one snapshot for it: refused
    outright, `--force` included. The only path from here is
    Store.record_pre_mis_adjustment() - an explicit, logged, reasoned
    adjustment, never a silent bulk overwrite.
  - A party already loaded with a DIFFERENT balance, but no weekly
    snapshot exists yet (this is still the pre-launch load, someone
    just made a mistake the first time): requires `--force` to correct.
    Without it, the row is skipped and reported so it can be reviewed.

CSV format (header row required):
    party_id,party_name,branch_id,pre_mis_outstanding

Amounts must already be in this pipeline's own sign convention (Section
2.3: Dr positive / Cr negative) - the same convention every other figure
in the system uses, not Tally's raw signed export. A debtor with a
genuine outstanding receivable is a positive number here.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ar_mis.models import CustomerMasterRecord
from ar_mis.money import to_money
from ar_mis.storage import Store

REQUIRED_COLUMNS = {"party_id", "party_name", "branch_id", "pre_mis_outstanding"}
BRANCH_SCOPED_REQUIRED_COLUMNS = {"party_id", "party_name", "pre_mis_outstanding"}
BRANCH_SCOPED_CSV_TEMPLATE = (
    "party_id,party_name,pre_mis_outstanding\n"
    "ACME001,Acme Traders,125000.00\n"
    "GLOBAL002,Global Enterprises,-5000.00\n"
)


@dataclass(frozen=True)
class SeedRowError:
    line_number: int
    reason: str


def _parse_seed_rows(reader: csv.DictReader, required_columns: set[str], branch_id: str | None):
    records: list[CustomerMasterRecord] = []
    errors: list[SeedRowError] = []
    missing = required_columns - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"CSV is missing required column(s): {', '.join(sorted(missing))}")
    for line_number, row in enumerate(reader, start=2):  # header is line 1
        party_id = (row.get("party_id") or "").strip()
        party_name = (row.get("party_name") or "").strip()
        row_branch_id = branch_id if branch_id is not None else (row.get("branch_id") or "").strip()
        raw_amount = (row.get("pre_mis_outstanding") or "").strip()
        if not party_id or not party_name or not row_branch_id:
            errors.append(
                SeedRowError(line_number, "party_id, party_name and branch_id are all required")
            )
            continue
        try:
            amount = to_money(Decimal(raw_amount))
        except InvalidOperation:
            errors.append(SeedRowError(line_number, f"'{raw_amount}' is not a valid decimal amount"))
            continue
        records.append(CustomerMasterRecord(party_id, party_name, row_branch_id, amount))
    return records, errors


def parse_seed_csv(path: str) -> tuple[list[CustomerMasterRecord], list[SeedRowError]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return _parse_seed_rows(csv.DictReader(f), REQUIRED_COLUMNS, branch_id=None)


def parse_seed_rows_for_branch(csv_text: str, branch_id: str) -> tuple[list[CustomerMasterRecord], list[SeedRowError]]:
    """Same Layer 1 seeding as parse_seed_csv, scoped to one already-known
    branch - the Branch Master webapp's own Pre-MIS Outstanding upload,
    where every row obviously belongs to the branch just created or being
    edited, so there's no reason to make an operator retype that branch_id
    on every single row (unlike the CLI tool above, which can seed
    multiple branches from one shared file). Takes CSV TEXT rather than a
    path, since this is an uploaded file's already-decoded content, not
    something living on disk.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    return _parse_seed_rows(reader, BRANCH_SCOPED_REQUIRED_COLUMNS, branch_id=branch_id)


@dataclass
class SeedSummary:
    loaded: int = 0
    unchanged: int = 0
    overwritten: int = 0
    skipped: list[str] = None

    def __post_init__(self):
        if self.skipped is None:
            self.skipped = []


def seed(
    store: Store,
    records: list[CustomerMasterRecord],
    force: bool = False,
    dry_run: bool = False,
    announce: Callable[[str], None] = print,
) -> SeedSummary:
    summary = SeedSummary()

    for record in records:
        key = f"{record.party_id}/{record.branch_id}"

        if not store.customer_master_exists(record.party_id, record.branch_id):
            announce(f"LOAD: {key} = {record.pre_mis_outstanding}")
            if not dry_run:
                store.upsert_customer_master(record)
            summary.loaded += 1
            continue

        current = store.get_opening_balance(record.party_id, record.branch_id)
        if current == to_money(record.pre_mis_outstanding):
            if not dry_run:
                store.upsert_customer_master(record)  # harmless: refreshes party_name only
            summary.unchanged += 1
            continue

        if store.has_weekly_snapshots(record.party_id, record.branch_id):
            reason = (
                f"{key}: on record as {current}, CSV has {record.pre_mis_outstanding}, and the "
                "weekly pipeline has already produced snapshot(s) for this party - refusing to "
                "overwrite even with --force. Use Store.record_pre_mis_adjustment() for a logged "
                "correction instead."
            )
            announce(f"SKIP: {reason}")
            summary.skipped.append(reason)
            continue

        if not force:
            reason = (
                f"{key}: on record as {current}, CSV has {record.pre_mis_outstanding}. No weekly "
                "data exists yet, so this is safe to correct - re-run with --force to overwrite."
            )
            announce(f"SKIP: {reason}")
            summary.skipped.append(reason)
            continue

        announce(f"OVERWRITE (--force): {key}: {current} -> {record.pre_mis_outstanding}")
        if not dry_run:
            store.force_overwrite_pre_mis_outstanding(
                record.party_id, record.branch_id, record.pre_mis_outstanding
            )
        summary.overwritten += 1

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-time Layer 1 Pre-MIS Outstanding seeding")
    parser.add_argument("csv_path", help="CSV with columns: party_id,party_name,branch_id,pre_mis_outstanding")
    parser.add_argument("--db-path", default="data/ar_mis.db")
    parser.add_argument("--force", action="store_true", help="Allow correcting a bad pre-launch load")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen; write nothing")
    args = parser.parse_args(argv)

    try:
        records, errors = parse_seed_csv(args.csv_path)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1

    if errors:
        print(f"{len(errors)} row(s) rejected before touching the database:")
        for err in errors:
            print(f"  line {err.line_number}: {err.reason}")
        return 1

    store = Store(args.db_path)
    try:
        summary = seed(store, records, force=args.force, dry_run=args.dry_run)
    finally:
        store.close()

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(
        f"{prefix}Loaded {summary.loaded}, unchanged {summary.unchanged}, "
        f"overwritten {summary.overwritten}, skipped {len(summary.skipped)}"
    )
    return 2 if summary.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
