"""Backup windows inferred from a VM's snapshot task history (read-only).

Image-level backup products (Veeam, Commvault, Rubrik, NetBackup) all work the
same way against vSphere: snapshot the VM, copy the frozen disks, delete the
snapshot. vCenter records both ends as tasks, so the period during which a
backup held a snapshot open is recoverable from task history alone -- without
credentials for the backup server.

What this is NOT: the backup job's duration. The product does work before the
snapshot is taken and after it is removed, and none of that is visible here.
Every number this module returns describes *the snapshot lifecycle*, which is
an observable lower bound on the job. The response says so in ``basis`` so a
caller reporting the figure cannot lose the qualifier on the way.

Read-only -- it reads the task collector and never creates, reverts, or deletes
a snapshot. Snapshot writes belong to vmware-aiops.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import sanitize

from vmware_monitor.ops._collect import _collect
from vmware_monitor.ops.vm_info import VMNotFoundError

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

MAX_DAYS = 365
DEFAULT_DAYS = 30
MAX_CYCLES_RETURNED = 200
# One ReadNextTasks page. vCenter caps what it will hand back per call; asking
# for more does not fail, it just returns fewer rows, so this is a request size
# rather than a limit we enforce.
_PAGE = 1000
# Refuse to walk forever if a collector never returns an empty page.
_MAX_PAGES = 100


class AmbiguousVMError(Exception):
    """Raised when several VMs share the requested name.

    vCenter permits duplicate VM names in different folders or datacenters.
    Picking one silently would attribute another VM's backup history to the
    caller's VM, and nothing downstream could detect the substitution.
    """


# ---------------------------------------------------------------------------
# Which tasks are snapshot creations and removals
# ---------------------------------------------------------------------------
#
# Read the three identifying fields off TaskInfo rather than one, because which
# of them a given vCenter populates has varied across versions. They are read
# in the shapes pyVmomi actually declares, which is not what their names
# suggest:
#
#   descriptionId  str                  e.g. "VirtualMachine.createSnapshot"
#   name           vmodl ManagedMethod  NOT a string -- str() on it yields
#                                       "<...ManagedMethod object at 0x...>".
#                                       The wire name is name.info.wsdlName.
#   description    vmodl.LocalizableMessage -- .key is the catalogue id, and
#                                       .message is localised prose that must
#                                       never be matched on.
#
# The middle one is the trap: this skill shipped a release that read a
# comparable pyVmomi field (EventDescription.EventDetail.key, declared as a
# type) as if it were a string, and every row on real hardware fell through to
# "unknown". Matching TaskInfo.name as a string would fail the same way -- and
# silently, since a stringified object simply matches nothing.

_CREATE_IDS = frozenset(
    {
        "VirtualMachine.createSnapshot",
        "VirtualMachine.createSnapshotEx",
        "CreateSnapshot_Task",
        "CreateSnapshotEx_Task",
        "com.vmware.vim.createSnapshot",
    }
)
_REMOVE_IDS = frozenset(
    {
        "VirtualMachineSnapshot.remove",
        "VirtualMachine.removeAllSnapshots",
        "RemoveSnapshot_Task",
        "RemoveAllSnapshots_Task",
        "com.vmware.vim.removeSnapshot",
    }
)


def _task_ids(info: vim.TaskInfo) -> set[str]:
    """Every identifier this task carries, read in its declared shape."""
    ids: set[str] = set()

    description_id = getattr(info, "descriptionId", None)
    if isinstance(description_id, str) and description_id:
        ids.add(description_id)

    # TaskInfo.name is a ManagedMethod; its .info.wsdlName is the wire name.
    method = getattr(info, "name", None)
    wsdl = getattr(getattr(method, "info", None), "wsdlName", None)
    if isinstance(wsdl, str) and wsdl:
        ids.add(wsdl)

    # LocalizableMessage.key -- the catalogue id, not the localised message.
    key = getattr(getattr(info, "description", None), "key", None)
    if isinstance(key, str) and key:
        ids.add(key)

    return ids


def _classify(info: vim.TaskInfo) -> str | None:
    """"create", "remove", or None when the task is not a snapshot operation."""
    ids = _task_ids(info)
    if ids & _CREATE_IDS:
        return "create"
    if ids & _REMOVE_IDS:
        return "remove"
    return None


def _aware(dt: datetime | None) -> datetime | None:
    """Normalise a vSphere timestamp to tz-aware UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hours(start: datetime | None, end: datetime | None) -> float | None:
    """Elapsed hours, or None when either end is missing. Never negative."""
    if start is None or end is None:
        return None
    delta = (end - start).total_seconds() / 3600
    return round(delta, 2) if delta >= 0 else None


