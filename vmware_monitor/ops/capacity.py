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
]
_RP_PROPS = [
    "name",
    "config.cpuAllocation",
    "config.memoryAllocation",
    "summary.quickStats",
]


def get_datastore_capacity(
    si: ServiceInstance,
    limit: int | None = None,
) -> dict:
    """Per-datastore capacity with thin-provisioning over-commit.

    Returns the family list envelope with a real ``total`` (every datastore is
    collected before ``limit`` is applied). Each row has
    name, type, capacity_gb, free_gb, committed_gb (allocated),
    provisioned_gb (committed + uncommitted thin reservations), used_pct, and
    overcommit_pct (provisioned / capacity * 100). overcommit_pct > 100 means
    more space is promised to VMs than the datastore physically has — a thin
    datastore can fill up even while showing free space. Sorted by over-commit
    descending so the riskiest datastores surface first.

    Args:
        si: vSphere ServiceInstance.
        limit: Max number of datastore rows to return (None = all).
    """
    # Batch the summary fields for every datastore in one PropertyCollector call
    # instead of a lazy ds.summary round-trip per datastore (issue #31 class;
    # limit used to apply only after collecting all of them).
    results: list[dict] = []
    for _obj, p in _collect(si, [vim.Datastore], _DS_CAP_PROPS):
        capacity = p.get("summary.capacity") or 0
        free = p.get("summary.freeSpace") or 0
        uncommitted = p.get("summary.uncommitted") or 0
        committed = capacity - free
        provisioned = committed + uncommitted
        used_pct = round(committed / capacity * 100, 1) if capacity else 0.0
        # 0.0 for a datastore with no capacity reading, as before: the rows are
        # sorted and coloured by this number.
        pct = overcommit_pct(capacity, free, uncommitted)
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
            }
        )
    results.sort(key=lambda x: x["overcommit_pct"], reverse=True)
    total = len(results)
    if limit is not None:
        results = results[:limit]
    return paginated(results, limit=limit, total=total)


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
