"""The daemon's host-log pass: each line once, the right lines paged, no false all-clear.

Three things this file pins, each a defect found on 2026-09-11:

* **Unread is not clear.** scan_host_logs returns ``logs_unavailable`` beside
  ``items``, but the daemon's scan cycle kept only ``items`` — so with
  NoPermission on every host it logged "Scan complete: all clear". The fix
  reached the MCP tool and not the daemon: the half-fix shape the handbook
  calls 形态 #5. A gap is an ``info`` row: enough that the cycle cannot report
  all-clear, not enough to page the webhook every interval for a log that
  never exists on a host.
* **Each line once.** Once the scanner really read logs (live: 228 matching
  lines per cycle), every 15-minute cycle re-read the last 500 lines and
  reported the same lines again. The daemon now remembers, per target, host
  and log, the last line it read, and reads only what came after.
* **Page what someone should be woken for.** 135 of those 228 lines were one
  routine hostd line. Host-log warnings go to the scan log only; host-log
  criticals, and alarm/event warnings, still page.

And a pass that raised must not end in "all clear" — it used to: the error was
logged and the summary fell through to the empty-issues branch.

These drive the real scanner over a real ``vim.DiagnosticManager`` (see
``_diag_fakes``); only the alarm and event passes are replaced.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from pyVmomi import vim

import vmware_monitor.scanner.scheduler as sched
from tests.eval.regression._diag_fakes import LogStub, diag_manager, host, numbered
from vmware_monitor.scanner import log_scanner


class _Recorder:
    def __init__(self):
        self.issues = []
        self.sent = []

    def log_issue(self, issue):
        self.issues.append(issue)

    def send(self, issues):
        self.sent.append(issues)


def _daemon(monkeypatch, stub, alarms=(), api_type="VirtualCenter"):
    """Wire _run_scan to ``stub`` for host logs; return (recorder, run-one-cycle)."""
    rec = _Recorder()
    monkeypatch.setattr(sched, "ScanLogger", lambda *a, **k: rec)
    monkeypatch.setattr(sched, "WebhookNotifier", lambda *a, **k: rec)
    monkeypatch.setattr(sched, "scan_alarms", lambda si: list(alarms))
    monkeypatch.setattr(sched, "scan_logs", lambda si, cfg: [])
    content = SimpleNamespace(diagnosticManager=diag_manager(stub),
                              about=SimpleNamespace(apiType=api_type))
    si = SimpleNamespace(RetrieveContent=lambda: content)
    rows = [(host("host-10", stub), {"name": "esx-01"})]
    monkeypatch.setattr(log_scanner, "_collect", lambda *a, **k: rows)
    conn = SimpleNamespace(list_targets=lambda: ["vc"], connect=lambda name: si)
    config = SimpleNamespace(
        scanner=SimpleNamespace(),
        notify=SimpleNamespace(log_file="unused.log", webhook_url="https://hook.example",
                               webhook_timeout=5),
    )
    cursors = sched.HostLogCursors()
    return rec, lambda: sched._run_scan(config, conn, cursors)


def _messages(issues):
    return [i["message"] for i in issues]


def test_unread_host_logs_are_reported_not_all_clear(monkeypatch, caplog):
    stub = LogStub({}, fail={k: vim.fault.NoPermission(privilegeId="Global.Diagnostics")
                             for k in ("hostd", "vmkernel", "vpxa")})
    rec, cycle = _daemon(monkeypatch, stub)
    with caplog.at_level(logging.INFO, logger=sched.logger.name):
        cycle()

    assert "all clear" not in caplog.text, "a cycle that read no host log reported all clear"
    assert "3 unreadable host log(s)" in caplog.text
    assert any("esx-01" in m and "hostd" in m for m in _messages(rec.issues))
    assert all(i["severity"] == "info" for i in rec.issues)
    assert rec.sent == [], "an unreadable log must not page anyone every interval"


def test_the_second_cycle_reports_only_lines_written_since_the_first(monkeypatch):
    logs = {k: numbered(600) for k in ("hostd", "vmkernel", "vpxa")}
    logs["hostd"][550] = "error: seen in the first cycle"
    stub = LogStub(logs)
    rec, cycle = _daemon(monkeypatch, stub)

    cycle()
    assert _messages(rec.issues) == [
        "[VSPHERE_HOST_LOG]esx-01: error: seen in the first cycle[/VSPHERE_HOST_LOG]"
    ]
    rec.issues.clear()

    cycle()
    assert rec.issues == [], "the second cycle reported a line the first already had"

    logs["vmkernel"].append("error: written between the cycles")
    cycle()
    assert _messages(rec.issues) == [
        "[VSPHERE_HOST_LOG]esx-01: error: written between the cycles[/VSPHERE_HOST_LOG]"
    ]


def test_a_rotated_log_is_read_again_from_its_tail(monkeypatch):
    logs = {k: numbered(600) for k in ("hostd", "vmkernel", "vpxa")}
    stub = LogStub(logs)
    rec, cycle = _daemon(monkeypatch, stub)
    cycle()

    logs["hostd"] = ["routine", "error: first line of interest after rotation"]
    cycle()
    assert "[VSPHERE_HOST_LOG]esx-01: error: first line of interest after rotation" \
        "[/VSPHERE_HOST_LOG]" in _messages(rec.issues)
    notes = [i for i in rec.issues if i["source"] == "host_log:skipped"]
    assert len(notes) == 1 and "rotated" in notes[0]["message"]
    assert notes[0]["severity"] == "info"


def test_an_overflow_reads_the_newest_lines_and_says_how_many_were_skipped(monkeypatch):
    logs = {k: numbered(10) for k in ("hostd", "vmkernel", "vpxa")}
    stub = LogStub(logs)
    rec, cycle = _daemon(monkeypatch, stub)
    cycle()

    logs["hostd"].extend(numbered(700, first=11))
    logs["hostd"][-1] = "error: the newest line"
    cycle()
    notes = [i for i in rec.issues if i["source"] == "host_log:skipped"]
    assert len(notes) == 1 and "200 line(s)" in notes[0]["message"]
    assert notes[0]["severity"] == "info"
    assert "[VSPHERE_HOST_LOG]esx-01: error: the newest line[/VSPHERE_HOST_LOG]" \
        in _messages(rec.issues)
    assert rec.sent == []


def test_host_log_warnings_are_logged_but_only_criticals_page(monkeypatch):
    alarm_warning = {"severity": "warning", "source": "alarm", "message": "[host:esx-01] CPU",
                     "time": "", "entity": "esx-01"}
    logs = {k: [] for k in ("hostd", "vmkernel", "vpxa")}
    logs["hostd"] = ["warning-level: operation timeout", "kernel panic: critical fault"]
    rec, cycle = _daemon(monkeypatch, LogStub(logs), alarms=[alarm_warning])
    cycle()

    by_sev = {i["severity"]: i for i in rec.issues if i["source"] == "host_log:hostd"}
    assert set(by_sev) == {"warning", "critical"}, "both host-log lines belong in the scan log"
    (sent,) = rec.sent
    assert by_sev["critical"] in sent, "a critical host-log line was not paged"
    assert by_sev["warning"] not in sent, "a host-log warning paged the webhook"
    assert alarm_warning in sent, "alarm warnings must page exactly as before"


def test_a_failed_pass_is_never_summarised_as_all_clear(monkeypatch, caplog):
    rec, cycle = _daemon(monkeypatch, LogStub({k: [] for k in ("hostd", "vmkernel", "vpxa")}))

    def _broken(si):
        raise RuntimeError("alarm manager exploded")

    monkeypatch.setattr(sched, "scan_alarms", _broken)
    with caplog.at_level(logging.INFO, logger=sched.logger.name):
        cycle()

    assert rec.issues == []
    assert "all clear" not in caplog.text, "a cycle whose alarm pass raised said all clear"
    assert "INCOMPLETE" in caplog.text and "vc: alarm scan" in caplog.text
    summary = [r for r in caplog.records if "INCOMPLETE" in r.getMessage()]
    assert summary[0].levelno == logging.WARNING


def test_a_bug_in_the_host_log_pass_fails_the_pass_loudly(monkeypatch, caplog):
    """A programming error propagates out of the scanner; the daemon marks the
    pass failed, with the traceback, instead of an unreadable-log info row."""
    stub = LogStub({k: [] for k in ("hostd", "vmkernel", "vpxa")})
    rec, cycle = _daemon(monkeypatch, stub)

    def _buggy_window(*args):
        raise NameError("name 'tail_start' is not defined")

    monkeypatch.setattr(log_scanner, "_window", _buggy_window)
    with caplog.at_level(logging.INFO, logger=sched.logger.name):
        cycle()

    assert not [i for i in rec.issues if i["source"] == "host_log:unavailable"]
    assert "vc: host log scan" in caplog.text
    failures = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert failures and failures[0].exc_info, "the failed pass was logged without its traceback"


def test_the_scheduler_shares_one_cursor_store_across_every_cycle(monkeypatch, tmp_path):
    seen = []
    jobs = []

    class _Scheduler:
        def add_job(self, func, **kw):
            jobs.append(kw["args"])

        def start(self):
            pass

    config = SimpleNamespace(scanner=SimpleNamespace(enabled=True, interval_minutes=15),
                             notify=SimpleNamespace())
    monkeypatch.setattr(sched, "PID_FILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(sched, "load_config", lambda p=None: config)
    monkeypatch.setattr(sched, "ConnectionManager", lambda cfg: SimpleNamespace(
        list_targets=lambda: [], disconnect_all=lambda: None))
    monkeypatch.setattr(sched, "BlockingScheduler", _Scheduler)
    monkeypatch.setattr(sched, "_run_scan", lambda cfg, conn, cursors=None: seen.append(cursors))
    monkeypatch.setattr(sched.signal, "signal", lambda *a: None)

    sched.start_scheduler()

    (job_args,) = jobs
    assert isinstance(seen[0], sched.HostLogCursors)
    assert job_args[2] is seen[0], "the scheduled cycles would forget what the first one read"
