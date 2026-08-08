"""Email notifications about cluster changes.

The bot scans on a loop, so the interesting signal is not "what is wrong" -- the
dashboard already shows that -- but **what changed since last time**. This module
diffs consecutive views of the cluster and mails the delta to the people in the
user registry, at the addresses they sign in with.

What counts as a change:
  * a defect that was not there before          (new)
  * a defect that was there and now isn't       (resolved)
  * a node agent that stopped or resumed reporting
  * a scan that failed or could not reach the cluster

Three things keep this from becoming a mailbomb, which is the usual way alerting
gets switched off entirely:
  * the first pass after startup is a silent baseline -- a pod restart does not
    re-announce every pre-existing defect
  * a defect that flaps is not re-announced within the cooldown window
  * each recipient has an hourly ceiling, after which mail is dropped and counted

Sending is best-effort: an SMTP failure is logged and recorded, never raised into
the scan loop.
"""
from __future__ import annotations

import logging
import smtplib
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import Settings, get_settings
from app.models import (
    Defect,
    NotificationDelivery,
    NotifyMode,
    ScanSummary,
    Severity,
    Source,
    User,
)

logger = logging.getLogger("k8s-defect-bot.notify")

# A defect that disappears and comes back inside this window is treated as
# flapping and not re-announced.
_RENOTIFY_COOLDOWN_SECONDS = 3600

_SEVERITY_RANK = {Severity.WARNING: 1, Severity.CRITICAL: 2}


def _rank(severity: Severity) -> int:
    return _SEVERITY_RANK.get(severity, 0)


@dataclass
class ChangeSet:
    """What changed between two views of the cluster."""

    new: list[Defect] = field(default_factory=list)
    resolved: list[Defect] = field(default_factory=list)
    agents_lost: list[str] = field(default_factory=list)
    agents_recovered: list[str] = field(default_factory=list)
    # Only ever set when the scan's error state *changed*. A cluster that stays
    # unreachable is one piece of news, not one per scan.
    scan_error: Optional[str] = None
    scan_recovered: bool = False

    def is_empty(self) -> bool:
        return not (
            self.new or self.resolved or self.agents_lost
            or self.agents_recovered or self.scan_error or self.scan_recovered
        )

    def merge(self, other: "ChangeSet") -> None:
        """Accumulate into a digest, keeping defect ids unique."""
        seen_new = {d.id for d in self.new}
        self.new.extend(d for d in other.new if d.id not in seen_new)
        seen_resolved = {d.id for d in self.resolved}
        self.resolved.extend(d for d in other.resolved if d.id not in seen_resolved)
        self.agents_lost = sorted(set(self.agents_lost) | set(other.agents_lost))
        self.agents_recovered = sorted(
            set(self.agents_recovered) | set(other.agents_recovered)
        )
        self.scan_error = other.scan_error or self.scan_error
        self.scan_recovered = other.scan_recovered or self.scan_recovered

    def headline(self, cluster: str) -> str:
        criticals = sum(1 for d in self.new if d.severity == Severity.CRITICAL)
        if self.scan_error:
            return f"[{cluster}] scan failed"
        parts = []
        if self.scan_recovered:
            parts.append("scanning again")
        if criticals:
            parts.append(f"{criticals} new critical")
        elif self.new:
            parts.append(f"{len(self.new)} new")
        if self.agents_lost:
            parts.append(f"{len(self.agents_lost)} node agent(s) offline")
        if not parts and self.resolved:
            parts.append(f"{len(self.resolved)} resolved")
        if not parts:
            parts.append("cluster update")
        return f"[{cluster}] " + ", ".join(parts)


