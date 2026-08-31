"""Health checks: alarms, events, hardware status, services."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pyVmomi import vim, vmodl
from vmware_policy import paginated, sanitize

from vmware_monitor.ops._collect import _collect, _collect_objects

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

# Event types by severity
CRITICAL_EVENTS = {
    "VmFailedToPowerOnEvent",
    "HostConnectionLostEvent",
    "HostShutdownEvent",
    "VmDiskFailedEvent",
    "DasHostFailedEvent",
    "DatastoreRemovedOnHostEvent",
}

WARNING_EVENTS = {
    "VmFailoverFailed",
    "DrsVmMigratedEvent",
    "DrsSoftRuleViolationEvent",
    "VmFailedToRebootGuestEvent",
    "DVPortgroupReconfiguredEvent",
    "VmGuestShutdownEvent",
    "HostIpChangedEvent",
    "BadUsernameSessionEvent",
}

INFO_EVENTS = {
    "VmPoweredOnEvent",
    "VmPoweredOffEvent",
    "VmMigratedEvent",
    "VmReconfiguredEvent",
    "UserLoginSessionEvent",
    "UserLogoutSessionEvent",
    "VmCreatedEvent",
    "VmRemovedEvent",
    "VmClonedEvent",
}

SEVERITY_ORDER = {"critical": 0, "warning": 1, "unknown": 1, "info": 2}

#: vCenter's own event categories, as published by
#: ``EventManager.description.eventInfo``, mapped onto this skill's three ranks.
#: "user" is an operator action (a login, a reconfigure) — routine unless one of
#: the override sets above says otherwise.
_VC_CATEGORY_RANK = {
    "error": "critical",
    "warning": "warning",
    "info": "info",
    "user": "info",
}

# Maps event type → suggested remediation skill/tool hint
_EVENT_SUGGESTIONS: dict[str, str] = {
    "VmFailedToPowerOnEvent": "vmware-aiops: vm_power_on(vm_name='{entity}')",
    "VmFailoverFailed": "vmware-aiops: vm_power_on(vm_name='{entity}')",
    "VmFailedToRebootGuestEvent": "vmware-aiops: vm_power_off then vm_power_on(vm_name='{entity}')",
    "HostConnectionLostEvent": "vmware-monitor: list_esxi_hosts — verify host is reachable",
    "HostShutdownEvent": "vmware-monitor: list_esxi_hosts — check if shutdown was intentional",
    "VmDiskFailedEvent": "vmware-storage: check datastore health; vmware-monitor: list_all_datastores",
    "DasHostFailedEvent": "vmware-monitor: list_esxi_hosts — check HA cluster status",
    "DatastoreRemovedOnHostEvent": "vmware-storage: rescan; vmware-monitor: list_all_datastores",
}


# Faults raised by QueryEvents on standalone ESXi (no event manager support).
# Only these mean "no events available here" — auth/network errors must
# propagate, otherwise a monitoring tool reports all-clear on failure.
# (vim.fault has no NotSupported class; vmodl.fault.NotSupported is the one.)
_NOT_SUPPORTED_FAULTS: tuple[type[Exception], ...] = (vmodl.fault.NotSupported,)


def query_events(event_mgr: vim.event.EventManager, filter_spec: vim.event.EventFilterSpec) -> list:
    """QueryEvents wrapper shared by ops.health and scanner.log_scanner.

    Standalone ESXi does not support QueryEvents — treat NotSupported as
    "no events". Everything else (auth, network, permission) re-raises.
    """
    try:
        return event_mgr.QueryEvents(filter_spec)
    except _NOT_SUPPORTED_FAULTS:
        return []


def _event_catalogue(event_mgr: object) -> dict[str, str]:
    """vCenter's ``event type key -> category`` map, minus the ambiguous keys.

    ``EventManager.description.eventInfo`` is vCenter telling us how it ranks
    every event type it knows. On a real VCF 9.1 vCenter it publishes 2328
    entries -- and they collapse to 443 distinct keys, because pyVmomi's VMODL
    declares ``EventDetail.key`` as a *type* and the thousands of distinct
    ``esx.problem.*`` types all arrive as the one class ``vim.event.EventEx``.

    The 1885 lost entries were not merely lost. Writing them into the same dict
    key meant the last one won, so every ``esx.problem.*`` event took the
    category of whichever description happened to be parsed last -- a confident
    wrong rank, which is worse than no rank. A key whose entries disagree is
    therefore dropped: ranking falls through to the event's own ``severity``,
    then to the name-prefix rule, then to "unknown", each of which says what it
    is (see ``severity_source``).

    Returns ``{}`` rather than raising when the property is missing (older or
    permission-restricted vCenter): the overrides still work, and losing the
    catalogue must not lose the events.
    """
    return _catalogue_and_coverage(event_mgr)[0]


def _catalogue_and_coverage(
    event_mgr: object,
) -> tuple[dict[str, str], dict[str, int]]:
    """The catalogue plus what had to be discarded to build it.

    The counts are reported to the caller rather than kept private: "vCenter
    described 2328 event types and we could use 443 of them" is the difference
    between a rank we do not have and a rank we silently guessed.
    """
    info = getattr(getattr(event_mgr, "description", None), "eventInfo", None)
    seen: dict[str, set[str]] = {}
    described = 0
    for detail in info or []:
        key = _catalogue_entry_key(detail)
        category = getattr(detail, "category", None)
        if not key or not category:
            continue
        described += 1
        seen.setdefault(key, set()).add(str(category).lower())
    catalogue = {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}
    coverage = {
        "described": described,
        "usable": len(catalogue),
        "ambiguous": len(seen) - len(catalogue),
    }
    return catalogue, coverage


def _catalogue_entry_key(detail: object) -> str:
    """The event type id this catalogue entry describes.

    Two shapes arrive in ``EventManager.description.eventInfo``, and they must be
    read differently. Measured on a live vCenter 8.0.3, 2196 entries:

    * 441 classic VMODL events. ``key`` is the pyVmomi class
      (``vim.event.AccountCreatedEvent``) and ``fullFormat`` is prose —
      "Account {spec.id} was created on host {host.name}". **None** of these
      contains a ``|``.
    * 1755 extended events. ``key`` is the single class ``vim.event.EventEx`` or
      ``vim.event.ExtendedEvent`` for *all* of them, so reading the class name
      collapsed 1755 distinct descriptions onto 2 keys — 80% of everything
      vCenter published about itself, discarded. **All** of these put the real
      id in ``fullFormat`` ahead of a ``|``::

          com.vmware.cis.CreatePermission|Permission created for user {User}...
          esx.problem.scsi.device.io.latency.high|Device {1} performance...

      which is exactly the value ``Event.eventTypeId`` carries, so the catalogue
      and :func:`_event_key` finally speak the same language.

    The ``|`` split is unambiguous because the two populations do not overlap:
    0 of 441 classic entries have one, 1755 of 1755 extended entries do, the
    1755 extracted ids are distinct (zero collisions) and none contains a space.

    ``key`` remains the fallback: an entry with no parseable id keeps the old
    behaviour rather than being dropped.

    Verified on 8.0.3 only. That the ids match what live events report was NOT
    confirmed — the lab produced 10 events, none of them extended — so the
    catalogue's own consistency is the evidence here, not a round trip.
    """
    full = getattr(detail, "fullFormat", None)
    if isinstance(full, str) and "|" in full:
        candidate = full.split("|", 1)[0].strip()
        # An id, not a sentence: vCenter's ids are dotted tokens. A prose
        # fullFormat that happens to contain a pipe must not become a key.
        if candidate and " " not in candidate:
            return candidate

    key = getattr(detail, "key", None)
    if key is None:
        return ""
    name = getattr(key, "__name__", None)
    return str(name) if name else str(key)


def _event_key(event: object) -> str:
    """What this event actually is.

    Modern vSphere emits most events as ``EventEx``/``ExtendedEvent``, where the
    Python class name is the literal string "EventEx" for all of them and the
    identity lives in ``eventTypeId`` (``esx.problem.scsi.device.io.latency.high``
    and the like). Keying on the class name collapsed every one of them into a
    single unmatched bucket — and reported "EventEx" to the user, which is not a
    thing anyone can look up.
    """
    return str(getattr(event, "eventTypeId", None) or type(event).__name__)


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
_PROBLEM_PREFIX = "esx.problem."


def _event_severity(event: object, key: str, catalogue: dict[str, str]) -> str:
    """Rank one event. See :func:`_event_severity_with_source` for the reasoning."""
    return _event_severity_with_source(event, key, catalogue)[0]


def _event_severity_with_source(
    event: object, key: str, catalogue: dict[str, str]
) -> tuple[str, str]:
    """Rank one event, and say where the rank came from.

    Order: this skill's judgement, then the event's own ``severity``, then
    vCenter's catalogue, then the ``esx.problem.`` naming convention, then
    "unknown".

    The override sets come first on purpose. vCenter files HostShutdownEvent
    under "info"; for a monitoring skill a host that shut down is critical, and
    that disagreement is the product rather than a defect.

    ``"unknown"`` is a real fifth answer and is NOT folded into "info". An event
    nobody can rank is not evidence that nothing happened, and defaulting it to
    the quietest rank is what let five warning-level events be reported as
    "No events above warning" (VCF 9.1, 2026-08-30).

    The source is returned because the five answers are not interchangeable. On
    the same estate 41 of 50 events ranked "unknown", every one of them an
    ``esx.problem.*`` -- and one of those was a full ramdisk. A caller that
    cannot tell "vCenter called this info" from "nothing would rank it" cannot
    tell a quiet estate from a blind one.
    """
    if key in CRITICAL_EVENTS:
        return "critical", "override"
    if key in WARNING_EVENTS:
        return "warning", "override"
    if key in INFO_EVENTS:
        return "info", "override"
    # EventEx carries its own severity inline; prefer it over the catalogue
    # lookup because a vendor-defined type may not be in the catalogue at all.
    # ExtendedEvent has no such field, which is why the fallbacks below exist.
    own = getattr(event, "severity", None)
    if own:
        rank = _VC_CATEGORY_RANK.get(str(own).lower())
        if rank:
            return rank, "event"
    from_catalogue = _VC_CATEGORY_RANK.get(catalogue.get(key, ""))
    if from_catalogue:
        return from_catalogue, "catalogue"
    if key.startswith(_PROBLEM_PREFIX):
        return "warning", "name_prefix"
    return "unknown", "unclassified"


def _get_event_entity(event: object) -> str | None:
    """Extract entity name from a pyVmomi event object."""
    for attr in ("vm", "host", "ds", "computeResource", "net"):
        obj = getattr(event, attr, None)
        if obj:
            name = getattr(obj, "name", None)
            return sanitize(name) if name else None
    return None


def _append_alarm(alarm_state: object, name_map: dict, results: list[dict]) -> None:
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
    alarm_name = sanitize(alarm_state.alarm.info.name)
    acknowledged = getattr(alarm_state, "acknowledged", False)

    actions: list[str] = []
    if not acknowledged:
        actions.append(
            f"vmware-aiops: acknowledge_vcenter_alarm"
            f"(entity_name='{entity_name}', alarm_name='{alarm_name}')"
        )
    actions.append(
        f"vmware-aiops: reset_vcenter_alarm"
        f"(entity_name='{entity_name}', alarm_name='{alarm_name}')"
    )

    results.append({
        "severity": severity_map.get(severity, severity),
        "alarm_name": alarm_name,
        "entity_name": entity_name,
        "entity_type": type(entity_ref).__name__,
        "time": str(alarm_state.time),
        "acknowledged": acknowledged,
        "suggested_actions": actions,
    })


def get_active_alarms(si: ServiceInstance, limit: int | None = None) -> dict:
    """Get all active/triggered alarms across the inventory.

    Returns the family list envelope with a real ``total``: every triggered
    alarm is collected and deduplicated before ``limit`` is applied.

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

    for triggered in triggered_lists:
        for alarm_state in triggered:
            _append_alarm(alarm_state, name_map, results)

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
    if limit is not None:
        unique = unique[:limit]
    return paginated(unique, limit=limit, total=total)


