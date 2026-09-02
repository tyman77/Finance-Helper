"""User access control: who gets in at all, and which sections they see."""

import pytest

from finance_helper.web import access
from finance_helper.web.app import RUNS, create_app


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_HELPER_DATA", str(tmp_path))
    monkeypatch.delenv("FINANCE_HELPER_ADMIN_EMAILS", raising=False)
    return tmp_path


def test_unknown_user_has_no_access(store):
    access.save_users({"a@x.com": {"sections": ["travel"]}})
    assert access.sections_for("stranger@x.com") is None
    assert not access.allowed("stranger@x.com", "travel")


def test_sections_and_admin_expansion(store):
    access.save_users({
        "viewer@x.com": {"sections": ["travel"]},
        "boss@x.com": {"sections": ["admin"]},
    })
    assert access.sections_for("viewer@x.com") == {"travel"}
    assert not access.allowed("viewer@x.com", "cashproof")
    # 'admin' section implies everything.
    assert access.sections_for("BOSS@x.com") == set(access.SECTIONS)
    assert access.is_admin("boss@x.com")


def test_env_admins_always_in(store, monkeypatch):
    monkeypatch.setenv("FINANCE_HELPER_ADMIN_EMAILS", "owner@x.com")
    assert access.sections_for("owner@x.com") == set(access.SECTIONS)
    # And their presence disables the first-login bootstrap.
    assert access.ensure_bootstrap_admin("someone@x.com") is False


def test_first_login_bootstraps_admin_once(store):
    assert access.ensure_bootstrap_admin("first@x.com") is True
    assert access.is_admin("first@x.com")
    assert access.ensure_bootstrap_admin("second@x.com") is False
    assert access.sections_for("second@x.com") is None


def test_endpoint_section_mapping():
    assert access.section_for_endpoint("cashproof.run_page") == "cashproof"
    assert access.section_for_endpoint("billcheck.landing") == "billcheck"
    assert access.section_for_endpoint("admin.users_page") == "admin"
    assert access.section_for_endpoint("domain_page") == "travel"
    assert access.section_for_endpoint("review_page") == "reviews"
    assert access.section_for_endpoint("index") is None
    assert access.section_for_endpoint("logout") is None


@pytest.fixture
def gated_client(store, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "cs")
    monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_DOMAIN", "x.com")
    monkeypatch.setenv("FINANCE_HELPER_SECRET", "t" * 32)
    RUNS.clear()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
    RUNS.clear()


def _login_as(client, email):
    with client.session_transaction() as s:
        s["authed"] = True
        s["email"] = email


def test_section_enforcement_and_nav(gated_client):
    access.save_users({"viewer@x.com": {"sections": ["travel"]},
                       "boss@x.com": {"sections": ["admin"]}})
    _login_as(gated_client, "viewer@x.com")
    assert gated_client.get("/d/hotels").status_code == 200
    # Denied section bounces to the landing page with an explanation.
    resp = gated_client.get("/cashproof/", follow_redirects=True)
    assert b"have access to that section" in resp.data
    # Nav hides what they can't open (check the link targets, not words —
    # the welcome text itself mentions "Admin").
    body = gated_client.get("/").data
    assert b"/cashproof/" not in body and b"/admin/users" not in body
    assert b"Flights" in body

    _login_as(gated_client, "boss@x.com")
    assert gated_client.get("/cashproof/").status_code == 200
    assert gated_client.get("/admin/users").status_code == 200


def test_admin_manages_users(gated_client):
    access.save_users({"boss@x.com": {"sections": ["admin"]}})
    _login_as(gated_client, "boss@x.com")
    resp = gated_client.post("/admin/users/save",
                             data={"email": "new@x.com",
                                   "sections": ["travel", "cashproof"]},
                             follow_redirects=True)
    assert b"Saved access for new@x.com" in resp.data
    assert access.sections_for("new@x.com") == {"travel", "cashproof"}
    # Removing themselves is refused; removing others works.
    gated_client.post("/admin/users/delete", data={"email": "boss@x.com"})
    assert access.is_admin("boss@x.com")
    gated_client.post("/admin/users/delete", data={"email": "new@x.com"})
    assert access.sections_for("new@x.com") is None


def test_preexisting_session_bootstraps_empty_store(gated_client):
    # A session from before the permission system existed: authed, store
    # empty -> first request makes them admin instead of locking them out.
    _login_as(gated_client, "tyson@x.com")
    resp = gated_client.get("/cashproof/")
    assert resp.status_code == 200
    assert access.is_admin("tyson@x.com")