class Notifier:
    def __init__(self, settings: Optional[Settings] = None, template_dir: str = "ui/templates"):
        self.settings = settings
        self._lock = threading.Lock()
        self._known: dict[str, Defect] = {}
        self._notified_at: dict[str, datetime] = {}
        self._reporting_agents: set[str] = set()
        self._pending: dict[str, ChangeSet] = {}
        self._last_scan_error: Optional[str] = None
        self._last_digest_flush = datetime.now(timezone.utc)
        self._sent: dict[str, deque] = {}
        self._baselined = False
        self.history: deque[NotificationDelivery] = deque(maxlen=50)
        self.suppressed = 0
        self._env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # -- configuration -----------------------------------------------------

    def configure(self, settings: Settings) -> None:
        self.settings = settings
        self._baselined = False

    def _config(self) -> Settings:
        return self.settings or get_settings()

    @property
    def enabled(self) -> bool:
        settings = self._config()
        return bool(settings.notify_enabled and settings.smtp_host)

    def misconfiguration(self) -> Optional[str]:
        settings = self._config()
        if not settings.notify_enabled:
            return None
        if not settings.smtp_host:
            return "NOTIFY_ENABLED is true but SMTP_HOST is not set"
        return None

    # -- change detection --------------------------------------------------

    def _diff(
        self, defects: list[Defect], summary: ScanSummary, reporting_agents: set[str]
    ) -> ChangeSet:
        current = {d.id: d for d in defects}
        changes = ChangeSet()

        with self._lock:
            known = self._known
            now = datetime.now(timezone.utc)

            # Report the scan's error state only when it transitions. A cluster
            # that stays unreachable would otherwise mail on every single pass
            # and exhaust the hourly cap before any real change got through.
            if summary.error != self._last_scan_error:
                changes.scan_error = summary.error
                changes.scan_recovered = (
                    summary.error is None and self._last_scan_error is not None
                )
                self._last_scan_error = summary.error

            for defect_id, defect in current.items():
                if defect_id in known:
                    continue
                last = self._notified_at.get(defect_id)
                if last and (now - last).total_seconds() < _RENOTIFY_COOLDOWN_SECONDS:
                    continue  # flapping -- already announced recently
                changes.new.append(defect)

            for defect_id, defect in known.items():
                if defect_id not in current:
                    changes.resolved.append(defect)

            changes.agents_lost = sorted(self._reporting_agents - reporting_agents)
            changes.agents_recovered = sorted(reporting_agents - self._reporting_agents)

            self._known = current
            self._reporting_agents = set(reporting_agents)
            for defect in changes.new:
                self._notified_at[defect.id] = now
            # Keep the cooldown map from growing without bound.
            cutoff = now - timedelta(seconds=_RENOTIFY_COOLDOWN_SECONDS * 2)
            self._notified_at = {
                k: v for k, v in self._notified_at.items() if v > cutoff
            }

        return changes

    # -- per-user filtering ------------------------------------------------

    def _floor(self) -> Severity:
        raw = (self._config().notify_min_severity or "critical").lower()
        return Severity.WARNING if raw == "warning" else Severity.CRITICAL

    def relevant_to(self, user: User, defect: Defect) -> bool:
        prefs = user.notify
        threshold = max(_rank(self._floor()), _rank(prefs.min_severity))
        if _rank(defect.severity) < threshold:
            return False
        if defect.type in prefs.muted_types:
            return False
        if defect.source == Source.NODE_AGENT:
            return prefs.include_node_findings
        if prefs.namespaces and defect.namespace not in prefs.namespaces:
            return False
        return True

    def _for_user(self, user: User, changes: ChangeSet) -> ChangeSet:
        subset = ChangeSet(
            new=[d for d in changes.new if self.relevant_to(user, d)],
            agents_lost=changes.agents_lost if user.notify.include_node_findings else [],
            agents_recovered=(
                changes.agents_recovered if user.notify.include_node_findings else []
            ),
            scan_error=changes.scan_error,
            scan_recovered=changes.scan_recovered,
        )
        if user.notify.include_resolved:
            subset.resolved = [d for d in changes.resolved if self.relevant_to(user, d)]
        return subset

    # -- rate limiting -----------------------------------------------------

    def _may_send(self, email: str) -> bool:
        settings = self._config()
        now = datetime.now(timezone.utc)
        with self._lock:
            history = self._sent.setdefault(email, deque())
            while history and (now - history[0]).total_seconds() > 3600:
                history.popleft()
            if len(history) >= settings.notify_max_emails_per_hour:
                return False
            history.append(now)
            return True

    # -- entry point -------------------------------------------------------

    def process(
        self,
        defects: list[Defect],
        summary: ScanSummary,
        reporting_agents: Optional[set[str]] = None,
        recipients: Optional[list[User]] = None,
    ) -> int:
        """Diffs against the previous view and mails whoever cares. Returns mails sent."""
        if recipients is None:
            from app.auth import auth

            recipients = auth.recipients()

        changes = self._diff(defects, summary, reporting_agents or set())

        if not self._baselined:
            # First pass after startup only establishes "what normal looks like".
            self._baselined = True
            if self._config().notify_baseline_first_scan:
                logger.info(
                    "notification baseline established: %d existing defect(s), no mail sent",
                    len(defects),
                )
                return 0

        if not self.enabled:
            return 0

        sent = 0
        for user in recipients:
            # auth.recipients() already filters these out, but honour the opt-out
            # here too: this method also takes an explicitly supplied list, and a
            # person who asked for no mail must not get mail by either route.
            if user.disabled or user.notify.mode == NotifyMode.OFF:
                continue
            subset = self._for_user(user, changes)
            if subset.is_empty():
                continue
            if user.notify.mode == NotifyMode.DIGEST:
                with self._lock:
                    self._pending.setdefault(user.email, ChangeSet()).merge(subset)
                continue
            if self._deliver(user, subset):
                sent += 1

        sent += self.flush_digests(recipients)
        return sent

    def flush_digests(self, recipients: Optional[list[User]] = None, force: bool = False) -> int:
        """Sends queued digests once the interval has elapsed."""
        if not self.enabled:
            return 0
        settings = self._config()
        now = datetime.now(timezone.utc)
        with self._lock:
            elapsed = (now - self._last_digest_flush).total_seconds()
            if not force and elapsed < settings.notify_digest_interval_seconds:
                return 0
            pending = self._pending
            self._pending = {}
            self._last_digest_flush = now

        if not pending:
            return 0
        if recipients is None:
            from app.auth import auth

            recipients = auth.recipients()
        by_email = {u.email: u for u in recipients}

        sent = 0
        for email, changes in pending.items():
            user = by_email.get(email)
            if user and not changes.is_empty() and self._deliver(user, changes, digest=True):
                sent += 1
        return sent

    # -- rendering and sending --------------------------------------------

    def render(self, user: User, changes: ChangeSet, digest: bool = False) -> tuple[str, str, str]:
        settings = self._config()
        cluster = settings.cluster_name
        subject = changes.headline(cluster)
        if digest:
            subject += " (digest)"
        context = {
            "user": user,
            "changes": changes,
            "cluster": cluster,
            "digest": digest,
            "dashboard_url": settings.dashboard_url.rstrip("/"),
            "generated_at": datetime.now(timezone.utc),
            "max_listed": settings.notify_max_defects_per_email,
        }
        text = self._env.get_template("email/notification.txt").render(**context)
        html = self._env.get_template("email/notification.html").render(**context)
        return subject, text, html

    def _deliver(self, user: User, changes: ChangeSet, digest: bool = False) -> bool:
        if not self._may_send(user.email):
            with self._lock:
                self.suppressed += 1
            logger.warning(
                "hourly email cap reached for %s; dropping this notification", user.email
            )
            return False
        subject, text, html = self.render(user, changes, digest=digest)
        return self.send(user.email, subject, text, html, defect_count=len(changes.new))

    def send(
        self, to: str, subject: str, text: str, html: str, defect_count: int = 0
    ) -> bool:
        settings = self._config()
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((settings.smtp_from_name, settings.smtp_from))
        message["To"] = to
        message.set_content(text)
        message.add_alternative(html, subtype="html")

        record = NotificationDelivery(
            to=to, subject=subject, defect_count=defect_count, ok=True
        )
        try:
            self._smtp_send(message)
            logger.info("sent notification to %s: %s", to, subject)
        except Exception as exc:
            record.ok = False
            record.error = f"{type(exc).__name__}: {exc}"
            logger.error("could not send notification to %s: %s", to, record.error)
        finally:
            with self._lock:
                self.history.appendleft(record)
        return record.ok

    def _smtp_send(self, message: EmailMessage) -> None:
        settings = self._config()
        if not settings.smtp_host:
            raise RuntimeError("SMTP_HOST is not configured")

        if settings.smtp_ssl:
            client = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
            )
        else:
            client = smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
            )
        try:
            client.ehlo()
            if settings.smtp_starttls and not settings.smtp_ssl:
                client.starttls()
                client.ehlo()
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password or "")
            client.send_message(message)
        finally:
            try:
                client.quit()
            except Exception:
                client.close()

    # -- test mail ---------------------------------------------------------

    def send_test(self, user: User) -> NotificationDelivery:
        """Sends a sample notification so a recipient can prove the path works."""
        settings = self._config()
        sample = ChangeSet(
            new=[
                Defect.create(
                    type="oomkilled",
                    severity=Severity.CRITICAL,
                    kind="Pod",
                    namespace="prod",
                    name="checkout-7d4f8c9b5-x2k9p",
                    component="api",
                    message="Container 'api' was OOMKilled (exit code 137)",
                    details={"container": "api", "memory_limit": "256Mi"},
                )
            ],
            agents_lost=["ip-10-0-3-12"],
        )
        sample.new[0].root_cause = (
            "This is a test notification from k8s-defect-bot. The finding below is "
            "an example, not a real defect in your cluster."
        )
        sample.new[0].remediation = ["Confirm you received this mail.", "Nothing is wrong."]
        subject = f"[{settings.cluster_name}] test notification"
        _, text, html = self.render(user, sample)
        self.send(user.email, subject, text, html, defect_count=0)
        with self._lock:
            return self.history[0]

    def status(self) -> dict:
        settings = self._config()
        with self._lock:
            pending = {email: len(c.new) for email, c in self._pending.items()}
            history = [h.model_dump(mode="json") for h in list(self.history)[:10]]
        return {
            "enabled": self.enabled,
            "misconfiguration": self.misconfiguration(),
            "smtp_host": settings.smtp_host,
            "from": settings.smtp_from,
            "cluster": settings.cluster_name,
            "min_severity": self._floor().value,
            "max_emails_per_hour": settings.notify_max_emails_per_hour,
            "baselined": self._baselined,
            "tracked_defects": len(self._known),
            "pending_digests": pending,
            "suppressed": self.suppressed,
            "recent": history,
        }


notifier = Notifier()
