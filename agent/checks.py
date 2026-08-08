"""Node-local health checks run by the DaemonSet agent.

These deliberately cover what the control-plane scan *cannot* see. The API server
reports a node as Ready or NotReady; by the time it flips, pods have already been
evicted. Reading the node's own filesystem, /proc, kubelet endpoint, and DNS
resolver catches the same problems while they are still trends.

Constraints every check honours:
  * stdlib only -- the agent must stay tiny and dependency-free
  * read-only -- nothing here mutates the node
  * never raises -- run_all_checks isolates failures per check
  * no root required (except node_kernel_errors, which is opt-in)

Host paths are injected via settings so the checks are testable against fixtures.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import stat
import urllib.error
import urllib.request
from typing import Callable, Optional

from app.config import Settings
from app.models import NodeFinding, Severity

logger = logging.getLogger("k8s-defect-bot.agent.checks")

_KERNEL_ERROR_PATTERNS = re.compile(
    r"(out of memory|oom-kill|killed process|blocked for more than|"
    r"I/O error|EXT4-fs error|XFS \(.*\): (metadata|log) I/O error|"
    r"nf_conntrack: table full|soft lockup|hung_task)",
    re.IGNORECASE,
)


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw.strip().split()[0])
    except (ValueError, IndexError):
        return None


def _pct(used: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return round(used / total * 100.0, 1)


def _candidate_filesystems(settings: Settings) -> list[str]:
    """Host paths worth measuring, deduplicated by underlying filesystem.

    Returns nothing where statvfs is unavailable (i.e. Windows), so the agent
    still runs during local development instead of erroring out. In production
    it only ever runs on Linux nodes.
    """
    if not hasattr(os, "statvfs"):
        return []

    root = settings.host_root
    candidates = [root]
    for sub in ("var/lib/kubelet", "var/lib/containerd", "var/log"):
        path = os.path.join(root, sub)
        if os.path.isdir(path):
            candidates.append(path)

    seen: set[tuple] = set()
    out = []
    for path in candidates:
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        key = (st.f_blocks, st.f_bsize, st.f_files)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _display_path(settings: Settings, path: str) -> str:
    """Report the path as it exists on the node, not as mounted in the agent pod."""
    rel = os.path.relpath(path, settings.host_root)
    return "/" if rel == "." else "/" + rel.replace(os.sep, "/")


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_node_disk_usage(settings: Settings) -> list[NodeFinding]:
    findings = []
    for path in _candidate_filesystems(settings):
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total <= 0:
            continue
        used_percent = _pct(total - free, total)
        if used_percent < settings.disk_warn_percent:
            continue
        severity = (
            Severity.CRITICAL
            if used_percent >= settings.disk_critical_percent
            else Severity.WARNING
        )
        shown = _display_path(settings, path)
        findings.append(
            NodeFinding(
                check="node_disk_usage",
                severity=severity,
                message=f"Filesystem {shown} is {used_percent}% full ({round(free / 2**30, 1)} GiB free)",
                details={
                    "path": shown,
                    "used_percent": used_percent,
                    "free_gb": round(free / 2**30, 1),
                    "total_gb": round(total / 2**30, 1),
                },
            )
        )
    return findings


def check_node_inode_usage(settings: Settings) -> list[NodeFinding]:
    findings = []
    for path in _candidate_filesystems(settings):
        st = os.statvfs(path)
        if st.f_files <= 0:
            continue  # some filesystems (tmpfs, overlayfs) don't report inodes
        used_percent = _pct(st.f_files - st.f_favail, st.f_files)
        if used_percent < settings.disk_warn_percent:
            continue
        severity = (
            Severity.CRITICAL
            if used_percent >= settings.disk_critical_percent
            else Severity.WARNING
        )
        shown = _display_path(settings, path)
        findings.append(
            NodeFinding(
                check="node_inode_usage",
                severity=severity,
                message=f"Filesystem {shown} has used {used_percent}% of its inodes",
                details={
                    "path": shown,
                    "used_percent": used_percent,
                    "free_inodes": st.f_favail,
                    "total_inodes": st.f_files,
                },
            )
        )
    return findings


def check_node_memory_available(settings: Settings) -> list[NodeFinding]:
    raw = _read_text(os.path.join(settings.host_proc, "meminfo"))
    if not raw:
        return []
    values = {}
    for line in raw.splitlines():
        parts = line.split(":")
        if len(parts) == 2:
            try:
                values[parts[0].strip()] = int(parts[1].strip().split()[0])  # kB
            except (ValueError, IndexError):
                continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return []

    used_percent = _pct(total - available, total)
    if used_percent < settings.memory_warn_percent:
        return []
    return [
        NodeFinding(
            check="node_memory_available",
            severity=Severity.CRITICAL if used_percent >= 95 else Severity.WARNING,
            message=(
                f"Node memory is {used_percent}% used, only "
                f"{round(available / 1024)} MiB available"
            ),
            details={
                "used_percent": used_percent,
                "available_percent": round(100 - used_percent, 1),
                "available_mb": round(available / 1024),
                "total_mb": round(total / 1024),
            },
        )
    ]


def _cpu_count(settings: Settings) -> int:
    stat_raw = _read_text(os.path.join(settings.host_proc, "stat")) or ""
    count = len(re.findall(r"^cpu\d+", stat_raw, re.MULTILINE))
    return count or os.cpu_count() or 1


def check_node_load_average(settings: Settings) -> list[NodeFinding]:
    raw = _read_text(os.path.join(settings.host_proc, "loadavg"))
    if not raw:
        return []
    try:
        load1, load5, load15 = (float(x) for x in raw.split()[:3])
    except (ValueError, IndexError):
        return []

    cpus = _cpu_count(settings)
    per_cpu = round(load5 / cpus, 2)
    if per_cpu < settings.load_per_cpu_warn:
        return []
    return [
        NodeFinding(
            check="node_load_average",
            severity=Severity.CRITICAL if per_cpu >= settings.load_per_cpu_warn * 2 else Severity.WARNING,
            message=f"Load average {load5} over {cpus} CPUs ({per_cpu} per CPU)",
            details={
                "load1": load1,
                "load5": load5,
                "load15": load15,
                "cpu_count": cpus,
                "load_per_cpu": per_cpu,
            },
        )
    ]


def check_node_pid_usage(settings: Settings) -> list[NodeFinding]:
    pid_max = _read_int(os.path.join(settings.host_proc, "sys/kernel/pid_max"))
    if not pid_max:
        return []
    try:
        pids = sum(1 for entry in os.listdir(settings.host_proc) if entry.isdigit())
    except OSError:
        return []

    used_percent = _pct(pids, pid_max)
    if used_percent < settings.pid_warn_percent:
        return []
    return [
        NodeFinding(
            check="node_pid_usage",
            severity=Severity.CRITICAL if used_percent >= 90 else Severity.WARNING,
            message=f"Node is using {used_percent}% of available PIDs ({pids}/{pid_max})",
            details={"pids": pids, "pid_max": pid_max, "used_percent": used_percent},
        )
    ]


def check_node_conntrack_usage(settings: Settings) -> list[NodeFinding]:
    base = os.path.join(settings.host_proc, "sys/net/netfilter")
    count = _read_int(os.path.join(base, "nf_conntrack_count"))
    maximum = _read_int(os.path.join(base, "nf_conntrack_max"))
    if count is None or not maximum:
        return []  # conntrack not loaded -- not an error

    used_percent = _pct(count, maximum)
    if used_percent < settings.conntrack_warn_percent:
        return []
    return [
        NodeFinding(
            check="node_conntrack_usage",
            severity=Severity.CRITICAL if used_percent >= 90 else Severity.WARNING,
            message=f"Conntrack table {used_percent}% full ({count}/{maximum})",
            details={"count": count, "max": maximum, "used_percent": used_percent},
        )
    ]


def check_node_kubelet_health(settings: Settings) -> list[NodeFinding]:
    """Hits the kubelet's own healthz port. Requires hostNetwork on the agent pod."""
    try:
        with urllib.request.urlopen(settings.kubelet_healthz_url, timeout=5) as resp:
            body = resp.read(256).decode("utf-8", "replace").strip()
            if resp.status == 200 and body.lower().startswith("ok"):
                return []
            return [
                NodeFinding(
                    check="node_kubelet_health",
                    severity=Severity.CRITICAL,
                    message=f"Kubelet healthz returned HTTP {resp.status}: {body[:120]}",
                    details={"status": resp.status, "body": body[:200],
                             "url": settings.kubelet_healthz_url},
                )
            ]
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return [
            NodeFinding(
                check="node_kubelet_health",
                severity=Severity.CRITICAL,
                message=f"Kubelet healthz unreachable: {exc}",
                details={"error": str(exc), "url": settings.kubelet_healthz_url},
            )
        ]