def _resolve_vm_unique(si: ServiceInstance, vm_name: str) -> vim.VirtualMachine:
    """Find exactly one VM by exact name, or raise.

    Deliberately not ``inventory.find_vm_by_name``: that helper returns the
    first match, which is the right trade for a listing tool and the wrong one
    here. A duration attributed to the wrong same-named VM is indistinguishable
    from a correct answer once it leaves this function.
    """
    matches = [obj for obj, p in _collect(si, [vim.VirtualMachine], ["name"]) if p.get("name") == vm_name]
    if not matches:
        raise VMNotFoundError(
            f"VM not found. Run list_virtual_machines (filter by name, e.g. "
            f"'{sanitize(vm_name[:3])}*') to see available VMs and copy an exact name. "
            f"Requested: '{sanitize(vm_name)}'"
        )
    if len(matches) > 1:
        raise AmbiguousVMError(
            f"{len(matches)} VMs are named '{sanitize(vm_name)}'. Backup history for the "
            f"wrong one would look like a valid answer, so this tool refuses to guess. "
            f"Run vm_investigation_bundle or list_virtual_machines to tell them apart, "
            f"then retry once the duplicate is renamed or removed."
        )
    return matches[0]


def _read_task_history(
    si: ServiceInstance, vm: vim.VirtualMachine, since: datetime
) -> tuple[list[vim.TaskInfo], str | None]:
    """Page the VM's task history back to ``since``.

    Returns (tasks, note). ``note`` is non-None when the history could not be
    read at all, in which case tasks is empty and the caller must not report
    "no backups" -- the two are different answers.
    """
    task_mgr = si.RetrieveContent().taskManager
    if task_mgr is None:
        return [], "This endpoint exposes no task manager, so snapshot history cannot be read."

    spec = vim.TaskFilterSpec(
        # Snapshot removal is invoked on vim.vm.Snapshot, which is not a
        # ManagedEntity -- TaskInfo.entity is typed ManagedEntity, so vCenter
        # records both ends of the cycle against the VM itself. "self" is
        # therefore the whole set, and says that on purpose.
        entity=vim.TaskFilterSpec.ByEntity(entity=vm, recursion=vim.TaskFilterSpec.RecursionOption.self),
        time=vim.TaskFilterSpec.ByTime(
            timeType=vim.TaskFilterSpec.TimeOption.startedTime, beginTime=since
        ),
    )

    try:
        collector = task_mgr.CreateCollectorForTasks(filter=spec)
    except vim.fault.NoPermission:
        return [], (
            "Reading task history needs the System.View privilege on this VM; the "
            "configured account does not have it. This is not an empty history."
        )
    except vim.fault.InvalidArgument as exc:
        return [], sanitize(
            f"vCenter rejected the task history filter: {getattr(exc, 'msg', None) or exc}", max_len=300
        )

    tasks: list[vim.TaskInfo] = []
    try:
        collector.RewindCollector()
        for _ in range(_MAX_PAGES):
            page = collector.ReadNextTasks(maxCount=_PAGE) or []
            if not page:
                break
            tasks.extend(page)
    finally:
        # Collectors are a bounded per-session resource on vCenter (32 by
        # default); leaking one per call exhausts the session's quota and then
        # every later call fails for an unrelated-looking reason.
        try:
            collector.DestroyCollector()
        except Exception:
            pass
    return tasks, None


