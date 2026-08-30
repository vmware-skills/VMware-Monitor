"""Regression evals — error-path resilience fixes (2026-06).

Bugs prevented (one test class per fix):

1. MCP tools annotated ``-> list[dict]`` returned a bare error *dict* on
   failure → FastMCP structured-output validation raised ToolError and the
   teaching hint never reached the agent.
2. ``_safe_error`` was called with the literal tool name "monitor" instead
   of the real tool name, making server-side logs useless.
3. ``doctor`` recommended the nonexistent command ``vmware-monitor init``.
4. ``get_recent_events`` swallowed *every* exception as "no events",
   masking auth/network failures as all-clear (and ``scan_logs`` had no
   guard at all for standalone ESXi NotSupported).
5. ``get_active_alarms`` died entirely when one alarm entity's ``name``
   round-trip failed (inaccessible entity).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyVmomi import vim, vmodl

REPO_ROOT = Path(__file__).resolve().parents[3]


# ── Fix 1 + 8: MCP tool error payloads match return annotations ──────────


ALL_TOOLS: list[tuple[str, dict]] = [
    ("list_virtual_machines", {}),
    ("list_esxi_hosts", {}),
    ("list_all_datastores", {}),
    ("list_all_clusters", {}),
    ("list_all_networks", {}),
    ("get_alarms", {}),
    ("get_events", {}),
    ("get_host_sensors", {}),
    ("get_host_services", {}),
    ("vm_info", {"vm_name": "ghost-vm"}),
    ("vm_list_snapshots", {"vm_name": "ghost-vm"}),
]


@pytest.fixture()
def broken_config(monkeypatch):
    """Point the MCP server at a nonexistent config and reset its cache."""
    import vmware_monitor.mcp_server.server as srv

    monkeypatch.setenv("VMWARE_MONITOR_CONFIG", "/nonexistent/vmware-monitor.yaml")
    monkeypatch.setattr(srv, "_conn_mgr", None)
    yield srv
    srv._conn_mgr = None


@pytest.mark.parametrize(("tool_name", "args"), ALL_TOOLS)
def test_tool_error_hint_survives_structured_output(broken_config, tool_name, args):
    """With a broken config, every tool must return a teaching hint instead
    of tripping FastMCP output validation (ToolError) or raising."""
    srv = broken_config
    result = asyncio.run(srv.mcp.call_tool(tool_name, args))  # must NOT raise
    text = str(result)
    assert "vmware-monitor doctor" in text, (
        f"{tool_name}: teaching hint lost in error path: {text[:300]}"
    )
    assert "error" in text


@pytest.mark.parametrize(("tool_name", "args"), ALL_TOOLS)
def test_tool_error_payload_shape_matches_annotation(broken_config, tool_name, args):
    """Calling the underlying function directly: list-annotated tools must
    return ``[{"error": ..., "hint": ...}]``, dict-annotated ``{...}``."""
    srv = broken_config
    fn = getattr(srv, tool_name)
    out = fn(**args)
    returns_list = str(fn.__annotations__.get("return", "")).startswith("list")
    if returns_list:
        assert isinstance(out, list) and len(out) == 1
        payload = out[0]
    else:
        assert isinstance(out, dict)
        payload = out
    assert "error" in payload and "hint" in payload
    assert "vmware-monitor doctor" in payload["hint"]


def test_catch_tool_errors_uses_real_tool_name(caplog):
    """_safe_error must receive the decorated function's real name (was the
    literal "monitor" for all 8 tools)."""
    from vmware_monitor.mcp_server.server import _catch_tool_errors

    def my_special_tool() -> dict:
        raise RuntimeError("boom")

    wrapped = _catch_tool_errors(my_special_tool)
    with caplog.at_level(logging.ERROR, logger="vmware_monitor.mcp_server.server"):
        out = wrapped()
    assert isinstance(out, dict) and "error" in out
    assert any("my_special_tool" in r.getMessage() for r in caplog.records)
    assert not any("Tool monitor failed" in r.getMessage() for r in caplog.records)


# ── Fix 2 (evolved): doctor may recommend `init` ONLY because it now exists ──
# Original invariant: "init doesn't exist → never mention it" (false-promise
# guard). Now an interactive `vmware-monitor init` wizard ships, so the guard
# flips to a consistency check: any `init` recommendation MUST be backed by a
# registered CLI command, and the manual fallback must remain.


def _init_command_is_registered() -> bool:
    from vmware_monitor.cli import app

    return any(c.name == "init" for c in app.registered_commands)


def test_doctor_recommends_init_which_actually_exists(monkeypatch, tmp_path):
    from vmware_monitor import config as cfg
    from vmware_monitor import doctor

    # The default path is patched on config, not on doctor: since 2026-08-30
    # the doctor asks resolve_config_path() rather than naming the default, so
    # that every check reports on the file the tools will actually open.
    monkeypatch.delenv("VMWARE_MONITOR_CONFIG", raising=False)
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "missing" / "config.yaml")
    monkeypatch.setattr(doctor, "ENV_FILE", tmp_path / "missing" / ".env")

    ok_cfg, msg_cfg = doctor._check_config_file()
    ok_env, msg_env = doctor._check_env_file()
    assert not ok_cfg and not ok_env
    # If (and only if) a message points at `vmware-monitor init`, the command
    # must be real — no false promises (踩坑 #2).
    for msg in (msg_cfg, msg_env):
        if "vmware-monitor init" in msg:
            assert _init_command_is_registered(), "doctor recommends init but it is not registered"
    # Manual fallback stays available for non-interactive setups.
    assert "config.example.yaml" in msg_cfg
    assert "chmod 600" in msg_env


def test_doctor_init_reference_is_backed_by_real_command():
    src = (REPO_ROOT / "vmware_monitor" / "doctor.py").read_text()
    if "vmware-monitor init" in src:
        assert _init_command_is_registered(), (
            "doctor.py references `vmware-monitor init` but no such CLI command is registered"
        )


# ── Fix 4: QueryEvents guard — NotSupported only, everything else raises ─


class _FakeEventMgr:
    def __init__(self, exc: Exception | None, events: list | None = None):
        self._exc = exc
        self._events = events or []

    def QueryEvents(self, spec):  # noqa: N802 — pyVmomi naming
        if self._exc is not None:
            raise self._exc
        return self._events


def test_query_events_not_supported_returns_empty():
    """Standalone ESXi (no event manager) → empty list, not a crash."""
    from vmware_monitor.ops.health import query_events

    mgr = _FakeEventMgr(vmodl.fault.NotSupported())
    assert query_events(mgr, filter_spec=None) == []


@pytest.mark.parametrize(
    "exc",
    [
        vim.fault.NoPermission(),
        vmodl.fault.SystemError(),
        ConnectionError("vCenter unreachable"),
        RuntimeError("session expired"),
    ],
)
def test_query_events_reraises_real_failures(exc):
    """Auth/network/permission failures must NOT be masked as 'no events'."""
    from vmware_monitor.ops.health import query_events

    mgr = _FakeEventMgr(exc)
    with pytest.raises(type(exc)):
        query_events(mgr, filter_spec=None)


def test_log_scanner_uses_shared_query_events_guard():
    """scan_logs must go through the shared guard, not raw QueryEvents."""
    src = (REPO_ROOT / "vmware_monitor" / "scanner" / "log_scanner.py").read_text()
    assert "query_events(" in src
    assert "event_mgr.QueryEvents(" not in src


def test_health_no_blanket_except_around_query_events():
    """The old ``except Exception: return []`` around QueryEvents is gone."""
    src = (REPO_ROOT / "vmware_monitor" / "ops" / "health.py").read_text()
    assert "events = query_events(" in src


# ── Fix 9: one inaccessible alarm entity must not kill get_alarms ────────


class _BrokenEntity:
    @property
    def name(self):
        raise vim.fault.NoPermission()


class _FakeAlarmState(SimpleNamespace):
    pass


def _alarm_state(entity) -> _FakeAlarmState:
    return _FakeAlarmState(
        overallStatus="red",
        entity=entity,
        alarm=SimpleNamespace(info=SimpleNamespace(name="Host CPU usage")),
        time="2026-06-11 00:00:00",
        acknowledged=False,
    )


class _FakeRootFolder(SimpleNamespace):
    pass


def test_get_alarms_survives_inaccessible_entity():
    from unittest.mock import patch

    from vmware_monitor.ops.health import get_active_alarms

    good = SimpleNamespace(name="esx-01")
    root = _FakeRootFolder(
        triggeredAlarmState=[_alarm_state(_BrokenEntity()), _alarm_state(good)]
    )
    content = SimpleNamespace(rootFolder=root)
    si = SimpleNamespace(RetrieveContent=lambda: content)

    # rootFolder alarms are read directly (O(1)); the DC/cluster/host sweep goes
    # through the batched _collect, which we stub empty for this fixture.
    with patch("vmware_monitor.ops.health._collect", return_value=[]):
        results = get_active_alarms(si)["items"]
    names = {r["entity_name"] for r in results}
    assert "esx-01" in names, "healthy entity must still be reported"
    assert "[inaccessible]" in names, "broken entity must appear as placeholder"