def check_node_container_runtime(settings: Settings) -> list[NodeFinding]:
    socket_path = os.path.join(
        settings.host_root, settings.container_runtime_socket.lstrip("/")
    )
    try:
        mode = os.stat(socket_path).st_mode
        if stat.S_ISSOCK(mode):
            return []
        problem = "path exists but is not a socket"
    except OSError as exc:
        problem = str(exc)
    return [
        NodeFinding(
            check="node_container_runtime",
            severity=Severity.CRITICAL,
            message=f"Container runtime socket unavailable: {problem}",
            details={"socket": settings.container_runtime_socket, "error": problem},
        )
    ]


def check_node_dns_resolution(settings: Settings) -> list[NodeFinding]:
    try:
        socket.getaddrinfo(settings.dns_probe_host, None)
        return []
    except socket.gaierror as exc:
        return [
            NodeFinding(
                check="node_dns_resolution",
                severity=Severity.CRITICAL,
                message=f"Cluster DNS lookup of {settings.dns_probe_host} failed: {exc}",
                details={"host": settings.dns_probe_host, "error": str(exc)},
            )
        ]


def check_node_apiserver_reachable(settings: Settings) -> list[NodeFinding]:
    host = os.getenv("KUBERNETES_SERVICE_HOST")
    port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        return []  # not running in a cluster -- nothing meaningful to check
    endpoint = f"{host}:{port}"
    try:
        with socket.create_connection((host, int(port)), timeout=5):
            return []
    except (OSError, ValueError) as exc:
        return [
            NodeFinding(
                check="node_apiserver_reachable",
                severity=Severity.CRITICAL,
                message=f"Cannot reach the Kubernetes API server at {endpoint}: {exc}",
                details={"endpoint": endpoint, "error": str(exc)},
            )
        ]