def get_recent_events(
    si: ServiceInstance,
    hours: int = 24,
    severity: str = "warning",
) -> dict:
    """Get recent events filtered by severity.

    Severity comes from this skill's own override sets first and vCenter's
    published event catalogue second (see :func:`_event_severity`). An event
    neither can rank is returned with severity ``"unknown"`` and counted in the
    envelope's ``unclassified`` — it is never quietly demoted to ``"info"``,
    which is how five warning-level events came back as "No events above
    warning" (VCF 9.1, 2026-08-30).

    Returns the family list envelope. ``total`` is deliberately left ``None``:
    QueryEvents applies its own server-side collector bounds, so the number of
    events matching the window is not something this code actually knows. No
    row limit is applied here, so ``truncated`` is False either way — the null
    total is the honest statement that the window itself may hide more.
    """
    content = si.RetrieveContent()
    event_mgr = content.eventManager

    now = datetime.now(tz=timezone.utc)
    begin = now - timedelta(hours=hours)

    filter_spec = vim.event.EventFilterSpec(
        time=vim.event.EventFilterSpec.ByTime(beginTime=begin, endTime=now)
    )

    events = query_events(event_mgr, filter_spec)
    min_level = SEVERITY_ORDER.get(severity, 1)
    catalogue, coverage = _catalogue_and_coverage(event_mgr)

    results = []
    unclassified = 0
    for event in events:
        event_type = _event_key(event)
        sev, sev_source = _event_severity_with_source(event, event_type, catalogue)
        if sev == "unknown":
            unclassified += 1

        if SEVERITY_ORDER.get(sev, 2) > min_level:
            continue

        entity_name = _get_event_entity(event)
        suggestion_template = _EVENT_SUGGESTIONS.get(event_type)
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
    extra: dict = {"unclassified": unclassified}
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


def get_host_hardware_status(si: ServiceInstance, limit: int | None = None) -> dict:
    """Get hardware sensor status for all hosts.

    Returns the family list envelope with a real ``total``: every host's sensor
    rows are collected before ``limit`` is applied.

    Args:
        si: vSphere ServiceInstance.
        limit: Max number of sensor rows to return (None = all).
    """
    # Batch name + healthSystemRuntime for every host in one PropertyCollector
    # call; the sensor list arrives inline instead of a lazy round-trip per host
    # (issue #31 class). healthSystemRuntime is a data object, not a managed ref.
    results = []
    for _obj, p in _collect(si, [vim.HostSystem], ["name", "runtime.healthSystemRuntime"]):
        runtime_health = p.get("runtime.healthSystemRuntime")
        if not runtime_health or not runtime_health.systemHealthInfo:
            continue
        host_name = sanitize(p.get("name", ""))
        for sensor in runtime_health.systemHealthInfo.numericSensorInfo:
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
    return paginated(results, limit=limit, total=total)


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
