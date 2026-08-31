"""Tests for CLI error translation, daemon PID handling, audit resilience,
scheduler PID-file lifecycle, and atexit Disconnect guard (2026-06 fixes).

Fixes covered:
3.  cli.py shared error-translation decorator → red teaching line + exit 1,
    never a raw traceback.
5.  notify/audit.py — audit file I/O failure degrades to stderr warning and
    never blocks the (read-only) main operation.
6.  scanner/scheduler.py — PID file removed even when the *first* scan
    crashes; one bad scan_logger.log_issue write doesn't discard the cycle.
7.  connection.py — atexit Disconnect wrapped so a dead session never
    sprays a traceback at interpreter exit.
10. cli.py daemon stop/status — corrupt PID file handled; status uses
    os.kill(pid, 0) liveness instead of file existence.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from vmware_monitor.cli import _cli_errors, app

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── Fix 3: CLI error-translation decorator ───────────────────────────────


def test_cli_missing_config_no_traceback():
    result = runner.invoke(
        app, ["inventory", "vms", "--config", "/nonexistent/config.yaml"]
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "Config file missing" in result.output


@pytest.mark.parametrize(
    ("exc", "expected_fragment"),
    [
        (FileNotFoundError("/x/config.yaml"), "Config file missing"),
        (KeyError("VMWARE_PROD_PASSWORD"), "Missing config key or password"),
        (ConnectionError("refused"), "Connection failed"),
        (OSError("[Errno 8] nodename nor servname provided"), "Connection failed"),
    ],
)
def test_cli_errors_decorator_translates(exc, expected_fragment, capsys):
    @_cli_errors
    def cmd():
        raise exc

    with pytest.raises(typer.Exit) as ei:
        cmd()
    assert ei.value.exit_code == 1
    assert expected_fragment in capsys.readouterr().out


def test_cli_errors_decorator_vm_not_found(capsys):
    from vmware_monitor.ops.vm_info import VMNotFoundError

    @_cli_errors
    def cmd():
        raise VMNotFoundError("VM 'web-99' not found")

    with pytest.raises(typer.Exit) as ei:
        cmd()
    assert ei.value.exit_code == 1
    out = capsys.readouterr().out
    assert "web-99" in out
    assert "inventory vms" in out  # teaching hint


def test_cli_errors_decorator_vim_fault(capsys):
    from pyVmomi import vim

    @_cli_errors
    def cmd():
        raise vim.fault.NoPermission()

    with pytest.raises(typer.Exit) as ei:
        cmd()
    assert ei.value.exit_code == 1
    assert "vmware-monitor doctor" in capsys.readouterr().out


def test_cli_errors_decorator_passes_through_success():
    @_cli_errors
    def cmd():
        return 42

    assert cmd() == 42


# ── Fix 10: daemon status / stop PID-file guards ─────────────────────────


@pytest.fixture()
def fake_config_dir(monkeypatch, tmp_path):
    import vmware_monitor.cli as cli_mod

    monkeypatch.setattr(cli_mod, "CONFIG_DIR", tmp_path)
    return tmp_path


def test_daemon_status_corrupt_pid(fake_config_dir):
    (fake_config_dir / "daemon.pid").write_text("not-a-pid\n", encoding="utf-8")
    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == 0
    assert "Corrupt PID file" in result.output


def test_daemon_status_stale_pid(fake_config_dir, monkeypatch):
    (fake_config_dir / "daemon.pid").write_text("12345", encoding="utf-8")

    def _kill(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", _kill)
    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == 0
    assert "stale" in result.output.lower()


def test_daemon_status_live_pid(fake_config_dir, monkeypatch):
    (fake_config_dir / "daemon.pid").write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == 0
    assert "running" in result.output.lower()
    assert "not running" not in result.output.lower()


def test_daemon_stop_corrupt_pid_cleans_up(fake_config_dir):
    pid_file = fake_config_dir / "daemon.pid"
    pid_file.write_text("garbage", encoding="utf-8")
    result = runner.invoke(app, ["daemon", "stop"])
    assert result.exit_code == 0
    assert "Traceback" not in result.output
    assert not pid_file.exists(), "corrupt PID file must be removed"


# ── Fix 5: audit logger never blocks the main operation ──────────────────


def test_audit_logger_unwritable_path_warns_not_raises(tmp_path, capsys):
    from vmware_monitor.notify.audit import AuditLogger

    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    log_path = blocker / "sub" / "audit.log"  # mkdir/open will fail (OSError)

    audit = AuditLogger(log_file=str(log_path))  # must not raise
    audit.log(operation="list_vms", target="prod", resource="*", result="ok")

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "audit" in err.lower()


def test_audit_logger_happy_path_still_writes(tmp_path):
    from vmware_monitor.notify.audit import AuditLogger

    log_path = tmp_path / "audit.log"
    audit = AuditLogger(log_file=str(log_path))
    audit.log(operation="list_vms", target="prod", resource="*", result="ok")
    assert "list_vms" in log_path.read_text(encoding="utf-8")


# ── Fix 6: scheduler PID lifecycle + per-issue log guard ─────────────────


def _fake_app_config():
    return SimpleNamespace(
        scanner=SimpleNamespace(
            enabled=True,
            interval_minutes=1,
            log_types=("vpxd",),
            severity_threshold="warning",
            lookback_hours=1,
        ),
        notify=SimpleNamespace(
            log_file="unused.log", webhook_url="", webhook_timeout=5
        ),
        targets=(),
    )


def test_scheduler_removes_pid_file_when_first_scan_crashes(monkeypatch, tmp_path):
    import vmware_monitor.scanner.scheduler as sched

    pid_file = tmp_path / "daemon.pid"
    monkeypatch.setattr(sched, "PID_FILE", pid_file)
    monkeypatch.setattr(sched, "load_config", lambda p=None: _fake_app_config())
    monkeypatch.setattr(
        sched, "ConnectionManager", lambda cfg: SimpleNamespace(
            list_targets=lambda: [], disconnect_all=lambda: None
        )
    )

    def _boom(config, conn_mgr):
        assert pid_file.exists(), "PID file must exist while first scan runs"
        raise RuntimeError("first scan exploded")

    monkeypatch.setattr(sched, "_run_scan", _boom)

    with pytest.raises(RuntimeError, match="first scan exploded"):
        sched.start_scheduler()
    assert not pid_file.exists(), "PID file must be unlinked on crash"


def test_scheduler_registers_signal_handlers_before_pid_write():
    """Source-order pin: SIGTERM during the initial scan must hit a handler
    that already knows to clean up the PID file."""
    src = (REPO_ROOT / "vmware_monitor" / "scanner" / "scheduler.py").read_text(encoding="utf-8")
    sig_idx = src.index("signal.signal(signal.SIGTERM")
    pid_idx = src.index("PID_FILE.write_text")
    scan_idx = src.rindex("_run_scan(config, conn_mgr)")
    assert sig_idx < pid_idx < scan_idx


def test_run_scan_one_bad_log_write_does_not_discard_cycle(monkeypatch):
    import vmware_monitor.scanner.scheduler as sched

    logged: list[dict] = []

    class FakeScanLogger:
        def __init__(self, path):
            self.calls = 0

        def log_issue(self, issue):
            self.calls += 1
            if self.calls == 1:
                raise OSError("disk full")
            logged.append(issue)

    class FakeWebhook:
        def __init__(self, url, timeout):
            pass

        def send(self, issues):
            pass

    monkeypatch.setattr(sched, "ScanLogger", FakeScanLogger)
    monkeypatch.setattr(sched, "WebhookNotifier", FakeWebhook)

    def _failing_connect(name):
        raise ConnectionError(f"cannot reach {name}")

    conn_mgr = SimpleNamespace(
        list_targets=lambda: ["vc-1", "vc-2"], connect=_failing_connect
    )

    # Two connection issues; first log write fails — second must survive.
    sched._run_scan(_fake_app_config(), conn_mgr)
    assert len(logged) == 1
    assert "vc-2" in logged[0]["message"]


# ── Fix 7: atexit Disconnect guard ───────────────────────────────────────


def test_atexit_disconnect_guard_swallows_dead_session(monkeypatch):
    import atexit

    import pyVim.connect as pvc

    from vmware_monitor.connection import ConnectionManager

    registered: list = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a, **k: registered.append(fn))

    fake_si = SimpleNamespace()
    monkeypatch.setattr(pvc, "SmartConnect", lambda **kw: fake_si)

    def _dead(si):
        raise RuntimeError("session already terminated")

    monkeypatch.setattr(pvc, "Disconnect", _dead)

    target = SimpleNamespace(
        name="t1", host="h", username="u", password="p", port=443, verify_ssl=True
    )
    si = ConnectionManager._create_connection(target)
    assert si is fake_si
    assert len(registered) == 1
    registered[0]()  # must NOT raise despite Disconnect blowing up
