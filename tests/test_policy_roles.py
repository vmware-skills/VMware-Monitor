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
def test_the_resolver_reads_the_config_once_like_the_connection(monkeypatch):
    """Review H1: a config edit after startup must not move the role check off the
    target the (load-once) connection manager uses."""
    from vmware_monitor import policy_roles as pr

    loads = []
    monkeypatch.setattr(pr, "load_config", lambda: loads.append(1) or object())
    pr._startup_config.cache_clear()
    try:
        first = pr._startup_config()
        assert pr._startup_config() is first
        assert loads == [1]
    finally:
        pr._startup_config.cache_clear()
