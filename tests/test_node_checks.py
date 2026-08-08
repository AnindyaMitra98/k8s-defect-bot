"""Node-agent check tests, driven against a fake /proc and host root on disk."""
from __future__ import annotations

import os
import socket

import pytest

from agent import checks
from app.config import Settings
from app.models import Severity


@pytest.fixture
def fake_host(tmp_path):
    """Builds a minimal host filesystem the checks can read."""
    proc = tmp_path / "proc"
    (proc / "sys" / "kernel").mkdir(parents=True)
    (proc / "sys" / "net" / "netfilter").mkdir(parents=True)

    (proc / "meminfo").write_text(
        "MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\nBuffers: 1024 kB\n"
    )
    (proc / "loadavg").write_text("0.50 0.80 0.75 2/512 12345\n")
    (proc / "stat").write_text("cpu  1 2 3\ncpu0 1 2 3\ncpu1 1 2 3\ncpu2 1 2 3\ncpu3 1 2 3\n")
    (proc / "sys" / "kernel" / "pid_max").write_text("32768\n")
    (proc / "sys" / "net" / "netfilter" / "nf_conntrack_count").write_text("1000\n")
    (proc / "sys" / "net" / "netfilter" / "nf_conntrack_max").write_text("131072\n")
    for pid in ("1", "42", "1337"):
        (proc / pid).mkdir()

    root = tmp_path / "root"
    root.mkdir()

    return Settings(
        host_proc=str(proc),
        host_root=str(root),
        enabled_node_checks="node_memory_available,node_load_average",
        llm_provider="none",
    )


def test_memory_is_quiet_when_there_is_headroom(fake_host):
    assert checks.check_node_memory_available(fake_host) == []


def test_memory_warns_when_available_drops(fake_host, tmp_path):
    (tmp_path / "proc" / "meminfo").write_text(
        "MemTotal:       16384000 kB\nMemAvailable:     819200 kB\n"
    )
    findings = checks.check_node_memory_available(fake_host)
    assert len(findings) == 1
    assert findings[0].check == "node_memory_available"
    assert findings[0].details["used_percent"] == 95.0
    assert findings[0].severity == Severity.CRITICAL


def test_memory_check_is_silent_on_unreadable_meminfo(fake_host, tmp_path):
    (tmp_path / "proc" / "meminfo").unlink()
    assert checks.check_node_memory_available(fake_host) == []


def test_load_uses_cpu_count_from_proc_stat(fake_host, tmp_path):
    # 0.80 over 4 CPUs = 0.2/CPU -- below the 2.0 threshold.
    assert checks.check_node_load_average(fake_host) == []

    (tmp_path / "proc" / "loadavg").write_text("9.00 12.00 10.00 2/512 1\n")
    findings = checks.check_node_load_average(fake_host)
    assert len(findings) == 1
    assert findings[0].details["cpu_count"] == 4
    assert findings[0].details["load_per_cpu"] == 3.0
    assert findings[0].severity == Severity.WARNING  # 3.0 < 2*2.0


def test_load_goes_critical_at_double_the_threshold(fake_host, tmp_path):
    (tmp_path / "proc" / "loadavg").write_text("20.00 20.00 20.00 2/512 1\n")
    findings = checks.check_node_load_average(fake_host)
    assert findings[0].severity == Severity.CRITICAL


def test_pid_usage_counts_numeric_proc_entries(fake_host, tmp_path):
    assert checks.check_node_pid_usage(fake_host) == []  # 3 of 32768

    (tmp_path / "proc" / "sys" / "kernel" / "pid_max").write_text("3\n")
    findings = checks.check_node_pid_usage(fake_host)
    assert len(findings) == 1
    assert findings[0].details["pids"] == 3
    assert findings[0].details["used_percent"] == 100.0


def test_conntrack_absent_is_not_an_error(fake_host, tmp_path):
    assert checks.check_node_conntrack_usage(fake_host) == []

    (tmp_path / "proc" / "sys" / "net" / "netfilter" / "nf_conntrack_count").unlink()
    assert checks.check_node_conntrack_usage(fake_host) == []


