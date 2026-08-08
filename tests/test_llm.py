"""Claude enrichment tests -- no real model calls; the CLI is faked."""
from __future__ import annotations

import json
import subprocess

import pytest

from analyzer import llm
from app.config import Settings
from app.models import Defect, Severity


def defect(name="web-1", severity=Severity.CRITICAL, logs="Traceback: KeyError 'DB_HOST'") -> Defect:
    d = Defect.create(
        type="crashloopbackoff",
        severity=severity,
        kind="Pod",
        namespace="default",
        name=name,
        component="app",
        message="Container 'app' is crash-looping (7 restarts)",
        details={"container": "app", "restart_count": 7},
        events=["[Warning] BackOff: restarting failed container (x7)"],
        logs=logs,
    )
    d.root_cause = "Container is repeatedly crashing after startup."
    d.remediation = ["Check the container logs."]
    return d


class FakeProvider:
    name = "fake"

    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def analyse(self, d):
        self.calls.append(d.id)
        if self.raises:
            raise self.raises
        return self.result


# -- prompt / parsing ------------------------------------------------------


def test_prompt_carries_the_evidence():
    prompt = llm.build_prompt(defect())
    assert "crashloopbackoff" in prompt
    assert "Pod/default/web-1" in prompt
    assert "KeyError 'DB_HOST'" in prompt
    assert "restart_count" in prompt
    assert "JSON object" in prompt


def test_prompt_truncates_a_huge_log_tail():
    prompt = llm.build_prompt(defect(logs="x" * 50_000))
    assert len(prompt) < 12_000


def test_parse_plain_json():
    parsed = llm.parse_response(
        '{"root_cause": "Missing env var DB_HOST.", "remediation": ["Set DB_HOST"], '
        '"confidence": "high"}'
    )
    assert parsed.root_cause == "Missing env var DB_HOST."
    assert parsed.remediation == ["Set DB_HOST"]
    assert parsed.confidence == "high"


def test_parse_strips_markdown_fences():
    parsed = llm.parse_response(
        '```json\n{"root_cause": "Bad config.", "remediation": [], "confidence": "low"}\n```'
    )
    assert parsed.root_cause == "Bad config."


def test_parse_recovers_json_embedded_in_prose():
    parsed = llm.parse_response(
        'Here is my analysis:\n{"root_cause": "OOM.", "remediation": ["Raise the limit"], '
        '"confidence": "medium"}\nHope that helps.'
    )
    assert parsed.root_cause == "OOM."


def test_parse_falls_back_to_prose():
    parsed = llm.parse_response("The pod cannot reach its database.")
    assert parsed.root_cause == "The pod cannot reach its database."
    assert parsed.confidence == "low"


def test_parse_rejects_empty_and_contentless_answers():
    assert llm.parse_response("") is None
    assert llm.parse_response("   ") is None
    assert llm.parse_response('{"remediation": []}') is None


def test_parse_normalises_a_bogus_confidence():
    parsed = llm.parse_response('{"root_cause": "x", "remediation": [], "confidence": "certain"}')
    assert parsed.confidence == "medium"


# -- batch behaviour -------------------------------------------------------


def test_enrich_is_a_no_op_when_disabled():
    settings = Settings(llm_provider="none")
    d = defect()
    assert llm.enrich([d], settings) == 0
    assert d.llm_enriched is False


def test_enrich_appends_without_destroying_the_heuristic():
    settings = Settings(llm_provider="claude_cli")
    provider = FakeProvider(llm.Enrichment("DB_HOST is unset.", ["Add the env var"], "high"))
    d = defect()

    assert llm.enrich([d], settings, provider=provider) == 1
    assert "Container is repeatedly crashing" in d.root_cause  # heuristic preserved
    assert "DB_HOST is unset." in d.root_cause
    assert d.remediation[0] == "Check the container logs."
    assert "[Claude] Add the env var" in d.remediation
    assert d.llm_enriched is True


