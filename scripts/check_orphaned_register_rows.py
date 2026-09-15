"""Finds register rows (sales_dn_register/credit_note_register/
receipt_journal_register) that fall outside their branch's own recorded
weekly_snapshot history - the exact class of orphan a silent
delete_branch_week failure (fixed) or a branch deleted without its
history being cleaned up first (Branch Master's "Delete", still
unfixed - see docs/ar_controls_tracker.md) can leave behind.

Two checks per branch:
  - Always run: nothing should be dated after that branch's latest
    recorded week_ending - a real invoice can't belong to a week that
    hasn't happened yet in this branch's history.
  - Only run when every one of that branch's weeks has a real
    period_start on record: nothing should be dated before the
    earliest one. Skipped otherwise, rather than risk a false positive
    from a legacy run with no period_start to compare against.

Run from the project root: python scripts/check_orphaned_register_rows.py
Optionally pass a different db path as the one argument.
"""
import sqlite3
import sys

db_path = sys.argv[1] if len(sys.argv) > 1 else "data/ar_mis.db"
conn = sqlite3.connect(db_path)

branch_info = {
    row[0]: (row[1], row[2], row[3])
    for row in conn.execute(
        "SELECT branch_id, MIN(period_start), MAX(week_ending),"
        " SUM(CASE WHEN period_start IS NULL THEN 1 ELSE 0 END) AS null_starts"
        " FROM weekly_snapshot GROUP BY branch_id"
    ).fetchall()
}

tables = [
    ("sales_dn_register", "invoice_date"),
    ("credit_note_register", "cn_date"),
    ("receipt_journal_register", "txn_date"),
]

found_any = False
for table, date_col in tables:
    branches_in_table = [r[0] for r in conn.execute(f"SELECT DISTINCT branch_id FROM {table}").fetchall()]
    for branch_id in branches_in_table:
        if branch_id not in branch_info:
            count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE branch_id=?", (branch_id,)).fetchone()[0]
            print(f"MISMATCH: {table} has {count} row(s) for branch '{branch_id}', which has no weekly_snapshot at all")
            found_any = True
            continue

        earliest, latest, null_starts = branch_info[branch_id]

        count, lo, hi = conn.execute(
            f"SELECT COUNT(*), MIN({date_col}), MAX({date_col}) FROM {table} WHERE branch_id=? AND {date_col} > ?",
            (branch_id, latest),
        ).fetchone()
        if count:
            print(
                f"MISMATCH: branch '{branch_id}', {table} has {count} row(s) dated {lo} to {hi}, "
                f"after its latest recorded week ({latest})"
            )
            found_any = True

        if null_starts == 0 and earliest is not None:
            count, lo, hi = conn.execute(
                f"SELECT COUNT(*), MIN({date_col}), MAX({date_col}) FROM {table} WHERE branch_id=? AND {date_col} < ?",
                (branch_id, earliest),
            ).fetchone()
            if count:
                print(
                    f"MISMATCH: branch '{branch_id}', {table} has {count} row(s) dated {lo} to {hi}, "
                    f"before its earliest recorded week ({earliest})"
                )
                found_any = True

if not found_any:
    print("Clean - no register rows found outside their branch's recorded extraction history.")