def check_node_kernel_errors(settings: Settings) -> list[NodeFinding]:
    """Scans the kernel ring buffer. Needs a privileged/root agent -- opt-in only."""
    path = os.path.join(settings.host_root, "dev/kmsg")
    matches: list[str] = []
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        logger.debug("node_kernel_errors: cannot open %s (%s)", path, exc)
        return []
    try:
        while len(matches) < 20:
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                break  # drained the buffer
            except OSError:
                break
            if not chunk:
                break
            for line in chunk.decode("utf-8", "replace").splitlines():
                if _KERNEL_ERROR_PATTERNS.search(line):
                    matches.append(line.strip()[:300])
    finally:
        os.close(fd)

    if not matches:
        return []
    return [
        NodeFinding(
            check="node_kernel_errors",
            severity=Severity.CRITICAL,
            message=f"{len(matches)} kernel error line(s) in the ring buffer",
            details={
                "match_count": len(matches),
                "pattern": "oom / blocked task / I-O error / conntrack full",
                "samples": matches[:5],
            },
        )
    ]


REGISTRY: dict[str, Callable[[Settings], list[NodeFinding]]] = {
    "node_disk_usage": check_node_disk_usage,
    "node_inode_usage": check_node_inode_usage,
    "node_memory_available": check_node_memory_available,
    "node_load_average": check_node_load_average,
    "node_pid_usage": check_node_pid_usage,
    "node_conntrack_usage": check_node_conntrack_usage,
    "node_kubelet_health": check_node_kubelet_health,
    "node_container_runtime": check_node_container_runtime,
    "node_dns_resolution": check_node_dns_resolution,
    "node_apiserver_reachable": check_node_apiserver_reachable,
    "node_kernel_errors": check_node_kernel_errors,
}


def run_all_checks(settings: Settings) -> tuple[list[NodeFinding], int]:
    """Runs every enabled check. Returns (findings, checks_run)."""
    enabled = settings.enabled_node_checks_set
    findings: list[NodeFinding] = []
    ran = 0
    for name, check_fn in REGISTRY.items():
        if name not in enabled:
            continue
        ran += 1
        try:
            findings.extend(check_fn(settings))
        except Exception:
            logger.exception("node check '%s' failed", name)
    return findings, ran
