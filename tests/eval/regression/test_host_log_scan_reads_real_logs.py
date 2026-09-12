"""host_log_scan must read host logs, and say so when it cannot.

From v0.1.0 until 2026-09-11 this tool never read a single log line. It called
``BrowseDiagnosticLog`` on ``host.configManager.diagnosticSystem`` — a
``vim.host.DiagnosticSystem``, which has no such method. Every call raised
``AttributeError``, ``except Exception: continue`` swallowed it, and the tool
(and the daemon's host-log pass) reported "checked, found nothing" on every
host, every time. Verified live the same day on vCenter 8.0.3 and a standalone
ESXi 8.0.3: the old call raised AttributeError on both.

The method lives on ``content.diagnosticManager``. Through vCenter it takes
``host=`` (live: hostd, lineEnd 46400); on a standalone ESXi passing ``host=``
is rejected with ``vmodl.fault.InvalidRequest`` and the call must omit it.

The old test passed because its fake diagnostic system HAD the method — the one
shape reality never produces (CLAUDE.md 形态 #3). Its replacement took
``**kwargs``, so a misspelled keyword would have passed too. These tests use a
real ``vim.DiagnosticManager``: pyVmomi checks the parameter names and types
(see ``_diag_fakes``). And the except that hid the bug now catches only vSphere
faults and transport errors — a bug in this code raises.
"""

from __future__ import annotations

import http.client
import logging
from types import SimpleNamespace

import pytest
from pyVmomi import vim

from tests.eval.regression._diag_fakes import LogStub, diag_manager, host, numbered
from vmware_monitor.ops.investigate_host import HostNotFoundError
from vmware_monitor.scanner import log_scanner

ERROR_LINE = "2026-09-11 error: lost access to volume"


def test_the_method_is_on_the_diagnostic_manager_not_the_host_system():
    assert not hasattr(vim.host.DiagnosticSystem, "BrowseDiagnosticLog"), (
        "pyVmomi changed: the host's diagnosticSystem now has BrowseDiagnosticLog"
    )
    assert hasattr(vim.DiagnosticManager, "BrowseDiagnosticLog")


def _si(monkeypatch, stub, api_type="VirtualCenter", hosts=None, diag=None):
    content = SimpleNamespace(
        diagnosticManager=diag if diag is not None else diag_manager(stub),
        about=SimpleNamespace(apiType=api_type),
    )
    rows = hosts if hosts is not None else [(host("host-10", stub), {"name": "esx-01"})]
    monkeypatch.setattr(log_scanner, "_collect", lambda *a, **k: rows)
    return SimpleNamespace(RetrieveContent=lambda: content)


def _scan(monkeypatch, stub, **kw):
    api_type = kw.pop("api_type", "VirtualCenter")
    si = _si(monkeypatch, stub, api_type=api_type, hosts=kw.pop("hosts", None))
    return log_scanner.scan_host_logs(si, log_keys=("hostd",), **kw)


def test_through_vcenter_the_host_is_named(monkeypatch):
    stub = LogStub({"hostd": [ERROR_LINE]})
    ref = host("host-10", stub)
    out = _scan(monkeypatch, stub, hosts=[(ref, {"name": "esx-01"})])
    assert out["items"], "an error line in the log produced no finding"
    assert stub.calls and all(c["host"] is ref for c in stub.calls)
    assert out["logs_unavailable"] == []


def test_on_a_standalone_esxi_no_host_argument_is_sent(monkeypatch):
    stub = LogStub({"hostd": ["error: disk degraded"]})
    out = _scan(monkeypatch, stub, api_type="HostAgent")
    assert out["items"]
    assert stub.calls and all(c["host"] is None for c in stub.calls), (
        "a standalone ESXi rejects host= with vmodl.fault.InvalidRequest"
    )


def test_the_read_is_the_tail_of_the_log_and_names_its_length(monkeypatch):
    """1200 lines, lines=500: read 701..1200 — an error at line 10 is not in it."""
    log = numbered(1200)
    log[9] = "error: line 10 is outside the window"
    log[1099] = "error: line 1100 is inside the window"
    stub = LogStub({"hostd": log})
    out = _scan(monkeypatch, stub, lines=500)

    (read,) = stub.reads()
    assert read["start"] == 701, f"read from line {read['start']}, not the last 500"
    assert read["lines"] == 500, "the read did not say how many lines it wants"
    assert [i["message"] for i in out["items"]] == [
        "[VSPHERE_HOST_LOG]esx-01: error: line 1100 is inside the window[/VSPHERE_HOST_LOG]"
    ]


