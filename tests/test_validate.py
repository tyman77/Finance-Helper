"""Chart-of-accounts and archived-project validation tests."""

from decimal import Decimal

from finance_helper import validate
from finance_helper.models import LineItem, SourceDocument


def _doc(line):
    return SourceDocument(source="united", destination="sage", vendor="United",
                          document_id="x", currency="USD", line_items=[line])


CHART = {
    "71000": {"title": "OH - Travel", "require_department": True,
              "disallow_direct_posting": False, "status": "Active"},
    "52300": {"title": "COGS Travel: Hotel", "require_department": False,
              "disallow_direct_posting": False, "status": "Active"},
    "50000": {"title": "Cost of Goods Sold", "require_department": False,
              "disallow_direct_posting": True, "status": "Active"},
}


def test_oh_account_requires_department():
    li = LineItem(description="HQ trip", amount=Decimal("100"), gl_account="71000")
    issues = validate.validate(_doc(li), CHART, projects={})
    assert any("requires a department" in m for m in issues)
    li.department = "10"
    assert validate.validate(_doc(li), CHART, projects={}) == []


def test_disallow_direct_posting_and_unknown_account():
    bad = LineItem(description="x", amount=Decimal("1"), gl_account="50000", department="60")
    assert any("disallows direct posting" in m for m in validate.validate(_doc(bad), CHART, projects={}))
    unknown = LineItem(description="y", amount=Decimal("1"), gl_account="99999")
    assert any("not in chart" in m for m in validate.validate(_doc(unknown), CHART, projects={}))


def test_cogs_hotel_ok_without_department():
    li = LineItem(description="hotel", amount=Decimal("100"), gl_account="52300")
    assert validate.validate(_doc(li), CHART, projects={}) == []


# --- Archived project (Sage) checks ----------------------------------------

PROJECTS = {
    "4804": {"name": "Echo Church", "status": "Active"},
    "3190": {"name": "Red Rocks Church", "status": "Archived"},
}


def test_archived_project_flagged():
    li = LineItem(description="x", amount=Decimal("1"), gl_account="52300", project="3190")
    issues = validate.validate_lines(_doc(li), chart={}, projects=PROJECTS)
    assert len(issues) == 1
    assert "3190" in issues[0]["message"] and "Red Rocks Church" in issues[0]["message"]
    assert "Archived" in issues[0]["message"]


def test_active_project_not_flagged():
    li = LineItem(description="x", amount=Decimal("1"), gl_account="52300", project="4804")
    assert validate.validate_lines(_doc(li), chart={}, projects=PROJECTS) == []


def test_project_not_in_sage_data_not_flagged():
    """A code we have no Sage record for at all isn't assumed archived —
    only an explicit non-active status is flagged."""
    li = LineItem(description="x", amount=Decimal("1"), gl_account="52300", project="9999")
    assert validate.validate_lines(_doc(li), chart={}, projects=PROJECTS) == []


def test_no_projects_data_skips_the_check_entirely():
    li = LineItem(description="x", amount=Decimal("1"), gl_account="52300", project="3190")
    assert validate.validate_lines(_doc(li), chart={}, projects={}) == []


def test_chart_and_project_checks_are_independent():
    """A line can fail both checks at once, and either can run without the
    other being configured."""
    li = LineItem(description="x", amount=Decimal("1"), gl_account="71000", project="3190")
    issues = validate.validate_lines(_doc(li), chart=CHART, projects=PROJECTS)
    messages = " ".join(i["message"] for i in issues)
    assert "requires a department" in messages
    assert "Archived" in messages
