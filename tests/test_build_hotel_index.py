"""scripts/build_hotel_index.py: Hotel Engine statement -> date/project index."""

import importlib.util
import os

_spec = importlib.util.spec_from_file_location(
    "build_hotel_index",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "build_hotel_index.py"),
)
build_hotel_index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_hotel_index)


def _row(start, end, project, dept="Install", city="Denver", hotel="Some Hotel"):
    return {"Start Date": start, "End Date": end, "Project Name": project,
            "Department Name": dept, "Hotel City": city, "Hotel Name": hotel}


def test_build_extracts_dates_project_department_city():
    rows = [_row("06/01/2026", "06/03/2026", "Northview Church [3531]", dept="Install")]
    recs = build_hotel_index.build(rows)
    assert recs == [{
        "start": "2026-06-01", "end": "2026-06-03", "project": "3531",
        "department": "60", "city": "Denver",
    }]


def test_overhead_stays_are_skipped():
    rows = [_row("06/01/2026", "06/02/2026", "HQ Visit")]  # overhead -> no project
    assert build_hotel_index.build(rows) == []


def test_rows_without_a_resolvable_code_are_skipped():
    rows = [_row("06/01/2026", "06/02/2026", "Some Client With No Code")]
    assert build_hotel_index.build(rows) == []


def test_merge_dedupes_identical_bookings():
    a = [{"start": "2026-06-01", "end": "2026-06-02", "project": "3531", "city": "Denver"}]
    b = [{"start": "2026-06-01", "end": "2026-06-02", "project": "3531", "city": "Denver"},
         {"start": "2026-07-01", "end": "2026-07-02", "project": "4152", "city": "Boise"}]
    merged = build_hotel_index.merge(a, b)
    assert len(merged) == 2
