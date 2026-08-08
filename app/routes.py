"""HTTP surface, split by who is allowed to reach it.

  public_router  -- health probes, the login page, and the node-agent intake
                    (which authenticates with its own shared token, not a user)
  router         -- the dashboard and the JSON API; every route requires a
                    signed-in user once the user registry is populated
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agent.checks import REGISTRY as CHECK_REGISTRY
from analyzer.solution_engine import generate_solution
from app.auth import auth, current_user, require_admin, require_user, _client_ip
from app.config import get_settings
from app.models import (
    Defect,
    NodeAgentStatus,
    NodeReport,
    Role,
    ScanSummary,
    Severity,
    Source,
    User,
)
from app.notify import notifier
from app.store import store
from scraper.cluster_scanner import perform_scan
from scraper.rules import REGISTRY as RULE_REGISTRY

logger = logging.getLogger("k8s-defect-bot.routes")

public_router = APIRouter()
router = APIRouter(dependencies=[Depends(require_user)])
templates = Jinja2Templates(directory="ui/templates")

# Rule and check names double as defect types -- used to populate the type filter.
DEFECT_TYPES = sorted(
    set(RULE_REGISTRY) | set(CHECK_REGISTRY) | {"node_clock_skew", "node_agent_unreachable"}
)


def _table_context(defects: list[Defect], namespace="", severity="", dtype="", source=""):
    return {
        "defects": defects,
        "namespaces": store.namespaces(),
        "types": DEFECT_TYPES,
        "filter_namespace": namespace,
        "filter_severity": severity,
        "filter_type": dtype,
        "filter_source": source,
    }


def _reporting_agents() -> set[str]:
    return {s.node for s in store.node_statuses() if not s.stale}


# ==========================================================================
# Public: health probes
# ==========================================================================


@public_router.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})


@public_router.get("/readyz")
async def readyz(request: Request):
    if getattr(request.app.state, "scan_loop_running", False):
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "not-ready"}, status_code=503)


# ==========================================================================
# Public: sign in / sign out
# ==========================================================================


@public_router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/", error: Optional[str] = None):
    settings = get_settings()
    if not auth.enabled:
        return RedirectResponse("/", status_code=303)
    if await current_user(request, request.headers.get("authorization")):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next": next or "/",
            "error": error,
            "cluster_name": settings.cluster_name,
            "config_error": auth.load_error,
        },
        status_code=200,
    )


@public_router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    settings = get_settings()
    if not auth.enabled:
        return RedirectResponse("/", status_code=303)

    ip = _client_ip(request)
    email = (email or "").strip().lower()

    locked_for = auth.locked_out(email, ip)
    if locked_for:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "next": next,
                "error": f"Too many failed attempts. Try again in {locked_for}s.",
                "cluster_name": settings.cluster_name,
                "config_error": auth.load_error,
            },
            status_code=429,
        )

    user = auth.authenticate(email, password, ip=ip)
    if user is None:
        logger.warning("failed sign-in for %r from %s", email, ip or "unknown")
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                # Deliberately generic: never reveal which addresses exist.
                "next": next,
                "error": "Incorrect email or password.",
                "cluster_name": settings.cluster_name,
                "config_error": auth.load_error,
            },
            status_code=401,
        )

    token = auth.create_session(user, ip=ip)
    # Only ever redirect within this app -- an attacker-supplied absolute URL
    # would turn the login form into an open redirect.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",  # blocks cross-site POSTs from carrying the session
        secure=settings.session_cookie_secure,
        path="/",
    )
    logger.info("%s signed in from %s", user.email, ip or "unknown")
    return response


@public_router.get("/logout")
@public_router.post("/logout")
async def logout(request: Request):
    settings = get_settings()
    auth.destroy_session(request.cookies.get(settings.session_cookie_name))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return response


# ==========================================================================
# Public: node-agent intake (its own shared token, not a user session)
# ==========================================================================


def _authorize_agent(authorization: Optional[str]) -> None:
    expected = get_settings().agent_token
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = authorization.split(" ", 1)[1]
    # Constant-time compare so a wrong token can't be recovered by timing the endpoint.
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="invalid agent token")


@public_router.post("/api/agent/report")
async def api_agent_report(
    report: NodeReport,
    authorization: Optional[str] = Header(default=None),
):
    """Intake for DaemonSet agents. Converts findings into defects and stores them."""
    _authorize_agent(authorization)
    settings = get_settings()

    received_at = datetime.now(timezone.utc)
    reported_at = report.reported_at
    if reported_at.tzinfo is None:
        reported_at = reported_at.replace(tzinfo=timezone.utc)
    skew = (received_at - reported_at).total_seconds()

    defects: list[Defect] = []
    for finding in report.findings:
        defect = Defect.create(
            type=finding.check,
            severity=finding.severity,
            kind="Node",
            name=report.node,
            component=finding.check,
            source=Source.NODE_AGENT,
            node=report.node,
            message=finding.message,
            details=finding.details,
        )
        generate_solution(defect, settings)
        defects.append(defect)

    # The agent can't detect its own clock skew -- only the collector sees both clocks.
    if abs(skew) > settings.clock_skew_warn_seconds:
        drift = Defect.create(
            type="node_clock_skew",
            severity=Severity.CRITICAL if abs(skew) > 300 else Severity.WARNING,
            kind="Node",
            name=report.node,
            component="clock",
            source=Source.NODE_AGENT,
            node=report.node,
            message=f"Node clock differs from the collector by {round(skew, 1)}s",
            details={"skew_seconds": round(skew, 1), "agent_time": reported_at.isoformat()},
        )
        generate_solution(drift, settings)
        defects.append(drift)

    store.record_node_report(report, defects, clock_skew_seconds=skew)
    try:
        notifier.process(store.list(), store.summary(), _reporting_agents())
    except Exception:
        logger.exception("notification pass failed after a node report")

    return JSONResponse(
        {
            "status": "accepted",
            "node": report.node,
            "findings_recorded": len(defects),
            "clock_skew_seconds": round(skew, 1),
        }
    )


# ==========================================================================
# Dashboard (authenticated)
# ==========================================================================


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: Optional[User] = Depends(current_user)):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "summary": store.summary(),
            "namespaces": store.namespaces(),
            "scan_interval_seconds": settings.scan_interval_seconds,
            "llm_provider": settings.llm_provider,
            "cluster_name": settings.cluster_name,
            "user": user,
            "auth_enabled": auth.enabled,
            "notify_enabled": notifier.enabled,
        },
    )


@router.get("/ui/summary", response_class=HTMLResponse)
async def ui_summary(request: Request):
    return templates.TemplateResponse(request, "_summary_bar.html", {"summary": store.summary()})


@router.get("/ui/nodes", response_class=HTMLResponse)
async def ui_nodes(request: Request):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "_node_agents.html",
        {
            "agents": store.node_statuses(settings.clock_skew_warn_seconds),
            "ttl_seconds": settings.node_report_ttl_seconds,
        },
    )


@router.get("/ui/notifications", response_class=HTMLResponse)
async def ui_notifications(request: Request, user: Optional[User] = Depends(current_user)):
    return templates.TemplateResponse(
        request,
        "_notifications.html",
        {
            "status": notifier.status(),
            "recipients": [u.public() for u in auth.recipients()],
            "user": user,
            "is_admin": user is None or user.role == Role.ADMIN,
        },
    )


@router.get("/ui/defects", response_class=HTMLResponse)
async def ui_defects(
    request: Request,
    namespace: Optional[str] = None,
    severity: Optional[str] = None,
    type: Optional[str] = None,
    source: Optional[str] = None,
):
    defects = store.list(
        namespace=namespace or None,
        severity=severity or None,
        defect_type=type or None,
        source=source or None,
    )
    return templates.TemplateResponse(
        request,
        "_defect_table.html",
        _table_context(defects, namespace or "", severity or "", type or "", source or ""),
    )


@router.get("/ui/defects/{defect_id}", response_class=HTMLResponse)
async def ui_defect_detail(request: Request, defect_id: str):
    defect = store.get(defect_id)
    if not defect:
        return HTMLResponse(
            "<div class='p-4 text-sm text-red-400'>Defect no longer present "
            "(it may have been resolved by a later scan).</div>",
            status_code=404,
        )
    return templates.TemplateResponse(request, "_defect_drawer.html", {"defect": defect})


@router.post("/ui/scan", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def ui_scan(request: Request):
    await perform_scan()
    body = templates.TemplateResponse(
        request, "_defect_table.html", _table_context(store.list())
    ).body.decode("utf-8")
    summary_html = templates.get_template("_summary_bar.html").render(
        {"summary": store.summary(), "oob": True}
    )
    return HTMLResponse(summary_html + body)


# ==========================================================================
# JSON API (authenticated)
# ==========================================================================


@router.get("/api/me")
async def api_me(user: Optional[User] = Depends(current_user)):
    if user is None:
        return JSONResponse({"authenticated": False, "auth_enabled": False})
    return JSONResponse({"authenticated": True, "auth_enabled": True, **user.public()})


@router.get("/api/users", dependencies=[Depends(require_admin)])
async def api_users():
    """Roster only -- password and token hashes are never serialised."""
    return JSONResponse([u.public() for u in sorted(auth.users.values(), key=lambda u: u.email)])


@router.get("/api/summary", response_model=ScanSummary)
async def api_summary():
    return store.summary()


@router.get("/api/defects", response_model=list[Defect])
async def api_list_defects(
    namespace: Optional[str] = None,
    severity: Optional[str] = None,
    type: Optional[str] = None,
    source: Optional[str] = None,
    node: Optional[str] = None,
):
    return store.list(
        namespace=namespace, severity=severity, defect_type=type, source=source, node=node
    )


@router.get("/api/defects/{defect_id}", response_model=Defect)
async def api_get_defect(defect_id: str):
    defect = store.get(defect_id)
    if not defect:
        raise HTTPException(status_code=404, detail="defect not found")
    return defect


@router.get("/api/nodes", response_model=list[NodeAgentStatus])
async def api_nodes():
    settings = get_settings()
    return store.node_statuses(settings.clock_skew_warn_seconds)


@router.post("/api/scan", response_model=ScanSummary, dependencies=[Depends(require_admin)])
async def api_scan():
    return await perform_scan()


@router.get("/api/notifications", dependencies=[Depends(require_admin)])
async def api_notifications():
    return JSONResponse(
        {
            **notifier.status(),
            "recipients": [u.public() for u in auth.recipients()],
        }
    )


@router.post("/api/notifications/test")
async def api_notifications_test(user: Optional[User] = Depends(require_admin)):
    """Sends a sample notification to the signed-in admin, to prove the SMTP path."""
    if user is None:
        raise HTTPException(
            status_code=400,
            detail="no signed-in user to mail: configure the user registry first",
        )
    if not notifier.enabled:
        raise HTTPException(
            status_code=400,
            detail=notifier.misconfiguration() or "notifications are disabled (NOTIFY_ENABLED)",
        )
    delivery = notifier.send_test(user)
    if not delivery.ok:
        raise HTTPException(status_code=502, detail=f"SMTP send failed: {delivery.error}")
    return JSONResponse({"status": "sent", "to": delivery.to, "subject": delivery.subject})


@router.post("/api/notifications/flush", dependencies=[Depends(require_admin)])
async def api_notifications_flush():
    sent = notifier.flush_digests(force=True)
    return JSONResponse({"status": "flushed", "emails_sent": sent})
