"""Tests for scripts/build_traveler_map.py's person-name resolution.

Regression: several real United history rows have a blank or junk Person
column (literally "Customer" in some rows) even though Department is coded
correctly. That blank `person` value broke the crew-schedule/calendar lookup
downstream, since those are keyed by name. build() should fall back to a
best-effort guess from the passenger name itself in that case.
"""

import csv
import importlib.util
import os

import pytest

_spec = importlib.util.spec_from_file_location(
    "build_traveler_map",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "build_traveler_map.py"),
)
build_traveler_map = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_traveler_map)


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path, monkeypatch):
    """Point FINANCE_HELPER_DATA at an empty temp dir so these tests never read
    the real (gitignored) data/name_aliases.yml sitting in the repo."""
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(tmp_path / "isolated_data"))


def _write_csv(path, rows):
    fieldnames = ["Passenger Name", "Person", "Department", "Account"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def test_blank_person_falls_back_to_name_guess(tmp_path):
    path = tmp_path / "hist.csv"
    _write_csv(path, [
        {"Passenger Name": "JUDY/JOSHUA", "Person": "", "Department": "60--Install Team", "Account": "52200"},
        {"Passenger Name": "JUDY/JOSHUA", "Person": "", "Department": "60--Install Team", "Account": "52200"},
    ])
    data = build_traveler_map.build(str(path))
    assert data["JUDY/JOSHUA"]["person"] == "Joshua Judy"


def test_junk_person_value_treated_as_blank(tmp_path):
    path = tmp_path / "hist.csv"
    _write_csv(path, [
        {"Passenger Name": "HARGADINE/CHRISTIAN", "Person": "Customer",
         "Department": "10--Sales Team", "Account": "64300"},
    ])
    data = build_traveler_map.build(str(path))
    assert data["HARGADINE/CHRISTIAN"]["person"] == "Christian Hargadine"
    assert data["HARGADINE/CHRISTIAN"]["person"] != "Customer"


def test_real_person_value_still_wins_over_guess(tmp_path):
    path = tmp_path / "hist.csv"
    _write_csv(path, [
        {"Passenger Name": "MUNGUIA/ISRAELDIEGO", "Person": "Israel Munguia",
         "Department": "60--Install Team", "Account": "52200"},
    ])
    data = build_traveler_map.build(str(path))
    assert data["MUNGUIA/ISRAELDIEGO"]["person"] == "Israel Munguia"


def test_alias_override_wins_for_nickname_mismatch(tmp_path, monkeypatch):
    """A crew-schedule nickname ("Diego Munguia") that doesn't match the legal
    name coded historically ("Israel Munguia") should be fixable via
    <data>/name_aliases.yml without re-deriving anything."""
    path = tmp_path / "hist.csv"
    _write_csv(path, [
        {"Passenger Name": "MUNGUIA/ISRAELDIEGO", "Person": "Israel Munguia",
         "Department": "60--Install Team", "Account": "52200"},
    ])
    data_dir = tmp_path / "isolated_data"
    data_dir.mkdir()
    (data_dir / "name_aliases.yml").write_text('"MUNGUIA/ISRAELDIEGO": "Diego Munguia"\n')
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(data_dir))

    data = build_traveler_map.build(str(path))
    assert data["MUNGUIA/ISRAELDIEGO"]["person"] == "Diego Munguia"
