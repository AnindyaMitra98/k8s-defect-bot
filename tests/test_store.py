"""Store tests: merging the two defect sources, and expiring dead agents."""
from __future__ import annotations

from datetime import timedelta

from app.models import (
    Defect,
    NodeFinding,
    NodeReport,
    ScanSummary,
    Severity,
    Source,
)
from app.store import ScanStore


def cluster_defect(name="web-1", severity=Severity.CRITICAL, component=None) -> Defect:
    return Defect.create(
        type="crashloopbackoff",
        severity=severity,
        kind="Pod",
        namespace="default",
        name=name,
        component=component,
        message="crash-looping",
    )


def node_defect(node="ip-10-0-1-5", check="node_disk_usage") -> Defect:
    return Defect.create(
        type=check,
        severity=Severity.WARNING,
        kind="Node",
        name=node,
        component=check,
        source=Source.NODE_AGENT,
        node=node,
        message="disk 85% full",
    )


def test_replace_swaps_the_whole_cluster_set(store):
    store.replace([cluster_defect("a"), cluster_defect("b")], ScanSummary())
    assert len(store.list()) == 2

    store.replace([cluster_defect("c")], ScanSummary())
    assert [d.name for d in store.list()] == ["c"]


def test_two_defects_on_one_pod_both_survive_the_store(store):
    """Regression: identical ids meant the dict dropped one silently."""
    store.replace(
        [cluster_defect("web-1", component="app"), cluster_defect("web-1", component="sidecar")],
        ScanSummary(),
    )
    assert len(store.list()) == 2


def test_node_reports_merge_with_cluster_defects(store):
    store.replace([cluster_defect()], ScanSummary())
    report = NodeReport(node="ip-10-0-1-5", checks_run=8, findings=[
        NodeFinding(check="node_disk_usage", severity=Severity.WARNING, message="85% full")
    ])
    store.record_node_report(report, [node_defect()])

    everything = store.list()
    assert len(everything) == 2
    assert {d.source for d in everything} == {Source.CLUSTER, Source.NODE_AGENT}


def test_a_cluster_scan_does_not_clear_node_findings(store):
    store.record_node_report(NodeReport(node="n1"), [node_defect("n1")])
    store.replace([cluster_defect()], ScanSummary())

    assert len(store.list(source="node-agent")) == 1


def test_a_new_report_replaces_that_node_only(store):
    store.record_node_report(NodeReport(node="n1"), [node_defect("n1"), node_defect("n1", "node_pid_usage")])
    store.record_node_report(NodeReport(node="n2"), [node_defect("n2")])
    assert len(store.list(source="node-agent")) == 3

    store.record_node_report(NodeReport(node="n1"), [])  # n1 recovered
    remaining = store.list(source="node-agent")
    assert len(remaining) == 1
    assert remaining[0].node == "n2"


def test_stale_node_findings_expire_but_the_agent_stays_visible(store):
    store.record_node_report(NodeReport(node="n1"), [node_defect("n1")])
    assert len(store.list(source="node-agent")) == 1

    # Age the entry past the TTL.
    entry = store._nodes["n1"]
    entry.received_at = entry.received_at - timedelta(seconds=1000)

    assert store.list(source="node-agent") == []
    statuses = store.node_statuses()
    assert len(statuses) == 1 and statuses[0].stale is True


def test_get_finds_defects_from_both_sources(store):
    cluster = cluster_defect()
    node = node_defect()
    store.replace([cluster], ScanSummary())
    store.record_node_report(NodeReport(node="ip-10-0-1-5"), [node])

    assert store.get(cluster.id).name == "web-1"
    assert store.get(node.id).source == Source.NODE_AGENT
    assert store.get("nope") is None


def test_filters_compose(store):
    store.replace(
        [cluster_defect("a", Severity.CRITICAL), cluster_defect("b", Severity.WARNING)],
        ScanSummary(),
    )
    store.record_node_report(NodeReport(node="n1"), [node_defect("n1")])

    assert len(store.list(severity="critical")) == 1
    assert len(store.list(source="cluster")) == 2
    assert len(store.list(node="n1")) == 1
    assert len(store.list(defect_type="node_disk_usage")) == 1


def test_criticals_sort_first(store):
    store.replace(
        [cluster_defect("z", Severity.WARNING), cluster_defect("a", Severity.CRITICAL)],
        ScanSummary(),
    )
    assert [d.severity for d in store.list()] == [Severity.CRITICAL, Severity.WARNING]


def test_summary_counts_span_both_sources(store):
    store.replace([cluster_defect()], ScanSummary(pods_scanned=42, nodes_scanned=3))
    store.record_node_report(NodeReport(node="n1", checks_run=9), [node_defect("n1")])

    summary = store.summary()
    assert summary.total_defects == 2
    assert summary.critical_count == 1
    assert summary.warning_count == 1
    assert summary.node_defects == 1
    assert summary.agents_reporting == 1
    assert summary.pods_scanned == 42, "scan metadata must survive the recount"


def test_node_status_reports_skew_and_counts(store):
    store.record_node_report(
        NodeReport(node="n1", agent_version="0.2.0", checks_run=10),
        [node_defect("n1")],
        clock_skew_seconds=12.34,
    )
    status = store.node_statuses()[0]
    assert status.node == "n1"
    assert status.checks_run == 10
    assert status.finding_count == 1
    assert status.clock_skew_seconds == 12.3
    assert status.stale is False


def test_configure_changes_the_ttl():
    store = ScanStore(node_report_ttl_seconds=900)
    store.configure(60)
    store.record_node_report(NodeReport(node="n1"), [node_defect("n1")])
    entry = store._nodes["n1"]
    entry.received_at = entry.received_at - timedelta(seconds=120)
    assert store.list(source="node-agent") == []
