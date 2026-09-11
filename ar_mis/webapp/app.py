"""Local web application: Branch Master (extraction config as
user-editable data, not hardcoded Python) and Test Extraction (a
self-serve way to check "can this reach my Tally and pull real data"
without touching code or waiting on a build environment that has no
LAN access to any real Tally instance).

Runs on localhost only, matching the spec's LAN-only/no-cloud-dependency
framing - this is an internal single-operator tool, not a public service.
No authentication: there is nothing here that isn't also reachable by
running the CLI directly, and it is not meant to be exposed beyond the
machine (or LAN) that also has Tally access.
"""
from __future__ import annotations

from datetime import date, timedelta

from flask import Flask, flash, redirect, render_template, request, url_for

from ar_mis.config import BranchConfig
from ar_mis.storage import Store
from ar_mis.tally_client import CompanyMismatchError, TallyClient, TallyConnectionError


def create_app(db_path: str = "data/ar_mis.db") -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.secret_key = "ar-mis-local-tool"  # localhost-only internal tool; no session security needed

    def get_store() -> Store:
        return Store(app.config["DB_PATH"])

    @app.route("/")
    def home():
        store = get_store()
        branch_count = len(store.list_branches())
        store.close()
        return render_template("home.html", branch_count=branch_count)

    @app.route("/branches")
    def branches_list():
        store = get_store()
        branches = store.list_branches()
        store.close()
        return render_template("branches_list.html", branches=branches)

    @app.route("/branches/new", methods=["GET", "POST"])
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
        return render_template("branch_form.html", branch=None)

    @app.route("/branches/<branch_id>/edit", methods=["GET", "POST"])
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
    def branch_delete(branch_id):
        store = get_store()
        store.delete_branch(branch_id)
        store.close()
        flash("Branch removed.", "success")
        return redirect(url_for("branches_list"))

    @app.route("/test-extraction", methods=["GET", "POST"])
    def test_extraction():
        store = get_store()
        branches = store.list_branches()
        result = None

        if request.method == "POST":
            branch_id = request.form.get("branch_id", "")
            branch = store.get_branch(branch_id)
            if branch is None:
                flash("Select a branch first.", "error")
                store.close()
                return render_template("test_extraction.html", branches=branches, result=None)

            days = int(request.form.get("days") or 7)
            to_date = date.today()
            from_date = to_date - timedelta(days=days)
            steps: list[tuple[str, bool, str]] = []
            client = TallyClient(branch=branch, timeout_seconds=15.0)

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

            result = {"branch": branch, "steps": steps, "all_ok": all(ok for _, ok, _ in steps)}

        store.close()
        return render_template("test_extraction.html", branches=branches, result=result)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=True)
