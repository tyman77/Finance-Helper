"""Login gate for the web UI (Sign in with Google) — required once the app is
reachable over the internet, since an unauthenticated route can post real
journal entries to Sage. No-login mode (existing local-dev behavior) must
stay unaffected.
"""

import pytest

from finance_helper.web import app as web_app_module
from finance_helper.web.app import RUNS, create_app


@pytest.fixture
def no_login_client(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_ALLOWED_DOMAIN", raising=False)
    monkeypatch.delenv("FINANCE_HELPER_SECRET", raising=False)
    RUNS.clear()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
    RUNS.clear()


@pytest.fixture
def protected_client(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_DOMAIN", "summitintegrated.com")
    monkeypatch.setenv("FINANCE_HELPER_SECRET", "a-real-random-secret-for-tests")
    RUNS.clear()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
    RUNS.clear()


def _login(client, monkeypatch, email, verified=True):
    """Drive the two-step Google redirect and stub the token/userinfo calls
    the callback makes, returning the callback response."""
    resp = client.get("/auth/google?next=/some/path")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        state = sess["oauth_state"]

    class _FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_post(url, data=None, timeout=None, **kw):
        assert url == web_app_module._GOOGLE_TOKEN_URL
        return _FakeResp({"access_token": "fake-access-token"})

    def fake_get(url, headers=None, timeout=None, **kw):
        assert url == web_app_module._GOOGLE_USERINFO_URL
        return _FakeResp({"email": email, "email_verified": verified})

    monkeypatch.setattr(web_app_module.requests, "post", fake_post)
    monkeypatch.setattr(web_app_module.requests, "get", fake_get)
    return client.get(f"/auth/google/callback?code=fake-code&state={state}")


def test_no_google_login_configured_has_no_gate(no_login_client):
    resp = no_login_client.get("/")
    assert resp.status_code == 200
    assert b"Process a vendor statement" in resp.data


def test_google_login_without_secret_refuses_to_start(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_OAUTH_ALLOWED_DOMAIN", "summitintegrated.com")
    monkeypatch.delenv("FINANCE_HELPER_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="FINANCE_HELPER_SECRET"):
        create_app()


def test_google_login_without_allowed_domain_refuses_to_start(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.delenv("GOOGLE_OAUTH_ALLOWED_DOMAIN", raising=False)
    monkeypatch.setenv("FINANCE_HELPER_SECRET", "a-real-random-secret-for-tests")
    with pytest.raises(RuntimeError, match="GOOGLE_OAUTH_ALLOWED_DOMAIN"):
        create_app()


def test_unauthenticated_request_redirects_to_login(protected_client):
    resp = protected_client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")


def test_login_page_has_google_sign_in_link(protected_client):
    resp = protected_client.get("/login")
    assert resp.status_code == 200
    assert b"Sign in with Google" in resp.data
    assert b"/auth/google" in resp.data


def test_state_mismatch_is_rejected(protected_client):
    resp = protected_client.get("/auth/google/callback?code=x&state=wrong")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")

    resp2 = protected_client.get("/")
    assert resp2.status_code == 302  # still logged out


def test_wrong_domain_is_rejected(protected_client, monkeypatch):
    _login(protected_client, monkeypatch, "someone@gmail.com")

    resp = protected_client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")


def test_unverified_email_is_rejected(protected_client, monkeypatch):
    _login(protected_client, monkeypatch, "person@summitintegrated.com", verified=False)

    resp = protected_client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")


def test_correct_domain_logs_in_and_redirects_to_next(protected_client, monkeypatch):
    resp = _login(protected_client, monkeypatch, "tyson@summitintegrated.com")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/some/path"

    resp2 = protected_client.get("/")
    assert resp2.status_code == 200
    assert b"Process a vendor statement" in resp2.data
    assert b"tyson@summitintegrated.com" in resp2.data


def test_logout_clears_session(protected_client, monkeypatch):
    _login(protected_client, monkeypatch, "tyson@summitintegrated.com")
    assert protected_client.get("/").status_code == 200

    resp = protected_client.get("/logout")
    assert resp.status_code == 302

    resp2 = protected_client.get("/")
    assert resp2.status_code == 302
    assert resp2.headers["Location"].startswith("/login")


def test_static_assets_are_not_gated(protected_client):
    resp = protected_client.get("/static/style.css")
    assert resp.status_code == 200
