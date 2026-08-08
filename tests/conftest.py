"""Shared fixtures.

The Kubernetes objects the rules consume are plain attribute bags, so the fakes
here are namespaces rather than mocks -- a rule that reaches for an attribute the
real object doesn't have fails loudly instead of silently returning a Mock.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.store import ScanStore  # noqa: E402


def ago(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def ns(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def container_status(
    name="app",
    image="nginx:1.25",
    restart_count=0,
    waiting_reason=None,
    terminated_reason=None,
    last_terminated_reason=None,
    exit_code=1,
) -> SimpleNamespace:
    waiting = ns(reason=waiting_reason, message="pull failed") if waiting_reason else None
    terminated = (
        ns(reason=terminated_reason, exit_code=exit_code) if terminated_reason else None
    )
    last_terminated = (
        ns(reason=last_terminated_reason, exit_code=exit_code)
        if last_terminated_reason
        else None
    )
    return ns(
        name=name,
        image=image,
        restart_count=restart_count,
        state=ns(waiting=waiting, terminated=terminated, running=None),
        last_state=ns(terminated=last_terminated, waiting=None, running=None),
    )


def pod(
    name="web-1",
    namespace="default",
    phase="Running",
    container_statuses=None,
    init_container_statuses=None,
    containers=None,
    created_minutes_ago=60,
) -> SimpleNamespace:
    return ns(
        metadata=ns(name=name, namespace=namespace, creation_timestamp=ago(created_minutes_ago)),
        spec=ns(containers=containers if containers is not None else [ns(
            name="app",
            resources=ns(
                limits={"cpu": "500m", "memory": "256Mi"},
                requests={"cpu": "100m", "memory": "128Mi"},
            ),
        )]),
        status=ns(
            phase=phase,
            container_statuses=container_statuses,
            init_container_statuses=init_container_statuses,
        ),
    )


def node(name="ip-10-0-1-5", ready="True", conditions=None) -> SimpleNamespace:
    conds = conditions or [ns(type="Ready", status=ready, reason=None, message=None)]
    return ns(metadata=ns(name=name), status=ns(conditions=conds))


def event(kind="Pod", name="web-1", namespace="default", reason="Unhealthy",
          etype="Warning", message="probe failed", count=3) -> SimpleNamespace:
    return ns(
        involved_object=ns(kind=kind, name=name, namespace=namespace),
        reason=reason,
        type=etype,
        message=message,
        count=count,
        last_timestamp=ago(1),
        event_time=None,
    )


class FakeSnapshot:
    """Mirrors ClusterSnapshot's surface without needing the kubernetes client."""

    def __init__(self, **kwargs):
        for field in (
            "pods", "nodes", "events", "services", "endpoints", "deployments",
            "replicasets", "statefulsets", "daemonsets", "pvcs", "pvs",
            "ingresses", "deprecated_ingresses", "namespaces",
        ):
            setattr(self, field, kwargs.get(field, []))

    def events_for(self, kind, namespace, name):
        return [
            e for e in self.events
            if e.involved_object
            and e.involved_object.kind == kind
            and e.involved_object.name == name
            and (e.involved_object.namespace or None) == (namespace or None)
        ]


@pytest.fixture
def settings() -> Settings:
    return Settings(llm_provider="none", agent_token=None)


@pytest.fixture
def store() -> ScanStore:
    return ScanStore(node_report_ttl_seconds=900)
