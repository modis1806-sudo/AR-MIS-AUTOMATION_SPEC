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


# Client's explicit, permanent rule: the reporting week is always
# Monday through Sunday - fixed to the real calendar, never floating
# based on whatever day an extraction happens to run on or whatever day
# the very first week happened to start. Without this, week_ending would
# just be whatever date got typed in, and a first extraction run on a
# Wednesday would anchor every subsequent week Wednesday-to-Wednesday
# instead of the real business week - silently corrupting the week-over-
# week trending (Weekly Movement Register, DSO) that depends on every
# row meaning the same real calendar week.
def week_start(d: date) -> date:
    """Monday of the calendar week containing `d`."""
    return d - timedelta(days=d.weekday())


def week_end(d: date) -> date:
    """Sunday of the calendar week containing `d` - this app's own
    week_ending value.
    """
    return week_start(d) + timedelta(days=6)


def snap_to_full_weeks(from_date: date, to_date: date) -> tuple[date, date]:
    """Extends [from_date, to_date] outward to the nearest full Monday-
    Sunday weeks - never narrows the requested range, matching this
    codebase's own never-discard principle (better to pull a little more
    than asked than to silently drop data the operator expected covered).
    """
    return week_start(from_date), week_end(to_date)


def split_into_weeks(from_date: date, to_date: date) -> list[tuple[date, date]]:
    """Splits an already week-aligned [from_date, to_date] range (as
    produced by snap_to_full_weeks) into consecutive (Monday, Sunday)
    pairs - one entry per real calendar week, each becoming its own
    weekly_snapshot write.
    """
    weeks = []
    current = from_date
    while current <= to_date:
        weeks.append((current, current + timedelta(days=6)))
        current += timedelta(days=7)
    return weeks
