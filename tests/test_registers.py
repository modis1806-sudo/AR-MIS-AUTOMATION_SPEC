from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ar_mis.models import (
    CustomerMasterRecord,
    LedgerEntry,
    NoteType,
    RegisterClassification,
    Voucher,
    VoucherType,
)
from ar_mis.parsers import parse_voucher_collection
from ar_mis.registers import (
    build_bill_reference_lookup,
    build_credit_note_register_row,
    build_receipt_journal_register_rows,
    build_sales_dn_register_row,
    classify_tax_ledger,
    compute_ageing_bucket,
    compute_due_date,
    compute_invoice_position,
    compute_unapplied_cash_by_party,
    compute_unapplied_cn_by_party,
    is_round_off_ledger,
    RegisterBuildExceptions,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "real_samples"

CUSTOMER = CustomerMasterRecord(
    party_id="ACME", party_name="Acme Corp", branch_id="B1",
    pre_mis_outstanding=Decimal("0.00"), credit_period_days=30,
)


def sales_voucher(entries, voucher_number="INV001", voucher_date=date(2026, 4, 10)):
    return Voucher(
        voucher_type=VoucherType.SALES,
        voucher_date=voucher_date,
        voucher_number=voucher_number,
        branch_id="B1",
        party_ledger_name="ACME",
        entries=entries,
    )


# ---- Tax ledger / Round Off classification -----------------------------


def test_classify_tax_ledger_exact_match_only():
    assert classify_tax_ledger("CGST") == "CGST"
    assert classify_tax_ledger(" sgst ") == "SGST"
    assert classify_tax_ledger("IGST") == "IGST"
    assert classify_tax_ledger("Freight") is None


def test_classify_tax_ledger_rejects_gst_suffixed_revenue_ledgers():
    # Confirmed against real data: many revenue ledgers are "_GST"-suffixed
    # and must NOT be caught by a substring match.
    assert classify_tax_ledger("Road Transport Services_GST_18%") is None
    assert classify_tax_ledger("Handling Services_GST_INTER") is None


def test_is_round_off_ledger():
    assert is_round_off_ledger("Round Off")
    assert is_round_off_ledger(" ROUND OFF ")
    assert not is_round_off_ledger("Round Off Charges")


# ---- compute_due_date ----------------------------------------------------


def test_compute_due_date_adds_credit_period():
    assert compute_due_date(date(2026, 4, 10), 30) == date(2026, 5, 10)


# ---- build_sales_dn_register_row -----------------------------------------


def test_build_sales_dn_register_row_splits_taxable_tax_and_round_off():
    voucher = sales_voucher(
        [
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-1180.00"), bill_name="INV001", bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Sales Revenue", amount_as_extracted=Decimal("1000.00")),
            LedgerEntry(party_ledger_name="CGST", amount_as_extracted=Decimal("90.00")),
            LedgerEntry(party_ledger_name="SGST", amount_as_extracted=Decimal("90.00")),
            LedgerEntry(party_ledger_name="Round Off", amount_as_extracted=Decimal("0.00")),
        ]
    )
    exceptions = RegisterBuildExceptions()
    row = build_sales_dn_register_row(voucher, CUSTOMER, exceptions)

    assert row is not None
    assert row.note_type == NoteType.INVOICE
    assert row.taxable_value == Decimal("1000.00")
    assert row.cgst == Decimal("90.00")
    assert row.sgst == Decimal("90.00")
    assert row.igst == Decimal("0.00")
    assert row.invoice_value == Decimal("1180.00")
    assert row.bill_allocation_reference == "INV001"
    assert row.due_date == date(2026, 5, 10)
    assert exceptions.unattributable_party == []


def test_build_sales_dn_register_row_round_off_can_reduce_total():
    voucher = sales_voucher(
        [
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-1179.00"), bill_name="INV002", bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Sales Revenue", amount_as_extracted=Decimal("1000.00")),
            LedgerEntry(party_ledger_name="CGST", amount_as_extracted=Decimal("90.00")),
            LedgerEntry(party_ledger_name="SGST", amount_as_extracted=Decimal("90.00")),
            LedgerEntry(party_ledger_name="Round Off", amount_as_extracted=Decimal("-1.00")),
        ],
        voucher_number="INV002",
    )
    row = build_sales_dn_register_row(voucher, CUSTOMER, RegisterBuildExceptions())
    assert row.invoice_value == Decimal("1179.00")


