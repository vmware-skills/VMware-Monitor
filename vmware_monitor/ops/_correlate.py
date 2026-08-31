"""Correlation engine for object-centered investigation bundles.

An investigation bundle answers *"what is happening around this object?"* — a VM,
host, or datastore and its related infrastructure and recent history (GitHub issue
#31 follow-up). The value is not re-running existing queries; it is **correlation**:
walking the object graph (VM -> host -> cluster -> datastore) and stitching each
entity's recent events into one time-ordered timeline.

Two capabilities live here because both are new and shared across all three bundle
types:

1. **Per-entity event timeline** — vSphere's ``get_recent_events`` only filters by
   time+severity *globally*; nothing in the codebase scopes events to a single
   managed entity. ``entity_timeline`` builds an ``EventFilterSpec.ByEntity`` per
   object, reuses ``health.query_events`` (which already swallows the standalone-ESXi
   ``NotSupported`` fault while re-raising real auth/network errors), classifies
   severity with ``health``'s event maps, de-dupes across overlapping scopes, and
   returns one merged, newest-first list tagged with the source entity.

2. **Alarm scan + batched name resolution** — mirrors ``cluster_summary``: count the
   ``triggeredAlarmState`` already read on each object, then resolve every alarm's
   display name in ONE ``_collect_objects`` call (never a lazy read per alarm).

Plus small **pure formatters** (``format_host``/``format_cluster``/
``format_datastore``) that map a batched property dict to a high-signal display dict.
I/O (the ``_collect_objects`` calls) stays in the bundle modules so these formatters
are pure and unit-testable; the ``*_CTX_PROPS`` constants keep the property paths the
formatters expect co-located with the formatters.

Read-only. Point-in-time — no trend is invented.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import sanitize

from vmware_monitor.ops._collect import _collect_objects
from vmware_monitor.ops.health import (
    CRITICAL_EVENTS,
    SEVERITY_ORDER,
    WARNING_EVENTS,
    query_events_or_none,
)

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

# Cap a single entity's returned events so one noisy object cannot flood the
# bundle context (high-signal, not a full log). Newest are kept.
MAX_TIMELINE_EVENTS = 50

# EventFilterSpec recursion: only the entity's OWN events. ``self`` is a valid
# attribute name (not a reserved word); child entities are queried separately so
# recursion here would only double-count.
_SELF = vim.event.EventFilterSpec.RecursionOption.self

# vSphere overallStatus -> our severity band (green/gray are not anomalies).
_STATUS_SEVERITY = {"red": "critical", "yellow": "warning"}


# ── Property path constants (kept next to the formatter that consumes them) ──
HOST_CTX_PROPS = [
    "name",
    "parent",
    "runtime.connectionState",
    "summary.quickStats.overallCpuUsage",
    "summary.quickStats.overallMemoryUsage",
    "summary.hardware.cpuMhz",
    "summary.hardware.numCpuCores",
    "summary.hardware.memorySize",
    "triggeredAlarmState",
]
CLUSTER_CTX_PROPS = [
    "name",
    "host",
    "configuration.dasConfig.enabled",
    "configuration.drsConfig.enabled",
    "triggeredAlarmState",
]
DS_CTX_PROPS = [
    "name",
    "summary.type",
    "summary.freeSpace",
    "summary.capacity",
    "summary.accessible",
    "triggeredAlarmState",
]


def pct(used: float, total: float) -> float:
    """Utilisation percentage, guarding divide-by-zero. Rounded to 1 dp."""
    if not total:
        return 0.0
    return round(used / total * 100, 1)


# ── Pure formatters: batched property dict -> high-signal display dict ───────
def format_host(props: dict) -> dict:
    """One host's context row from ``HOST_CTX_PROPS`` (pure, no I/O)."""
    total_mhz = (props.get("summary.hardware.cpuMhz") or 0) * (
        props.get("summary.hardware.numCpuCores") or 0
    )
    mem_used = (props.get("summary.quickStats.overallMemoryUsage") or 0) * 1024 * 1024
    return {
        "name": sanitize(props.get("name", "")),
        "connection": str(props.get("runtime.connectionState") or "unknown"),
        "cpu_pct": pct(props.get("summary.quickStats.overallCpuUsage") or 0, total_mhz),
        "mem_pct": pct(mem_used, props.get("summary.hardware.memorySize") or 0),
    }


