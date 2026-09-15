"""The health summary names a datastore promised past its capacity.

2026-09-14/15, lab vCenter 8.0.3: ``capacity datastores`` showed datastore1
thin-provisioned to 193.5%, then 216.5%, of its 803.2 GB (1738.8 GB promised),
in red — and ``summary``, the command that answers "is anything on fire?",
listed six issues without it. A thin datastore that far over-committed can fill
while it still shows free space, and every VM on it stops together.

The summary now reads datastores in one more batched pass and raises a capacity
issue above the same 100% line the capacity view colours red.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vmware_monitor.ops import capacity, cluster_summary

GB = 1024**3


def _si():
    folder = SimpleNamespace(name="Datacenters", triggeredAlarmState=[])
    return SimpleNamespace(content=SimpleNamespace(rootFolder=folder))


def _datastore(name, capacity_gb, used_pct, provisioned_gb, mounted_on):
    cap = capacity_gb * GB
    free = cap * (1 - used_pct / 100)
    committed = cap - free
    return (
        object(),
        {
            "name": name,
            "summary.capacity": cap,
            "summary.freeSpace": free,
            "summary.uncommitted": provisioned_gb * GB - committed,
            "host": [SimpleNamespace(key=h) for h in mounted_on],
        },
    )


def _collect_with(host_ref, datastores, clusters=(), paths_seen=None):
    def fake_collect(si, obj_type, paths):
        t = obj_type[0]
        if paths_seen is not None:
            paths_seen[t] = list(paths)
        if t is vim.ClusterComputeResource:
            return list(clusters)
        if t is vim.HostSystem:
            return [
                (
                    host_ref,
                    {
                        "name": "192.168.60.15",
                        "runtime.connectionState": "connected",
                        "summary.quickStats.overallCpuUsage": 100,
                        "summary.quickStats.overallMemoryUsage": 1024,
                        "summary.hardware.cpuMhz": 2800,
                        "summary.hardware.numCpuCores": 8,
                        "summary.hardware.memorySize": 64 * GB,
                        "triggeredAlarmState": [],
                    },
                )
            ]
        if t is vim.Datastore:
            return list(datastores)
        return []

    return fake_collect


def _issues(data):
    return [i for i in data["top_issues"] if i.get("scope") == "datastore"]


@pytest.mark.unit
def test_an_overcommitted_datastore_is_a_top_issue(monkeypatch):
    host = object()
    stores = [
        _datastore("datastore1", 803.2, 59.8, 1738.8, [host]),
        _datastore("datastore1 (1)", 432.5, 73.0, 315.9, [host]),
    ]
    seen: dict = {}
    monkeypatch.setattr(cluster_summary, "_collect", _collect_with(host, stores, paths_seen=seen))
    data = cluster_summary.get_cluster_health_summary(_si(), include_vms=False)

    [issue] = _issues(data)
    assert issue["object"] == "datastore1"
    assert issue["kind"] == "capacity"
    assert issue["severity"] == "warning"
    assert issue["cluster"] == "(standalone hosts)"
    assert "216.5%" in issue["detail"] and "1738.8" in issue["detail"] and "803.2" in issue["detail"]
    assert "datastore_capacity" in issue["drilldown"]
    assert {"summary.capacity", "summary.freeSpace", "summary.uncommitted", "host"} <= set(seen[vim.Datastore])


@pytest.mark.unit
def test_the_line_is_the_capacity_views_own_threshold(monkeypatch):
    host = object()
    at = capacity.DATASTORE_OVERCOMMIT_WARN_PCT
    stores = [
        _datastore("at-line", 100.0, 50.0, at, [host]),
        _datastore("over-line", 100.0, 50.0, at + 0.2, [host]),
    ]
    monkeypatch.setattr(cluster_summary, "_collect", _collect_with(host, stores))
    data = cluster_summary.get_cluster_health_summary(_si(), include_vms=False)
    assert [i["object"] for i in _issues(data)] == ["over-line"]


@pytest.mark.unit
def test_a_cluster_filter_leaves_out_datastores_outside_it(monkeypatch):
    host = object()
    stores = [_datastore("datastore1", 803.2, 59.8, 1738.8, [host])]
    monkeypatch.setattr(cluster_summary, "_collect", _collect_with(host, stores))
    data = cluster_summary.get_cluster_health_summary(_si(), cluster_filter="prod", include_vms=False)
    assert _issues(data) == []


@pytest.mark.unit
def test_an_unmounted_datastore_is_not_filed_under_standalone_hosts(monkeypatch):
    """No host mounts it, so no row owns it; the standalone row is hosts, not leftovers."""
    host = object()
    stores = [_datastore("orphan", 803.2, 59.8, 1738.8, [])]
    monkeypatch.setattr(cluster_summary, "_collect", _collect_with(host, stores))
    data = cluster_summary.get_cluster_health_summary(_si(), include_vms=False)
    assert _issues(data) == []


@pytest.mark.unit
def test_a_datastore_with_no_capacity_reading_is_skipped_not_divided(monkeypatch):
    host = object()
    broken = (object(), {"name": "ghost", "summary.capacity": 0, "summary.freeSpace": 0,
                         "summary.uncommitted": 5 * GB, "host": [SimpleNamespace(key=host)]})
    monkeypatch.setattr(cluster_summary, "_collect", _collect_with(host, [broken]))
    data = cluster_summary.get_cluster_health_summary(_si(), include_vms=False)
    assert _issues(data) == []


@pytest.mark.unit
def test_the_capacity_view_and_the_summary_compute_the_same_percentage():
    host = object()
    _obj, props = _datastore("datastore1", 803.2, 59.8, 1738.8, [host])
    assert capacity.overcommit_pct(
        props["summary.capacity"], props["summary.freeSpace"], props["summary.uncommitted"]
    ) == 216.5