def test_build_sales_dn_register_row_falls_back_to_voucher_number_when_no_bill_name():
    voucher = sales_voucher(
        [
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-1000.00")),
            LedgerEntry(party_ledger_name="Sales Revenue", amount_as_extracted=Decimal("1000.00")),
        ]
    )
    row = build_sales_dn_register_row(voucher, CUSTOMER, RegisterBuildExceptions())
    assert row.bill_allocation_reference == "INV001"


def test_build_sales_dn_register_row_missing_party_is_flagged_not_dropped_silently():
    voucher = Voucher(
        voucher_type=VoucherType.SALES,
        voucher_date=date(2026, 4, 10),
        voucher_number="INV999",
        branch_id="B1",
        party_ledger_name="",
        entries=[],
    )
    exceptions = RegisterBuildExceptions()
    row = build_sales_dn_register_row(voucher, CUSTOMER, exceptions)
    assert row is None
    assert exceptions.unattributable_party == [("INV999", "No PARTYLEDGERNAME on this voucher - cannot attribute to a customer")]


def test_build_sales_dn_register_row_rejects_wrong_voucher_type():
    voucher = Voucher(
        voucher_type=VoucherType.RECEIPT,
        voucher_date=date(2026, 4, 10),
        voucher_number="R1",
        branch_id="B1",
        party_ledger_name="ACME",
    )
    with pytest.raises(ValueError, match="not Sales/Debit Note"):
        build_sales_dn_register_row(voucher, CUSTOMER, RegisterBuildExceptions())


def test_build_sales_dn_register_row_debit_note_is_note_type_debit_note():
    voucher = Voucher(
        voucher_type=VoucherType.DEBIT_NOTE,
        voucher_date=date(2026, 4, 10),
        voucher_number="DN1",
        branch_id="B1",
        party_ledger_name="ACME",
        entries=[
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-500.00")),
            LedgerEntry(party_ledger_name="Purchase Return", amount_as_extracted=Decimal("500.00")),
        ],
    )
    row = build_sales_dn_register_row(voucher, CUSTOMER, RegisterBuildExceptions())
    assert row.note_type == NoteType.DEBIT_NOTE


# ---- Bill-reference matching (item 8) via CN and Receipt/Journal --------


def _tracked_invoice():
    voucher = sales_voucher(
        [
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-1180.00"), bill_name="INV001", bill_type="New Ref"),
            LedgerEntry(party_ledger_name="Sales Revenue", amount_as_extracted=Decimal("1000.00")),
            LedgerEntry(party_ledger_name="CGST", amount_as_extracted=Decimal("90.00")),
            LedgerEntry(party_ledger_name="SGST", amount_as_extracted=Decimal("90.00")),
        ]
    )
    row = build_sales_dn_register_row(voucher, CUSTOMER, RegisterBuildExceptions())
    return build_bill_reference_lookup([row])


def test_credit_note_agst_ref_matching_tracked_invoice_is_current():
    lookup = _tracked_invoice()
    voucher = Voucher(
        voucher_type=VoucherType.CREDIT_NOTE,
        voucher_date=date(2026, 4, 15),
        voucher_number="CN001",
        branch_id="B1",
        party_ledger_name="ACME",
        entries=[
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("200.00"), bill_name="INV001", bill_type="Agst Ref"),
        ],
    )
    row = build_credit_note_register_row(voucher, lookup, RegisterBuildExceptions())
    assert row.classification == RegisterClassification.CURRENT
    assert row.bill_allocation_reference == "INV001"
    assert row.cn_amount == Decimal("200.00")


def test_credit_note_advance_treated_same_as_agst_ref():
    lookup = _tracked_invoice()
    voucher = Voucher(
        voucher_type=VoucherType.CREDIT_NOTE,
        voucher_date=date(2026, 4, 15),
        voucher_number="CN002",
        branch_id="B1",
        party_ledger_name="ACME",
        entries=[
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("200.00"), bill_name="INV001", bill_type="Advance"),
        ],
    )
    row = build_credit_note_register_row(voucher, lookup, RegisterBuildExceptions())
    assert row.classification == RegisterClassification.CURRENT