def test_a_log_that_cannot_be_read_is_reported_not_empty(monkeypatch):
    stub = LogStub({}, fail={"hostd": vim.fault.NoPermission(privilegeId="Global.Diagnostics")})
    out = _scan(monkeypatch, stub)
    assert out["items"] == []
    assert out["logs_unavailable"], "a failed read came back looking like a clean log"
    row = out["logs_unavailable"][0]
    assert row["host"] == "esx-01" and row["log"] == "hostd"
    assert "Global.Diagnostics" in row["reason"]


@pytest.mark.parametrize("exc", [
    ConnectionResetError("peer reset"),
    http.client.HTTPException("503 Service Unavailable"),
    vim.fault.CannotAccessFile(file="/var/run/log/vpxa.log"),
])
def test_faults_and_transport_errors_are_unreadable_rows(monkeypatch, exc):
    stub = LogStub({}, fail={"hostd": exc})
    out = _scan(monkeypatch, stub)
    assert [r["log"] for r in out["logs_unavailable"]] == ["hostd"]
    assert type(exc).__name__ in out["logs_unavailable"][0]["reason"]


def test_a_bug_in_this_code_raises_instead_of_becoming_an_unreadable_row(monkeypatch):
    """The original bug, replayed: the method called on an object that lacks it.

    AttributeError used to be swallowed into "this log could not be read" —
    which is how the scan read nothing for years without anyone noticing.
    """
    stub = LogStub({"hostd": [ERROR_LINE]})
    si = _si(monkeypatch, stub, diag=vim.host.DiagnosticSystem("diagsys-1", stub))
    with pytest.raises(AttributeError):
        log_scanner.scan_host_logs(si, log_keys=("hostd",))


def test_a_misnamed_keyword_raises(monkeypatch):
    """pyVmomi rejects an unknown keyword with TypeError; it must reach the caller."""
    stub = LogStub({"hostd": numbered(10)})
    diag = diag_manager(stub)
    original = log_scanner._read_log

    def _misnamed(diag_mgr, log_key, scope, lines, prior):
        diag_mgr.BrowseDiagnosticLog(key=log_key, start=1, line=lines, **scope)
        return original(diag_mgr, log_key, scope, lines, prior)

    monkeypatch.setattr(log_scanner, "_read_log", _misnamed)
    si = _si(monkeypatch, stub, diag=diag)
    with pytest.raises(TypeError, match="line"):
        log_scanner.scan_host_logs(si, log_keys=("hostd",))


def test_an_unreadable_log_is_logged_at_warning_with_host_key_and_type(monkeypatch, caplog):
    """The reason row says "the server log has a warning" — the server runs at INFO."""
    stub = LogStub({}, fail={"hostd": vim.fault.CannotAccessFile(file="/x")})
    with caplog.at_level(logging.INFO, logger=log_scanner._log.name):
        _scan(monkeypatch, stub)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the failure is only logged below the level the server prints"
    text = warnings[0].getMessage()
    assert "esx-01" in text and "hostd" in text and "CannotAccessFile" in text


def test_host_names_are_sanitized_like_every_other_vsphere_text(monkeypatch):
    stub = LogStub({"hostd": [ERROR_LINE]}, fail={"vmkernel": vim.fault.NoPermission()})
    hostile = "esx-01\x1b[2J‮"
    si = _si(monkeypatch, stub, hosts=[(host("host-10", stub), {"name": hostile})])
    out = log_scanner.scan_host_logs(si, log_keys=("hostd", "vmkernel"))
    texts = [i["message"] for i in out["items"]] + [i["entity"] for i in out["items"]]
    texts += [r["host"] for r in out["logs_unavailable"]]
    assert texts and all("\x1b" not in t and "‮" not in t for t in texts)
    assert all("esx-01" in t for t in texts)


