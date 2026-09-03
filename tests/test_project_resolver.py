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


def test_extract_leading_codes_common_formats():
    assert project_resolver.extract_leading_codes("4804 - Echo Church - Ready to Finish") == ["4804"]
    assert project_resolver.extract_leading_codes("5084 | Westfield Sync up") == ["5084"]
    assert project_resolver.extract_leading_codes("3335 - LifeChurch PKR - Phase Gate") == ["3335"]
    assert project_resolver.extract_leading_codes("4798") == ["4798"]
    assert project_resolver.extract_leading_codes("Lunch") == []


def test_extract_leading_codes_year_range_is_not_a_code():
    assert project_resolver.extract_leading_codes(
        "2025–2026 High-Value Projects: Scope & Travel Review") == []
    assert project_resolver.extract_leading_codes("2025-2026 Planning") == []


def test_extract_leading_codes_slash_pair_is_ambiguous():
    assert project_resolver.extract_leading_codes("4471/3831 Combined trip") == ["4471", "3831"]


def test_resolve_calendar_title_code_autocodes_without_registry():
    """A literal project-code prefix should resolve even with no location/
    external attendees and no registry — this was the real Lynette/Echo
    Church case: the event had no location, wasn't marked external, and
    wasn't all-day, so it never reached the noise-filtered "hits" path."""
    cal = {"rlynette@summitintegrated.com": [
        {"summary": "4804 - Echo Church - Ready to Finish", "start": "2026-05-01",
         "end": "2026-05-01", "location": "", "all_day": False, "external": False, "domains": []},
    ]}
    got = project_resolver.resolve_calendar("rlynette@summitintegrated.com", date(2026, 5, 1), cal)
    assert got["project"] == "4804"
    assert got["account"] == "52200"


def test_resolve_calendar_title_code_ignores_year_range_noise():
    cal = {"x@summitintegrated.com": [
        {"summary": "2025–2026 High-Value Projects: Scope & Travel Review",
         "start": "2026-05-01", "end": "2026-05-01", "location": "",
         "all_day": False, "external": False, "domains": []},
    ]}
    got = project_resolver.resolve_calendar("x@summitintegrated.com", date(2026, 5, 1), cal)
    assert got is None    # no travel-relevant signal at all, correctly falls through


def test_resolve_calendar_multiple_title_codes_are_candidates():
    cal = {"x@summitintegrated.com": [
        {"summary": "4471 - Site A", "start": "2026-05-01", "end": "2026-05-01",
         "location": "", "all_day": False, "external": False, "domains": []},
        {"summary": "3831 - Site B", "start": "2026-05-02", "end": "2026-05-02",
         "location": "", "all_day": False, "external": False, "domains": []},
    ]}
    got = project_resolver.resolve_calendar("x@summitintegrated.com", date(2026, 5, 1), cal)
    assert "project" not in got
    assert got["candidates"] == ["3831", "4471"]


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


def test_match_project_unique_and_candidates():
    registry = {
        "registry": {"3428": {"client": "Northview Church"},
                     "2630": {"client": "Life.Church"}, "3642": {"client": "Life.Church"}},
        "index": {"northviewchurch": ["3428"], "lifechurch": ["2630", "3642"]},
    }
    # Unique client -> auto project + 52200
    uniq = project_resolver.match_project(
        [{"summary": "Northview Church - Camera Upgrade", "domains": []}], registry)
    assert uniq["project"] == "3428" and uniq["account"] == "52200"
    # Client via attendee domain with multiple codes -> candidates for review
    multi = project_resolver.match_project(
        [{"summary": "Studio C Bid", "domains": ["life.church"]}], registry)
    assert "project" not in multi and multi["candidates"] == ["2630", "3642"]


def test_resolve_calendar_registry_autocode():
    registry = {"registry": {"3428": {"client": "Northview Church"}},
                "index": {"northviewchurch": ["3428"]}}
    cal = {"jdoe@summitintegrated.com": [
        {"summary": "Northview Church - Install", "start": "2026-05-10", "end": "2026-05-12",
         "location": "Carmel, IN", "all_day": False, "external": True, "domains": []}]}
    got = project_resolver.resolve_calendar("jdoe@summitintegrated.com", date(2026, 5, 10), cal, registry)
    assert got["project"] == "3428"
    assert got["account"] == "52200"


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