def test_credit_note_unresolved_reference_is_pending_review_not_dropped():
    lookup = _tracked_invoice()
    voucher = Voucher(
        voucher_type=VoucherType.CREDIT_NOTE,
        voucher_date=date(2026, 4, 15),
        voucher_number="CN003",
        branch_id="B1",
        party_ledger_name="ACME",
        entries=[
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("200.00"), bill_name="INV-PRE-MIS-1", bill_type="Agst Ref"),
        ],
    )
    row = build_credit_note_register_row(voucher, lookup, RegisterBuildExceptions())
    assert row.classification == RegisterClassification.PENDING_REVIEW
    assert row.bill_allocation_reference == "INV-PRE-MIS-1"


def test_credit_note_new_ref_is_unapplied_and_current():
    voucher = Voucher(
        voucher_type=VoucherType.CREDIT_NOTE,
        voucher_date=date(2026, 4, 15),
        voucher_number="CN004",
        branch_id="B1",
        party_ledger_name="ACME",
        entries=[
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("200.00"), bill_name="CN004", bill_type="New Ref"),
        ],
    )
    row = build_credit_note_register_row(voucher, {}, RegisterBuildExceptions())
    assert row.classification == RegisterClassification.CURRENT
    assert row.bill_allocation_reference is None


def test_credit_note_missing_party_is_flagged():
    voucher = Voucher(
        voucher_type=VoucherType.CREDIT_NOTE,
        voucher_date=date(2026, 4, 15),
        voucher_number="CN005",
        branch_id="B1",
        party_ledger_name="",
    )
    exceptions = RegisterBuildExceptions()
    row = build_credit_note_register_row(voucher, {}, exceptions)
    assert row is None
    assert len(exceptions.unattributable_party) == 1


def test_credit_note_rejects_wrong_voucher_type():
    voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=date(2026, 4, 10),
        voucher_number="X", branch_id="B1", party_ledger_name="ACME",
    )
    with pytest.raises(ValueError, match="not Credit Note"):
        build_credit_note_register_row(voucher, {}, RegisterBuildExceptions())


# ---- Receipt/Journal voucher-level classification promotion (item 10) --


def test_receipt_journal_promotes_entire_voucher_to_pending_review():
    lookup = _tracked_invoice()
    voucher = Voucher(
        voucher_type=VoucherType.RECEIPT,
        voucher_date=date(2026, 4, 20),
        voucher_number="RCPT001",
        branch_id="B1",
        party_ledger_name="ACME",
        entries=[
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("1180.00"), bill_name="INV001", bill_type="Agst Ref"),
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("500.00"), bill_name="UNKNOWN-REF", bill_type="Agst Ref"),
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("300.00"), bill_name="RCPT001", bill_type="New Ref"),
        ],
    )
    rows = build_receipt_journal_register_rows(voucher, lookup, RegisterBuildExceptions())
    assert len(rows) == 3
    # Every line - including the resolved match and the unapplied leg -
    # must share the most severe classification found anywhere in the
    # voucher, per item 10's phantom-imbalance fix.
    assert all(r.classification == RegisterClassification.PENDING_REVIEW for r in rows)


def test_receipt_journal_all_current_when_every_line_resolves_or_is_unapplied():
    lookup = _tracked_invoice()
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        voucher_date=date(2026, 4, 20),
        voucher_number="JV001",
        branch_id="B1",
        party_ledger_name="ACME",
        entries=[
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("1180.00"), bill_name="INV001", bill_type="Agst Ref"),
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("300.00"), bill_name="JV001", bill_type="New Ref"),
        ],
    )
    rows = build_receipt_journal_register_rows(voucher, lookup, RegisterBuildExceptions())
    assert all(r.classification == RegisterClassification.CURRENT for r in rows)


def test_receipt_journal_missing_party_returns_empty_and_flags():
    exceptions = RegisterBuildExceptions()
    voucher = Voucher(
        voucher_type=VoucherType.RECEIPT, voucher_date=date(2026, 4, 20),
        voucher_number="R2", branch_id="B1", party_ledger_name="",
    )
    rows = build_receipt_journal_register_rows(voucher, {}, exceptions)
    assert rows == []
    assert len(exceptions.unattributable_party) == 1


