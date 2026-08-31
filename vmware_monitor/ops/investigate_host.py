"""Host investigation bundle: "what is happening around this ESXi host?".

Drill-down from an affected host into its cluster, the VMs it runs, the datastores
it mounts, and their correlated recent history (GitHub issue #31 follow-up). One
call collects, filters and correlates so the model explains the aggregated result
in operational language instead of receiving raw inventory.

Read-only. All cross-object reads go through the batched ``_collect_objects`` /
``_correlate`` helpers (never a lazy pyVmomi attribute per object). Point-in-time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import sanitize

from vmware_monitor.ops import _correlate
from vmware_monitor.ops._collect import _collect_objects
from vmware_monitor.ops.inventory import find_host_by_name
from vmware_monitor.ops.performance import get_host_performance

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

# High-signal host properties + the morefs needed to walk the object graph.
_HOST_PROPS = [
    "name",
    "parent",
    "vm",
    "datastore",
    "runtime.connectionState",
    "summary.quickStats.overallCpuUsage",
    "summary.quickStats.overallMemoryUsage",
    "summary.quickStats.uptime",
    "summary.hardware.cpuMhz",
    "summary.hardware.numCpuCores",
    "summary.hardware.memorySize",
    "summary.overallStatus",
    "config.product.version",
    "triggeredAlarmState",
]

CUSTOMIZATION_HINT = (
    "Want more or less? Just ask — e.g. "
    '"widen the event window to 72h", "only critical events", '
    '"list every VM on this host", or "render this as an HTML page". '
    "The timeline, context objects, and detail sections are all adjustable."
)


class HostNotFoundError(Exception):
    """Raised when a host is not found by name (teaching message attached)."""


def _is_cluster(ref: object) -> bool:
    """True if a host's ``parent`` is a real cluster (not a standalone ComputeResource).

    Uses ``isinstance``. The previous form compared ``type(ref).__name__`` against
    the bare leaf name, and pyVmomi names its types with the fully-qualified VMODL
    name — on real hardware that attribute reads ``"vim.ClusterComputeResource"``,
    so the comparison was False for every host that has ever existed. It never
    raised; it simply never matched, so ``cluster`` came back ``null`` on every
    bundle and every cluster-scoped alarm was dropped — 9 of the 11 on the host
    this was caught with (真机 vCenter 9.1, 2026-08-31; the qualified name was
    reproduced locally on 8.0.3, so it is not a 9.x behaviour).

    Why it was written that way is the actual root cause and is worth keeping:
    it let the pure-aggregation tests pass a lightweight
    ``type("ClusterComputeResource", (), {})()`` sentinel instead of a pyVmomi
    object. The test could only pass with a fake that nothing real resembles
    (形态 #3 — verified in an environment where the defect cannot appear). The
    tests now build ``vim.ClusterComputeResource("domain-cN")``, which pyVmomi
    constructs without a live connection, so fake and field agree again.
    """
    return isinstance(ref, vim.ClusterComputeResource)


def _format_host_object(props: dict, fallback_name: str) -> dict:
    """The host's own high-signal state row (pure)."""
    total_mhz = (props.get("summary.hardware.cpuMhz") or 0) * (
        props.get("summary.hardware.numCpuCores") or 0
    )
    mem_used = (props.get("summary.quickStats.overallMemoryUsage") or 0) * 1024 * 1024
    uptime_s = props.get("summary.quickStats.uptime") or 0
    return {
        "name": sanitize(props.get("name") or fallback_name),
        "connection": str(props.get("runtime.connectionState") or "unknown"),
        "cpu_pct": _correlate.pct(props.get("summary.quickStats.overallCpuUsage") or 0, total_mhz),
        "mem_pct": _correlate.pct(mem_used, props.get("summary.hardware.memorySize") or 0),
        "uptime_hours": round(uptime_s / 3600, 1),
        "version": str(props.get("config.product.version") or "N/A"),
        "status": str(props.get("summary.overallStatus") or "gray"),
    }


def get_host_investigation_bundle(si: ServiceInstance, host_name: str, hours: int = 24) -> dict:
    """Correlated investigation bundle for a single ESXi host.

    Args:
        si: vSphere ServiceInstance.
        host_name: Exact host name. Unknown names raise a teaching ``HostNotFoundError``.
        hours: Event-timeline look-back window (default 24).

    Returns:
        dict with ``object`` (host state), ``cluster`` context, ``vms`` rollup,
        ``datastores``, ``alarms`` (host/cluster/datastore), ``performance``,
        ``timeline`` (merged, newest-first), plus ``stats``, ``hours``, ``snapshot``
        and ``customization_hint``.
    """
    host = find_host_by_name(si, host_name)
    if host is None:
        raise HostNotFoundError(
            f"Host not found. Run list_esxi_hosts to see available hosts and copy "
            f"an exact name. Requested: '{host_name}'"
        )

    got = _collect_objects(si, [host], vim.HostSystem, _HOST_PROPS)
    hprops = got[0][1] if got else {}
    object_dict = _format_host_object(hprops, host_name)
    host_triggered = hprops.get("triggeredAlarmState") or []
    vm_refs = list(hprops.get("vm") or [])
    ds_refs = list(hprops.get("datastore") or [])

    # Cluster context via host.parent.
    cluster_dict = None
    cluster_ref = None
    cluster_triggered: list = []
    parent = hprops.get("parent")
    if parent is not None and _is_cluster(parent):
        cluster_ref = parent
        cgot = _collect_objects(
            si, [cluster_ref], vim.ClusterComputeResource, _correlate.CLUSTER_CTX_PROPS
        )
        if cgot:
            cprops = cgot[0][1]
            cluster_dict = _correlate.format_cluster(cprops)
            cluster_triggered = cprops.get("triggeredAlarmState") or []

    # VMs on the host + mounted datastores.
    vms = _correlate.summarize_vms(si, vm_refs)
    ds_dicts: list[dict] = []
    ds_entities: list[tuple] = []
    ds_triggered: list[tuple] = []
    if ds_refs:
        for ref, dp in _collect_objects(si, ds_refs, vim.Datastore, _correlate.DS_CTX_PROPS):
            d = _correlate.format_datastore(dp)
            ds_dicts.append(d)
            ds_entities.append(("datastore", d["name"], ref))
            ds_triggered.append(("datastore", d["name"], dp.get("triggeredAlarmState")))

    cluster_name = cluster_dict["name"] if cluster_dict else ""
    alarms = _correlate.collect_alarms(
        si,
        [
            ("host", object_dict["name"], host_triggered),
            ("cluster", cluster_name, cluster_triggered),
            *ds_triggered,
        ],
    )

    entities = [
        ("host", object_dict["name"], host),
        ("cluster", cluster_name, cluster_ref),
        *ds_entities,
    ]
    # Two values on purpose: an empty timeline because nothing happened and an
    # empty timeline because this endpoint has no event service must not look
    # the same to whoever reads the bundle.
    timeline, timeline_unavailable = _correlate.entity_timeline(si, entities, hours=hours)

    perf_rows = get_host_performance(si, host_name=host_name, limit=1)["items"]
    performance = (
        perf_rows[0]
        if perf_rows
        else {"note": "no live metrics (host disconnected or no provider)"}
    )

    stats = [
        {"k": "CPU", "v": f"{object_dict['cpu_pct']:g}%"},
        {"k": "Memory", "v": f"{object_dict['mem_pct']:g}%"},
        {"k": "VMs on", "v": f"{vms['powered_on']}/{vms['total']}"},
        {"k": "ESXi", "v": object_dict["version"]},
    ]

    return {
        "object": object_dict,
        "cluster": cluster_dict,
        "vms": vms,
        "datastores": ds_dicts,
        "alarms": alarms,
        "performance": performance,
        "timeline": timeline,
        # None when the timeline was read. A sentence when this endpoint serves
        # no event history -- which a standalone ESXi does by exposing an event
        # manager and then refusing QueryEvents. Before this, that raised a raw
        # vmodl.fault.NotImplemented at the operator after every other read had
        # already succeeded, on 4 of the 5 targets configured here.
        "timeline_unavailable": timeline_unavailable,
        "stats": stats,
        "hours": hours,
        "snapshot": "point-in-time; not a trend (no history retained)",
        "customization_hint": CUSTOMIZATION_HINT,
    }