def test_enrich_installer_falls_back_to_calendar_when_not_on_schedule():
    """An installer not on the crew grid (e.g. no longer current crew) should
    still get a shot at their calendar instead of being left with only the
    account hint — regression for a bug where dept==Install stopped at the
    schedule lookup even when it returned nothing."""
    doc = sources.load("united", "samples/united_sample.csv")
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "60--Install Team",
                         "department_confidence": 1.0, "account_hint": "52200--x",
                         "account_confidence": 0.4, "n": 5}}
    schedule = {"Someone Else": {"2026-05-11": "5555"}}  # John Doe not on the sheet
    cal = {"jdoe@summitintegrated.com": [
        {"summary": "Northview Church - Install", "start": "2026-05-10", "end": "2026-05-12",
         "location": "Carmel, IN", "all_day": False, "external": True, "domains": []}]}
    registry = {"registry": {"3428": {"client": "Northview Church"}},
                "index": {"northviewchurch": ["3428"]}}
    enrich.enrich_united(doc, tmap, schedule_index=schedule, calendar_index=cal, registry=registry)
    john = next(li for li in doc.line_items if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.project == "3428"             # resolved via calendar registry fallback
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


# --- Archived-project filtering ------------------------------------------

def test_load_active_projects_from_file(tmp_path):
    f = tmp_path / "sage_projects.json"
    f.write_text('{"4804": {"status": "Active"}, "3190": {"status": "Archived"}, '
                 '"5036": {"status": "active"}}')  # case-insensitive match
    active = project_resolver.load_active_projects(str(f))
    assert active == {"4804", "5036"}


def test_load_active_projects_missing_file_returns_none():
    assert project_resolver.load_active_projects("/nonexistent/path.json") is None


def test_resolve_schedule_skips_archived_for_next_active_code():
    idx = {"Hal Seefeld": {
        "2026-05-18": "3190",  # most common, but archived
        "2026-05-19": "3190",
        "2026-05-20": "4499",  # active, second most common
    }}
    active = {"4499"}  # 3190 is not in the active set -> treated as archived
    got = project_resolver.resolve_schedule("Hal Seefeld", date(2026, 5, 18), idx, active)
    assert got["project"] == "4499"


def test_resolve_schedule_no_active_code_returns_none():
    idx = {"Hal Seefeld": {"2026-05-18": "3190"}}
    got = project_resolver.resolve_schedule("Hal Seefeld", date(2026, 5, 18), idx, active_projects=set())
    assert got is None


def test_match_project_filters_archived_candidates():
    registry = {"registry": {"2630": {"client": "Life.Church"}, "3063": {"client": "Life.Church"},
                             "3642": {"client": "Life.Church"}},
                "index": {"lifechurch": ["2630", "3063", "3642"]}}
    events = [{"summary": "Life.Church visit", "domains": []}]
    # Without filtering: three old codes are genuinely ambiguous.
    assert project_resolver.match_project(events, registry)["candidates"] == ["2630", "3063", "3642"]
    # With only one still active in Sage, the ambiguity resolves cleanly —
    # this is the actual point of the feature, not just "hide archived ones".
    got = project_resolver.match_project(events, registry, active_projects={"3642"})
    assert got["project"] == "3642"
    assert "candidates" not in got


def test_match_project_all_candidates_archived_returns_none():
    registry = {"registry": {"2630": {"client": "Life.Church"}},
                "index": {"lifechurch": ["2630"]}}
    events = [{"summary": "Life.Church visit", "domains": []}]
    assert project_resolver.match_project(events, registry, active_projects=set()) is None


def test_resolve_calendar_title_code_archived_falls_through():
    cal = {"x@summitintegrated.com": [
        {"summary": "4804 - Echo Church - Ready to Finish", "start": "2026-05-01",
         "end": "2026-05-01", "location": "", "all_day": False, "external": False, "domains": []},
    ]}
    # 4804 is archived and there's no other travel-relevant signal -> no match at all,
    # not a silent fall-through to the archived code.
    got = project_resolver.resolve_calendar("x@summitintegrated.com", date(2026, 5, 1), cal,
                                            active_projects=set())
    assert got is None


def test_enrich_united_never_assigns_an_archived_project(monkeypatch):
    """End-to-end: an installer whose schedule cell is an archived project
    should not get it auto-coded, even though the underlying schedule data
    is unchanged — the filter has to actually reach through enrich_united."""
    doc = sources.load("united", "samples/united_sample.csv")
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "60--Install Team",
                         "department_confidence": 1.0, "account_hint": "52200--x",
                         "account_confidence": 0.4, "n": 5}}
    schedule = {"John Doe": {"2026-05-11": "5555"}}
    enrich.enrich_united(doc, tmap, schedule_index=schedule, calendar_index={},
                         active_projects=set())  # 5555 is not active
    john = next(li for li in doc.line_items if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.project != "5555"


def test_hotel_projects_in_window_matches_overlapping_bookings():
    idx = [
        {"start": "2026-05-31", "end": "2026-06-02", "project": "3531", "department": "10", "city": "Denver"},
        {"start": "2026-06-01", "end": "2026-06-03", "project": "9999", "department": "60", "city": "Phoenix"},
        {"start": "2026-07-01", "end": "2026-07-02", "project": "1111", "department": "10", "city": "Boise"},
    ]
    # A June 1 departure overlaps the first two, not the July booking.
    assert set(project_resolver.hotel_projects_in_window(idx, date(2026, 6, 1))) == {"3531", "9999"}
    # Department filter narrows to the sales booking.
    assert project_resolver.hotel_projects_in_window(idx, date(2026, 6, 1), department="10") == ["3531"]
    # Archived filter drops codes not in the active set.
    assert project_resolver.hotel_projects_in_window(
        idx, date(2026, 6, 1), active_projects={"9999"}) == ["9999"]
    # Nothing overlaps a far-off date.
    assert project_resolver.hotel_projects_in_window(idx, date(2026, 9, 1)) == []


def test_hotel_cross_reference_auto_fills_single_agreement():
    """History says several projects; the hotel that week pins exactly one."""
    doc = sources.load("united", "samples/united_sample.csv")
    for li in doc.line_items:
        if li.raw.get("Passenger Name") == "DOE/JOHN":
            li.raw["Departure Date"] = "06/01/2026"
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "10--Sales Team",
                         "department_confidence": 1.0, "account_hint": "71000--OH",
                         "account_confidence": 0.6, "projects": ["3976", "3531", "4152"], "n": 10}}
    hotel = [{"start": "2026-05-31", "end": "2026-06-02", "project": "3531",
              "department": "10", "city": "Denver"}]
    enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index={}, roster={},
                         registry={}, active_projects=None, hotel_index=hotel)
    john = next(li for li in doc.line_items if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.project == "3531"
    assert "hotel booking that week" in john.note


def test_hotel_cross_reference_narrows_but_does_not_auto_fill_when_ambiguous():
    doc = sources.load("united", "samples/united_sample.csv")
    for li in doc.line_items:
        if li.raw.get("Passenger Name") == "DOE/JOHN":
            li.raw["Departure Date"] = "06/01/2026"
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "10--Sales Team",
                         "department_confidence": 1.0, "account_hint": "71000--OH",
                         "account_confidence": 0.6, "projects": ["3976", "3531", "4152"], "n": 10}}
    # Two of the traveler's historical projects both had hotels that week.
    hotel = [{"start": "2026-05-31", "end": "2026-06-02", "project": "3531", "department": "10", "city": "Denver"},
             {"start": "2026-06-01", "end": "2026-06-02", "project": "4152", "department": "10", "city": "Boise"}]
    enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index={}, roster={},
                         registry={}, active_projects=None, hotel_index=hotel)
    john = next(li for li in doc.line_items if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.project is None  # ambiguous -> not auto-filled
    assert "past projects 3531, 4152 — pick one" in john.note  # narrowed to the two with hotels


def test_hotel_projects_for_person_requires_named_guest_and_overlap():
    idx = [
        {"start": "2026-06-01", "end": "2026-06-05", "project": "4499",
         "guests": ["Jake Cody"], "department": "60"},
        {"start": "2026-06-01", "end": "2026-06-05", "project": "9999",
         "guests": ["Someone Else"], "department": "60"},
        {"start": "2026-07-10", "end": "2026-07-12", "project": "1111",
         "guests": ["Jake Cody"], "department": "60"},
    ]
    got = project_resolver.hotel_projects_for_person(idx, "Jake Cody", date(2026, 6, 2))
    assert got == ["4499"]                       # names him AND overlaps
    assert project_resolver.hotel_projects_for_person(idx, "jake  CODY", date(2026, 6, 2)) == ["4499"]
    assert project_resolver.hotel_projects_for_person(idx, "Jake Cody", date(2026, 6, 2),
                                                      active_projects=set()) == []


def test_flight_auto_tags_project_from_named_hotel_stay():
    doc = sources.load("united", "samples/united_sample.csv")
    for li in doc.line_items:
        if li.raw.get("Passenger Name") == "DOE/JOHN":
            li.raw["Departure Date"] = "06/02/2026"
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "10--Sales Team",
                         "department_confidence": 1.0, "account_hint": "71000--OH",
                         "account_confidence": 0.6, "projects": [], "n": 10}}
    hotel = [{"start": "2026-06-01", "end": "2026-06-05", "project": "3531",
              "guests": ["John Doe"], "department": "10", "city": "Denver"}]
    enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index={}, roster={},
                         registry={}, active_projects=None, hotel_index=hotel)
    john = next(li for li in doc.line_items if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.project == "3531"
    assert john.gl_account == "52200"            # project work -> COGS
    assert "hotel stay names John Doe" in john.note


def test_flight_offers_chips_when_person_has_two_stays_that_week():
    doc = sources.load("united", "samples/united_sample.csv")
    for li in doc.line_items:
        if li.raw.get("Passenger Name") == "DOE/JOHN":
            li.raw["Departure Date"] = "06/02/2026"
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "10--Sales Team",
                         "department_confidence": 1.0, "account_hint": "71000--OH",
                         "account_confidence": 0.6, "projects": [], "n": 10}}
    hotel = [{"start": "2026-06-01", "end": "2026-06-03", "project": "3531",
              "guests": ["John Doe"]},
             {"start": "2026-06-03", "end": "2026-06-05", "project": "4499",
              "guests": ["John Doe"]}]
    enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index={}, roster={},
                         registry={}, active_projects=None, hotel_index=hotel)
    john = next(li for li in doc.line_items if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.project is None
    assert "hotel-stay projects 3531, 4499 — pick one" in john.note
    from finance_helper.web.app import _line_candidates
    assert _line_candidates(john.note) == ["3531", "4499"]


def test_same_person_handles_real_name_variants():
    assert project_resolver.same_person("Jake Cody", "Jacob Cody")          # nickname
    assert project_resolver.same_person("Jake Cody", "Jake Lee Cody")       # middle name
    assert project_resolver.same_person("Jake Cody", "Cody, Jake")          # last-first
    assert project_resolver.same_person("JAKE CODY", "jake cody")           # case
    assert not project_resolver.same_person("Jake Cody", "Natalie Cody")    # sibling
    assert not project_resolver.same_person("Jake Cody", "Jake Brady")
    assert not project_resolver.same_person("", "Jake Cody")


def test_fuzzy_guest_names_still_match_stays():
    idx = [{"start": "2026-07-06", "end": "2026-07-09", "project": "4232",
            "guests": ["Keanon James Davidson"]}]
    got = project_resolver.hotel_projects_for_person(idx, "Keanon Davidson", date(2026, 7, 6))
    assert got == ["4232"]


def test_no_match_notes_explain_which_link_broke():
    def run_with(hotel_index, active=None):
        doc = sources.load("united", "samples/united_sample.csv")
        for li in doc.line_items:
            if li.raw.get("Passenger Name") == "DOE/JOHN":
                li.raw["Departure Date"] = "07/06/2026"
        tmap = {"DOE/JOHN": {"person": "John Doe", "department": "10--Sales Team",
                             "department_confidence": 1.0, "account_hint": "71000--OH",
                             "account_confidence": 0.6, "projects": [], "n": 10}}
        enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index={}, roster={},
                             registry={}, active_projects=active, hotel_index=hotel_index)
        return next(li for li in doc.line_items
                    if li.raw.get("Passenger Name") == "DOE/JOHN").note

    # Stay overlaps but carries no project number.
    note = run_with([{"start": "2026-07-05", "end": "2026-07-08", "project": None,
                      "guests": ["John Doe"], "hotel": "La Quinta"}])
    assert "carries no project number" in note and "La Quinta" in note
    # Stay overlaps but its code is archived.
    note = run_with([{"start": "2026-07-05", "end": "2026-07-08", "project": "5555",
                      "guests": ["John Doe"]}], active=set())
    assert "archived in Sage" in note
    # Stays exist, wrong dates.
    note = run_with([{"start": "2026-05-01", "end": "2026-05-03", "project": "4499",
                      "guests": ["John Doe"]}])
    assert "other dates" in note
    # Index has no guest names at all.
    note = run_with([{"start": "2026-07-05", "end": "2026-07-08", "project": "4499",
                      "guests": []}])
    assert "no guest names" in note
    # No index at all.
    note = run_with([])
    assert "no hotel stays indexed yet" in note


