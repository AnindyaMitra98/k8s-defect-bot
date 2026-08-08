"""Heuristic defect-detection rules.

Each rule takes a ClusterSnapshot and returns a list of Defect objects. Rules are
independently toggleable via ENABLED_RULES in the ConfigMap/Helm values.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from app.config import Settings
from app.models import Defect, Severity

logger = logging.getLogger("k8s-defect-bot.rules")

RESTART_COUNT_WARNING_THRESHOLD = 5
RESTART_COUNT_CRITICAL_THRESHOLD = 20
PENDING_WARNING_MINUTES = 5
PENDING_CRITICAL_MINUTES = 15
PVC_PENDING_WARNING_MINUTES = 5

# Event reasons already surfaced by a more specific rule -- excluded from the
# generic "warning_events" catch-all to avoid duplicate defects for the same issue.
_HANDLED_EVENT_REASONS = {
    "BackOff",
    "Failed",
    "FailedScheduling",
    "Unhealthy",
    "OOMKilling",
    "FailedMount",
    "FailedAttachVolume",
}


def _age_minutes(timestamp) -> float:
    if timestamp is None:
        return 0.0
    now = datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (now - timestamp).total_seconds() / 60.0


def _events_to_strings(events: list) -> list[str]:
    out = []
    for e in events[:10]:
        reason = getattr(e, "reason", "") or ""
        message = getattr(e, "message", "") or ""
        count = getattr(e, "count", None) or 1
        etype = getattr(e, "type", "") or ""
        out.append(f"[{etype}] {reason}: {message} (x{count})")
    return out


def _pod_events(snapshot, pod) -> list:
    return snapshot.events_for("Pod", pod.metadata.namespace, pod.metadata.name)


def _container_statuses(pod):
    statuses = list(pod.status.container_statuses or [])
    statuses += list(pod.status.init_container_statuses or [])
    return statuses


def rule_crashloopbackoff(snapshot, settings: Settings) -> list[Defect]:
    defects = []
    for pod in snapshot.pods:
        # Guard on pod.status only -- _container_statuses() also covers init
        # containers, and a pod stuck on a crash-looping init container often has
        # no app container statuses yet. Requiring container_statuses here would
        # skip exactly the case worth catching.
        if not pod.status:
            continue
        for cs in _container_statuses(pod):
            waiting = cs.state.waiting if cs.state else None
            if waiting and waiting.reason == "CrashLoopBackOff":
                events = _pod_events(snapshot, pod)
                last_term = cs.last_state.terminated if cs.last_state else None
                defects.append(
                    Defect.create(
                        type="crashloopbackoff",
                        severity=Severity.CRITICAL,
                        kind="Pod",
                        namespace=pod.metadata.namespace,
                        name=pod.metadata.name,
                        component=cs.name,
                        message=f"Container '{cs.name}' is crash-looping ({cs.restart_count} restarts)",
                        details={
                            "container": cs.name,
                            "image": cs.image,
                            "restart_count": cs.restart_count,
                            "last_exit_code": getattr(last_term, "exit_code", None),
                            "last_reason": getattr(last_term, "reason", None),
                        },
                        events=_events_to_strings(events),
                    )
                )
    return defects


def rule_imagepullbackoff(snapshot, settings: Settings) -> list[Defect]:
    defects = []
    for pod in snapshot.pods:
        if not pod.status:
            continue
        for cs in _container_statuses(pod):
            waiting = cs.state.waiting if cs.state else None
            if waiting and waiting.reason in ("ImagePullBackOff", "ErrImagePull"):
                events = _pod_events(snapshot, pod)
                defects.append(
                    Defect.create(
                        type="imagepullbackoff",
                        severity=Severity.CRITICAL,
                        kind="Pod",
                        namespace=pod.metadata.namespace,
                        name=pod.metadata.name,
                        component=cs.name,
                        message=f"Container '{cs.name}' cannot pull image '{cs.image}' ({waiting.reason})",
                        details={
                            "container": cs.name,
                            "image": cs.image,
                            "reason": waiting.reason,
                            "waiting_message": waiting.message,
                        },
                        events=_events_to_strings(events),
                    )
                )
    return defects


def rule_pending_pods(snapshot, settings: Settings) -> list[Defect]:
    defects = []
    for pod in snapshot.pods:
        if not pod.status or pod.status.phase != "Pending":
            continue
        age = _age_minutes(pod.metadata.creation_timestamp)
        if age < PENDING_WARNING_MINUTES:
            continue
        events = _pod_events(snapshot, pod)
        severity = Severity.CRITICAL if age >= PENDING_CRITICAL_MINUTES else Severity.WARNING
        reason = next((e.reason for e in events if getattr(e, "type", "") == "Warning"), "Unscheduled")
        defects.append(
            Defect.create(
                type="pending_pods",
                severity=severity,
                kind="Pod",
                namespace=pod.metadata.namespace,
                name=pod.metadata.name,
                message=f"Pod has been Pending for {int(age)}m ({reason})",
                details={"age_minutes": round(age, 1), "reason": reason},
                events=_events_to_strings(events),
            )
        )
    return defects


def rule_oomkilled(snapshot, settings: Settings) -> list[Defect]:
    defects = []
    for pod in snapshot.pods:
        if not pod.status:
            continue
        for cs in _container_statuses(pod):
            term = cs.state.terminated if cs.state else None
            last_term = cs.last_state.terminated if cs.last_state else None
            hit = None
            if term and term.reason == "OOMKilled":
                hit = term
            elif last_term and last_term.reason == "OOMKilled":
                hit = last_term
            if not hit:
                continue
            container_spec = next(
                (c for c in (pod.spec.containers or []) if c.name == cs.name), None
            )
            mem_limit = None
            if container_spec and container_spec.resources and container_spec.resources.limits:
                mem_limit = container_spec.resources.limits.get("memory")
            defects.append(
                Defect.create(
                    type="oomkilled",
                    severity=Severity.CRITICAL,
                    kind="Pod",
                    namespace=pod.metadata.namespace,
                    name=pod.metadata.name,
                    component=cs.name,
                    message=f"Container '{cs.name}' was OOMKilled (exit code {hit.exit_code})",
                    details={
                        "container": cs.name,
                        "exit_code": hit.exit_code,
                        "memory_limit": mem_limit,
                        "restart_count": cs.restart_count,
                    },
                    events=_events_to_strings(_pod_events(snapshot, pod)),
                )
            )
    return defects


def rule_failing_probes(snapshot, settings: Settings) -> list[Defect]:
    defects = []
    seen = set()
    for e in snapshot.events:
        obj = e.involved_object
        if not obj or obj.kind != "Pod" or getattr(e, "reason", "") != "Unhealthy":
            continue
        key = (obj.namespace, obj.name)
        if key in seen:
            continue
        seen.add(key)
        pod = next(
            (p for p in snapshot.pods if p.metadata.namespace == obj.namespace and p.metadata.name == obj.name),
            None,
        )
        events = snapshot.events_for("Pod", obj.namespace, obj.name)
        probe_events = [ev for ev in events if getattr(ev, "reason", "") == "Unhealthy"]
        message = probe_events[0].message if probe_events else "Liveness/Readiness probe failing"
        defects.append(
            Defect.create(
                type="failing_probes",
                severity=Severity.WARNING,
                kind="Pod",
                namespace=obj.namespace,
                name=obj.name,
                message=message,
                details={
                    "container": (pod.spec.containers[0].name if pod and pod.spec.containers else None),
                },
                events=_events_to_strings(probe_events),
            )
        )
    return defects


def rule_high_restart_count(snapshot, settings: Settings) -> list[Defect]:
    defects = []
    for pod in snapshot.pods:
        if not pod.status:
            continue
        for cs in _container_statuses(pod):
            if cs.restart_count < RESTART_COUNT_WARNING_THRESHOLD:
                continue
            # Avoid duplicating the crashloopbackoff defect for the same container.
            waiting = cs.state.waiting if cs.state else None
            if waiting and waiting.reason == "CrashLoopBackOff":
                continue
            severity = (
                Severity.CRITICAL if cs.restart_count >= RESTART_COUNT_CRITICAL_THRESHOLD else Severity.WARNING
            )
            defects.append(
                Defect.create(
                    type="high_restart_count",
                    severity=severity,
                    kind="Pod",
                    namespace=pod.metadata.namespace,
                    name=pod.metadata.name,
                    component=cs.name,
                    message=f"Container '{cs.name}' has restarted {cs.restart_count} times",
                    details={"container": cs.name, "restart_count": cs.restart_count},
                    events=_events_to_strings(_pod_events(snapshot, pod)),
                )
            )
    return defects


def rule_node_pressure(snapshot, settings: Settings) -> list[Defect]:
    defects = []
    pressure_types = {"DiskPressure", "MemoryPressure", "PIDPressure"}
    for node in snapshot.nodes:
        if not node.status or not node.status.conditions:
            continue
        for cond in node.status.conditions:
            if cond.type in pressure_types and cond.status == "True":
                defects.append(
                    Defect.create(
                        type="node_pressure",
                        severity=Severity.CRITICAL,
                        kind="Node",
                        name=node.metadata.name,
                        component=cond.type,
                        message=f"Node under {cond.type} ({cond.message or cond.reason})",
                        details={"condition": cond.type, "reason": cond.reason},
                    )
                )
    return defects


def rule_node_not_ready(snapshot, settings: Settings) -> list[Defect]:
    defects = []
    for node in snapshot.nodes:
        if not node.status or not node.status.conditions:
            continue
        ready = next((c for c in node.status.conditions if c.type == "Ready"), None)
        if ready and ready.status != "True":
            defects.append(
                Defect.create(
                    type="node_not_ready",
                    severity=Severity.CRITICAL,
                    kind="Node",
                    name=node.metadata.name,
                    message=f"Node is NotReady ({ready.reason or 'unknown reason'})",
                    details={"reason": ready.reason, "message": ready.message},
                )
            )
    return defects


def rule_warning_events(snapshot, settings: Settings) -> list[Defect]:
    defects = []
    cutoff = settings.event_lookback_minutes
    seen = set()
    for e in snapshot.events:
        if getattr(e, "type", "") != "Warning":
            continue
        reason = getattr(e, "reason", "") or ""
        if reason in _HANDLED_EVENT_REASONS:
            continue
        obj = e.involved_object
        if not obj:
            continue
        last_seen = getattr(e, "last_timestamp", None) or getattr(e, "event_time", None)
        if last_seen and _age_minutes(last_seen) > cutoff:
            continue
        key = (obj.kind, obj.namespace, obj.name, reason)
        if key in seen:
            continue
        seen.add(key)
        defects.append(
            Defect.create(
                type="warning_events",
                severity=Severity.WARNING,
                kind=obj.kind or "Unknown",
                namespace=obj.namespace,
                name=obj.name or "unknown",
                component=reason,
                message=f"{reason}: {e.message}",
                details={"reason": reason, "count": getattr(e, "count", 1)},
                events=[f"[Warning] {reason}: {e.message}"],
            )
        )
    return defects


def rule_missing_resource_limits(snapshot, settings: Settings) -> list[Defect]:
    defects = []
    for pod in snapshot.pods:
        if not pod.spec or not pod.spec.containers:
            continue
        missing = []
        for c in pod.spec.containers:
            res = c.resources
            limits = (res.limits if res else None) or {}
            requests = (res.requests if res else None) or {}
            gaps = []
            if "cpu" not in limits:
                gaps.append("cpu limit")
            if "memory" not in limits:
                gaps.append("memory limit")
            if "cpu" not in requests:
                gaps.append("cpu request")
            if "memory" not in requests:
                gaps.append("memory request")
            if gaps:
                missing.append(f"{c.name} ({', '.join(gaps)})")
        if missing:
            defects.append(
                Defect.create(
                    type="missing_resource_limits",
                    severity=Severity.WARNING,
                    kind="Pod",
                    namespace=pod.metadata.namespace,
                    name=pod.metadata.name,
                    message=f"Missing resource requests/limits: {'; '.join(missing)}",
                    details={"containers": missing},
                )
            )
    return defects


def rule_pvc_binding_failures(snapshot, settings: Settings) -> list[Defect]:
    defects = []
    for pvc in snapshot.pvcs:
        phase = pvc.status.phase if pvc.status else None
        if phase == "Bound":
            continue
        age = _age_minutes(pvc.metadata.creation_timestamp)
        if phase == "Pending" and age < PVC_PENDING_WARNING_MINUTES:
            continue
        severity = Severity.CRITICAL if phase == "Lost" else Severity.WARNING
        events = snapshot.events_for("PersistentVolumeClaim", pvc.metadata.namespace, pvc.metadata.name)
        defects.append(
            Defect.create(
                type="pvc_binding_failures",
                severity=severity,
                kind="PersistentVolumeClaim",
                namespace=pvc.metadata.namespace,
                name=pvc.metadata.name,
                message=f"PVC is {phase} (unbound for {int(age)}m)",
                details={
                    "phase": phase,
                    "storage_class": pvc.spec.storage_class_name if pvc.spec else None,
                    "requested_storage": (
                        pvc.spec.resources.requests.get("storage")
                        if pvc.spec and pvc.spec.resources and pvc.spec.resources.requests
                        else None
                    ),
                },
                events=_events_to_strings(events),
            )
        )
    return defects


def rule_service_port_mismatch(snapshot, settings: Settings) -> list[Defect]:
    defects = []
    endpoints_by_key = {
        (ep.metadata.namespace, ep.metadata.name): ep for ep in snapshot.endpoints
    }
    for svc in snapshot.services:
        if not svc.spec or not svc.spec.selector or svc.spec.type == "ExternalName":
            continue
        key = (svc.metadata.namespace, svc.metadata.name)
        ep = endpoints_by_key.get(key)
        has_addresses = bool(
            ep and ep.subsets and any(s.addresses for s in ep.subsets)
        )
        if not has_addresses:
            defects.append(
                Defect.create(
                    type="service_port_mismatch",
                    severity=Severity.WARNING,
                    kind="Service",
                    namespace=svc.metadata.namespace,
                    name=svc.metadata.name,
                    message="Service has no matching pod endpoints (selector/targetPort mismatch or no ready pods)",
                    details={"selector": svc.spec.selector, "ports": [
                        f"{p.port}->{p.target_port}" for p in (svc.spec.ports or [])
                    ]},
                )
            )
    return defects


def rule_deprecated_apis(snapshot, settings: Settings) -> list[Defect]:
    defects = []
    for ing in snapshot.deprecated_ingresses:
        defects.append(
            Defect.create(
                type="deprecated_apis",
                severity=Severity.WARNING,
                kind="Ingress",
                namespace=ing.metadata.namespace,
                name=ing.metadata.name,
                message="Ingress is using the deprecated 'extensions/v1beta1' API group",
                details={"deprecated_api_version": "extensions/v1beta1", "replacement": "networking.k8s.io/v1"},
            )
        )
    return defects


REGISTRY: dict[str, Callable] = {
    "crashloopbackoff": rule_crashloopbackoff,
    "imagepullbackoff": rule_imagepullbackoff,
    "pending_pods": rule_pending_pods,
    "oomkilled": rule_oomkilled,
    "failing_probes": rule_failing_probes,
    "high_restart_count": rule_high_restart_count,
    "node_pressure": rule_node_pressure,
    "node_not_ready": rule_node_not_ready,
    "warning_events": rule_warning_events,
    "missing_resource_limits": rule_missing_resource_limits,
    "pvc_binding_failures": rule_pvc_binding_failures,
    "service_port_mismatch": rule_service_port_mismatch,
    "deprecated_apis": rule_deprecated_apis,
}


def run_all_rules(snapshot, settings: Settings) -> list[Defect]:
    enabled = settings.enabled_rules_set
    defects: list[Defect] = []
    for name, rule_fn in REGISTRY.items():
        if name not in enabled:
            continue
        try:
            defects.extend(rule_fn(snapshot, settings))
        except Exception:
            logger.exception("rule '%s' failed", name)
    return defects
