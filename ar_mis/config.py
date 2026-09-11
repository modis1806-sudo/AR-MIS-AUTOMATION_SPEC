"""Branch config shape and run-level constants.

The branch list itself is NOT here - it is user-editable master data
stored in the branch_master table (ar_mis/storage.py: list_branches,
upsert_branch, delete_branch) and managed through the Branch Master
screen in ar_mis/webapp, precisely so adding a branch or fixing a Tally
host/port never requires editing Python source or redeploying anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


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