def test_receipt_journal_rejects_wrong_voucher_type():
    voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=date(2026, 4, 10),
        voucher_number="X", branch_id="B1", party_ledger_name="ACME",
    )
    with pytest.raises(ValueError):
        build_receipt_journal_register_rows(voucher, {}, RegisterBuildExceptions())


# ---- Unapplied cash/CN netting (item 9) ----------------------------------


def test_unapplied_cash_nets_a_receipt_against_its_reversing_journal():
    from ar_mis.models import ReceiptJournalRegisterRow

    rows = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 4, 1), voucher_type="Receipt",
            voucher_number="R1", party_id="ACME", amount=Decimal("1000.00"), target_doc_no=None,
        ),
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 4, 2), voucher_type="Journal",
            voucher_number="J1", party_id="ACME", amount=Decimal("-1000.00"), target_doc_no=None,
        ),
    ]
    totals = compute_unapplied_cash_by_party(rows)
    assert totals == {"ACME": Decimal("0.00")}


def test_unapplied_cash_excludes_pending_review_and_pre_mis():
    from ar_mis.models import ReceiptJournalRegisterRow

    rows = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 4, 1), voucher_type="Receipt",
            voucher_number="R1", party_id="ACME", amount=Decimal("1000.00"), target_doc_no=None,
            classification=RegisterClassification.PENDING_REVIEW,
        ),
    ]
    assert compute_unapplied_cash_by_party(rows) == {}


def test_unapplied_cn_nets_per_party_and_excludes_allocated():
    from ar_mis.models import CreditNoteRegisterRow

    rows = [
        CreditNoteRegisterRow(
            branch_id="B1", cn_date=date(2026, 4, 1), voucher_number="CN1",
            party_id="ACME", cn_amount=Decimal("500.00"), bill_allocation_reference=None,
        ),
        CreditNoteRegisterRow(
            branch_id="B1", cn_date=date(2026, 4, 2), voucher_number="CN2",
            party_id="ACME", cn_amount=Decimal("200.00"), bill_allocation_reference="INV001",
        ),
    ]
    assert compute_unapplied_cn_by_party(rows) == {"ACME": Decimal("500.00")}


# ---- Ageing bucket --------------------------------------------------------


@pytest.mark.parametrize(
    "dpd,expected",
    [(0, "Current"), (-5, "Current"), (1, "1-30"), (30, "1-30"), (31, "31-60"),
     (60, "31-60"), (61, "61-90"), (90, "61-90"), (91, "91-120"), (120, "91-120"),
     (121, "121-150"), (150, "121-150"), (151, "151-180"), (180, "151-180"), (181, "181+")],
)
def test_compute_ageing_bucket(dpd, expected):
    assert compute_ageing_bucket(dpd) == expected


# ---- As-of-date invoice position (item 14) -------------------------------


def test_compute_invoice_position_filters_to_as_of_date():
    from ar_mis.models import CreditNoteRegisterRow, ReceiptJournalRegisterRow

    invoice = build_sales_dn_register_row(
        sales_voucher(
            [
                LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-1000.00"), bill_name="INV001", bill_type="New Ref"),
                LedgerEntry(party_ledger_name="Sales Revenue", amount_as_extracted=Decimal("1000.00")),
            ]
        ),
        CUSTOMER,
        RegisterBuildExceptions(),
    )
    cn_rows = [
        CreditNoteRegisterRow(
            branch_id="B1", cn_date=date(2026, 4, 20), voucher_number="CN1",
            party_id="ACME", cn_amount=Decimal("100.00"), bill_allocation_reference="INV001",
        ),
        # This CN is dated AFTER our as-of date and must not count yet.
        CreditNoteRegisterRow(
            branch_id="B1", cn_date=date(2026, 6, 1), voucher_number="CN2",
            party_id="ACME", cn_amount=Decimal("100.00"), bill_allocation_reference="INV001",
        ),
    ]
    receipt_rows = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 4, 25), voucher_type="Receipt",
            voucher_number="R1", party_id="ACME", amount=Decimal("400.00"), target_doc_no="INV001",
        ),
    ]

    position = compute_invoice_position(invoice, cn_rows, receipt_rows, as_of=date(2026, 5, 1))
    assert position.linked_cn_amount == Decimal("100.00")
    assert position.receipts_applied == Decimal("400.00")
    assert position.open_amount == Decimal("500.00")  # 1000 - 100 - 400
    # Due date is 2026-05-10 (invoice_date 2026-04-10 + 30 days) - as-of
    # 2026-05-01 is still before that, so not yet overdue.
    assert position.is_overdue is False


