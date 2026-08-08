"""Gathers a point-in-time snapshot of cluster state and runs it through the rule engine."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

from kubernetes.client.rest import ApiException

from app.config import Settings, get_settings
from app.models import Defect, ScanSummary, Severity
from scraper.k8s_client import ApiClients, get_api_clients, get_pod_log_tail

logger = logging.getLogger("k8s-defect-bot.scanner")


@dataclass
class ClusterSnapshot:
    pods: list = field(default_factory=list)
    nodes: list = field(default_factory=list)
    events: list = field(default_factory=list)
    services: list = field(default_factory=list)
    endpoints: list = field(default_factory=list)
    deployments: list = field(default_factory=list)
    replicasets: list = field(default_factory=list)
    statefulsets: list = field(default_factory=list)
    daemonsets: list = field(default_factory=list)
    pvcs: list = field(default_factory=list)
    pvs: list = field(default_factory=list)
    ingresses: list = field(default_factory=list)
    deprecated_ingresses: list = field(default_factory=list)
    namespaces: list = field(default_factory=list)

    def events_for(self, kind: str, namespace: Optional[str], name: str) -> list:
        out = []
        for e in self.events:
            obj = e.involved_object
            if obj is None:
                continue
            if obj.kind == kind and obj.name == name and (obj.namespace or None) == (namespace or None):
                out.append(e)
        return out


class ClusterScanner:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.clients: ApiClients = get_api_clients()
        # Reset per gather(); see _scan_error(). Without this, an unreachable
        # cluster produces an empty result set that is indistinguishable from a
        # healthy one -- the dashboard would show a confident all-clear.
        self._api_failures: list[str] = []
        self._api_calls = 0

    def _ns_filter(self) -> Optional[list[str]]:
        return self.settings.namespace_filter_list

    def _safe(self, fn, *args, **kwargs) -> list:
        name = getattr(fn, "__name__", str(fn))
        self._api_calls += 1
        try:
            return fn(*args, **kwargs).items
        except ApiException as exc:
            logger.warning("%s failed: %s", name, exc.reason)
            self._api_failures.append(f"{name}: {exc.reason}")
            return []
        except Exception as exc:
            logger.exception("%s failed unexpectedly", name)
            self._api_failures.append(f"{name}: {type(exc).__name__}")
            return []

    def _scan_error(self, snap: "ClusterSnapshot") -> Optional[str]:
        """Turns swallowed per-call API failures into one honest summary message."""
        if not self._api_failures:
            return None
        first = self._api_failures[0]
        if not snap.namespaces and not snap.nodes:
            return (
                f"Cluster appears unreachable: all {self._api_calls} API calls failed "
                f"({first}). Results below are empty because nothing could be read, "
                f"not because the cluster is healthy."
            )
        return (
            f"{len(self._api_failures)} of {self._api_calls} API calls failed "
            f"({first}). Results are incomplete."
        )

    def _list_all(self, list_all_fn, list_ns_fn) -> list:
        ns_filter = self._ns_filter()
        if not ns_filter:
            return self._safe(list_all_fn)
        items = []
        for ns in ns_filter:
            items.extend(self._safe(list_ns_fn, ns))
        return items

    def _list_deprecated_ingresses(self) -> list:
        ext = self.clients.extensions
        if ext is None:
            return []
        ns_filter = self._ns_filter()
        if not ns_filter:
            return self._safe(ext.list_ingress_for_all_namespaces)
        items = []
        for ns in ns_filter:
            items.extend(self._safe(ext.list_namespaced_ingress, ns))
        return items

    def gather(self) -> ClusterSnapshot:
        self._api_failures = []
        self._api_calls = 0
        snap = ClusterSnapshot()
        core, apps, networking = self.clients.core, self.clients.apps, self.clients.networking

        all_namespaces = [n.metadata.name for n in self._safe(core.list_namespace)]
        ns_filter = self._ns_filter()
        # Report what was actually scanned, not what exists -- otherwise the
        # summary claims coverage the namespace filter excluded.
        snap.namespaces = (
            [n for n in all_namespaces if n in set(ns_filter)] if ns_filter else all_namespaces
        )
        snap.pods = self._list_all(core.list_pod_for_all_namespaces, core.list_namespaced_pod)
        snap.nodes = self._safe(core.list_node)
        snap.events = self._list_all(core.list_event_for_all_namespaces, core.list_namespaced_event)
        snap.services = self._list_all(core.list_service_for_all_namespaces, core.list_namespaced_service)
        snap.endpoints = self._list_all(core.list_endpoints_for_all_namespaces, core.list_namespaced_endpoints)
        snap.deployments = self._list_all(apps.list_deployment_for_all_namespaces, apps.list_namespaced_deployment)
        snap.replicasets = self._list_all(
            apps.list_replica_set_for_all_namespaces, apps.list_namespaced_replica_set
        )
        snap.statefulsets = self._list_all(
            apps.list_stateful_set_for_all_namespaces, apps.list_namespaced_stateful_set
        )
        snap.daemonsets = self._list_all(apps.list_daemon_set_for_all_namespaces, apps.list_namespaced_daemon_set)
        snap.pvcs = self._list_all(
            core.list_persistent_volume_claim_for_all_namespaces,
            core.list_namespaced_persistent_volume_claim,
        )
        snap.pvs = self._safe(core.list_persistent_volume)
        snap.ingresses = self._list_all(networking.list_ingress_for_all_namespaces, networking.list_namespaced_ingress)
        snap.deprecated_ingresses = self._list_deprecated_ingresses()

        return snap

    def _missing_agent_defects(self, snap: ClusterSnapshot) -> list[Defect]:
        """Flags nodes the DaemonSet agent isn't reporting from.

        Only fires once at least one agent has checked in -- otherwise a cluster
        that simply hasn't deployed the DaemonSet would show one defect per node.
        """
        from analyzer.solution_engine import generate_solution
        from app.models import Severity, Source
        from app.store import store

        statuses = store.node_statuses()
        if not statuses:
            return []
        reporting = {s.node for s in statuses if not s.stale}
        defects = []
        for node in snap.nodes:
            name = node.metadata.name
            if name in reporting:
                continue
            defect = Defect.create(
                type="node_agent_unreachable",
                severity=Severity.WARNING,
                kind="Node",
                name=name,
                component="agent",
                source=Source.CLUSTER,
                node=name,
                message="No node agent reporting from this node",
                details={"known_agents": len(statuses)},
            )
            generate_solution(defect, self.settings)
            defects.append(defect)
        return defects

    def fetch_log_tail(self, namespace: str, pod: str, container: Optional[str]) -> Optional[str]:
        return get_pod_log_tail(self.clients.core, namespace, pod, container, self.settings.log_tail_lines)

    def scan(self) -> tuple[list[Defect], ScanSummary]:
        # Imported here to avoid a module import cycle (analyzer -> app.config -> ... -> scraper).
        from analyzer.solution_engine import generate_solution, enrich_batch

        from scraper.rules import run_all_rules

        start = time.monotonic()
        error: Optional[str] = None
        defects: list[Defect] = []
        snap = ClusterSnapshot()
        try:
            snap = self.gather()
            error = self._scan_error(snap)
            defects = run_all_rules(snap, self.settings)
            for defect in defects:
                if defect.kind == "Pod" and defect.namespace and defect.details.get("container"):
                    defect.logs = self.fetch_log_tail(
                        defect.namespace, defect.name, defect.details.get("container")
                    )
                generate_solution(defect, self.settings)
            defects.extend(self._missing_agent_defects(snap))
            # LLM enrichment runs after every defect has its heuristic answer, so a
            # slow or failing model never delays or degrades the base result.
            enrich_batch(defects, self.settings)
        except Exception as exc:  # a bad scan pass shouldn't crash the background loop
            logger.exception("scan failed")
            error = str(exc)

        duration = time.monotonic() - start
        summary = ScanSummary(
            last_scan_at=datetime.now(timezone.utc),
            duration_seconds=round(duration, 2),
            total_defects=len(defects),
            critical_count=sum(1 for d in defects if d.severity == Severity.CRITICAL),
            warning_count=sum(1 for d in defects if d.severity == Severity.WARNING),
            namespaces_scanned=len(snap.namespaces),
            nodes_scanned=len(snap.nodes),
            pods_scanned=len(snap.pods),
            error=error,
        )
        return defects, summary


@lru_cache
def get_scanner() -> ClusterScanner:
    return ClusterScanner()


# Serializes the background loop against operator-triggered scans from the API,
# so a "Scan now" click during a scheduled pass doesn't double-hammer the API server.
_scan_lock = asyncio.Lock()


async def perform_scan() -> ScanSummary:
    """Runs a scan off the event loop (the kubernetes client is blocking) and updates the shared store."""
    from app.notify import notifier
    from app.store import store

    async with _scan_lock:
        scanner = get_scanner()
        defects, summary = await asyncio.to_thread(scanner.scan)
        store.replace(defects, summary)

        # Notifications compare this view against the previous one. Run it off the
        # event loop (SMTP is blocking) and never let a mail problem fail a scan.
        try:
            reporting = {s.node for s in store.node_statuses() if not s.stale}
            await asyncio.to_thread(
                notifier.process, store.list(), store.summary(), reporting
            )
        except Exception:
            logger.exception("notification pass failed after a scan")

        return summary
