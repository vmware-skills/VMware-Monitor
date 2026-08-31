"""Regression — VM investigation bundle: correlation, shape, batching, safety.

Locks the object-centered drill-down added for the issue #31 follow-up. The bundle
must (a) raise a *teaching* error for an unknown VM, (b) walk the object graph
(VM -> host -> cluster -> datastores) via batched reads only, (c) aggregate alarms
across every correlated scope with names resolved in one batched call, (d) merge a
newest-first, de-duplicated, severity-tagged event timeline, and (e) never let a
crafted object name inject into the offline HTML.

All I/O helpers are monkeypatched so the pure aggregation/correlation logic runs
with zero pyVmomi SOAP plumbing (``si`` is just ``object()``).
"""

from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest
from pyVmomi import vim
from vmware_policy import paginated

from vmware_monitor.ops import _correlate, investigate_vm
from vmware_monitor.ops.investigate_html import render_bundle_html
from vmware_monitor.ops.vm_info import VMNotFoundError


class _AlarmRef:
    """Hashable stand-in for a vim.alarm.Alarm managed-object ref (SimpleNamespace
    defines __eq__ so it is unhashable, unlike real managed objects)."""

    def __init__(self, name: str) -> None:
        self._name = name


def _alarm(status: str, name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(overallStatus=status, alarm=_AlarmRef(name))


def _cluster_ref():
    """A stand-in whose leaf type name matches ``_is_cluster``'s check."""
    # A real pyVmomi managed-object reference — it constructs fine without a
    # live connection. The old stand-in was a bare namespace class whose
    # __name__ happened to equal the leaf type name: the only object in
    # existence that the old production check matched. This test passed
    # precisely because the fake was shaped like the bug (形态 #3).
    return vim.ClusterComputeResource("domain-c1")


def _install_graph(monkeypatch, *, vm_alarms=(), host_alarms=(), with_cluster=True):
    """Wire a fake object graph. Returns a call-counter dict for N+1 assertions."""
    vm_ref, host_ref, ds_ref = object(), object(), object()
    cl_ref = _cluster_ref() if with_cluster else None
    counter = {"collect_objects": 0}

    def fake_find(si, name):
        return vm_ref if name == "web-01" else None

    def fake_collect_objects(si, objs, obj_type, paths):
        counter["collect_objects"] += 1
        if obj_type is vim.VirtualMachine:
            return [
                (
                    vm_ref,
                    {
                        "name": "web-01",
                        "runtime.host": host_ref,
                        "runtime.powerState": "poweredOn",
                        "datastore": [ds_ref],
                        "summary.config.numCpu": 4,
                        "summary.config.memorySizeMB": 16384,
                        "summary.guest.ipAddress": "10.0.1.5",
                        "summary.config.guestFullName": "Ubuntu Linux (64-bit)",
                        "summary.overallStatus": "yellow",
                        "triggeredAlarmState": [_alarm(s, n) for s, n in vm_alarms],
                    },
                )
            ]
        if obj_type is vim.HostSystem:
            return [
                (
                    host_ref,
                    {
                        "name": "esxi-09",
                        "parent": cl_ref,
                        "runtime.connectionState": "connected",
                        "summary.quickStats.overallCpuUsage": 5000,
                        "summary.quickStats.overallMemoryUsage": 32000,
                        "summary.hardware.cpuMhz": 2500,
                        "summary.hardware.numCpuCores": 8,
                        "summary.hardware.memorySize": 64 * 1024**3,
                        "triggeredAlarmState": [_alarm(s, n) for s, n in host_alarms],
                    },
                )
            ]
        if obj_type is vim.ClusterComputeResource:
            return [
                (
                    cl_ref,
                    {
                        "name": "prod-A",
                        "host": [host_ref, object()],
                        "configuration.dasConfig.enabled": False,
                        "configuration.drsConfig.enabled": True,
                        "triggeredAlarmState": [],
                    },
                )
            ]
        if obj_type is vim.Datastore:
            return [
                (
                    ds_ref,
                    {
                        "name": "ds-ssd-01",
                        "summary.type": "VMFS",
                        "summary.freeSpace": 300 * 1024**3,
                        "summary.capacity": 1000 * 1024**3,
                        "summary.accessible": True,
                        "triggeredAlarmState": [],
                    },
                )
            ]
        # alarm-name resolution batch (vim.alarm.Alarm): objs are the .alarm refs.
        return [(o, {"info.name": getattr(o, "_name", "alarm")}) for o in objs]

    monkeypatch.setattr(investigate_vm, "find_vm_by_name", fake_find)
    monkeypatch.setattr(investigate_vm, "_collect_objects", fake_collect_objects)
    monkeypatch.setattr(_correlate, "_collect_objects", fake_collect_objects)
    monkeypatch.setattr(_correlate, "entity_timeline", lambda si, ents, hours=24: [])
    monkeypatch.setattr(investigate_vm, "list_snapshots", lambda si, name: paginated([], total=0))
    monkeypatch.setattr(
        investigate_vm,
        "get_vm_performance",
        lambda si, vm_name, limit: paginated([], limit=limit, total=0),
    )
    return counter


def test_unknown_vm_raises_teaching_error(monkeypatch):
    """The remedy must name a tool the model can actually call.

    This asserted `list_vms` until v1.9 — an internal ops function, not an MCP
    tool. The error read as a perfectly good teaching error and this test kept
    it green, so a model following the instruction called something that does
    not exist, on the one path where it was already recovering from a failure.
    Pinned against the live registry now, not a string, so the tool being
    renamed or removed fails here instead of shipping.
    """
    import asyncio

    from vmware_monitor.mcp_server.server import mcp

    monkeypatch.setattr(investigate_vm, "find_vm_by_name", lambda si, name: None)
    with pytest.raises(VMNotFoundError) as exc:
        investigate_vm.get_vm_investigation_bundle(object(), "does-not-exist")
    msg = str(exc.value)
    assert "not found" in msg

    tool_names = {t.name for t in asyncio.run(mcp.list_tools())}
    named = {t for t in tool_names if t in msg}
    assert named, (
        f"the remedy names no callable tool — message was {msg!r}. A teaching "
        "error that points at an internal function teaches the model to fail."
    )


def test_bundle_correlates_full_graph(monkeypatch):
    _install_graph(monkeypatch)
    b = investigate_vm.get_vm_investigation_bundle(object(), "web-01", hours=48)

    assert b["object"] == {
        "name": "web-01",
        "power": "poweredOn",
        "cpu": 4,
        "memory_gb": 16.0,
        "guest_os": "Ubuntu Linux (64-bit)",
        "ip": "10.0.1.5",
        "status": "yellow",
    }
    assert b["host"]["name"] == "esxi-09" and b["host"]["connection"] == "connected"
    assert b["cluster"]["name"] == "prod-A" and b["cluster"]["drs_enabled"] is True
    assert [d["name"] for d in b["datastores"]] == ["ds-ssd-01"]
    assert b["datastores"][0]["free_pct"] == 30.0
    assert b["hours"] == 48
    assert "point-in-time" in b["snapshot"]
    assert b["customization_hint"]


def test_standalone_host_has_no_cluster(monkeypatch):
    # parent is not a cluster -> cluster context is None, bundle still valid.
    _install_graph(monkeypatch, with_cluster=False)
    b = investigate_vm.get_vm_investigation_bundle(object(), "web-01")
    assert b["cluster"] is None
    assert b["host"]["name"] == "esxi-09"


def test_alarms_aggregate_across_scopes_with_resolved_names(monkeypatch):
    _install_graph(
        monkeypatch,
        vm_alarms=[("red", "VM CPU usage")],
        host_alarms=[("yellow", "Host memory"), ("green", "ignored-green")],
    )
    b = investigate_vm.get_vm_investigation_bundle(object(), "web-01")
    names = {(a["scope"], a["name"], a["severity"]) for a in b["alarms"]}
    assert ("vm", "VM CPU usage", "critical") in names
    assert ("host", "Host memory", "warning") in names
    # green/gray states are not anomalies -> dropped
    assert all(a["severity"] in ("critical", "warning") for a in b["alarms"])


def test_batched_reads_are_bounded(monkeypatch):
    # VM + host + cluster + datastore + alarm-resolution = at most a handful of
    # batched calls, never one-per-object (issue #31 regression guard).
    counter = _install_graph(monkeypatch, vm_alarms=[("red", "a")])
    investigate_vm.get_vm_investigation_bundle(object(), "web-01")
    assert counter["collect_objects"] <= 5


def test_html_escapes_crafted_names(monkeypatch):
    _install_graph(monkeypatch)
    b = investigate_vm.get_vm_investigation_bundle(object(), "web-01")
    b["object"]["name"] = "<img src=x onerror=alert(1)>"
    html = render_bundle_html(b, "vm", "vcenter-prod", datetime(2026, 7, 14, tzinfo=timezone.utc))
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html


# ── entity_timeline: correlation ordering, de-dup, severity filter ───────────
def _event(cls_name: str, key: int, when: str, msg: str):
    ev = type(cls_name, (), {})()
    ev.key = key
    ev.createdTime = when
    ev.fullFormattedMessage = msg
    ev.userName = "root"
    return ev


def test_entity_timeline_merges_dedups_and_orders(monkeypatch):
    vm_ref, host_ref = object(), object()
    shared = _event("VmReconfiguredEvent", 1, "2026-07-14T10:00:00Z", "reconfigured")
    events_by_ref = {
        id(vm_ref): [
            shared,
            _event("HostConnectionLostEvent", 2, "2026-07-14T12:00:00Z", "lost"),
        ],
        id(host_ref): [shared, _event("VmPoweredOnEvent", 3, "2026-07-14T08:00:00Z", "on")],
    }

    monkeypatch.setattr(
        _correlate,
        "_entity_events",
        lambda mgr, ref, begin, now: events_by_ref.get(id(ref), []),
    )
    si = types.SimpleNamespace(RetrieveContent=lambda: types.SimpleNamespace(eventManager=object()))
    entities = [("vm", "web-01", vm_ref), ("host", "esxi-09", host_ref)]
    tl = _correlate.entity_timeline(si, entities, hours=24)

    # de-dup: the shared event appears once, tagged with the first (vm) scope.
    assert sum(1 for e in tl if e["event_type"] == "VmReconfiguredEvent") == 1
    assert next(e for e in tl if e["event_type"] == "VmReconfiguredEvent")["scope"] == "vm"
    # newest first
    assert [e["time"] for e in tl] == sorted((e["time"] for e in tl), reverse=True)
    # severity classification via health's maps
    sev = {e["event_type"]: e["severity"] for e in tl}
    assert sev["HostConnectionLostEvent"] == "critical"
    assert sev["VmPoweredOnEvent"] == "info"


def test_entity_timeline_severity_threshold(monkeypatch):
    ref = object()
    monkeypatch.setattr(
        _correlate,
        "_entity_events",
        lambda mgr, r, begin, now: [
            _event("HostConnectionLostEvent", 1, "2026-07-14T12:00:00Z", "crit"),
            _event("VmPoweredOnEvent", 2, "2026-07-14T11:00:00Z", "info"),
        ],
    )
    si = types.SimpleNamespace(RetrieveContent=lambda: types.SimpleNamespace(eventManager=object()))
    tl = _correlate.entity_timeline(si, [("vm", "web-01", ref)], min_severity="warning")
    assert [e["event_type"] for e in tl] == ["HostConnectionLostEvent"]
