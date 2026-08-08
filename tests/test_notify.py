"""Notification tests -- no real SMTP; the send path is captured."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.models import (
    Defect,
    NotifyMode,
    NotifyPrefs,
    Role,
    ScanSummary,
    Severity,
    Source,
    User,
)
from app.notify import ChangeSet, Notifier


def defect(name="web-1", severity=Severity.CRITICAL, dtype="crashloopbackoff",
           namespace="prod", source=Source.CLUSTER, component=None) -> Defect:
    d = Defect.create(
        type=dtype, severity=severity, kind="Pod", namespace=namespace,
        name=name, component=component or name, source=source,
        message=f"{dtype} on {name}",
    )
    d.root_cause = "Because of a thing.\nSecond line."
    d.remediation = ["Do the first thing."]
    d.commands = [f"kubectl describe pod {name} -n {namespace}"]
    return d


def node_defect(node="ip-10-0-1-5", dtype="node_disk_usage") -> Defect:
    d = Defect.create(
        type=dtype, severity=Severity.CRITICAL, kind="Node", name=node,
        component=dtype, source=Source.NODE_AGENT, node=node,
        message="Filesystem / is 93% full",
    )
    d.root_cause = "Disk nearly full."
    return d


def user(email="a@b.com", **notify) -> User:
    return User(email=email, name=email.split("@")[0], role=Role.ADMIN,
                password_hash="x", notify=NotifyPrefs(**notify))


def settings(**overrides) -> Settings:
    base = dict(
        notify_enabled=True, smtp_host="smtp.test", smtp_from="bot@test",
        cluster_name="prod-eks", notify_baseline_first_scan=False,
        auth_users_file=None, llm_provider="none",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def notifier(monkeypatch):
    """A notifier whose SMTP layer is replaced by an outbox."""
    n = Notifier(settings())
    n.outbox = []
    monkeypatch.setattr(n, "_smtp_send", lambda message: n.outbox.append(message))
    return n


# -- change detection ------------------------------------------------------


def test_first_pass_is_a_silent_baseline():
    n = Notifier(settings(notify_baseline_first_scan=True))
    sent = []
    n._smtp_send = lambda m: sent.append(m)

    assert n.process([defect(), defect("web-2")], ScanSummary(), recipients=[user()]) == 0
    assert sent == []


def test_defects_present_at_baseline_are_not_announced_later(notifier):
    existing = defect()
    notifier._baselined = False
    notifier.settings = settings(notify_baseline_first_scan=True)
    notifier.process([existing], ScanSummary(), recipients=[user()])
    notifier.process([existing], ScanSummary(), recipients=[user()])
    assert notifier.outbox == []


def test_a_new_defect_is_mailed(notifier):
    recipients = [user()]
    notifier.process([defect("web-1")], ScanSummary(), recipients=recipients)
    notifier.outbox.clear()

    sent = notifier.process(
        [defect("web-1"), defect("web-2")], ScanSummary(), recipients=recipients
    )
    assert sent == 1
    assert len(notifier.outbox) == 1
    assert "web-2" in notifier.outbox[0].get_body("plain").get_content()


def test_an_unchanged_defect_is_not_re_announced(notifier):
    recipients = [user()]
    d = defect()
    notifier.process([d], ScanSummary(), recipients=recipients)
    notifier.outbox.clear()

    assert notifier.process([d], ScanSummary(), recipients=recipients) == 0
    assert notifier.outbox == []


def test_a_resolved_defect_is_reported(notifier):
    recipients = [user()]
    notifier.process([defect()], ScanSummary(), recipients=recipients)
    notifier.outbox.clear()

    notifier.process([], ScanSummary(), recipients=recipients)
    body = notifier.outbox[0].get_body("plain").get_content()
    assert "RESOLVED" in body


def test_a_flapping_defect_is_not_re_announced_within_the_cooldown(notifier):
    recipients = [user()]
    d = defect()
    notifier.process([d], ScanSummary(), recipients=recipients)
    notifier.process([], ScanSummary(), recipients=recipients)   # resolves
    notifier.outbox.clear()

    notifier.process([d], ScanSummary(), recipients=recipients)  # comes back
    combined = " ".join(m.get_body("plain").get_content() for m in notifier.outbox)
    assert "NEW" not in combined


def test_agents_going_offline_and_recovering(notifier):
    recipients = [user()]
    notifier.process([], ScanSummary(), reporting_agents={"n1", "n2"}, recipients=recipients)
    notifier.outbox.clear()

    notifier.process([], ScanSummary(), reporting_agents={"n1"}, recipients=recipients)
    assert "n2 stopped reporting" in notifier.outbox[0].get_body("plain").get_content()

    notifier.outbox.clear()
    notifier.process([], ScanSummary(), reporting_agents={"n1", "n2"}, recipients=recipients)
    assert "reporting again" in notifier.outbox[0].get_body("plain").get_content()


def test_a_scan_error_is_mailed(notifier):
    notifier.process(
        [], ScanSummary(error="Cluster appears unreachable"), recipients=[user()]
    )
    body = notifier.outbox[0].get_body("plain").get_content()
    assert "SCAN PROBLEM" in body
    assert "unreachable" in body
    assert "scan failed" in notifier.outbox[0]["Subject"]


def test_a_persistent_scan_error_is_mailed_once(notifier):
    """Regression: a cluster that stays down used to mail on every single pass."""
    recipients = [user()]
    summary = ScanSummary(error="Cluster appears unreachable")

    notifier.process([], summary, recipients=recipients)
    assert len(notifier.outbox) == 1

    for _ in range(5):
        notifier.process([], summary, recipients=recipients)
    assert len(notifier.outbox) == 1, "an unchanged error is not news"


def test_recovery_from_a_scan_error_is_mailed(notifier):
    recipients = [user()]
    notifier.process([], ScanSummary(error="boom"), recipients=recipients)
    notifier.outbox.clear()

    notifier.process([], ScanSummary(), recipients=recipients)
    assert len(notifier.outbox) == 1
    body = notifier.outbox[0].get_body("plain").get_content()
    assert "SCAN RECOVERED" in body
    assert "scanning again" in notifier.outbox[0]["Subject"]


def test_a_changed_error_message_is_mailed_again(notifier):
    recipients = [user()]
    notifier.process([], ScanSummary(error="first problem"), recipients=recipients)
    notifier.outbox.clear()

    notifier.process([], ScanSummary(error="a different problem"), recipients=recipients)
    assert len(notifier.outbox) == 1


# -- per-user filtering ----------------------------------------------------


def test_min_severity_filters_per_user(notifier):
    strict = user("strict@b.com", min_severity=Severity.CRITICAL)
    loose = user("loose@b.com", min_severity=Severity.WARNING)
    notifier.settings = settings(notify_min_severity="warning")

    notifier.process([], ScanSummary(), recipients=[strict, loose])
    notifier.outbox.clear()

    notifier.process(
        [defect("w", severity=Severity.WARNING)], ScanSummary(), recipients=[strict, loose]
    )
    assert [m["To"] for m in notifier.outbox] == ["loose@b.com"]


def test_the_cluster_floor_overrides_a_looser_user_preference(notifier):
    loose = user("loose@b.com", min_severity=Severity.WARNING)
    notifier.settings = settings(notify_min_severity="critical")

    notifier.process([], ScanSummary(), recipients=[loose])
    notifier.outbox.clear()
    notifier.process([defect("w", severity=Severity.WARNING)], ScanSummary(), recipients=[loose])
    assert notifier.outbox == []


def test_muted_types_are_skipped(notifier):
    u = user(muted_types=["missing_resource_limits"])
    notifier.process([], ScanSummary(), recipients=[u])
    notifier.outbox.clear()

    notifier.process(
        [defect("p", dtype="missing_resource_limits")], ScanSummary(), recipients=[u]
    )
    assert notifier.outbox == []


def test_namespace_scope_is_respected(notifier):
    u = user(namespaces=["prod"])
    notifier.process([], ScanSummary(), recipients=[u])
    notifier.outbox.clear()

    notifier.process(
        [defect("a", namespace="staging"), defect("b", namespace="prod")],
        ScanSummary(), recipients=[u],
    )
    body = notifier.outbox[0].get_body("plain").get_content()
    assert "b" in body and "staging" not in body


def test_node_findings_bypass_the_namespace_scope(notifier):
    u = user(namespaces=["prod"])
    notifier.process([], ScanSummary(), recipients=[u])
    notifier.outbox.clear()

    notifier.process([node_defect()], ScanSummary(), recipients=[u])
    assert "node_disk_usage" in notifier.outbox[0].get_body("plain").get_content()


def test_node_findings_can_be_opted_out_of(notifier):
    u = user(include_node_findings=False)
    notifier.process([], ScanSummary(), recipients=[u])
    notifier.outbox.clear()

    notifier.process([node_defect()], ScanSummary(), recipients=[u])
    assert notifier.outbox == []


def test_resolved_can_be_opted_out_of(notifier):
    u = user(include_resolved=False)
    notifier.process([defect()], ScanSummary(), recipients=[u])
    notifier.outbox.clear()

    assert notifier.process([], ScanSummary(), recipients=[u]) == 0


def test_mode_off_recipients_get_nothing(notifier):
    quiet = user("quiet@b.com", mode=NotifyMode.OFF)
    loud = user("loud@b.com")
    notifier.process([], ScanSummary(), recipients=[quiet, loud])
    notifier.outbox.clear()

    notifier.process([defect()], ScanSummary(), recipients=[quiet, loud])
    assert [m["To"] for m in notifier.outbox] == ["loud@b.com"]


# -- digest ----------------------------------------------------------------


def test_digest_defers_then_flushes(notifier):
    u = user(mode=NotifyMode.DIGEST)
    notifier.process([], ScanSummary(), recipients=[u])
    notifier.outbox.clear()

    notifier.process([defect("a")], ScanSummary(), recipients=[u])
    notifier.process([defect("a"), defect("b")], ScanSummary(), recipients=[u])
    assert notifier.outbox == [], "digest recipients wait for the flush"

    assert notifier.flush_digests(recipients=[u], force=True) == 1
    body = notifier.outbox[0].get_body("plain").get_content()
    assert "a" in body and "b" in body
    assert "digest" in notifier.outbox[0]["Subject"]


def test_digest_flush_is_a_no_op_when_nothing_is_pending(notifier):
    assert notifier.flush_digests(recipients=[user()], force=True) == 0


def test_digest_waits_for_the_interval(notifier):
    u = user(mode=NotifyMode.DIGEST)
    notifier.settings = settings(notify_digest_interval_seconds=3600)
    notifier.process([], ScanSummary(), recipients=[u])
    notifier.process([defect()], ScanSummary(), recipients=[u])
    assert notifier.flush_digests(recipients=[u]) == 0

    notifier._last_digest_flush -= timedelta(seconds=4000)
    assert notifier.flush_digests(recipients=[u]) == 1


# -- rate limiting and failures -------------------------------------------


def test_hourly_cap_suppresses_further_mail(notifier):
    u = user()
    notifier.settings = settings(notify_max_emails_per_hour=2)
    notifier.process([], ScanSummary(), recipients=[u])
    notifier.outbox.clear()

    for i in range(5):
        notifier.process([defect(f"web-{i}")], ScanSummary(), recipients=[u])

    assert len(notifier.outbox) == 2
    assert notifier.suppressed == 3


def test_an_smtp_failure_is_recorded_not_raised():
    n = Notifier(settings())

    def boom(_message):
        raise OSError("connection refused")

    n._smtp_send = boom
    n.process([], ScanSummary(), recipients=[user()])
    n.process([defect()], ScanSummary(), recipients=[user()])

    assert n.history[0].ok is False
    assert "connection refused" in n.history[0].error


def test_disabled_notifications_send_nothing(notifier):
    notifier.settings = settings(notify_enabled=False)
    assert notifier.process([defect()], ScanSummary(), recipients=[user()]) == 0
    assert notifier.outbox == []


def test_missing_smtp_host_is_reported_as_misconfiguration():
    n = Notifier(settings(smtp_host=None))
    assert n.enabled is False
    assert "SMTP_HOST" in n.misconfiguration()


# -- rendering -------------------------------------------------------------


def test_rendered_mail_has_both_parts_and_useful_content(notifier):
    changes = ChangeSet(new=[defect("checkout-1")], agents_lost=["ip-10-0-3-12"])
    subject, text, html = notifier.render(user(), changes)

    assert "prod-eks" in subject
    assert "1 new critical" in subject
    for body in (text, html):
        assert "checkout-1" in body
        assert "Because of a thing." in body
        assert "Do the first thing." in body
        assert "ip-10-0-3-12" in body
    assert "<div" in html and "<div" not in text


def test_long_lists_are_truncated(notifier):
    notifier.settings = settings(notify_max_defects_per_email=3)
    changes = ChangeSet(new=[defect(f"pod-{i}") for i in range(10)])
    _, text, html = notifier.render(user(), changes)
    assert "and 7 more" in text
    assert "and 7 more" in html


def test_dashboard_link_appears_only_when_configured(notifier):
    changes = ChangeSet(new=[defect()])
    _, text, _ = notifier.render(user(), changes)
    assert "Dashboard:" not in text

    notifier.settings = settings(dashboard_url="https://bot.example.com/")
    _, text, html = notifier.render(user(), changes)
    assert "https://bot.example.com" in text
    assert 'href="https://bot.example.com"' in html


def test_html_escapes_injected_content(notifier):
    nasty = defect("<script>alert(1)</script>")
    _, _, html = notifier.render(user(), ChangeSet(new=[nasty]))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_subject_reflects_what_happened(notifier):
    assert "1 new critical" in ChangeSet(new=[defect()]).headline("c")
    assert "1 new" in ChangeSet(new=[defect(severity=Severity.WARNING)]).headline("c")
    assert "offline" in ChangeSet(agents_lost=["n1"]).headline("c")
    assert "resolved" in ChangeSet(resolved=[defect()]).headline("c")
    assert "scan failed" in ChangeSet(scan_error="boom").headline("c")


def test_send_test_produces_a_delivery_record(notifier):
    delivery = notifier.send_test(user())
    assert delivery.ok is True
    assert "test notification" in delivery.subject
    assert "test notification" in notifier.outbox[0].get_body("plain").get_content()


def test_status_reports_configuration_and_history(notifier):
    notifier.process([defect()], ScanSummary(), recipients=[user()])
    status = notifier.status()
    assert status["enabled"] is True
    assert status["cluster"] == "prod-eks"
    assert status["tracked_defects"] == 1
    assert status["baselined"] is True


def test_changeset_merge_deduplicates():
    a = ChangeSet(new=[defect("x")], agents_lost=["n1"])
    b = ChangeSet(new=[defect("x"), defect("y")], agents_lost=["n1", "n2"])
    a.merge(b)
    assert len(a.new) == 2
    assert a.agents_lost == ["n1", "n2"]
