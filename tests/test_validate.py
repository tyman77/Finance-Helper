"""Chart-of-accounts validation tests."""

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
    issues = validate.validate(_doc(li), CHART)
    assert any("requires a department" in m for m in issues)
    li.department = "10"
    assert validate.validate(_doc(li), CHART) == []


def test_disallow_direct_posting_and_unknown_account():
    bad = LineItem(description="x", amount=Decimal("1"), gl_account="50000", department="60")
    assert any("disallows direct posting" in m for m in validate.validate(_doc(bad), CHART))
    unknown = LineItem(description="y", amount=Decimal("1"), gl_account="99999")
    assert any("not in chart" in m for m in validate.validate(_doc(unknown), CHART))


def test_cogs_hotel_ok_without_department():
    li = LineItem(description="hotel", amount=Decimal("100"), gl_account="52300")
    assert validate.validate(_doc(li), CHART) == []
