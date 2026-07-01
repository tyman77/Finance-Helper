"""UPS enrichment: project -> COGS Shipping, tracking inheritance, overhead."""

from decimal import Decimal

from finance_helper import enrich, pipeline, sources


def _rows(doc):
    return {li.description: li for li in doc.line_items}


def test_project_from_reference_and_net_amount():
    doc = sources.load("ups", "samples/ups_sample.csv")
    enrich.enrich_ups(doc, registry={})
    # Net = Billed Charge + Incentive Credit (20.00 + -2.00).
    speaker = next(li for li in doc.line_items if "Speaker" in li.description)
    assert speaker.amount == Decimal("18.00")
    assert speaker.project == "4804"
    assert speaker.gl_account == "51700"      # COGS Shipping


def test_blank_reference_inherits_project_from_tracking():
    doc = sources.load("ups", "samples/ups_sample.csv")
    enrich.enrich_ups(doc, registry={})
    corr = next(li for li in doc.line_items if "correction" in li.description)
    assert corr.project == "4804"             # inherited from tracking 1Z0002
    assert corr.gl_account == "51700"


def test_overhead_reference_sets_postage_and_department():
    doc = sources.load("ups", "samples/ups_sample.csv")
    enrich.enrich_ups(doc, registry={})
    mkt = next(li for li in doc.line_items if "Swag" in li.description)
    assert mkt.gl_account == "65565"          # OH Postage & Shipping
    assert mkt.department == "20"             # inferred from "Marketing"
    misc = next(li for li in doc.line_items if "Misc" in li.description)
    assert misc.gl_account == "65565"
    assert misc.department is None            # no signal -> flagged for a department


def test_ups_builds_bill_with_line_coding():
    doc = pipeline.process("ups", "samples/ups_sample.csv")
    from finance_helper import destinations
    payload = destinations.build_payload(doc)
    assert payload["vendorName"] == "UPS"
    assert any(li.get("chartOfAccountId") == "51700" for li in payload["billLineItems"])
    assert any(li.get("project") == "4804" for li in payload["billLineItems"])
