"""Claude-backed enrichment for detected defects.

Three providers, selected with LLM_PROVIDER:

  none           Heuristics only. No credentials, no network, no cost. (default)

  claude_cli     Shells out to the Claude Code CLI in headless mode
                 (`claude -p --output-format json`). The CLI uses whatever it is
                 already logged into, so a **Claude Pro or Max subscription
                 works** -- no API key and no separate API billing. This is the
                 provider to use when running the bot on your own machine, or
                 in-cluster with a CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`.

  anthropic_api  Direct Anthropic API with an API key. Independent of any
                 subscription and billed per token.

Every provider is best-effort: enrichment failures are logged and swallowed so
the heuristic root cause and remediation are always returned.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from app.config import Settings
from app.models import Defect

logger = logging.getLogger("k8s-defect-bot.llm")

SYSTEM_PROMPT = (
    "You are a Kubernetes SRE assistant embedded in a cluster monitoring tool. "
    "You analyse a single detected defect and explain what is actually wrong. "
    "You have no tools and no access to the cluster -- work only from the data given. "
    "Be concrete and specific to the evidence; if the evidence does not support a "
    "conclusion beyond the heuristic one already provided, say so plainly rather "
    "than speculating. Never invent log lines, resource names, or metrics."
)

RESPONSE_CONTRACT = (
    "Respond with a single JSON object and nothing else -- no prose before or "
    "after, no markdown code fences. Schema:\n"
    '{"root_cause": "<2-4 sentences in plain language for an on-call engineer>", '
    '"remediation": ["<concrete step>", "..."], '
    '"confidence": "high" | "medium" | "low"}\n'
    "Use at most 4 remediation steps. Give a kubectl command inside a step only "
    "when it is the specific next thing to run."
)

# Model aliases the Claude Code CLI accepts, mapped to API model IDs for the
# anthropic_api provider. Anything else is passed through as a full model ID.
_MODEL_ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5",
}

# Claude Code ships with file/shell/web tools enabled. This analysis is pure
# text-in/text-out, so deny them all: it keeps the call to one turn and makes it
# impossible for the CLI to touch the filesystem of whatever host it runs on.
_CLI_DENIED_TOOLS = [
    "Bash", "Read", "Write", "Edit", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "NotebookEdit", "TodoWrite",
]

_MAX_LOG_CHARS = 4000
_MAX_EVENTS = 8


@dataclass
class Enrichment:
    root_cause: str
    remediation: list[str]
    confidence: str = "medium"


class LLMUnavailable(RuntimeError):
    """Provider cannot run at all (missing CLI, missing key). Logged once, not per defect."""


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------


def build_prompt(defect: Defect) -> str:
    parts = [
        "A Kubernetes defect was detected. Analyse it.",
        "",
        f"Defect type:      {defect.type}",
        f"Severity:         {defect.severity.value}",
        f"Resource:         {defect.kind}/{defect.namespace or '-'}/{defect.name}",
        f"Detected by:      {defect.source.value}",
    ]
    if defect.node:
        parts.append(f"Node:             {defect.node}")
    parts += [
        f"Summary:          {defect.message}",
        "",
        "Heuristic root cause already shown to the user:",
        defect.root_cause or "(none)",
    ]
    if defect.details:
        parts += ["", "Structured details:", json.dumps(defect.details, indent=2, default=str)]
    if defect.events:
        parts += ["", "Recent Kubernetes events:"]
        parts += [f"  {e}" for e in defect.events[:_MAX_EVENTS]]
    if defect.logs:
        parts += ["", "Container log tail (most recent last):", defect.logs[-_MAX_LOG_CHARS:]]
    parts += ["", RESPONSE_CONTRACT]
    return "\n".join(parts)


def parse_response(text: str) -> Optional[Enrichment]:
    """Parse the model's JSON answer, tolerating stray fences or surrounding prose."""
    if not text or not text.strip():
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    data = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fall back to the first {...} span in the text.
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = None

    if not isinstance(data, dict):
        # The model answered in prose. Still useful -- keep it as the root cause.
        return Enrichment(root_cause=cleaned, remediation=[], confidence="low")

    root_cause = str(data.get("root_cause") or "").strip()
    if not root_cause:
        return None
    remediation = [str(s).strip() for s in (data.get("remediation") or []) if str(s).strip()]
    confidence = str(data.get("confidence") or "medium").lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"
    return Enrichment(root_cause=root_cause, remediation=remediation[:6], confidence=confidence)


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


