"""Entrypoint: wires up the FastAPI app and the background scan loop.

Run locally with:  python main.py       (uses your current kubeconfig context)
Run in-cluster:    the container's CMD is `python main.py`
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.auth import auth
from app.config import get_settings
from app.notify import notifier
from app.routes import public_router, router
from app.store import store
from scraper.cluster_scanner import perform_scan

logger = logging.getLogger("k8s-defect-bot")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


async def _scan_loop(app: FastAPI) -> None:
    """Scans on startup, then every scan_interval_seconds until cancelled."""
    settings = get_settings()
    while True:
        try:
            summary = await perform_scan()
            app.state.scan_loop_running = True
            logger.info(
                "scan complete in %.2fs: %d defects (%d critical, %d warning) across %d pods / %d nodes",
                summary.duration_seconds,
                summary.total_defects,
                summary.critical_count,
                summary.warning_count,
                summary.pods_scanned,
                summary.nodes_scanned,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # perform_scan already swallows per-scan errors; this catches anything
            # unexpected so a single bad pass can never kill the loop.
            logger.exception("scan loop iteration failed")

        # Housekeeping that only needs doing on the scan cadence.
        try:
            await asyncio.to_thread(notifier.flush_digests)
            auth.purge_expired()
        except Exception:
            logger.exception("post-scan housekeeping failed")

        await asyncio.sleep(settings.scan_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)
    store.configure(settings.node_report_ttl_seconds)
    auth.configure(settings)
    notifier.configure(settings)

    logger.info(
        "starting k8s-defect-bot on cluster '%s' (interval=%ss, namespaces=%s, rules=%d, llm=%s)",
        settings.cluster_name,
        settings.scan_interval_seconds,
        settings.namespace_filter_list or "all",
        len(settings.enabled_rules_set),
        settings.llm_provider,
    )

    # -- surface every misconfiguration at startup, not on first use --
    if auth.enabled:
        logger.info(
            "authentication enabled for %d user(s); %d may receive notifications",
            len(auth.users), len(auth.recipients()),
        )
    else:
        logger.warning(
            "AUTHENTICATION IS OFF -- no users are configured, so anyone who can "
            "reach this Service has full access to the dashboard and API. "
            "Mount a user registry to enable it (see usage.md)."
        )
    if auth.load_error:
        logger.error(
            "the user registry could not be loaded (%s); all sign-ins will be refused",
            auth.load_error,
        )

    if settings.notify_enabled:
        problem = notifier.misconfiguration()
        if problem:
            logger.error("notifications are enabled but not usable: %s", problem)
        else:
            logger.info(
                "email notifications on via %s:%s as %s",
                settings.smtp_host, settings.smtp_port, settings.smtp_from,
            )
            if not auth.recipients():
                logger.warning(
                    "notifications are enabled but no user has a delivery mode set, "
                    "so nothing will be sent"
                )
    if settings.llm_enabled:
        from analyzer.llm import get_provider

        if get_provider(settings) is None:
            logger.warning(
                "LLM_PROVIDER=%s could not be initialised; running heuristics only",
                settings.llm_provider,
            )
    if not settings.agent_token:
        logger.warning(
            "AGENT_TOKEN is not set -- POST /api/agent/report accepts unauthenticated "
            "reports from anything that can reach this Service."
        )

    app.state.scan_loop_running = False
    task = asyncio.create_task(_scan_loop(app), name="scan-loop")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        app.state.scan_loop_running = False
        logger.info("scan loop stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="k8s-defect-bot",
        description=(
            "Scans a Kubernetes cluster for common defects, collects node-local health "
            "from a DaemonSet agent, and suggests remediations."
        ),
        version="0.3.0",
        lifespan=lifespan,
    )

    # Static assets stay public so the login page can style itself.
    app.mount("/static", StaticFiles(directory="ui/static"), name="static")
    app.include_router(public_router)
    app.include_router(router)
    return app


app = create_app()


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=os.getenv("DEV_RELOAD", "").lower() in ("1", "true", "yes"),
    )