def test_compute_invoice_position_overdue_and_ageing_bucket():
    from ar_mis.models import CreditNoteRegisterRow, ReceiptJournalRegisterRow

    invoice = build_sales_dn_register_row(
        sales_voucher(
            [
                LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-1000.00"), bill_name="INV001", bill_type="New Ref"),
                LedgerEntry(party_ledger_name="Sales Revenue", amount_as_extracted=Decimal("1000.00")),
            ],
            voucher_date=date(2026, 1, 1),
        ),
        CUSTOMER,
        RegisterBuildExceptions(),
    )
    # Due date = 2026-01-31. As-of 2026-05-01 is 90 days past due.
    position = compute_invoice_position(invoice, [], [], as_of=date(2026, 5, 1))
    assert position.open_amount == Decimal("1000.00")
    assert position.is_overdue is True
    assert position.days_past_due == 90
    assert position.ageing_bucket == "61-90"


def test_compute_invoice_position_fully_paid_is_not_overdue():
    from ar_mis.models import ReceiptJournalRegisterRow

    invoice = build_sales_dn_register_row(
        sales_voucher(
            [
                LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-1000.00"), bill_name="INV001", bill_type="New Ref"),
                LedgerEntry(party_ledger_name="Sales Revenue", amount_as_extracted=Decimal("1000.00")),
            ],
            voucher_date=date(2026, 1, 1),
        ),
        CUSTOMER,
        RegisterBuildExceptions(),
    )
    receipt_rows = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 1, 15), voucher_type="Receipt",
            voucher_number="R1", party_id="ACME", amount=Decimal("1000.00"), target_doc_no="INV001",
        ),
    ]
    position = compute_invoice_position(invoice, [], receipt_rows, as_of=date(2026, 5, 1))
    assert position.open_amount == Decimal("0.00")
    assert position.is_overdue is False
    assert position.ageing_bucket == "Current"


# ---- End-to-end against real client-exported sample XML ------------------


def _load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_full_pipeline_against_real_sample_fixtures():
    exceptions = RegisterBuildExceptions()
    default_customer = CustomerMasterRecord(
        party_id="_default", party_name="_default", branch_id="B1",
        pre_mis_outstanding=Decimal("0.00"),
    )

    sales_vouchers = parse_voucher_collection(_load("SalesReg.xml"), branch_id="B1")
    dn_vouchers = parse_voucher_collection(_load("DNReg.xml"), branch_id="B1")
    cn_vouchers = parse_voucher_collection(_load("CreditNoteReg.xml"), branch_id="B1")
    receipt_vouchers = parse_voucher_collection(_load("ReceiptReg.xml"), branch_id="B1")
    journal_vouchers = parse_voucher_collection(_load("JournalReg.xml"), branch_id="B1")

    sales_dn_rows = [
        row
        for v in sales_vouchers + dn_vouchers
        if (row := build_sales_dn_register_row(v, default_customer, exceptions)) is not None
    ]
    # One real voucher in this fixture set has no PARTYLEDGERNAME - it must
    # be flagged, not silently dropped or crash the pipeline.
    assert len(exceptions.unattributable_party) == 1
    assert len(sales_dn_rows) == len(sales_vouchers) + len(dn_vouchers) - 1

    lookup = build_bill_reference_lookup(sales_dn_rows)

    cn_rows = [
        row
        for v in cn_vouchers
        if (row := build_credit_note_register_row(v, lookup, exceptions)) is not None
    ]
    assert len(cn_rows) == len(cn_vouchers)

    rj_rows = []
    for v in receipt_vouchers + journal_vouchers:
        rj_rows.extend(build_receipt_journal_register_rows(v, lookup, exceptions))
    assert len(rj_rows) > 0

    # No crash, no silently-zero amounts anywhere in the built registers -
    # every row's own amount field must be a real, parsed Decimal.
    assert all(row.invoice_value != Decimal("0.00") for row in sales_dn_rows)
