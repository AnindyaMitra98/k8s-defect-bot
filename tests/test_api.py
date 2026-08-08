"""API tests, including the node-agent intake and its auth."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import routes
from app.auth import auth
from app.config import Settings
from app.models import Defect, ScanSummary, Severity, Source
from app.notify import notifier
from app.store import store as global_store


def build_app(settings: Settings, monkeypatch) -> TestClient:
    """Mounts the real routers against injected settings, with no user registry."""
    from fastapi import FastAPI

    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr("app.auth.get_settings", lambda: settings)
    auth.configure(settings)
    notifier.configure(settings)
    app = FastAPI()
    app.include_router(routes.public_router)
    app.include_router(routes.router)
    app.state.scan_loop_running = True
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_state():
    global_store.replace([], ScanSummary())
    global_store._nodes.clear()
    global_store.configure(900)
    yield
    global_store.replace([], ScanSummary())
    global_store._nodes.clear()
    auth.users.clear()
    auth.load_error = None


@pytest.fixture
def client(monkeypatch):
    return build_app(
        Settings(llm_provider="none", agent_token=None, auth_users_file=None), monkeypatch
    )


@pytest.fixture
def authed_client(monkeypatch):
    return build_app(
        Settings(llm_provider="none", agent_token="s3cret", auth_users_file=None), monkeypatch
    )


def sample_report(node="ip-10-0-1-5", reported_at=None) -> dict:
    return {
        "node": node,
        "agent_version": "0.2.0",
        "reported_at": (reported_at or datetime.now(timezone.utc)).isoformat(),
        "checks_run": 10,
        "findings": [
            {
                "check": "node_disk_usage",
                "severity": "critical",
                "message": "Filesystem / is 93% full",
                "details": {"path": "/", "used_percent": 93.0, "free_gb": 4.2},
            }
        ],
    }


def test_healthz_and_readyz(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").status_code == 200


def test_readyz_is_503_before_the_first_scan(client):
    client.app.state.scan_loop_running = False
    assert client.get("/readyz").status_code == 503


def test_agent_report_creates_a_solved_defect(client):
    resp = client.post("/api/agent/report", json=sample_report())
    assert resp.status_code == 200
    assert resp.json()["findings_recorded"] == 1

    defects = client.get("/api/defects?source=node-agent").json()
    assert len(defects) == 1
    assert defects[0]["type"] == "node_disk_usage"
    assert defects[0]["node"] == "ip-10-0-1-5"
    # The intake must run the solution engine, not just store a raw message.
    assert defects[0]["root_cause"]
    assert defects[0]["remediation"]
    assert defects[0]["commands"]


def test_agent_report_flags_clock_skew(client):
    stale_clock = datetime.now(timezone.utc) - timedelta(seconds=400)
    resp = client.post("/api/agent/report", json=sample_report(reported_at=stale_clock))
    assert resp.json()["findings_recorded"] == 2

    skew = [d for d in client.get("/api/defects").json() if d["type"] == "node_clock_skew"]
    assert len(skew) == 1
    assert skew[0]["severity"] == "critical"


def test_agent_report_ignores_small_skew(client):
    resp = client.post("/api/agent/report", json=sample_report())
    assert resp.json()["findings_recorded"] == 1


def test_agent_report_rejects_a_malformed_body(client):
    assert client.post("/api/agent/report", json={"findings": []}).status_code == 422


def test_agent_auth_required_when_a_token_is_configured(authed_client):
    assert authed_client.post("/api/agent/report", json=sample_report()).status_code == 401
    assert authed_client.post(
        "/api/agent/report", json=sample_report(), headers={"Authorization": "Bearer wrong"}
    ).status_code == 403
    assert authed_client.post(
        "/api/agent/report", json=sample_report(), headers={"Authorization": "Bearer s3cret"}
    ).status_code == 200


def test_nodes_endpoint_lists_agents(client):
    client.post("/api/agent/report", json=sample_report("n1"))
    client.post("/api/agent/report", json=sample_report("n2"))

    nodes = client.get("/api/nodes").json()
    assert [n["node"] for n in nodes] == ["n1", "n2"]
    assert nodes[0]["checks_run"] == 10
    assert nodes[0]["stale"] is False


def test_defect_detail_and_404(client):
    client.post("/api/agent/report", json=sample_report())
    defect_id = client.get("/api/defects").json()[0]["id"]

    assert client.get(f"/api/defects/{defect_id}").status_code == 200
    assert client.get("/api/defects/deadbeef").status_code == 404


def test_summary_reflects_node_reports(client):
    client.post("/api/agent/report", json=sample_report())
    summary = client.get("/api/summary").json()
    assert summary["total_defects"] == 1
    assert summary["critical_count"] == 1
    assert summary["node_defects"] == 1
    assert summary["agents_reporting"] == 1


def test_filters_on_the_defects_endpoint(client):
    global_store.replace(
        [
            Defect.create(
                type="crashloopbackoff", severity=Severity.CRITICAL, kind="Pod",
                namespace="prod", name="web-1", component="app", message="looping",
            )
        ],
        ScanSummary(),
    )
    client.post("/api/agent/report", json=sample_report())

    assert len(client.get("/api/defects").json()) == 2
    assert len(client.get("/api/defects?namespace=prod").json()) == 1
    assert len(client.get("/api/defects?source=cluster").json()) == 1
    assert len(client.get("/api/defects?type=node_disk_usage").json()) == 1
    assert len(client.get("/api/defects?severity=warning").json()) == 0


def test_dashboard_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "k8s-defect-bot" in resp.text


def test_ui_partials_render(client):
    client.post("/api/agent/report", json=sample_report())

    assert client.get("/ui/summary").status_code == 200
    assert "node_disk_usage" in client.get("/ui/defects").text
    nodes_html = client.get("/ui/nodes").text
    assert "ip-10-0-1-5" in nodes_html
    assert client.get("/ui/defects?source=node-agent").status_code == 200


def test_ui_defect_detail_404_renders_a_message(client):
    resp = client.get("/ui/defects/nope")
    assert resp.status_code == 404
    assert "no longer present" in resp.text