def test_route_states_excludes_home_airport_not_position():
    assert project_resolver.route_states("DEN AUS DEN") == ["TX"]
    assert project_resolver.route_states("DEN PIT DEN") == ["PA"]
    # One-way RETURN: the traveler is coming back FROM the Wichita job.
    assert project_resolver.route_states("ICT DEN") == ["KS"]
    assert project_resolver.route_states("DEN DFW ATL DEN") == ["TX", "GA"]
    assert project_resolver.route_states("") == []
    assert project_resolver.route_states("XXX YYY") == []
    assert project_resolver.route_states("DEN DEN") == []


def test_destination_state_narrows_candidates_to_one():
    """Two historical candidates; the flight lands in the one's state ->
    auto-filled with a confirm note."""
    doc = sources.load("united", "samples/united_sample.csv")
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "60--Install",
                         "department_confidence": 1.0, "account_hint": "52200--COGS",
                         "account_confidence": 0.9,
                         "projects": ["3495", "4048"], "n": 10}}
    registry = {"registry": {
        "3495": {"client": "Rock Point Church, AZ"},
        "4048": {"client": "Traders Point, IN"},
    }}
    # Sample routing "DEN AUS DEN" lands in TX — matches neither candidate,
    # so nothing is auto-filled from the destination.
    doc2 = enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index={},
                                roster={}, registry=registry, active_projects=None,
                                hotel_index=[], ramp_index=[], timecard_index={})
    john2 = next(li for li in doc2.line_items
                 if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john2.project is None

    doc3 = sources.load("united", "samples/united_sample.csv")
    for li in doc3.line_items:
        if li.raw.get("Passenger Name") == "DOE/JOHN":
            li.raw["Routing (Origin To To To To )"] = "DEN PHX DEN"
    doc3 = enrich.enrich_united(doc3, tmap, schedule_index={}, calendar_index={},
                                roster={}, registry=registry, active_projects=None,
                                hotel_index=[], ramp_index=[], timecard_index={})
    john3 = next(li for li in doc3.line_items
                 if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john3.project == "3495"
    assert john3.gl_account == "52200"
    assert "flight lands in AZ -> project 3495" in john3.note


def test_destination_state_suggests_registry_projects_when_no_history():
    """No candidate list at all: the active projects in the destination state
    become the suggestion."""
    doc = sources.load("united", "samples/united_sample.csv")
    for li in doc.line_items:
        if li.raw.get("Passenger Name") == "DOE/JOHN":
            li.raw["Routing (Origin To To To To )"] = "DEN PHX DEN"
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "60--Install",
                         "department_confidence": 1.0, "account_hint": "52200--COGS",
                         "account_confidence": 0.4, "projects": [], "n": 10}}
    registry = {"registry": {
        "3495": {"client": "Rock Point Church, AZ"},
        "3496": {"client": "Desert Springs, AZ"},
        "4048": {"client": "Traders Point, IN"},
    }}
    doc = enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index={},
                               roster={}, registry=registry, active_projects=None,
                               hotel_index=[], ramp_index=[], timecard_index={})
    john = next(li for li in doc.line_items
                if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.project is None
    assert "flight lands in AZ — projects there: 3495, 3496 — pick one" in john.note


def test_auto_coded_project_contradicting_flight_gets_flagged():
    """August statement finding: DILL flew DEN MCI DEN (Missouri) but the
    schedule pinned project 3495 (Rock Point Church, AZ) — that sailed
    through as auto-coded. Now it's flagged for review."""
    doc = sources.load("united", "samples/united_sample.csv")
    for li in doc.line_items:
        if li.raw.get("Passenger Name") == "DOE/JOHN":
            li.raw["Routing (Origin To To To To )"] = "DEN MCI DEN"
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "60--Install",
                         "department_confidence": 1.0, "account_hint": "52200--COGS",
                         "account_confidence": 0.9, "projects": [], "n": 10}}
    registry = {"registry": {"3495": {"client": "Rock Point Church, AZ"},
                             "4960": {"client": "Grace Chapel, MO"}}}
    schedule = {"John Doe": {"2026-05-10": "3495", "2026-05-11": "3495",
                             "2026-05-12": "3495"}}
    doc = enrich.enrich_united(doc, tmap, schedule_index=schedule,
                               calendar_index={}, roster={}, registry=registry,
                               active_projects=None, hotel_index=[],
                               ramp_index=[], timecard_index={})
    john = next(li for li in doc.line_items
                if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.project is None            # cleared — it was almost surely wrong
    assert john.needs_review
    assert "project 3495 is in AZ but the flight went to MO — double-check" in john.note
    # The old project and the projects actually in MO become the pick list.
    assert "registry: candidate projects 3495, 4960 — pick one" in john.note


def test_skip_calendar_env_disables_calendar_coding(monkeypatch):
    monkeypatch.setenv("FINANCE_HELPER_SKIP_CALENDAR", "1")
    doc = sources.load("united", "samples/united_sample.csv")
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "60--Install",
                         "department_confidence": 1.0, "account_hint": "52200--COGS",
                         "account_confidence": 0.9, "projects": [], "n": 10}}
    cal = {"jdoe@summitintegrated.com": [
        {"summary": "5368 Emmaus install", "start": "2026-05-10",
         "end": "2026-05-12", "location": "Atlanta, GA", "all_day": True,
         "external": False, "domains": []}]}
    roster = {"John Doe": "jdoe@summitintegrated.com"}
    doc = enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index=cal,
                               roster=roster, registry={}, active_projects=None,
                               hotel_index=[], ramp_index=[], timecard_index={})
    john = next(li for li in doc.line_items
                if li.raw.get("Passenger Name") == "DOE/JOHN")
    assert john.project is None
    assert "calendar" not in (john.note or "").lower()


