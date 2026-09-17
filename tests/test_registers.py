from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ar_mis.models import (
    CreditNoteRegisterRow,
    CustomerMasterRecord,
    InvoiceFollowUp,
    LedgerEntry,
    NoteType,
    ReceiptJournalRegisterRow,
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
    compute_collection_efficiency,
    compute_collections_in_window,
    compute_dso,
    compute_due_date,
    compute_invoice_position,
    compute_linked_cn_reference_text,
    compute_ptp_kept_rate,
    compute_ptp_outcome,
    compute_receipt_journal_display_fields,
    compute_sales_in_window,
    compute_unapplied_cash_by_party,
    financial_year_label,
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


def test_classify_tax_ledger_bare_names():
    assert classify_tax_ledger("CGST") == "CGST"
    assert classify_tax_ledger(" sgst ") == "SGST"
    assert classify_tax_ledger("IGST") == "IGST"
    assert classify_tax_ledger("Freight") is None


def test_classify_tax_ledger_matches_real_naming_variants():
    # Confirmed live (CHARZE INDUSTRIES real data): actual tax ledgers are
    # named "OUTPUT CGST 9%"/"OUTPUT SGST 9%", not a bare "CGST"/"SGST" -
    # an exact match silently dumped these into taxable_value instead of
    # cgst/sgst, with the real Sales amount separately missing entirely
    # (see the ALLINVENTORYENTRIES.LIST fallback fix in parsers.py).
    assert classify_tax_ledger("OUTPUT CGST 9%") == "CGST"
    assert classify_tax_ledger("OUTPUT SGST 9%") == "SGST"
    assert classify_tax_ledger("OUTPUT IGST 18%") == "IGST"


def test_classify_tax_ledger_rejects_gst_suffixed_revenue_ledgers():
    # Confirmed against real data (a different real client): many revenue
    # ledgers are "_GST"-suffixed and must NOT be caught - matching on the
    # specific "cgst"/"sgst"/"igst" token, never the generic "gst", is what
    # keeps these two real, opposite requirements both satisfied.
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
    assert row.round_off == Decimal("0.00")
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
    assert row.round_off == Decimal("-1.00")


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
    assert exceptions.unattributable_party == [("INV999", "", "Sales", "No PARTYLEDGERNAME on this voucher - cannot attribute to a customer")]


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
    row = build_credit_note_register_row(voucher, {"ACME"}, lookup, RegisterBuildExceptions())
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
    row = build_credit_note_register_row(voucher, {"ACME"}, lookup, RegisterBuildExceptions())
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
    row = build_credit_note_register_row(voucher, {"ACME"}, lookup, RegisterBuildExceptions())
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
    row = build_credit_note_register_row(voucher, {"ACME"}, {}, RegisterBuildExceptions())
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
    row = build_credit_note_register_row(voucher, set(), {}, exceptions)
    assert row is None
    assert len(exceptions.unattributable_party) == 1


def test_credit_note_party_not_a_tracked_debtor_is_flagged_not_guessed():
    # A supplier-side credit note: Tally uses the same voucher type, and
    # the top-level PARTYLEDGERNAME names the supplier - which is never
    # on the real Sundry Debtors list. Must be flagged, never silently
    # attributed as if it were a customer transaction.
    voucher = Voucher(
        voucher_type=VoucherType.CREDIT_NOTE,
        voucher_date=date(2026, 4, 15),
        voucher_number="CN006",
        branch_id="B1",
        party_ledger_name="Some Supplier Pvt Ltd",
        entries=[
            LedgerEntry(party_ledger_name="Some Supplier Pvt Ltd", amount_as_extracted=Decimal("200.00")),
            LedgerEntry(party_ledger_name="Purchase Returns", amount_as_extracted=Decimal("-200.00")),
        ],
    )
    exceptions = RegisterBuildExceptions()
    row = build_credit_note_register_row(voucher, {"ACME"}, {}, exceptions)
    assert row is None
    assert len(exceptions.unattributable_party) == 1


def test_credit_note_rejects_wrong_voucher_type():
    voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=date(2026, 4, 10),
        voucher_number="X", branch_id="B1", party_ledger_name="ACME",
    )
    with pytest.raises(ValueError, match="not Credit Note"):
        build_credit_note_register_row(voucher, set(), {}, RegisterBuildExceptions())


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
    rows = build_receipt_journal_register_rows(voucher, {"ACME"}, lookup, RegisterBuildExceptions())
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
    rows = build_receipt_journal_register_rows(voucher, {"ACME"}, lookup, RegisterBuildExceptions())
    assert all(r.classification == RegisterClassification.CURRENT for r in rows)


