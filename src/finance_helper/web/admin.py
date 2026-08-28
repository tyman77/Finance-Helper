"""Admin panel: regenerate the gitignored data/*.json indices (traveler map,
project registry, roster, crew-schedule/calendar indices, Sage projects,
chart of accounts) directly on the server.

These files carry employee/client names, so they're gitignored and don't
come along with a deploy — this exists so a hosted deployment can build them
itself (using credentials already configured as environment variables)
instead of someone copying files off their laptop by hand.

Loads scripts/*.py by file path rather than importing them as a package
(same approach the test suite already uses) since scripts/ isn't part of
the installed finance_helper package.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path

import yaml
from flask import Blueprint, flash, redirect, render_template, request, url_for

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_build_traveler_map = _load_script("build_traveler_map")
_build_project_registry = _load_script("build_project_registry")
_build_roster = _load_script("build_roster")
_build_chart = _load_script("build_chart")
_build_hotel_index = _load_script("build_hotel_index")
_fetch_schedule_index = _load_script("fetch_schedule_index")
_fetch_calendar_index = _load_script("fetch_calendar_index")
_fetch_sage_projects = _load_script("fetch_sage_projects")

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _data_dir() -> str:
    return os.environ.get(
        "FINANCE_HELPER_DATA", os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")
    )


def _data_path(name: str) -> str:
    return os.path.join(_data_dir(), name)


def _load_json_file(name: str):
    path = _data_path(name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# (label, filename, what it's for)
_FILES = [
    ("Traveler map", "united_travelers.yml", "How United travelers get coded (from historical export)"),
    ("Project registry", "project_registry.json", "Client name → project code (from historical export)"),
    ("Roster", "roster.json", "Traveler → calendar id (auto-drafted from the traveler map)"),
    ("Crew schedule", "schedule_index.json", "Installer project-by-day grid (from Google Sheets)"),
    ("Calendars", "calendar_index.json", "Per-traveler calendar events (from Google Calendar)"),
    ("Sage projects", "sage_projects.json", "Active/archived status per project (from Sage Intacct)"),
    ("Chart of accounts", "chart_of_accounts.json", "GL account posting rules (from Sage GL export)"),
    ("Hotel cross-reference", "hotel_project_index.json",
     "Hotel booking dates → project, to match United flights (from Hotel Engine statements)"),
    ("Ramp per-diem", "ramp_reimbursements.json",
     "Reimbursement dates & memos per person, to corroborate trips and tag flights (from Ramp API)"),
    ("Timecards", "timecards_index.json",
     "Logged hours per person/day/job — the strongest flight-project signal (Paychex API or CSV export)"),
]


def _describe(filename: str) -> str:
    path = _data_path(filename)
    if not os.path.exists(path):
        return "not generated yet"
    mtime = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) if filename.endswith(".yml") else json.load(fh)
        count = len(data.get("registry", data)) if isinstance(data, dict) else len(data)
    except Exception:
        count = "?"
    return f"{count} entries — updated {mtime}"


def _save_upload(file) -> str:
    suffix = os.path.splitext(file.filename)[1] or ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        return tmp.name


@admin_bp.get("/")
def admin_page():
    rows = [(label, filename, note, _describe(filename)) for label, filename, note in _FILES]
    return render_template(
        "admin.html",
        rows=rows,
        default_year=date.today().year,
    )


@admin_bp.post("/traveler-map")
def refresh_traveler_map():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose the historical United export CSV first.")
        return redirect(url_for("admin.admin_page"))
    tmp_path = _save_upload(file)
    try:
        travelers = _build_traveler_map.build(tmp_path)
        os.makedirs(_data_dir(), exist_ok=True)
        with open(_data_path("united_travelers.yml"), "w", encoding="utf-8") as fh:
            fh.write(_build_traveler_map.dump_yaml(travelers))

        registry = _build_project_registry.build(tmp_path)
        with open(_data_path("project_registry.json"), "w", encoding="utf-8") as fh:
            json.dump(registry, fh, indent=2)

        flash(
            f"Wrote {len(travelers)} travelers and {len(registry['registry'])} "
            "project codes from the historical export."
        )
    except Exception as exc:  # surface a friendly error instead of a 500
        flash(f"Could not process that file: {exc}")
    finally:
        os.unlink(tmp_path)
    return redirect(url_for("admin.admin_page"))


@admin_bp.post("/chart")
def refresh_chart():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose the Sage GL account export CSV first.")
        return redirect(url_for("admin.admin_page"))
    tmp_path = _save_upload(file)
    try:
        chart = _build_chart.build(tmp_path)
        os.makedirs(_data_dir(), exist_ok=True)
        with open(_data_path("chart_of_accounts.json"), "w", encoding="utf-8") as fh:
            json.dump(chart, fh, indent=2)
        flash(f"Wrote {len(chart)} GL accounts.")
    except Exception as exc:
        flash(f"Could not process that file: {exc}")
    finally:
        os.unlink(tmp_path)
    return redirect(url_for("admin.admin_page"))


@admin_bp.post("/hotel-index")
def refresh_hotel_index():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a Hotel Engine statement CSV first.")
        return redirect(url_for("admin.admin_page"))
    tmp_path = _save_upload(file)
    try:
        with open(tmp_path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        from .. import project_resolver
        registry = _load_json_file("project_registry.json") or {}
        active = project_resolver.load_active_projects()
        recs = _build_hotel_index.build(rows, registry, active)

        existing = _load_json_file("hotel_project_index.json") or []
        merged = _build_hotel_index.merge(existing, recs)
        os.makedirs(_data_dir(), exist_ok=True)
        with open(_data_path("hotel_project_index.json"), "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
        added = len(merged) - len(existing)
        flash(f"Added {added} hotel bookings ({len(merged)} total) to the cross-reference.")
    except Exception as exc:
        flash(f"Could not process that Hotel Engine file: {exc}")
    finally:
        os.unlink(tmp_path)
    return redirect(url_for("admin.admin_page"))


@admin_bp.post("/ramp")
def refresh_ramp():
    from datetime import timedelta

    from .. import ramp_api
    days = int(request.form.get("days") or 365)
    try:
        idx = ramp_api.fetch_index(date.today() - timedelta(days=days), date.today())
        os.makedirs(_data_dir(), exist_ok=True)
        with open(_data_path("ramp_reimbursements.json"), "w", encoding="utf-8") as fh:
            json.dump(idx, fh, indent=2)
        coded = sum(1 for r in idx if r.get("project"))
        flash(f"Fetched {len(idx)} Ramp reimbursements — {coded} carry a project "
              "number in the memo. Flights now cross-reference them on re-run.")
    except Exception as exc:
        flash(f"Could not fetch Ramp reimbursements: {exc}")
    return redirect(url_for("admin.admin_page"))


def _write_timecards(index: dict) -> None:
    os.makedirs(_data_dir(), exist_ok=True)
    with open(_data_path("timecards_index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)


@admin_bp.post("/timecards")
def refresh_timecards():
    from datetime import timedelta

    from .. import paychex_api
    days = int(request.form.get("days") or 120)
    try:
        index = paychex_api.fetch_index(date.today() - timedelta(days=days), date.today())
        _write_timecards(index)
        entries = sum(len(v) for v in index.values())
        flash(f"Fetched timecards for {len(index)} people ({entries} day-entries "
              "with a project code). Flights use them on re-run.")
    except Exception as exc:
        flash(f"Could not fetch Paychex timecards: {exc}")
    return redirect(url_for("admin.admin_page"))


@admin_bp.post("/timecards-csv")
def upload_timecards_csv():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a timecard export CSV first.")
        return redirect(url_for("admin.admin_page"))
    import io as _io

    from .. import paychex_api
    try:
        rows = list(csv.DictReader(_io.StringIO(file.read().decode("utf-8-sig"))))
        index = paychex_api.build_index(rows)
        # Merge into what's there so months accumulate.
        existing = _load_json_file("timecards_index.json") or {}
        for person, days_ in index.items():
            existing.setdefault(person, {}).update(days_)
        _write_timecards(existing)
        entries = sum(len(v) for v in index.values())
        flash(f"Loaded timecards for {len(index)} people ({entries} day-entries) from the CSV.")
    except Exception as exc:
        flash(f"Could not read that timecard CSV: {exc}")
    return redirect(url_for("admin.admin_page"))


@admin_bp.post("/roster")
def refresh_roster():
    tmap_path = _data_path("united_travelers.yml")
    if not os.path.exists(tmap_path):
        flash("Build the traveler map first — the roster is drawn from it.")
        return redirect(url_for("admin.admin_page"))
    try:
        with open(tmap_path, encoding="utf-8") as fh:
            travelers = yaml.safe_load(fh) or {}
        roster, review = _build_roster.build(travelers)
        with open(_data_path("roster.json"), "w", encoding="utf-8") as fh:
            json.dump(roster, fh, indent=2)
        low_confidence = sum(1 for line in review if "[convention" in line)
        flash(
            f"Wrote {len(roster)} people to the roster ({low_confidence} matched by "
            "email convention rather than a confirmed vanity calendar — spot-check "
            "those in data/roster.json)."
        )
    except Exception as exc:
        flash(f"Could not build the roster: {exc}")
    return redirect(url_for("admin.admin_page"))


@admin_bp.post("/schedule")
def refresh_schedule():
    year = int(request.form.get("year") or date.today().year)
    sheet_id = os.environ.get("SCHEDULE_SHEET_ID")
    if not sheet_id:
        flash("Set SCHEDULE_SHEET_ID (and Google credentials) as environment variables first.")
        return redirect(url_for("admin.admin_page"))
    rng = os.environ.get("SCHEDULE_SHEET_RANGE", f"'{year}'!A1:NZ1008")
    name_col = os.environ.get("SCHEDULE_NAME_COL")
    try:
        values = _fetch_schedule_index.fetch(sheet_id, rng)
        index = _fetch_schedule_index.parse_grid(values, year, int(name_col) if name_col else None)
        os.makedirs(_data_dir(), exist_ok=True)
        with open(_data_path("schedule_index.json"), "w", encoding="utf-8") as fh:
            json.dump(index, fh, indent=2)
        flash(f"Wrote {len(index)} people to the crew schedule index for {year}.")
    except Exception as exc:
        flash(f"Could not fetch the crew schedule: {exc}")
    return redirect(url_for("admin.admin_page"))


@admin_bp.post("/calendars")
def refresh_calendars():
    start = request.form.get("start", "")
    end = request.form.get("end", "")
    if not start or not end:
        flash("Pick a start and end date for the calendar fetch.")
        return redirect(url_for("admin.admin_page"))
    roster_path = _data_path("roster.json")
    if not os.path.exists(roster_path):
        flash("Build the roster first — calendars are fetched per person from it.")
        return redirect(url_for("admin.admin_page"))
    try:
        with open(roster_path, encoding="utf-8") as fh:
            roster = json.load(fh)
        use_dwd = bool(os.environ.get("USE_DWD"))
        os.makedirs(_data_dir(), exist_ok=True)
        index, skipped = _fetch_calendar_index.fetch_all(
            roster, start, end, use_dwd, checkpoint_path=_data_path("calendar_index.json")
        )
        msg = f"Wrote {len(index)} calendars ({start} to {end})."
        if skipped:
            msg += f" {len(skipped)} skipped: {', '.join(skipped)}"
        flash(msg)
    except Exception as exc:
        flash(f"Could not fetch calendars: {exc}")
    return redirect(url_for("admin.admin_page"))


@admin_bp.post("/sage-projects")
def refresh_sage_projects():
    try:
        token = _fetch_sage_projects.get_token()
        records = _fetch_sage_projects.fetch_projects(token)
        out = _fetch_sage_projects.build(records)
        os.makedirs(_data_dir(), exist_ok=True)
        with open(_data_path("sage_projects.json"), "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, default=str)
        active = sum(1 for v in out.values() if (v["status"] or "").lower() == "active")
        flash(f"Wrote {len(out)} Sage projects ({active} active).")
    except Exception as exc:
        flash(f"Could not fetch Sage projects: {exc}")
    return redirect(url_for("admin.admin_page"))
