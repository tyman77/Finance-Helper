"""Tests for the two-path project resolver and its enrichment wiring."""

from datetime import date

from finance_helper import enrich, project_resolver, sources


def test_email_for():
    assert project_resolver.email_for("CLARK/JOHN") == "jclark@summitintegrated.com"
    assert project_resolver.email_for("CODY/JACOBLEE") == "jcody@summitintegrated.com"
    assert project_resolver.email_for("SPRENG   /FIRST CHECKED BAG") is not None


def test_resolve_schedule_picks_project_during_stay():
    idx = {
        "Hal Seefeld": {
            "2026-05-18": "✈️",           # travel day, ignored
            "2026-05-19": "4499",
            "2026-05-20": "4499",
            "2026-05-21": "4499",
        }
    }
    got = project_resolver.resolve_schedule("Hal Seefeld", date(2026, 5, 18), idx)
    assert got["project"] == "4499"
    assert got["account"] == "52200"


def test_resolve_calendar_surfaces_client_context():
    cal = {
        "andrew@summitintegrated.com": [
            {"summary": "Studio C Bid: OKC SOW/Part List Review",
             "start": "2026-05-27", "end": "2026-05-27",
             "location": "Microsoft Teams Meeting", "all_day": False, "external": True},
            {"summary": "Lunch", "start": "2026-05-27", "end": "2026-05-27",
             "location": "", "all_day": False, "external": False},
        ]
    }
    got = project_resolver.resolve_calendar("andrew@summitintegrated.com", date(2026, 5, 27), cal)
    assert "Studio C Bid" in got["note"]      # client/trip event surfaced
    assert "Lunch" not in got["note"]         # internal noise filtered out
    assert "account" not in got               # personal calendars give context, not a code


def test_resolve_calendar_no_match_returns_none():
    cal = {"andrew@summitintegrated.com": [
        {"summary": "Emails / Slack", "start": "2026-05-18", "end": "2026-05-18",
         "location": "", "all_day": False, "external": False}]}
    assert project_resolver.resolve_calendar("andrew@summitintegrated.com", date(2026, 5, 18), cal) is None


def test_enrich_routes_installer_to_schedule():
    doc = sources.load("united", "samples/united_sample.csv")
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "60--Install Team",
                         "department_confidence": 1.0, "account_hint": "52200--x",
                         "account_confidence": 0.4, "n": 5}}
    schedule = {"John Doe": {"2026-05-11": "5555", "2026-05-12": "5555"}}
    enrich.enrich_united(doc, tmap, schedule_index=schedule, calendar_index=[])
    john = next(li for li in doc.line_items if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.project == "5555"
    assert john.gl_account == "52200"


def test_enrich_routes_noninstaller_to_calendar():
    doc = sources.load("united", "samples/united_sample.csv")
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "10--Sales Team",
                         "department_confidence": 1.0, "account_hint": "71000--OH Travel",
                         "account_confidence": 0.4, "n": 5}}
    cal = {"jdoe@summitintegrated.com": [
        {"summary": "First Baptist Dallas - Discovery", "start": "2026-05-10", "end": "2026-05-12",
         "location": "Dallas, TX", "all_day": False, "external": True}]}
    roster = {"John Doe": "jdoe@summitintegrated.com"}
    enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index=cal, roster=roster)
    john = next(li for li in doc.line_items if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.gl_account == "71000"                 # keeps traveler's usual OH account
    assert "First Baptist Dallas" in john.note        # calendar context attached for review
    assert john.needs_review
