"""VM investigation bundle: "what is happening around this VM?".

Drill-down from an affected VM into its related infrastructure and recent history
(GitHub issue #31 follow-up). One call collects, filters and *correlates* — the
model never sees raw inventory, it explains the aggregated result in operational
language:

    VM state  ·  host it runs on  ·  cluster context  ·  backing datastores
    snapshots  ·  triggered alarms  ·  live performance  ·  a merged event
    timeline correlating the VM, its host, its cluster and its datastores.

Read-only. All cross-object reads go through the batched ``_collect_objects`` /
``_correlate`` helpers (never a lazy pyVmomi attribute per object) so the bundle
stays cheap even on large fleets — same batching discipline as the #31 fixes.
Point-in-time; no trend is invented.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import sanitize

from vmware_monitor.ops import _correlate
from vmware_monitor.ops._collect import _collect_objects
from vmware_monitor.ops.inventory import find_vm_by_name
from vmware_monitor.ops.performance import get_vm_performance
from vmware_monitor.ops.vm_info import VMNotFoundError, list_snapshots

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

# High-signal VM properties + the morefs needed to walk the object graph. Read in
# one batched call for the single target VM (not a whole-inventory sweep).
_VM_PROPS = [
    "name",
    "runtime.host",
    "runtime.powerState",
    "datastore",
    "summary.config.numCpu",
    "summary.config.memorySizeMB",
    "summary.guest.ipAddress",
    "summary.config.guestFullName",
    "summary.overallStatus",
    "triggeredAlarmState",
]

CUSTOMIZATION_HINT = (
    "Want more or less? Just ask — e.g. "
    '"widen the event window to 72h", "only critical events", '
    '"show the datastore latency", or "render this as an HTML page". '
    "The timeline, context objects, and detail sections are all adjustable."
)


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


def _format_vm(props: dict, fallback_name: str) -> dict:
    """The VM's own high-signal state row from ``_VM_PROPS`` (pure)."""
    return {
        "name": sanitize(props.get("name") or fallback_name),
        "power": str(props.get("runtime.powerState") or "unknown"),
        "cpu": props.get("summary.config.numCpu") or 0,
        "memory_gb": round((props.get("summary.config.memorySizeMB") or 0) / 1024, 1),
        "guest_os": sanitize(props.get("summary.config.guestFullName") or "N/A"),
        "ip": props.get("summary.guest.ipAddress"),
        "status": str(props.get("summary.overallStatus") or "gray"),
    }


def get_vm_investigation_bundle(si: ServiceInstance, vm_name: str, hours: int = 24) -> dict:
    """Correlated investigation bundle for a single VM.

    Args:
        si: vSphere ServiceInstance.
        vm_name: Exact VM name. Unknown names raise a teaching ``VMNotFoundError``.
        hours: Event-timeline look-back window (default 24).

    Returns:
        dict with ``object`` (VM state), ``host``/``cluster`` context (None if not
        placed), ``datastores``, ``snapshots``, ``alarms`` (across VM/host/cluster/
        datastore), ``performance`` (live, or a note when powered off), ``timeline``
        (merged, newest-first, cross-entity), plus ``hours``, ``snapshot`` and
        ``customization_hint``.
    """
    vm = find_vm_by_name(si, vm_name)
    if vm is None:
        raise VMNotFoundError(
            f"VM not found. Run list_virtual_machines (filter by name, e.g. "
            f"'{vm_name[:3]}*') to see available VMs and copy an exact name. "
            f"Requested: '{vm_name}'"
        )

    # Pass 1 — the VM's own props + graph morefs, batched for this one object.
    got = _collect_objects(si, [vm], vim.VirtualMachine, _VM_PROPS)
    vprops = got[0][1] if got else {}
    object_dict = _format_vm(vprops, vm_name)
    host_ref = vprops.get("runtime.host")
    ds_refs = list(vprops.get("datastore") or [])

    # Pass 2 — host context (and, via host.parent, the owning cluster).
    host_dict = None
    host_triggered: list = []
    cluster_ref = None
    if host_ref is not None:
        hgot = _collect_objects(si, [host_ref], vim.HostSystem, _correlate.HOST_CTX_PROPS)
        if hgot:
            hprops = hgot[0][1]
            host_dict = _correlate.format_host(hprops)
            host_triggered = hprops.get("triggeredAlarmState") or []
            parent = hprops.get("parent")
            if parent is not None and _is_cluster(parent):
                cluster_ref = parent

    # Pass 3 — cluster context.
    cluster_dict = None
    cluster_triggered: list = []
    if cluster_ref is not None:
        cgot = _collect_objects(
            si, [cluster_ref], vim.ClusterComputeResource, _correlate.CLUSTER_CTX_PROPS
        )
        if cgot:
            cprops = cgot[0][1]
            cluster_dict = _correlate.format_cluster(cprops)
            cluster_triggered = cprops.get("triggeredAlarmState") or []

    # Pass 4 — backing datastores (one batched read for all of them).
    ds_dicts: list[dict] = []
    ds_entities: list[tuple] = []
    ds_triggered: list[tuple] = []
    if ds_refs:
        for ref, dp in _collect_objects(si, ds_refs, vim.Datastore, _correlate.DS_CTX_PROPS):
            d = _correlate.format_datastore(dp)
            ds_dicts.append(d)
            ds_entities.append(("datastore", d["name"], ref))
            ds_triggered.append(("datastore", d["name"], dp.get("triggeredAlarmState")))

    # Alarms across every correlated entity, names resolved in one batched call.
    host_name = host_dict["name"] if host_dict else ""
    cluster_name = cluster_dict["name"] if cluster_dict else ""
    alarms = _correlate.collect_alarms(
        si,
        [
            ("vm", object_dict["name"], vprops.get("triggeredAlarmState")),
            ("host", host_name, host_triggered),
            ("cluster", cluster_name, cluster_triggered),
            *ds_triggered,
        ],
    )

    # Merged event timeline — VM first so its scope wins de-dup.
    entities = [
        ("vm", object_dict["name"], vm),
        ("host", host_name, host_ref),
        ("cluster", cluster_name, cluster_ref),
        *ds_entities,
    ]
    timeline = _correlate.entity_timeline(si, entities, hours=hours)

    # Snapshots + live performance reuse the existing read-only ops.
    snapshots = list_snapshots(si, vm_name)["items"]
    perf_rows = get_vm_performance(si, vm_name=vm_name, limit=1)["items"]
    performance = (
        perf_rows[0]
        if perf_rows
        else {"note": "no live metrics (VM powered off or no real-time provider)"}
    )

    stats = [
        {"k": "Power", "v": object_dict["power"]},
        {"k": "vCPU", "v": str(object_dict["cpu"])},
        {"k": "Memory", "v": f"{object_dict['memory_gb']:g} GB"},
        {"k": "IP", "v": object_dict["ip"] or "—"},
    ]

    return {
        "object": object_dict,
        "host": host_dict,
        "cluster": cluster_dict,
        "datastores": ds_dicts,
        "snapshots": snapshots,
        "alarms": alarms,
        "performance": performance,
        "timeline": timeline,
        "stats": stats,
        "hours": hours,
        "snapshot": "point-in-time; not a trend (no history retained)",
        "customization_hint": CUSTOMIZATION_HINT,
    }
