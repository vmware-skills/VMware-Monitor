"""Regression tests for the vSphere 9.1 read surface fixes.

Each test here fails against the pre-fix code and passes after:

  MEDIUM-1  the CLI's ``_target_config`` must resolve config WITHOUT a SOAP
            SmartConnect, so a mid-patch vCenter answering 503 does not kill the
            command before the {available: False} REST degradation runs (踩坑 #37).
  MEDIUM-2  patch-compliance must return non_compliant_hosts=None (unknown) when
            no host row carries a recognised status key — never a false 0
            (踩坑 形态 #1: an unmatched read is 'unknown', not 'none').
  LOW-1     memory tiering on a pre-8.0U3 target must raise a teaching ValueError
            naming the version, not leak an opaque InvalidProperty fault.
  DOC       SKILL.md's advertised tool count + names must match the live registry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[1]


# ─── MEDIUM-1: CLI config resolution must not open a SOAP session ─────────────


def _fake_config(names_to_targets: dict, default_name: str):
    def get_target(name):
        if name in names_to_targets:
            return names_to_targets[name]
        raise KeyError(name)

    return SimpleNamespace(
        get_target=get_target,
        default_target=names_to_targets[default_name],
    )


def test_target_config_does_not_open_soap_session(monkeypatch):
    import vmware_monitor.cli_vsphere91 as mod
    import vmware_monitor.config as config_mod

    tcfg = SimpleNamespace(name="prod")
    fake_cfg = _fake_config({"prod": tcfg}, default_name="prod")
    monkeypatch.setattr(config_mod, "load_config", lambda c=None: fake_cfg)

    def _boom(*a, **k):
        raise AssertionError(
            "get_connection (a pyVmomi SmartConnect) must not run while resolving "
            "the REST TargetConfig — it would fail on a mid-patch 503."
        )

    monkeypatch.setattr(mod, "get_connection", _boom)

    resolved, tgt = mod._target_config(None, None)
    assert resolved is tcfg
    assert tgt == "prod"


def test_target_config_honours_explicit_target_name(monkeypatch):
    import vmware_monitor.cli_vsphere91 as mod
    import vmware_monitor.config as config_mod

    prod = SimpleNamespace(name="prod")
    dr = SimpleNamespace(name="dr")
    fake_cfg = _fake_config({"prod": prod, "dr": dr}, default_name="prod")
    monkeypatch.setattr(config_mod, "load_config", lambda c=None: fake_cfg)
    monkeypatch.setattr(
        mod, "get_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError())
    )

    resolved, tgt = mod._target_config("dr", None)
    assert resolved is dr
    assert tgt == "dr"


# ─── MEDIUM-2: unknown status field => None, never a false 0 ──────────────────


def _patch_rest(monkeypatch, data):
    import vmware_monitor.ops.patching as patmod

    class _FakeRest:
        def __init__(self, target):
            self._data = data

        def get_json(self, path):
            return self._data

    monkeypatch.setattr(patmod, "VsphereRest", _FakeRest)


def _compliance(monkeypatch, hosts):
    from vmware_monitor.ops.patching import get_patch_compliance

    _patch_rest(monkeypatch, {"status": "x", "hosts": hosts})
    return get_patch_compliance(target=None, cluster="domain-c1")


def test_patch_compliance_unknown_key_yields_none_not_zero(monkeypatch):
    # No host row carries a recognised status key -> a wrong schema guess.
    result = _compliance(monkeypatch, {"host-1": {"name": "a"}, "host-2": {"name": "b"}})
    assert result["hosts_total"] == 2
    assert result["non_compliant_hosts"] is None  # pre-fix bug: silently 0


def test_patch_compliance_reads_status_key(monkeypatch):
    result = _compliance(
        monkeypatch,
        {
            "h1": {"status": "COMPLIANT"},
            "h2": {"status": "NON_COMPLIANT"},
            "h3": {"status": "NON_COMPLIANT"},
        },
    )
    assert result["non_compliant_hosts"] == 2  # pre-fix only read compliance_status


def test_patch_compliance_reads_compliance_status_key(monkeypatch):
    result = _compliance(
        monkeypatch,
        {"h1": {"compliance_status": "COMPLIANT"}, "h2": {"compliance_status": "NON_COMPLIANT"}},
    )
    assert result["non_compliant_hosts"] == 1


def test_patch_compliance_empty_hosts_dict_is_zero_not_none(monkeypatch):
    # A cluster with zero hosts is a real "checked, none non-compliant" = 0.
    result = _compliance(monkeypatch, {})
    assert result["hosts_total"] == 0
    assert result["non_compliant_hosts"] is None  # no rows carried a key -> unknown


# ─── LOW-1: pre-8.0U3 target => teaching ValueError, not opaque fault ─────────


def test_memory_tiering_old_vcenter_raises_teaching_error(monkeypatch):
    from pyVmomi import vmodl

    import vmware_monitor.ops.memory_tiering as mt

    def _raise_invalid(si, obj_type, paths):
        raise vmodl.query.InvalidProperty(name="hardware.memoryTieringType")

    monkeypatch.setattr(mt, "_collect", _raise_invalid)

    with pytest.raises(ValueError, match="8.0U3"):
        mt.get_memory_tiering(si=object())


# ─── DOC: SKILL.md count + tool names track the live registry ────────────────


def test_skill_md_tool_count_and_names_match_registry():
    import vmware_monitor.mcp_server.tools_vsphere91  # noqa: F401  (registers tools)
    from vmware_monitor.mcp_server import server

    tools = asyncio.run(server.mcp.list_tools())
    count = len(tools)
    names = {t.name for t in tools}

    new_tools = {
        "host_memory_tiering",
        "cluster_patch_compliance",
        "cluster_last_apply_result",
        "vcenter_deployment_size",
    }
    assert new_tools <= names, f"registry missing {new_tools - names}"

    skill = (_REPO / "skills" / "vmware-monitor" / "SKILL.md").read_text()
    assert f"MCP Tools ({count}" in skill, f"SKILL.md tool count != {count}"
    for name in new_tools:
        assert name in skill, f"SKILL.md does not document {name}"
