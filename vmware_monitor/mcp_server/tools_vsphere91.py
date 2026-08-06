"""vSphere 9.1 read-only MCP tools (memory tiering + vLCM/deployment REST).

Registered on the shared ``mcp`` instance from :mod:`vmware_monitor.mcp_server.server`
by importing this module (done at the bottom of ``server.py``). Kept separate so
``server.py`` stays bounded and this 9.1 surface is reviewable on its own.

Every tool here is READ-only: ``readOnlyHint=True``, ``@vmware_tool(risk_level="low")``,
no state-changing call exists. Signatures use ``Optional[X]`` (never PEP 604 ``X | None``)
because FastMCP reflects these under older mcp/pydantic paths (踩坑 #33).
"""

from __future__ import annotations

from typing import Optional

from vmware_policy import vmware_tool

from vmware_monitor.mcp_server.server import (
    _catch_tool_errors,
    _get_connection,
    _get_target_config,
    mcp,
)

_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


@mcp.tool(annotations=_READ_ONLY)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def host_memory_tiering(
    target: Optional[str] = None,
    host_name: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] Per-host memory tiering (vSphere 9.1) and NVMe uplift ratio.

    When to use: to see which ESXi hosts back RAM with NVMe (memory tiering, 8.0U3+),
    how large each tier is, and how much of a host's apparent memory rides on NVMe
    rather than DRAM. This is the only source of measured tier byte sizes.

    Returns the list envelope {items, returned, limit, total, truncated, hint}. Each
    row: host, tiering_type (noTiering|hardwareTiering|softwareTiering), tiering_active,
    dram_gb, nvme_gb, total_tiered_gb, uplift_ratio (total/DRAM, None if DRAM unknown),
    and a per-tier breakdown. A host with tiering off reads as tiering_type "noTiering",
    nvme_gb 0.0 — that is a real "checked, none", not a gap.

    Gotchas: reads pyVmomi HostSystem.hardware.memoryTierInfo (needs ESXi 8.0U3+/9.x);
    older hosts report tiering_type "unknown". Read-only.

    Args:
        target: vCenter/ESXi target from config (default if omitted).
        host_name: Filter to one host by exact name (None = all).
        limit: Max host rows (None = all).
    """
    from vmware_monitor.ops.memory_tiering import get_memory_tiering

    si = _get_connection(target)
    return get_memory_tiering(si, host_name=host_name, limit=limit)


@mcp.tool(annotations=_READ_ONLY)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def cluster_patch_compliance(
    cluster: str,
    target: Optional[str] = None,
) -> dict:
    """[READ] vLCM software (patch) compliance for one cluster (vSphere Automation REST).

    When to use: to check whether a cluster's hosts match their assigned software
    image/baseline before or after a patch cycle. Complements host_memory_tiering
    (that is per-host hardware; this is per-cluster lifecycle state).

    What it returns: {available, cluster, status, hosts_total, non_compliant_hosts,
    scan_time, note, fields}. ``available: False`` means vCenter answered 503 — it is
    likely mid-patch (vSphere has no maintenance-ETA endpoint; retry shortly), not an
    error. Field parse is best-effort pending live 9.1 verification (see ``note``).

    Gotchas: ``cluster`` must be the cluster MoID (e.g. domain-c123), which the REST
    API requires — get it from list_all_clusters, not the display name. Read-only:
    reports compliance only; it never runs a remediation.

    Args:
        cluster: Cluster MoID (e.g. domain-c123).
        target: vCenter target from config (default if omitted).
    """
    from vmware_monitor.ops.patching import get_patch_compliance

    return get_patch_compliance(_get_target_config(target), cluster)


@mcp.tool(annotations=_READ_ONLY)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def cluster_last_apply_result(
    cluster: str,
    target: Optional[str] = None,
) -> dict:
    """[READ] Result of the last vLCM remediation (apply) on one cluster (REST).

    When to use: after a patch/remediation, to confirm the last apply succeeded and
    when it finished. Use cluster_patch_compliance for current drift; use this for
    the outcome of the most recent apply.

    What it returns: {available, cluster, status, end_time, note, fields}, read
    defensively. A cluster never remediated may return a "not found" teaching error;
    a 503 yields ``available: False`` (mid-patch, retry).

    Gotchas: ``cluster`` is the cluster MoID (e.g. domain-c123) — see list_all_clusters.
    Read-only. Field parse best-effort pending live 9.1 verification.

    Args:
        cluster: Cluster MoID (e.g. domain-c123).
        target: vCenter target from config (default if omitted).
    """
    from vmware_monitor.ops.patching import get_last_apply_result

    return get_last_apply_result(_get_target_config(target), cluster)


@mcp.tool(annotations=_READ_ONLY)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def vcenter_deployment_size(
    target: Optional[str] = None,
) -> dict:
    """[READ] vCenter appliance deployment size (NEW in vSphere 9.1, REST).

    When to use: to read the vCenter appliance's current (and, where reported, target)
    deployment size class — capacity-planning context that inventory/perf tools do not
    cover.

    What it returns: {available, note, fields} where ``fields`` is a defensive
    passthrough of the endpoint's top-level scalar values. ``available: False`` means
    vCenter answered 503 (busy/restarting); nothing crashed.

    Gotchas: 9.1-only endpoint — older vCenters will 404 (authored teaching error).
    Field parse best-effort pending live 9.1 verification. Read-only.

    Args:
        target: vCenter target from config (default if omitted).
    """
    from vmware_monitor.ops.patching import get_deployment_size

    return get_deployment_size(_get_target_config(target))
