"""Inventory-wide snapshot aging and sprawl analysis (read-only).

`ops.vm_info.list_snapshots` lists snapshots for *one* VM but does no
judgement. This module sweeps the whole inventory to surface the operational
risk that snapshots create: forgotten old snapshots that silently consume
datastore space and slow down I/O.

Read-only — it never creates, reverts, or deletes a snapshot. Remediation is
routed to vmware-aiops (which owns snapshot delete/consolidate).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import sanitize

from vmware_monitor.ops._collect import _collect

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

# Snapshot-attributable files in vm.layoutEx — used to estimate the space a
# VM's snapshots consume. Delta-disk growth is not separable per-snapshot from
# the public API, so this is a deliberate lower-bound proxy, labelled as such.
_SNAPSHOT_FILE_TYPES = {"snapshotData", "snapshotMemory"}

DEFAULT_AGE_THRESHOLD_DAYS = 30


def _walk_snapshots(nodes: list, vm_name: str, now: datetime, level: int = 0) -> list[dict]:
    """Flatten a snapshot tree into rows with computed age in days."""
    rows: list[dict] = []
    for node in nodes:
        created = node.createTime
        age_days = None
        if created is not None:
            # createTime is tz-aware UTC; guard against naive just in case.
            ct = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
            age_days = round((now - ct).total_seconds() / 86400, 1)
        rows.append(
            {
                "vm_name": vm_name,
                "snapshot_name": sanitize(node.name),
                "description": sanitize(node.description, max_len=200),
                "created": str(created),
                "age_days": age_days,
                "level": level,
            }
        )
        children = getattr(node, "childSnapshotList", None)
        if children:
            rows.extend(_walk_snapshots(children, vm_name, now, level + 1))
    return rows


def _snapshot_size_mb(layout: vim.vm.FileLayoutEx | None) -> float:
    """Lower-bound estimate of snapshot space for a VM, in MB.

    Sums snapshotData/snapshotMemory files from a prefetched ``layoutEx`` object.
    Returns 0.0 when the layout is unavailable. This under-counts delta-disk
    growth — surfaced to the user as an estimate, never as an exact figure.
    """
    if not layout or not getattr(layout, "file", None):
        return 0.0
    total = 0
    for f in layout.file:
        if getattr(f, "type", None) in _SNAPSHOT_FILE_TYPES:
            total += getattr(f, "size", 0) or 0
    return round(total / (1024 * 1024), 1)


def list_snapshot_aging(
    si: ServiceInstance,
    age_threshold_days: int = DEFAULT_AGE_THRESHOLD_DAYS,
    only_old: bool = False,
    limit: int | None = None,
) -> dict:
    """Sweep all VMs for snapshots and flag old / sprawling ones.

    Returns a dict:
        total_snapshots   - count across the inventory
        old_snapshots     - count older than age_threshold_days
        vms_with_snapshots- count of VMs that have at least one snapshot
        threshold_days    - the threshold applied
        snapshots         - per-snapshot rows (vm_name, snapshot_name, age_days,
                            is_old, est_size_mb, level), oldest first
        hint              - actionable guidance when old snapshots exist

    Args:
        si: vSphere ServiceInstance.
        age_threshold_days: Age above which a snapshot is flagged "old".
        only_old: When True, return only snapshots older than the threshold.
        limit: Max number of snapshot rows to return (None = all).
    """
    now = datetime.now(tz=timezone.utc)
    rows: list[dict] = []
    vms_with_snaps = 0

    # Batch name/snapshot/layoutEx for every VM in one PropertyCollector call.
    # The heavy layoutEx (used by _snapshot_size_mb) and the snapshot tree are
    # fetched server-side instead of one lazy round-trip per VM (issue #31).
    for _obj, p in _collect(si, [vim.VirtualMachine], ["name", "snapshot", "layoutEx"]):
        snap_info = p.get("snapshot")
        if not snap_info or not getattr(snap_info, "rootSnapshotList", None):
            continue
        vms_with_snaps += 1
        vm_name = sanitize(p.get("name", ""))
        est_size = _snapshot_size_mb(p.get("layoutEx"))
        vm_rows = _walk_snapshots(snap_info.rootSnapshotList, vm_name, now)
        # Attribute the VM-level size estimate to the FIRST root row only, so a
        # VM with several root snapshots (branched trees after reverts) reports
        # its total once rather than once per root.
        size_assigned = False
        for r in vm_rows:
            if r["level"] == 0 and not size_assigned:
                r["est_size_mb"] = est_size
                size_assigned = True
            else:
                r["est_size_mb"] = 0.0
        rows.extend(vm_rows)

    total = len(rows)
    for r in rows:
        r["is_old"] = bool(r["age_days"] is not None and r["age_days"] > age_threshold_days)

    old_count = sum(1 for r in rows if r["is_old"])

    if only_old:
        rows = [r for r in rows if r["is_old"]]

    # Oldest first — None ages (unknown) sort last.
    rows.sort(key=lambda r: (r["age_days"] is None, -(r["age_days"] or 0)))
    if limit is not None:
        rows = rows[:limit]

    hint = None
    if old_count:
        hint = (
            f"{old_count} snapshot(s) older than {age_threshold_days} days. "
            "Old snapshots consume datastore space and degrade I/O. "
            "Delete via vmware-aiops: delete_vm_snapshot(vm_name=..., snapshot_name=...)."
        )

    return {
        "total_snapshots": total,
        "old_snapshots": old_count,
        "vms_with_snapshots": vms_with_snaps,
        "threshold_days": age_threshold_days,
        "snapshots": rows,
        "hint": hint,
    }