def test_a_host_name_that_matches_nothing_says_so(monkeypatch):
    """A typo must not come back in the exact shape of a clean host."""
    stub = LogStub({"hostd": [ERROR_LINE]})
    with pytest.raises(HostNotFoundError, match="list_esxi_hosts"):
        _scan(monkeypatch, stub, host_name="esx-99")
    assert stub.calls == [], "no host matched, yet a log was read"


def test_the_mcp_tool_returns_the_teaching_error_for_an_unknown_host(monkeypatch):
    import vmware_monitor.mcp_server.server as server

    stub = LogStub({"hostd": [ERROR_LINE]})
    si = _si(monkeypatch, stub)
    monkeypatch.setattr(server, "_get_connection", lambda target=None: si)
    out = server.host_log_scan(host_name="esx-99")
    assert "items" not in out
    assert "list_esxi_hosts" in out["error"] and "esx-99" in out["error"]


def test_lines_below_one_is_refused(monkeypatch):
    stub = LogStub({"hostd": [ERROR_LINE]})
    with pytest.raises(ValueError, match="lines"):
        _scan(monkeypatch, stub, lines=0)


# ---------------------------------------------------------------------------
# Incremental reading — scan_host_logs_since, used by the daemon only
# ---------------------------------------------------------------------------


def _since(monkeypatch, stub, positions, lines=500):
    si = _si(monkeypatch, stub)
    return log_scanner.scan_host_logs_since(si, positions, log_keys=("hostd",), lines=lines)


def test_the_second_pass_reads_only_the_new_lines(monkeypatch):
    log = numbered(1000)
    log[899] = "error: already reported in the first pass"
    stub = LogStub({"hostd": log})
    first = _since(monkeypatch, stub, {})
    assert len(first.items) == 1
    assert first.positions == {("esx-01", "hostd"): 1000}

    log.extend(["routine", "error: new since the first pass", "routine"])
    second = _since(monkeypatch, stub, first.positions)

    assert stub.reads()[-1]["start"] == 1001 and stub.reads()[-1]["lines"] == 3
    assert [i["message"] for i in second.items] == [
        "[VSPHERE_HOST_LOG]esx-01: error: new since the first pass[/VSPHERE_HOST_LOG]"
    ]
    assert second.positions == {("esx-01", "hostd"): 1003}
    assert second.lines_skipped == ()


def test_an_unchanged_log_is_not_read_again(monkeypatch):
    stub = LogStub({"hostd": numbered(40)})
    first = _since(monkeypatch, stub, {})
    reads_before = len(stub.reads())
    second = _since(monkeypatch, stub, first.positions)
    assert len(stub.reads()) == reads_before, "no new line, yet the log was read again"
    assert second.items == () and second.positions == first.positions


def test_a_rotated_log_is_read_from_its_tail_and_the_gap_is_said(monkeypatch):
    stub = LogStub({"hostd": numbered(1000)})
    first = _since(monkeypatch, stub, {})
    stub.logs["hostd"] = numbered(20, text="error: after rotation")
    second = _since(monkeypatch, stub, first.positions)

    assert stub.reads()[-1]["start"] == 1, "a rotated log was read from the old position"
    assert len(second.items) == 20
    assert second.lines_skipped == ({"host": "esx-01", "log": "hostd", "skipped": None},)
    assert second.positions == {("esx-01", "hostd"): 20}


def test_more_new_lines_than_one_read_reads_the_newest_and_counts_the_rest(monkeypatch):
    stub = LogStub({"hostd": numbered(100)})
    first = _since(monkeypatch, stub, {}, lines=50)
    stub.logs["hostd"].extend(numbered(120, first=101))
    second = _since(monkeypatch, stub, first.positions, lines=50)

    assert stub.reads()[-1]["start"] == 171 and stub.reads()[-1]["lines"] == 50
    assert second.lines_skipped == ({"host": "esx-01", "log": "hostd", "skipped": 70},)
    assert second.positions == {("esx-01", "hostd"): 220}


def test_an_unreadable_log_keeps_its_position_for_the_next_pass(monkeypatch):
    stub = LogStub({"hostd": numbered(10)})
    first = _since(monkeypatch, stub, {})
    stub.fail["hostd"] = ConnectionResetError("dropped")
    second = _since(monkeypatch, stub, first.positions)
    assert second.logs_unavailable and second.positions == first.positions
