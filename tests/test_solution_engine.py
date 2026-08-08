"""Remediation-template tests.

The templates are the product: every defect the dashboard shows is one of these
rendered against a Defect. They never raise -- a rule that stops populating a
details key must degrade to vaguer prose, not break the scan -- so most of what
follows is parametrized over the whole registry rather than picked by hand.
"""
from __future__ import annotations

import pytest

from analyzer import llm
from analyzer.solution_engine import TEMPLATES, enrich_batch, generate_solution
from app.models import Defect, Severity, Source

CLUSTER_TYPES = [
    "crashloopbackoff", "imagepullbackoff", "pending_pods", "oomkilled",
    "failing_probes", "high_restart_count", "node_pressure", "node_not_ready",
    "warning_events", "missing_resource_limits", "pvc_binding_failures",
    "service_port_mismatch", "deprecated_apis",
]

NODE_TYPES = [t for t in TEMPLATES if t.startswith("node_") and t not in CLUSTER_TYPES]

# Types that name a single pod, so their kubectl commands must be namespace-scoped.
POD_SCOPED_TYPES = [
    "crashloopbackoff", "imagepullbackoff", "oomkilled", "pending_pods",
    "high_restart_count", "failing_probes",
]


def cluster_defect(defect_type: str, name="web-1", namespace="prod", kind="Pod", **details) -> Defect:
    return Defect.create(
        type=defect_type,
        severity=Severity.CRITICAL,
        kind=kind,
        namespace=namespace,
        name=name,
        message=f"{defect_type} on {name}",
        details=details,
    )


def node_defect(defect_type: str, name="ip-10-0-1-5", **details) -> Defect:
    return Defect.create(
        type=defect_type,
        severity=Severity.WARNING,
        kind="Node",
        namespace=None,
        name=name,
        message=f"{defect_type} on {name}",
        source=Source.NODE_AGENT,
        node=name,
        details=details,
    )


@pytest.mark.parametrize("defect_type", sorted(TEMPLATES))
def test_every_template_returns_the_three_part_contract(defect_type):
    d = node_defect(defect_type) if defect_type in NODE_TYPES else cluster_defect(defect_type)
    root_cause, remediation, commands = TEMPLATES[defect_type](d)

    assert isinstance(root_cause, str) and root_cause.strip()
    assert isinstance(remediation, list) and remediation
    assert all(isinstance(step, str) and step.strip() for step in remediation)
    assert isinstance(commands, list)
    assert all(isinstance(c, str) for c in commands)


@pytest.mark.parametrize("defect_type", sorted(TEMPLATES))
def test_no_template_raises_on_an_empty_details_dict(defect_type):
    """A rule that stops populating a details key must not break its template."""
    d = node_defect(defect_type) if defect_type in NODE_TYPES else cluster_defect(defect_type)
    d.details = {}

    root_cause, remediation, _ = TEMPLATES[defect_type](d)
    assert root_cause.strip()
    assert remediation


@pytest.mark.parametrize("defect_type", sorted(NODE_TYPES))
def test_node_templates_never_render_a_namespace(defect_type):
    """Node findings have namespace=None; a template using d.namespace prints 'None'.

    Hardcoded namespaces (kube-system, k8s-defect-bot) are fine and expected --
    what must never appear is an interpolated one.
    """
    _, _, commands = TEMPLATES[defect_type](node_defect(defect_type))

    assert not [c for c in commands if "None" in c]


@pytest.mark.parametrize("defect_type", POD_SCOPED_TYPES)
def test_pod_scoped_commands_carry_the_right_namespace_and_name(defect_type):
    d = cluster_defect(defect_type, name="checkout-7d4f8c9b5", namespace="prod", container="api")
    _, _, commands = TEMPLATES[defect_type](d)

    targeted = [c for c in commands if d.name in c]
    assert targeted, "no command actually names the pod"
    for command in targeted:
        assert f"-n {d.namespace}" in command, command


def test_crashloopbackoff_uses_previous_logs_for_the_named_container():
    """--previous is the whole point after a restart: the live container is new."""
    d = cluster_defect("crashloopbackoff", container="api", restart_count=7)
    _, _, commands = TEMPLATES["crashloopbackoff"](d)

    logs = next(c for c in commands if c.startswith("kubectl logs "))
    assert "-c api" in logs
    assert "--previous" in logs


def test_oomkilled_reports_the_memory_limit_and_falls_back_when_unset():
    with_limit, _, _ = TEMPLATES["oomkilled"](
        cluster_defect("oomkilled", container="api", memory_limit="256Mi")
    )
    assert "256Mi" in with_limit

    without_limit, _, _ = TEMPLATES["oomkilled"](cluster_defect("oomkilled", container="api"))
    assert "not set" in without_limit
    assert "None" not in without_limit


def test_deprecated_apis_names_the_replacement_version():
    d = cluster_defect(
        "deprecated_apis",
        name="web",
        kind="Ingress",
        deprecated_api_version="extensions/v1beta1",
        replacement="networking.k8s.io/v1",
    )
    _, remediation, commands = TEMPLATES["deprecated_apis"](d)

    assert any("networking.k8s.io/v1" in step for step in remediation)
    convert = next(c for c in commands if c.startswith("kubectl-convert"))
    assert "networking.k8s.io/v1" in convert


def test_generate_solution_populates_the_defect_in_place(settings):
    d = cluster_defect("crashloopbackoff", container="api", restart_count=7)
    returned = generate_solution(d, settings)

    assert returned is d
    assert d.root_cause and d.remediation and d.commands
    assert d.llm_enriched is False


def test_generate_solution_falls_back_for_an_unknown_type(settings):
    """The path a new rule takes before someone writes its template."""
    d = cluster_defect("brand_new_rule")
    generate_solution(d, settings)

    assert d.root_cause == d.message
    assert len(d.remediation) == 1
    assert d.commands == []


def test_enrich_batch_returns_the_provider_count(settings, monkeypatch):
    monkeypatch.setattr(llm, "enrich", lambda defects, s: len(defects))
    defects = [cluster_defect("oomkilled"), cluster_defect("oomkilled", name="web-2")]

    assert enrich_batch(defects, settings) == 2


def test_enrich_batch_swallows_a_failing_enrichment(settings, monkeypatch):
    """Enrichment is an add-on: its failure must not cost us the heuristic answer."""
    def explode(_defects, _settings):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm, "enrich", explode)
    d = generate_solution(cluster_defect("oomkilled", container="api"), settings)
    heuristic_root_cause = d.root_cause

    assert enrich_batch([d], settings) == 0
    assert d.root_cause == heuristic_root_cause
    assert d.llm_enriched is False