def test_club_subscription_is_overhead_never_project(monkeypatch):
    """The $750 FRIES club subscription kept getting a project from the
    hotel matcher (and its ORD IAH "route" defeats the destination check) —
    a membership is 71000 overhead, full stop."""
    doc = sources.load("united", "samples/united_sample.csv")
    target = None
    for li in doc.line_items:
        if li.raw.get("Passenger Name") == "DOE/JOHN":
            li.description = "DOE       /CLUB SUBSCRIPTION ORD IAH"
            target = li
    tmap = {"DOE/JOHN": {"person": "John Doe", "department": "30--Solution Architect",
                         "department_confidence": 1.0, "account_hint": "52200--COGS",
                         "account_confidence": 0.9, "projects": ["4570"], "n": 10}}
    hotel = [{"start": "2026-05-09", "end": "2026-05-12", "project": "4570",
              "department": "30", "city": "Dallas", "guests": ["John Doe"]}]
    doc = enrich.enrich_united(doc, tmap, schedule_index={}, calendar_index={},
                               roster={}, registry={}, active_projects=None,
                               hotel_index=hotel, ramp_index=[], timecard_index={})
    assert target.gl_account == "71000"
    assert target.project is None
    assert target.department == "30"     # traveler's dept still carried
    assert "United Club membership" in target.note