def test_receipt_journal_missing_party_returns_empty_and_flags():
    exceptions = RegisterBuildExceptions()
    voucher = Voucher(
        voucher_type=VoucherType.RECEIPT, voucher_date=date(2026, 4, 20),
        voucher_number="R2", branch_id="B1", party_ledger_name="",
    )
    rows = build_receipt_journal_register_rows(voucher, set(), {}, exceptions)
    assert rows == []
    assert len(exceptions.unattributable_party) == 1


def test_receipt_journal_debtor_creditor_journal_attributes_to_the_debtor_leg():
    # The exact live-reproduced case: Tally tags the CREDITOR as this
    # Journal's top-level party, but one entry genuinely touches a
    # tracked debtor. The register must show the debtor's line, not the
    # creditor's name, and never drop the debtor leg silently.
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        voucher_date=date(2026, 4, 3),
        voucher_number="JV9002",
        branch_id="B1",
        party_ledger_name="Some Creditor Pvt Ltd",
        entries=[
            LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-30000.00")),
            LedgerEntry(party_ledger_name="Some Creditor Pvt Ltd", amount_as_extracted=Decimal("30000.00")),
        ],
    )
    rows = build_receipt_journal_register_rows(voucher, {"ACME"}, {}, RegisterBuildExceptions())
    assert len(rows) == 1
    assert rows[0].party_id == "ACME"
    assert rows[0].amount == Decimal("30000.00")


def test_receipt_journal_creditor_only_journal_is_flagged_not_recorded():
    # Neither leg touches a tracked debtor at all (a creditor/loan
    # adjustment) - must be excluded entirely, not attributed to whoever
    # Tally happens to call "the party".
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        voucher_date=date(2026, 4, 3),
        voucher_number="JV9001",
        branch_id="B1",
        party_ledger_name="Some Creditor Pvt Ltd",
        entries=[
            LedgerEntry(party_ledger_name="Some Creditor Pvt Ltd", amount_as_extracted=Decimal("-50000.00")),
            LedgerEntry(party_ledger_name="Bank Loan Account", amount_as_extracted=Decimal("50000.00")),
        ],
    )
    exceptions = RegisterBuildExceptions()
    rows = build_receipt_journal_register_rows(voucher, {"ACME"}, {}, exceptions)
    assert rows == []
    assert len(exceptions.unattributable_party) == 1


def test_receipt_journal_rejects_wrong_voucher_type():
    voucher = Voucher(
        voucher_type=VoucherType.SALES, voucher_date=date(2026, 4, 10),
        voucher_number="X", branch_id="B1", party_ledger_name="ACME",
    )
    with pytest.raises(ValueError):
        build_receipt_journal_register_rows(voucher, set(), {}, RegisterBuildExceptions())


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
    # Live-confirmed real bug: this exact scenario (paid off, well past its
    # own due date) used to still show the raw day count here (139 in the
    # real case) instead of 0 - this test never checked the field that was
    # actually wrong, which is exactly how it went unnoticed.
    assert position.days_past_due == 0


