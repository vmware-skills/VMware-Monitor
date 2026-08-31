"""Regression — host & datastore investigation bundles: correlation, shape, safety.

Locks the object-centered drill-down for hosts and datastores (issue #31 follow-up),
mirroring the VM bundle guards: teaching errors for unknown names, batched-only graph
walks, cross-scope alarm aggregation with resolved names, a merged event timeline,
and HTML escaping of crafted object names. All I/O helpers are monkeypatched so the
pure aggregation logic runs without pyVmomi SOAP plumbing.
"""

from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest
from pyVmomi import vim
from vmware_policy import paginated

from vmware_monitor.ops import _correlate, investigate_datastore, investigate_host
from vmware_monitor.ops.investigate_datastore import DatastoreNotFoundError
from vmware_monitor.ops.investigate_host import HostNotFoundError
from vmware_monitor.ops.investigate_html import render_bundle_html


class _AlarmRef:
    """Hashable stand-in for a vim.alarm.Alarm ref (SimpleNamespace is unhashable)."""

    def __init__(self, name: str) -> None:
        self._name = name


def _alarm(status: str, name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(overallStatus=status, alarm=_AlarmRef(name))


def _cluster_ref():
    # A real pyVmomi managed-object reference — it constructs fine without a
    # live connection. The old stand-in was a bare namespace class whose
    # __name__ happened to equal the leaf type name: the only object in
    # existence that the old production check matched. This test passed
    # precisely because the fake was shaped like the bug (形态 #3).
    return vim.ClusterComputeResource("domain-c1")


def _vm_rows(refs, powered):
    return [
        (r, {"name": f"vm-{i}", "runtime.powerState": "poweredOn" if on else "poweredOff"})
        for i, (r, on) in enumerate(zip(refs, powered))
    ]


# ── Host bundle ──────────────────────────────────────────────────────────────
def _install_host(monkeypatch, *, host_alarms=(), with_cluster=True):
    host_ref, ds_ref = object(), object()
    vm_refs = [object(), object(), object()]
    cl_ref = _cluster_ref() if with_cluster else None
    counter = {"n": 0}

    def fake_collect_objects(si, objs, obj_type, paths):
        counter["n"] += 1
        if obj_type is vim.HostSystem:
            return [
                (
                    host_ref,
                    {
                        "name": "esxi-09",
                        "parent": cl_ref,
                        "vm": vm_refs,
                        "datastore": [ds_ref],
                        "runtime.connectionState": "connected",
                        "summary.quickStats.overallCpuUsage": 5000,
                        "summary.quickStats.overallMemoryUsage": 32000,
                        "summary.quickStats.uptime": 7200,
                        "summary.hardware.cpuMhz": 2500,
                        "summary.hardware.numCpuCores": 8,
                        "summary.hardware.memorySize": 64 * 1024**3,
                        "summary.overallStatus": "green",
                        "config.product.version": "8.0.3",
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
                        "host": [host_ref],
                        "configuration.dasConfig.enabled": True,
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
        if obj_type is vim.VirtualMachine:
            return _vm_rows(vm_refs, [True, True, False])
        return [(o, {"info.name": getattr(o, "_name", "alarm")}) for o in objs]

    monkeypatch.setattr(
        investigate_host, "find_host_by_name", lambda si, n: host_ref if n == "esxi-09" else None
    )
    monkeypatch.setattr(investigate_host, "_collect_objects", fake_collect_objects)
    monkeypatch.setattr(_correlate, "_collect_objects", fake_collect_objects)
    monkeypatch.setattr(_correlate, "entity_timeline", lambda si, ents, hours=24: [])
    monkeypatch.setattr(
        investigate_host,
        "get_host_performance",
        lambda si, host_name, limit: paginated([], limit=limit, total=0),
    )
    return counter


def test_host_unknown_raises_teaching_error(monkeypatch):
    monkeypatch.setattr(investigate_host, "find_host_by_name", lambda si, n: None)
    with pytest.raises(HostNotFoundError) as exc:
        investigate_host.get_host_investigation_bundle(object(), "nope")
    assert "list_esxi_hosts" in str(exc.value)


def test_host_bundle_correlates_graph(monkeypatch):
    _install_host(monkeypatch, host_alarms=[("yellow", "Host memory")])
    b = investigate_host.get_host_investigation_bundle(object(), "esxi-09")
    assert b["object"]["name"] == "esxi-09"
    assert b["object"]["version"] == "8.0.3"
    assert b["object"]["uptime_hours"] == 2.0
    assert b["cluster"]["name"] == "prod-A"
    assert b["vms"] == {"total": 3, "powered_on": 2, "sample": ["vm-0", "vm-1"]}
    assert [d["name"] for d in b["datastores"]] == ["ds-ssd-01"]
    assert ("host", "Host memory", "warning") in {
        (a["scope"], a["name"], a["severity"]) for a in b["alarms"]
    }
    assert {s["k"] for s in b["stats"]} == {"CPU", "Memory", "VMs on", "ESXi"}


def test_host_standalone_no_cluster(monkeypatch):
    _install_host(monkeypatch, with_cluster=False)
    b = investigate_host.get_host_investigation_bundle(object(), "esxi-09")
    assert b["cluster"] is None


def test_host_batched_reads_bounded(monkeypatch):
    counter = _install_host(monkeypatch)
    investigate_host.get_host_investigation_bundle(object(), "esxi-09")
    # host + cluster + datastore + vm-rollup (+ no alarms) = at most a handful.
    assert counter["n"] <= 5


# ── Datastore bundle ─────────────────────────────────────────────────────────
def _install_datastore(monkeypatch, *, ds_alarms=()):
    ds_ref, host_ref = object(), object()
    vm_refs = [object(), object()]
    mount = types.SimpleNamespace(key=host_ref)
    counter = {"n": 0}

    def fake_collect_objects(si, objs, obj_type, paths):
        counter["n"] += 1
        if obj_type is vim.Datastore:
            return [
                (
                    ds_ref,
                    {
                        "name": "ds-ssd-01",
                        "summary.type": "VMFS",
                        "summary.freeSpace": 120 * 1024**3,
                        "summary.capacity": 1000 * 1024**3,
                        "summary.accessible": True,
                        "overallStatus": "yellow",
                        "host": [mount],
                        "vm": vm_refs,
                        "triggeredAlarmState": [_alarm(s, n) for s, n in ds_alarms],
                    },
                )
            ]
        if obj_type is vim.HostSystem:
            return [
                (
                    host_ref,
                    {
                        "name": "esxi-09",
                        "runtime.connectionState": "connected",
                        "summary.quickStats.overallCpuUsage": 1000,
                        "summary.quickStats.overallMemoryUsage": 8000,
                        "summary.hardware.cpuMhz": 2500,
                        "summary.hardware.numCpuCores": 8,
                        "summary.hardware.memorySize": 64 * 1024**3,
                        "triggeredAlarmState": [],
                    },
                )
            ]
        if obj_type is vim.VirtualMachine:
            return _vm_rows(vm_refs, [True, False])
        return [(o, {"info.name": getattr(o, "_name", "alarm")}) for o in objs]

    monkeypatch.setattr(
        investigate_datastore,
        "find_datastore_by_name",
        lambda si, n: ds_ref if n == "ds-ssd-01" else None,
    )
    monkeypatch.setattr(investigate_datastore, "_collect_objects", fake_collect_objects)
    monkeypatch.setattr(_correlate, "_collect_objects", fake_collect_objects)
    monkeypatch.setattr(_correlate, "entity_timeline", lambda si, ents, hours=24: [])
    return counter


def test_datastore_unknown_raises_teaching_error(monkeypatch):
    monkeypatch.setattr(investigate_datastore, "find_datastore_by_name", lambda si, n: None)
    with pytest.raises(DatastoreNotFoundError) as exc:
        investigate_datastore.get_datastore_investigation_bundle(object(), "nope")
    assert "list_all_datastores" in str(exc.value)


def test_datastore_bundle_correlates_graph(monkeypatch):
    _install_datastore(monkeypatch, ds_alarms=[("red", "Datastore usage on disk")])
    b = investigate_datastore.get_datastore_investigation_bundle(object(), "ds-ssd-01")
    assert b["object"]["name"] == "ds-ssd-01"
    assert b["object"]["free_pct"] == 12.0
    assert b["object"]["status"] == "yellow"
    # host mount extracted via .key
    assert [h["name"] for h in b["hosts"]] == ["esxi-09"]
    assert b["vms"] == {"total": 2, "powered_on": 1, "sample": ["vm-0"]}
    assert ("datastore", "Datastore usage on disk", "critical") in {
        (a["scope"], a["name"], a["severity"]) for a in b["alarms"]
    }
    # datastore bundle has no snapshots concept
    assert "snapshots" not in b


def test_datastore_html_omits_snapshots_and_escapes(monkeypatch):
    _install_datastore(monkeypatch)
    b = investigate_datastore.get_datastore_investigation_bundle(object(), "ds-ssd-01")
    b["object"]["name"] = "<script>bad</script>"
    html = render_bundle_html(b, "datastore", "vc", datetime(2026, 7, 14, tzinfo=timezone.utc))
    assert "<script>bad" not in html
    assert "&lt;script&gt;bad" in html
    assert "Snapshots" not in html  # snapshots section omitted for datastores
    assert "DATASTORE investigation" in html
