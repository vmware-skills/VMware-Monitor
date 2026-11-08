"""Background reads and notifications must leave truthful, minimal audit rows."""

from types import SimpleNamespace

import pytest
from vmware_policy.policy import PolicyDenied, PolicyResult

from vmware_monitor.scanner import scheduler as sched


@pytest.fixture
def cycle(monkeypatch, tmp_path):
    from vmware_policy.audit import reset_engine
    from vmware_policy.policy import reset_policy_engine as reset_policy

    monkeypatch.setenv("OPS_HOME", str(tmp_path))
    reset_engine()
    reset_policy()
    config = SimpleNamespace(
        scanner=SimpleNamespace(),
        notify=SimpleNamespace(
            log_file="unused", webhook_url="https://hook.example/token-secret", webhook_timeout=1
        ),
    )
    conn = SimpleNamespace(list_targets=lambda: ["vc"], connect=lambda name: object())
    monkeypatch.setattr(sched, "ScanLogger", lambda *a: SimpleNamespace(log_issue=lambda i: None))
    monkeypatch.setattr(sched, "scan_alarms", lambda si: [])
    monkeypatch.setattr(sched, "scan_logs", lambda *a: [])
    monkeypatch.setattr(sched, "_host_log_issues", lambda *a: [])
    monkeypatch.setattr(sched.WebhookNotifier, "send", lambda self, issues: True)
    return config, conn, sched.HostLogCursors()


def rows():
    from vmware_policy.audit import get_engine

    return get_engine().query(skill="monitor", limit=100)


def test_successful_cycle_is_audited_on_daemon_surface(cycle):
    sched._run_scan(*cycle)
    found = rows()
    assert len(found) == 1
    assert found[0]["tool"] == "daemon_scan"
    assert found[0]["surface"] == "daemon" and found[0]["status"] == "ok"


def test_partial_scan_is_not_audited_as_success(cycle, monkeypatch):
    monkeypatch.setattr(sched, "scan_logs", lambda *a: (_ for _ in ()).throw(ValueError("secret")))
    sched._run_scan(*cycle)
    assert rows()[0]["status"] == "error"
    assert "secret" not in str(rows())


def test_repeated_failure_is_recorded_when_outcome_changes(cycle, monkeypatch):
    cycle[1].connect = lambda name: (_ for _ in ()).throw(ConnectionError("private-secret"))
    sched._run_scan(*cycle)
    sched._run_scan(*cycle)
    assert len(rows()) == 3  # read failure once; successful webhook send each cycle
    assert "private-secret" not in str(rows())
    cycle[1].connect = lambda name: object()
    sched._run_scan(*cycle)
    assert any(r["tool"] == "daemon_scan" and r["status"] == "ok" for r in rows())


def test_interrupted_cycle_is_recorded(cycle):
    cycle[1].connect = lambda name: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        sched._run_scan(*cycle)
    assert rows()[0]["status"] == "interrupted"


@pytest.mark.parametrize("sent", [True, False])
def test_webhook_has_own_audit_row_without_endpoint_or_payload(cycle, monkeypatch, sent):
    monkeypatch.setattr(
        sched,
        "scan_alarms",
        lambda si: [
            {"severity": "critical", "source": "alarm", "entity": "host", "message": "private log"}
        ],
    )
    monkeypatch.setattr(sched.WebhookNotifier, "send", lambda self, issues: sent)
    sched._run_scan(*cycle)
    webhook = next(r for r in rows() if r["tool"] == "webhook_send")
    assert webhook["status"] == ("ok" if sent else "error")
    assert "token-secret" not in str(rows()) and "private log" not in str(rows())


def test_policy_denial_prevents_connection(cycle, monkeypatch):
    calls = []
    cycle[1].connect = lambda name: calls.append(name)
    monkeypatch.setattr(
        "vmware_monitor.scanner.audit.guard",
        lambda *a, **k: (_ for _ in ()).throw(PolicyDenied(PolicyResult(False, "stop", "denied"))),
    )
    sched._run_scan(*cycle)
    assert calls == []
    assert rows()[0]["status"] == "denied"


def test_real_environment_policy_blocks_before_network(cycle, tmp_path, monkeypatch):
    import vmware_policy.environment as env
    from vmware_policy import set_environment_resolver
    from vmware_policy.policy import reset_policy_engine

    monkeypatch.setattr(env, "_resolvers", {})
    set_environment_resolver(lambda name: "production" if name == "vc" else "", skill="monitor")
    (tmp_path / "rules.yaml").write_text(
        "deny:\n  - name: no-background-reads\n    operations: [daemon_scan]\n"
        "    environments: [production]\n    reason: paused\n",
        encoding="utf-8",
    )
    reset_policy_engine()
    calls = []
    cycle[1].connect = lambda name: calls.append(name)
    sched._run_scan(*cycle)
    assert calls == []
    read = next(r for r in rows() if r["tool"] == "daemon_scan")
    assert read["status"] == "denied" and "no-background-reads" in str(read["result"])


def test_webhook_transport_errors_do_not_log_secret_urls(caplog, monkeypatch):
    import httpx

    from vmware_monitor.notify.webhook import WebhookNotifier

    def fail(*a, **k):
        raise httpx.ConnectError("https://hook.example/private-token")

    monkeypatch.setattr(httpx, "post", fail)
    assert not WebhookNotifier("https://hook.example/private-token", timeout=1).send(
        [{"severity": "critical", "message": "secret incident"}]
    )
    assert "private-token" not in caplog.text and "secret incident" not in caplog.text


def test_webhook_denial_blocks_send_and_preserves_read_audit(cycle, tmp_path, monkeypatch):
    from vmware_policy.policy import reset_policy_engine

    (tmp_path / "rules.yaml").write_text(
        "deny:\n  - name: no-egress\n    operations: [webhook_send]\n    reason: paused\n",
        encoding="utf-8",
    )
    reset_policy_engine()
    monkeypatch.setattr(
        sched,
        "scan_alarms",
        lambda si: [{"severity": "critical", "source": "alarm", "message": "incident"}],
    )
    sent = []
    monkeypatch.setattr(sched.WebhookNotifier, "send", lambda *a: sent.append(True))
    sched._run_scan(*cycle)
    assert sent == []
    assert {(r["tool"], r["status"]) for r in rows()} == {
        ("daemon_scan", "ok"),
        ("webhook_send", "denied"),
    }


def test_webhook_interruption_is_audited_and_propagated(cycle, monkeypatch):
    monkeypatch.setattr(
        sched,
        "scan_alarms",
        lambda si: [{"severity": "critical", "source": "alarm", "message": "incident"}],
    )

    def stop(*args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(sched.WebhookNotifier, "send", stop)
    with pytest.raises(KeyboardInterrupt):
        sched._run_scan(*cycle)
    assert {(r["tool"], r["status"]) for r in rows()} == {
        ("daemon_scan", "ok"),
        ("webhook_send", "interrupted"),
    }


def test_failed_notification_is_not_summarised_as_sent(cycle, monkeypatch, caplog):
    import logging
    monkeypatch.setattr(sched, "scan_alarms", lambda si: [
        {"severity": "critical", "source": "alarm", "message": "incident"}])
    monkeypatch.setattr(sched.WebhookNotifier, "send", lambda *a: False)
    with caplog.at_level(logging.INFO, logger=sched.logger.name):
        sched._run_scan(*cycle)
    assert "INCOMPLETE" in caplog.text and "0 sent to the webhook" in caplog.text