def test_compute_invoice_position_before_invoice_date_contributes_nothing():
    # Found via a real cross-check building the AR Snapshot dashboard:
    # an as-of date earlier than the invoice's own invoice_date must not
    # show the invoice's full value as "open" - it didn't exist yet.
    invoice = build_sales_dn_register_row(
        sales_voucher(
            [
                LedgerEntry(party_ledger_name="ACME", amount_as_extracted=Decimal("-1000.00"), bill_name="INV001", bill_type="New Ref"),
                LedgerEntry(party_ledger_name="Sales Revenue", amount_as_extracted=Decimal("1000.00")),
            ],
            voucher_date=date(2026, 6, 15),
        ),
        CUSTOMER,
        RegisterBuildExceptions(),
    )
    position = compute_invoice_position(invoice, [], [], as_of=date(2026, 6, 14))
    assert position.open_amount == Decimal("0.00")
    assert position.linked_cn_amount == Decimal("0.00")
    assert position.receipts_applied == Decimal("0.00")
    assert position.is_overdue is False
    assert position.days_past_due == 0

    # The day it's actually raised, it appears normally.
    position_on_date = compute_invoice_position(invoice, [], [], as_of=date(2026, 6, 15))
    assert position_on_date.open_amount == Decimal("1000.00")


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
    # Every real voucher in this fixture set that survives parsing (custom
    # voucher-type names recognized, cancelled vouchers already excluded
    # by parse_voucher_collection itself) has a real party - none left
    # unattributable.
    assert len(exceptions.unattributable_party) == 0
    assert len(sales_dn_rows) == len(sales_vouchers) + len(dn_vouchers)

    lookup = build_bill_reference_lookup(sales_dn_rows)
    # Every real party name mentioned anywhere in this real fixture set -
    # standing in for customer_master in this pure-function test (the
    # actual pipeline builds this from customer_master; see
    # pipeline._build_and_persist_registers).
    tracked_party_names = {
        v.party_ledger_name
        for v in sales_vouchers + dn_vouchers + cn_vouchers + receipt_vouchers + journal_vouchers
        if v.party_ledger_name
    }

    cn_rows = [
        row
        for v in cn_vouchers
        if (row := build_credit_note_register_row(v, tracked_party_names, lookup, exceptions)) is not None
    ]
    assert len(cn_rows) == len(cn_vouchers)

    rj_rows = []
    for v in receipt_vouchers + journal_vouchers:
        rj_rows.extend(build_receipt_journal_register_rows(v, tracked_party_names, lookup, exceptions))
    assert len(rj_rows) > 0

    # Previously a known gap (8 real vouchers carried only the party's own
    # ledger entry, no offsetting revenue/tax entry at all, so this
    # function's taxable_value+cgst+sgst+igst+round_off sum came out zero
    # even though the party really was debited a real amount) - fixed by
    # parse_voucher_collection's inventory-allocation fallback. No zero-
    # value rows should remain anywhere in this real fixture set.
    assert all(row.invoice_value != Decimal("0.00") for row in sales_dn_rows)


# ---- KPI formulas (design doc item 15) -----------------------------------


def _invoice(voucher_number="INV001", invoice_date=date(2026, 1, 1), value=Decimal("1000.00")):
    return build_sales_dn_register_row(
        Voucher(
            voucher_type=VoucherType.SALES, voucher_date=invoice_date, voucher_number=voucher_number,
            branch_id="B1", party_ledger_name="ACME",
            entries=[
                LedgerEntry(party_ledger_name="ACME", amount_as_extracted=-value, bill_name=voucher_number, bill_type="New Ref"),
                LedgerEntry(party_ledger_name="Sales Revenue", amount_as_extracted=value),
            ],
        ),
        CUSTOMER,
        RegisterBuildExceptions(),
    )


def test_compute_sales_in_window_sums_only_invoices_inside_the_window():
    rows = [
        _invoice("INV001", date(2026, 1, 1), Decimal("1000.00")),
        _invoice("INV002", date(2026, 2, 15), Decimal("500.00")),
        _invoice("INV003", date(2026, 3, 1), Decimal("2000.00")),  # outside the window
    ]
    total = compute_sales_in_window(rows, date(2026, 1, 1), date(2026, 2, 28))
    assert total == Decimal("1500.00")


