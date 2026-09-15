"""Capacity analytics: datastore over-commit and resource-pool usage (read-only).

Inventory's `list_datastores` returns free/total only. This module adds the
operational risk signal: thin-provisioning over-commit (provisioned space that
exceeds physical capacity) and resource-pool reservation/usage. Both are
point-in-time — see honesty note.

Read-only.

Honesty note: these are *snapshots*, not trends. True capacity trending
(growth rate, days-until-full) needs retained history, which this skill does
not store — pair vCenter/Aria with a metrics store for that. We compute
over-commit from current values and never extrapolate a fake runway date.

Whose view: ``summary.uncommitted`` is summed by the endpoint over the VMs *it*
has registered on the datastore. On the lab (2026-09-15) the same datastore1 read
1734.0 GB provisioned through vCenter and 1094.9 GB through the ESXi host that
mounts it: vCenter held two powered-off VMs on that host which the host itself no
longer had registered. Both figures are right for their endpoint, so the output
says which endpoint it is and how many VMs each figure covers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import paginated, sanitize

from vmware_monitor.ops._collect import _collect

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

_GB = 1024**3

#: Provisioned space above this share of a datastore's capacity is flagged: more
#: is promised to VMs than exists, so a thin datastore can fill while it still
#: shows free space. The capacity view colours it red and the health summary
#: raises it as an issue — one number for both.
DATASTORE_OVERCOMMIT_WARN_PCT = 100.0


def overcommit_pct(capacity: float, free: float, uncommitted: float) -> float | None:
    """Provisioned space (committed + uncommitted) as a percentage of capacity, 1 dp.

    ``None`` when capacity is zero or unknown: there is nothing to divide by, and
    0.0 would read as "nothing provisioned".
    """
    if not capacity:
        return None
    return round(((capacity - free) + uncommitted) / capacity * 100, 1)

_DS_CAP_PROPS = [
    "name",
    "summary.type",
    "summary.capacity",
    "summary.freeSpace",
    "summary.uncommitted",
    "vm",
]
_RP_PROPS = [
    "name",
    "config.cpuAllocation",
    "config.memoryAllocation",
    "summary.quickStats",
]

_API_TYPE_VIEW = {"VirtualCenter": "vCenter", "HostAgent": "ESXi host"}

_VIEW_NOTES = {
    "vCenter": (
        "vCenter view: provisioned_gb is summed by vCenter over every VM it has "
        "registered on each datastore (vm_count). A direct ESXi target sums only the "
        "VMs registered on that host, so the same datastore can read lower there — "
        "for example when vCenter still holds VMs the host no longer has registered "
        "(see vms_not_connected). Committed space is measured on disk and agrees "
        "across views."
    ),
    "ESXi host": (
        "ESXi host view: provisioned_gb is summed by this host over the VMs "
        "registered on it (vm_count). A vCenter managing this host can read higher "
        "for the same datastore when it has VMs registered there that this host does "
        "not list. Committed space is measured on disk and agrees across views."
    ),
    "unknown": (
        "Endpoint type could not be read. provisioned_gb is summed over the VMs the "
        "endpoint has registered on each datastore (vm_count); a vCenter and a "
        "directly-reached ESXi host can report different figures for one datastore."
    ),
}


def _endpoint_view(si: ServiceInstance) -> str:
    try:
        return _API_TYPE_VIEW.get(str(si.content.about.apiType), "unknown")
    except Exception:  # noqa: BLE001 — an unreadable endpoint kind is not guessed
        return "unknown"


def _vm_states(si: ServiceInstance) -> dict | None:
    """``{vm ref: (name, connectionState)}`` in one batched read; ``None`` if unreadable."""
    try:
        return {
            ref: (p.get("name", ""), str(p.get("runtime.connectionState", "")))
            for ref, p in _collect(si, [vim.VirtualMachine], ["name", "runtime.connectionState"])
        }
    except Exception:  # noqa: BLE001 — degrade to "not read", never to "all connected"
        return None


def _not_connected(refs: list, states: dict | None) -> list[str] | None:
    """Names (with state) of VMs on the datastore not reported as connected.

    ``None`` when the states could not be read or a VM on the datastore is missing
    from them — an empty list would claim every VM was checked.
    """
    if not refs:
        return []
    if states is None or any(ref not in states for ref in refs):
        return None
    return [
        f"{sanitize(states[ref][0])} ({sanitize(states[ref][1])})"
        for ref in refs
        if states[ref][1] != "connected"
    ]


def get_datastore_capacity(
    si: ServiceInstance,
    limit: int | None = None,
) -> dict:
    """Per-datastore capacity with thin-provisioning over-commit.

    Returns the family list envelope with a real ``total`` (every datastore is
    collected before ``limit`` is applied). Each row has
    name, type, capacity_gb, free_gb, committed_gb (allocated),
    provisioned_gb (committed + uncommitted thin reservations), used_pct,
    overcommit_pct (provisioned / capacity * 100), vm_count (VMs the endpoint has
    registered on it — the ones provisioned_gb covers) and vms_not_connected (those
    the endpoint reports as orphaned / inaccessible / disconnected; ``None`` when
    unreadable). overcommit_pct > 100 means
    more space is promised to VMs than the datastore physically has — a thin
    datastore can fill up even while showing free space. Sorted by over-commit
    descending so the riskiest datastores surface first.

    The envelope's ``view`` (vCenter / ESXi host / unknown) and ``view_note`` say
    whose figure this is: a vCenter and a directly-reached ESXi host can report
    different provisioned space for the same datastore.

    Args:
        si: vSphere ServiceInstance.
        limit: Max number of datastore rows to return (None = all).
    """
    # Batch the summary fields for every datastore in one PropertyCollector call
    # instead of a lazy ds.summary round-trip per datastore (issue #31 class;
    # limit used to apply only after collecting all of them).
    collected = _collect(si, [vim.Datastore], _DS_CAP_PROPS)
    states = _vm_states(si) if any(p.get("vm") for _obj, p in collected) else {}
    results: list[dict] = []
    for _obj, p in collected:
        capacity = p.get("summary.capacity") or 0
        free = p.get("summary.freeSpace") or 0
        uncommitted = p.get("summary.uncommitted") or 0
        committed = capacity - free
        provisioned = committed + uncommitted
        used_pct = round(committed / capacity * 100, 1) if capacity else 0.0
        # 0.0 for a datastore with no capacity reading, as before: the rows are
        # sorted and coloured by this number.
        pct = overcommit_pct(capacity, free, uncommitted)
        vm_refs = list(p.get("vm") or [])
        results.append(
            {
                "name": sanitize(p.get("name", "")),
                "type": p.get("summary.type"),
                "capacity_gb": round(capacity / _GB, 1),
                "free_gb": round(free / _GB, 1),
                "committed_gb": round(committed / _GB, 1),
                "provisioned_gb": round(provisioned / _GB, 1),
                "used_pct": used_pct,
                "overcommit_pct": pct if pct is not None else 0.0,
                "vm_count": len(vm_refs),
                "vms_not_connected": _not_connected(vm_refs, states),
            }
        )
    results.sort(key=lambda x: x["overcommit_pct"], reverse=True)
    total = len(results)
    if limit is not None:
        results = results[:limit]
    view = _endpoint_view(si)
    return paginated(results, limit=limit, total=total, view=view, view_note=_VIEW_NOTES[view])


def get_resource_pool_usage(
    si: ServiceInstance,
    limit: int | None = None,
) -> dict:
    """Per-resource-pool CPU/memory reservation, limit, and current usage.

    Returns the family list envelope with a real ``total`` (every pool is
    collected before ``limit`` is applied). Each row has
    name, cpu_reservation_mhz, cpu_limit_mhz, cpu_usage_mhz,
    mem_reservation_mb, mem_limit_mb, mem_usage_mb. A limit of -1 means
    unlimited (vSphere's sentinel). The implicit cluster root pool ("Resources")
    is included. Sorted by memory usage descending.

    Args:
        si: vSphere ServiceInstance.
        limit: Max number of pool rows to return (None = all).
    """
    # Batch config allocations + quickStats for every pool in one
    # PropertyCollector call instead of lazy pool.config / pool.summary reads per
    # pool (issue #31 class).
    results: list[dict] = []
    for _obj, p in _collect(si, [vim.ResourcePool], _RP_PROPS):
        cpu_alloc = p.get("config.cpuAllocation")
        mem_alloc = p.get("config.memoryAllocation")
        qs = p.get("summary.quickStats")
        results.append(
            {
                "name": sanitize(p.get("name", "")),
                "cpu_reservation_mhz": cpu_alloc.reservation if cpu_alloc else 0,
                "cpu_limit_mhz": cpu_alloc.limit if cpu_alloc else -1,
                "cpu_usage_mhz": qs.overallCpuUsage if qs else 0,
                "mem_reservation_mb": mem_alloc.reservation if mem_alloc else 0,
                "mem_limit_mb": mem_alloc.limit if mem_alloc else -1,
                "mem_usage_mb": qs.guestMemoryUsage if qs else 0,
            }
        )
    results.sort(key=lambda x: x["mem_usage_mb"], reverse=True)
    total = len(results)
    if limit is not None:
        results = results[:limit]
    return paginated(results, limit=limit, total=total)
