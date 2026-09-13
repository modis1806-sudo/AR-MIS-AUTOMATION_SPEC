"""Tests for ar_mis.register_export - the Excel export for the three
webapp Registers screens. Each build_*_workbook() function takes exactly
the same display_rows structure the corresponding webapp route builds
for its Jinja template, so these tests construct that structure directly
rather than going through a live Flask app or Store.
"""
from datetime import date
from decimal import Decimal

from ar_mis.models import (
    CreditNoteRegisterRow,
    InvoiceFollowUp,
    NoteType,
    ReceiptJournalRegisterRow,
    RegisterClassification,
    SalesDNRegisterRow,
)
from ar_mis.register_export import (
    build_credit_note_register_workbook,
    build_receipt_journal_register_workbook,
    build_sales_dn_register_workbook,
)
from ar_mis.registers import InvoicePosition, ReceiptJournalDisplayFields


def _sales_dn_row():
    return SalesDNRegisterRow(
        branch_id="MUN", invoice_date=date(2026, 6, 10), note_type=NoteType.INVOICE,
        voucher_number="INV/001", bill_allocation_reference="INV/001", party_id="GIRIDHAN",
        taxable_value=Decimal("394655.00"), cgst=Decimal("34822.50"), sgst=Decimal("34822.50"),
        igst=Decimal("0.00"), invoice_value=Decimal("464300.00"), due_date=date(2026, 7, 10),
        round_off=Decimal("0.00"),
    )


def test_sales_dn_register_workbook_contains_headers_and_row_values():
    row = _sales_dn_row()
    position = InvoicePosition(
        row=row, linked_cn_amount=Decimal("0.00"), receipts_applied=Decimal("200000.00"),
        open_amount=Decimal("264300.00"), is_overdue=True, days_past_due=64, ageing_bucket="61-90",
    )
    follow_up = InvoiceFollowUp(
        branch_id="MUN", voucher_number="INV/001", party_id="GIRIDHAN",
        ptp_date=date(2026, 9, 25), ptp_amount=Decimal("264300.00"), next_action="Call customer",
    )
    display_rows = [
        {
            "row": row, "position": position, "linked_cn_no": "", "follow_up": follow_up,
            "ptp_status": "Active", "grouping": "Sundry Debtor",
        }
    ]

    wb = build_sales_dn_register_workbook(display_rows, as_of=date(2026, 9, 12))
    ws = wb.active
    assert ws.title == "Sales & DN Register"
    header_row = [c.value for c in ws[3]]
    assert "Voucher No." in header_row
    assert "Ageing Bucket" in header_row
    assert "Round Off" in header_row

    data_row = [c.value for c in ws[4]]
    assert "INV/001" in data_row
    assert "GIRIDHAN" in data_row
    assert "Sundry Debtor" in data_row
    assert 264300.0 in data_row
    assert "61-90" in data_row


def test_sales_dn_register_workbook_includes_nonzero_round_off():
    row = SalesDNRegisterRow(
        branch_id="MUN", invoice_date=date(2026, 6, 10), note_type=NoteType.INVOICE,
        voucher_number="INV/002", bill_allocation_reference="INV/002", party_id="GIRIDHAN",
        taxable_value=Decimal("1000.00"), cgst=Decimal("90.00"), sgst=Decimal("90.00"),
        igst=Decimal("0.00"), invoice_value=Decimal("1179.00"), due_date=date(2026, 7, 10),
        round_off=Decimal("-1.00"),
    )
    position = InvoicePosition(
        row=row, linked_cn_amount=Decimal("0.00"), receipts_applied=Decimal("0.00"),
        open_amount=Decimal("1179.00"), is_overdue=False, days_past_due=0, ageing_bucket="Current",
    )
    display_rows = [
        {"row": row, "position": position, "linked_cn_no": "", "follow_up": None,
         "ptp_status": "", "grouping": None}
    ]
    wb = build_sales_dn_register_workbook(display_rows, as_of=date(2026, 9, 12))
    ws = wb.active
    header_row = [c.value for c in ws[3]]
    round_off_col = header_row.index("Round Off") + 1
    assert ws.cell(row=4, column=round_off_col).value == -1.00


def test_credit_note_register_workbook_contains_headers_and_row_values():
    row = CreditNoteRegisterRow(
        branch_id="MUN", cn_date=date(2026, 7, 20), voucher_number="CN/01", party_id="BIHAR-FC",
        cn_amount=Decimal("5000.00"), bill_allocation_reference="INV/002",
        classification=RegisterClassification.CURRENT,
    )
    display_rows = [{"row": row, "unapplied_amount": None}]

    wb = build_credit_note_register_workbook(display_rows)
    ws = wb.active
    assert ws.title == "Credit Note Register"
    header_row = [c.value for c in ws[1]]
    assert "CN Number" in header_row

    data_row = [c.value for c in ws[2]]
    assert "CN/01" in data_row
    assert "INV/002" in data_row
    assert 5000.0 in data_row
    assert "Current" in data_row


