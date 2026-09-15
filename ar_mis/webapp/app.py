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
from ar_mis.config import BranchConfig, financial_year_start, split_into_chunks
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
from ar_mis.models import CustomerMasterRecord, RegisterClassification
from ar_mis.money import to_money
from ar_mis.drift_correction import (
    DriftFindingAlreadyIncorporated,
    DriftFindingCorrectionWeekConflict,
    incorporate_drift_finding,
)
from ar_mis.pipeline import process_branch_data
from ar_mis.reconciliation import isolate_drift
from ar_mis.reconciliation_report import compute_tb_cross_check_summary, compute_unreconciled_parties
from ar_mis.seed import BRANCH_SCOPED_CSV_TEMPLATE, parse_seed_rows_for_branch, seed as seed_pre_mis
from ar_mis.register_export import (
    build_credit_note_register_workbook,
    build_receipt_journal_register_workbook,
    build_sales_dn_register_workbook,
    build_unreconciled_parties_workbook,
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
from ar_mis.webapp.formatting import format_inr

ROLES = {
    "maker": "Maker",
    "checker": "Checker",
}


def create_app(db_path: str = "data/ar_mis.db") -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.secret_key = "ar-mis-local-tool"  # localhost-only internal tool; no session security needed

    # Every amount in this app is Indian Rupees - client's explicit ask:
    # group digits the Indian way (1,23,45,678.00), not the Western
    # 3-digit grouping "%.2f" or a bare Decimal would otherwise produce,
    # which forces a reader to count digits to tell a lakh from a crore.
    app.jinja_env.filters["inr"] = format_inr

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
        data_summaries = {b.branch_id: store.branch_data_summary(b.branch_id) for b in branches}
        store.close()
        return render_template("branches_list.html", branches=branches, data_summaries=data_summaries)

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
            _process_pre_mis_upload(store, branch.branch_id)
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
            _process_pre_mis_upload(store, branch_id)
            store.close()
            flash(f"Branch '{updated.branch_name}' updated.", "success")
            return redirect(url_for("branches_list"))

        store.close()
        return render_template("branch_form.html", branch=existing)

    def _process_pre_mis_upload(store: Store, branch_id: str) -> None:
        """Optional one-time Pre-MIS Outstanding (Section 2.5) bulk load,
        offered right on Branch Master's create/edit form - client's
        explicit ask, since this baseline "is to be provided just once in
        a lifetime" and recon "will always fail" without it. A no-op if
        no file was attached: the field is optional on every submit, not
        just the first one, so a maker can come back later and load
        parties missed the first time.

        Mirrors the CLI seed tool's own all-or-nothing validation: any
        row-level error rejects the whole file (nothing is loaded) rather
        than silently loading the good rows and burying the bad ones in a
        flash message - a maker fixes the file and re-uploads it clean.

        Always seeds with force=True. seed()'s has_weekly_snapshots()
        check runs BEFORE its force check and is never bypassed by it -
        a party with real extraction history on record can never be
        touched here no matter what. Without force, though, a party that
        exists only because an earlier (possibly buggy, since-deleted)
        extraction auto-created it at a placeholder Pre-MIS Outstanding
        of 0.00 - the ordinary "still the pre-launch load, first attempt
        was wrong" case - would also get skipped, with no way for a
        browser-only maker to pass --force to unlock it. Forcing here
        closes that gap without weakening the real protection.
        """
        csv_text = _decode_upload(request.files.get("pre_mis_csv"))
        if not csv_text.strip():
            return
        try:
            records, errors = parse_seed_rows_for_branch(csv_text, branch_id)
        except ValueError as exc:
            flash(f"Pre-MIS Outstanding CSV rejected: {exc}", "error")
            return
        if errors:
            first = errors[0]
            more = f", and {len(errors) - 1} more" if len(errors) > 1 else ""
            flash(
                f"Pre-MIS Outstanding CSV rejected: line {first.line_number}: "
                f"{first.reason}{more}. Nothing was loaded - fix and re-upload.",
                "error",
            )
            return
        summary = seed_pre_mis(store, records, force=True, announce=lambda *_: None)
        parts = [f"{summary.loaded} loaded"]
        if summary.unchanged:
            parts.append(f"{summary.unchanged} unchanged")
        if summary.overwritten:
            parts.append(f"{summary.overwritten} corrected")
        if summary.skipped:
            parts.append(f"{len(summary.skipped)} skipped - already has weekly data, use an adjustment instead")
        flash(
            f"Pre-MIS Outstanding CSV: {', '.join(parts)}.",
            "error" if summary.skipped else "success",
        )

    @app.route("/branches/pre-mis-template.csv")
    @requires_role("maker")
    def branch_pre_mis_template():
        buf = BytesIO(BRANCH_SCOPED_CSV_TEMPLATE.encode("utf-8"))
        return send_file(
            buf, as_attachment=True, download_name="pre_mis_outstanding_template.csv", mimetype="text/csv",
        )

    @app.route("/branches/<branch_id>/delete", methods=["POST"])
    @requires_role("maker")
    def branch_delete(branch_id):
        store = get_store()
        store.delete_branch(branch_id)
        store.close()
        flash("Branch removed.", "success")
        return redirect(url_for("branches_list"))

    @app.route("/branches/<branch_id>/delete-week", methods=["POST"])
    @requires_role("maker")
    def branch_delete_week(branch_id):
        """Undoes exactly one mistaken extraction - client's explicit
        ask: with no way to remove bad data today, an error caught right
        after Save (wrong company, wrong range) is stuck forever. Only
        ever removes the branch's own latest recorded week (see
        Store.delete_branch_week's own docstring for why); Store itself
        enforces that even if this route is ever reached with a stale
        week_ending value, so it fails loudly rather than deleting the
        wrong week.
        """
        store = get_store()
        try:
            week_ending = date.fromisoformat(request.form["week_ending"])
        except (KeyError, ValueError):
            flash("That delete request was missing its week - please try again.", "error")
            store.close()
            return redirect(url_for("branches_list"))

        try:
            store.delete_branch_week(branch_id, week_ending)
            flash(f"Deleted the week ending {week_ending.isoformat()} for '{branch_id}' - re-extract when ready.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        store.close()
        return redirect(url_for("branches_list"))

    @app.route("/branches/<branch_id>/catch-up-party", methods=["GET", "POST"])
    @requires_role("maker")
    def catch_up_party(branch_id):
        """The catch-up tool agreed for a party that was wrongly excluded
        from Sundry Debtors in Tally (misclassified group, or genuinely
        never onboarded) - client's own real example: a debtor mistakenly
        filed as a Sundry Creditor, so neither its opening balance nor
        its sales/write-off vouchers were ever pulled or reconciled.

        Deliberately does NOT ask for a hand-typed closing figure. A
        typed-in number only fixes the TB total; it can't know whether
        the missed activity was a sale, a receipt, or a write-off, so it
        silently leaves the real registers wrong. Instead: the operator
        supplies the party's real balance as of some past anchor date
        (read directly off Tally, exactly like the original Pre-MIS
        Outstanding load), and this route pulls that party's ACTUAL
        voucher history since then and replays it through the exact
        same process_branch_data() every normal weekly run uses - so
        Sales, Credit Notes, Receipts, Journals (a Bad Debt write-off
        included) are each handled by the one general mechanism that
        already understands them, not a new one invented for a single
        voucher type.

        Refuses outright if this party already has weekly_snapshot
        history - this tool is only for a party never tracked before;
        an already-tracked party's gap needs a different path (re-run
        Test & Save Extraction for the missed range, or a logged
        adjustment), never a bulk replay that could double-count
        history a normal run already recorded.

        Only vouchers touching THIS party are replayed, never the full
        company-wide pull for the date range: other parties in that same
        historical window already have their own register rows from
        their own normal weekly runs, and re-running the unfiltered
        voucher list through process_branch_data would duplicate those
        rows. Filtering to this one party's own vouchers keeps every
        other party's history untouched.
        """
        store = get_store()
        branch = store.get_branch(branch_id)
        if branch is None:
            store.close()
            flash(f"No such branch '{branch_id}'.", "error")
            return redirect(url_for("branches_list"))

        if request.method == "GET":
            store.close()
            return render_template(
                "catch_up_party.html", branch=branch, today=date.today().isoformat(),
                prefill_party_name=request.args.get("party_name", ""),
            )

        party_name = request.form.get("party_name", "").strip()
        raw_anchor_date = request.form.get("anchor_date", "")
        raw_anchor_balance = request.form.get("anchor_balance", "")
        raw_as_of = request.form.get("as_of", "")

        def _redisplay(message: str):
            flash(message, "error")
            store.close()
            return render_template("catch_up_party.html", branch=branch, today=date.today().isoformat())

        if not party_name:
            return _redisplay("Party name is required - exactly as it appears in Tally.")
        try:
            anchor_date = date.fromisoformat(raw_anchor_date)
        except ValueError:
            return _redisplay("Enter a valid anchor date.")
        try:
            anchor_balance = to_money(Decimal(raw_anchor_balance))
        except InvalidOperation:
            return _redisplay(f"'{raw_anchor_balance}' is not a valid decimal amount.")
        as_of = date.today()
        if raw_as_of:
            try:
                as_of = date.fromisoformat(raw_as_of)
            except ValueError:
                return _redisplay("Enter a valid 'as of' date.")

        from_date = anchor_date + timedelta(days=1)
        if from_date > as_of:
            return _redisplay("The anchor date must be before the 'as of' date - there needs to be at least one day of activity to pull.")

        if store.has_weekly_snapshots(party_name, branch_id):
            return _redisplay(
                f"'{party_name}' already has weekly extraction history on record - this tool is only for a "
                "party never tracked before. Use a logged adjustment for an already-tracked party instead."
            )

        seed_summary = seed_pre_mis(
            store, [CustomerMasterRecord(party_name, party_name, branch_id, anchor_balance)],
            force=True, announce=lambda *_: None,
        )
        if seed_summary.skipped:
            return _redisplay(seed_summary.skipped[0])

        client = TallyClient(branch=branch)
        try:
            client.confirm_current_company()
            all_vouchers = client.fetch_vouchers(from_date, as_of)
            fy_start = financial_year_start(as_of)
            closing_extracted = {
                name: flip_sign(balance) for name, balance in client.fetch_ytd_sundry_debtors(fy_start, as_of).items()
            }
        except (CompanyMismatchError, TallyConnectionError) as exc:
            return _redisplay(f"Could not reach Tally: {exc}")

        if party_name not in closing_extracted:
            return _redisplay(
                f"'{party_name}' was not found in Tally's Sundry Debtors group as of {as_of.isoformat()}. "
                "Confirm the ledger's group has actually been changed in Tally, and that the name here is "
                "spelled exactly as it appears there (case and spacing must match)."
            )

        party_vouchers = [
            v for v in all_vouchers if any(e.party_ledger_name == party_name for e in v.entries)
        ]

        outcome = process_branch_data(
            store, branch_id, branch.branch_name, week_ending=as_of,
            all_vouchers=party_vouchers, closing_extracted={party_name: closing_extracted[party_name]},
            period_start=from_date,
        )
        store.close()
        flash(f"Caught up '{party_name}': {outcome.detail}", "error" if outcome.failed_parties else "success")
        return redirect(url_for("tb_cross_check_report", to_date=as_of.isoformat()))

    def _default_test_range() -> tuple[date, date]:
        """A plain, generic starting suggestion - the last 7 days ending
        yesterday - shown before a branch is even picked, so this can't
        know that branch's own last-extracted date yet. Purely a
        convenience default; the operator can type any range at all,
        including one starting well before or after this.
        """
        yesterday = date.today() - timedelta(days=1)
        return yesterday - timedelta(days=6), yesterday

    @app.route("/test-extraction", methods=["GET", "POST"])
    @requires_role("maker")
    def test_extraction():
        """One page for both checking and committing a live Tally pull -
        client's explicit ask: a separate Extract & Save page risked
        testing one date range and saving a different one (confirmed live:
        exactly this happened once, from a stale date left in a second
        page's form). Testing here always shows the range that a
        follow-up Save would actually use, since a Save (see
        test_extraction_save below) is only ever offered against the same
        range this test just ran.

        Client's explicit reversal this session: extraction runs on
        EXACTLY the range typed in - never snapped to a calendar week
        boundary (see ar_mis.config's own docstring for the real
        incident this undoes: a first extraction for a new branch got
        silently stretched backward across its own go-live date). A big
        range is still broken into ar_mis.config.split_into_chunks
        pieces below, purely as a practical batch size - not because any
        piece is meant to mean "a calendar week."
        """
        store = get_store()
        branches = store.list_branches()
        result = None
        default_from, default_to = _default_test_range()

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

            # Any range the operator picks - a day, a month, a full
            # financial year for a first-time backfill - is used exactly
            # as typed; no artificial min/max, and nothing snapped.
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

            all_ok = all(ok for _, ok, _ in steps)
            chunks = [
                {
                    "start": c_start.isoformat(), "end": c_end.isoformat(),
                    "already_recorded": store.has_weekly_snapshot_for_branch_week(branch_id, c_end),
                }
                for c_start, c_end in split_into_chunks(from_date, to_date)
            ]

            result = {
                "mode": "test",
                "branch": branch, "steps": steps, "all_ok": all_ok,
                "from_date": from_date.isoformat(), "to_date": to_date.isoformat(),
                "chunks": chunks,
                "any_chunk_new": any(not c["already_recorded"] for c in chunks),
            }
            default_from, default_to = from_date, to_date

        store.close()
        return render_template(
            "test_extraction.html", branches=branches, result=result,
            default_from=default_from.isoformat(), default_to=default_to.isoformat(),
        )

    @app.route("/test-extraction/save", methods=["POST"])
    @requires_role("maker")
    def test_extraction_save():
        """The genuine live-Tally commit action - only ever reached from a
        successful Test Extraction result above, posting back the exact
        same range that was just tested, so what gets saved is never a
        different range than what was checked.

        Same write path Manual Upload uses (process_branch_data),
        sourced from a live Tally pull instead, mirroring
        ar_mis.pipeline.build_branch_runner (the CLI's own live path) -
        including the Section 4.2 YTD full-pull drift check per chunk, so
        a backdated entry is caught here too.

        A big range is saved one ar_mis.config.split_into_chunks piece at
        a time, in chronological order - required, not just tidy: each
        chunk's opening balance rolls forward from the immediately
        preceding chunk's own just-saved closing
        (rollforward.resolve_opening_balances), so processing out of
        order or skipping ahead past a failure would silently corrupt
        every later chunk's opening. A chunk that already has recorded
        data is skipped (first one in wins, same conflict rule Manual
        Upload enforces) without stopping the run; a genuine extraction
        failure on one chunk DOES stop the run, rather than risk building
        every later chunk's opening on top of a gap.
        """
        store = get_store()
        branches = store.list_branches()

        branch_id = request.form.get("branch_id", "")
        branch = store.get_branch(branch_id)
        if branch is None:
            flash("Select a branch first.", "error")
            store.close()
            default_from, default_to = _default_test_range()
            return render_template(
                "test_extraction.html", branches=branches, result=None,
                default_from=default_from.isoformat(), default_to=default_to.isoformat(),
            )

        try:
            from_date = date.fromisoformat(request.form["from_date"])
            to_date = date.fromisoformat(request.form["to_date"])
        except (KeyError, ValueError):
            flash("That save request was missing its date range - please run the test again.", "error")
            store.close()
            default_from, default_to = _default_test_range()
            return render_template(
                "test_extraction.html", branches=branches, result=None,
                default_from=default_from.isoformat(), default_to=default_to.isoformat(),
            )

        client = TallyClient(branch=branch)
        chunk_results = []

        for c_start, c_end in split_into_chunks(from_date, to_date):
            if store.has_weekly_snapshot_for_branch_week(branch_id, c_end):
                chunk_results.append({
                    "start": c_start.isoformat(), "end": c_end.isoformat(), "status": "skipped",
                    "detail": f"Already has recorded data for the run ending {c_end.isoformat()} - not overwritten.",
                })
                continue

            fy_start = financial_year_start(c_end)
            try:
                client.confirm_current_company()
                vouchers_by_type = client.fetch_all_voucher_types(c_start, c_end)
                all_vouchers = [v for vs in vouchers_by_type.values() for v in vs]
                closing_extracted = {
                    name: flip_sign(balance)
                    for name, balance in client.fetch_ytd_sundry_debtors(fy_start, c_end).items()
                }
            except (CompanyMismatchError, TallyConnectionError) as exc:
                chunk_results.append({
                    "start": c_start.isoformat(), "end": c_end.isoformat(), "status": "failed",
                    "detail": f"Extraction failed: {exc}",
                })
                break  # later chunks would roll forward from a gap - stop here, don't guess onward

            outcome = process_branch_data(
                store, branch.branch_id, branch.branch_name, c_end, all_vouchers, closing_extracted,
                period_start=c_start,
            )

            drift_findings = []
            try:
                ytd_vouchers_by_type = client.fetch_all_voucher_types(fy_start, c_end)
                ytd_vouchers = [v for vs in ytd_vouchers_by_type.values() for v in vs]
                party_names = set(closing_extracted)
                logged_keys = {p: store.logged_voucher_keys(branch.branch_id, p) for p in party_names}
                week_boundaries = store.all_week_endings()
                drift_findings = isolate_drift(branch.branch_id, ytd_vouchers, party_names, logged_keys, week_boundaries)
                store.record_drift_findings(branch.branch_id, drift_findings, datetime.now())
            except TallyConnectionError as exc:
                flash(f"Run ending {c_end.isoformat()} was saved, but its YTD drift check could not run: {exc}", "error")

            chunk_results.append({
                "start": c_start.isoformat(), "end": c_end.isoformat(), "status": "saved",
                "outcome": outcome, "drift_findings": drift_findings,
            })

        result = {
            "mode": "save", "branch": branch,
            "from_date": from_date.isoformat(), "to_date": to_date.isoformat(),
            "chunk_results": chunk_results,
        }
        store.close()
        default_from, default_to = from_date, to_date
        return render_template(
            "test_extraction.html", branches=branches, result=result,
            default_from=default_from.isoformat(), default_to=default_to.isoformat(),
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

    @app.route("/customers/set-credit-limit", methods=["POST"])
    @requires_role("maker")
    def customers_set_credit_limit():
        party_id = request.form.get("party_id", "")
        branch_id = request.form.get("branch_id", "")
        raw_limit = request.form.get("credit_limit", "").strip()
        try:
            new_limit = to_money(Decimal(raw_limit))
        except InvalidOperation:
            flash(f"'{raw_limit}' is not a valid credit limit amount.", "error")
            return redirect(url_for("customers_list"))

        store = get_store()
        try:
            store.set_credit_limit(party_id, branch_id, new_limit)
        except ValueError as exc:
            flash(str(exc), "error")
            store.close()
            return redirect(url_for("customers_list"))
        store.close()
        flash(f"Credit limit for {party_id} set to {format_inr(new_limit)}.", "success")
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
                    from_date=from_date,
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

    def _build_credit_note_display_rows(as_of: date):
        store = get_store()
        rows = store.all_credit_note_rows()
        freshness = _freshness(store.last_extraction_at())
        store.close()
        # Open/Unapplied CN Amount (design doc item 2): the CN's own
        # amount only when it's genuinely on-account (no bill reference)
        # AND Current - a Pending Review/Pre-MIS CN isn't a real
        # unapplied balance this system can vouch for yet, matching
        # registers.compute_unapplied_cn_by_party's own condition.
        #
        # `as_of` (item 14, added this session): a CN dated after the
        # selected date didn't exist yet as of that date - same "future
        # dated line contributes nothing yet" rule compute_invoice_position
        # already applies to Sales/DN. Every row still appears (nothing
        # here is ever hidden), only the Unapplied CN Amount is zeroed
        # for a CN not yet in effect as of the chosen date.
        display_rows = [
            {
                "row": row,
                "unapplied_amount": (
                    row.cn_amount
                    if row.bill_allocation_reference is None
                    and row.classification == RegisterClassification.CURRENT
                    and row.cn_date <= as_of
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

    # ---- Register snapshot summaries (client's explicit ask) ------------
    # A running sum-total as of the selected date, shown at the top of
    # each register - so a viewer can trust the figure on screen without
    # exporting to Excel and totaling it by hand first. Built from the
    # exact same already-computed display_rows the table itself renders
    # (never a second, separate recomputation - same principle
    # ar_mis.register_export's own docstring states for the Excel
    # exports), so a summary tile can never silently drift from the rows
    # underneath it.

    def _sales_dn_summary(display_rows: list[dict], as_of: date) -> dict:
        total_invoice_value = sum(
            (d["row"].invoice_value for d in display_rows if d["row"].invoice_date <= as_of), Decimal("0.00")
        )
        total_open_amount = Decimal("0.00")
        open_invoice_count = 0
        total_overdue_amount = Decimal("0.00")
        overdue_invoice_count = 0
        for d in display_rows:
            pos = d["position"]
            if pos.open_amount != 0:
                total_open_amount += pos.open_amount
                open_invoice_count += 1
            if pos.is_overdue:
                total_overdue_amount += pos.open_amount
                overdue_invoice_count += 1
        return {
            "as_of": as_of,
            "total_invoice_value": total_invoice_value,
            "total_open_amount": total_open_amount,
            "open_invoice_count": open_invoice_count,
            "total_overdue_amount": total_overdue_amount,
            "overdue_invoice_count": overdue_invoice_count,
        }

    def _credit_note_summary(display_rows: list[dict], as_of: date) -> dict:
        total_cn_amount = Decimal("0.00")
        total_unapplied_amount = Decimal("0.00")
        unapplied_count = 0
        pending_review_amount = Decimal("0.00")
        pending_review_count = 0
        for d in display_rows:
            row = d["row"]
            if row.cn_date > as_of:
                continue
            total_cn_amount += row.cn_amount
            if d["unapplied_amount"] is not None:
                total_unapplied_amount += d["unapplied_amount"]
                unapplied_count += 1
            if row.classification == RegisterClassification.PENDING_REVIEW:
                pending_review_amount += row.cn_amount
                pending_review_count += 1
        return {
            "as_of": as_of,
            "total_cn_amount": total_cn_amount,
            "total_unapplied_amount": total_unapplied_amount,
            "unapplied_count": unapplied_count,
            "pending_review_amount": pending_review_amount,
            "pending_review_count": pending_review_count,
        }

    def _receipt_journal_summary(display_rows: list[dict], as_of: date) -> dict:
        total_applied_amount = Decimal("0.00")
        total_unapplied_amount = Decimal("0.00")
        unapplied_count = 0
        pending_review_amount = Decimal("0.00")
        pending_review_count = 0
        for d in display_rows:
            row = d["row"]
            if row.txn_date > as_of:
                continue
            if row.target_doc_no:
                total_applied_amount += row.amount
            else:
                total_unapplied_amount += row.amount
                unapplied_count += 1
            if row.classification == RegisterClassification.PENDING_REVIEW:
                pending_review_amount += row.amount
                pending_review_count += 1
        return {
            "as_of": as_of,
            "total_applied_amount": total_applied_amount,
            "total_unapplied_amount": total_unapplied_amount,
            "unapplied_count": unapplied_count,
            "pending_review_amount": pending_review_amount,
            "pending_review_count": pending_review_count,
        }

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
        summary = _sales_dn_summary(display_rows, as_of)
        return render_template(
            "sales_dn_register.html", display_rows=display_rows, as_of=as_of.isoformat(),
            freshness=freshness, summary=summary,
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
        as_of = _parse_as_of()
        display_rows, freshness = _build_credit_note_display_rows(as_of)
        summary = _credit_note_summary(display_rows, as_of)
        return render_template(
            "credit_note_register.html", display_rows=display_rows, as_of=as_of.isoformat(),
            freshness=freshness, summary=summary,
        )

    @app.route("/registers/credit-notes/export.xlsx")
    def credit_note_register_export():
        as_of = _parse_as_of()
        display_rows, _ = _build_credit_note_display_rows(as_of)
        wb = build_credit_note_register_workbook(display_rows, as_of)
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(
            buf, as_attachment=True, download_name=f"Credit_Note_Register_{as_of.isoformat()}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.route("/registers/receipts-journals")
    def receipt_journal_register():
        as_of = _parse_as_of()
        display_rows, freshness = _build_receipt_journal_display_rows(as_of)
        summary = _receipt_journal_summary(display_rows, as_of)
        return render_template(
            "receipt_journal_register.html", display_rows=display_rows, as_of=as_of.isoformat(),
            freshness=freshness, summary=summary,
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

        From/To date range (client's second explicit ask this session):
        scopes which weeks are LISTED below, so a specific period can be
        reviewed on its own rather than scrolling the entire history. The
        Total Debtor as per Books tile, however, is always computed from
        the FULL history up to `to_date` (see compute_tb_cross_check_
        summary's own docstring) - a party untouched again inside a
        narrow display window still owes their last known balance, and
        scoping the running total to the same narrow window would
        silently understate it, not just narrow what's shown.
        """
        store = get_store()
        all_rows = store.all_weekly_snapshot_rows()
        freshness = _freshness(store.last_extraction_at())
        store.close()

        today = date.today()
        try:
            from_date = date.fromisoformat(request.args.get("from_date", ""))
        except ValueError:
            from_date = financial_year_start(today)
        try:
            to_date = date.fromisoformat(request.args.get("to_date", ""))
        except ValueError:
            to_date = today

        rows = [r for r in all_rows if from_date <= r.week_ending <= to_date]
        summary = compute_tb_cross_check_summary(all_rows, as_of=to_date)
        return render_template(
            "tb_cross_check.html", rows=rows, summary=summary, freshness=freshness,
            from_date=from_date.isoformat(), to_date=to_date.isoformat(),
        )

    @app.route("/reports/tb-cross-check/export-unreconciled")
    def export_unreconciled_parties():
        """Client's explicit ask: a one-click export of exactly the
        parties behind the "Parties currently showing a difference"
        tile, so a Maker doesn't have to scroll and eyeball the full TB
        Cross-Check table for the handful of non-zero Difference rows.
        Uses the same as-of-`to_date` latest-per-party state as that
        tile (see compute_unreconciled_parties), never the display-range-
        filtered table - the export must always match the tile's count.
        """
        today = date.today()
        try:
            to_date = date.fromisoformat(request.args.get("to_date", ""))
        except ValueError:
            to_date = today

        store = get_store()
        all_rows = store.all_weekly_snapshot_rows()
        store.close()

        mismatched = compute_unreconciled_parties(all_rows, as_of=to_date)
        wb = build_unreconciled_parties_workbook(mismatched, to_date)
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(
            buf, as_attachment=True, download_name=f"Unreconciled_Parties_{to_date.isoformat()}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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

    @app.route("/reports/register-exceptions")
    def register_exceptions_review():
        """Durable review log for "vouchers not added to a register"
        (registers.RegisterBuildExceptions) - the same real gap Drift
        Findings fixed for backdated entries, now fixed here too: this
        was previously shown once on the result page of the run that
        found it, then gone. Two dispositions, mirroring Drift Findings'
        own acknowledge/incorporate split: "reviewed, no action needed"
        (the exclusion is genuinely correct, e.g. a real Sundry Creditor)
        or "resolved via catch-up" (the party was onboarded through
        Catch Up a Party - this only records that the call was made).
        """
        store = get_store()
        records = store.all_register_build_exceptions()
        freshness = _freshness(store.last_extraction_at())
        store.close()

        open_count = sum(1 for r in records if r.status == "open")
        return render_template(
            "register_exceptions.html", records=records, open_count=open_count, freshness=freshness,
        )

    @app.route("/reports/register-exceptions/<int:exception_id>/review", methods=["POST"])
    @requires_role("maker")
    def register_exception_review_submit(exception_id):
        status = request.form.get("status", "")
        reviewed_by = request.form.get("reviewed_by", "").strip()
        reviewed_note = request.form.get("reviewed_note", "").strip()

        if status not in ("reviewed_no_action", "resolved_via_catchup"):
            flash("Choose a valid review outcome.", "error")
            return redirect(url_for("register_exceptions_review"))
        if not reviewed_by:
            flash("Enter your name to review an exception.", "error")
            return redirect(url_for("register_exceptions_review"))

        store = get_store()
        store.review_register_build_exception(exception_id, status, reviewed_by, datetime.now(), reviewed_note)
        store.close()
        flash("Exception reviewed.", "success")
        return redirect(url_for("register_exceptions_review"))

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=True)
