"""Rule engine tests, focused on the identity/coverage bugs that lose findings."""
from __future__ import annotations

from conftest import FakeSnapshot, container_status, event, node, ns, pod

from app.models import Severity, make_defect_id
from scraper import rules


def test_two_crashlooping_containers_in_one_pod_produce_two_defects(settings):
    """Regression: without a per-container id both collapsed onto one key."""
    snap = FakeSnapshot(
        pods=[
            pod(
                container_statuses=[
                    container_status(name="app", restart_count=7, waiting_reason="CrashLoopBackOff"),
                    container_status(name="sidecar", restart_count=3, waiting_reason="CrashLoopBackOff"),
                ]
            )
        ]
    )
    defects = rules.rule_crashloopbackoff(snap, settings)

    assert len(defects) == 2
    assert len({d.id for d in defects}) == 2, "container must participate in the defect id"
    assert {d.details["container"] for d in defects} == {"app", "sidecar"}


def test_crashlooping_init_container_is_detected(settings):
    """A pod stuck on an init container has no app container statuses yet."""
    snap = FakeSnapshot(
        pods=[
            pod(
                phase="Pending",
                container_statuses=None,
                init_container_statuses=[
                    container_status(name="migrate", restart_count=4, waiting_reason="CrashLoopBackOff")
                ],
            )
        ]
    )
    defects = rules.rule_crashloopbackoff(snap, settings)

    assert len(defects) == 1
    assert defects[0].details["container"] == "migrate"
    assert defects[0].severity == Severity.CRITICAL


def test_defect_ids_are_stable_across_scans(settings):
    snap = FakeSnapshot(
        pods=[pod(container_statuses=[container_status(waiting_reason="CrashLoopBackOff")])]
    )
    first = rules.rule_crashloopbackoff(snap, settings)[0]
    second = rules.rule_crashloopbackoff(snap, settings)[0]
    assert first.id == second.id


def test_make_defect_id_distinguishes_components():
    a = make_defect_id("oomkilled", "Pod", "default", "web-1", "app")
    b = make_defect_id("oomkilled", "Pod", "default", "web-1", "sidecar")
    assert a != b


def test_imagepullbackoff_reports_the_image(settings):
    snap = FakeSnapshot(
        pods=[
            pod(
                container_statuses=[
                    container_status(image="ghcr.io/x/y:missing", waiting_reason="ImagePullBackOff")
                ]
            )
        ]
    )
    defects = rules.rule_imagepullbackoff(snap, settings)
    assert len(defects) == 1
    assert defects[0].details["image"] == "ghcr.io/x/y:missing"


def test_pending_pod_severity_escalates_with_age(settings):
    young = FakeSnapshot(pods=[pod(phase="Pending", created_minutes_ago=7)])
    old = FakeSnapshot(pods=[pod(phase="Pending", created_minutes_ago=40)])
    fresh = FakeSnapshot(pods=[pod(phase="Pending", created_minutes_ago=1)])

    assert rules.rule_pending_pods(fresh, settings) == []
    assert rules.rule_pending_pods(young, settings)[0].severity == Severity.WARNING
    assert rules.rule_pending_pods(old, settings)[0].severity == Severity.CRITICAL


def test_high_restart_count_skips_containers_already_crashlooping(settings):
    snap = FakeSnapshot(
        pods=[
            pod(
                container_statuses=[
                    container_status(name="app", restart_count=30, waiting_reason="CrashLoopBackOff"),
                    container_status(name="flaky", restart_count=9),
                ]
            )
        ]
    )
    defects = rules.rule_high_restart_count(snap, settings)
    assert [d.details["container"] for d in defects] == ["flaky"]
    assert defects[0].severity == Severity.WARNING


def test_oomkilled_reads_the_memory_limit(settings):
    snap = FakeSnapshot(
        pods=[
            pod(
                container_statuses=[
                    container_status(name="app", last_terminated_reason="OOMKilled", exit_code=137)
                ]
            )
        ]
    )
    defects = rules.rule_oomkilled(snap, settings)
    assert len(defects) == 1
    assert defects[0].details["memory_limit"] == "256Mi"
    assert defects[0].details["exit_code"] == 137


def test_node_not_ready(settings):
    snap = FakeSnapshot(nodes=[node(ready="Unknown")])
    defects = rules.rule_node_not_ready(snap, settings)
    assert len(defects) == 1
    assert defects[0].kind == "Node"


def test_node_pressure_reports_each_condition_separately(settings):
    snap = FakeSnapshot(
        nodes=[
            node(
                conditions=[
                    ns(type="DiskPressure", status="True", reason="Full", message="disk full"),
                    ns(type="MemoryPressure", status="True", reason="Low", message="low memory"),
                    ns(type="PIDPressure", status="False", reason=None, message=None),
                ]
            )
        ]
    )
    defects = rules.rule_node_pressure(snap, settings)
    assert len(defects) == 2
    assert len({d.id for d in defects}) == 2


def test_missing_resource_limits_lists_the_gaps(settings):
    bare = pod(containers=[ns(name="app", resources=ns(limits=None, requests=None))])
    defects = rules.rule_missing_resource_limits(FakeSnapshot(pods=[bare]), settings)
    assert len(defects) == 1
    assert "cpu limit" in defects[0].message and "memory request" in defects[0].message


def test_service_without_endpoints_is_flagged(settings):
    svc = ns(
        metadata=ns(name="api", namespace="default"),
        spec=ns(selector={"app": "api"}, type="ClusterIP",
                ports=[ns(port=80, target_port=8080)]),
    )
    endpoints = ns(metadata=ns(name="api", namespace="default"), subsets=None)
    defects = rules.rule_service_port_mismatch(
        FakeSnapshot(services=[svc], endpoints=[endpoints]), settings
    )
    assert len(defects) == 1
    assert defects[0].kind == "Service"


def test_warning_events_skips_reasons_owned_by_specific_rules(settings):
    snap = FakeSnapshot(
        events=[
            event(reason="Unhealthy"),          # handled by failing_probes
            event(reason="FailedScheduling"),   # handled by pending_pods
            event(reason="NodeNotSchedulable"),  # generic -> should surface
        ]
    )
    defects = rules.rule_warning_events(snap, settings)
    assert [d.details["reason"] for d in defects] == ["NodeNotSchedulable"]


def test_run_all_rules_respects_the_enabled_set(settings):
    snap = FakeSnapshot(
        pods=[pod(container_statuses=[container_status(waiting_reason="CrashLoopBackOff")])]
    )
    only_crashloop = settings.model_copy(update={"enabled_rules": "crashloopbackoff"})
    nothing = settings.model_copy(update={"enabled_rules": "imagepullbackoff"})

    assert len(rules.run_all_rules(snap, only_crashloop)) == 1
    assert rules.run_all_rules(snap, nothing) == []


def test_a_broken_rule_does_not_abort_the_scan(settings, monkeypatch):
    def explode(_snapshot, _settings):
        raise RuntimeError("boom")

    monkeypatch.setitem(rules.REGISTRY, "crashloopbackoff", explode)
    snap = FakeSnapshot(nodes=[node(ready="False")])

    defects = rules.run_all_rules(snap, settings)
    assert any(d.type == "node_not_ready" for d in defects)


def test_every_registered_rule_has_a_remediation_template():
    from analyzer.solution_engine import TEMPLATES

    missing = set(rules.REGISTRY) - set(TEMPLATES)
    assert not missing, f"cluster rules without a remediation template: {sorted(missing)}"
