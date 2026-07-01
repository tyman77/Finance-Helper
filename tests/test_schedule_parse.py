"""Tests for the crew-grid parser (scripts/fetch_schedule_index.parse_grid).

Verifies it tolerates tabs with different numbers of leading columns, since the
real planner has a 3-column lead (Project / Person / Role) on some tabs and a
1-column lead on others.
"""

import importlib.util
import os

_spec = importlib.util.spec_from_file_location(
    "fetch_schedule_index",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "fetch_schedule_index.py"),
)
fetch_schedule_index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_schedule_index)
parse_grid = fetch_schedule_index.parse_grid


def test_parse_grid_three_leading_columns():
    # Project | Person | Role, then daily cells (mirrors the 2026 tab).
    values = [
        ["", "", "", "January/26", "", "", ""],
        ["ID : Project", "Person", "Role", "1/1", "1/2", "1/3", "1/4"],
        ["Olive Knolls", "Hal Seefeld", "Leads", "✈️", "4499", "4499", "4499"],
        ["Compass", "Zach Kay", "JRs", "4436", "4436", "HQ", "PTO"],
    ]
    idx = parse_grid(values, 2026)
    assert idx["Hal Seefeld"]["2026-01-02"] == "4499"
    assert idx["Hal Seefeld"]["2026-01-01"] == "✈️"
    assert idx["Zach Kay"]["2026-01-01"] == "4436"
    assert idx["Zach Kay"]["2026-01-03"] == "HQ"


def test_parse_grid_single_leading_column():
    # Name in column 0, daily cells after (mirrors the 2025 tab).
    values = [
        ["Foreman/Technicians", "3/1", "3/2", "3/3"],
        ["James Haynes", "4211", "4211", "REST"],
    ]
    idx = parse_grid(values, 2025)
    assert idx["James Haynes"]["2025-03-01"] == "4211"
    assert idx["James Haynes"]["2025-03-03"] == "REST"


def test_parse_grid_ignores_non_name_rows():
    values = [
        ["", "Person", "1/1", "1/2"],
        ["", "", "4499", "4499"],          # no name -> skipped
        ["", "Total", "", ""],             # not name-like + empty -> skipped
        ["", "Hal Seefeld", "5555", ""],
    ]
    idx = parse_grid(values, 2026)
    assert list(idx) == ["Hal Seefeld"]
    assert idx["Hal Seefeld"]["2026-01-01"] == "5555"
