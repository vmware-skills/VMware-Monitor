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
    """vCenter's ``event type key -> category`` map, or ``{}`` if unavailable.

    ``EventManager.description.eventInfo`` is vCenter telling us how it ranks
    every event type it knows — roughly a thousand of them. The three sets above
    name 23. Consulting this instead of guessing is the whole fix; consulting it
    is also one property read, not a round trip per event.

    Returns ``{}`` rather than raising when the property is missing (older or
    permission-restricted vCenter): the overrides still work, and losing the
    catalogue must not lose the events.
    """
    info = getattr(getattr(event_mgr, "description", None), "eventInfo", None)
    catalogue: dict[str, str] = {}
    for detail in info or []:
        key = getattr(detail, "key", None)
        category = getattr(detail, "category", None)
        if key and category:
            catalogue[str(key)] = str(category).lower()
    return catalogue


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


def _event_severity(event: object, key: str, catalogue: dict[str, str]) -> str:
    """Rank one event: this skill's judgement first, then vCenter's, then unknown.

    The override sets come first on purpose. vCenter files HostShutdownEvent
    under "info"; for a monitoring skill a host that shut down is critical, and
    that disagreement is the product rather than a defect.

    ``"unknown"`` is a real fourth answer and is NOT folded into "info". An
    event nobody can rank is not evidence that nothing happened, and defaulting
    it to the quietest rank is what let five warning-level events be reported as
    "No events above warning" (VCF 9.1, 2026-08-30).
    """
    if key in CRITICAL_EVENTS:
        return "critical"
    if key in WARNING_EVENTS:
        return "warning"
    if key in INFO_EVENTS:
        return "info"
    # EventEx carries its own severity inline; prefer it over the catalogue
    # lookup because a vendor-defined type may not be in the catalogue at all.
    own = getattr(event, "severity", None)
    if own:
        rank = _VC_CATEGORY_RANK.get(str(own).lower())
        if rank:
            return rank
    return _VC_CATEGORY_RANK.get(catalogue.get(key, ""), "unknown")


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
    catalogue = _event_catalogue(event_mgr)

    results = []
    unclassified = 0
    for event in events:
        event_type = _event_key(event)
        sev = _event_severity(event, event_type, catalogue)
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
            "event_type": event_type,
            "entity_name": entity_name,
            "message": sanitize(event.fullFormattedMessage or str(event), max_len=1000),
            "time": str(event.createdTime),
            "username": event.userName if hasattr(event, "userName") else "N/A",
            "suggested_actions": actions,
        })

    results.sort(key=lambda x: x["time"], reverse=True)
    extra: dict = {"unclassified": unclassified}
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
