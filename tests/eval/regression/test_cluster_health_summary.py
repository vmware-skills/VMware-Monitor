"""Regression — cluster_health_summary aggregation, status rollup, and shape.

Locks the opinionated triage logic added for the issue #31 follow-up discussion:
one aggregated read that rolls hosts / VMs / alarms up to the owning cluster and
assigns an opinionated status. The tool must (a) map hosts and VMs to clusters
via the cluster's own host list, (b) compute CPU/memory % against cluster
capacity, (c) escalate status on disconnected hosts / critical alarms / capacity
pressure / HA-off, (d) honour cluster_filter and include_vms, and (e) always
return the friendly customization_hint.

``_collect`` is monkeypatched so the test exercises the pure aggregation logic
without any pyVmomi SOAP plumbing.
"""

from __future__ import annotations

import types

from pyVmomi import vim

from vmware_monitor.ops import cluster_summary


def _alarm(status: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(overallStatus=status)


def _mk_collect(hosts_by_cluster, host_props, vm_rows):
    """Build a fake _collect dispatching on the managed-object type requested.

    hosts_by_cluster: {cluster_name: (ha, drs, total_cpu_mhz, total_mem_bytes,
                                      [host_ref, ...], [alarm_status, ...])}
    host_props: {host_ref: {conn, cpu_mhz, mem_mb, alarms:[status]}}
    vm_rows: [(host_ref, powerState), ...]
    """

    def fake_collect(si, obj_type, paths):
        t = obj_type[0]
        if t is vim.ClusterComputeResource:
            out = []
            for name, (ha, drs, cpu, mem, hosts, alarms) in hosts_by_cluster.items():
                out.append(
                    (
                        object(),
                        {
                            "name": name,
                            "host": list(hosts),
                            "summary.totalCpu": cpu,
                            "summary.totalMemory": mem,
                            "configuration.dasConfig.enabled": ha,
                            "configuration.drsConfig.enabled": drs,
                            "triggeredAlarmState": [_alarm(a) for a in alarms],
                        },
                    )
                )
            return out
        if t is vim.HostSystem:
            out = []
            for ref, hp in host_props.items():
                out.append(
                    (
                        ref,
                        {
                            "name": hp.get("name", "host"),
                            "runtime.connectionState": hp["conn"],
                            "summary.quickStats.overallCpuUsage": hp["cpu_mhz"],
                            "summary.quickStats.overallMemoryUsage": hp["mem_mb"],
                            "triggeredAlarmState": [_alarm(a) for a in hp.get("alarms", [])],
                        },
                    )
                )
            return out
        if t is vim.VirtualMachine:
            return [(object(), {"runtime.host": h, "runtime.powerState": ps}) for h, ps in vm_rows]
        return []

    return fake_collect


def test_rollup_maps_hosts_vms_alarms_and_escalates_status(monkeypatch):
    ha_host1, ha_host2 = object(), object()
    down_host = object()
    gb = 1024**3
    hosts_by_cluster = {
        # healthy: 2/2 connected, ~50% cpu/mem, no alarms, HA on
        "prod-A": (True, True, 20000.0, 200 * gb, [ha_host1, ha_host2], []),
        # critical: one host disconnected
        "prod-B": (True, True, 10000.0, 100 * gb, [down_host], []),
    }
    host_props = {
        ha_host1: {"conn": "connected", "cpu_mhz": 5000, "mem_mb": 50 * 1024, "alarms": []},
        ha_host2: {"conn": "connected", "cpu_mhz": 5000, "mem_mb": 50 * 1024, "alarms": []},
        down_host: {"conn": "disconnected", "cpu_mhz": 0, "mem_mb": 0, "alarms": []},
    }
    vm_rows = [
        (ha_host1, "poweredOn"),
        (ha_host1, "poweredOff"),
        (ha_host2, "poweredOn"),
        (down_host, "poweredOff"),
    ]
    monkeypatch.setattr(
        cluster_summary, "_collect", _mk_collect(hosts_by_cluster, host_props, vm_rows)
    )

    data = cluster_summary.get_cluster_health_summary(object())

    assert data["customization_hint"] == cluster_summary.CUSTOMIZATION_HINT
    assert "point-in-time" in data["snapshot"]

    rows = {c["name"]: c for c in data["clusters"]}
    # Empty standalone bucket must be dropped.
    assert cluster_summary._STANDALONE not in rows

    a = rows["prod-A"]
    assert (a["hosts_connected"], a["hosts_total"]) == (2, 2)
    assert (a["vms_on"], a["vms_total"]) == (2, 3)
    assert a["cpu_used_pct"] == 50.0  # 10000/20000
    assert a["mem_used_pct"] == 50.0  # 100GB/200GB
    assert a["status"] == "ok"
    assert a["attention"] == []

    b = rows["prod-B"]
    assert (b["hosts_connected"], b["hosts_total"]) == (0, 1)
    assert b["status"] == "critical"
    assert any("disconnected" in r for r in b["attention"])

    # Worst-first ordering + totals.
    assert data["clusters"][0]["name"] == "prod-B"
    t = data["totals"]
    assert t["clusters"] == 2
    assert t["hosts_total"] == 3 and t["hosts_connected"] == 2
    assert t["vms_total"] == 4 and t["vms_on"] == 2
    assert t["worst_status"] == "critical"


def test_capacity_and_alarm_and_ha_rules(monkeypatch):
    h_hot, h_ok, h_ok2 = object(), object(), object()
    gb = 1024**3
    hosts_by_cluster = {
        # CPU 96% -> critical from capacity alone
        "hot": (True, True, 10000.0, 100 * gb, [h_hot], []),
        # multi-host with HA off + a warning alarm -> warn
        "noha": (False, True, 10000.0, 100 * gb, [h_ok, h_ok2], ["yellow"]),
    }
    host_props = {
        h_hot: {"conn": "connected", "cpu_mhz": 9600, "mem_mb": 10 * 1024, "alarms": []},
        h_ok: {"conn": "connected", "cpu_mhz": 1000, "mem_mb": 10 * 1024, "alarms": []},
        h_ok2: {"conn": "connected", "cpu_mhz": 1000, "mem_mb": 10 * 1024, "alarms": []},
    }
    monkeypatch.setattr(cluster_summary, "_collect", _mk_collect(hosts_by_cluster, host_props, []))
    rows = {c["name"]: c for c in cluster_summary.get_cluster_health_summary(object())["clusters"]}
    assert rows["hot"]["status"] == "critical"
    assert any("CPU at 96.0%" in r for r in rows["hot"]["attention"])
    assert rows["noha"]["status"] == "warn"
    assert any("HA disabled" in r for r in rows["noha"]["attention"])
    assert rows["noha"]["alarms"]["warning"] == 1


def test_include_vms_false_skips_vm_fields(monkeypatch):
    h = object()
    hosts_by_cluster = {"c1": (True, True, 10000.0, 100 * 1024**3, [h], [])}
    host_props = {h: {"conn": "connected", "cpu_mhz": 1000, "mem_mb": 1024, "alarms": []}}
    # VM rows present but must be ignored when include_vms=False.
    monkeypatch.setattr(
        cluster_summary,
        "_collect",
        _mk_collect(hosts_by_cluster, host_props, [(h, "poweredOn")]),
    )
    data = cluster_summary.get_cluster_health_summary(object(), include_vms=False)
    assert "vms_total" not in data["clusters"][0]
    assert "vms_total" not in data["totals"]


def test_cluster_filter_substring(monkeypatch):
    h1, h2 = object(), object()
    hosts_by_cluster = {
        "prod-east": (True, True, 10000.0, 100 * 1024**3, [h1], []),
        "dev-west": (True, True, 10000.0, 100 * 1024**3, [h2], []),
    }
    host_props = {
        h1: {"conn": "connected", "cpu_mhz": 1000, "mem_mb": 1024, "alarms": []},
        h2: {"conn": "connected", "cpu_mhz": 1000, "mem_mb": 1024, "alarms": []},
    }
    monkeypatch.setattr(cluster_summary, "_collect", _mk_collect(hosts_by_cluster, host_props, []))
    data = cluster_summary.get_cluster_health_summary(object(), cluster_filter="PROD")
    names = [c["name"] for c in data["clusters"]]
    assert names == ["prod-east"]
    # Standalone bucket is suppressed when filtering.
    assert cluster_summary._STANDALONE not in names


def _alarm_with_ref(status: str, ref: object) -> types.SimpleNamespace:
    return types.SimpleNamespace(overallStatus=status, alarm=ref)


def test_top_issues_ranked_named_and_capped(monkeypatch):
    """Individual anomalies flatten into a ranked, named, capped focus list."""
    hot_host, dev_host = object(), object()
    down_host = object()
    alarm_ref = object()
    gb = 1024**3

    def fake_collect(si, obj_type, paths):
        t = obj_type[0]
        if t is vim.ClusterComputeResource:
            return [
                (
                    object(),
                    {
                        "name": "prod",
                        "host": [hot_host, down_host],
                        "summary.totalCpu": 10000.0,
                        "summary.totalMemory": 100 * gb,
                        "configuration.dasConfig.enabled": True,
                        "configuration.drsConfig.enabled": True,
                        # one critical alarm attached to the cluster
                        "triggeredAlarmState": [_alarm_with_ref("red", alarm_ref)],
                    },
                ),
                (
                    object(),
                    {
                        "name": "dev",
                        "host": [dev_host],
                        "summary.totalCpu": 10000.0,
                        "summary.totalMemory": 100 * gb,
                        "configuration.dasConfig.enabled": True,
                        "configuration.drsConfig.enabled": True,
                        "triggeredAlarmState": [],
                    },
                ),
            ]
        if t is vim.HostSystem:
            return [
                (
                    hot_host,
                    {
                        "name": "esx-hot",
                        "runtime.connectionState": "connected",
                        "summary.quickStats.overallCpuUsage": 3000,
                        "summary.quickStats.overallMemoryUsage": 96 * 1024,  # 96% mem
                        "triggeredAlarmState": [],
                    },
                ),
                (
                    down_host,
                    {
                        "name": "esx-down",
                        "runtime.connectionState": "notResponding",
                        "summary.quickStats.overallCpuUsage": 0,
                        "summary.quickStats.overallMemoryUsage": 0,
                        "triggeredAlarmState": [],
                    },
                ),
                (
                    dev_host,
                    {
                        "name": "esx-dev",
                        "runtime.connectionState": "connected",
                        "summary.quickStats.overallCpuUsage": 100,
                        "summary.quickStats.overallMemoryUsage": 1024,
                        "triggeredAlarmState": [],
                    },
                ),
            ]
        return []

    monkeypatch.setattr(cluster_summary, "_collect", fake_collect)
    monkeypatch.setattr(
        cluster_summary,
        "_collect_objects",
        lambda si, objs, typ, paths: [(alarm_ref, {"info.name": "Host CPU alarm"})],
    )

    data = cluster_summary.get_cluster_health_summary(object(), top_n=2)
    issues = data["top_issues"]
    # 3 anomalies exist (host down, critical alarm, memory 96%); capped to 2.
    assert data["issues_total"] == 3
    assert len(issues) == 2
    # Ranking within the critical band: host_down first, then alarm.
    assert issues[0]["kind"] == "host_down" and issues[0]["object"] == "esx-down"
    assert issues[1]["kind"] == "alarm" and issues[1]["detail"] == "Host CPU alarm"
    # Every issue carries a drill-down hint and cluster attribution.
    assert all(i["drilldown"] and i["cluster"] == "prod" for i in issues)

    # top_n=0 hides the list but still counts.
    d0 = cluster_summary.get_cluster_health_summary(object(), top_n=0)
    assert d0["top_issues"] == [] and d0["issues_total"] == 3


# ── Standalone ESXi (no vCenter) ────────────────────────────────────────────
#
# Found on a real host, 2026-08-29: an ESXi at 70.7% memory with three VMs
# running reported "0/0 VMs on, 0.0% CPU, 0.0% memory, overall OK — no issues
# detected". Every test above builds a cluster, so the standalone path could not
# fail in any of them; the defect needed an environment the suite did not have.
#
# Two causes, one root — the standalone bucket was a second-class citizen. Pass 2
# routed hosts into it, pass 3 did not route VMs into it, and nothing ever gave
# it the capacity denominators that only cluster properties carried.
#
# The severity is not the wrong numbers. It is that the tool whose whole job is
# "is anything on fire?" answered affirmatively that nothing was, on the one
# deployment shape it could not measure.


def _standalone_collect(host_ref, host, vm_rows):
    """No clusters at all — a bare ESXi, which is how many labs and edge sites
    actually run."""

    def fake_collect(si, obj_type, paths):
        t = obj_type[0]
        if t is vim.ClusterComputeResource:
            return []
        if t is vim.HostSystem:
            return [(host_ref, host)]
        if t is vim.VirtualMachine:
            return [
                (object(), {"runtime.host": h, "runtime.powerState": ps})
                for h, ps in vm_rows
            ]
        return []

    return fake_collect


def _standalone_host(cpu_mhz_used, mem_mb_used):
    gb = 1024**3
    return {
        "name": "localhost.localdomain",
        "runtime.connectionState": "connected",
        "summary.quickStats.overallCpuUsage": cpu_mhz_used,
        "summary.quickStats.overallMemoryUsage": mem_mb_used,
        "summary.hardware.cpuMhz": 2500,
        "summary.hardware.numCpuCores": 4,
        "summary.hardware.memorySize": 64 * gb,
        "triggeredAlarmState": [],
    }


def test_standalone_host_counts_its_vms(monkeypatch):
    """The reproduction: three VMs on a host that belongs to no cluster."""
    host_ref = object()
    monkeypatch.setattr(
        cluster_summary,
        "_collect",
        _standalone_collect(
            host_ref,
            _standalone_host(330, 46094),
            [
                (host_ref, "poweredOn"),
                (host_ref, "poweredOn"),
                (host_ref, "poweredOn"),
                (host_ref, "poweredOff"),
            ],
        ),
    )
    out = cluster_summary.get_cluster_health_summary(object())
    [row] = out["clusters"]
    assert row["vms_total"] == 4, "VMs on a standalone host were dropped"
    assert row["vms_on"] == 3


def test_standalone_host_reports_real_utilisation(monkeypatch):
    """Percentages need a denominator, and only cluster properties carried one.
    A standalone host has its own capacity; 0% must not stand in for unmeasured.
    """
    host_ref = object()
    monkeypatch.setattr(
        cluster_summary,
        "_collect",
        _standalone_collect(host_ref, _standalone_host(330, 46094), [(host_ref, "poweredOn")]),
    )
    [row] = cluster_summary.get_cluster_health_summary(object())["clusters"]
    assert 60 < row["mem_used_pct"] < 80, f"memory read as {row['mem_used_pct']}%"
    assert row["cpu_used_pct"] > 0


def test_a_loaded_standalone_host_is_not_reported_as_all_clear(monkeypatch):
    """The part that matters. Whatever the numbers, a host under real memory
    pressure must not come back as 'no issues detected'."""
    host_ref = object()
    monkeypatch.setattr(
        cluster_summary,
        "_collect",
        _standalone_collect(
            host_ref,
            _standalone_host(330, int(0.93 * 64 * 1024)),
            [(host_ref, "poweredOn")],
        ),
    )
    out = cluster_summary.get_cluster_health_summary(object())
    [row] = out["clusters"]
    assert row["status"] != "ok", "93% memory reported as OK"
