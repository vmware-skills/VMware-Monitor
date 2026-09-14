"""Regression — host log findings are ranked by the log's own level and grouped.

Found 2026-09-14 on a lab vCenter 8.0.3: one ``host_log_scan`` call returned 353
rows, about 150 KB. Every row was ``warning``, including informational lines
that merely contain "error" or "timed out" (``In(166) ... HTTP Connection has
timed out while waiting for further requests``), and 195 of them were the same
statistics-provider line with a different process id. Neither a person nor a
model reads that; and the tool had no CLI command at all.

* ESXi writes the level on every line (``Em/Al/Cr`` / ``Er`` / ``Wa`` / ``No`` /
  ``In`` / ``Db``). A finding's severity now follows it; ``log_level`` carries
  the raw token. A line with no level keeps the keyword rule.
* ``host_log_scan`` groups findings by pattern by default — count, hosts,
  first/last log time, one sample — and ``group=false`` returns the raw rows.
* ``vmware-monitor scan logs`` runs it from the CLI.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from vmware_monitor.scanner import log_scanner as ls


def _line(ts: str, level: str, text: str) -> str:
    return f"{ts} {level} {text}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "line,severity,level",
    [
        (
            _line(
                "2026-09-14T13:02:45.003Z",
                "Er(163)",
                "Hostd[1049638]: Detected error while retrieving stats",
            ),
            "warning",
            "Er",
        ),
        (
            _line(
                "2026-09-14T13:03:11.720Z",
                "In(166)",
                "Hostd[1049650]: HTTP Connection timeout while waiting",
            ),
            "info",
            "In",
        ),
        (
            _line("2026-09-14T13:03:11.720Z", "Wa(164)", "Hostd[1]: vm failed to consolidate"),
            "warning",
            "Wa",
        ),
        (
            _line("2026-09-14T13:03:11.720Z", "Cr(162)", "vmkernel: cpu2 lost access to volume"),
            "critical",
            "Cr",
        ),
        (
            _line("2026-09-14T13:03:11.720Z", "In(182)", "vmkernel: PSOD panic backtrace"),
            "critical",
            "In",
        ),
        ("vmkernel: lost access to volume datastore1", "warning", None),
    ],
)
def test_severity_follows_the_log_level(line, severity, level):
    [row] = ls._findings((line,), "esx-01", "hostd")
    assert row["severity"] == severity
    assert row["log_level"] == level


@pytest.mark.unit
def test_findings_are_grouped_by_pattern_across_hosts():
    stats = (
        "Er(163) Hostd[{pid}]: [Originator@6876 sub=VMkernelStatsProvider(0000004a2fe75400)] "
        "GetKernelStatValues: Detected error while retrieving stats: VSINode({n}): Not found"
    )
    rows = []
    rows += ls._findings(
        (f"2026-09-14T13:02:45.003Z {stats.format(pid=1049638, n=2648)}",), "esx-01", "hostd"
    )
    rows += ls._findings(
        (f"2026-09-14T13:03:05.002Z {stats.format(pid=1049650, n=2649)}",), "esx-01", "hostd"
    )
    rows += ls._findings(
        (f"2026-09-14T13:04:05.001Z {stats.format(pid=1049657, n=17)}",), "esx-02", "hostd"
    )
    rows += ls._findings(
        ("2026-09-14T13:05:00.000Z Cr(162) vmkernel: cpu0 lost access to volume",),
        "esx-02",
        "vmkernel",
    )
    groups = ls.group_findings(rows)
    assert [g["count"] for g in groups] == [1, 3], "critical first, then by count"
    stats_group = groups[1]
    assert stats_group["hosts"] == ["esx-01", "esx-02"]
    assert stats_group["first_seen"] == "2026-09-14T13:02:45.003Z"
    assert stats_group["last_seen"] == "2026-09-14T13:04:05.001Z"
    assert stats_group["severity"] == "warning" and stats_group["source"] == "host_log:hostd"
    assert "VSINode" in stats_group["sample"]
    assert "1049638" not in stats_group["pattern"], "process ids must not split a pattern"


def _scan_si(monkeypatch, lines):
    log = SimpleNamespace(lineEnd=len(lines), lineText=list(lines))
    diag = SimpleNamespace(BrowseDiagnosticLog=lambda key, start, **scope: log)
    content = SimpleNamespace(
        diagnosticManager=diag, about=SimpleNamespace(apiType="VirtualCenter")
    )
    monkeypatch.setattr(ls, "_collect", lambda si, t, p: [(object(), {"name": "esx-01"})])
    return SimpleNamespace(RetrieveContent=lambda: content)


@pytest.mark.unit
def test_the_mcp_tool_groups_by_default_and_can_return_raw(monkeypatch):
    from vmware_monitor.mcp_server import server

    lines = [
        f"2026-09-14T13:0{i}:00.000Z In(166) Hostd[{100 + i}]: "
        "HTTP Connection timeout while waiting"
        for i in range(5)
    ]
    si = _scan_si(monkeypatch, lines)
    monkeypatch.setattr(server, "_get_connection", lambda target: si)
    grouped = server.host_log_scan()
    assert grouped["grouped"] is True and grouped["lines_matched"] == 15  # 3 logs × 5 lines
    # One group per pattern per log: the same text in hostd and vpxa is two findings.
    assert [g["count"] for g in grouped["items"]] == [5, 5, 5]
    assert {g["source"] for g in grouped["items"]} == {
        "host_log:hostd",
        "host_log:vmkernel",
        "host_log:vpxa",
    }
    raw = server.host_log_scan(group=False)
    assert raw["grouped"] is False and len(raw["items"]) == 15
    doc = inspect.getdoc(server.host_log_scan)
    assert "group:" in doc


@pytest.mark.unit
def test_the_cli_runs_the_scan_and_prints_groups(monkeypatch):
    from rich.console import Console
    from typer.testing import CliRunner

    from vmware_monitor import cli

    lines = ["2026-09-14T13:00:00.000Z Er(163) Hostd[1]: Detected error while retrieving stats"]
    si = _scan_si(monkeypatch, lines)
    monkeypatch.setattr(cli, "_get_connection", lambda target, config: (si, None, "vc"))
    # Wide enough that the folded Sample column keeps the line on one row.
    monkeypatch.setattr(cli, "console", Console(width=400, color_system=None))
    result = CliRunner().invoke(cli.app, ["scan", "logs", "--host", "esx-01", "--lines", "50"])
    assert result.exit_code == 0, result.output
    assert "Detected error while retrieving stats" in result.output and "3" in result.output
    raw = CliRunner().invoke(cli.app, ["scan", "logs", "--raw"])
    assert raw.exit_code == 0 and raw.output.count("Detected error") == 3


@pytest.mark.unit
def test_scan_now_prints_a_message_carrying_boundary_markers(monkeypatch):
    """Scanner messages are wrapped in [VSPHERE_EVENT]…[/VSPHERE_EVENT]. Rich reads
    those as markup and raised MarkupError — unseen only because, until the
    event-name fix, the scan never found an event to print."""
    from rich.console import Console
    from typer.testing import CliRunner

    from vmware_monitor import cli
    from vmware_monitor.scanner import alarm_scanner

    msg = "[VSPHERE_EVENT]Host 192.168.60.15 in home is not responding[/VSPHERE_EVENT]"
    monkeypatch.setattr(alarm_scanner, "scan_alarms", lambda si: [])
    monkeypatch.setattr(ls, "scan_logs", lambda si, cfg: [{"severity": "critical", "message": msg}])
    cfg = SimpleNamespace(scanner=SimpleNamespace())
    monkeypatch.setattr(cli, "_get_connection", lambda target, config: (object(), cfg, "vc"))
    monkeypatch.setattr(cli, "console", Console(width=220, color_system=None))
    result = CliRunner().invoke(cli.app, ["scan", "now"])
    assert result.exit_code == 0, result.output
    assert "not responding" in result.output