def test_enrich_survives_a_provider_exception():
    settings = Settings(llm_provider="claude_cli")
    provider = FakeProvider(raises=RuntimeError("network down"))
    d = defect()

    assert llm.enrich([d], settings, provider=provider) == 0
    assert d.root_cause == "Container is repeatedly crashing after startup."
    assert d.llm_enriched is False


def test_enrich_respects_the_per_scan_cap():
    settings = Settings(llm_provider="claude_cli", llm_max_defects_per_scan=2)
    provider = FakeProvider(llm.Enrichment("cause", [], "medium"))
    defects = [defect(f"web-{i}") for i in range(5)]

    assert llm.enrich(defects, settings, provider=provider) == 2
    assert len(provider.calls) == 2


def test_enrich_prioritises_criticals_with_logs():
    settings = Settings(llm_provider="claude_cli", llm_max_defects_per_scan=1)
    provider = FakeProvider(llm.Enrichment("cause", [], "medium"))
    warning = defect("warn-1", severity=Severity.WARNING)
    critical = defect("crit-1", severity=Severity.CRITICAL)

    llm.enrich([warning, critical], settings, provider=provider)
    assert provider.calls == [critical.id]


# -- CLI provider ----------------------------------------------------------


def _cli_settings(**overrides) -> Settings:
    return Settings(llm_provider="claude_cli", claude_cli_path="claude", **overrides)


def test_cli_provider_builds_a_safe_command(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    cmd = llm.ClaudeCLIProvider(_cli_settings(claude_model="sonnet"))._command()

    assert "-p" in cmd and "--output-format" in cmd and "json" in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert "--disallowed-tools" in cmd
    for tool in ("Bash", "Read", "Write", "WebFetch"):
        assert tool in cmd, "the analysis call must not be able to touch the host"


def test_cli_provider_requires_the_binary(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: None)
    monkeypatch.setattr(llm.os.path, "exists", lambda _: False)
    with pytest.raises(llm.LLMUnavailable):
        llm.ClaudeCLIProvider(_cli_settings())


def test_cli_provider_parses_a_successful_run(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    payload = {
        "type": "result",
        "is_error": False,
        "total_cost_usd": 0.01,
        "result": '{"root_cause": "Missing secret.", "remediation": ["Create it"], '
                  '"confidence": "high"}',
    }
    monkeypatch.setattr(
        llm.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(payload), ""),
    )
    result = llm.ClaudeCLIProvider(_cli_settings()).analyse(defect())
    assert result.root_cause == "Missing secret."


def test_cli_provider_handles_a_timeout(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")

    def timeout(*_a, **_k):
        raise subprocess.TimeoutExpired("claude", 120)

    monkeypatch.setattr(llm.subprocess, "run", timeout)
    assert llm.ClaudeCLIProvider(_cli_settings()).analyse(defect()) is None


def test_cli_provider_handles_a_nonzero_exit(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        llm.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "not logged in"),
    )
    assert llm.ClaudeCLIProvider(_cli_settings()).analyse(defect()) is None


def test_cli_provider_handles_an_error_payload(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    payload = {"is_error": True, "result": "rate limit reached"}
    monkeypatch.setattr(
        llm.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(payload), ""),
    )
    assert llm.ClaudeCLIProvider(_cli_settings()).analyse(defect()) is None


def test_api_provider_needs_a_key():
    with pytest.raises(llm.LLMUnavailable):
        llm.AnthropicAPIProvider(Settings(llm_provider="anthropic_api", anthropic_api_key=None))


def test_get_provider_returns_none_for_an_unknown_name():
    assert llm.get_provider(Settings(llm_provider="gpt")) is None


def test_get_provider_returns_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: None)
    monkeypatch.setattr(llm.os.path, "exists", lambda _: False)
    assert llm.get_provider(Settings(llm_provider="claude_cli")) is None
