"""Regression — sessions separate people from vCenter's own service accounts.

Found 2026-09-14 on a lab vCenter 8.0.3: ``activity sessions`` listed 33
sessions, and 24 of them were vCenter talking to itself — solution users named
``vpxd-extension-<machine id>``, ``vpxd-svcs-user-<id>``, ``sps-<id>``,
``vsphere-webclient-<id>``, ``vmware-vsm-<id>``. The nine that answer "who is
logged in" were buried, and nothing said what client each session was:
six ``Administrator`` sessions from 192.168.60.210 were Aria's adapter
(``VMware vim-java``), seven from 127.0.0.1 were the vSphere Client's vAPI
proxy, one was this tool (``pyvmomi``). ``extensionSession`` was False on
every one of them, so it cannot tell a service apart; the SSO solution-user
naming convention — a service name followed by the machine UUID — can.

Service sessions are folded by default into ``service_sessions`` (count per
account) with a note; ``include_service`` lists them. Every row now carries
``user_agent``, ``call_count`` and ``kind``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vmware_monitor.ops import activity

_ID = "c995e30c-d0ee-4851-8cc9-c403ac02ba3e"


def _s(key, user, agent, ip="127.0.0.1", calls=1):
    return SimpleNamespace(
        key=key,
        userName=user,
        fullName="",
        loginTime="2026-09-14 13:00",
        lastActiveTime=f"2026-09-14 13:{int(key[1:]):02d}",
        ipAddress=ip,
        userAgent=agent,
        callCount=calls,
        extensionSession=False,
    )


def _si(sessions, current="k9"):
    mgr = SimpleNamespace(currentSession=SimpleNamespace(key=current), sessionList=sessions)
    return SimpleNamespace(RetrieveContent=lambda: SimpleNamespace(sessionManager=mgr))


_LAB = [
    _s("k1", f"VSPHERE.LOCAL\\vpxd-extension-{_ID}", "govmomi/0.33.0"),
    _s("k2", f"VSPHERE.LOCAL\\vpxd-extension-{_ID}", "VMware-client/8.0.3"),
    _s("k3", f"VSPHERE.LOCAL\\vpxd-svcs-user-{_ID}", "VMware vim-java 1.0"),
    _s("k4", f"VSPHERE.LOCAL\\sps-{_ID}", "VMware vim-java 1.0"),
    _s(
        "k5", "VSPHERE.LOCAL\\Administrator", "VMware vim-java 1.0", ip="192.168.60.210", calls=4211
    ),
    _s("k6", "VSPHERE.LOCAL\\Administrator", "vAPI/2.100.0 Java/1.8.0_401", calls=12),
    _s(
        "k9",
        "VSPHERE.LOCAL\\Administrator",
        "pyvmomi 9.0.0.0 OSS Python/3.12.5",
        ip="192.168.60.96",
    ),
]


@pytest.mark.unit
def test_service_accounts_are_folded_and_counted():
    out = activity.get_active_sessions(_si(_LAB))
    assert [r["kind"] for r in out["items"]] == ["user", "user", "user"]
    assert out["total"] == 3
    assert out["service_sessions"] == {
        "VSPHERE.LOCAL\\sps-<machine id>": 1,
        "VSPHERE.LOCAL\\vpxd-extension-<machine id>": 2,
        "VSPHERE.LOCAL\\vpxd-svcs-user-<machine id>": 1,
    }
    assert "4" in out["service_note"] and "include_service" in out["service_note"]


@pytest.mark.unit
def test_rows_say_what_client_each_session_is():
    rows = {r["ip_address"]: r for r in activity.get_active_sessions(_si(_LAB))["items"]}
    aria = rows["192.168.60.210"]
    assert aria["user_agent"] == "VMware vim-java 1.0" and aria["call_count"] == 4211
    assert rows["192.168.60.96"]["current"] is True


@pytest.mark.unit
def test_include_service_lists_every_session():
    out = activity.get_active_sessions(_si(_LAB), include_service=True)
    assert out["total"] == 7 and out["service_sessions"] == {}
    assert "service_note" not in out
    assert sum(1 for r in out["items"] if r["kind"] == "service") == 4


@pytest.mark.unit
def test_a_person_whose_name_merely_contains_a_dash_is_not_a_service():
    people = [_s("k1", "CORP\\jean-luc", "vSphere Client"), _s("k2", "ops-team-01", "govc")]
    out = activity.get_active_sessions(_si(people))
    assert [r["kind"] for r in out["items"]] == ["user", "user"]


@pytest.mark.unit
def test_the_mcp_tool_and_cli_expose_the_switch(monkeypatch):
    import inspect

    from rich.console import Console
    from typer.testing import CliRunner

    from vmware_monitor import cli, cli_observability
    from vmware_monitor.mcp_server import server

    assert "include_service" in inspect.signature(server.active_sessions).parameters
    assert "include_service:" in inspect.getdoc(server.active_sessions)

    monkeypatch.setattr(
        cli_observability, "get_connection", lambda target, config: (_si(_LAB), None, "vc")
    )
    monkeypatch.setattr(cli_observability, "console", Console(width=250, color_system=None))
    result = CliRunner().invoke(cli.app, ["activity", "sessions"])
    assert result.exit_code == 0, result.output
    assert "vim-java" in result.output and "4 service" in result.output
    listed = CliRunner().invoke(cli.app, ["activity", "sessions", "--include-service"])
    assert "vpxd-extension" in listed.output