def test_compute_collections_in_window_excludes_non_current_and_out_of_range():
    rows = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 1, 10), voucher_type="Receipt",
            voucher_number="R1", party_id="ACME", amount=Decimal("400.00"),
        ),
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 1, 20), voucher_type="Receipt",
            voucher_number="R2", party_id="ACME", amount=Decimal("300.00"),
            classification=RegisterClassification.PENDING_REVIEW,
        ),
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 3, 1), voucher_type="Receipt",
            voucher_number="R3", party_id="ACME", amount=Decimal("999.00"),
        ),
    ]
    total = compute_collections_in_window(rows, date(2026, 1, 1), date(2026, 1, 31))
    assert total == Decimal("400.00")


def test_compute_dso_standard_formula():
    # 90 lakh open AR, 9 lakh sold in the trailing 90 days -> 10/day -> 90 days.
    dso = compute_dso(total_open_ar=Decimal("9000000"), sales_last_90_days=Decimal("900000"))
    assert dso == Decimal("900")


def test_compute_dso_is_none_when_no_sales_in_window():
    assert compute_dso(total_open_ar=Decimal("50000"), sales_last_90_days=Decimal("0")) is None


def test_compute_collection_efficiency_standard_formula():
    # Owed = 100000 opening + 50000 new sales = 150000; collected 120000 -> 80%.
    pct = compute_collection_efficiency(
        opening_ar=Decimal("100000"), sales_in_period=Decimal("50000"), collected_in_period=Decimal("120000")
    )
    assert pct == Decimal("80")


def test_compute_collection_efficiency_is_none_when_nothing_was_owed():
    assert compute_collection_efficiency(Decimal("0"), Decimal("0"), Decimal("0")) is None


def test_ptp_outcome_kept_when_promised_amount_paid_since_the_promise_was_logged():
    invoice = _invoice("INV001", date(2026, 1, 1), Decimal("100000.00"))
    follow_up = InvoiceFollowUp(
        branch_id="B1", voucher_number="INV001", party_id="ACME",
        ptp_date=date(2026, 2, 15), ptp_amount=Decimal("60000.00"), logged_at=date(2026, 2, 1),
    )
    receipts = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 2, 10), voucher_type="Receipt",
            voucher_number="R1", party_id="ACME", amount=Decimal("60000.00"), target_doc_no="INV001",
        ),
    ]
    outcome = compute_ptp_outcome(invoice, follow_up, [], receipts)
    assert outcome.kept is True
    assert outcome.amount_collected_since_promise == Decimal("60000.00")


def test_ptp_outcome_ignores_money_collected_before_the_promise_was_logged():
    # Client's own worked example scenario, inverted: the invoice already
    # had SOME payment before the promise existed - that must not count
    # toward keeping a later, separate promise.
    invoice = _invoice("INV001", date(2026, 1, 1), Decimal("100000.00"))
    follow_up = InvoiceFollowUp(
        branch_id="B1", voucher_number="INV001", party_id="ACME",
        ptp_date=date(2026, 2, 15), ptp_amount=Decimal("60000.00"), logged_at=date(2026, 2, 1),
    )
    receipts = [
        # Paid BEFORE the promise was logged - must not count.
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 1, 15), voucher_type="Receipt",
            voucher_number="R1", party_id="ACME", amount=Decimal("50000.00"), target_doc_no="INV001",
        ),
        # Paid AFTER logging but short of the promised amount.
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 2, 10), voucher_type="Receipt",
            voucher_number="R2", party_id="ACME", amount=Decimal("20000.00"), target_doc_no="INV001",
        ),
    ]
    outcome = compute_ptp_outcome(invoice, follow_up, [], receipts)
    assert outcome.amount_collected_since_promise == Decimal("20000.00")
    assert outcome.kept is False