def test_credit_note_register_workbook_unapplied_amount_blank_when_allocated():
    row = CreditNoteRegisterRow(
        branch_id="MUN", cn_date=date(2026, 7, 20), voucher_number="CN/01", party_id="BIHAR-FC",
        cn_amount=Decimal("5000.00"), bill_allocation_reference="INV/002",
    )
    wb = build_credit_note_register_workbook([{"row": row, "unapplied_amount": None}])
    data_row = [c.value for c in wb.active[2]]
    unapplied_col_index = [c.value for c in wb.active[1]].index("Open/Unapplied CN Amount")
    assert data_row[unapplied_col_index] is None


def test_receipt_journal_register_workbook_contains_headers_and_row_values():
    row = ReceiptJournalRegisterRow(
        branch_id="MUN", txn_date=date(2026, 7, 1), voucher_type="Receipt", voucher_number="RCPT/01",
        party_id="GIRIDHAN", amount=Decimal("200000.00"), target_doc_no="INV/001",
    )
    fields = ReceiptJournalDisplayFields(dpd_at_application=0, invoice_fin_year="2026-27", age_unapplied_days=None)
    display_rows = [{"row": row, "fields": fields}]

    wb = build_receipt_journal_register_workbook(display_rows, as_of=date(2026, 9, 12))
    ws = wb.active
    assert ws.title == "Receipt & Journal Register"
    header_row = [c.value for c in ws[3]]
    assert "Target Doc No." in header_row

    data_row = [c.value for c in ws[4]]
    assert "RCPT/01" in data_row
    assert "INV/001" in data_row
    assert 200000.0 in data_row
    assert "2026-27" in data_row


def test_workbooks_handle_zero_rows_without_error():
    assert build_sales_dn_register_workbook([], as_of=date(2026, 9, 12)) is not None
    assert build_credit_note_register_workbook([]) is not None
    assert build_receipt_journal_register_workbook([], as_of=date(2026, 9, 12)) is not None


# ---- Indian number formatting (client's explicit ask) ---------------------


def test_sales_dn_register_workbook_applies_indian_format_to_amount_columns_only():
    row = _sales_dn_row()
    position = InvoicePosition(
        row=row, linked_cn_amount=Decimal("0.00"), receipts_applied=Decimal("200000.00"),
        open_amount=Decimal("264300.00"), is_overdue=True, days_past_due=64, ageing_bucket="61-90",
    )
    display_rows = [
        {"row": row, "position": position, "linked_cn_no": "", "follow_up": None,
         "ptp_status": "", "grouping": None}
    ]
    wb = build_sales_dn_register_workbook(display_rows, as_of=date(2026, 9, 12))
    ws = wb.active
    header_row = [c.value for c in ws[3]]

    taxable_col = header_row.index("Taxable Value") + 1
    assert ws.cell(row=4, column=taxable_col).number_format == "#,##,##0.00"

    # A day count and a text column must keep their own/default format -
    # never the currency format, even though they're on the same row.
    dpd_col = header_row.index("DPD") + 1
    assert ws.cell(row=4, column=dpd_col).number_format != "#,##,##0.00"
    voucher_col = header_row.index("Voucher No.") + 1
    assert ws.cell(row=4, column=voucher_col).number_format != "#,##,##0.00"


def test_credit_note_register_workbook_applies_indian_format_to_cn_amount():
    row = CreditNoteRegisterRow(
        branch_id="MUN", cn_date=date(2026, 7, 20), voucher_number="CN/01", party_id="BIHAR-FC",
        cn_amount=Decimal("5000.00"), bill_allocation_reference="INV/002",
    )
    wb = build_credit_note_register_workbook([{"row": row, "unapplied_amount": None}])
    ws = wb.active
    header_row = [c.value for c in ws[1]]
    cn_amount_col = header_row.index("CN Amount") + 1
    assert ws.cell(row=2, column=cn_amount_col).number_format == "#,##,##0.00"


def test_receipt_journal_register_workbook_applies_indian_format_to_applied_amount():
    row = ReceiptJournalRegisterRow(
        branch_id="MUN", txn_date=date(2026, 7, 1), voucher_type="Receipt", voucher_number="RCPT/01",
        party_id="GIRIDHAN", amount=Decimal("200000.00"), target_doc_no="INV/001",
    )
    fields = ReceiptJournalDisplayFields(dpd_at_application=0, invoice_fin_year="2026-27", age_unapplied_days=None)
    wb = build_receipt_journal_register_workbook([{"row": row, "fields": fields}], as_of=date(2026, 9, 12))
    ws = wb.active
    header_row = [c.value for c in ws[3]]
    applied_col = header_row.index("Applied Amount") + 1
    assert ws.cell(row=4, column=applied_col).number_format == "#,##,##0.00"
