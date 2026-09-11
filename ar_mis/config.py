"""Branch registry and run configuration.

Branch list, Tally gateway host/port per branch, and financial-year start
date live here. This is the one file that needs editing when a branch is
added/removed or a Tally host changes.
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


# Placeholder registry — replace with the client's actual 6-7 branches
# and confirmed Tally company names before the first live run.
BRANCHES: list[BranchConfig] = [
    BranchConfig(branch_id="KOL", branch_name="Kolkata", tally_company_name="Kolkata HQ"),
    BranchConfig(branch_id="DEL", branch_name="Delhi", tally_company_name="Delhi Branch"),
    BranchConfig(branch_id="MUM", branch_name="Mumbai", tally_company_name="Mumbai Branch"),
    BranchConfig(branch_id="CHN", branch_name="Chennai", tally_company_name="Chennai Branch"),
    BranchConfig(branch_id="BLR", branch_name="Bangalore", tally_company_name="Bangalore Branch"),
    BranchConfig(branch_id="HYD", branch_name="Hyderabad", tally_company_name="Hyderabad Branch"),
]

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
