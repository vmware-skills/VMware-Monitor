"""Regression — each surface names its own next step, and the terminal can read it.

Found 2026-09-15 on a lab vCenter 8.0.3. ``vmware-monitor attention`` / ``summary``
told the operator ``vmware-monitor get_alarms for detail`` — ``get_alarms`` is the
MCP tool; the CLI command is ``health alarms``, and ``vmware-monitor get_alarms``
exits with "No such command". The MCP payload had the mirror problem: its hints
named CLI commands (``vmware-monitor perf hosts / capacity datastores``) to a model
that can only call tools.

At the default 80-column terminal the Problem and Next-step columns were squeezed
until words were cut ("thin-provisi…"), which is the one column the table exists
to show.

Checked mechanically, not by reading strings (CLAUDE.md 形态 #6): every
underscore identifier in an MCP hint must be a tool in the live registry, and
every ``vmware-monitor <group> <command>`` in a CLI hint must answer ``--help``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import pytest
from rich.console import Console
from typer.testing import CliRunner

from vmware_monitor import cli_observability
from vmware_monitor.cli import app
from vmware_monitor.mcp_server import server
from vmware_monitor.ops import cluster_summary
from vmware_monitor.ops.attention_html import render_attention_html
from vmware_monitor.ops.health_html import render_cluster_health_html

runner = CliRunner()

_LAB_ISSUES = [
    {
        "severity": "critical",
        "kind": "host_down",
        "object": "192.168.60.15",
        "scope": "host",
        "cluster": "(standalone hosts)",
        "detail": "host notResponding",
    },
    {
        "severity": "critical",
        "kind": "alarm",
        "object": "192.168.60.15",
        "scope": "host",
        "cluster": "(standalone hosts)",
        "detail": "Host connection and power state",
    },
    {
        "severity": "warning",
        "kind": "capacity",
        "object": "(standalone hosts)",
        "scope": "cluster",
        "cluster": "(standalone hosts)",
        "detail": "memory at 85.7%",
    },
    {
        "severity": "warning",
        "kind": "capacity",
        "object": "datastore1",
        "scope": "datastore",
        "cluster": "(standalone hosts)",
        "detail": (
            "thin-provisioned to 215.9% of capacity (1734.0 of 803.2 GB promised) "
            "— can fill while it shows free space"
        ),
    },
    {
        "severity": "warning",
        "kind": "config",
        "object": "prod",
        "scope": "cluster",
        "cluster": "prod",
        "detail": "HA disabled on a multi-host cluster",
    },
]


def _tool_names() -> set[str]:
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names, "empty MCP registry — nothing would be checked"
    return names


def _issues() -> list[dict]:
    return [
        {**i, "drilldown": cluster_summary.mcp_drilldown(i)} for i in _LAB_ISSUES
    ]


def _summary() -> dict:
    return {
        "totals": {
            "clusters": 0,
            "hosts_total": 2,
            "hosts_connected": 2,
            "alarms": {"critical": 1, "warning": 0},
            "worst_status": "critical",
        },
        "top_issues": _issues(),
        "issues_total": len(_LAB_ISSUES),
        "clusters": [],
        "customization_hint": "hint",
    }


def _render(monkeypatch, fn, *args) -> str:
    narrow = Console(width=80, record=True, force_terminal=False, color_system=None)
    monkeypatch.setattr(cli_observability, "console", narrow)
    fn(*args)
    return narrow.export_text()


@pytest.mark.unit
def test_every_mcp_hint_names_only_registered_tools():
    tools = _tool_names()
    for issue in _issues():
        hint = issue["drilldown"]
        assert "vmware-monitor " not in hint, f"MCP hint names a CLI command: {hint!r}"
        for ident in re.findall(r"\b[a-z]+(?:_[a-z0-9]+)+\b", hint):
            assert ident in tools, f"{ident!r} in {hint!r} is not a registered MCP tool"


@pytest.mark.unit
def test_every_cli_hint_names_real_commands_and_no_tool():
    tools = _tool_names()
    seen = 0
    for issue in _LAB_ISSUES:
        hint = cluster_summary.cli_drilldown(issue)
        for ident in re.findall(r"\b[a-z]+(?:_[a-z0-9]+)+\b", hint):
            assert ident not in tools, f"CLI hint names the MCP tool {ident!r}: {hint!r}"
        for group, command in re.findall(r"vmware-monitor ([a-z-]+) ([a-z-]+)", hint):
            seen += 1
            result = runner.invoke(app, [group, command, "--help"])
            assert result.exit_code == 0, f"'vmware-monitor {group} {command}' is not a command"
    assert seen, "no CLI command was found in any hint — this test checked nothing"


@pytest.mark.unit
def test_the_summary_table_is_readable_at_80_columns(monkeypatch):
    text = _render(monkeypatch, cli_observability.render_summary_console, _summary(), 10)
    assert "get_alarms" not in text
    assert "health alarms" in text
    assert "…" not in text, "a column was cut at 80 columns:\n" + text
    tokens = set(re.split(r"[\s│┃]+", text))
    for issue in _LAB_ISSUES:
        for word in issue["detail"].split():
            assert word in tokens, f"{word!r} was split across lines:\n{text}"


@pytest.mark.unit
def test_the_attention_table_is_readable_at_80_columns(monkeypatch):
    data = {
        "targets": [],
        "top_issues": [{**i, "vcenter": "home-vcenter"} for i in _issues()],
        "issues_total": len(_LAB_ISSUES),
        "totals": {
            "vcenters": 1,
            "esxi_targets": 0,
            "clusters": 0,
            "hosts_total": 2,
            "hosts_connected": 2,
            "alarms": {"critical": 1, "warning": 0},
            "worst_status": "critical",
        },
        "unreachable": [],
        "customization_hint": "hint",
    }
    text = _render(monkeypatch, cli_observability.render_attention_console, data)
    assert "get_alarms" not in text
    assert "…" not in text, "a column was cut at 80 columns:\n" + text
    tokens = set(re.split(r"[\s│┃]+", text))
    for issue in _LAB_ISSUES:
        for word in issue["detail"].split():
            assert word in tokens, f"{word!r} was split across lines:\n{text}"


@pytest.mark.unit
def test_the_html_snapshots_written_by_the_cli_name_cli_commands():
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    summary_html = render_cluster_health_html(_summary(), "home-vcenter", now)
    attention_html = render_attention_html(
        {
            "targets": [],
            "top_issues": [{**i, "vcenter": "home-vcenter"} for i in _issues()],
            "issues_total": len(_LAB_ISSUES),
            "totals": _summary()["totals"] | {"vcenters": 1},
            "unreachable": [],
        },
        now,
    )
    for html in (summary_html, attention_html):
        assert "get_alarms" not in html
        assert "vmware-monitor health alarms" in html
