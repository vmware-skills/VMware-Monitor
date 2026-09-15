"""The health summary names a datastore promised past its capacity.

2026-09-14/15, lab vCenter 8.0.3: ``capacity datastores`` showed datastore1
thin-provisioned to 193.5%, then 216.5%, of its 803.2 GB (1738.8 GB promised),
in red — and ``summary``, the command that answers "is anything on fire?",
listed six issues without it. A thin datastore that far over-committed can fill
while it still shows free space, and every VM on it stops together.

The summary now reads datastores' capacity in one more batched pass, reads the
mount list only for the over-committed ones, raises a capacity issue above the
same 100% line the capacity view colours red, and moves the owning row's status
with it (review, 2026-09-15: the first version left the row reading OK beside
its own warning).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vmware_monitor.ops import capacity, cluster_summary, health_html

GB = 1024**3


def _si():
    folder = SimpleNamespace(name="Datacenters", triggeredAlarmState=[])
    return SimpleNamespace(content=SimpleNamespace(rootFolder=folder))


def _mount(host, mounted=True):
    return SimpleNamespace(key=host, mountInfo=SimpleNamespace(mounted=mounted))


def _datastore(name, capacity_gb, used_pct, provisioned_gb, mounts):
    cap = capacity_gb * GB
    free = cap * (1 - used_pct / 100)
    committed = cap - free
    props = {
        "name": name,
        "summary.capacity": cap,
        "summary.freeSpace": free,
        "summary.uncommitted": provisioned_gb * GB - committed,
    }
    return (object(), props, mounts)


def _host(name="192.168.60.15"):
    return {
        "name": name,
        "runtime.connectionState": "connected",
        "summary.quickStats.overallCpuUsage": 100,
        "summary.quickStats.overallMemoryUsage": 1024,
        "summary.hardware.cpuMhz": 2800,
        "summary.hardware.numCpuCores": 8,
        "summary.hardware.memorySize": 64 * GB,
        "triggeredAlarmState": [],
    }


def _install(monkeypatch, host_ref, datastores, clusters=(), fail=None):
    """Fake both batched reads; returns what they were asked for."""
    asked = {"paths": {}, "mount_refs": []}
    mounts = {obj: m for obj, _p, m in datastores}

    def fake_collect(si, obj_type, paths):
        t = obj_type[0]
        asked["paths"][t] = list(paths)
        if t is vim.ClusterComputeResource:
            return list(clusters)
        if t is vim.HostSystem:
            return [(host_ref, _host())]
        if t is vim.Datastore:
            if fail:
                raise fail
            return [(obj, p) for obj, p, _m in datastores]
        return []

    def fake_collect_objects(si, refs, kind, paths):
        if kind is vim.alarm.Alarm:  # the summary's alarm-name lookup; no alarms here
            return []
        assert kind is vim.Datastore and paths == ["host"], (kind, paths)
        asked["mount_refs"].extend(refs)
        return [(ref, {"host": mounts[ref]}) for ref in refs]

    monkeypatch.setattr(cluster_summary, "_collect", fake_collect)
    monkeypatch.setattr(cluster_summary, "_collect_objects", fake_collect_objects)
    return asked


def _issues(data):
    return [i for i in data["top_issues"] if i.get("scope") == "datastore"]


@pytest.mark.unit
def test_an_overcommitted_datastore_is_a_top_issue(monkeypatch):
    host = object()
    over = _datastore("datastore1", 803.2, 59.8, 1738.8, [_mount(host)])
    under = _datastore("datastore1 (1)", 432.5, 73.0, 315.9, [_mount(host)])
    asked = _install(monkeypatch, host, [over, under])
    data = cluster_summary.get_cluster_health_summary(_si(), include_vms=False)

    [issue] = _issues(data)
    assert issue["object"] == "datastore1"
    assert issue["kind"] == "capacity" and issue["severity"] == "warning"
    assert issue["cluster"] == "(standalone hosts)"
    assert "216.5%" in issue["detail"] and "1738.8" in issue["detail"] and "803.2" in issue["detail"]
    assert "datastore_capacity" in issue["drilldown"]


@pytest.mark.unit
def test_mount_lists_are_read_only_for_overcommitted_datastores(monkeypatch):
    host = object()
    over = _datastore("datastore1", 803.2, 59.8, 1738.8, [_mount(host)])
    under = _datastore("datastore1 (1)", 432.5, 73.0, 315.9, [_mount(host)])
    asked = _install(monkeypatch, host, [over, under])
    cluster_summary.get_cluster_health_summary(_si(), include_vms=False)
    assert "host" not in asked["paths"][vim.Datastore]
    assert {"summary.capacity", "summary.freeSpace", "summary.uncommitted"} <= set(asked["paths"][vim.Datastore])
    assert asked["mount_refs"] == [over[0]]


@pytest.mark.unit
def test_the_owning_row_and_the_overall_verdict_move_with_the_issue(monkeypatch):
    host = object()
    _install(monkeypatch, host, [_datastore("datastore1", 803.2, 59.8, 1738.8, [_mount(host)])])
    data = cluster_summary.get_cluster_health_summary(_si(), include_vms=False)
    [row] = [c for c in data["clusters"] if c["name"] == "(standalone hosts)"]
    assert row["status"] == "warn"
    assert any("datastore1" in a and "216.5%" in a for a in row["attention"])
    assert data["totals"]["worst_status"] == "warn"


@pytest.mark.unit
def test_a_datastore_on_clustered_hosts_is_filed_under_that_cluster(monkeypatch):
    host = object()
    prod = (
        object(),
        {
            "name": "prod",
            "host": [host],
            "summary.totalCpu": 22400.0,
            "summary.totalMemory": 64 * GB,
            "configuration.dasConfig.enabled": True,
            "configuration.drsConfig.enabled": True,
            "triggeredAlarmState": [],
        },
    )
    _install(monkeypatch, host, [_datastore("vsan-prod", 1000.0, 50.0, 1500.0, [_mount(host)])], clusters=[prod])
    data = cluster_summary.get_cluster_health_summary(_si(), include_vms=False)
    [issue] = _issues(data)
    assert issue["cluster"] == "prod"
    assert {c["name"]: c["status"] for c in data["clusters"]}["prod"] == "warn"


@pytest.mark.unit
def test_the_line_is_the_capacity_views_own_threshold(monkeypatch):
    host = object()
    at = capacity.DATASTORE_OVERCOMMIT_WARN_PCT
    stores = [
        _datastore("at-line", 100.0, 50.0, at, [_mount(host)]),
        _datastore("over-line", 100.0, 50.0, at + 0.2, [_mount(host)]),
    ]
    _install(monkeypatch, host, stores)
    data = cluster_summary.get_cluster_health_summary(_si(), include_vms=False)
    assert [i["object"] for i in _issues(data)] == ["over-line"]


@pytest.mark.unit
def test_a_cluster_filter_leaves_out_datastores_outside_it(monkeypatch):
    host = object()
    _install(monkeypatch, host, [_datastore("datastore1", 803.2, 59.8, 1738.8, [_mount(host)])])
    data = cluster_summary.get_cluster_health_summary(_si(), cluster_filter="prod", include_vms=False)
    assert _issues(data) == []


@pytest.mark.unit
@pytest.mark.parametrize("mounts", [[], "unmounted"])
def test_a_datastore_no_host_has_mounted_is_not_filed_under_standalone_hosts(monkeypatch, mounts):
    """No host mounts it, so no row owns it; the standalone row is hosts, not leftovers."""
    host = object()
    records = [_mount(host, mounted=False)] if mounts == "unmounted" else []
    _install(monkeypatch, host, [_datastore("orphan", 803.2, 59.8, 1738.8, records)])
    data = cluster_summary.get_cluster_health_summary(_si(), include_vms=False)
    assert _issues(data) == []


@pytest.mark.unit
def test_a_datastore_with_no_capacity_reading_is_skipped_not_divided(monkeypatch):
    host = object()
    broken = (object(), {"name": "ghost", "summary.capacity": 0, "summary.freeSpace": 0,
                         "summary.uncommitted": 5 * GB}, [_mount(host)])
    _install(monkeypatch, host, [broken])
    data = cluster_summary.get_cluster_health_summary(_si(), include_vms=False)
    assert _issues(data) == []


@pytest.mark.unit
def test_a_failed_datastore_read_degrades_to_a_visible_warning(monkeypatch):
    host = object()
    _install(monkeypatch, host, [], fail=RuntimeError("NoPermission"))
    data = cluster_summary.get_cluster_health_summary(_si(), include_vms=False)
    assert data["totals"]["hosts_total"] == 1, "the hosts already read must survive"
    [warning] = [i for i in data["top_issues"] if i["object"] == "datastores"]
    assert "could not be read" in warning["detail"] and "NoPermission" in warning["detail"]


@pytest.mark.unit
def test_the_html_page_calls_it_a_datastore(monkeypatch):
    host = object()
    _install(monkeypatch, host, [_datastore("datastore1", 803.2, 59.8, 1738.8, [_mount(host)])])
    [issue] = _issues(cluster_summary.get_cluster_health_summary(_si(), include_vms=False))
    row = health_html._issue_row(1, issue)
    assert "datastore <b>datastore1</b>" in row
    assert "cluster <b>datastore1</b>" not in row


@pytest.mark.unit
def test_the_capacity_view_and_the_summary_report_the_same_percentage(monkeypatch):
    host = object()
    store = _datastore("datastore1", 803.2, 59.8, 1738.8, [_mount(host)])
    _install(monkeypatch, host, [store])
    [issue] = _issues(cluster_summary.get_cluster_health_summary(_si(), include_vms=False))
    monkeypatch.setattr(
        capacity, "_collect", lambda si, types, paths: [(store[0], {**store[1], "summary.type": "VMFS"})]
    )
    [row] = capacity.get_datastore_capacity(None)["items"]
    assert row["overcommit_pct"] == 216.5
    assert f"{row['overcommit_pct']}%" in issue["detail"]
