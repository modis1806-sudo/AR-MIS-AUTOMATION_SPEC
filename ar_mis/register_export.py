"""Excel export for the three master registers (docs/registers_and_
reporting_design.md item 2), reached from the webapp's Registers screens.

Each build_*_workbook() function takes the exact same `display_rows`
list-of-dicts structure the corresponding webapp route already builds
for its Jinja template - the export is a second rendering of the same
computed data, never a separate recomputation, so what a user downloads
can never silently drift from what they were just looking at on screen.
"""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet

_HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
_HEADER_FONT = Font(bold=True, color="FFFFFF")


def _header_row(ws: Worksheet, headers: list[str]) -> None:
    ws.append(headers)
    for cell in ws[ws.max_row]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT


def _autosize(ws: Worksheet) -> None:
    for col_cells in ws.columns:
        length = max((len(str(c.value)) if c.value is not None else 0) for c in col_cells)
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 10), 45)


_SALES_DN_HEADERS = [
    "Branch", "Date", "Type", "Voucher No.", "Bill Allocation Reference", "Job ID", "Customer",
    "Grouping", "Taxable Value", "CGST", "SGST", "IGST", "Invoice Value", "Due Date",
    "Linked CN No.", "Linked CN Amount", "Net Receivable", "Receipts Applied", "Open Amount",
    "Overdue", "DPD", "Ageing Bucket", "PTP Date", "PTP Amount", "PTP Status", "Next Action",
    "Expected Collection Date",
]


def build_sales_dn_register_workbook(display_rows: list[dict], as_of) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales & DN Register"
    ws.append([f"Sales & Debit Note Register - position as of {as_of}"])
    ws["A1"].font = Font(bold=True, size=12)
    ws.append([])
    _header_row(ws, _SALES_DN_HEADERS)

    for d in display_rows:
        row, pos, fu = d["row"], d["position"], d["follow_up"]
        ws.append(
            [
                row.branch_id, row.invoice_date, row.note_type.value, row.voucher_number,
                row.bill_allocation_reference, row.job_id or "", row.party_id, d["grouping"] or "",
                float(row.taxable_value), float(row.cgst), float(row.sgst), float(row.igst),
                float(row.invoice_value), row.due_date, d["linked_cn_no"] or "",
                float(pos.linked_cn_amount), float(row.invoice_value - pos.linked_cn_amount),
                float(pos.receipts_applied), float(pos.open_amount), "Yes" if pos.is_overdue else "No",
                pos.days_past_due, pos.ageing_bucket,
                fu.ptp_date if fu else None, float(fu.ptp_amount) if fu and fu.ptp_amount is not None else None,
                d["ptp_status"] or "", (fu.next_action if fu else "") or "",
                fu.expected_collection_date if fu else None,
            ]
        )
    _autosize(ws)
    return wb


_CREDIT_NOTE_HEADERS = [
    "Branch", "Date", "Customer", "CN Number", "Original Invoice/DN Ref", "CN Amount",
    "Open/Unapplied CN Amount", "Classification",
]


def build_credit_note_register_workbook(display_rows: list[dict]) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Credit Note Register"
    _header_row(ws, _CREDIT_NOTE_HEADERS)

    for d in display_rows:
        row = d["row"]
        ws.append(
            [
                row.branch_id, row.cn_date, row.party_id, row.voucher_number,
                row.bill_allocation_reference or "", float(row.cn_amount),
                float(d["unapplied_amount"]) if d["unapplied_amount"] is not None else None,
                row.classification.value,
            ]
        )
    _autosize(ws)
    return wb


_RECEIPT_JOURNAL_HEADERS = [
    "Branch", "Date", "Voucher Type", "Voucher No.", "Customer", "Allocation Type", "Target Doc No.",
    "Applied Amount", "Unapplied Balance", "Classification", "DPD at Application", "Age Unapplied Days",
    "Invoice Fin Year", "Narration",
]


def build_receipt_journal_register_workbook(display_rows: list[dict], as_of) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Receipt & Journal Register"
    ws.append([f"Receipt & Journal Register - Age Unapplied Days as of {as_of}"])
    ws["A1"].font = Font(bold=True, size=12)
    ws.append([])
    _header_row(ws, _RECEIPT_JOURNAL_HEADERS)

    for d in display_rows:
        row, f = d["row"], d["fields"]
        ws.append(
            [
                row.branch_id, row.txn_date, row.voucher_type, row.voucher_number, row.party_id,
                "Against Ref" if row.target_doc_no else "Unapplied", row.target_doc_no or "",
                float(row.amount) if row.target_doc_no else None,
                float(row.amount) if not row.target_doc_no else None,
                row.classification.value, f.dpd_at_application, f.age_unapplied_days, f.invoice_fin_year or "",
                row.narration or "",
            ]
        )
    _autosize(ws)
    return wb