def test_ptp_outcome_kept_even_though_rest_of_invoice_still_open():
    # The client's exact worked example: ₹1,00,000 invoice, promise to pay
    # ₹60,000 by the 15th, exactly ₹60,000 paid, ₹40,000 left open. Kept.
    invoice = _invoice("INV001", date(2026, 1, 1), Decimal("100000.00"))
    follow_up = InvoiceFollowUp(
        branch_id="B1", voucher_number="INV001", party_id="ACME",
        ptp_date=date(2026, 1, 15), ptp_amount=Decimal("60000.00"), logged_at=date(2026, 1, 5),
    )
    receipts = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 1, 15), voucher_type="Receipt",
            voucher_number="R1", party_id="ACME", amount=Decimal("60000.00"), target_doc_no="INV001",
        ),
    ]
    outcome = compute_ptp_outcome(invoice, follow_up, [], receipts)
    assert outcome.kept is True
    position = compute_invoice_position(invoice, [], receipts, as_of=date(2026, 1, 15))
    assert position.open_amount == Decimal("40000.00")  # the rest is still open, and that's fine


def test_ptp_outcome_returns_none_when_no_promise_logged():
    invoice = _invoice()
    follow_up = InvoiceFollowUp(branch_id="B1", voucher_number="INV001", party_id="ACME")
    assert compute_ptp_outcome(invoice, follow_up, [], []) is None


def test_ptp_kept_rate_excludes_promises_not_yet_due():
    invoice = _invoice("INV001", date(2026, 1, 1), Decimal("100000.00"))
    lookup = {("B1", "INV001", "ACME"): invoice}
    follow_ups = [
        InvoiceFollowUp(branch_id="B1", voucher_number="INV001", party_id="ACME",
                         ptp_date=date(2026, 6, 1), ptp_amount=Decimal("60000.00"), logged_at=date(2026, 5, 1)),
    ]
    # as_of is before the promised date - not due yet, must be excluded entirely.
    rate = compute_ptp_kept_rate(follow_ups, lookup, [], [], as_of=date(2026, 3, 1))
    assert rate is None


def test_ptp_kept_rate_percentage_across_multiple_promises():
    invoice1 = _invoice("INV001", date(2026, 1, 1), Decimal("100000.00"))
    invoice2 = _invoice("INV002", date(2026, 1, 1), Decimal("50000.00"))
    lookup = {("B1", "INV001", "ACME"): invoice1, ("B1", "INV002", "ACME"): invoice2}
    follow_ups = [
        # Kept.
        InvoiceFollowUp(branch_id="B1", voucher_number="INV001", party_id="ACME",
                         ptp_date=date(2026, 2, 1), ptp_amount=Decimal("100000.00"), logged_at=date(2026, 1, 20)),
        # Broken - nothing paid.
        InvoiceFollowUp(branch_id="B1", voucher_number="INV002", party_id="ACME",
                         ptp_date=date(2026, 2, 1), ptp_amount=Decimal("50000.00"), logged_at=date(2026, 1, 20)),
    ]
    receipts = [
        ReceiptJournalRegisterRow(
            branch_id="B1", txn_date=date(2026, 1, 25), voucher_type="Receipt",
            voucher_number="R1", party_id="ACME", amount=Decimal("100000.00"), target_doc_no="INV001",
        ),
    ]
    rate = compute_ptp_kept_rate(follow_ups, lookup, [], receipts, as_of=date(2026, 3, 1))
    assert rate == Decimal("50")


# ---- Register-view presentation helpers (design doc item 2) --------------


def test_financial_year_label():
    assert financial_year_label(date(2026, 4, 1)) == "2026-27"
    assert financial_year_label(date(2026, 3, 31)) == "2025-26"


def test_linked_cn_reference_text_blank_when_none_linked():
    invoice = _invoice("INV001", date(2026, 1, 1), Decimal("1000.00"))
    assert compute_linked_cn_reference_text(invoice, [], as_of=date(2026, 2, 1)) == ""


