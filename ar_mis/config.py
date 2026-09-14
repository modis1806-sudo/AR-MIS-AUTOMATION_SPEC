"""Branch config shape and run-level constants.

The branch list itself is NOT here - it is user-editable master data
stored in the branch_master table (ar_mis/storage.py: list_branches,
upsert_branch, delete_branch) and managed through the Branch Master
screen in ar_mis/webapp, precisely so adding a branch or fixing a Tally
host/port never requires editing Python source or redeploying anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class BranchConfig:
    branch_id: str
    branch_name: str
    tally_company_name: str  # exact company name as loaded in Tally
    tally_host: str = "localhost"
    tally_port: int = 9000

    @property
    def gateway_url(self) -> str:
        return f"http://{self.tally_host}:{self.tally_port}"


# Financial year start — used for the Section 4.2 YTD full-pull cross-check.
FINANCIAL_YEAR_START_MONTH = 4  # April
FINANCIAL_YEAR_START_DAY = 1


def financial_year_start(as_of: date) -> date:
    """Start-of-FY date for the FY containing `as_of` (April 1 in India)."""
    if (as_of.month, as_of.day) >= (FINANCIAL_YEAR_START_MONTH, FINANCIAL_YEAR_START_DAY):
        year = as_of.year
    else:
        year = as_of.year - 1
    return date(year, FINANCIAL_YEAR_START_MONTH, FINANCIAL_YEAR_START_DAY)


# Reversed, client's own explicit call (previously this module snapped
# every extraction outward to a fixed Monday-Sunday calendar grid). Real
# incident that forced the reversal: a brand-new branch's very first
# extraction, requested as "1 April" (a Wednesday), got silently
# stretched backward to include 30-31 March - two days that predate the
# branch's own go-live cutoff, double-counting against the Pre-MIS
# Outstanding baseline that was supposed to already cover them.
#
# Client's own reasoning, confirmed explicitly: this tool isn't custom to
# one customer's calendar - the underlying data (every invoice, CN,
# receipt, journal) is already stored by its own real date, never by
# "which week" (see SalesDNRegisterRow/CreditNoteRegisterRow/
# ReceiptJournalRegisterRow - none of them carry a week_ending column,
# never did). "Weekly" is only a real business requirement in two places,
# neither of which needs the underlying data pinned to a calendar grid:
# (1) management's chosen extraction CADENCE (roughly once a week -  a
# process decision, not a data rule), and (2) the Weekly Movement
# Register, which is a Maker's own deliberate, point-in-time recording
# action (ar_mis.webapp.app.weekly_movement_record) already computed live
# from whatever continuous data is on record - it never depended on
# extraction itself being chunked by calendar week.
#
# So extraction now runs on exactly the range asked for - never snapped,
# never silently widened past what was typed. A big backfill is still
# broken into CHUNK_DAYS-sized pieces below, purely as a practical batch
# size (the same reason voucher-fetching was already chunked - see
# tally_client._VOUCHER_FETCH_CHUNK_DAYS) - not because any chunk is
# meant to mean "a calendar week." Gaps aren't newly risked by dropping
# the grid: Section 4.2's YTD full-pull drift check already re-diffs the
# entire financial year on every run, independent of how any individual
# extraction was chunked, and would surface a missed date range exactly
# the same way it surfaces any other backdated/missed voucher.
CHUNK_DAYS = 7


def split_into_chunks(from_date: date, to_date: date, chunk_days: int = CHUNK_DAYS) -> list[tuple[date, date]]:
    """Splits [from_date, to_date] into consecutive `chunk_days`-sized
    pieces, starting exactly at from_date - never snapped to any calendar
    boundary. The final piece is shorter than chunk_days when the range
    doesn't divide evenly; every other piece is exactly chunk_days long.
    Covers the full range with no gap and no overlap between pieces.
    """
    chunks = []
    current = from_date
    while current <= to_date:
        chunk_end = min(current + timedelta(days=chunk_days - 1), to_date)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return chunks