class ClaudeCLIProvider:
    """Headless Claude Code. Works with a Claude Pro/Max subscription login."""

    name = "claude_cli"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.binary = shutil.which(settings.claude_cli_path) or settings.claude_cli_path
        if not shutil.which(settings.claude_cli_path) and not os.path.exists(self.binary):
            raise LLMUnavailable(
                f"Claude CLI '{settings.claude_cli_path}' not found on PATH. "
                "Install Claude Code and run `claude` once to log in, or set "
                "LLM_PROVIDER=none."
            )

    def _command(self) -> list[str]:
        return [
            self.binary,
            "-p",
            "--output-format", "json",
            "--model", self.settings.claude_model,
            # Replace (not append) the default Claude Code system prompt: this task
            # needs none of it, and dropping it cuts several thousand tokens per call.
            "--system-prompt", SYSTEM_PROMPT,
            "--disallowed-tools", *_CLI_DENIED_TOOLS,
        ]

    def analyse(self, defect: Defect) -> Optional[Enrichment]:
        prompt = build_prompt(defect)
        try:
            proc = subprocess.run(
                self._command(),
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.settings.claude_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "claude CLI timed out after %ss for defect %s",
                self.settings.claude_timeout_seconds, defect.id,
            )
            return None

        if proc.returncode != 0:
            logger.warning(
                "claude CLI exited %s for defect %s: %s",
                proc.returncode, defect.id, (proc.stderr or "").strip()[:400],
            )
            return None

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            logger.warning("claude CLI returned non-JSON output for defect %s", defect.id)
            return None

        if payload.get("is_error"):
            logger.warning(
                "claude CLI reported an error for defect %s: %s",
                defect.id, str(payload.get("result"))[:400],
            )
            return None

        cost = payload.get("total_cost_usd")
        if cost is not None:
            # On a subscription this is notional -- usage counts against the plan,
            # it is not separately billed. Useful for spotting a runaway loop.
            logger.debug("claude CLI enriched %s (notional cost $%.4f)", defect.id, cost)
        return parse_response(payload.get("result") or "")


class AnthropicAPIProvider:
    """Direct Anthropic API. Requires an API key -- billed separately from any subscription."""

    name = "anthropic_api"

    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.anthropic_api_key:
            raise LLMUnavailable(
                "LLM_PROVIDER=anthropic_api requires ANTHROPIC_API_KEY. "
                "If you want to use a Claude Pro subscription instead, set "
                "LLM_PROVIDER=claude_cli."
            )
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise LLMUnavailable(
                "LLM_PROVIDER=anthropic_api requires the 'anthropic' package "
                "(pip install anthropic)."
            ) from exc
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.claude_timeout_seconds,
        )
        self.model = _MODEL_ALIASES.get(settings.claude_model, settings.claude_model)

    def analyse(self, defect: Defect) -> Optional[Enrichment]:
        response = self._client.messages.create(
            model=self.model,
            # Generous: on current models max_tokens caps thinking *and* the answer.
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            # A scoped single-defect summarisation -- low effort keeps it quick.
            output_config={
                "effort": "low",
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "root_cause": {"type": "string"},
                            "remediation": {"type": "array", "items": {"type": "string"}},
                            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        },
                        "required": ["root_cause", "remediation", "confidence"],
                        "additionalProperties": False,
                    },
                },
            },
            messages=[{"role": "user", "content": build_prompt(defect)}],
        )
        if response.stop_reason == "refusal":
            logger.warning("model declined to analyse defect %s", defect.id)
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        return parse_response(text)


def get_provider(settings: Settings):
    """Returns a provider instance, or None when enrichment is off/unavailable."""
    if not settings.llm_enabled:
        return None
    provider = settings.llm_provider.strip().lower()
    try:
        if provider == "claude_cli":
            return ClaudeCLIProvider(settings)
        if provider == "anthropic_api":
            return AnthropicAPIProvider(settings)
    except LLMUnavailable as exc:
        logger.error("LLM enrichment disabled: %s", exc)
        return None
    logger.error("unknown LLM_PROVIDER '%s' -- enrichment disabled", settings.llm_provider)
    return None


# --------------------------------------------------------------------------
# Batch entry point
# --------------------------------------------------------------------------


def _priority(defect: Defect) -> tuple:
    """Worst-first: critical before warning, defects with logs before those without."""
    from app.models import Severity

    return (defect.severity != Severity.CRITICAL, defect.logs is None, defect.type)


def enrich(defects: list[Defect], settings: Settings, provider=None) -> int:
    """Enriches at most llm_max_defects_per_scan defects in place. Returns the count enriched."""
    provider = provider or get_provider(settings)
    if provider is None or not defects:
        return 0

    targets = sorted(defects, key=_priority)[: max(0, settings.llm_max_defects_per_scan)]
    if not targets:
        return 0

    def _one(defect: Defect) -> bool:
        try:
            result = provider.analyse(defect)
        except Exception:
            logger.warning("LLM enrichment failed for defect %s", defect.id, exc_info=True)
            return False
        if result is None:
            return False
        defect.root_cause = (
            f"{defect.root_cause}\n\nClaude analysis (confidence: {result.confidence}): "
            f"{result.root_cause}"
            if defect.root_cause
            else result.root_cause
        )
        if result.remediation:
            defect.remediation = list(defect.remediation) + [
                f"[Claude] {step}" for step in result.remediation
            ]
        defect.llm_enriched = True
        return True

    with ThreadPoolExecutor(max_workers=min(3, len(targets))) as pool:
        results = list(pool.map(_one, targets))

    enriched = sum(1 for r in results if r)
    logger.info("LLM enriched %d/%d defects via %s", enriched, len(targets), provider.name)
    return enriched
