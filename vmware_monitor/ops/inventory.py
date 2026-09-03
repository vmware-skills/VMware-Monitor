"""Inventory queries for vCenter/ESXi resources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import paginated, sanitize

from vmware_monitor.ops._collect import _collect

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance


def folder_path(managed_entity) -> str:
    """Return the vCenter inventory folder path for a managed entity.

    Walks ``parent`` references up the inventory tree, stopping at the
    enclosing Datacenter. The Datacenter's root vmFolder (named ``vm`` by
    default) is omitted so the returned path matches what users see in the
    vSphere UI under "VMs and Templates".

    Args:
        managed_entity: Any vim.ManagedEntity (VirtualMachine, Folder, etc.).

    Returns:
        Forward-slash separated path beginning with ``/``. Returns ``"/"`` for
        entities that sit directly in the datacenter vmFolder. vApps are
        included in the path as folders.

    Examples:
        ``"/"`` — VM at the root of the datacenter vmFolder
        ``"/Colocation"`` — VM in a top-level "Colocation" folder
        ``"/Colocation/Colo - ISER"`` — VM nested in a department subfolder
        ``"/My vApp"`` — VM inside a vApp at the root
    """
    parts: list[str] = []
    p = getattr(managed_entity, "parent", None)
    # Walk upward, stopping at the Datacenter boundary so the path is
    # relative to the dc-level vmFolder (which is itself omitted).
    while p is not None:
        if isinstance(p, vim.Datacenter):
            break
        # Skip the dc's root vmFolder ("vm"); its direct parent is the dc.
        if isinstance(p, vim.Folder) and isinstance(getattr(p, "parent", None), vim.Datacenter):
            break
        parts.append(sanitize(p.name))
        p = getattr(p, "parent", None)
    if not parts:
        return "/"
    return "/" + "/".join(reversed(parts))


_VM_SORT_KEYS = {"name", "cpu", "memory_mb", "power_state", "folder_path"}
_COMPACT_FIELDS = ("name", "power_state", "cpu", "memory_mb", "folder_path")
_VM_VALID_FIELDS = {
    "name",
    "power_state",
    "cpu",
    "memory_mb",
    "guest_os",
    "ip_address",
    "host",
    "uuid",
    "tools_status",
    "folder_path",
}
_VM_PROPS = [
    "name",
    "parent",
    "runtime.powerState",
    "runtime.host",
    "config.hardware.numCPU",
    "config.hardware.memoryMB",
    "config.guestFullName",
    "config.uuid",
    "guest.ipAddress",
    "guest.toolsRunningStatus",
]


def _folder_map(si: ServiceInstance) -> dict:
    """Map moRef -> (name, parent_moRef, is_datacenter) for path-bearing containers.

    Fetches ``name``/``parent`` for every Folder, vApp, and Datacenter in three
    batched calls so ``_resolve_folder_path`` can walk the inventory tree locally
    instead of triggering a round-trip per ancestor per VM.
    """
    fmap: dict = {}
    for obj_type, is_dc in (
        ([vim.Folder], False),
        ([vim.VirtualApp], False),
        ([vim.Datacenter], True),
    ):
        for obj, p in _collect(si, obj_type, ["name", "parent"]):
            fmap[obj] = (sanitize(p.get("name", "")), p.get("parent"), is_dc)
    return fmap


def _resolve_folder_path(parent_ref, fmap: dict) -> str:
    """Compute a VM's folder path from a prefetched ``_folder_map``.

    Mirrors ``folder_path()`` but resolves ancestors from the local map. Stops at
    the Datacenter boundary and omits the datacenter's root ``vm`` folder so the
    result matches the vSphere "VMs and Templates" view.
    """
    parts: list[str] = []
    p = parent_ref
    while p is not None:
        entry = fmap.get(p)
        if entry is None:
            break
        name, grandparent, is_dc = entry
        if is_dc:
            break
        # Skip the dc's root vmFolder ("vm"): its direct parent is a Datacenter.
        gp = fmap.get(grandparent)
        if gp is not None and gp[2]:
            break
        parts.append(name)
        p = grandparent
    if not parts:
        return "/"
    return "/" + "/".join(reversed(parts))


def list_vms(
    si: ServiceInstance,
    limit: int | None = None,
    sort_by: str = "name",
    power_state: str | None = None,
    fields: list[str] | None = None,
    folder_filter: str | None = None,
    name_filter: str | None = None,
    compact_threshold: int = 50,
) -> dict:
    """List virtual machines with optional filtering, sorting, and field selection.

    Returns the family list envelope ``{items, returned, limit, total,
    truncated, hint}`` plus one extra key, ``mode`` (``"full"`` or
    ``"compact"``). ``total`` is the count after filtering and before ``limit``,
    so a full page that matches it is reported complete rather than
    possibly-truncated.

    ``hint`` carries the compact-mode suggestion when auto-compact fires; when a
    page is also truncated both notes are joined, so neither is lost.

    .. deprecated:: 1.8.6
       ``vms`` is a compatibility alias for ``items`` and will be removed in
       2.0. Until v1.8.0 this function returned ``{total, mode, vms, hint}``;
       the envelope renamed ``vms`` to ``items``, and because the payload was
       already a keyed dict the break was silent — ``result.get("vms", [])``
       started returning ``[]``, which in a monitoring tool reads as "no VMs"
       rather than as a failure. Both keys are the *same* list object, so they
       cannot drift. Migrate to ``items``.

    Auto-compact: when no explicit limit/fields are set and total VMs exceed
    compact_threshold (default 50), only compact fields are returned to keep
    context manageable. Use limit or fields to override.

    Args:
        si: vSphere ServiceInstance.
        limit: Max number of VMs to return (None = all).
        sort_by: Sort field: "name" | "cpu" | "memory_mb" | "power_state" | "folder_path".
        power_state: Filter by power state: "poweredOn" | "poweredOff" | "suspended".
        fields: Return only these fields (None = auto).
            Available: name, power_state, cpu, memory_mb, guest_os, ip_address,
                       host, uuid, tools_status, folder_path.
        folder_filter: Case-insensitive substring match against folder_path.
        name_filter: Case-insensitive substring match against the VM name. The
            one filter this was missing: on an estate with thousands of VMs there
            was no way to find one by name, so a caller told to "find the VM with
            list_virtual_machines" reached for folder_filter and passed it a VM
            name (issue #26, field report 2026-09-01). An error whose remedy
            cannot be carried out is worse than no remedy.
            Example: "Colocation" returns VMs anywhere under a Colocation folder
            (including nested subfolders like /Colocation/Colo - ISER).
        compact_threshold: Auto-compact when VM count exceeds this (default 50).
    """
    # Batched lookups: host moRef -> name, and the folder tree, so per-VM host
    # and folder_path resolution don't each trigger round-trips.
    host_names = {obj: p.get("name") for obj, p in _collect(si, [vim.HostSystem], ["name"])}
    fmap = _folder_map(si)

    results = []
    for _obj, p in _collect(si, [vim.VirtualMachine], _VM_PROPS):
        host_ref = p.get("runtime.host")
        guest_os = p.get("config.guestFullName")
        tools = p.get("guest.toolsRunningStatus")
        entry = {
            "name": sanitize(p.get("name", "")),
            "power_state": str(p.get("runtime.powerState", "N/A")),
            "cpu": p.get("config.hardware.numCPU") or 0,
            "memory_mb": p.get("config.hardware.memoryMB") or 0,
            "guest_os": sanitize(guest_os) if guest_os else "N/A",
            "ip_address": p.get("guest.ipAddress"),
            "host": sanitize(host_names.get(host_ref) or "N/A") if host_ref else "N/A",
            "uuid": p.get("config.uuid") or "N/A",
            "tools_status": str(tools) if tools else "N/A",
            "folder_path": _resolve_folder_path(p.get("parent"), fmap),
        }
        results.append(entry)

    # Filter by power state
    if power_state:
        results = [r for r in results if power_state.lower() in r["power_state"].lower()]

    # Filter by folder path (case-insensitive substring)
    if folder_filter:
        needle = folder_filter.lower()
        results = [r for r in results if needle in r["folder_path"].lower()]

    # Filter by VM name (case-insensitive substring)
    if name_filter:
        needle = name_filter.lower()
        results = [r for r in results if needle in r["name"].lower()]

    # Sort
    sort_key = sort_by if sort_by in _VM_SORT_KEYS else "name"
    results = sorted(results, key=lambda x: x[sort_key])

    total = len(results)

    # Limit
    if limit is not None and limit > 0:
        results = results[:limit]

    # Determine mode and field selection
    explicit_fields = bool(fields)
    explicit_limit = limit is not None and limit > 0

    if not explicit_fields and not explicit_limit and total > compact_threshold:
        # Auto-compact: large inventory, no explicit constraints
        mode = "compact"
        results = [{k: r[k] for k in _COMPACT_FIELDS if k in r} for r in results]
        compact_hint = (
            f"Large inventory ({total} VMs): showing compact fields only. "
            "Use --limit N or --fields to get full details."
        )
    else:
        mode = "full"
        compact_hint = None
        if fields:
            keep = [f for f in fields if f in _VM_VALID_FIELDS]
            if keep:
                results = [{k: r[k] for k in keep if k in r} for r in results]

    envelope = paginated(results, limit=limit, total=total, mode=mode)
    if compact_hint is not None:
        # The envelope owns ``hint``. Compact mode only fires without an explicit
        # limit, so a truncation hint should not coexist — join rather than assume,
        # because dropping either note is a silent loss of guidance.
        merged = f"{envelope['hint']} {compact_hint}" if envelope["hint"] else compact_hint
        envelope = {**envelope, "hint": merged}
    # Deprecated alias for pre-v1.8.0 callers; removed in 2.0. It must be the
    # same list object as ``items`` — a copy would let the two drift and turn
    # one silent break into two.
    return {**envelope, "vms": envelope["items"]}


_HOST_PROPS = [
    "name",
    "runtime.connectionState",
    "runtime.powerState",
    "hardware.cpuInfo.numCpuCores",
    "hardware.cpuInfo.numCpuThreads",
    "hardware.memorySize",
    "config.product.version",
    "config.product.build",
    "vm",
    "summary.quickStats.uptime",
]
_DS_PROPS = [
    "name",
    "summary.type",
    "summary.freeSpace",
    "summary.capacity",
    "summary.accessible",
    "summary.url",
    "vm",
]
_CLUSTER_PROPS = [
    "name",
    "host",
    "configuration.drsConfig.enabled",
    "configuration.drsConfig.defaultVmBehavior",
    "configuration.dasConfig.enabled",
    "summary.totalCpu",
    "summary.totalMemory",
]
_NET_PROPS = ["name", "vm", "summary.accessible"]


def list_hosts(si: ServiceInstance, limit: int | None = None) -> dict:
    """List all ESXi hosts with basic info.

    Returns the family list envelope. ``total`` is a real count: every host is
    enumerated before ``limit`` is applied, so a full page whose total matches
    is reported as complete rather than possibly-truncated.

    Args:
        si: vSphere ServiceInstance.
        limit: Max number of host rows to return (None = all).
    """
    results = []
    for _obj, p in _collect(si, [vim.HostSystem], _HOST_PROPS):
        mem = p.get("hardware.memorySize")
        results.append(
            {
                "name": p.get("name", ""),
                "connection_state": str(p.get("runtime.connectionState", "N/A")),
                "power_state": str(p.get("runtime.powerState", "N/A")),
                "cpu_cores": p.get("hardware.cpuInfo.numCpuCores") or 0,
                "cpu_threads": p.get("hardware.cpuInfo.numCpuThreads") or 0,
                "memory_gb": round(mem / (1024**3)) if mem else 0,
                "esxi_version": p.get("config.product.version") or "N/A",
                "esxi_build": p.get("config.product.build") or "N/A",
                "vm_count": len(p.get("vm") or []),
                "uptime_seconds": p.get("summary.quickStats.uptime") or 0,
            }
        )
    results.sort(key=lambda x: x["name"])
    total = len(results)
    if limit is not None:
        results = results[:limit]
    return paginated(results, limit=limit, total=total)


def list_datastores(si: ServiceInstance, limit: int | None = None) -> dict:
    """List all datastores with capacity info.

    Returns the family list envelope with a real ``total`` (every datastore is
    enumerated before ``limit`` is applied).

    Args:
        si: vSphere ServiceInstance.
        limit: Max number of datastore rows to return (None = all).
    """
    results = []
    for _obj, p in _collect(si, [vim.Datastore], _DS_PROPS):
        free = p.get("summary.freeSpace")
        cap = p.get("summary.capacity")
        results.append(
            {
                "name": p.get("name", ""),
                "type": p.get("summary.type"),
                "free_gb": round(free / (1024**3), 1) if free else 0,
                "total_gb": round(cap / (1024**3), 1) if cap else 0,
                "accessible": p.get("summary.accessible"),
                "url": p.get("summary.url"),
                "vm_count": len(p.get("vm") or []),
            }
        )
    results.sort(key=lambda x: x["name"])
    total = len(results)
    if limit is not None:
        results = results[:limit]
    return paginated(results, limit=limit, total=total)


def list_clusters(si: ServiceInstance, limit: int | None = None) -> dict:
    """List all clusters with configuration info.

    Returns the family list envelope with a real ``total`` (every cluster is
    enumerated before ``limit`` is applied).

    Args:
        si: vSphere ServiceInstance.
        limit: Max number of cluster rows to return (None = all).
    """
    results = []
    for _obj, p in _collect(si, [vim.ClusterComputeResource], _CLUSTER_PROPS):
        total_mem = p.get("summary.totalMemory")
        drs_behavior = p.get("configuration.drsConfig.defaultVmBehavior")
        results.append(
            {
                "name": p.get("name", ""),
                "host_count": len(p.get("host") or []),
                "drs_enabled": bool(p.get("configuration.drsConfig.enabled")),
                "drs_behavior": str(drs_behavior) if drs_behavior else "N/A",
                "ha_enabled": bool(p.get("configuration.dasConfig.enabled")),
                "total_cpu_mhz": p.get("summary.totalCpu") or 0,
                "total_memory_gb": round(total_mem / (1024**3)) if total_mem else 0,
            }
        )
    results.sort(key=lambda x: x["name"])
    total = len(results)
    if limit is not None:
        results = results[:limit]
    return paginated(results, limit=limit, total=total)


def list_networks(si: ServiceInstance, limit: int | None = None) -> dict:
    """List all networks.

    Returns the family list envelope with a real ``total`` (every network is
    enumerated before ``limit`` is applied).

    Args:
        si: vSphere ServiceInstance.
        limit: Max number of network rows to return (None = all).
    """
    results = []
    for _obj, p in _collect(si, [vim.Network], _NET_PROPS):
        accessible = p.get("summary.accessible")
        results.append(
            {
                "name": p.get("name", ""),
                "vm_count": len(p.get("vm") or []),
                "accessible": accessible if accessible is not None else True,
            }
        )
    results.sort(key=lambda x: x["name"])
    total = len(results)
    if limit is not None:
        results = results[:limit]
    return paginated(results, limit=limit, total=total)


def find_vm_by_name(si: ServiceInstance, vm_name: str) -> vim.VirtualMachine | None:
    """Find a VM by exact name. Returns None if not found."""
    for obj, p in _collect(si, [vim.VirtualMachine], ["name"]):
        if p.get("name") == vm_name:
            return obj
    return None


def find_host_by_name(si: ServiceInstance, host_name: str) -> vim.HostSystem | None:
    """Find an ESXi host by exact name. Returns None if not found."""
    for obj, p in _collect(si, [vim.HostSystem], ["name"]):
        if p.get("name") == host_name:
            return obj
    return None


def find_datastore_by_name(si: ServiceInstance, datastore_name: str) -> vim.Datastore | None:
    """Find a datastore by exact name. Returns None if not found."""
    for obj, p in _collect(si, [vim.Datastore], ["name"]):
        if p.get("name") == datastore_name:
            return obj
    return None