def _build_cycles(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """Pair each creation with the next removal. Returns (complete, orphans).

    Orphans are kept and labelled rather than dropped: a creation with no
    removal is the operationally interesting case (a backup that left its
    snapshot behind), and a removal with no creation is normally just the
    window boundary cutting a cycle in half.
    """
    complete: list[dict] = []
    orphans: list[dict] = []
    pending: dict | None = None

    for ev in events:
        if ev["kind"] == "create":
            if pending is not None:
                orphans.append({**pending, "reason": "snapshot creation with no matching removal"})
            pending = ev
            continue
        # removal
        if pending is None:
            orphans.append({**ev, "reason": "snapshot removal with no creation inside the window"})
            continue
        complete.append(
            {
                "snapshot_created_started": _iso(pending["start"]),
                "snapshot_created": _iso(pending["end"]),
                "snapshot_removal_started": _iso(ev["start"]),
                "snapshot_removal_completed": _iso(ev["end"]),
                "backup_active_hours": _hours(pending["end"], ev["start"]),
                "snapshot_present_hours": _hours(pending["end"], ev["end"]),
                "total_window_hours": _hours(pending["start"], ev["end"]),
                "snapshot_removal_hours": _hours(ev["start"], ev["end"]),
            }
        )
        pending = None

    if pending is not None:
        orphans.append({**pending, "reason": "snapshot creation with no matching removal"})

    # Orphan rows carry raw datetimes from the event stream; serialise them.
    for o in orphans:
        o["started"] = _iso(o.pop("start", None))
        o["completed"] = _iso(o.pop("end", None))
        o.pop("kind", None)
    return complete, orphans


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _stats(values: list[float]) -> dict | None:
    """average/minimum/maximum over the values that exist, or None."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    return {
        "average": round(sum(present) / len(present), 2),
        "minimum": round(min(present), 2),
        "maximum": round(max(present), 2),
        "samples": len(present),
    }


def get_backup_snapshot_history(
    si: ServiceInstance,
    vm_name: str,
    days: int = DEFAULT_DAYS,
    include_cycles: bool = False,
    limit: int | None = None,
) -> dict:
    """Backup windows for one VM, inferred from its snapshot task history.

    Args:
        si: vSphere ServiceInstance.
        vm_name: Exact VM name. Duplicates raise rather than resolve to one.
        days: How far back to look, 1..365 (default 30).
        include_cycles: Include the per-cycle rows, not just the aggregates.
        limit: Max cycle rows to return when include_cycles is set.
    """
    if not isinstance(days, int) or days < 1 or days > MAX_DAYS:
        raise ValueError(f"days must be an integer from 1 to {MAX_DAYS}; got {days!r}.")

    vm = _resolve_vm_unique(si, vm_name)
    now = datetime.now(tz=timezone.utc)
    since = now - timedelta(days=days)

    tasks, unavailable = _read_task_history(si, vm, since)

    events: list[dict] = []
    oldest_seen: datetime | None = None
    for info in tasks:
        started = _aware(getattr(info, "startTime", None))
        if started is not None and (oldest_seen is None or started < oldest_seen):
            oldest_seen = started
        kind = _classify(info)
        if kind is None:
            continue
        # Only successful operations describe a real backup window. A failed
        # creation opened nothing; a failed removal left the snapshot in place
        # and its cycle is correctly reported as incomplete.
        if str(getattr(info, "state", "")) != "success":
            continue
        end = _aware(getattr(info, "completeTime", None))
        if started is None or end is None:
            continue
        events.append({"kind": kind, "start": started, "end": end})

    events.sort(key=lambda e: e["start"])
    complete, orphans = _build_cycles(events)

    creations = sum(1 for e in events if e["kind"] == "create")
    removals = sum(1 for e in events if e["kind"] == "remove")

    # Whether the window was actually covered. vCenter expires task history on
    # its own retention schedule (vpxd.task.maxAge, 30 days out of the box), so
    # a 90-day request against a 30-day retention returns 30 days of cycles and
    # nothing marks the difference. Reporting the oldest task of ANY kind seen
    # -- not the oldest snapshot task -- is what separates "no backups ran" from
    # "vCenter no longer remembers".
    coverage_note = None
    if unavailable is None:
        if oldest_seen is None:
            coverage_note = (
                f"vCenter returned no tasks at all for this VM in the last {days} days. "
                "That means no recorded activity of any kind, not specifically no backups."
            )
        elif (oldest_seen - since).total_seconds() > 86400:
            covered = max(0, round((now - oldest_seen).total_seconds() / 86400, 1))
            coverage_note = (
                f"Requested {days} days but the oldest task vCenter still holds for this VM "
                f"is {covered} days old. Anything before that has aged out of task history "
                f"(vpxd.task.maxAge), so the counts below describe {covered} days, not {days}."
            )

    result: dict = {
        "vm": sanitize(vm_name),
        "period_days": days,
        "basis": (
            "Snapshot lifecycle observed in vCenter task history. This is a lower bound on "
            "the backup job: work the backup product does before the snapshot is taken and "
            "after it is removed is not visible to vCenter. Not the official job duration."
        ),
        "window_start": _iso(since),
        "window_end": _iso(now),
        "history_oldest_task_seen": _iso(oldest_seen),
        "snapshot_creations": creations,
        "snapshot_removals": removals,
        "complete_cycles": len(complete),
        "incomplete_cycles": len(orphans),
        "backup_active_hours": _stats([c["backup_active_hours"] for c in complete]),
        "snapshot_present_hours": _stats([c["snapshot_present_hours"] for c in complete]),
        "total_window_hours": _stats([c["total_window_hours"] for c in complete]),
        "snapshot_removal_hours": _stats([c["snapshot_removal_hours"] for c in complete]),
        "latest_cycle": complete[-1] if complete else None,
        "longest_cycle": (
            max(
                (c for c in complete if c["backup_active_hours"] is not None),
                key=lambda c: c["backup_active_hours"],
                default=None,
            )
        ),
        "unmatched": orphans[:MAX_CYCLES_RETURNED],
        "history_unavailable": unavailable,
        "coverage_note": coverage_note,
    }

    if include_cycles:
        rows = complete
        cap = MAX_CYCLES_RETURNED if limit is None else min(limit, MAX_CYCLES_RETURNED)
        result["cycles"] = rows[-cap:] if len(rows) > cap else rows
        result["cycles_truncated"] = len(rows) > cap

    return result
