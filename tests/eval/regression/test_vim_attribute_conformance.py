"""pyVmomi vim-API conformance — regression net for the whole "method doesn't
exist" bug class (2026-06 audit: Folder.MoveInto_Task, AlarmManager.SetAlarmStatus).

Validates every vSphere property chain and managed-object method this skill
family relies on against pyVmomi's own type metadata (``_propList`` /
``_methodInfo`` + wsdlNames), with inheritance walking and array unwrapping.
A chain or method that does not resolve fails at test time instead of at a
customer site.

Generic by design: this exact file is installed across the VMware skill family
(AIops / Monitor, later Storage / VKS). It has no repo-specific imports — the
manifest is a superset of what the family uses; pyVmomi metadata is identical
everywhere. The source scan discovers this repo's packages automatically.

Contributors: when ops code starts using a new vim chain or method, add it to
PROPERTY_CHAINS / METHODS below. Never remove FORBIDDEN entries.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pyVmomi import vim

# ---------------------------------------------------------------------------
# Introspection helpers (inheritance walk + array unwrap)
# ---------------------------------------------------------------------------


def _resolve_type(name: str):
    """Resolve 'alarm.AlarmManager' → vim.alarm.AlarmManager."""
    obj = vim
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


def _props_of(t) -> dict:
    """All properties of a vim type incl. inherited: {name: type}."""
    out: dict = {}
    for klass in getattr(t, "__mro__", []):
        for p in vars(klass).get("_propList") or []:
            out.setdefault(p.name, p.type)
    return out


def _unwrap_array(t):
    """vim.X[] array types expose the element type as .Item."""
    return getattr(t, "Item", t)


def _find_method(t, name: str):
    """Find a managed method by Python name or wsdlName, walking inheritance."""
    for klass in getattr(t, "__mro__", []):
        for key, info in (vars(klass).get("_methodInfo") or {}).items():
            if name in (key, info.wsdlName):
                return info
    return None


def _check_chain(start_name: str, chain: str) -> str | None:
    """Return None if the chain resolves, else a diagnostic message."""
    try:
        t = _resolve_type(start_name)
    except AttributeError:
        return f"start type vim.{start_name} not found"
    path = start_name
    for attr in chain.split("."):
        t = _unwrap_array(t)
        props = _props_of(t)
        if attr in props:
            t = props[attr]
            path += "." + attr
            continue
        if _find_method(t, attr) is not None:  # chain may end in a method ref
            return None
        near = [k for k in props if attr.lower() in k.lower() or k.lower() in attr.lower()]
        return (
            f"'{attr}' not on {getattr(t, '__name__', t)} "
            f"(after {path}); near={near[:6]}"
        )
    return None


# ---------------------------------------------------------------------------
# Manifest — every vim property chain the family's ops code reads
# ---------------------------------------------------------------------------

PROPERTY_CHAINS = [
    # ServiceInstance / ServiceContent
    ("ServiceInstance", "content"),
    ("ServiceContent", "rootFolder"),
    ("ServiceContent", "viewManager"),
    ("ServiceContent", "eventManager"),
    ("ServiceContent", "alarmManager"),
    ("ServiceContent", "taskManager"),
    ("ServiceContent", "ovfManager"),
    ("ServiceContent", "guestOperationsManager"),
    ("ServiceContent", "searchIndex"),
    ("ServiceContent", "about.version"),
    ("ServiceContent", "about.apiVersion"),
    ("view.ContainerView", "view"),
    # VirtualMachine
    ("VirtualMachine", "name"),
    ("VirtualMachine", "runtime.powerState"),
    ("VirtualMachine", "runtime.host.name"),
    ("VirtualMachine", "runtime.bootTime"),
    ("VirtualMachine", "summary.quickStats.overallCpuUsage"),
    ("VirtualMachine", "summary.quickStats.guestMemoryUsage"),
    ("VirtualMachine", "summary.config.numCpu"),
    ("VirtualMachine", "summary.config.memorySizeMB"),
    ("VirtualMachine", "summary.storage.committed"),
    ("VirtualMachine", "guest.ipAddress"),
    ("VirtualMachine", "guest.guestState"),
    ("VirtualMachine", "guest.toolsRunningStatus"),
    ("VirtualMachine", "guest.guestFullName"),
    ("VirtualMachine", "config.template"),
    ("VirtualMachine", "config.uuid"),
    ("VirtualMachine", "config.hardware.device"),
    ("VirtualMachine", "config.annotation"),
    ("VirtualMachine", "snapshot.rootSnapshotList.name"),
    ("VirtualMachine", "snapshot.rootSnapshotList.snapshot"),
    ("VirtualMachine", "snapshot.rootSnapshotList.childSnapshotList"),
    ("VirtualMachine", "snapshot.currentSnapshot"),
    ("VirtualMachine", "datastore.name"),
    ("VirtualMachine", "network"),
    ("VirtualMachine", "resourcePool"),
    ("VirtualMachine", "parent"),
    ("VirtualMachine", "triggeredAlarmState.overallStatus"),
    ("VirtualMachine", "triggeredAlarmState.acknowledged"),
    ("VirtualMachine", "triggeredAlarmState.time"),
    ("VirtualMachine", "triggeredAlarmState.entity.name"),
    ("VirtualMachine", "triggeredAlarmState.alarm.info.name"),
    # HostSystem
    ("HostSystem", "name"),
    ("HostSystem", "vm.name"),
    ("HostSystem", "parent"),
    ("HostSystem", "runtime.inMaintenanceMode"),
    ("HostSystem", "runtime.connectionState"),
    ("HostSystem", "runtime.powerState"),
    # H2 regression: status is healthState.key, sensorType is the category
    ("HostSystem", "runtime.healthSystemRuntime.systemHealthInfo"
                   ".numericSensorInfo.healthState.key"),
    ("HostSystem", "runtime.healthSystemRuntime.systemHealthInfo"
                   ".numericSensorInfo.sensorType"),
    ("HostSystem", "runtime.healthSystemRuntime.systemHealthInfo"
                   ".numericSensorInfo.currentReading"),
    ("HostSystem", "runtime.healthSystemRuntime.systemHealthInfo"
                   ".numericSensorInfo.baseUnits"),
    ("HostSystem", "configManager.serviceSystem.serviceInfo.service.key"),
    ("HostSystem", "configManager.serviceSystem.serviceInfo.service.running"),
    ("HostSystem", "configManager.serviceSystem.serviceInfo.service.policy"),
    ("HostSystem", "configManager.storageSystem.storageDeviceInfo"),
    ("HostSystem", "configManager.datastoreSystem"),
    ("HostSystem", "configManager.networkSystem"),
    ("HostSystem", "summary.quickStats.overallCpuUsage"),
    ("HostSystem", "summary.quickStats.overallMemoryUsage"),
    ("HostSystem", "summary.hardware.cpuMhz"),
    ("HostSystem", "summary.hardware.numCpuCores"),
    ("HostSystem", "summary.hardware.memorySize"),
    ("HostSystem", "hardware.memorySize"),
    ("HostSystem", "datastore.name"),
    # ClusterComputeResource
    ("ClusterComputeResource", "name"),
    ("ClusterComputeResource", "host.name"),
    ("ClusterComputeResource", "parent"),
    ("ClusterComputeResource", "resourcePool"),
    ("ClusterComputeResource", "configurationEx"),
    ("ClusterComputeResource", "summary.numHosts"),
    # Datacenter / Folder
    ("Datacenter", "name"),
    ("Datacenter", "hostFolder"),
    ("Datacenter", "vmFolder"),
    ("Datacenter", "datastoreFolder"),
    ("Datacenter", "networkFolder"),
    ("Datacenter", "datastore"),
    ("Folder", "childEntity"),
    # Datastore — H1 regression: .host is HostMount[]; HostSystem is in .key
    ("Datastore", "name"),
    ("Datastore", "host.key.name"),
    ("Datastore", "host.mountInfo.accessible"),
    ("Datastore", "summary.capacity"),
    ("Datastore", "summary.freeSpace"),
    ("Datastore", "summary.accessible"),
    ("Datastore", "browser"),
    # Tasks
    ("Task", "info.state"),
    ("TaskInfo", "state"),
    ("TaskInfo", "error"),
    ("TaskInfo", "result"),
    ("TaskInfo", "progress"),
    ("TaskInfo", "entityName"),
    ("TaskInfo", "descriptionId"),
    # Alarms — C2 regression: AlarmFilterSpec has ONLY these three fields
    ("alarm.AlarmFilterSpec", "status"),
    ("alarm.AlarmFilterSpec", "typeEntity"),
    ("alarm.AlarmFilterSpec", "typeTrigger"),
    ("alarm.AlarmState", "alarm.info.name"),
    ("alarm.AlarmState", "entity.name"),
    ("alarm.AlarmState", "overallStatus"),
    ("alarm.AlarmState", "acknowledged"),
    # Events
    ("event.Event", "fullFormattedMessage"),
    ("event.Event", "createdTime"),
    ("event.EventFilterSpec", "time"),
    ("event.EventFilterSpec.ByTime", "beginTime"),
    ("event.EventFilterSpec.ByTime", "endTime"),
    # Guest operations
    ("vm.guest.GuestOperationsManager", "processManager"),
    ("vm.guest.GuestOperationsManager", "fileManager"),
    # OVF / HttpNfcLease
    ("HttpNfcLease", "info.deviceUrl"),
    ("HttpNfcLease", "state"),
    # --- Observability expansion (perf/snapshots/infra/capacity/activity) ---
    # Performance counters
    ("ServiceContent", "perfManager"),
    ("PerformanceManager", "perfCounter.key"),
    ("PerformanceManager", "perfCounter.groupInfo.key"),
    ("PerformanceManager", "perfCounter.nameInfo.key"),
    ("PerformanceManager", "perfCounter.rollupType"),
    # Snapshot aging / sprawl
    ("VirtualMachine", "snapshot.rootSnapshotList.createTime"),
    ("VirtualMachine", "snapshot.rootSnapshotList.description"),
    ("VirtualMachine", "layoutEx.file.type"),
    ("VirtualMachine", "layoutEx.file.size"),
    # Infra health — certificates, licenses, NTP
    ("HostSystem", "configManager.certificateManager.certificateInfo.notAfter"),
    ("HostSystem", "config.dateTimeInfo.ntpConfig.server"),
    ("ServiceContent", "licenseManager"),
    ("LicenseManager", "licenses.name"),
    ("LicenseManager", "licenses.editionKey"),
    ("LicenseManager", "licenses.total"),
    ("LicenseManager", "licenses.used"),
    ("LicenseManager", "licenses.properties.key"),
    ("LicenseManager", "licenses.properties.value"),
    # Capacity — datastore over-commit + resource pools
    ("Datastore", "summary.type"),
    ("Datastore", "summary.uncommitted"),
    ("ResourcePool", "name"),
    ("ResourcePool", "config.cpuAllocation.reservation"),
    ("ResourcePool", "config.cpuAllocation.limit"),
    ("ResourcePool", "config.memoryAllocation.reservation"),
    ("ResourcePool", "config.memoryAllocation.limit"),
    ("ResourcePool", "summary.quickStats.overallCpuUsage"),
    ("ResourcePool", "summary.quickStats.guestMemoryUsage"),
    # Activity — tasks + sessions
    ("ServiceContent", "sessionManager"),
    ("TaskManager", "recentTask"),
    ("TaskInfo", "startTime"),
    ("TaskInfo", "key"),
    ("TaskInfo", "reason"),
    ("TaskInfo", "error.msg"),
    ("TaskReasonUser", "userName"),
    ("SessionManager", "currentSession.key"),
    ("SessionManager", "sessionList.userName"),
    ("SessionManager", "sessionList.fullName"),
    ("SessionManager", "sessionList.loginTime"),
    ("SessionManager", "sessionList.lastActiveTime"),
    ("SessionManager", "sessionList.ipAddress"),
    ("SessionManager", "sessionList.key"),
]

# ---------------------------------------------------------------------------
# Manifest — every managed-object method the family's ops code calls
# ---------------------------------------------------------------------------

METHODS = [
    ("ServiceInstance", "RetrieveContent"),
    ("ServiceInstance", "CurrentTime"),
    ("view.ViewManager", "CreateContainerView"),
    ("view.ContainerView", "Destroy"),
    ("event.EventManager", "QueryEvents"),
    ("event.EventManager", "CreateCollectorForEvents"),
    ("event.EventHistoryCollector", "ReadPreviousEvents"),
    ("HistoryCollector", "SetCollectorPageSize"),
    ("HistoryCollector", "ResetCollector"),
    ("HistoryCollector", "DestroyCollector"),
    ("PerformanceManager", "QueryPerf"),
    ("PerformanceManager", "QueryPerfProviderSummary"),
    # C1 regression: the Folder method is MoveIntoFolder_Task (param 'list');
    # plain MoveInto_Task exists only on ClusterComputeResource (param 'host').
    ("Folder", "MoveIntoFolder_Task"),
    ("Folder", "CreateFolder"),
    ("Folder", "CreateClusterEx"),
    ("Folder", "CreateVM_Task"),
    ("ClusterComputeResource", "MoveInto_Task"),
    ("ClusterComputeResource", "AddHost_Task"),
    ("ComputeResource", "ReconfigureComputeResource_Task"),
    ("ManagedEntity", "Destroy_Task"),
    ("ManagedEntity", "Rename_Task"),
    # VirtualMachine lifecycle
    ("VirtualMachine", "PowerOn"),
    ("VirtualMachine", "PowerOff"),
    ("VirtualMachine", "Reset"),
    ("VirtualMachine", "Suspend"),
    ("VirtualMachine", "ShutdownGuest"),
    ("VirtualMachine", "RebootGuest"),
    ("VirtualMachine", "Relocate"),
    ("VirtualMachine", "Clone"),
    ("VirtualMachine", "CreateSnapshot_Task"),
    ("VirtualMachine", "ReconfigVM_Task"),
    ("VirtualMachine", "MarkAsTemplate"),
    ("VirtualMachine", "MarkAsVirtualMachine"),
    ("vm.Snapshot", "RevertToSnapshot_Task"),
    ("vm.Snapshot", "RemoveSnapshot_Task"),
    # Host maintenance / storage
    ("HostSystem", "EnterMaintenanceMode_Task"),
    ("HostSystem", "ExitMaintenanceMode_Task"),
    ("host.StorageSystem", "AddInternetScsiSendTargets"),
    ("host.StorageSystem", "RemoveInternetScsiSendTargets"),
    ("host.StorageSystem", "RescanAllHba"),
    ("host.StorageSystem", "RescanVmfs"),
    ("host.StorageSystem", "UpdateSoftwareInternetScsiEnabled"),
    ("host.DatastoreBrowser", "SearchDatastoreSubFolders_Task"),
    # Alarms — C2 regression
    ("alarm.AlarmManager", "AcknowledgeAlarm"),
    ("alarm.AlarmManager", "ClearTriggeredAlarms"),
    # OVF deploy
    ("OvfManager", "ParseDescriptor"),
    ("OvfManager", "CreateImportSpec"),
    ("ResourcePool", "ImportVApp"),
    ("HttpNfcLease", "Complete"),
    ("HttpNfcLease", "Abort"),
    ("HttpNfcLease", "Progress"),
    # Guest operations
    ("vm.guest.ProcessManager", "StartProgramInGuest"),
    ("vm.guest.ProcessManager", "ListProcessesInGuest"),
    ("vm.guest.ProcessManager", "TerminateProcessInGuest"),
    ("vm.guest.FileManager", "InitiateFileTransferToGuest"),
    ("vm.guest.FileManager", "InitiateFileTransferFromGuest"),
    ("vm.guest.FileManager", "MakeDirectoryInGuest"),
    ("vm.guest.FileManager", "DeleteFileInGuest"),
]

# Methods that do NOT exist in the vSphere API. If pyVmomi ever grows them,
# these tests flag it so the manifest (and any workaround) can be revisited.
FORBIDDEN_METHODS = [
    ("Folder", "MoveInto_Task"),        # C1: hallucinated; use MoveIntoFolder_Task
    ("alarm.AlarmManager", "SetAlarmStatus"),  # C2: hallucinated; use ClearTriggeredAlarms
]

# Source patterns that must never reappear in shipped code (regex, scanned
# over this repo's package dirs — tests excluded).
FORBIDDEN_SOURCE_PATTERNS = [
    (r"SetAlarmStatus", "AlarmManager.SetAlarmStatus does not exist — use ClearTriggeredAlarms"),
    (r"[Ff]older\.MoveInto_Task\b",
     "vim.Folder has no MoveInto_Task — use MoveIntoFolder_Task(list=[...])"),
    (r"DVPortGroupReconfiguredEvent",
     "event class is DVPortgroupReconfiguredEvent (lowercase g)"),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "type_name,chain", PROPERTY_CHAINS, ids=[f"{t}.{c}" for t, c in PROPERTY_CHAINS]
)
def test_property_chain_resolves(type_name: str, chain: str) -> None:
    err = _check_chain(type_name, chain)
    assert err is None, f"vim.{type_name} :: {chain} — {err}"


@pytest.mark.parametrize("type_name,method", METHODS, ids=[f"{t}.{m}" for t, m in METHODS])
def test_method_exists(type_name: str, method: str) -> None:
    t = _resolve_type(type_name)
    assert _find_method(t, method) is not None, (
        f"vim.{type_name}.{method} not found in _methodInfo/wsdlNames — "
        "method does not exist in this pyVmomi version"
    )


@pytest.mark.parametrize(
    "type_name,method", FORBIDDEN_METHODS, ids=[f"{t}.{m}" for t, m in FORBIDDEN_METHODS]
)
def test_forbidden_method_still_absent(type_name: str, method: str) -> None:
    t = _resolve_type(type_name)
    assert _find_method(t, method) is None, (
        f"vim.{type_name}.{method} now exists in pyVmomi — "
        "revisit FORBIDDEN_METHODS and any workaround code"
    )


def test_move_into_folder_task_signature() -> None:
    """Lock the calling convention: single 'list' param of ManagedEntity[],
    so positional MoveIntoFolder_Task([entity]) is unambiguous and correct."""
    info = _find_method(vim.Folder, "MoveIntoFolder_Task")
    assert info is not None
    params = [(p.name, getattr(p.type, "__name__", str(p.type))) for p in info.params]
    assert params == [("list", "vim.ManagedEntity[]")], params


def test_clear_triggered_alarms_signature() -> None:
    """Lock the calling convention: single 'filter' param of AlarmFilterSpec."""
    info = _find_method(vim.alarm.AlarmManager, "ClearTriggeredAlarms")
    assert info is not None
    params = [(p.name, getattr(p.type, "__name__", str(p.type))) for p in info.params]
    assert params == [("filter", "vim.alarm.AlarmFilterSpec")], params


def _repo_source_files() -> list[Path]:
    """This repo's shipped python packages (top-level dirs with __init__.py,
    excluding tests). Works unchanged in every family repo."""
    repo_root = Path(__file__).resolve().parents[3]
    files: list[Path] = []
    for child in sorted(repo_root.iterdir()):
        if child.name.startswith(".") or child.name == "tests":
            continue
        if child.is_dir() and (child / "__init__.py").exists():
            files.extend(sorted(child.rglob("*.py")))
    return files


@pytest.mark.parametrize(
    "pattern,why", FORBIDDEN_SOURCE_PATTERNS, ids=[p for p, _ in FORBIDDEN_SOURCE_PATTERNS]
)
def test_forbidden_pattern_absent_from_source(pattern: str, why: str) -> None:
    rx = re.compile(pattern)
    hits = []
    for path in _repo_source_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if rx.search(line):
                hits.append(f"{path}:{lineno}: {line.strip()}")
    assert not hits, f"{why}\n" + "\n".join(hits)
