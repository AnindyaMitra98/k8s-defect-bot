"""Scanner tests: failure reporting and namespace-filter accounting."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from app.config import Settings
from scraper.cluster_scanner import ClusterScanner, ClusterSnapshot


@pytest.fixture
def scanner(monkeypatch):
    """A scanner with the Kubernetes client swapped out entirely."""
    monkeypatch.setattr(
        "scraper.cluster_scanner.get_api_clients",
        lambda: SimpleNamespace(core=None, apps=None, networking=None, extensions=None),
    )
    return ClusterScanner(Settings(llm_provider="none"))


def ok(items):
    def _call():
        return SimpleNamespace(items=items)

    _call.__name__ = "list_thing"
    return _call


def fails(reason="Unauthorized"):
    def _call():
        raise ApiException(status=401, reason=reason)

    _call.__name__ = "list_thing"
    return _call


def test_safe_returns_empty_and_records_the_failure(scanner):
    assert scanner._safe(fails()) == []
    assert scanner._api_calls == 1
    assert "Unauthorized" in scanner._api_failures[0]


def test_safe_passes_through_success(scanner):
    assert scanner._safe(ok([1, 2, 3])) == [1, 2, 3]
    assert scanner._api_failures == []


def test_no_error_when_everything_succeeds(scanner):
    scanner._safe(ok([1]))
    snap = ClusterSnapshot(namespaces=["default"], nodes=[object()])
    assert scanner._scan_error(snap) is None


def test_total_failure_is_reported_rather_than_looking_healthy(scanner):
    """Regression: an unreachable cluster used to render as a clean all-clear."""
    for _ in range(4):
        scanner._safe(fails("Name resolution failed"))

    error = scanner._scan_error(ClusterSnapshot())
    assert error is not None
    assert "unreachable" in error
    assert "not because the cluster is healthy" in error


def test_partial_failure_is_reported_as_incomplete(scanner):
    scanner._safe(ok([1]))
    scanner._safe(fails("Forbidden"))

    error = scanner._scan_error(ClusterSnapshot(namespaces=["default"], nodes=[object()]))
    assert error is not None
    assert "incomplete" in error
    assert "1 of 2" in error


def test_namespaces_scanned_respects_the_filter(scanner, monkeypatch):
    """The summary must report coverage, not everything that exists."""
    all_ns = [SimpleNamespace(metadata=SimpleNamespace(name=n))
              for n in ("default", "prod", "kube-system")]
    scanner.settings = Settings(llm_provider="none", namespace_filter="prod")
    monkeypatch.setattr(scanner, "_safe", lambda fn, *a, **k: all_ns if not a else [])
    monkeypatch.setattr(scanner, "_list_all", lambda *a, **k: [])
    monkeypatch.setattr(scanner, "_list_deprecated_ingresses", lambda: [])
    scanner.clients = SimpleNamespace(
        core=SimpleNamespace(
            list_namespace=None, list_pod_for_all_namespaces=None, list_namespaced_pod=None,
            list_node=None, list_event_for_all_namespaces=None, list_namespaced_event=None,
            list_service_for_all_namespaces=None, list_namespaced_service=None,
            list_endpoints_for_all_namespaces=None, list_namespaced_endpoints=None,
            list_persistent_volume_claim_for_all_namespaces=None,
            list_namespaced_persistent_volume_claim=None, list_persistent_volume=None,
        ),
        apps=SimpleNamespace(**{k: None for k in (
            "list_deployment_for_all_namespaces", "list_namespaced_deployment",
            "list_replica_set_for_all_namespaces", "list_namespaced_replica_set",
            "list_stateful_set_for_all_namespaces", "list_namespaced_stateful_set",
            "list_daemon_set_for_all_namespaces", "list_namespaced_daemon_set")}),
        networking=SimpleNamespace(
            list_ingress_for_all_namespaces=None, list_namespaced_ingress=None
        ),
        extensions=None,
    )

    snap = scanner.gather()
    assert snap.namespaces == ["prod"]