def format_cluster(props: dict) -> dict:
    """One cluster's context row from ``CLUSTER_CTX_PROPS`` (pure, no I/O)."""
    return {
        "name": sanitize(props.get("name", "")),
        "ha_enabled": bool(props.get("configuration.dasConfig.enabled")),
        "drs_enabled": bool(props.get("configuration.drsConfig.enabled")),
        "host_count": len(props.get("host") or []),
    }


def format_datastore(props: dict) -> dict:
    """One datastore's context row from ``DS_CTX_PROPS`` (pure, no I/O)."""
    free = props.get("summary.freeSpace") or 0
    cap = props.get("summary.capacity") or 0
    return {
        "name": sanitize(props.get("name", "")),
        "type": str(props.get("summary.type") or "N/A"),
        "free_gb": round(free / (1024**3), 1),
        "capacity_gb": round(cap / (1024**3), 1),
        "free_pct": pct(free, cap),
        "accessible": bool(props.get("summary.accessible", True)),
    }


# Cap the powered-on VM sample carried in a host/datastore bundle (high-signal,
# not a full inventory dump).
MAX_VM_SAMPLE = 15


def summarize_vms(si: ServiceInstance, vm_refs: list) -> dict:
    """Roll a list of VM morefs up to ``{total, powered_on, sample}`` in one call.

    ``sample`` is up to ``MAX_VM_SAMPLE`` powered-on VM names (so a host/datastore
    bundle shows *which* VMs without dumping the whole inventory). One batched read
    over the known refs — never a lazy per-VM round-trip (issue #31 class).
    """
    if not vm_refs:
        return {"total": 0, "powered_on": 0, "sample": []}
    total = 0
    on_names: list[str] = []
    for _ref, p in _collect_objects(
        si, vm_refs, vim.VirtualMachine, ["name", "runtime.powerState"]
    ):
        total += 1
        if str(p.get("runtime.powerState")) == "poweredOn":
            on_names.append(sanitize(p.get("name", "")))
    on_names.sort()
    return {"total": total, "powered_on": len(on_names), "sample": on_names[:MAX_VM_SAMPLE]}


# ── Alarms ──────────────────────────────────────────────────────────────────
def collect_alarms(si: ServiceInstance, entities_triggered: list[tuple]) -> list[dict]:
    """Scan triggered alarms across entities, resolve names in ONE batched call.

    Args:
        si: vSphere ServiceInstance.
        entities_triggered: list of ``(scope, entity_name, triggeredAlarmState)``
            where the third item is the raw list already read on that object.

    Returns:
        list of ``{severity, scope, object, name}`` (green/gray states dropped).
        Alarm display names are resolved together via ``_collect_objects`` — never
        a lazy read per alarm (issue #31 class).
    """
    raw: list[tuple] = []
    for scope, name, triggered in entities_triggered:
        for state in triggered or []:
            sev = _STATUS_SEVERITY.get(str(getattr(state, "overallStatus", "")))
            if sev is None:
                continue
            raw.append((getattr(state, "alarm", None), sev, scope, name))

    refs = [r[0] for r in raw if r[0] is not None]
    name_by_ref = {
        ref: (props.get("info.name") or "alarm")
        for ref, props in _collect_objects(si, refs, vim.alarm.Alarm, ["info.name"])
    }
    out: list[dict] = []
    for ref, sev, scope, name in raw:
        out.append(
            {
                "severity": sev,
                "scope": scope,
                "object": name,
                "name": sanitize(str(name_by_ref.get(ref, "alarm"))),
            }
        )
    return out


# ── Event timeline ──────────────────────────────────────────────────────────
def _classify(event: object) -> str:
    """Map an event object to critical/warning/info via ``health``'s maps."""
    et = type(event).__name__
    if et in CRITICAL_EVENTS:
        return "critical"
    if et in WARNING_EVENTS:
        return "warning"
    return "info"


def _event_key(event: object) -> object:
    """Stable de-dup key so one event under two scopes appears once."""
    key = getattr(event, "key", None)
    if key is not None:
        return key
    return (
        type(event).__name__,
        str(getattr(event, "createdTime", "")),
        getattr(event, "fullFormattedMessage", ""),
    )


