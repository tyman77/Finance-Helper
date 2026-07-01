"""Hotel Engine enrichment: Department + Project dimensions from the file."""

from finance_helper import enrich, sources


def _by_project_name(doc, needle):
    return [li for li in doc.line_items if needle in (li.raw.get("Project Name") or "")]


def test_department_and_project_from_columns():
    doc = sources.load("hotel_engine", "samples/hotel_engine_sample.csv")
    enrich.enrich_hotel_engine(doc, registry={})

    # Department mapped from Department Name (incl. plural "Solutions Architects").
    assert all(li.department == "60" for li in _by_project_name(doc, "PPN 4499"))
    assert all(li.department == "30" for li in _by_project_name(doc, "Echo Church"))
    assert all(li.department == "10" for li in _by_project_name(doc, "HQ Visit"))

    # Project code extracted from the Project Name.
    assert all(li.project == "4499" for li in _by_project_name(doc, "PPN 4499"))
    assert all(li.project == "4804" for li in _by_project_name(doc, "Echo Church"))


def test_overhead_project_names_set_account_and_no_project():
    doc = sources.load("hotel_engine", "samples/hotel_engine_sample.csv")
    enrich.enrich_hotel_engine(doc, registry={})
    hq = _by_project_name(doc, "HQ Visit")
    assert hq and all(li.gl_account == "71000" for li in hq)   # OH - Travel
    assert all(li.project is None for li in hq)                # overhead != project work


def test_registry_fills_code_for_named_booking():
    doc = sources.load("hotel_engine", "samples/hotel_engine_sample.csv")
    registry = {"registry": {"2493": {"client": "Grace Bible Church"}},
                "index": {"echochurchexample": ["4804"]}}
    # Echo Church row already has an explicit 4804; registry shouldn't override it.
    enrich.enrich_hotel_engine(doc, registry=registry)
    assert all(li.project == "4804" for li in _by_project_name(doc, "Echo Church"))
