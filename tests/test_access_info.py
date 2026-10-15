"""D-17: get_access_info answers who the server acts as and whether a tool's privileges are held."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from vmware_monitor import cli_access
from vmware_monitor.access_requirements import REQUIREMENTS
from vmware_monitor.cli import app
from vmware_monitor.mcp_server import server

pytestmark = pytest.mark.unit


def _fake_si(granted: set[str]) -> MagicMock:
    si = MagicMock()
    si.content.sessionManager.currentSession = SimpleNamespace(
        key="s-1", userName="VSPHERE.LOCAL\\svc"
    )

    def _has(entity, sessionId, privId):  # noqa: N803 — pyVmomi's keyword names
        avail = [SimpleNamespace(privId=p, isGranted=p in granted) for p in privId]
        return [SimpleNamespace(privAvailability=avail)]

    am = si.content.authorizationManager
    am.HasPrivilegeOnEntities.side_effect = _has
    am.roleList = [SimpleNamespace(roleId=-2, name="ReadOnly")]
    am.RetrieveAllPermissions.return_value = [
        SimpleNamespace(
            principal="VSPHERE.LOCAL\\svc",
            roleId=-2,
            propagate=True,
            entity=SimpleNamespace(name="Datacenters"),
        ),
    ]
    return si


def test_requirements_cover_exactly_the_registered_tools() -> None:
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names, "no tools registered"
    assert set(REQUIREMENTS) == names


def test_get_access_info_is_read_only() -> None:
    tool = {t.name: t for t in asyncio.run(server.mcp.list_tools())}["get_access_info"]
    assert tool.annotations.readOnlyHint is True


@pytest.fixture
def _wired(monkeypatch):
    def wire(si: MagicMock) -> None:
        monkeypatch.setattr(server, "_get_connection", lambda target=None: si)
        monkeypatch.setattr(
            server,
            "_get_target_config",
            lambda target=None: SimpleNamespace(name="vc1", type="vcenter"),
        )

    return wire


def test_tool_returns_the_helpers_verdict(_wired) -> None:
    _wired(_fake_si({"System.View", "System.Read"}))
    ok = server.get_access_info(tool="list_virtual_machines")
    assert ok["account"] == "VSPHERE.LOCAL\\svc"
    assert ok["target"] == "vc1"
    assert ok["tool"]["verdict"] == "granted_at_root"

    missing = server.get_access_info(tool="list_active_sessions")
    assert missing["tool"]["verdict"] == "not_granted_at_root"
    assert missing["tool"]["missing_at_root"] == ["Sessions.TerminateSession"]
    assert missing["direct_permissions"][0]["role"] == "ReadOnly"


def test_all_tools_summary_puts_the_session_tool_in_not_granted(_wired) -> None:
    _wired(_fake_si({"System.View", "System.Read"}))
    out = server.get_access_info()
    assert "list_active_sessions" in out["tools_not_granted_at_root"]
    assert "list_virtual_machines" in out["tools_granted_at_root"]


def test_a_failed_lookup_is_undetermined_not_granted(_wired) -> None:
    si = _fake_si(set())
    si.content.authorizationManager.HasPrivilegeOnEntities.side_effect = RuntimeError("boom")
    _wired(si)
    assert server.get_access_info(tool="get_alarms")["tool"]["verdict"] == "undetermined"


def test_unknown_tool_is_a_teaching_error(monkeypatch) -> None:
    def no_login(*_a, **_k):
        raise AssertionError("connected for a misspelled tool name")

    monkeypatch.setattr(server, "_get_connection", no_login)
    out = server.get_access_info(tool="list_sessions")
    assert "list_active_sessions" in out["error"]


def test_cli_access_info_prints_the_verdict(monkeypatch) -> None:
    si = _fake_si({"System.View", "System.Read"})
    monkeypatch.setattr(cli_access, "get_connection", lambda target, config: (si, None, "vc1"))
    result = CliRunner().invoke(app, ["access-info", "--tool", "get_alarms"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["tool"]["verdict"] == "granted_at_root"


def test_cli_unknown_tool_exits_with_the_hint(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_access, "get_connection", lambda *a: pytest.fail("must not connect for an unknown tool")
    )
    result = CliRunner().invoke(app, ["access-info", "--tool", "list_sessions"])
    assert result.exit_code == 1
    assert "list_active_sessions" in result.output


def test_rest_tools_are_undetermined_not_granted(_wired) -> None:
    """Their deciding permission is not a privilege this check can ask about."""
    _wired(_fake_si({"System.View", "System.Read"}))
    out = server.get_access_info(tool="get_cluster_patch_compliance")
    assert out["tool"]["verdict"] == "undetermined"
