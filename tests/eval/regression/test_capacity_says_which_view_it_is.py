"""Regression — datastore capacity says whose view it is and why views differ.

Investigated 2026-09-15 on the lab. The same datastore1 (803.2 GB, 60.8% used)
reported provisioned 1734.0 GB through ``home-vcenter`` and 1094.9 GB through
``home-esxi`` (the host that mounts it). Same formula over ``summary.uncommitted``;
the two endpoints returned different values.

The cause, read through this skill: vCenter lists 11 VMs on host .15 and on
datastore1, the host itself lists 9. The two extra are powered-off VMs in
vCenter's "/Discovered virtual machine" folder that the host does not have
registered — "VMware vCenter Server" (17 thin disks, ~587 GB, a copy of the
running vcsa's disk layout) and "linux-hermers" (25 GB thick). vCenter sums
provisioned space over the VMs *it* has registered on the datastore; hostd sums
over VMs registered on the host. Committed space is measured on disk and agrees.
Both numbers are legitimate; neither is a stale refresh.

So the formula is unchanged. What changed is that the output says which view it
is (``view``, ``view_note``), how many VMs each datastore's figure covers
(``vm_count``), and names any of those VMs vCenter itself does not report as
connected (``vms_not_connected``) — ``None`` when that could not be read, never
an empty list standing in for "unknown".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyVmomi import vim
from typer.testing import CliRunner

from vmware_monitor.cli import app
from vmware_monitor.mcp_server import server
from vmware_monitor.ops import capacity

GB = 1024**3
runner = CliRunner()


class _SI:
    def __init__(self, api_type):
        self.content = SimpleNamespace(about=SimpleNamespace(apiType=api_type))


def _vms(n):
    return [vim.VirtualMachine(f"vm-{i}") for i in range(n)]


def _install(monkeypatch, vm_refs, vm_rows, vm_fail=None):
    asked: list = []

    def fake_collect(si, obj_type, paths):
        t = obj_type[0]
        asked.append(t)
        if t is vim.Datastore:
            return [
                (
                    vim.Datastore("datastore-1"),
                    {
                        "name": "datastore1",
                        "summary.type": "VMFS",
                        "summary.capacity": 803.2 * GB,
                        "summary.freeSpace": 315.1 * GB,
                        "summary.uncommitted": 1246.0 * GB,
                        "vm": vm_refs,
                    },
                )
            ]
        if t is vim.VirtualMachine:
            if vm_fail:
                raise vm_fail
            return vm_rows
        return []

    monkeypatch.setattr(capacity, "_collect", fake_collect)
    return asked


def _lab_vcenter(monkeypatch):
    refs = _vms(11)
    rows = [
        (r, {"name": f"vm{i}", "runtime.connectionState": "connected"})
        for i, r in enumerate(refs)
    ]
    rows[3] = (refs[3], {"name": "VMware vCenter Server", "runtime.connectionState": "orphaned"})
    return _install(monkeypatch, refs, rows)


@pytest.mark.unit
def test_a_vcenter_view_says_so_and_counts_the_vms_behind_the_figure(monkeypatch):
    _lab_vcenter(monkeypatch)
    out = capacity.get_datastore_capacity(_SI("VirtualCenter"))
    assert out["view"] == "vCenter"
    assert "ESXi" in out["view_note"] and "registered" in out["view_note"]
    row = out["items"][0]
    assert row["vm_count"] == 11
    assert row["vms_not_connected"] == ["VMware vCenter Server (orphaned)"]


@pytest.mark.unit
def test_an_esxi_view_says_a_vcenter_may_show_more(monkeypatch):
    refs = _vms(9)
    connected = [(r, {"name": "x", "runtime.connectionState": "connected"}) for r in refs]
    _install(monkeypatch, refs, connected)
    out = capacity.get_datastore_capacity(_SI("HostAgent"))
    assert out["view"] == "ESXi host"
    assert "vCenter" in out["view_note"]
    assert out["items"][0]["vm_count"] == 9
    assert out["items"][0]["vms_not_connected"] == []


@pytest.mark.unit
def test_an_unreadable_endpoint_kind_is_not_guessed(monkeypatch):
    _lab_vcenter(monkeypatch)
    out = capacity.get_datastore_capacity(_SI(None))
    assert out["view"] == "unknown"


@pytest.mark.unit
def test_an_unreadable_vm_state_is_none_not_an_empty_list(monkeypatch):
    _install(monkeypatch, _vms(2), [], vm_fail=RuntimeError("PropertyCollector refused"))
    out = capacity.get_datastore_capacity(_SI("VirtualCenter"))
    row = out["items"][0]
    assert row["vm_count"] == 2
    assert row["vms_not_connected"] is None
    assert row["provisioned_gb"] == round((803.2 - 315.1 + 1246.0), 1)


@pytest.mark.unit
def test_no_vm_read_when_no_datastore_holds_vms(monkeypatch):
    asked = _install(monkeypatch, [], [])
    capacity.get_datastore_capacity(_SI("VirtualCenter"))
    assert vim.VirtualMachine not in asked


@pytest.mark.unit
def test_cli_capacity_datastores_shows_the_view_and_vm_count(monkeypatch):
    _lab_vcenter(monkeypatch)
    monkeypatch.setattr(
        "vmware_monitor.cli_observability.get_connection",
        lambda t, c: (_SI("VirtualCenter"), None, "lab"),
    )
    result = runner.invoke(app, ["capacity", "datastores"])
    assert result.exit_code == 0, result.output
    # Rich wraps at the console's own width; compare with whitespace collapsed.
    flat = " ".join(result.output.split())
    assert "VMs" in flat and "11" in flat
    assert "vCenter view" in flat
    assert "VMware vCenter Server (orphaned)" in flat


@pytest.mark.unit
def test_the_mcp_tool_documents_the_view_fields():
    doc = server.datastore_capacity.__doc__ or ""
    for field in ("view", "view_note", "vm_count", "vms_not_connected"):
        assert field in doc, f"datastore_capacity docstring does not mention {field}"