def _entity_events(event_mgr: object, ref: object, begin: datetime, now: datetime) -> list:
    """Fetch one entity's own events in the window (pyVmomi spec boilerplate).

    Split out so the testable classification/de-dup/ordering logic in
    ``entity_timeline`` can be exercised without constructing a real
    ``ByEntity`` spec (which pyVmomi rejects for non-managed stand-ins).
    """
    spec = vim.event.EventFilterSpec(
        entity=vim.event.EventFilterSpec.ByEntity(entity=ref, recursion=_SELF),
        time=vim.event.EventFilterSpec.ByTime(beginTime=begin, endTime=now),
    )
    return query_events_or_none(event_mgr, spec)


def entity_timeline(
    si: ServiceInstance,
    entities: list[tuple],
    hours: int = 24,
    min_severity: str = "info",
) -> list[dict]:
    """Merged, newest-first event timeline scoped to a set of entities.

    Args:
        si: vSphere ServiceInstance.
        entities: list of ``(scope, display_name, moref)``; a None moref is
            skipped (e.g. a standalone host with no cluster). Order matters —
            earlier scopes win de-dup, so pass the most specific object first.
        hours: Look-back window (default 24).
        min_severity: Drop events below this band ("critical"/"warning"/"info").

    Returns:
        ``(rows, unavailable_reason)``. ``rows`` is
        ``{time, scope, entity, severity, event_type, message, username}`` newest
        first, de-duplicated across overlapping scopes, capped at
        ``MAX_TIMELINE_EVENTS``. ``unavailable_reason`` is None when the events
        were read, and a sentence when this endpoint does not serve them.

        The tuple exists so callers cannot accidentally publish an empty
        timeline as a quiet all-clear. A standalone ESXi exposes an
        ``eventManager`` object and then refuses ``QueryEvents``
        (vmodl.fault.NotImplemented, verified on a live 8.0.3), so "no events"
        and "no event service" arrive looking identical unless they are kept
        apart here.
    """
    content = si.RetrieveContent()
    event_mgr = content.eventManager
    now = datetime.now(tz=timezone.utc)
    begin = now - timedelta(hours=hours)
    threshold = SEVERITY_ORDER.get(min_severity, 2)

    seen: set = set()
    rows: list[dict] = []
    refused: list[str] = []
    for scope, display_name, ref in entities:
        if ref is None:
            continue
        events = _entity_events(event_mgr, ref, begin, now)
        if events is None:
            refused.append(scope)
            continue
        for event in events:
            sev = _classify(event)
            if SEVERITY_ORDER.get(sev, 2) > threshold:
                continue
            key = _event_key(event)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "time": str(getattr(event, "createdTime", "")),
                    "scope": scope,
                    "entity": display_name,
                    "severity": sev,
                    "event_type": type(event).__name__,
                    "message": sanitize(
                        getattr(event, "fullFormattedMessage", None) or str(event),
                        max_len=500,
                    ),
                    "username": getattr(event, "userName", "") or "",
                }
            )
    rows.sort(key=lambda r: r["time"], reverse=True)
    # Any refusal is reported, not only a total one. The first version of this
    # said `if refused and not rows`, which suppressed the warning as soon as a
    # single scope answered -- so a timeline missing the host's events entirely
    # would have looked complete. That is the same "partial failure rendered as
    # success" this function exists to prevent, reintroduced inside the fix for
    # it (形态 #5).
    reason = None
    if refused:
        scopes = ", ".join(sorted(set(refused)))
        if rows:
            reason = (
                f"Events could not be read for these scopes: {scopes}. The "
                f"endpoint exposes an event manager and refuses QueryEvents, "
                f"which is how a standalone ESXi host behaves. The timeline "
                f"below is therefore incomplete, not empty."
            )
        else:
            reason = (
                "This endpoint does not serve event history: it exposes an event "
                "manager but refuses QueryEvents, which is how a standalone ESXi "
                "host behaves. Connect to the vCenter that manages it for a "
                "timeline. Everything else in this bundle was read normally."
            )
    return rows[:MAX_TIMELINE_EVENTS], reason
