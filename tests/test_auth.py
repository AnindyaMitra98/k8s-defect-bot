"""Authentication tests: credentials, sessions, throttling, and route protection."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import routes
from app.auth import (
    AuthManager,
    auth,
    hash_api_token,
    hash_password,
    new_api_token,
    verify_password,
)
from app.config import Settings
from app.models import Role, ScanSummary
from app.notify import notifier
from app.store import store as global_store


# -- password and token primitives ----------------------------------------


def test_password_round_trip():
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)


def test_password_hashes_are_salted():
    assert hash_password("same") != hash_password("same")


def test_verify_rejects_junk():
    assert not verify_password("x", None)
    assert not verify_password("x", "")
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "scrypt$bad$fields")
    assert not verify_password("", hash_password("x"))


def test_api_token_is_only_stored_hashed():
    token, digest = new_api_token()
    assert token.startswith("kdb_")
    assert digest == hash_api_token(token)
    assert token not in digest


# -- registry loading ------------------------------------------------------


def users_file(tmp_path, entries) -> Settings:
    path = tmp_path / "users.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return Settings(auth_users_file=str(path), llm_provider="none")


def test_loads_users_from_a_file(tmp_path):
    settings = users_file(tmp_path, [
        {"email": "A@Example.com", "name": "Ann", "role": "admin",
         "password_hash": hash_password("pw")}
    ])
    manager = AuthManager()
    manager.configure(settings)

    assert manager.enabled
    assert "a@example.com" in manager.users, "emails must be normalised to lowercase"
    assert manager.users["a@example.com"].role == Role.ADMIN


def test_loads_users_from_inline_json():
    manager = AuthManager()
    manager.configure(Settings(
        auth_users=json.dumps([{"email": "a@b.com", "password_hash": hash_password("pw")}]),
        auth_users_file=None,
        llm_provider="none",
    ))
    assert "a@b.com" in manager.users


def test_accepts_a_wrapped_users_object(tmp_path):
    settings = users_file(tmp_path, {"users": [
        {"email": "a@b.com", "password_hash": hash_password("pw")}
    ]})
    manager = AuthManager()
    manager.configure(settings)
    assert "a@b.com" in manager.users


def test_no_registry_means_auth_is_off():
    manager = AuthManager()
    manager.configure(Settings(auth_users_file=None, llm_provider="none"))
    assert manager.users == {}
    assert manager.enabled is False


def test_a_broken_registry_locks_the_door_rather_than_removing_it(tmp_path):
    path = tmp_path / "users.json"
    path.write_text("{ not json", encoding="utf-8")
    manager = AuthManager()
    manager.configure(Settings(auth_users_file=str(path), llm_provider="none"))

    assert manager.users == {}
    assert manager.load_error is not None
    assert manager.enabled is True, "a typo must not silently disable authentication"


def test_users_without_credentials_are_skipped(tmp_path):
    settings = users_file(tmp_path, [
        {"email": "nocreds@b.com"},
        {"email": "not-an-email", "password_hash": hash_password("pw")},
        {"email": "ok@b.com", "password_hash": hash_password("pw")},
    ])
    manager = AuthManager()
    manager.configure(settings)
    assert list(manager.users) == ["ok@b.com"]


def test_auth_enabled_flag_overrides_the_registry(tmp_path):
    settings = users_file(tmp_path, [{"email": "a@b.com", "password_hash": hash_password("pw")}])
    manager = AuthManager()
    manager.configure(settings.model_copy(update={"auth_enabled": False}))
    assert manager.enabled is False


# -- authentication and sessions ------------------------------------------


@pytest.fixture
def manager(tmp_path) -> AuthManager:
    settings = users_file(tmp_path, [
        {"email": "admin@b.com", "role": "admin", "password_hash": hash_password("adminpw")},
        {"email": "viewer@b.com", "role": "viewer", "password_hash": hash_password("viewpw")},
        {"email": "gone@b.com", "role": "viewer", "password_hash": hash_password("pw"),
         "disabled": True},
    ])
    m = AuthManager()
    m.configure(settings)
    return m


def test_authenticate_accepts_and_rejects(manager):
    assert manager.authenticate("admin@b.com", "adminpw").role == Role.ADMIN
    assert manager.authenticate("admin@b.com", "nope") is None
    assert manager.authenticate("nobody@b.com", "adminpw") is None
    assert manager.authenticate("gone@b.com", "pw") is None, "disabled users cannot sign in"


def test_authenticate_is_case_insensitive_on_email(manager):
    assert manager.authenticate("ADMIN@B.com", "adminpw") is not None


def test_lockout_after_repeated_failures(manager):
    for _ in range(5):
        assert manager.authenticate("admin@b.com", "wrong", ip="1.2.3.4") is None
    remaining = manager.locked_out("admin@b.com", "1.2.3.4")
    assert remaining and remaining > 0


def test_lockout_is_scoped_to_the_source_address(manager):
    for _ in range(5):
        manager.authenticate("admin@b.com", "wrong", ip="1.2.3.4")
    assert manager.locked_out("admin@b.com", "5.6.7.8") is None


def test_successful_login_clears_the_failure_count(manager):
    for _ in range(3):
        manager.authenticate("admin@b.com", "wrong", ip="1.2.3.4")
    assert manager.authenticate("admin@b.com", "adminpw", ip="1.2.3.4") is not None
    for _ in range(4):
        manager.authenticate("admin@b.com", "wrong", ip="1.2.3.4")
    assert manager.locked_out("admin@b.com", "1.2.3.4") is None


def test_session_round_trip(manager):
    user = manager.users["viewer@b.com"]
    token = manager.create_session(user)
    assert manager.session_user(token).email == "viewer@b.com"

    manager.destroy_session(token)
    assert manager.session_user(token) is None
    assert manager.session_user(None) is None
    assert manager.session_user("made-up") is None


def test_session_expires_on_absolute_ttl(manager):
    manager.settings = manager.settings.model_copy(update={"session_ttl_seconds": 60})
    token = manager.create_session(manager.users["viewer@b.com"])
    manager._sessions[token].created_at -= timedelta(seconds=120)
    assert manager.session_user(token) is None


def test_session_expires_when_idle(manager):
    manager.settings = manager.settings.model_copy(update={"session_idle_timeout_seconds": 60})
    token = manager.create_session(manager.users["viewer@b.com"])
    manager._sessions[token].last_seen_at -= timedelta(seconds=120)
    assert manager.session_user(token) is None


def test_activity_refreshes_the_idle_window(manager):
    manager.settings = manager.settings.model_copy(update={"session_idle_timeout_seconds": 60})
    token = manager.create_session(manager.users["viewer@b.com"])
    manager._sessions[token].last_seen_at -= timedelta(seconds=30)
    assert manager.session_user(token) is not None       # touch refreshes it
    manager._sessions[token].last_seen_at -= timedelta(seconds=30)
    assert manager.session_user(token) is not None


def test_disabling_a_user_kills_their_live_session(manager):
    token = manager.create_session(manager.users["viewer@b.com"])
    manager.users["viewer@b.com"].disabled = True
    assert manager.session_user(token) is None


def test_purge_expired_drops_dead_sessions(manager):
    manager.settings = manager.settings.model_copy(update={"session_ttl_seconds": 60})
    token = manager.create_session(manager.users["viewer@b.com"])
    manager._sessions[token].created_at -= timedelta(seconds=120)
    assert manager.purge_expired() == 1
    assert manager._sessions == {}


def test_api_token_lookup(tmp_path):
    token, digest = new_api_token()
    settings = users_file(tmp_path, [
        {"email": "bot@b.com", "role": "viewer", "api_token_hash": digest}
    ])
    manager = AuthManager()
    manager.configure(settings)

    assert manager.user_for_api_token(token).email == "bot@b.com"
    assert manager.user_for_api_token("kdb_wrong") is None
    assert manager.user_for_api_token(None) is None


def test_recipients_excludes_disabled_and_opted_out(tmp_path):
    settings = users_file(tmp_path, [
        {"email": "a@b.com", "password_hash": hash_password("p")},
        {"email": "b@b.com", "password_hash": hash_password("p"), "notify": {"mode": "off"}},
        {"email": "c@b.com", "password_hash": hash_password("p"), "disabled": True},
    ])
    manager = AuthManager()
    manager.configure(settings)
    assert [u.email for u in manager.recipients()] == ["a@b.com"]


# -- route protection ------------------------------------------------------


@pytest.fixture
def secured(tmp_path, monkeypatch):
    """A live app with two real users."""
    token, digest = new_api_token()
    settings = users_file(tmp_path, [
        {"email": "admin@b.com", "name": "Admin", "role": "admin",
         "password_hash": hash_password("adminpw"), "api_token_hash": digest},
        {"email": "viewer@b.com", "name": "Viewer", "role": "viewer",
         "password_hash": hash_password("viewpw")},
    ]).model_copy(update={"agent_token": "agentsecret"})

    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr("app.auth.get_settings", lambda: settings)
    auth.configure(settings)
    notifier.configure(settings)
    global_store.replace([], ScanSummary())

    app = FastAPI()
    app.include_router(routes.public_router)
    app.include_router(routes.router)
    app.state.scan_loop_running = True
    yield TestClient(app, follow_redirects=False), token
    auth.users.clear()
    auth.load_error = None


def test_api_requires_credentials(secured):
    client, _ = secured
    assert client.get("/api/defects").status_code == 401
    assert client.get("/api/summary").status_code == 401


def test_browser_is_redirected_to_login(secured):
    client, _ = secured
    resp = client.get("/", headers={"accept": "text/html"})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_redirect_remembers_the_requested_page(secured):
    client, _ = secured
    resp = client.get("/ui/defects", headers={"accept": "text/html"})
    assert "next=/ui/defects" in resp.headers["location"]


def test_health_probes_stay_public(secured):
    client, _ = secured
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_agent_intake_uses_its_own_token_not_a_session(secured):
    client, _ = secured
    report = {"node": "n1", "findings": [], "checks_run": 3}
    assert client.post("/api/agent/report", json=report).status_code == 401
    assert client.post(
        "/api/agent/report", json=report, headers={"Authorization": "Bearer agentsecret"}
    ).status_code == 200


def test_login_and_session_flow(secured):
    client, _ = secured
    resp = client.post(
        "/login", data={"email": "viewer@b.com", "password": "viewpw", "next": "/"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    cookie = resp.cookies.get("kdb_session")
    assert cookie
    assert client.get("/api/summary").status_code == 200  # cookie jar carries it

    me = client.get("/api/me").json()
    assert me["email"] == "viewer@b.com" and me["role"] == "viewer"

    client.get("/logout")
    assert client.get("/api/summary").status_code == 401


def test_login_rejects_a_bad_password(secured):
    client, _ = secured
    resp = client.post("/login", data={"email": "viewer@b.com", "password": "nope"})
    assert resp.status_code == 401
    assert "Incorrect email or password" in resp.text
    assert "kdb_session" not in resp.cookies


def test_login_error_does_not_reveal_whether_the_account_exists(secured):
    client, _ = secured
    known = client.post("/login", data={"email": "viewer@b.com", "password": "nope"})
    unknown = client.post("/login", data={"email": "ghost@b.com", "password": "nope"})
    assert known.status_code == unknown.status_code
    assert "Incorrect email or password" in known.text
    assert "Incorrect email or password" in unknown.text


def test_login_refuses_an_offsite_redirect(secured):
    client, _ = secured
    resp = client.post(
        "/login",
        data={"email": "viewer@b.com", "password": "viewpw", "next": "https://evil.example.com"},
    )
    assert resp.headers["location"] == "/", "next must never leave this app"


def test_session_cookie_is_httponly_and_samesite(secured):
    client, _ = secured
    resp = client.post("/login", data={"email": "viewer@b.com", "password": "viewpw"})
    cookie_header = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie_header
    assert "samesite=lax" in cookie_header


def test_api_token_authenticates_without_a_session(secured):
    client, token = secured
    resp = client.get("/api/defects", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_viewer_cannot_trigger_privileged_actions(secured):
    client, _ = secured
    client.post("/login", data={"email": "viewer@b.com", "password": "viewpw"})

    assert client.post("/api/scan").status_code == 403
    assert client.get("/api/users").status_code == 403
    assert client.get("/api/notifications").status_code == 403
    assert client.get("/api/summary").status_code == 200  # reading is still fine


def test_admin_can_reach_privileged_endpoints(secured):
    client, _ = secured
    client.post("/login", data={"email": "admin@b.com", "password": "adminpw"})

    assert client.get("/api/users").status_code == 200
    assert client.get("/api/notifications").status_code == 200


def test_user_listing_never_leaks_hashes(secured):
    client, _ = secured
    client.post("/login", data={"email": "admin@b.com", "password": "adminpw"})
    body = client.get("/api/users").text
    assert "scrypt$" not in body
    assert "password_hash" not in body
    assert "api_token_hash" not in body


def test_lockout_is_enforced_by_the_login_route(secured):
    client, _ = secured
    for _ in range(5):
        client.post("/login", data={"email": "viewer@b.com", "password": "wrong"})
    resp = client.post("/login", data={"email": "viewer@b.com", "password": "viewpw"})
    assert resp.status_code == 429
    assert "Too many failed attempts" in resp.text
