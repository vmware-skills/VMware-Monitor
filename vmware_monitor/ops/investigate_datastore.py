"""Datastore investigation bundle: "what is happening around this datastore?".

Drill-down from a datastore into the hosts that mount it, the VMs it backs, and
their correlated recent history (GitHub issue #31 follow-up). One call collects,
filters and correlates so the model explains the aggregated result in operational
language instead of receiving raw inventory.

Read-only. Batched reads only (never a lazy pyVmomi attribute per object).
Point-in-time — live per-datastore latency is intentionally out of scope here
(that is a separate perf report); this bundle answers "what is around it?".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import sanitize

from vmware_monitor.ops import _correlate
from vmware_monitor.ops._collect import _collect_objects
from vmware_monitor.ops.inventory import find_datastore_by_name

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

# High-signal datastore properties + the morefs needed to walk the object graph.
# ``host`` is a list of DatastoreHostMount objects (``.key`` is the HostSystem).
_DS_PROPS = [
    "name",
    "summary.type",
    "summary.freeSpace",
    "summary.capacity",
    "summary.accessible",
    "overallStatus",
    "host",
    "vm",
    "triggeredAlarmState",
]

CUSTOMIZATION_HINT = (
    "Want more or less? Just ask — e.g. "
    '"widen the event window to 72h", "only critical events", '
    '"list every VM on this datastore", or "render this as an HTML page". '
    "The timeline, context objects, and detail sections are all adjustable."
)


class DatastoreNotFoundError(Exception):
    """Raised when a datastore is not found by name (teaching message attached)."""


def _format_datastore_object(props: dict, fallback_name: str) -> dict:
    """The datastore's own high-signal state row (pure)."""
    d = _correlate.format_datastore(props)
    d["name"] = sanitize(props.get("name") or fallback_name)
    d["status"] = str(props.get("overallStatus") or "gray")
    return d


def _mounted_host_refs(mounts: list) -> list:
    """Extract HostSystem morefs from a datastore's ``host`` mount list."""
    refs = []
    for mount in mounts or []:
        ref = getattr(mount, "key", None)
        if ref is not None:
            refs.append(ref)
    return refs


def get_datastore_investigation_bundle(
    si: ServiceInstance, datastore_name: str, hours: int = 24
) -> dict:
    """Correlated investigation bundle for a single datastore.

    Args:
        si: vSphere ServiceInstance.
        datastore_name: Exact datastore name. Unknown names raise a teaching
            ``DatastoreNotFoundError``.
        hours: Event-timeline look-back window (default 24).

    Returns:
        dict with ``object`` (datastore state), ``hosts`` (mounting it), ``vms``
        rollup, ``alarms`` (datastore/host), ``performance`` (note), ``timeline``
        (merged, newest-first), plus ``stats``, ``hours``, ``snapshot`` and
        ``customization_hint``.
    """
    ds = find_datastore_by_name(si, datastore_name)
    if ds is None:
        raise DatastoreNotFoundError(
            f"Datastore not found. Run list_all_datastores to see available "
            f"datastores and copy an exact name. Requested: '{datastore_name}'"
        )

    got = _collect_objects(si, [ds], vim.Datastore, _DS_PROPS)
    dprops = got[0][1] if got else {}
    object_dict = _format_datastore_object(dprops, datastore_name)
    ds_triggered = dprops.get("triggeredAlarmState") or []
    host_refs = _mounted_host_refs(dprops.get("host"))
    vm_refs = list(dprops.get("vm") or [])

    # Hosts mounting the datastore.
    host_dicts: list[dict] = []
    host_entities: list[tuple] = []
    host_triggered: list[tuple] = []
    if host_refs:
        for ref, hp in _collect_objects(si, host_refs, vim.HostSystem, _correlate.HOST_CTX_PROPS):
            h = _correlate.format_host(hp)
            host_dicts.append(h)
            host_entities.append(("host", h["name"], ref))
            host_triggered.append(("host", h["name"], hp.get("triggeredAlarmState")))

    vms = _correlate.summarize_vms(si, vm_refs)

    alarms = _correlate.collect_alarms(
        si,
        [
            ("datastore", object_dict["name"], ds_triggered),
            *host_triggered,
        ],
    )

    entities = [
        ("datastore", object_dict["name"], ds),
        *host_entities,
    ]
    # Two values on purpose: an empty timeline because nothing happened and an
    # empty timeline because this endpoint has no event service must not look
    # the same to whoever reads the bundle.
    timeline, timeline_unavailable = _correlate.entity_timeline(si, entities, hours=hours)

    stats = [
        {"k": "Free", "v": f"{object_dict['free_pct']:g}%"},
        {"k": "Capacity", "v": f"{object_dict['capacity_gb']:g} GB"},
        {"k": "VMs", "v": f"{vms['powered_on']}/{vms['total']} on"},
        {"k": "Hosts", "v": str(len(host_dicts))},
    ]

    return {
        "object": object_dict,
        "hosts": host_dicts,
        "vms": vms,
        "alarms": alarms,
        "performance": {
            "note": "per-datastore latency is a separate perf report; not collected here"
        },
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
