"""Local web application: Extraction (Branch Master, Test Extraction,
Discover Companies, Manual Upload - all Preparer-only), Registers (the
Sales & DN, Credit Note, and Receipt & Journal registers plus Customer
Master, viewable by anyone), and Reports (still a placeholder - the
report catalog itself, docs/registers_and_reporting_design.md item 15,
is separately pending).

Runs on localhost only, matching the spec's LAN-only/no-cloud-dependency
framing - this is an internal single-operator tool, not a public service.

Role gating is a deliberate PLACEHOLDER, not real security (client's
explicit choice - see docs/registers_and_reporting_design.md's role
decision): a role picker sets a plain session value with no password
behind it, so it hides the Extraction section from a Viewer (you/CFO)
in the UI and blocks its routes server-side, but anyone with access to
this machine can still pick "Preparer" themselves. Real login (item 11,
"non-negotiable" once the full AR team is using this) is separate,
later work - this only needs to hold up while it's the client's own
team testing on a private machine.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import wraps
from io import BytesIO

from flask import Flask, flash, redirect, render_template, request, send_file, session, url_for

from ar_mis.config import BranchConfig, financial_year_start
from ar_mis.dashboard import compute_ar_snapshot, compute_branch_ageing_schedule
from ar_mis.manual_upload import WEEKLY_VOUCHER_SLOTS, ManualUploadRefused, process_manual_upload
from ar_mis.models import RegisterClassification
from ar_mis.register_export import (
    build_credit_note_register_workbook,
    build_receipt_journal_register_workbook,
    build_sales_dn_register_workbook,
)
from ar_mis.registers import (
    build_bill_reference_lookup,
    compute_invoice_position,
    compute_linked_cn_reference_text,
    compute_ptp_kept_rate,
    compute_ptp_outcome,
    compute_receipt_journal_display_fields,
)
from ar_mis.storage import Store
from ar_mis.tally_client import CompanyMismatchError, TallyClient, TallyConnectionError

ROLES = {
    "preparer": "Preparer",
    "viewer": "Viewer",
}


def create_app(db_path: str = "data/ar_mis.db") -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.secret_key = "ar-mis-local-tool"  # localhost-only internal tool; no session security needed

    def get_store() -> Store:
        return Store(app.config["DB_PATH"])

    def _freshness(last_extracted: datetime | None) -> dict:
        """Client's explicit ask: an indicator of how old what's on screen
        is. `last_extracted` is the real wall-clock timestamp from
        store.last_extraction_at() (extraction_log), never the business
        week_ending a run covers - see that table's own docstring for why
        those two differ. Flagged "stale" past 7 days, matching the
        weekly extraction cadence this whole pipeline is built around: a
        register more than one cycle old is worth calling out, not just
        stating the date and leaving the reader to do that math.
        """
        if last_extracted is None:
            return {"label": "No data extracted yet", "css_class": "never"}
        age_days = (datetime.now() - last_extracted).days
        return {
            "label": f"Data last extracted: {last_extracted.strftime('%d %b %Y, %I:%M %p')}",
            "css_class": "stale" if age_days > 7 else "",
        }

    def requires_role(role: str):
        """Route-level enforcement of the role placeholder described in
        this module's docstring - the nav already hides Extraction links
        from a Viewer, but a link being hidden is not the same as a route
        being blocked, and the client was explicit that Extraction must
        not be reachable by a Viewer at all, not just tucked out of the
        menu.
        """

        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                if session.get("role") != role:
                    flash("This section isn't available for your role.", "error")
                    return redirect(url_for("home"))
                return view(*args, **kwargs)

            return wrapped

        return decorator

    @app.before_request
    def require_role_chosen():
        if request.endpoint in (None, "static", "choose_role") or session.get("role") in ROLES:
            return None
        return redirect(url_for("choose_role", next=request.path))

    @app.route("/choose-role", methods=["GET", "POST"])
    def choose_role():
        if request.method == "POST":
            role = request.form.get("role", "")
            if role not in ROLES:
                flash("Choose a role to continue.", "error")
                return render_template("choose_role.html", roles=ROLES)
            session["role"] = role
            next_path = request.form.get("next") or url_for("home")
            return redirect(next_path)
        return render_template("choose_role.html", roles=ROLES, next=request.args.get("next", ""))

    @app.route("/")
    def home():
        store = get_store()
        branch_count = len(store.list_branches())
        freshness = _freshness(store.last_extraction_at())
        store.close()
        return render_template("home.html", branch_count=branch_count, freshness=freshness)

    @app.route("/branches")
    @requires_role("preparer")
    def branches_list():
        store = get_store()
        branches = store.list_branches()
        store.close()
        return render_template("branches_list.html", branches=branches)

    @app.route("/branches/new", methods=["GET", "POST"])
    @requires_role("preparer")
    def branch_new():
        if request.method == "POST":
            branch_id = request.form.get("branch_id", "").strip()
            branch_name = request.form.get("branch_name", "").strip()
            tally_company_name = request.form.get("tally_company_name", "").strip()
            if not branch_id or not branch_name or not tally_company_name:
                flash("Branch ID, Branch Name and Tally Company Name are all required.", "error")
                return render_template("branch_form.html", branch=None)
            branch = BranchConfig(
                branch_id=branch_id,
                branch_name=branch_name,
                tally_company_name=tally_company_name,
                tally_host=request.form.get("tally_host", "").strip() or "localhost",
                tally_port=int(request.form.get("tally_port") or 9000),
            )
            store = get_store()
            if store.get_branch(branch.branch_id) is not None:
                store.close()
                flash(f"Branch ID '{branch.branch_id}' already exists.", "error")
                return render_template("branch_form.html", branch=branch)
            store.upsert_branch(branch)
            store.close()
            flash(f"Branch '{branch.branch_name}' added.", "success")
            return redirect(url_for("branches_list"))

        # A GET can arrive from Discover Companies with the company/host/
        # port already known - pre-fill those fields, but branch_id and
        # branch_name still need a human to name the branch.
        prefill = None
        if request.args.get("tally_company_name"):
            prefill = {
                "tally_company_name": request.args["tally_company_name"],
                "tally_host": request.args.get("tally_host", "localhost") or "localhost",
                "tally_port": request.args.get("tally_port", "9000") or "9000",
            }
        return render_template("branch_form.html", branch=None, prefill=prefill)

    @app.route("/branches/<branch_id>/edit", methods=["GET", "POST"])
    @requires_role("preparer")
    def branch_edit(branch_id):
        store = get_store()
        existing = store.get_branch(branch_id)
        if existing is None:
            store.close()
            flash(f"No such branch '{branch_id}'.", "error")
            return redirect(url_for("branches_list"))

        if request.method == "POST":
            branch_name = request.form.get("branch_name", "").strip()
            tally_company_name = request.form.get("tally_company_name", "").strip()
            if not branch_name or not tally_company_name:
                store.close()
                flash("Branch Name and Tally Company Name are required.", "error")
                return render_template("branch_form.html", branch=existing)
            updated = BranchConfig(
                branch_id=branch_id,
                branch_name=branch_name,
                tally_company_name=tally_company_name,
                tally_host=request.form.get("tally_host", "").strip() or "localhost",
                tally_port=int(request.form.get("tally_port") or 9000),
            )
            store.upsert_branch(updated)
            store.close()
            flash(f"Branch '{updated.branch_name}' updated.", "success")
            return redirect(url_for("branches_list"))

        store.close()
        return render_template("branch_form.html", branch=existing)

    @app.route("/branches/<branch_id>/delete", methods=["POST"])
    @requires_role("preparer")
    def branch_delete(branch_id):
        store = get_store()
        store.delete_branch(branch_id)
        store.close()
        flash("Branch removed.", "success")
        return redirect(url_for("branches_list"))

    @app.route("/test-extraction", methods=["GET", "POST"])
    @requires_role("preparer")
    def test_extraction():
        store = get_store()
        branches = store.list_branches()
        result = None
        default_to = date.today()
        default_from = default_to - timedelta(days=7)

        if request.method == "POST":
            branch_id = request.form.get("branch_id", "")
            branch = store.get_branch(branch_id)
            if branch is None:
                flash("Select a branch first.", "error")
                store.close()
                return render_template(
                    "test_extraction.html", branches=branches, result=None,
                    default_from=default_from.isoformat(), default_to=default_to.isoformat(),
                )

            # Any range the operator picks - a week, a month, a full
            # financial year for a first-time backfill. No artificial
            # min/max: this is a diagnostic tool, not the weekly production
            # cadence (which cli.py fixes to 7 days by design).
            try:
                from_date = date.fromisoformat(request.form["from_date"])
                to_date = date.fromisoformat(request.form["to_date"])
            except (KeyError, ValueError):
                flash("Enter valid From and To dates.", "error")
                store.close()
                return render_template(
                    "test_extraction.html", branches=branches, result=None,
                    default_from=default_from.isoformat(), default_to=default_to.isoformat(),
                )
            if from_date > to_date:
                flash("From Date must be on or before To Date.", "error")
                store.close()
                return render_template(
                    "test_extraction.html", branches=branches, result=None,
                    default_from=from_date.isoformat(), default_to=to_date.isoformat(),
                )

            steps: list[tuple[str, bool, str]] = []
            # Uses TallyClient's default timeout (see tally_client.
            # DEFAULT_TIMEOUT_SECONDS) - a fixed short timeout here
            # (an earlier version used 15s) is exactly what broke this
            # page against any real voucher-heavy range: a single 15-day
            # chunk can legitimately take close to a minute against a
            # busy company, confirmed via live-Tally diagnostic.
            client = TallyClient(branch=branch)

            try:
                client.confirm_current_company()
                steps.append(("Company check", True, f"'{branch.tally_company_name}' is currently loaded"))
            except CompanyMismatchError as exc:
                steps.append(("Company check", False, str(exc)))
            except TallyConnectionError as exc:
                steps.append(("Connection", False, str(exc)))

            all_ok_so_far = all(ok for _, ok, _ in steps)

            if all_ok_so_far:
                try:
                    vouchers_by_type = client.fetch_all_voucher_types(from_date, to_date)
                    counts = ", ".join(f"{vt.value}: {len(vs)}" for vt, vs in vouchers_by_type.items())
                    steps.append(("Voucher extraction", True, counts or "0 vouchers in range"))
                except TallyConnectionError as exc:
                    steps.append(("Voucher extraction", False, str(exc)))

            all_ok_so_far = all(ok for _, ok, _ in steps)

            if all_ok_so_far:
                try:
                    balances = client.fetch_ytd_sundry_debtors(from_date, to_date)
                    steps.append(("Sundry Debtors pull", True, f"{len(balances)} ledger(s) found"))
                except TallyConnectionError as exc:
                    steps.append(("Sundry Debtors pull", False, str(exc)))

            result = {
                "branch": branch, "steps": steps, "all_ok": all(ok for _, ok, _ in steps),
                "from_date": from_date.isoformat(), "to_date": to_date.isoformat(),
            }
            default_from, default_to = from_date, to_date

        store.close()
        return render_template(
            "test_extraction.html", branches=branches, result=result,
            default_from=default_from.isoformat(), default_to=default_to.isoformat(),
        )

    @app.route("/discover", methods=["GET", "POST"])
    @requires_role("preparer")
    def discover_companies():
        """Asks Tally what companies are open right now, so a company name
        can be picked rather than typed from memory - the source of a real
        production incident (a company that was open when this project
        started had moved on by the time testing caught up with it).
        """
        host = request.form.get("tally_host", "localhost") if request.method == "POST" else "localhost"
        port = request.form.get("tally_port", "9000") if request.method == "POST" else "9000"
        result = None

        if request.method == "POST":
            probe_branch = BranchConfig(
                branch_id="_probe",
                branch_name="_probe",
                tally_company_name="",  # irrelevant - this request doesn't target one company
                tally_host=host or "localhost",
                tally_port=int(port or 9000),
            )
            client = TallyClient(branch=probe_branch)
            try:
                companies = client.list_open_companies()
                result = {"ok": True, "companies": companies}
            except TallyConnectionError as exc:
                result = {"ok": False, "error": str(exc)}

        return render_template("discover.html", host=host, port=port, result=result)

    @app.route("/customers")
    def customers_list():
        store = get_store()
        records = store.all_customer_master_records()
        freshness = _freshness(store.last_extraction_at())
        store.close()
        # Never-reconciled parties first, then longest-unreconciled - this
        # view exists to put a name against who last actually looked at a
        # party, so it should lead with whoever needs that look most.
        rows = sorted(
            records,
            key=lambda r: (r["last_reconciled_on"] is not None, r["last_reconciled_on"] or "", r["party_name"]),
        )
        return render_template(
            "customers_list.html", rows=rows, today=date.today().isoformat(), freshness=freshness
        )

    @app.route("/customers/reconcile", methods=["POST"])
    @requires_role("preparer")
    def customers_reconcile():
        party_id = request.form.get("party_id", "")
        branch_id = request.form.get("branch_id", "")
        reconciled_by = request.form.get("reconciled_by", "").strip()
        if not reconciled_by:
            flash("Enter who is confirming this reconciliation.", "error")
            return redirect(url_for("customers_list"))
        store = get_store()
        store.mark_reconciled(party_id, branch_id, date.today(), reconciled_by)
        store.close()
        flash(f"Marked reconciled by {reconciled_by}.", "success")
        return redirect(url_for("customers_list"))

    def _decode_upload(file_storage) -> str:
        """Same UTF-8-then-UTF-16 fallback as TallyClient._post - a
        manually exported Tally file can use either encoding depending
        on Tally version, same as a live HTTP response would.
        """
        if file_storage is None or not file_storage.filename:
            return ""
        raw = file_storage.read()
        if not raw:
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-16")

    @app.route("/manual-upload", methods=["GET", "POST"])
    @requires_role("preparer")
    def manual_upload():
        store = get_store()
        branches = store.list_branches()
        default_to = date.today()
        default_from = default_to - timedelta(days=7)
        result = None

        if request.method == "POST":
            branch_id = request.form.get("branch_id", "")
            branch = store.get_branch(branch_id)
            if branch is None:
                flash("Select a branch first.", "error")
                store.close()
                return render_template(
                    "manual_upload.html", branches=branches, result=None, slots=WEEKLY_VOUCHER_SLOTS,
                    default_from=default_from.isoformat(), default_to=default_to.isoformat(),
                )

            try:
                from_date = date.fromisoformat(request.form["from_date"])
                to_date = date.fromisoformat(request.form["to_date"])
            except (KeyError, ValueError):
                flash("Enter valid From and To dates.", "error")
                store.close()
                return render_template(
                    "manual_upload.html", branches=branches, result=None, slots=WEEKLY_VOUCHER_SLOTS,
                    default_from=default_from.isoformat(), default_to=default_to.isoformat(),
                )
            if from_date > to_date:
                flash("From Date must be on or before To Date.", "error")
                store.close()
                return render_template(
                    "manual_upload.html", branches=branches, result=None, slots=WEEKLY_VOUCHER_SLOTS,
                    default_from=from_date.isoformat(), default_to=to_date.isoformat(),
                )

            weekly_voucher_xml = {
                slot: _decode_upload(request.files.get(f"voucher_{slot}")) for slot in WEEKLY_VOUCHER_SLOTS
            }
            trial_balance_xml = _decode_upload(request.files.get("trial_balance"))
            ytd_voucher_xml = _decode_upload(request.files.get("ytd_vouchers"))

            if not trial_balance_xml.strip():
                flash("Trial Balance / Sundry Debtors closing balance file is required.", "error")
                store.close()
                return render_template(
                    "manual_upload.html", branches=branches, result=None, slots=WEEKLY_VOUCHER_SLOTS,
                    default_from=from_date.isoformat(), default_to=to_date.isoformat(),
                )
            if not any(xml.strip() for xml in weekly_voucher_xml.values()):
                flash("At least one voucher file is required.", "error")
                store.close()
                return render_template(
                    "manual_upload.html", branches=branches, result=None, slots=WEEKLY_VOUCHER_SLOTS,
                    default_from=from_date.isoformat(), default_to=to_date.isoformat(),
                )

            try:
                upload_result = process_manual_upload(
                    store, branch.branch_id, branch.branch_name, to_date,
                    weekly_voucher_xml=weekly_voucher_xml,
                    trial_balance_xml=trial_balance_xml,
                    ytd_voucher_xml=ytd_voucher_xml,
                )
                result = {
                    "branch": branch, "from_date": from_date.isoformat(), "to_date": to_date.isoformat(),
                    "refused": False, "outcome": upload_result.outcome, "drift_findings": upload_result.drift_findings,
                    "fy_start": financial_year_start(to_date).isoformat(),
                }
            except ManualUploadRefused as exc:
                result = {
                    "branch": branch, "from_date": from_date.isoformat(), "to_date": to_date.isoformat(),
                    "refused": True, "detail": str(exc),
                }
            default_from, default_to = from_date, to_date

        store.close()
        return render_template(
            "manual_upload.html", branches=branches, result=result, slots=WEEKLY_VOUCHER_SLOTS,
            default_from=default_from.isoformat(), default_to=default_to.isoformat(),
        )

    def _parse_as_of() -> date:
        raw = request.args.get("as_of", "")
        try:
            return date.fromisoformat(raw) if raw else date.today()
        except ValueError:
            return date.today()

    def _build_sales_dn_display_rows(as_of: date):
        """Shared by the Sales & DN Register's HTML view and its Excel
        export - both render exactly this data, so a download can never
        silently drift from what's on screen.
        """
        store = get_store()
        sales_dn_rows = store.all_sales_dn_rows()
        cn_rows = store.all_credit_note_rows()
        rj_rows = store.all_receipt_journal_rows()
        follow_ups = {(f.branch_id, f.voucher_number, f.party_id): f for f in store.all_invoice_follow_ups()}
        groupings = {
            (r["party_id"], r["branch_id"]): r["grouping"] for r in store.all_customer_master_records()
        }
        freshness = _freshness(store.last_extraction_at())
        store.close()

        display_rows = []
        for row in sales_dn_rows:
            position = compute_invoice_position(row, cn_rows, rj_rows, as_of)
            follow_up = follow_ups.get((row.branch_id, row.voucher_number, row.party_id))
            ptp_status = None
            if follow_up is not None and follow_up.ptp_date is not None:
                if follow_up.ptp_date > as_of:
                    ptp_status = "Active"
                else:
                    outcome = compute_ptp_outcome(row, follow_up, cn_rows, rj_rows)
                    if outcome is not None:
                        ptp_status = "Kept" if outcome.kept else "Broken"
            display_rows.append(
                {
                    "row": row,
                    "position": position,
                    "linked_cn_no": compute_linked_cn_reference_text(row, cn_rows, as_of),
                    "follow_up": follow_up,
                    "ptp_status": ptp_status,
                    "grouping": groupings.get((row.party_id, row.branch_id)),
                }
            )
        return display_rows, freshness

    def _build_credit_note_display_rows():
        store = get_store()
        rows = store.all_credit_note_rows()
        freshness = _freshness(store.last_extraction_at())
        store.close()
        # Open/Unapplied CN Amount (design doc item 2): the CN's own
        # amount only when it's genuinely on-account (no bill reference)
        # AND Current - a Pending Review/Pre-MIS CN isn't a real
        # unapplied balance this system can vouch for yet, matching
        # registers.compute_unapplied_cn_by_party's own condition.
        display_rows = [
            {
                "row": row,
                "unapplied_amount": (
                    row.cn_amount
                    if row.bill_allocation_reference is None and row.classification == RegisterClassification.CURRENT
                    else None
                ),
            }
            for row in rows
        ]
        return display_rows, freshness

    def _build_receipt_journal_display_rows(as_of: date):
        store = get_store()
        rj_rows = store.all_receipt_journal_rows()
        sales_dn_rows = store.all_sales_dn_rows()
        freshness = _freshness(store.last_extraction_at())
        store.close()

        lookup = build_bill_reference_lookup(sales_dn_rows)
        display_rows = [
            {"row": row, "fields": compute_receipt_journal_display_fields(row, lookup, as_of)} for row in rj_rows
        ]
        return display_rows, freshness

    @app.route("/registers/sales-dn")
    def sales_dn_register():
        """Design doc item 2's flagship register, item 14's as-of-date
        mechanism applied: every derived column (Linked CN Amount,
        Receipts Applied, Open Amount, Overdue Flag, DPD, Ageing Bucket,
        PTP Status) is recomputed live for the selected `as_of` date, not
        read from a stored total - changing the date changes what's shown
        without touching any data.
        """
        as_of = _parse_as_of()
        display_rows, freshness = _build_sales_dn_display_rows(as_of)
        return render_template(
            "sales_dn_register.html", display_rows=display_rows, as_of=as_of.isoformat(),
            freshness=freshness,
        )

    @app.route("/registers/sales-dn/export.xlsx")
    def sales_dn_register_export():
        as_of = _parse_as_of()
        display_rows, _ = _build_sales_dn_display_rows(as_of)
        wb = build_sales_dn_register_workbook(display_rows, as_of)
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(
            buf, as_attachment=True, download_name=f"Sales_DN_Register_{as_of.isoformat()}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/registers/credit-notes")
    def credit_note_register():
        display_rows, freshness = _build_credit_note_display_rows()
        return render_template("credit_note_register.html", display_rows=display_rows, freshness=freshness)

    @app.route("/registers/credit-notes/export.xlsx")
    def credit_note_register_export():
        display_rows, _ = _build_credit_note_display_rows()
        wb = build_credit_note_register_workbook(display_rows)
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(
            buf, as_attachment=True, download_name=f"Credit_Note_Register_{date.today().isoformat()}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/registers/receipts-journals")
    def receipt_journal_register():
        as_of = _parse_as_of()
        display_rows, freshness = _build_receipt_journal_display_rows(as_of)
        return render_template(
            "receipt_journal_register.html", display_rows=display_rows, as_of=as_of.isoformat(),
            freshness=freshness,
        )

    @app.route("/registers/receipts-journals/export.xlsx")
    def receipt_journal_register_export():
        as_of = _parse_as_of()
        display_rows, _ = _build_receipt_journal_display_rows(as_of)
        wb = build_receipt_journal_register_workbook(display_rows, as_of)
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(
            buf, as_attachment=True, download_name=f"Receipt_Journal_Register_{as_of.isoformat()}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/reports")
    def reports_home():
        store = get_store()
        freshness = _freshness(store.last_extraction_at())
        store.close()
        return render_template("reports_home.html", freshness=freshness)

    @app.route("/reports/ar-snapshot")
    def ar_snapshot_report():
        """Design doc item 15's flagship report - every figure recomputed
        for the selected `as_of` date (item 14), nothing read from a
        stored total. Trend Analysis (Sales/Collection MIS-vs-Pre-MIS,
        last 4 months) is deliberately not built yet - it needs a
        configured MIS go-live date this application doesn't have
        anywhere yet, and guessing one would put a wrong number in front
        of whoever's using this to make a decision.
        """
        as_of = _parse_as_of()
        store = get_store()
        sales_dn_rows = store.all_sales_dn_rows()
        cn_rows = store.all_credit_note_rows()
        rj_rows = store.all_receipt_journal_rows()
        customer_masters = {(c.party_id, c.branch_id): c for c in store.all_customer_masters()}
        follow_ups = store.all_invoice_follow_ups()
        latest_closing_total = store.latest_weekly_snapshot_closing_total()
        freshness = _freshness(store.last_extraction_at())
        store.close()

        sales_dn_by_identity = {(r.branch_id, r.voucher_number, r.party_id): r for r in sales_dn_rows}
        ptp_rate = compute_ptp_kept_rate(follow_ups, sales_dn_by_identity, cn_rows, rj_rows, as_of)

        snapshot = compute_ar_snapshot(
            sales_dn_rows, cn_rows, rj_rows, customer_masters, ptp_rate, as_of, latest_closing_total
        )
        return render_template(
            "ar_snapshot.html", snapshot=snapshot, as_of=as_of.isoformat(), freshness=freshness
        )

    @app.route("/reports/branch-ageing")
    def branch_ageing_report():
        as_of = _parse_as_of()
        store = get_store()
        sales_dn_rows = store.all_sales_dn_rows()
        cn_rows = store.all_credit_note_rows()
        rj_rows = store.all_receipt_journal_rows()
        freshness = _freshness(store.last_extraction_at())
        store.close()

        rows = compute_branch_ageing_schedule(sales_dn_rows, cn_rows, rj_rows, as_of)
        bucket_order = ["Current", "1-30", "31-60", "61-90", "91-120", "121-150", "151-180", "181+"]
        return render_template(
            "branch_ageing.html", rows=rows, bucket_order=bucket_order, as_of=as_of.isoformat(), freshness=freshness
        )

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=True)
