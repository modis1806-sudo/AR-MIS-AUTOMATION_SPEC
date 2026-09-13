"""Local web application: Extraction (Branch Master, Test Extraction,
Discover Companies, Manual Upload - all Maker-only), Registers (the
Sales & DN, Credit Note, and Receipt & Journal registers plus Customer
Master, viewable by anyone), and Reports (the full report catalog,
docs/registers_and_reporting_design.md item 15).

Runs on localhost only, matching the spec's LAN-only/no-cloud-dependency
framing - this is an internal single-operator tool, not a public service.

Role gating uses a plain session value with no password behind it - a
Maker/Checker picker, not real authentication. This is a **permanent**
decision, not a stopgap awaiting a later login system: the client
explicitly chose to leave password-based login out of the project (see
docs/registers_and_reporting_design.md's Open Items, which also records
that this deliberately supersedes item 11's original "real login is
non-negotiable" requirement). It hides the Extraction section from a
Checker (you/CFO) in the UI and blocks its routes server-side, so a
Checker genuinely cannot reach Extraction - but anyone with access to
this machine can still pick "Maker" themselves, since nothing checks who
is actually sitting at the keyboard. That is accepted, not overlooked.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from io import BytesIO

from flask import Flask, flash, redirect, render_template, request, send_file, session, url_for

from ar_mis.ageing_matrix import compute_ageing_matrix
from ar_mis.branch_totals import compute_branch_sales_cn_dn_totals
from ar_mis.config import BranchConfig, financial_year_start
from ar_mis.dashboard import compute_ar_snapshot, compute_branch_ageing_schedule
from ar_mis.exception_register import (
    compute_negative_open_amount_invoices,
    compute_non_active_debtors,
    compute_top_overdue_customers,
    compute_unapplied_cash_exceptions,
    compute_unapplied_cn_exceptions,
    compute_unresolved_references,
)
from ar_mis.manual_upload import WEEKLY_VOUCHER_SLOTS, ManualUploadRefused, process_manual_upload
from ar_mis.models import RegisterClassification
from ar_mis.drift_correction import (
    DriftFindingAlreadyIncorporated,
    DriftFindingCorrectionWeekConflict,
    incorporate_drift_finding,
)
from ar_mis.pipeline import process_branch_data
from ar_mis.reconciliation import isolate_drift
from ar_mis.reconciliation_report import compute_tb_cross_check_summary
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
    financial_year_label,
)
from ar_mis.sign import flip_sign
from ar_mis.storage import Store
from ar_mis.tally_client import CompanyMismatchError, TallyClient, TallyConnectionError
from ar_mis.weekly_movement import attach_trends, build_weekly_movement_row

ROLES = {
    "maker": "Maker",
    "checker": "Checker",
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
        """Route-level enforcement of the role gating described in this
        module's docstring - the nav already hides Extraction links from
        a Checker, but a link being hidden is not the same as a route
        being blocked, and the client was explicit that Extraction must
        not be reachable by a Checker at all, not just tucked out of the
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
    @requires_role("maker")
    def branches_list():
        store = get_store()
        branches = store.list_branches()
        store.close()
        return render_template("branches_list.html", branches=branches)

    @app.route("/branches/new", methods=["GET", "POST"])
    @requires_role("maker")
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
    @requires_role("maker")
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
    @requires_role("maker")
    def branch_delete(branch_id):
        store = get_store()
        store.delete_branch(branch_id)
        store.close()
        flash("Branch removed.", "success")
        return redirect(url_for("branches_list"))

    @app.route("/test-extraction", methods=["GET", "POST"])
    @requires_role("maker")
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

    @app.route("/extract-and-save", methods=["GET", "POST"])
    @requires_role("maker")
    def extract_and_save():
        """The genuine live-Tally commit action, missing from this webapp
        until now - Test Extraction (above) is diagnostic-only and writes
        nothing; Manual Upload writes data but only from uploaded files.
        This is the same write path Manual Upload uses
        (process_branch_data), sourced from a live Tally pull instead,
        mirroring exactly what ar_mis.pipeline.build_branch_runner (the
        CLI's own live path) does - including the Section 4.2 YTD full-
        pull drift check, so a backdated entry is caught here too, not
        only when running via Manual Upload or the CLI.

        Refuses outright if this branch/week already has recorded data,
        the same conflict rule Manual Upload enforces - first one in
        wins, no silent overwrite. `week_ending` is a single date (not a
        from/to range like the diagnostic Test Extraction page): the
        7-day window and the Sundry Debtors YTD cutoff are both derived
        from it, matching the CLI's own weekly cadence, since this data
        becomes a real weekly_snapshot row, not just a diagnostic count.
        """
        store = get_store()
        branches = store.list_branches()
        default_week_ending = date.today()
        result = None

        if request.method == "POST":
            branch_id = request.form.get("branch_id", "")
            branch = store.get_branch(branch_id)
            if branch is None:
                flash("Select a branch first.", "error")
                store.close()
                return render_template(
                    "extract_and_save.html", branches=branches, result=None,
                    default_week_ending=default_week_ending.isoformat(),
                )

            try:
                week_ending = date.fromisoformat(request.form["week_ending"])
            except (KeyError, ValueError):
                flash("Enter a valid week-ending date.", "error")
                store.close()
                return render_template(
                    "extract_and_save.html", branches=branches, result=None,
                    default_week_ending=default_week_ending.isoformat(),
                )
            default_week_ending = week_ending

            if store.has_weekly_snapshot_for_branch_week(branch_id, week_ending):
                result = {
                    "branch": branch, "week_ending": week_ending.isoformat(),
                    "refused": True,
                    "detail": (
                        f"Branch '{branch.branch_name}' already has recorded data for the week "
                        f"ending {week_ending.isoformat()}. Refusing to overwrite - a genuine "
                        "correction needs a deliberate, separate action."
                    ),
                }
                store.close()
                return render_template(
                    "extract_and_save.html", branches=branches, result=result,
                    default_week_ending=default_week_ending.isoformat(),
                )

            from_date = week_ending - timedelta(days=6)
            fy_start = financial_year_start(week_ending)
            client = TallyClient(branch=branch)

            try:
                client.confirm_current_company()
                vouchers_by_type = client.fetch_all_voucher_types(from_date, week_ending)
                all_vouchers = [v for vs in vouchers_by_type.values() for v in vs]
                closing_extracted = {
                    name: flip_sign(balance)
                    for name, balance in client.fetch_ytd_sundry_debtors(fy_start, week_ending).items()
                }
            except (CompanyMismatchError, TallyConnectionError) as exc:
                result = {
                    "branch": branch, "week_ending": week_ending.isoformat(),
                    "refused": True, "detail": f"Extraction failed: {exc}",
                }
                store.close()
                return render_template(
                    "extract_and_save.html", branches=branches, result=result,
                    default_week_ending=default_week_ending.isoformat(),
                )

            outcome = process_branch_data(
                store, branch.branch_id, branch.branch_name, week_ending, all_vouchers, closing_extracted
            )

            # Section 4.2: a separate full YTD pull, diffed against
            # everything ever logged, to catch backdated entries an
            # incremental weekly pull structurally cannot see.
            drift_findings = []
            try:
                ytd_vouchers_by_type = client.fetch_all_voucher_types(fy_start, week_ending)
                ytd_vouchers = [v for vs in ytd_vouchers_by_type.values() for v in vs]
                party_names = set(closing_extracted)
                logged_keys = {p: store.logged_voucher_keys(branch.branch_id, p) for p in party_names}
                week_boundaries = store.all_week_endings()
                drift_findings = isolate_drift(branch.branch_id, ytd_vouchers, party_names, logged_keys, week_boundaries)
                store.record_drift_findings(branch.branch_id, drift_findings, datetime.now())
            except TallyConnectionError as exc:
                flash(f"Data was saved, but the YTD drift check could not run: {exc}", "error")

            result = {
                "branch": branch, "week_ending": week_ending.isoformat(),
                "refused": False, "outcome": outcome, "drift_findings": drift_findings,
                "fy_start": fy_start.isoformat(),
            }

        store.close()
        return render_template(
            "extract_and_save.html", branches=branches, result=result,
            default_week_ending=default_week_ending.isoformat(),
        )

    @app.route("/discover", methods=["GET", "POST"])
    @requires_role("maker")
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
    @requires_role("maker")
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
    @requires_role("maker")
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

    @app.route("/reports/exceptions")
    def exception_register_report():
        """Design doc item 15's six sub-reports in one screen - the
        situations this whole design exists to surface rather than bury
        in a total: money sitting unapplied, an invoice overpaid, a
        customer gone quiet while still owing money, a reference nobody
        resolved, and who's most worth chasing right now.
        """
        as_of = _parse_as_of()
        store = get_store()
        sales_dn_rows = store.all_sales_dn_rows()
        cn_rows = store.all_credit_note_rows()
        rj_rows = store.all_receipt_journal_rows()
        freshness = _freshness(store.last_extraction_at())
        store.close()

        unapplied_cash = compute_unapplied_cash_exceptions(rj_rows)
        unapplied_cn = compute_unapplied_cn_exceptions(cn_rows)
        negative_open = compute_negative_open_amount_invoices(sales_dn_rows, cn_rows, rj_rows, as_of)
        non_active = compute_non_active_debtors(sales_dn_rows, cn_rows, rj_rows, as_of)
        unresolved = compute_unresolved_references(cn_rows, rj_rows)
        top_overdue = compute_top_overdue_customers(sales_dn_rows, cn_rows, rj_rows, as_of)

        return render_template(
            "exception_register.html",
            as_of=as_of.isoformat(),
            freshness=freshness,
            unapplied_cash=unapplied_cash,
            unapplied_cn=unapplied_cn,
            negative_open=negative_open,
            non_active=non_active,
            unresolved=unresolved,
            top_overdue=top_overdue,
        )

    @app.route("/reports/ageing-matrix")
    def ageing_matrix_report():
        """Design doc item 15's customer-level detail report - a branch
        summary (reusing compute_branch_ageing_schedule as-is) plus a
        party-level breakdown that independently cross-checks the
        registers-derived Total Open against Tally's own ledger closing
        balance for that party (weekly_snapshot.closing_extracted). FY
        scoping is applied here, at the call site, by filtering
        sales_dn_rows before either compute function runs - neither
        compute function itself knows about financial years.
        """
        as_of = _parse_as_of()
        store = get_store()
        sales_dn_rows = store.all_sales_dn_rows()
        cn_rows = store.all_credit_note_rows()
        rj_rows = store.all_receipt_journal_rows()
        customer_masters = {(c.party_id, c.branch_id): c for c in store.all_customer_masters()}
        tally_closing_by_party = store.latest_weekly_snapshot_closing_by_party(as_of)
        freshness = _freshness(store.last_extraction_at())
        store.close()

        all_fys = sorted({financial_year_label(row.invoice_date) for row in sales_dn_rows})
        selected_fy = request.args.get("fy", "")
        if selected_fy and selected_fy in all_fys:
            scoped_sales_dn_rows = [row for row in sales_dn_rows if financial_year_label(row.invoice_date) == selected_fy]
        else:
            selected_fy = ""
            scoped_sales_dn_rows = sales_dn_rows

        branch_rows = compute_branch_ageing_schedule(scoped_sales_dn_rows, cn_rows, rj_rows, as_of)
        customer_rows = compute_ageing_matrix(
            scoped_sales_dn_rows, cn_rows, rj_rows, customer_masters, tally_closing_by_party, as_of
        )
        bucket_order = ["Current", "1-30", "31-60", "61-90", "91-120", "121-150", "151-180", "181+"]

        return render_template(
            "ageing_matrix.html",
            as_of=as_of.isoformat(),
            freshness=freshness,
            all_fys=all_fys,
            selected_fy=selected_fy,
            branch_rows=branch_rows,
            customer_rows=customer_rows,
            bucket_order=bucket_order,
        )

    @app.route("/reports/weekly-movement")
    def weekly_movement_report():
        """Design doc item 15's fifth report - unlike every other report in
        this app, this one does NOT recompute live: it lists whatever weeks
        a Maker has explicitly recorded (append-only, Open Item 4's
        resolution), oldest first, with a week-over-week trend indicator
        next to each top-line figure.
        """
        store = get_store()
        rows = store.all_weekly_movement_rows()
        freshness = _freshness(store.last_extraction_at())
        store.close()

        display_rows = attach_trends(rows)
        today = date.today().isoformat()
        return render_template(
            "weekly_movement.html", display_rows=display_rows, freshness=freshness, today=today
        )

    @app.route("/reports/weekly-movement/record", methods=["POST"])
    @requires_role("maker")
    def weekly_movement_record():
        """Recording a week is a one-way door by design (append-only
        history, per WeeklyMovementRow's own docstring) - a Maker picks
        the week_ending to record, the current portfolio position as of
        that date is computed exactly like the AR Snapshot dashboard does,
        and it's then locked in. A week already recorded is refused with a
        message, not silently skipped or overwritten.
        """
        raw_week_ending = request.form.get("week_ending", "")
        try:
            week_ending = date.fromisoformat(raw_week_ending)
        except ValueError:
            flash("Choose a valid week-ending date.", "error")
            return redirect(url_for("weekly_movement_report"))

        store = get_store()
        if store.has_weekly_movement_for_week(week_ending):
            store.close()
            flash(f"{week_ending.isoformat()} has already been recorded and can't be overwritten.", "error")
            return redirect(url_for("weekly_movement_report"))

        sales_dn_rows = store.all_sales_dn_rows()
        cn_rows = store.all_credit_note_rows()
        rj_rows = store.all_receipt_journal_rows()
        customer_masters = {(c.party_id, c.branch_id): c for c in store.all_customer_masters()}

        snapshot = compute_ar_snapshot(
            sales_dn_rows, cn_rows, rj_rows, customer_masters,
            ptp_kept_rate=None, as_of=week_ending, latest_weekly_snapshot_closing_total=None,
        )
        row = build_weekly_movement_row(snapshot, week_ending, recorded_at=datetime.now())
        store.record_weekly_movement(row)
        store.close()
        flash(f"Recorded the position as of {week_ending.isoformat()}.", "success")
        return redirect(url_for("weekly_movement_report"))

    @app.route("/reports/tb-cross-check")
    def tb_cross_check_report():
        """The client's explicit ask this session: the Trial Balance /
        Sundry Debtors closing balance this app already extracts from
        Tally, kept visible in its own sheet - one row per party per
        branch per week - alongside this app's own workings and the
        difference between the two, so a Maker or Checker can always see
        for themselves whether the data is valid, not just trust that it
        is. Every row already exists in weekly_snapshot (data is never
        discarded, reconciled or not - see ar_mis.pipeline); this report
        does no independent computation, only the summary rollup.
        """
        store = get_store()
        rows = store.all_weekly_snapshot_rows()
        freshness = _freshness(store.last_extraction_at())
        store.close()

        summary = compute_tb_cross_check_summary(rows)
        return render_template(
            "tb_cross_check.html", rows=rows, summary=summary, freshness=freshness
        )

    @app.route("/reports/branch-totals")
    def branch_totals_report():
        """The client's other explicit ask this session: a plain sum of
        Sales + Debit Notes - Credit Notes per branch for a chosen
        period, simple enough to eyeball directly against Tally's own
        P&L page - see ar_mis.branch_totals for why this is a gross
        (GST-inclusive) figure, not tax-exclusive turnover.
        """
        today = date.today()
        default_start = financial_year_start(today)
        try:
            period_start = date.fromisoformat(request.args.get("period_start", "")) if request.args.get("period_start") else default_start
        except ValueError:
            period_start = default_start
        try:
            period_end = date.fromisoformat(request.args.get("period_end", "")) if request.args.get("period_end") else today
        except ValueError:
            period_end = today

        store = get_store()
        sales_dn_rows = store.all_sales_dn_rows()
        cn_rows = store.all_credit_note_rows()
        freshness = _freshness(store.last_extraction_at())
        store.close()

        rows = compute_branch_sales_cn_dn_totals(sales_dn_rows, cn_rows, period_start, period_end)
        return render_template(
            "branch_totals.html", rows=rows, freshness=freshness,
            period_start=period_start.isoformat(), period_end=period_end.isoformat(),
        )

    @app.route("/reports/drift-findings")
    def drift_findings_report():
        """Section 4.2's backdated-entry findings, found and fixed this
        session so they no longer vanish once the run that found them is
        over: every finding ever discovered (across Extract & Save,
        Manual Upload, and the CLI alike) is listed here, most recent
        first. Two independent actions: acknowledging (an audit note
        only - "someone has seen this") and incorporating (the actual
        fix - ar_mis.drift_correction). The headline count tracks
        incorporation, not acknowledgement - acknowledging a finding
        doesn't change the fact that Tally and this app still disagree
        until the voucher is actually incorporated.
        """
        store = get_store()
        records = store.all_drift_findings()
        freshness = _freshness(store.last_extraction_at())
        store.close()

        outstanding_count = sum(1 for r in records if not r.incorporated)
        return render_template(
            "drift_findings.html", records=records, outstanding_count=outstanding_count, freshness=freshness,
            default_week_ending=date.today().isoformat(),
        )

    @app.route("/reports/drift-findings/<int:finding_id>/acknowledge", methods=["POST"])
    @requires_role("maker")
    def drift_finding_acknowledge(finding_id):
        acknowledged_by = request.form.get("acknowledged_by", "").strip()
        if not acknowledged_by:
            flash("Enter your name to acknowledge a finding.", "error")
            return redirect(url_for("drift_findings_report"))

        store = get_store()
        store.acknowledge_drift_finding(finding_id, acknowledged_by, datetime.now())
        store.close()
        flash("Finding acknowledged.", "success")
        return redirect(url_for("drift_findings_report"))

    @app.route("/reports/drift-findings/<int:finding_id>/incorporate", methods=["POST"])
    @requires_role("maker")
    def drift_finding_incorporate(finding_id):
        """The actual correction mechanism, per ar_mis.drift_correction's
        own docstring: replays the finding's original voucher into the
        registers under a NEW week the Maker picks - never an edit to an
        already-locked historical week. Requires that party's real,
        Tally-sourced Sundry Debtors closing balance as of that date, the
        same trust level as every other closing_extracted figure in this
        app; a wrong figure here doesn't get silently accepted - it just
        shows up as a fresh mismatch on the TB Cross-Check sheet.
        """
        store = get_store()
        finding_record = store.get_drift_finding(finding_id)
        if finding_record is None:
            store.close()
            flash("That finding no longer exists.", "error")
            return redirect(url_for("drift_findings_report"))

        incorporated_by = request.form.get("incorporated_by", "").strip()
        raw_week_ending = request.form.get("week_ending", "")
        raw_closing = request.form.get("party_closing_extracted", "").strip()

        if not incorporated_by:
            store.close()
            flash("Enter your name to incorporate a finding.", "error")
            return redirect(url_for("drift_findings_report"))
        try:
            week_ending = date.fromisoformat(raw_week_ending)
        except ValueError:
            store.close()
            flash("Choose a valid week-ending date for the correction.", "error")
            return redirect(url_for("drift_findings_report"))
        try:
            party_closing_extracted = Decimal(raw_closing)
        except (InvalidOperation, ValueError):
            store.close()
            flash("Enter a valid closing balance for this party.", "error")
            return redirect(url_for("drift_findings_report"))

        branch = store.get_branch(finding_record.branch_id)
        branch_name = branch.branch_name if branch else finding_record.branch_id

        try:
            result = incorporate_drift_finding(
                store, finding_record.branch_id, branch_name, finding_record,
                week_ending, party_closing_extracted, incorporated_by, datetime.now(),
            )
        except (DriftFindingAlreadyIncorporated, DriftFindingCorrectionWeekConflict) as exc:
            store.close()
            flash(str(exc), "error")
            return redirect(url_for("drift_findings_report"))

        store.close()
        if result.outcome.failed_parties:
            flash(
                f"Voucher incorporated under week ending {week_ending.isoformat()}, but this party still "
                "shows a difference - see the TB Reconciliation Cross-Check sheet.", "error",
            )
        else:
            flash(f"Voucher incorporated under week ending {week_ending.isoformat()} - reconciled clean.", "success")
        return redirect(url_for("drift_findings_report"))

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=True)
