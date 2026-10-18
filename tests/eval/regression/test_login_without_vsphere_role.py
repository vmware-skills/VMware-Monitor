"""A login that works but has no vSphere role must say so (AIops#47).

Reported against vCenter 8.0.3, not reproduced here (the lab is down): an SSO
account with no vSphere role logs in, ``sessionManager.currentSession`` reads
``None``, and every inventory call then fails with ``vim.fault.NotAuthenticated``.
``doctor`` passed, the CLI said "verify connectivity and credentials", and
``attention`` rolled the dead target up as ``overall: OK``.

Driven through the real ``ConnectionManager`` with only ``SmartConnect`` and
``Disconnect`` replaced, so the production factory, pool and cleanup all run.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from pyVmomi import vim
from rich.console import Console

from vmware_monitor import connection as conn_mod
from vmware_monitor.config import AppConfig, TargetConfig
from vmware_monitor.connection import ConnectionManager, NoVSphereRoleError

pytestmark = pytest.mark.unit

ROLE = "no vSphere role"


@pytest.fixture(autouse=True)
def _password(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("VC1", "VC2"):
        monkeypatch.setenv(f"VMWARE_{name}_PASSWORD", "test-only")
        monkeypatch.setenv(f"VMWARE_MONITOR_{name}_PASSWORD", "test-only")


def _config(*names: str) -> AppConfig:
    return AppConfig(
        targets=tuple(
            TargetConfig(name=n, host=f"{n}.example.com", config_username="svc") for n in names
        )
    )


def _si(has_session: bool, *, root_readable: bool = False) -> MagicMock:
    """A login as vCenter answers it.

    No role (AIops#47) takes both signals: ``currentSession`` reads ``None`` and
    a container view on the root folder is refused with NotAuthenticated.
    ``root_readable`` is the scoped account (one folder, or Supervisor namespace
    permissions only): no session either, but the root view opens.
    """
    si = MagicMock()
    si.content.sessionManager.currentSession = object() if has_session else None
    if not has_session and not root_readable:
        si.content.viewManager.CreateContainerView.side_effect = vim.fault.NotAuthenticated()
    return si


def _connect(mgr: ConnectionManager, target: str, *, role: bool, root_readable: bool = False):
    si = _si(role, root_readable=root_readable)
    with (
        patch("pyVim.connect.SmartConnect", return_value=si),
        patch("pyVim.connect.Disconnect") as disconnect,
    ):
        try:
            return si, mgr.connect(target), disconnect
        except NoVSphereRoleError as e:
            return si, e, disconnect


def test_role_less_login_is_refused_closed_and_not_cached() -> None:
    mgr = ConnectionManager(_config("vc1"))
    si, result, disconnect = _connect(mgr, "vc1", role=False)

    assert isinstance(result, NoVSphereRoleError)
    msg = str(result)
    assert ROLE in msg and "Read-Only" in msg and "'vc1'" in msg
    assert "vmware-monitor doctor" in msg
    disconnect.assert_called_once_with(si)  # the login is logged out
    assert id(si) not in conn_mod._SI_ATEXIT  # and its atexit cleanup taken back
    assert mgr.list_connected() == []  # and the pool kept nothing


def test_a_normal_login_is_unaffected() -> None:
    mgr = ConnectionManager(_config("vc1"))
    si, result, disconnect = _connect(mgr, "vc1", role=True)

    assert result is si
    disconnect.assert_not_called()
    assert mgr.list_connected() == ["vc1"]
    mgr.disconnect_all()


def test_a_scoped_account_without_a_session_still_connects() -> None:
    """``currentSession`` alone is not a verdict: a readable root view lets it through."""
    mgr = ConnectionManager(_config("vc1"))
    si, result, disconnect = _connect(mgr, "vc1", role=False, root_readable=True)

    assert result is si  # no NoVSphereRoleError
    disconnect.assert_not_called()
    assert mgr.list_connected() == ["vc1"]  # cached like any session
    view = si.content.viewManager.CreateContainerView.return_value
    view.Destroy.assert_called_once_with()  # the probe leaves no view behind
    mgr.disconnect_all()


def test_an_unreadable_session_is_not_a_verdict() -> None:
    """Unknown is not "no role": a probe that raises lets the login through."""
    mgr = ConnectionManager(_config("vc1"))
    si = MagicMock()
    type(si.content.sessionManager).currentSession = PropertyMock(side_effect=RuntimeError)
    with patch("pyVim.connect.SmartConnect", return_value=si), patch("pyVim.connect.Disconnect"):
        assert ConnectionManager._create_connection(mgr._config.get_target("vc1")) is si
    conn_mod._release_si(si)


def test_connect_all_names_the_role_case() -> None:
    mgr = ConnectionManager(_config("vc1", "vc2"))
    sis = {"vc1.example.com": _si(True), "vc2.example.com": _si(False)}
    with (
        patch("pyVim.connect.SmartConnect", side_effect=lambda host, **_: sis[host]),
        patch("pyVim.connect.Disconnect"),
    ):
        sessions, unreachable = mgr.connect_all()
    assert [n for n, _ in sessions] == ["vc1"]
    assert unreachable == [("vc2", "NoVSphereRoleError")]
    mgr.disconnect_all()


def test_doctor_auth_row_fails_with_the_role_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from vmware_monitor import doctor as doc

    monkeypatch.setattr(type(doc.resolve_config_path()), "exists", lambda self: True)
    monkeypatch.setattr("vmware_monitor.config.load_config", lambda: _config("vc1"))
    with (
        patch("pyVim.connect.SmartConnect", return_value=_si(False)),
        patch("pyVim.connect.Disconnect"),
    ):
        ok, row = doc._check_auth()
    assert ok is False
    assert ROLE in row and "vc1 ✗" in row


def _cli_output(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> str:
    import typer

    from vmware_monitor import cli_base

    buf = io.StringIO()
    monkeypatch.setattr(cli_base, "console", Console(file=buf, width=2000))

    @cli_base.cli_errors
    def cmd() -> None:
        raise exc

    with pytest.raises(typer.Exit):
        cmd()
    return buf.getvalue()


def test_cli_prints_the_role_message_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _cli_output(
        monkeypatch, NoVSphereRoleError(conn_mod.no_role_message("vc1", "vmware-monitor"))
    )
    assert ROLE in out and "will not help" in out
    assert "verify connectivity and credentials" not in out


def test_cli_not_authenticated_names_both_causes(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _cli_output(
        monkeypatch, vim.fault.NotAuthenticated(msg="The session is not authenticated.")
    )
    assert "expired" in out and "vSphere role" in out
    assert "verify connectivity and credentials" not in out


def _mcp_payload(exc: Exception) -> dict:
    from vmware_monitor.mcp_server import server

    @server._catch_tool_errors
    def tool() -> dict:
        raise exc

    return tool()


def test_mcp_passes_the_whole_role_message() -> None:
    msg = conn_mod.no_role_message("a-rather-long-target-name-in-config", "vmware-monitor")
    payload = _mcp_payload(NoVSphereRoleError(msg))
    assert payload["error"] == msg  # not cut at the 300-character cap, not a class name
    assert "credentials" not in payload["hint"]


def test_mcp_not_authenticated_names_both_causes() -> None:
    payload = _mcp_payload(vim.fault.NotAuthenticated(msg="The session is not authenticated."))
    assert "expired" in payload["error"] and "vSphere role" in payload["error"]
    assert "credentials" not in payload["hint"]


# ─── attention: an unread target is never "ok" ──────────────────────────────


def _attention(monkeypatch: pytest.MonkeyPatch, sessions, unreachable, worst="ok"):
    from vmware_monitor.ops import attention

    summary = {
        "totals": {
            "clusters": 1,
            "hosts_connected": 1,
            "hosts_total": 1,
            "alarms": {"critical": 0, "warning": 0},
            "worst_status": worst,
        },
        "top_issues": [],
        "issues_total": 0,
        "clusters": [],
    }
    monkeypatch.setattr(attention, "get_cluster_health_summary", lambda si, **_: summary)
    monkeypatch.setattr(
        attention,
        "target_identity",
        lambda si: {"endpoint": "vcenter", "hosts": None, "datastore_urls": None},
    )
    return attention.get_cross_vcenter_attention(sessions, unreachable=unreachable)


def test_attention_all_unreachable_is_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _attention(monkeypatch, [], [("vmsso", "NoVSphereRoleError")])
    assert data["totals"]["worst_status"] == "critical"
    assert data["totals"]["status_reason"] == "1 of 1 target could not be read"


def test_attention_some_unreachable_is_at_least_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _attention(
        monkeypatch, [("vc1", object())], [("vc2", "TimeoutError"), ("vc3", "gaierror")]
    )
    assert data["totals"]["worst_status"] == "warn"
    assert data["totals"]["status_reason"] == "2 of 3 targets could not be read"


def test_attention_a_worse_status_is_not_lowered(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _attention(monkeypatch, [("vc1", object())], [("vc2", "TimeoutError")], worst="critical")
    assert data["totals"]["worst_status"] == "critical"


def test_attention_all_read_keeps_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _attention(monkeypatch, [("vc1", object())], [])
    assert data["totals"]["worst_status"] == "ok"
    assert data["totals"]["status_reason"] is None


def test_attention_nothing_configured_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _attention(monkeypatch, [], [])
    assert data["totals"]["worst_status"] == "critical"


def test_attention_renderers_say_why(monkeypatch: pytest.MonkeyPatch) -> None:
    from vmware_monitor import cli_observability
    from vmware_monitor.ops.attention_html import render_attention_html

    data = _attention(monkeypatch, [("vc1", object())], [("vc2", "NoVSphereRoleError")])

    buf = io.StringIO()
    monkeypatch.setattr(cli_observability, "console", Console(file=buf, width=2000))
    cli_observability.render_attention_console(data)
    out = buf.getvalue()
    assert "overall: WARN (1 of 2 targets could not be read)" in out
    assert "vc2 (NoVSphereRoleError)" in out

    html = render_attention_html(data, datetime(2026, 10, 16, tzinfo=timezone.utc))
    # Beside the overall status, and again where "no issues" would have read as all-clear.
    assert html.count("1 of 2 targets could not be read") == 2
    assert "every vCenter is OK" not in html
    # The skipped-targets row must not say they were left out of the status, which they floor.
    assert "not included in the status" not in html
    assert "1 target(s) unreachable and not read; the overall status is raised for them" in html


# 2026-10-18, lab vCenter 8.0.3, SSO account agt-norole@vsphere.local with no
# permission anywhere: vCenter refuses the *login itself* with NoPermission
# (System.View on the root folder, group-d1). The connect-time check above never
# ran, so doctor printed the raw pyVmomi fault and every MCP tool said
# "vim.fault.NoPermission: operation failed." with a connectivity hint.
def _login_refused(exc: Exception):
    mgr = ConnectionManager(_config("vc1"))
    with patch("pyVim.connect.SmartConnect", side_effect=exc):
        try:
            mgr.connect("vc1")
        except Exception as e:  # noqa: BLE001 — the test inspects whatever is raised
            return e
    raise AssertionError("connect did not raise")


def test_login_refused_with_no_permission_names_the_missing_role() -> None:
    fault = vim.fault.NoPermission(
        privilegeId="System.View", msg="Permission to perform this operation was denied."
    )
    err = _login_refused(fault)
    assert isinstance(err, NoVSphereRoleError)
    msg = str(err)
    assert ROLE in msg and "Read-Only" in msg and "'vc1'" in msg
    assert "dynamicType" not in msg and "operation failed" not in msg
    assert err.__cause__ is fault


def test_wrong_password_is_not_reported_as_a_missing_role() -> None:
    err = _login_refused(vim.fault.InvalidLogin())
    assert not isinstance(err, NoVSphereRoleError)
    assert isinstance(err, vim.fault.InvalidLogin)


@pytest.mark.parametrize(
    ("privilege", "esxi"),
    [("System.View", True), ("Sessions.ValidateSession", False), (None, False)],
)
def test_other_login_refusals_get_a_neutral_remedy(privilege, esxi) -> None:
    # Review 2026-10-18 (M3): an ESXi host (lockdown mode, host permissions) or
    # another privilege must not be sent to vCenter Global Permissions.
    from dataclasses import replace

    fault = vim.fault.NoPermission(privilegeId=privilege)
    mgr = ConnectionManager(_config("vc1"))
    if esxi:
        esxi_targets = tuple(replace(t, type="esxi") for t in mgr._config.targets)
        mgr._config = replace(mgr._config, targets=esxi_targets)
    with patch("pyVim.connect.SmartConnect", side_effect=fault):
        with pytest.raises(Exception) as info:
            mgr.connect("vc1")
    msg = str(info.value)
    assert "Global Permissions" not in msg and "lockdown mode" in msg
    if privilege:
        assert f"({privilege})" in msg
    assert info.value.__cause__ is fault and len(msg) <= 300
