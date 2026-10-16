"""HLD §18: the role metadata this skill declares matches its real tools."""

from __future__ import annotations

import asyncio

import pytest

from vmware_monitor import policy_roles


def _registry() -> set[str]:
    from vmware_monitor.mcp_server.server import mcp

    return {t.name for t in asyncio.run(mcp.list_tools())}


@pytest.mark.unit
def test_every_declared_name_is_a_real_tool():
    named = set(policy_roles.ESCAPES) | set(getattr(policy_roles, "AKO_TOOLS", ()))
    assert named - _registry() == set(), sorted(named - _registry())


@pytest.mark.unit
def test_metadata_is_declared_when_policy_has_roles():
    roles = pytest.importorskip("vmware_policy.roles")
    import vmware_monitor.mcp_server.server  # noqa: F401  (registers the metadata on import)

    meta = roles.metadata("monitor")
    assert meta is not None and meta.version == 1
    assert meta.escapes == policy_roles.ESCAPES
    assert (meta.resolver is not None) is True


@pytest.mark.unit
def test_the_resolver_uses_the_config_the_connection_manager_holds(monkeypatch):
    """Review H1/H-1: once the server's connection manager exists, a config edit must
    not move the role check off the config it connects with; before, read the file."""
    import importlib
    import types

    from vmware_monitor import policy_roles as pr

    server = importlib.import_module("vmware_monitor.mcp_server.server")
    assert hasattr(server, "_conn_mgr"), "policy_roles looks up a global the server does not have"
    edited, loads = object(), []
    monkeypatch.setattr(pr, "load_config", lambda: loads.append(1) or edited)

    monkeypatch.setattr(server, "_conn_mgr", None)
    assert pr._connection_config() is edited and pr._connection_config() is edited
    assert len(loads) == 2  # no manager yet: the file as it is now, every time

    held = object()
    monkeypatch.setattr(server, "_conn_mgr", types.SimpleNamespace(_config=held))
    assert pr._connection_config() is held  # the manager's copy, not the edited file
