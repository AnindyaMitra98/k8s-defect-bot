"""Data models shared across the scraper, node agent, analyzer, and API layers."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"


class Source(str, Enum):
    CLUSTER = "cluster"      # produced by the control-plane scan (scraper/)
    NODE_AGENT = "node-agent"  # produced by a DaemonSet agent (agent/)


def make_defect_id(
    defect_type: str,
    kind: str,
    namespace: Optional[str],
    name: str,
    component: Optional[str] = None,
) -> str:
    """Stable id derived from identity, not content, so it survives across scans.

    `component` disambiguates several defects of the same type on one object --
    without it, two crash-looping containers in one pod collapse into a single id
    and the store (keyed by id) silently drops all but the last.
    """
    raw = f"{defect_type}|{kind}|{namespace or '-'}|{name}|{component or '-'}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class Defect(BaseModel):
    id: str
    type: str
    severity: Severity
    kind: str
    namespace: Optional[str] = None
    name: str
    message: str
    source: Source = Source.CLUSTER
    node: Optional[str] = None  # set for node-agent findings
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Raw context used by the solution engine to fill in templates
    # (e.g. container, image, node, reason, exit_code, restart_count)
    details: dict[str, Any] = Field(default_factory=dict)
    events: list[str] = Field(default_factory=list)
    logs: Optional[str] = None

    # Filled in by analyzer.solution_engine
    root_cause: Optional[str] = None
    remediation: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    llm_enriched: bool = False

    @classmethod
    def create(
        cls,
        *,
        type: str,
        severity: Severity,
        kind: str,
        name: str,
        message: str,
        namespace: Optional[str] = None,
        component: Optional[str] = None,
        source: Source = Source.CLUSTER,
        node: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
        events: Optional[list[str]] = None,
        logs: Optional[str] = None,
    ) -> "Defect":
        return cls(
            id=make_defect_id(type, kind, namespace, name, component),
            type=type,
            severity=severity,
            kind=kind,
            namespace=namespace,
            name=name,
            message=message,
            source=source,
            node=node,
            details=details or {},
            events=events or [],
            logs=logs,
        )


# --------------------------------------------------------------------------
# Node agent wire format (agent/ -> POST /api/agent/report)
# --------------------------------------------------------------------------


class NodeFinding(BaseModel):
    """One failed check from a node agent. Deliberately small and JSON-friendly."""

    check: str
    severity: Severity
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class NodeReport(BaseModel):
    node: str
    agent_version: str = "0.3.0"
    reported_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    checks_run: int = 0
    findings: list[NodeFinding] = Field(default_factory=list)


class NodeAgentStatus(BaseModel):
    """What the dashboard shows for each node agent."""

    node: str
    agent_version: str
    last_report_at: datetime
    age_seconds: float
    stale: bool
    checks_run: int
    finding_count: int
    critical_count: int
    clock_skew_seconds: float = 0.0


# --------------------------------------------------------------------------
# People: who may look at the dashboard, and where their mail goes
# --------------------------------------------------------------------------


class Role(str, Enum):
    ADMIN = "admin"    # everything a viewer can do, plus triggering scans and test mail
    VIEWER = "viewer"  # read-only


class NotifyMode(str, Enum):
    OFF = "off"
    IMMEDIATE = "immediate"  # mail as soon as a scan turns up something new
    DIGEST = "digest"        # batch changes and mail them on an interval


class NotifyPrefs(BaseModel):
    mode: NotifyMode = NotifyMode.IMMEDIATE
    # Only mail about defects at least this severe.
    min_severity: Severity = Severity.CRITICAL
    # Defect types to never mail about (e.g. "missing_resource_limits").
    muted_types: list[str] = Field(default_factory=list)
    # Namespaces to mail about; empty means all. Node findings have no namespace
    # and are governed by include_node_findings instead.
    namespaces: list[str] = Field(default_factory=list)
    include_resolved: bool = True
    include_node_findings: bool = True


class User(BaseModel):
    """A person who may sign in. Loaded from a Secret; never created at runtime."""

    email: str
    name: str = ""
    role: Role = Role.VIEWER
    # scrypt$n$r$p$salt$hash -- produced by `python -m app.auth hash-password`.
    password_hash: Optional[str] = None
    # Bootstrap convenience only: a plaintext password, hashed at load time and
    # then discarded from memory. Anyone who can read the Secret can read this,
    # whereas a hash is useless to them -- prefer password_hash and rotate away
    # from this. Using it logs a warning at startup.
    password: Optional[str] = None
    # sha256 of a bearer token for scripted API access. The plaintext is shown
    # once at creation and never stored.
    api_token_hash: Optional[str] = None
    disabled: bool = False
    notify: NotifyPrefs = Field(default_factory=NotifyPrefs)

    @property
    def display_name(self) -> str:
        return self.name or self.email

    def public(self) -> dict[str, Any]:
        """Safe to serialise to a browser -- no hashes."""
        return {
            "email": self.email,
            "name": self.name,
            "role": self.role.value,
            "disabled": self.disabled,
            "notify": self.notify.model_dump(mode="json"),
        }


class NotificationDelivery(BaseModel):
    """One record of a mail we tried to send, for the dashboard and /api/notifications."""

    to: str
    subject: str
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ok: bool = True
    error: Optional[str] = None
    defect_count: int = 0


class ScanSummary(BaseModel):
    last_scan_at: Optional[datetime] = None
    duration_seconds: float = 0.0
    total_defects: int = 0
    critical_count: int = 0
    warning_count: int = 0
    namespaces_scanned: int = 0
    nodes_scanned: int = 0
    pods_scanned: int = 0
    # Node-agent fleet health
    agents_reporting: int = 0
    agents_stale: int = 0
    node_defects: int = 0
    error: Optional[str] = None