def test_linked_cn_reference_text_shows_the_single_cn_number():
    invoice = _invoice("INV001", date(2026, 1, 1), Decimal("1000.00"))
    cn = CreditNoteRegisterRow(
        branch_id="B1", cn_date=date(2026, 1, 15), voucher_number="CN/01",
        party_id="ACME", cn_amount=Decimal("200.00"), bill_allocation_reference="INV001",
    )
    assert compute_linked_cn_reference_text(invoice, [cn], as_of=date(2026, 2, 1)) == "CN/01"


def test_linked_cn_reference_text_multiple_cns():
    invoice = _invoice("INV001", date(2026, 1, 1), Decimal("1000.00"))
    cns = [
        CreditNoteRegisterRow(branch_id="B1", cn_date=date(2026, 1, 15), voucher_number="CN/01",
                               party_id="ACME", cn_amount=Decimal("100.00"), bill_allocation_reference="INV001"),
        CreditNoteRegisterRow(branch_id="B1", cn_date=date(2026, 1, 20), voucher_number="CN/02",
                               party_id="ACME", cn_amount=Decimal("100.00"), bill_allocation_reference="INV001"),
    ]
    assert compute_linked_cn_reference_text(invoice, cns, as_of=date(2026, 2, 1)) == "Multiple credit notes issued"


def test_linked_cn_reference_text_excludes_cns_after_as_of_and_non_current():
    invoice = _invoice("INV001", date(2026, 1, 1), Decimal("1000.00"))
    cns = [
        CreditNoteRegisterRow(branch_id="B1", cn_date=date(2026, 6, 1), voucher_number="CN/LATE",
                               party_id="ACME", cn_amount=Decimal("100.00"), bill_allocation_reference="INV001"),
        CreditNoteRegisterRow(branch_id="B1", cn_date=date(2026, 1, 15), voucher_number="CN/PENDING",
                               party_id="ACME", cn_amount=Decimal("100.00"), bill_allocation_reference="INV001",
                               classification=RegisterClassification.PENDING_REVIEW),
    ]
    assert compute_linked_cn_reference_text(invoice, cns, as_of=date(2026, 2, 1)) == ""


def test_receipt_journal_display_fields_for_applied_line():
    invoice = _invoice("INV001", date(2026, 1, 1), Decimal("1000.00"))  # due 2026-01-31
    lookup = {("ACME", "INV001"): invoice}
    row = ReceiptJournalRegisterRow(
        branch_id="B1", txn_date=date(2026, 2, 5), voucher_type="Receipt",
        voucher_number="R1", party_id="ACME", amount=Decimal("1000.00"), target_doc_no="INV001",
    )
    fields = compute_receipt_journal_display_fields(row, lookup, as_of=date(2026, 3, 1))
    assert fields.dpd_at_application == 5  # paid 5 days after the Jan 31 due date
    assert fields.invoice_fin_year == "2025-26"
    assert fields.age_unapplied_days is None


def test_receipt_journal_display_fields_for_unapplied_line():
    row = ReceiptJournalRegisterRow(
        branch_id="B1", txn_date=date(2026, 2, 5), voucher_type="Receipt",
        voucher_number="R1", party_id="ACME", amount=Decimal("1000.00"), target_doc_no=None,
    )
    fields = compute_receipt_journal_display_fields(row, {}, as_of=date(2026, 3, 1))
    assert fields.dpd_at_application is None
    assert fields.invoice_fin_year is None
    assert fields.age_unapplied_days == 24  # 2026-02-05 to 2026-03-01


def test_receipt_journal_display_fields_for_unresolved_reference():
    row = ReceiptJournalRegisterRow(
        branch_id="B1", txn_date=date(2026, 2, 5), voucher_type="Receipt",
        voucher_number="R1", party_id="ACME", amount=Decimal("1000.00"), target_doc_no="INV-UNKNOWN",
        classification=RegisterClassification.PENDING_REVIEW,
    )
    fields = compute_receipt_journal_display_fields(row, {}, as_of=date(2026, 3, 1))
    assert fields.dpd_at_application is None
    assert fields.invoice_fin_year is None
    assert fields.age_unapplied_days is None
