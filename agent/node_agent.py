"""DaemonSet entrypoint: runs node-local checks and reports them to the collector.

One of these runs on every node. It holds no state, needs no Kubernetes API
access (its ServiceAccount is deliberately permission-less), and talks to
exactly one endpoint: POST /api/agent/report on the collector Service.

Run in-cluster:  python -m agent.node_agent
Run locally:     NODE_NAME=$(hostname) COLLECTOR_URL=http://localhost:8080 \
                 HOST_ROOT=/ HOST_PROC=/proc python -m agent.node_agent --once
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request

from agent.checks import run_all_checks
from app.config import get_settings
from app.models import NodeReport

logger = logging.getLogger("k8s-defect-bot.agent")

AGENT_VERSION = "0.3.0"

_shutdown = False


def _handle_signal(signum, _frame) -> None:
    global _shutdown
    logger.info("received signal %s, shutting down after the current cycle", signum)
    _shutdown = True


def build_report(settings) -> NodeReport:
    findings, checks_run = run_all_checks(settings)
    node = settings.node_name or os.getenv("NODE_NAME") or socket.gethostname()
    return NodeReport(
        node=node,
        agent_version=AGENT_VERSION,
        checks_run=checks_run,
        findings=findings,
    )


def send_report(settings, report: NodeReport) -> bool:
    url = settings.collector_url.rstrip("/") + "/api/agent/report"
    body = report.model_dump_json().encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    if settings.agent_token:
        request.add_header("Authorization", f"Bearer {settings.agent_token}")

    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            skew = payload.get("clock_skew_seconds")
            logger.info(
                "reported %d finding(s) from %d check(s)%s",
                len(report.findings),
                report.checks_run,
                f", collector clock skew {skew}s" if skew else "",
            )
            return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code in (401, 403):
            logger.error(
                "collector rejected the report (HTTP %s). Check that AGENT_TOKEN "
                "matches the collector's: %s", exc.code, detail,
            )
        else:
            logger.error("collector returned HTTP %s: %s", exc.code, detail)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.error("could not reach collector at %s: %s", url, exc)
    return False


def run_once(settings) -> bool:
    started = time.monotonic()
    report = build_report(settings)
    ok = send_report(settings, report)
    logger.debug("cycle finished in %.2fs", time.monotonic() - started)
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="k8s-defect-bot node agent")
    parser.add_argument(
        "--once", action="store_true", help="run a single check cycle and exit"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    node = settings.node_name or os.getenv("NODE_NAME") or socket.gethostname()
    logger.info(
        "starting node agent v%s on node=%s (interval=%ss, checks=%d, collector=%s)",
        AGENT_VERSION,
        node,
        settings.agent_interval_seconds,
        len(settings.enabled_node_checks_set),
        settings.collector_url,
    )
    if not settings.agent_token:
        logger.warning(
            "AGENT_TOKEN is not set -- reports will be sent unauthenticated. "
            "Set one on both the agent and the collector for anything beyond a test cluster."
        )

    if args.once:
        return 0 if run_once(settings) else 1

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    consecutive_failures = 0
    while not _shutdown:
        try:
            if run_once(settings):
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures in (5, 25, 100):
                    logger.error(
                        "%d consecutive failed reports -- the collector may be down "
                        "or the Service name wrong", consecutive_failures,
                    )
        except Exception:
            # A single bad cycle must never take the agent down; the DaemonSet
            # restarting in a loop would hide the very node problem it looks for.
            logger.exception("check cycle failed")

        # Sleep in short slices so SIGTERM is honoured promptly.
        deadline = time.monotonic() + settings.agent_interval_seconds
        while not _shutdown and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    logger.info("node agent stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
