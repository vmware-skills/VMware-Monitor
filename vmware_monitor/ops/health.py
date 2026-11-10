"""Health checks: alarms, events, hardware status, services."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pyVmomi import vim, vmodl
from vmware_policy import paginated, sanitize

from vmware_monitor.ops import alarm_condition
from vmware_monitor.ops._collect import _collect, _collect_objects
from vmware_monitor.ops.alarm_triage import license_evidence

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

from vmware_monitor.ops._event_shapes import (
    CRITICAL_EVENTS as CRITICAL_EVENTS,
    INFO_EVENTS as INFO_EVENTS,
    WARNING_EVENTS as WARNING_EVENTS,
    _PROBLEM_PREFIX as _PROBLEM_PREFIX,
    _VC_CATEGORY_RANK as _VC_CATEGORY_RANK,
    _VIM_EVENT_PREFIX as _VIM_EVENT_PREFIX,
    _catalogue_and_coverage as _catalogue_and_coverage,
    _catalogue_entry_key as _catalogue_entry_key,
    _event_catalogue as _event_catalogue,
    _event_key as _event_key,
    _event_severity as _event_severity,
    _event_severity_with_source as _event_severity_with_source,
    parse_event_time as parse_event_time,
    short_event_type as short_event_type,
)


_log = logging.getLogger(__name__)

# Event types by severity



SEVERITY_ORDER = {"critical": 0, "warning": 1, "unknown": 1, "info": 2}

#: Session events routine automation produces in volume. On a lab vCenter 8.0.3
#: (2026-09-14) nearly all of 1174 events in 48 h were these, one login/logout
#: pair every five minutes from a local agent, and they filled the bundles'
#: timelines. Folded by default and always counted; failed logins
#: (BadUsernameSessionEvent) are not routine and are never folded.
ROUTINE_EVENT_TYPES = frozenset({"UserLoginSessionEvent", "UserLogoutSessionEvent"})






#: vCenter's own event categories, as published by
#: ``EventManager.description.eventInfo``, mapped onto this skill's three ranks.
#: "user" is an operator action (a login, a reconfigure) — routine unless one of
#: the override sets above says otherwise.

# Maps event type → suggested remediation skill/tool hint
_EVENT_SUGGESTIONS: dict[str, str] = {
    "VmFailedToPowerOnEvent": "vmware-aiops: power_on_vm(vm_name='{entity}')",
    "VmFailoverFailed": "vmware-aiops: power_on_vm(vm_name='{entity}')",
    "VmFailedToRebootGuestEvent": "vmware-aiops: power_off_vm then power_on_vm(vm_name='{entity}')",
    "HostConnectionLostEvent": "vmware-monitor: list_esxi_hosts — verify host is reachable",
    "HostShutdownEvent": "vmware-monitor: list_esxi_hosts — check if shutdown was intentional",
    "VmDiskFailedEvent": "vmware-storage: check datastore health; vmware-monitor: list_all_datastores",
    "DasHostFailedEvent": "vmware-monitor: list_esxi_hosts — check HA cluster status",
    "DatastoreRemovedOnHostEvent": "vmware-storage: rescan; vmware-monitor: list_all_datastores",
}


# Faults meaning "this endpoint does not serve event history at all". Only these;
# auth/network/permission errors must propagate, or a monitoring tool reports
# all-clear on failure.
#
# Both are needed and only one was here. A standalone ESXi raises
# **NotImplemented** -- verified on a live 8.0.3 host, and the reason both
# investigation bundles died there with a raw VMODL traceback after doing all
# their work. The original comment reasoned correctly about which class *exists*
# (vim.fault has no NotSupported; vmodl.fault does) and never checked which one
# ESXi actually raises. Knowing a class exists is not knowing it is thrown.
_NOT_SUPPORTED_FAULTS: tuple[type[Exception], ...] = (
    vmodl.fault.NotSupported,
    vmodl.fault.NotImplemented,
)


#: Newest events read per query. The read stops here and reports that it did.
MAX_EVENTS_READ = 5000

#: vSphere's per-call ceiling for an event history collector page.
_EVENT_PAGE = 1000


@dataclass(frozen=True)
class EventRead:
    """Events matching a filter, newest first, and whether the read stopped early.

    ``truncated`` is True when more events matched than were read; the ones not
    read are the oldest in the window.
    """

    events: tuple
    truncated: bool


def read_events(
    event_mgr: vim.event.EventManager,
    filter_spec: vim.event.EventFilterSpec,
    max_events: int | None = None,
) -> EventRead | None:
    """The newest events matching ``filter_spec``, or ``None`` when this endpoint has none.

    Why not ``QueryEvents``: measured on vCenter 8.0.3 (2026-09-14), it returns at
    most 1000 events and they are the **oldest** 1000 in the window — a 24 h query
    came back with the previous afternoon and none of the latest 16 hours, and
    nothing in the reply said so. A standalone ESXi refuses it outright
    (``vmodl.fault.NotImplemented``) while serving the collector.

    So this reads through an event history collector, newest first:
    ``latestPage`` (page size raised to 1000) is the newest page, newest first;
    after ``ResetCollector``, each ``ReadPreviousEvents`` returns the next-older
    page, oldest first within it, with no gap or overlap. It stops once more than
    ``max_events`` are in hand, keeps the newest ``max_events``, and says so in
    ``truncated``. The collector is always released: vCenter allows a bounded
    number per session.

    ``None`` means the endpoint does not serve event history at all; ``[]`` in
    ``events`` means the window really is empty. Auth, permission and network
    failures propagate — a monitoring tool must not read them as a quiet window.
    """
    limit = MAX_EVENTS_READ if max_events is None else max_events
    try:
        collector = event_mgr.CreateCollectorForEvents(filter_spec)
    except _NOT_SUPPORTED_FAULTS:
        return None
    try:
        collector.SetCollectorPageSize(_EVENT_PAGE)
        events = list(collector.latestPage or [])
        if len(events) >= _EVENT_PAGE:
            collector.ResetCollector()
            while len(events) <= limit:
                page = collector.ReadPreviousEvents(_EVENT_PAGE)
                if not page:
                    break
                events.extend(reversed(page))
    finally:
        try:
            collector.DestroyCollector()
        except Exception as exc:  # noqa: BLE001 — the read already has its answer
            _log.warning("Could not release an event history collector: %s", exc)
    seen: set = set()
    unique = []
    for event in events:
        key = getattr(event, "key", None)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        unique.append(event)
    return EventRead(events=tuple(unique[:limit]), truncated=len(unique) > limit)


def query_events_or_none(
    event_mgr: vim.event.EventManager, filter_spec: vim.event.EventFilterSpec
) -> list | None:
    """Events newest first, or ``None`` when this endpoint cannot answer at all.

    The two answers are different and callers need them apart: ``[]`` is "the
    window really is empty", ``None`` is "we could not ask". A caller that
    collapses them reports a quiet all-clear on a host it never read (形态 #1).
    Callers that report to an operator should use :func:`read_events`, which also
    says whether the read stopped at ``MAX_EVENTS_READ``.
    """
    read = read_events(event_mgr, filter_spec)
    return None if read is None else list(read.events)


def query_events(event_mgr: vim.event.EventManager, filter_spec: vim.event.EventFilterSpec) -> list:
    """Events newest first; "unsupported" reads as "no events".

    Kept for callers that genuinely have nothing to say about the difference
    (the log scanner scans what it can reach). Anything that reports to an
    operator should use :func:`read_events` and say what it got.
    """
    events = query_events_or_none(event_mgr, filter_spec)
    return [] if events is None else events










#: ESXi names its own problem events ``esx.problem.<subsystem>.<condition>``.
#:
#: These events do NOT carry ``.severity``. That took three rounds and two
#: separate estates to pin down: 0 of 38 ``esx.problem.*`` events on a live VCF
#: 9.1 vCenter had the field, across two runs (2026-08-31). It is written here
#: so the next person does not go reading it again and find None — and so that
#: this prefix rule is understood as the real ranking path for these events,
#: not a curiosity. (Since ``_catalogue_entry_key`` learned to read extended ids out of
#: ``fullFormat``, the catalogue does now cover ~372 ``esx.problem.*`` types, so
#: the prefix is once again a genuine last resort rather than the main road.)
#:
#: The prefix is vCenter's own convention and it is the last thing consulted --
#: only after the overrides, the event's own severity, and the catalogue have
#: all declined to rank it. "warning" rather than "critical" because the prefix
#: says a problem was reported, not how bad it is; ``severity_source`` says the
#: rank came from the name so a caller can tell it apart from vCenter's word.






def _get_event_entity(event: object) -> str | None:
    """Extract entity name from a pyVmomi event object."""
    for attr in ("vm", "host", "ds", "computeResource", "net"):
        obj = getattr(event, attr, None)
        if obj:
            name = getattr(obj, "name", None)
            return sanitize(name) if name else None
    return None


def _ref_key(obj: object) -> object:
    """A dict key for a managed-object reference; ``id()`` for unhashable stand-ins."""
    try:
        hash(obj)
        return obj
    except TypeError:
        return ("id", id(obj))


def _alarm_definitions(si: ServiceInstance, alarm_refs: list) -> dict:
    """``{alarm ref: (name, expression)}`` for every real Alarm, in one batched read.

    References that are not ``vim.alarm.Alarm`` managed objects are left out; the
    row builder reads those lazily. A failed batch is logged and yields ``{}`` —
    names then come from the lazy read and every verdict says the definition
    could not be read, rather than the whole alarm list failing.
    """
    real = [r for r in alarm_refs if isinstance(r, vim.alarm.Alarm)]
    if not real:
        return {}
    try:
        rows = _collect_objects(si, real, vim.alarm.Alarm, ["info.name", "info.expression"])
    except Exception as exc:  # noqa: BLE001 — degrade to lazy names, verdicts unknown
        _log.warning("Could not batch-read alarm definitions: %s", exc)
        return {}
    return {
        _ref_key(ref): (props.get("info.name"), props.get("info.expression"))
        for ref, props in rows
    }


def _entity_state_values(si: ServiceInstance, states: list, definitions: dict) -> dict:
    """``{entity ref: {path: value}}`` for the paths each alarm's state parts compare.

    One batched read per managed-object type. A failed batch is logged and the
    affected verdicts come out ``unknown``, never ``cleared``.
    """
    wanted: dict[type, tuple[dict, set]] = {}
    for state in states:
        definition = definitions.get(_ref_key(state.alarm))
        entity = state.entity
        if definition is None or not isinstance(entity, vim.ManagedEntity):
            continue
        paths = alarm_condition.state_paths(definition[1])
        if not paths:
            continue
        entities, all_paths = wanted.setdefault(type(entity), ({}, set()))
        entities[_ref_key(entity)] = entity
        all_paths |= paths
    values: dict = {}
    for obj_type, (entities, paths) in wanted.items():
        try:
            rows = _collect_objects(si, list(entities.values()), obj_type, sorted(paths))
        except Exception as exc:  # noqa: BLE001 — verdicts for these become unknown
            _log.warning("Could not read alarm state properties for %s: %s", obj_type.__name__, exc)
            continue
        for ref, props in rows:
            values[_ref_key(ref)] = props
    return values


def _append_alarm(
    alarm_state: object,
    name_map: dict,
    results: list[dict],
    definition: tuple | None = None,
    values: dict | None = None,
    licenses: alarm_condition.LicenseEvidence | None = None,
) -> None:
    """Turn one triggered AlarmState into a result row, appending to ``results``."""
    severity = str(alarm_state.overallStatus)
    severity_map = {"red": "critical", "yellow": "warning", "green": "info"}
    # The alarm's source entity name is prefetched in ``name_map``; fall back to
    # a guarded lazy read only when it is not there (e.g. rootFolder alarms). One
    # bad/inaccessible entity must not kill the whole alarm listing.
    entity_ref = alarm_state.entity
    try:
        raw_entity_name = name_map.get(entity_ref)
    except TypeError:
        raw_entity_name = None  # entity ref not hashable (unusual) — read lazily
    if raw_entity_name is None:
        try:
            raw_entity_name = getattr(entity_ref, "name", None)
        except Exception:
            raw_entity_name = None
    entity_name = sanitize(raw_entity_name) if raw_entity_name else "[inaccessible]"
    if definition is not None:
        raw_alarm_name, expression = definition
    else:
        info = getattr(alarm_state.alarm, "info", None)
        raw_alarm_name = getattr(info, "name", None)
        expression = getattr(info, "expression", None)
    alarm_name = sanitize(raw_alarm_name or "alarm")
    acknowledged = getattr(alarm_state, "acknowledged", False)
    acked_by = getattr(alarm_state, "acknowledgedByUser", None)
    acked_at = getattr(alarm_state, "acknowledgedTime", None)
    verdict = alarm_condition.evaluate(
        expression,
        severity,
        entity_ref,
        (values or {}).get(_ref_key(entity_ref), {}),
        licenses=licenses,
    )
    # An alarm vCenter raises about its own appliance sits on the inventory root,
    # whose name ("Datacenters") says nothing about what is wrong. entity_name
    # stays as-is: vmware-aiops resolves it to acknowledge or reset the alarm.
    object_label = (
        alarm_condition.appliance_label(expression)
        if isinstance(entity_ref, (vim.Folder, vim.Datacenter))
        else None
    )

    reset = (
        f"vmware-aiops: reset_vcenter_alarm"
        f"(entity_name='{entity_name}', alarm_name='{alarm_name}')"
    )
    actions: list[str] = []
    if verdict.state == alarm_condition.CLEARED:
        # The condition is gone; the useful next step is the reset, not an ack.
        actions.append(reset)
    if not acknowledged:
        actions.append(
            f"vmware-aiops: acknowledge_vcenter_alarm"
            f"(entity_name='{entity_name}', alarm_name='{alarm_name}')"
        )
    if verdict.state != alarm_condition.CLEARED:
        actions.append(reset)

    results.append({
        "severity": severity_map.get(severity, severity),
        "alarm_name": alarm_name,
        "entity_name": entity_name,
        "entity_type": type(entity_ref).__name__,
        "object_label": object_label,
        "time": str(alarm_state.time),
        "acknowledged": acknowledged,
        "acknowledged_by": sanitize(acked_by) if acked_by else None,
        "acknowledged_at": str(acked_at) if acked_at else None,
        # holds / cleared / unknown — see ops/alarm_condition.py. "cleared" means
        # vCenter still shows the alarm although its state condition is false now.
        "condition_now": verdict.state,
        "condition_note": verdict.note,
        "suggested_actions": actions,
    })


def get_active_alarms(si: ServiceInstance, limit: int | None = None) -> dict:
    """Get all active/triggered alarms across the inventory.

    Returns the family list envelope with a real ``total``: every triggered
    alarm is collected and deduplicated before ``limit`` is applied. Each row
    says who acknowledged it and when, and whether its condition still holds
    (``condition_now``: holds / cleared / unknown). ``stale_alarms`` counts the
    rows whose condition no longer holds, with ``stale_note`` when any do.

    Args:
        si: vSphere ServiceInstance.
        limit: Max number of alarm rows to return (None = all).
    """
    content = si.RetrieveContent()
    results: list[dict] = []
    name_map: dict = {}
    triggered_lists: list[list] = []

    # rootFolder alarms: a single object read (O(1), not the N+1 path).
    root = content.rootFolder
    root_triggered = getattr(root, "triggeredAlarmState", None) or []
    if root_triggered:
        triggered_lists.append(root_triggered)

    # Datacenters, clusters, hosts: batch name + triggeredAlarmState in one
    # PropertyCollector call per type instead of a lazy round-trip per entity
    # (N+1 on large inventories — GitHub issue #31 class).
    for obj_type in (vim.Datacenter, vim.ClusterComputeResource, vim.HostSystem):
        for obj, p in _collect(si, [obj_type], ["name", "triggeredAlarmState"]):
            name_map[obj] = p.get("name")
            triggered = p.get("triggeredAlarmState")
            if triggered:
                triggered_lists.append(triggered)

    states = [state for triggered in triggered_lists for state in triggered]
    # Two batched reads cover every alarm: the definitions, then the entity
    # properties their state conditions compare (one call per entity type).
    definitions = _alarm_definitions(si, [s.alarm for s in states])
    values = _entity_state_values(si, states, definitions)
    # Read only when a license alarm is present (one QueryAssignedLicenses call).
    licenses = license_evidence(content, [d[1] for d in definitions.values()])
    for alarm_state in states:
        _append_alarm(
            alarm_state,
            name_map,
            results,
            definitions.get(_ref_key(alarm_state.alarm)),
            values,
            licenses,
        )

    # Deduplicate by alarm + entity
    seen = set()
    unique = []
    for a in results:
        key = (a["alarm_name"], a["entity_name"])
        if key not in seen:
            seen.add(key)
            unique.append(a)

    unique.sort(key=lambda x: SEVERITY_ORDER.get(x["severity"], 9))
    total = len(unique)
    stale = sum(1 for a in unique if a["condition_now"] == alarm_condition.CLEARED)
    extra: dict = {"stale_alarms": stale}
    if stale:
        extra["stale_note"] = (
            f"{stale} alarm(s) are still triggered although their condition no longer "
            f"holds — vCenter did not reset them. Confirm on the object, then reset "
            f"(vSphere Client, or vmware-aiops reset_vcenter_alarm)."
        )
    if limit is not None:
        unique = unique[:limit]
    return paginated(unique, limit=limit, total=total, **extra)


def get_recent_events(
    si: ServiceInstance,
    hours: int = 24,
    severity: str = "warning",
    start: str | None = None,
    end: str | None = None,
    include_routine: bool = False,
) -> dict:
    """Get recent events filtered by severity.

    Severity comes from this skill's own override sets first and vCenter's
    published event catalogue second (see :func:`_event_severity`). An event
    neither can rank is returned with severity ``"unknown"`` and counted in the
    envelope's ``unclassified`` — it is never quietly demoted to ``"info"``,
    which is how five warning-level events came back as "No events above
    warning" (VCF 9.1, 2026-08-30).

    Returns the family list envelope. Events are read newest first (see
    :func:`read_events`). ``read_truncated`` is True when more than
    ``MAX_EVENTS_READ`` events matched the window, and ``read_note`` then says how
    far back the read got — the oldest events in the window were not examined.
    ``total`` stays ``None``: the read stops rather than counting the window.

    ``start`` / ``end`` (ISO 8601; no zone = UTC) choose the window instead of
    "the last ``hours``": ``start`` alone runs to now, ``end`` alone reaches back
    ``hours``. ``window`` echoes what was queried. Routine login/logout events
    (``ROUTINE_EVENT_TYPES``) that pass the severity filter are folded unless
    ``include_routine`` — counted per type in ``routine_folded``, with
    ``routine_note`` when any were.
    """
    explicit = start is not None or end is not None
    now = parse_event_time(end, "end") if end is not None else datetime.now(tz=timezone.utc)
    begin = parse_event_time(start, "start") if start is not None else now - timedelta(hours=hours)
    if begin >= now:
        raise ValueError(
            f"start ({begin.isoformat()}) must be before end ({now.isoformat()}). "
            f"Pass an earlier start or a later end."
        )
    content = si.RetrieveContent()
    event_mgr = content.eventManager

    filter_spec = vim.event.EventFilterSpec(
        time=vim.event.EventFilterSpec.ByTime(beginTime=begin, endTime=now)
    )

    read = read_events(event_mgr, filter_spec)
    events = () if read is None else read.events
    min_level = SEVERITY_ORDER.get(severity, 1)
    catalogue, coverage = _catalogue_and_coverage(event_mgr)

    results = []
    unclassified = 0
    folded: dict[str, int] = {}
    for event in events:
        event_type = _event_key(event)
        sev, sev_source = _event_severity_with_source(event, event_type, catalogue)
        if sev == "unknown":
            unclassified += 1

        if SEVERITY_ORDER.get(sev, 2) > min_level:
            continue
        short = short_event_type(event_type)
        if not include_routine and short in ROUTINE_EVENT_TYPES:
            folded[short] = folded.get(short, 0) + 1
            continue

        entity_name = _get_event_entity(event)
        suggestion_template = _EVENT_SUGGESTIONS.get(short)
        actions: list[str] = []
        if suggestion_template:
            actions.append(suggestion_template.format(entity=entity_name or "?"))

        results.append({
            "severity": sev,
            # Where the rank came from: "override" (this skill's own judgement),
            # "event" (vCenter set it on the event), "catalogue" (vCenter's
            # published description), "name_prefix" (inferred from the
            # esx.problem.* convention) or "unclassified". A caller filtering on
            # severity alone cannot tell a ranked event from an inferred one.
            "severity_source": sev_source,
            "event_type": event_type,
            "entity_name": entity_name,
            "message": sanitize(event.fullFormattedMessage or str(event), max_len=1000),
            "time": str(event.createdTime),
            "username": event.userName if hasattr(event, "userName") else "N/A",
            "suggested_actions": actions,
        })

    results.sort(key=lambda x: x["time"], reverse=True)
    truncated = bool(read and read.truncated)
    extra: dict = {
        "unclassified": unclassified,
        "read_truncated": truncated,
        "window": {"start": begin.isoformat(), "end": now.isoformat()},
        "routine_folded": folded,
    }
    if folded:
        count = sum(folded.values())
        breakdown = ", ".join(f"{n} {t}" for t, n in sorted(folded.items()))
        extra["routine_note"] = (
            f"{count} routine login/logout event(s) were folded ({breakdown}). Pass "
            f"include_routine=true (CLI --include-routine) to list them."
        )
    if truncated:
        oldest = getattr(events[-1], "createdTime", None) if events else None
        span = f"{begin.isoformat()} – {now.isoformat()}" if explicit else f"the last {hours}h"
        extra["read_note"] = (
            f"More than {MAX_EVENTS_READ} events matched {span}. The newest "
            f"{MAX_EVENTS_READ} were read (back to {oldest}); older events in the window "
            f"were not examined. Narrow hours to see the rest."
        )
    if coverage["ambiguous"]:
        # vCenter described more event types than we can key. Say so rather than
        # let a caller read a short "unknown" count as a well-understood estate:
        # pyVmomi hands back the same class for thousands of distinct
        # esx.problem.* descriptions, and entries that disagree are dropped
        # instead of overwriting one another.
        extra["catalogue_coverage"] = (
            f"vCenter described {coverage['described']} event types; "
            f"{coverage['usable']} could be keyed unambiguously and "
            f"{coverage['ambiguous']} key(s) were discarded because their "
            f"descriptions disagreed. Events under a discarded key are ranked "
            f"from the event's own severity or its name, not from the catalogue."
        )
    if unclassified:
        # Only when there is something to say — a note on every clean run is a
        # note nobody reads on the run that matters. Its own key rather than the
        # envelope's `hint`, which family-wide means "this page was truncated".
        extra["classification_note"] = (
            f"{unclassified} event(s) could not be ranked: neither this skill nor "
            f"vCenter's own event catalogue knows the type. They are included with "
            f"severity 'unknown' rather than filtered out — an unrankable event is "
            f"not evidence that nothing happened."
        )
    return paginated(results, **extra)


#: ESXi's CIM Server. Hardware sensors (``numericSensorInfo``) come from the CIM
#: providers it runs, so while it is stopped a host reports none.
_CIM_SERVER_SERVICE = "sfcbd-watchdog"


def _cim_server_states(si: ServiceInstance, hosts: list[tuple[str, object]]) -> list[dict]:
    """The CIM Server's running state and policy for each ``(host, serviceSystem)``.

    One batched read for all of them. A host whose service list could not be read
    gets ``None`` for both — unknown, not stopped.
    """
    refs = [ref for _name, ref in hosts if ref]
    try:
        info_by_ref = (
            {
                ref: props.get("serviceInfo")
                for ref, props in _collect_objects(si, refs, vim.HostServiceSystem, ["serviceInfo"])
            }
            if refs
            else {}
        )
    except Exception:
        # The sensor rows are already read. Losing them to a failed service
        # read would trade an answer for none, so every CIM state is unknown
        # instead — and the note says so for each host.
        info_by_ref = {}
    rows = []
    for name, ref in hosts:
        services = getattr(info_by_ref.get(ref) if ref else None, "service", None) or []
        cim = next((s for s in services if s.key == _CIM_SERVER_SERVICE), None)
        rows.append(
            {
                "host": name,
                "cim_server_running": None if cim is None else bool(cim.running),
                "cim_server_policy": None if cim is None or cim.policy is None else str(cim.policy),
            }
        )
    return rows


#: Host names quoted per group in a note, and rows kept in ``hosts_without_sensors``.
#: A fleet with the CIM Server stopped everywhere is one sentence, not one per host.
_MAX_HOSTS_NAMED = 10
_MAX_HOSTS_LISTED = 50


def _named_hosts(hosts: list[str]) -> str:
    more = len(hosts) - _MAX_HOSTS_NAMED
    return ", ".join(hosts[:_MAX_HOSTS_NAMED]) + (f" and {more} more" if more > 0 else "")


def _no_sensors_note(rows: list[dict]) -> str:
    groups = (
        (False, f"the CIM Server ({_CIM_SERVER_SERVICE}) is not running; ESXi reads hardware "
                f"sensors through it, so none are reported until it runs"),
        (True, "the CIM Server is running, so nothing on the host supplies sensors — usually "
               "no BMC/IPMI device or vendor CIM provider (scan_host_logs shows ipmi errors "
               "when the device is missing)"),
        (None, "the CIM Server's state could not be read, so why no sensors are reported is unknown"),
    )
    parts = [
        f"{_named_hosts(hosts)}: {why}"
        for state, why in groups
        if (hosts := [r["host"] for r in rows if r["cim_server_running"] is state])
    ]
    return f"{len(rows)} connected host(s) report no hardware sensors. " + "; ".join(parts) + "."


_SENSOR_INTERPRETATION_NOTE = (
    "A sensor status is vSphere's reported health for the named sensor. "
    "A red aggregate memory sensor alone does not identify a DIMM or slot, "
    "establish ECC errors, or predict host failure; use component-level "
    "diagnostics for those claims."
)


def get_host_hardware_status(si: ServiceInstance, limit: int | None = None) -> dict:
    """Get hardware sensor status for all hosts.

    Returns the family list envelope with a real ``total``: every host's sensor
    rows are collected before ``limit`` is applied.

    A connected host that reports no sensors is listed in ``hosts_without_sensors``
    with its CIM Server (``sfcbd-watchdog``) state, and ``sensors_note`` says what
    that state means. On the lab vCenter 8.0.3 (2026-09-15) the only output was a
    green "no data" line while the CIM Server — which supplies the sensors — was
    set to start and not running. A host vCenter cannot reach is not listed: its
    sensors were not read, which is not the same as having none.

    Args:
        si: vSphere ServiceInstance.
        limit: Max number of sensor rows to return (None = all).
    """
    # Batch name + healthSystemRuntime for every host in one PropertyCollector
    # call; the sensor list arrives inline instead of a lazy round-trip per host
    # (issue #31 class). healthSystemRuntime is a data object, not a managed ref.
    results = []
    no_sensors: list[tuple[str, object]] = []
    props = [
        "name",
        "runtime.connectionState",
        "runtime.healthSystemRuntime",
        "configManager.serviceSystem",
    ]
    for _obj, p in _collect(si, [vim.HostSystem], props):
        host_name = sanitize(p.get("name", ""))
        runtime_health = p.get("runtime.healthSystemRuntime")
        info = getattr(runtime_health, "systemHealthInfo", None) if runtime_health else None
        sensors = list(getattr(info, "numericSensorInfo", None) or []) if info else []
        if not sensors:
            if str(p.get("runtime.connectionState")) == "connected":
                no_sensors.append((host_name, p.get("configManager.serviceSystem")))
            continue
        for sensor in sensors:
            # Health (green/yellow/red) lives in healthState.key;
            # sensorType is the category (temperature/voltage/fan...).
            health = getattr(sensor, "healthState", None)
            status = str(health.key) if health is not None else "unknown"
            results.append({
                "host": host_name,
                "sensor_name": sanitize(sensor.name),
                "type": str(getattr(sensor, "sensorType", "unknown")),
                "reading": sensor.currentReading,
                "unit": sensor.baseUnits,
                "status": status,
            })
    total = len(results)
    if limit is not None:
        results = results[:limit]
    without = _cim_server_states(si, no_sensors)
    extra: dict = {
        "hosts_without_sensors": without[:_MAX_HOSTS_LISTED],
        "hosts_without_sensors_total": len(without),
    }
    if total:
        extra["interpretation_note"] = _SENSOR_INTERPRETATION_NOTE
    if without:
        extra["sensors_note"] = _no_sensors_note(without)
    return paginated(results, limit=limit, total=total, **extra)


def get_host_services(si: ServiceInstance, host_name: str | None = None) -> dict:
    """Get service status for hosts.

    Returns the family list envelope. No row limit exists here, and every
    matching host's services are enumerated, so ``total`` is real and
    ``truncated`` is False — i.e. "this is the whole picture".
    """
    # Pass 1: batch name + the serviceSystem reference for every host in one
    # PropertyCollector call (issue #31 class). serviceInfo itself lives on the
    # HostServiceSystem managed object, which a HostSystem container view cannot
    # cross.
    results = []
    hosts: list[tuple[str, object]] = []
    svc_refs: list[object] = []
    for _obj, p in _collect(si, [vim.HostSystem], ["name", "configManager.serviceSystem"]):
        name = p.get("name", "")
        if host_name and name != host_name:
            continue
        svc_system = p.get("configManager.serviceSystem")
        if not svc_system:
            continue
        hosts.append((name, svc_system))
        svc_refs.append(svc_system)
    # Pass 2: batch serviceInfo for every serviceSystem ref in ONE more call,
    # instead of one lazy read per matched host.
    info_by_ref = {
        ref: props.get("serviceInfo")
        for ref, props in _collect_objects(
            si, svc_refs, vim.HostServiceSystem, ["serviceInfo"]
        )
    }
    for name, svc_system in hosts:
        svc_info = info_by_ref.get(svc_system)
        if not svc_info:
            continue
        for svc in svc_info.service:
            results.append({
                "host": sanitize(name),
                "service": svc.key,
                "label": sanitize(svc.label),
                "running": svc.running,
                "policy": svc.policy,
            })
    return paginated(results, total=len(results))