def test_conntrack_warns_when_the_table_fills(fake_host, tmp_path):
    (tmp_path / "proc" / "sys" / "net" / "netfilter" / "nf_conntrack_count").write_text("120000\n")
    findings = checks.check_node_conntrack_usage(fake_host)
    assert len(findings) == 1
    assert findings[0].details["used_percent"] == 91.6
    assert findings[0].severity == Severity.CRITICAL


@pytest.mark.skipif(not hasattr(os, "statvfs"), reason="statvfs is POSIX-only")
def test_disk_usage_reports_host_relative_paths(fake_host):
    findings = checks.check_node_disk_usage(
        fake_host.model_copy(update={"disk_warn_percent": 0.0, "disk_critical_percent": 101.0})
    )
    assert findings, "a 0% threshold must always report"
    assert findings[0].details["path"] == "/"
    assert findings[0].severity == Severity.WARNING


def test_disk_checks_degrade_quietly_without_statvfs(fake_host, monkeypatch):
    monkeypatch.delattr(os, "statvfs", raising=False)
    assert checks.check_node_disk_usage(fake_host) == []
    assert checks.check_node_inode_usage(fake_host) == []


def test_container_runtime_missing_socket_is_critical(fake_host):
    findings = checks.check_node_container_runtime(fake_host)
    assert len(findings) == 1
    assert findings[0].severity == Severity.CRITICAL
    assert findings[0].details["socket"] == "/run/containerd/containerd.sock"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX is POSIX-only")
def test_container_runtime_accepts_a_real_socket(fake_host, tmp_path):
    sock_dir = tmp_path / "root" / "run" / "containerd"
    sock_dir.mkdir(parents=True)
    sock_path = sock_dir / "containerd.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
    except OSError:
        pytest.skip("cannot bind a unix socket here")
    try:
        assert checks.check_node_container_runtime(fake_host) == []
    finally:
        server.close()
        os.unlink(sock_path)


def test_apiserver_check_is_skipped_outside_a_cluster(fake_host, monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert checks.check_node_apiserver_reachable(fake_host) == []


def test_apiserver_unreachable_is_reported(fake_host, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "1")  # nothing listens here
    findings = checks.check_node_apiserver_reachable(fake_host)
    assert len(findings) == 1
    assert findings[0].check == "node_apiserver_reachable"


def test_dns_failure_is_reported(fake_host):
    settings = fake_host.model_copy(
        update={"dns_probe_host": "this-host-does-not-exist.invalid"}
    )
    findings = checks.check_node_dns_resolution(settings)
    assert len(findings) == 1
    assert findings[0].severity == Severity.CRITICAL


def test_kubelet_health_unreachable_is_reported(fake_host):
    settings = fake_host.model_copy(
        update={"kubelet_healthz_url": "http://127.0.0.1:1/healthz"}
    )
    findings = checks.check_node_kubelet_health(settings)
    assert len(findings) == 1
    assert findings[0].check == "node_kubelet_health"


def test_run_all_checks_honours_the_enabled_set(fake_host):
    findings, ran = checks.run_all_checks(fake_host)
    assert ran == 2  # memory + load, per the fixture's enabled set
    assert findings == []


def test_run_all_checks_isolates_a_failing_check(fake_host, monkeypatch):
    def explode(_settings):
        raise RuntimeError("boom")

    monkeypatch.setitem(checks.REGISTRY, "node_memory_available", explode)
    settings = fake_host.model_copy(
        update={"enabled_node_checks": "node_memory_available,node_load_average"}
    )
    findings, ran = checks.run_all_checks(settings)
    assert ran == 2
    assert findings == []  # load is healthy; the exploding check didn't propagate


def test_every_registered_check_has_a_remediation_template():
    from analyzer.solution_engine import TEMPLATES

    missing = set(checks.REGISTRY) - set(TEMPLATES)
    assert not missing, f"node checks without a remediation template: {sorted(missing)}"
