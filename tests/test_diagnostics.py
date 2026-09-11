"""Tests for the committed diagnostic CLI (ar_mis.diagnostics) - the
generalized replacement for the one-off scripts hand-built during real
Tally troubleshooting, which never made it into the repo."""
from pathlib import Path

import pytest

from ar_mis import diagnostics
from ar_mis.tally_client import TallyClient

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def stub_post(monkeypatch):
    """Every diagnostics command posts through TallyClient._post - stub it
    once here so each test only has to say what the canned response is.
    """
    responses = {}

    def fake_post(self, xml_request):
        return responses["next"]

    monkeypatch.setattr(TallyClient, "_post", fake_post)
    return responses


def test_companies_command_lists_open_companies(stub_post, capsys):
    stub_post["next"] = (FIXTURES / "list_of_companies.xml").read_text()
    exit_code = diagnostics.main(["companies", "--host", "localhost", "--port", "9000"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "SPEEDWAYS LOGISTICS PRIVATE LIMITED (MUNDRA)" in out
    assert "Currently open:" in out


def test_companies_command_raw_flag_also_prints_xml(stub_post, capsys):
    stub_post["next"] = (FIXTURES / "list_of_companies.xml").read_text()
    diagnostics.main(["companies", "--raw"])
    out = capsys.readouterr().out
    assert "<ENVELOPE>" in out
    assert "Currently open:" in out


def test_vouchers_command_writes_all_five_types_to_output_file(stub_post, tmp_path):
    stub_post["next"] = (FIXTURES / "voucher_collection_sales.xml").read_text()
    output_path = tmp_path / "dump.txt"
    exit_code = diagnostics.main(
        ["vouchers", "--company", "Acme Corp", "--output", str(output_path)]
    )
    assert exit_code == 0
    text = output_path.read_text()
    assert "===== Sales =====" in text
    assert "===== Journal =====" in text
    assert text.count("<ENVELOPE>") == 5  # one per voucher type


def test_ledgers_command_prints_raw_response(stub_post, capsys):
    stub_post["next"] = (FIXTURES / "ledger_closing_balances.xml").read_text()
    diagnostics.main(["ledgers", "--company", "Acme Corp", "--as-of", "2026-04-07"])
    out = capsys.readouterr().out
    assert "A &amp; B Transport Pvt Ltd" in out or "A & B Transport Pvt Ltd" in out
