"""Thread-safe in-memory store for the latest scan results and node-agent reports.

Intentionally not persisted (no DB/PVC) to keep the collector stateless and
lightweight. Because of this, only a single replica should run at a time --
see usage.md.

Two independent sets of findings live here:
  * cluster defects  -- wholesale replaced by each control-plane scan
  * node defects     -- upserted per node whenever that node's agent reports,
                        and expired once the report goes stale
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from app.models import (
    Defect,
    NodeAgentStatus,
    NodeReport,
    ScanSummary,
    Severity,
    Source,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _NodeEntry:
    __slots__ = ("report", "defects", "received_at", "clock_skew_seconds")

    def __init__(self, report: NodeReport, defects: list[Defect], clock_skew_seconds: float):
        self.report = report
        self.defects = defects
        self.received_at = _utcnow()
        self.clock_skew_seconds = clock_skew_seconds


class ScanStore:
    def __init__(self, node_report_ttl_seconds: int = 900) -> None:
        self._lock = threading.Lock()
        self._defects: dict[str, Defect] = {}
        self._summary: ScanSummary = ScanSummary()
        self._nodes: dict[str, _NodeEntry] = {}
        self._ttl = node_report_ttl_seconds

    def configure(self, node_report_ttl_seconds: int) -> None:
        """Called once at startup so the module-level singleton picks up config."""
        with self._lock:
            self._ttl = node_report_ttl_seconds

    # -- cluster scan ------------------------------------------------------

    def replace(self, defects: list[Defect], summary: ScanSummary) -> None:
        with self._lock:
            self._defects = {d.id: d for d in defects}
            self._summary = summary
        self._refresh_summary_counts()

    # -- node agents -------------------------------------------------------

    def record_node_report(
        self, report: NodeReport, defects: list[Defect], clock_skew_seconds: float = 0.0
    ) -> None:
        with self._lock:
            self._nodes[report.node] = _NodeEntry(report, defects, clock_skew_seconds)
        self._refresh_summary_counts()

    def _live_nodes(self) -> dict[str, _NodeEntry]:
        """Caller must hold the lock. Prunes entries older than the TTL."""
        now = _utcnow()
        live = {}
        for node, entry in self._nodes.items():
            if (now - entry.received_at).total_seconds() <= self._ttl:
                live[node] = entry
        return live

    def node_statuses(self, clock_skew_warn_seconds: float = 60.0) -> list[NodeAgentStatus]:
        now = _utcnow()
        with self._lock:
            entries = dict(self._nodes)
        out = []
        for node, entry in entries.items():
            age = (now - entry.received_at).total_seconds()
            out.append(
                NodeAgentStatus(
                    node=node,
                    agent_version=entry.report.agent_version,
                    last_report_at=entry.received_at,
                    age_seconds=round(age, 1),
                    stale=age > self._ttl,
                    checks_run=entry.report.checks_run,
                    finding_count=len(entry.defects),
                    critical_count=sum(
                        1 for d in entry.defects if d.severity == Severity.CRITICAL
                    ),
                    clock_skew_seconds=round(entry.clock_skew_seconds, 1),
                )
            )
        return sorted(out, key=lambda s: s.node)

    # -- reads -------------------------------------------------------------

    def _all_defects(self) -> list[Defect]:
        with self._lock:
            defects = list(self._defects.values())
            for entry in self._live_nodes().values():
                defects.extend(entry.defects)
        return defects

    def list(
        self,
        namespace: Optional[str] = None,
        severity: Optional[str] = None,
        defect_type: Optional[str] = None,
        source: Optional[str] = None,
        node: Optional[str] = None,
    ) -> list[Defect]:
        defects = self._all_defects()
        if namespace:
            defects = [d for d in defects if d.namespace == namespace]
        if severity:
            defects = [d for d in defects if d.severity.value == severity]
        if defect_type:
            defects = [d for d in defects if d.type == defect_type]
        if source:
            defects = [d for d in defects if d.source.value == source]
        if node:
            defects = [d for d in defects if d.node == node or d.name == node]
        return sorted(
            defects,
            key=lambda d: (d.severity != Severity.CRITICAL, d.namespace or "", d.name),
        )

    def get(self, defect_id: str) -> Optional[Defect]:
        with self._lock:
            hit = self._defects.get(defect_id)
            if hit:
                return hit
            for entry in self._live_nodes().values():
                for d in entry.defects:
                    if d.id == defect_id:
                        return d
        return None

    def summary(self) -> ScanSummary:
        with self._lock:
            return self._summary

    def namespaces(self) -> list[str]:
        return sorted({d.namespace for d in self._all_defects() if d.namespace})

    def _refresh_summary_counts(self) -> None:
        """Recompute the totals that span both sources, leaving scan metadata alone."""
        defects = self._all_defects()
        with self._lock:
            live = self._live_nodes()
            summary = self._summary.model_copy(
                update={
                    "total_defects": len(defects),
                    "critical_count": sum(
                        1 for d in defects if d.severity == Severity.CRITICAL
                    ),
                    "warning_count": sum(
                        1 for d in defects if d.severity == Severity.WARNING
                    ),
                    "node_defects": sum(
                        1 for d in defects if d.source == Source.NODE_AGENT
                    ),
                    "agents_reporting": len(live),
                    "agents_stale": len(self._nodes) - len(live),
                }
            )
            self._summary = summary


store = ScanStore()
