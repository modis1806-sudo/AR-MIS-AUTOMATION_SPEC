from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ar_mis.models import VoucherType
from ar_mis.parsers import (
    _sanitize_xml,
    categorize_voucher_type,
    parse_currently_loaded_companies,
    parse_ledger_closing_balances,
    parse_voucher_collection,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_categorize_voucher_type_matches_exact_canonical_names():
    assert categorize_voucher_type("Sales") == VoucherType.SALES
    assert categorize_voucher_type("Credit Note") == VoucherType.CREDIT_NOTE
    assert categorize_voucher_type("Debit Note") == VoucherType.DEBIT_NOTE
    assert categorize_voucher_type("Receipt") == VoucherType.RECEIPT
    assert categorize_voucher_type("Journal") == VoucherType.JOURNAL


def test_categorize_voucher_type_matches_custom_prefixed_suffixed_names():
    # Real Tally deployments commonly customize voucher type names -
    # these must still categorize correctly, not be rejected.
    assert categorize_voucher_type("Sales - Export") == VoucherType.SALES
    assert categorize_voucher_type("Local Sales") == VoucherType.SALES
    assert categorize_voucher_type("GST Credit Note") == VoucherType.CREDIT_NOTE
    assert categorize_voucher_type("Debit Note - Purchase Return") == VoucherType.DEBIT_NOTE
    assert categorize_voucher_type("Bank Receipt") == VoucherType.RECEIPT
    assert categorize_voucher_type("Journal Voucher") == VoucherType.JOURNAL


def test_categorize_voucher_type_is_case_insensitive():
    assert categorize_voucher_type("SALES") == VoucherType.SALES
    assert categorize_voucher_type("credit note") == VoucherType.CREDIT_NOTE


def test_categorize_voucher_type_prefers_credit_note_over_sales_on_ambiguous_name():
    # A hypothetical custom name containing both keywords should resolve
    # to the more specific category, not the more generic one.
    assert categorize_voucher_type("Sales Credit Note") == VoucherType.CREDIT_NOTE


def test_categorize_voucher_type_returns_none_for_irrelevant_types():
    # Payment, Contra, Purchase, Stock Journal etc. are legitimate real
    # voucher types that simply don't belong to the five AR categories -
    # returning None for them is correct, not a failure to recognize them.
    assert categorize_voucher_type("Payment") is None
    assert categorize_voucher_type("Contra") is None
    assert categorize_voucher_type("Purchase") is None
    assert categorize_voucher_type("Stock Journal") is None
    assert categorize_voucher_type("") is None


def test_categorize_voucher_type_matches_tax_invoice_as_sales():
    # Confirmed against a real client's data: a genuine, separate Tally
    # voucher type literally named "Tax Invoice" (own numbering series)
    # is the same AR category as Sales.
    assert categorize_voucher_type("Tax Invoice") == VoucherType.SALES


def test_categorize_voucher_type_matches_transport_invoice_as_sales():
    # Confirmed against Speedways' own real export: 90% of one week's
    # real Sales vouchers used this exact custom voucher type name -
    # silently dropped entirely before this keyword was added.
    assert categorize_voucher_type("Transport Invoice") == VoucherType.SALES


def test_categorize_voucher_type_does_not_match_bare_invoice():
    # "transport invoice"/"tax invoice" are specific keywords, not a
    # broad "invoice" substring - a Purchase Invoice must never be
    # miscategorized as a Sales voucher just for containing that word.
    assert categorize_voucher_type("Purchase Invoice") is None


def test_sanitize_xml_fixes_bare_ampersand():
    raw = "<LEDGERNAME>Ramesh & Sons</LEDGERNAME>"
    fixed = _sanitize_xml(raw)
    assert fixed == "<LEDGERNAME>Ramesh &amp; Sons</LEDGERNAME>"


def test_sanitize_xml_leaves_valid_entities_alone():
    raw = "<X>A &amp; B &lt; C</X>"
    assert _sanitize_xml(raw) == raw


def test_parse_voucher_collection_sales_with_single_bill_allocation():
    raw = (FIXTURES / "voucher_collection_sales.xml").read_text()
    vouchers = parse_voucher_collection(raw, branch_id="KOL")
    assert len(vouchers) == 2

    v = vouchers[0]
    assert v.voucher_type == VoucherType.SALES
    assert v.voucher_date == date(2026, 4, 6)
    assert v.voucher_number == "SB/0142"
    # Party ledger entry (first ALLLEDGERENTRIES.LIST) has one bill allocation.
    party_entries = [e for e in v.entries if e.party_ledger_name == "A & B Transport Pvt Ltd"]
    assert len(party_entries) == 1
    assert party_entries[0].amount_as_extracted == Decimal("-125000.00")
    assert party_entries[0].bill_name == "SB/0142"


def test_parse_voucher_collection_receipt_with_partial_multi_bill_allocation():
    raw = (FIXTURES / "voucher_collection_receipt.xml").read_text()
    vouchers = parse_voucher_collection(raw, branch_id="KOL")
    assert len(vouchers) == 1
    v = vouchers[0]
    assert v.voucher_type == VoucherType.RECEIPT

    party_entries = [e for e in v.entries if e.party_ledger_name == "Reliable Cargo Movers"]
    # One receipt split across two bills must produce two allocation lines,
    # not one flattened total - this is exactly the detail ODBC drops and
    # the reason Section 2.1 mandates XML export.
    assert len(party_entries) == 2
    amounts = {e.bill_name: e.amount_as_extracted for e in party_entries}
    assert amounts == {"SB/0143": Decimal("60000.00"), "SB/0120": Decimal("27500.00")}


def test_parse_voucher_collection_keeps_custom_named_ar_types_and_skips_irrelevant_ones():
    raw = (FIXTURES / "voucher_collection_mixed_types.xml").read_text()
    vouchers = parse_voucher_collection(raw, branch_id="KOL")
    # Payment and Stock Journal must be silently skipped - not errors,
    # just not AR-relevant. Only 2 of the 4 source vouchers survive.
    assert len(vouchers) == 2

    by_number = {v.voucher_number: v for v in vouchers}
    assert by_number["SB/0200"].voucher_type == VoucherType.SALES
    assert by_number["SB/0200"].raw_voucher_type_name == "Sales - Export"
    assert by_number["JV/0009"].voucher_type == VoucherType.JOURNAL
    assert by_number["JV/0009"].raw_voucher_type_name == "Provision Journal"
    assert "PAY/0011" not in by_number
    assert "SJ/0004" not in by_number


def test_parse_voucher_collection_skips_cancelled_vouchers():
    # Confirmed against real Speedways data: 9 real Sales vouchers in one
    # week's export were cancelled in Tally (ISCANCELLED=Yes) - a
    # cancelled voucher never happened and must never count as AR
    # movement, even if it still carried real ledger entries.
    raw = """<ENVELOPE>
 <VOUCHER>
  <DATE>20260406</DATE>
  <VOUCHERNUMBER>SB/0001</VOUCHERNUMBER>
  <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
  <ISCANCELLED>Yes</ISCANCELLED>
  <ALLLEDGERENTRIES.LIST>
   <LEDGERNAME>Acme Corp</LEDGERNAME>
   <AMOUNT>-1000.00</AMOUNT>
  </ALLLEDGERENTRIES.LIST>
  <ALLLEDGERENTRIES.LIST>
   <LEDGERNAME>Sales Revenue</LEDGERNAME>
   <AMOUNT>1000.00</AMOUNT>
  </ALLLEDGERENTRIES.LIST>
 </VOUCHER>
 <VOUCHER>
  <DATE>20260406</DATE>
  <VOUCHERNUMBER>SB/0002</VOUCHERNUMBER>
  <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
  <ISCANCELLED>No</ISCANCELLED>
  <ALLLEDGERENTRIES.LIST>
   <LEDGERNAME>Acme Corp</LEDGERNAME>
   <AMOUNT>-2000.00</AMOUNT>
  </ALLLEDGERENTRIES.LIST>
  <ALLLEDGERENTRIES.LIST>
   <LEDGERNAME>Sales Revenue</LEDGERNAME>
   <AMOUNT>2000.00</AMOUNT>
  </ALLLEDGERENTRIES.LIST>
 </VOUCHER>
</ENVELOPE>"""
    vouchers = parse_voucher_collection(raw, branch_id="KOL")
    assert len(vouchers) == 1
    assert vouchers[0].voucher_number == "SB/0002"


def test_parse_voucher_collection_falls_back_to_inventory_allocations_when_no_offsetting_ledger_entry():
    # Confirmed against real Speedways data: a stock-item invoice can
    # carry only the party's own top-level ledger entry - the real
    # revenue breakdown lives in each stock item's own
    # ACCOUNTINGALLOCATIONS.LIST inside ALLINVENTORYENTRIES.LIST.
    raw = """<ENVELOPE>
 <VOUCHER>
  <DATE>20260401</DATE>
  <VOUCHERNUMBER>26/HSLPL/26-27</VOUCHERNUMBER>
  <VOUCHERTYPENAME>Transport Invoice</VOUCHERTYPENAME>
  <LEDGERENTRIES.LIST>
   <LEDGERNAME>Acme Corp</LEDGERNAME>
   <AMOUNT>-100000.00</AMOUNT>
  </LEDGERENTRIES.LIST>
  <ALLINVENTORYENTRIES.LIST>
   <ACCOUNTINGALLOCATIONS.LIST>
    <LEDGERNAME>Road Transport Services_Exempt Goods</LEDGERNAME>
    <AMOUNT>60000.00</AMOUNT>
   </ACCOUNTINGALLOCATIONS.LIST>
  </ALLINVENTORYENTRIES.LIST>
  <ALLINVENTORYENTRIES.LIST>
   <ACCOUNTINGALLOCATIONS.LIST>
    <LEDGERNAME>Road Transport Services_Exempt Goods</LEDGERNAME>
    <AMOUNT>40000.00</AMOUNT>
   </ACCOUNTINGALLOCATIONS.LIST>
  </ALLINVENTORYENTRIES.LIST>
 </VOUCHER>
</ENVELOPE>"""
    vouchers = parse_voucher_collection(raw, branch_id="KOL")
    assert len(vouchers) == 1
    entries = vouchers[0].entries
    assert len(entries) == 3
    non_party = [e for e in entries if e.party_ledger_name != "Acme Corp"]
    assert sum(e.amount_as_extracted for e in non_party) == Decimal("100000.00")


def test_parse_voucher_collection_does_not_double_count_when_top_level_entries_are_complete():
    # A voucher that already has a full ledger-level picture (party + tax)
    # commonly ALSO carries inventory detail for line-item breakdown -
    # confirmed against real data that 154 of 171 real vouchers in one
    # week's export have both. The inventory allocations must be ignored
    # in that case, not added on top of an already-complete total.
    raw = """<ENVELOPE>
 <VOUCHER>
  <DATE>20260401</DATE>
  <VOUCHERNUMBER>1/HSLPL/26-27</VOUCHERNUMBER>
  <VOUCHERTYPENAME>Transport Invoice</VOUCHERTYPENAME>
  <LEDGERENTRIES.LIST>
   <LEDGERNAME>Acme Corp</LEDGERNAME>
   <AMOUNT>-21830.00</AMOUNT>
  </LEDGERENTRIES.LIST>
  <LEDGERENTRIES.LIST>
   <LEDGERNAME>CGST</LEDGERNAME>
   <AMOUNT>1665.00</AMOUNT>
  </LEDGERENTRIES.LIST>
  <LEDGERENTRIES.LIST>
   <LEDGERNAME>SGST</LEDGERNAME>
   <AMOUNT>1665.00</AMOUNT>
  </LEDGERENTRIES.LIST>
  <ALLINVENTORYENTRIES.LIST>
   <ACCOUNTINGALLOCATIONS.LIST>
    <LEDGERNAME>Road Transport Services_GST_18%</LEDGERNAME>
    <AMOUNT>18500.00</AMOUNT>
   </ACCOUNTINGALLOCATIONS.LIST>
  </ALLINVENTORYENTRIES.LIST>
 </VOUCHER>
</ENVELOPE>"""
    vouchers = parse_voucher_collection(raw, branch_id="KOL")
    assert len(vouchers) == 1
    assert len(vouchers[0].entries) == 3


def test_parse_ledger_closing_balances():
    raw = (FIXTURES / "ledger_closing_balances.xml").read_text()
    balances = parse_ledger_closing_balances(raw)
    assert balances["A & B Transport Pvt Ltd"] == Decimal("-425000.00")
    assert balances["Reliable Cargo Movers"] == Decimal("-150000.00")


def test_parse_ledger_closing_balances_handles_real_display_report_shape():
    # Confirmed against a real manually-exported Trial Balance: Tally's
    # "Display Report" shape has no LEDGER element at all - each party is
    # a DSPACCNAME/DSPDISPNAME element immediately followed by a sibling
    # DSPACCINFO/DSPCLAMT/DSPCLAMTA element, paired by document order.
    raw = (FIXTURES / "real_samples" / "TBDebtors.xml").read_text(encoding="utf-8")
    balances = parse_ledger_closing_balances(raw)
    assert len(balances) == 389
    assert balances["ADINATH GLOBAL CORPORATION"] == Decimal("-113019.00")
    assert balances["YAKSHARAT INFRACON PRIVATE LIMITED(TRPT)"] == Decimal("-1647890.70")
    # A blank DSPCLAMTA (a handful of real zero-balance accounts) must
    # parse as a real 0, not be skipped or raise.
    zero_balance_parties = [name for name, bal in balances.items() if bal == Decimal("0")]
    assert len(zero_balance_parties) == 3


def test_parse_ledger_closing_balances_display_report_raises_on_mismatched_pair_counts():
    raw = """<ENVELOPE>
 <DSPACCNAME><DSPDISPNAME>Only Name, No Info</DSPDISPNAME></DSPACCNAME>
</ENVELOPE>"""
    with pytest.raises(ValueError, match="cannot pair name to closing balance"):
        parse_ledger_closing_balances(raw)


def test_parse_currently_loaded_companies():
    # Fixture reflects the real response from a live TallyPrime Gold
    # instance: multiple companies can be open at once, and the request
    # (xml_requests.list_of_companies_request) is a Company-type TDL
    # Collection, not the simpler REPORTNAME form an earlier version of
    # this function used - that form returned "Unknown Request, cannot
    # be processed" against real Tally instead of company data.
    raw = (FIXTURES / "list_of_companies.xml").read_text()
    names = parse_currently_loaded_companies(raw)
    assert names == [
        "SPEEDWAYS LOGISTICS PRIVATE LIMITED (MUNDRA)",
        "SPEEDWAYS LOGISTICS PRIVATE LIMITED (NAGPUR) - (from-1.4.23)",
        "SPEEDWAYS LOGISTICS PRIVATE LIMITED VIZAG - (From 1.4.23)",
        "SPEEDWAYS LOGISTICS PVT. LTD. (DELHI) (from 1-Apr-23)",
    ]


def test_parse_currently_loaded_companies_ignores_cmpinfo_diagnostics_block():
    # CONFIRMED against a live TallyPrime Gold instance (a different real
    # response shape than the fixture above): the response's
    # BODY/DESC/CMPINFO block carries plain master-count fields, one of
    # which is literally named <COMPANY>0</COMPANY> - a count, not a
    # company. A tree-wide search for any COMPANY/NAME tag picked this up
    # as a phantom company named "0" alongside the one real company.
    raw = (FIXTURES / "list_of_companies_with_cmpinfo_counts.xml").read_text()
    names = parse_currently_loaded_companies(raw)
    assert names == ["CHARZE INDUSTRIES PRIVATE LIMITED - From 1-Apr-25"]
