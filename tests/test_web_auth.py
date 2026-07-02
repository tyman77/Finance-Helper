"""Login gate for the web UI — required once the app is reachable over the
internet, since an unauthenticated route can post real journal entries to
Sage. No-password mode (existing local-dev behavior) must stay unaffected.
"""

import pytest

from finance_helper.web.app import RUNS, create_app


@pytest.fixture
def no_password_client(monkeypatch):
    monkeypatch.delenv("FINANCE_HELPER_WEB_PASSWORD", raising=False)
    monkeypatch.delenv("FINANCE_HELPER_SECRET", raising=False)
    RUNS.clear()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
    RUNS.clear()


@pytest.fixture
def protected_client(monkeypatch):
    monkeypatch.setenv("FINANCE_HELPER_WEB_PASSWORD", "hunter2")
    monkeypatch.setenv("FINANCE_HELPER_SECRET", "a-real-random-secret-for-tests")
    RUNS.clear()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
    RUNS.clear()


def test_no_password_configured_has_no_gate(no_password_client):
    resp = no_password_client.get("/")
    assert resp.status_code == 200
    assert b"Process a vendor statement" in resp.data


def test_password_without_secret_refuses_to_start(monkeypatch):
    monkeypatch.setenv("FINANCE_HELPER_WEB_PASSWORD", "hunter2")
    monkeypatch.delenv("FINANCE_HELPER_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="FINANCE_HELPER_SECRET"):
        create_app()


def test_unauthenticated_request_redirects_to_login(protected_client):
    resp = protected_client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")


def test_login_page_itself_is_reachable(protected_client):
    resp = protected_client.get("/login")
    assert resp.status_code == 200
    assert b"Log in" in resp.data


def test_wrong_password_shows_error_and_stays_logged_out(protected_client):
    resp = protected_client.post("/login", data={"password": "nope"})
    assert resp.status_code == 401
    assert b"Wrong password" in resp.data

    resp2 = protected_client.get("/")
    assert resp2.status_code == 302
    assert resp2.headers["Location"].startswith("/login")


def test_correct_password_logs_in_and_unlocks_the_app(protected_client):
    resp = protected_client.post("/login", data={"password": "hunter2"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"

    resp2 = protected_client.get("/")
    assert resp2.status_code == 200
    assert b"Process a vendor statement" in resp2.data


def test_correct_password_redirects_to_originally_requested_page(protected_client):
    protected_client.get("/review/doesnotexist")  # bounced to /login?next=...
    resp = protected_client.post(
        "/login?next=/review/doesnotexist", data={"password": "hunter2"}
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/review/doesnotexist"


def test_logout_clears_session(protected_client):
    protected_client.post("/login", data={"password": "hunter2"})
    assert protected_client.get("/").status_code == 200

    resp = protected_client.get("/logout")
    assert resp.status_code == 302

    resp2 = protected_client.get("/")
    assert resp2.status_code == 302
    assert resp2.headers["Location"].startswith("/login")


def test_static_assets_are_not_gated(protected_client):
    resp = protected_client.get("/static/style.css")
    assert resp.status_code == 200
