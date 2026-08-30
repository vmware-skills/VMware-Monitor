"""MCP server wrapping VMware Monitor read-only operations.

This module exposes **read-only** VMware vCenter/ESXi monitoring tools via
the Model Context Protocol (MCP) using stdio transport.  Each ``@mcp.tool()``
function delegates to the corresponding function in the ``vmware_monitor``
package (ops.inventory, ops.health, ops.vm_info).

**NO destructive operations exist in this module or anywhere in the
vmware-monitor codebase**: no power_on, power_off, create, delete,
reconfigure, snapshot-create/revert/delete, clone, or migrate.

Security considerations
-----------------------
* **All tools are read-only**: Every tool only queries vSphere state;
  none can modify VMs, hosts, or configuration.
* **Credential handling**: Credentials are loaded from environment
  variables / ``.env`` file — never passed via MCP messages.
* **Transport**: Uses stdio transport (local only); no network listener.

Source: https://github.com/vmware-skills/VMware-Monitor
License: MIT
"""

import functools
import logging
import ssl
from collections.abc import Callable
from typing import Any, Literal, Optional

# MCP SDK — Model Context Protocol server framework
from mcp.server.fastmcp import FastMCP
from vmware_policy import (
    describe_tool_parameters,
    mtime_cached_loader,
    sanitize,
    set_environment_resolver,
    vmware_tool,
)

from vmware_monitor import __version__

# Internal VMware monitoring modules (all read-only operations)
from vmware_monitor.config import CONFIG_FILE, ConfigError, load_config
from vmware_monitor.connection import ConnectionManager
from vmware_monitor.ops.activity import get_active_sessions, get_active_tasks
from vmware_monitor.ops.attention import get_cross_vcenter_attention
from vmware_monitor.ops.capacity import (
    get_datastore_capacity,
    get_resource_pool_usage,
)
from vmware_monitor.ops.cluster_summary import get_cluster_health_summary
from vmware_monitor.ops.health import (
    get_active_alarms,
    get_host_hardware_status,
    get_recent_events,
)
from vmware_monitor.ops.health import get_host_services as _ops_get_host_services
from vmware_monitor.ops.infra_health import (
    get_certificate_status,
    get_license_status,
    get_ntp_status,
)
from vmware_monitor.ops.inventory import (
    list_clusters,
    list_datastores,
    list_hosts,
    list_networks,
    list_vms,
)
from vmware_monitor.ops.investigate_datastore import (
    DatastoreNotFoundError,
    get_datastore_investigation_bundle,
)
from vmware_monitor.ops.investigate_host import (
    HostNotFoundError,
    get_host_investigation_bundle,
)
from vmware_monitor.ops.investigate_vm import get_vm_investigation_bundle
from vmware_monitor.ops.performance import get_host_performance, get_vm_performance
from vmware_monitor.ops.backup_window import (
    AmbiguousVMError,
    get_backup_snapshot_history,
)
from vmware_monitor.ops.snapshots import list_snapshot_aging
from vmware_monitor.ops.vm_info import VMNotFoundError, get_vm_info, list_snapshots
from vmware_monitor.scanner.log_scanner import scan_host_logs as _ops_scan_host_logs

logger = logging.getLogger(__name__)


def _safe_error(exc: Exception, tool: str) -> str:
    """Return an agent-safe error string; log full detail server-side only.

    Raw exception text can carry API response bodies, internal paths, or
    host:port pairs. Full traceback goes to the server log; the agent sees only
    a control-char-stripped, length-capped message.

    Every exception this skill raises on purpose passes through: the builtin
    validation errors, and the domain exceptions defined under
    ``vmware_monitor.ops``. Those three cover this skill's most common failure —
    a name that does not resolve — and each carries the sentence naming the
    listing tool that produces a valid one. Omitting them replaced that sentence
    with ``VMNotFoundError: operation failed.`` on the way to the agent, so the
    teaching text was written, printed in full by the CLI, and discarded at the
    one surface where a model would have used it.

    ``TimeoutError`` and ``ConnectionError`` are here for the same reason: the
    CLI path catches ``OSError`` and prints a retry hint, and the two surfaces
    should not disagree about what a dropped connection means.

    The configuration errors this skill raises on purpose — the missing-password
    error, this family's most common first-run failure, and the connection
    layer's authored replacement for a transport failure — pass through as the
    narrow ``ConfigError``. Bare ``OSError`` was briefly listed here for them and
    was too wide a door: ``sanitize`` strips control characters and truncates, it
    does not redact, so ``ssl.SSLCertVerificationError`` (certificate subject and
    hostname), ``socket.gaierror`` (the name that failed to resolve) and
    ``requests``-style connection errors (full scheme://host:port/path) all
    reached the agent verbatim through it. Only the narrow ``OSError`` subclasses
    that were allowed before it remain.

    Anything else is reduced to its type — an unplanned exception's text was
    written for a developer reading a traceback, not for an agent choosing what
    to do next, and it is the one that can carry credentials.
    """
    logger.error("Tool %s failed", tool, exc_info=True)
    # Checked ahead of the allowlist: ssl.SSLCertVerificationError inherits from
    # ValueError as well as OSError, so it matches the ValueError entry — which
    # predates the OSError one — and narrowing the OSError side does not keep it
    # out. Its text quotes the certificate subject and the host. The connection
    # layer replaces the ones raised on a connect attempt with an authored
    # ConnectError that names the target and verify_ssl instead.
    if isinstance(exc, ssl.SSLError):
        return f"{type(exc).__name__}: operation failed."
    _passthrough = (
        ValueError,
        FileNotFoundError,
        KeyError,
        PermissionError,
        TimeoutError,
        ConnectionError,
        ConfigError,
        VMNotFoundError,
        AmbiguousVMError,
        HostNotFoundError,
        DatastoreNotFoundError,
    )
    if isinstance(exc, _passthrough):
        return sanitize(str(exc), 300)
    return f"{type(exc).__name__}: operation failed."


_DOCTOR_HINT = "Run 'vmware-monitor doctor' to verify connectivity and credentials."


def _catch_tool_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Translate tool exceptions into an error payload.

    Every tool returns a dict — list-returning tools return the family list
    envelope (``{items, returned, limit, total, truncated, hint}``) rather than
    a bare ``list[dict]``, so the error payload is a plain dict too and matches
    the return annotation FastMCP validates against. Passes the real tool name
    (fn.__name__) to _safe_error instead of the old literal "monitor".
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return {"error": _safe_error(e, fn.__name__), "hint": _DOCTOR_HINT}

    return wrapper


mcp = FastMCP(
    "vmware-monitor",
    instructions=(
        "VMware vCenter/ESXi read-only monitoring. "
        "Query inventory, check health/alarms, and view VM info. "
        "No destructive operations — code-level enforced."
    ),
)

# FastMCP takes no version argument and leaves the lowlevel server's at
# None, which makes `initialize` answer with the MCP SDK's version rather
# than ours. Set it so a client can tell which release it is talking to.
mcp._mcp_server.version = __version__

# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

_conn_mgr: Optional[ConnectionManager] = None


def _ensure_conn_mgr() -> ConnectionManager:
    """Lazily build the shared ConnectionManager (does not connect anything)."""
    global _conn_mgr  # noqa: PLW0603
    if _conn_mgr is None:
        # No env-var read here: load_config resolves the path (explicit arg,
        # then the environment, then the default). This copy was the reason the
        # server and the CLI opened different files — load_config did not look
        # at the variable at all, so only this path honoured it (形态 #6).
        config = load_config()
        _conn_mgr = ConnectionManager(config)
    return _conn_mgr


def _get_connection(target: Optional[str] = None) -> Any:
    """Return a pyVmomi ServiceInstance, lazily initialising the manager."""
    return _ensure_conn_mgr().connect(target)


def _get_target_config(target: Optional[str] = None) -> Any:
    """Return the resolved TargetConfig for ``target`` (default if omitted).

    The vSphere 9.1 REST reads (patching / deployment size) authenticate against
    the same per-target credentials as the SOAP path but do not use a pyVmomi
    ServiceInstance — they need the TargetConfig (host, creds, verify_ssl) instead.
    """
    cfg = _ensure_conn_mgr()._config
    return cfg.get_target(target) if target else cfg.default_target


# ---------------------------------------------------------------------------
# Inventory tools (read-only)
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def list_virtual_machines(
    target: Optional[str] = None,
    limit: Optional[int] = None,
    sort_by: str = "name",
    power_state: Optional[str] = None,
    fields: Optional[list[str]] = None,
    folder_filter: Optional[str] = None,
) -> dict:
    """[READ] List virtual machines, with filtering, sorting, and field selection.

    Returns the family list envelope {items, returned, limit, total, truncated,
    hint}; ``total`` is the real post-filter count, so read ``truncated`` before
    summarising. Over 50 VMs with no limit/fields, only the first five fields
    below come back (``mode`` says which). ``vms`` is a deprecated pre-1.8.0
    alias of ``items``, removed in 2.0 — read ``items``.

    Use this to resolve an exact VM name, then vm_info for detail or
    vm_investigation_bundle to drill into one; for a fleet-wide view start at
    cluster_health_summary instead.

    Args:
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max VMs (None = all).
        sort_by: name | cpu | memory_mb | power_state | folder_path.
        power_state: poweredOn | poweredOff | suspended.
        fields: Any of name, power_state, cpu, memory_mb, folder_path, guest_os,
            ip_address, host, uuid, tools_status (None = auto).
        folder_filter: Case-insensitive folder_path substring; nested folders
            match.
    """
    si = _get_connection(target)
    return list_vms(
        si,
        limit=limit,
        sort_by=sort_by,
        power_state=power_state,
        fields=fields,
        folder_filter=folder_filter,
    )


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def list_esxi_hosts(
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] List ESXi hosts with CPU cores, memory, version, VM count, and uptime.

    Returns the list envelope; ``total`` is the real host count. Static config only
    (cores, total GB) — use this to resolve a host name, then host_performance for
    live load or host_investigation_bundle to drill into one host.

    Args:
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max hosts to return (None = all).
    """
    si = _get_connection(target)
    return list_hosts(si, limit=limit)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def list_all_datastores(
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] List datastores with capacity, free space, type, and VM count.

    Returns the list envelope with a real ``total``. Raw free/used only — use this
    to resolve a datastore name, then datastore_capacity for thin-provisioning
    over-commit risk or datastore_investigation_bundle to drill into one datastore.

    Args:
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max datastores to return (None = all).
    """
    si = _get_connection(target)
    return list_datastores(si, limit=limit)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def list_all_clusters(
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] List clusters with host count, DRS/HA status, and resource totals.

    Returns the list envelope with a real ``total``. Static topology only — use this
    to resolve a cluster name, then cluster_health_summary, which supersedes
    stitching this with list_esxi_hosts and get_alarms yourself.

    Args:
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max clusters to return (None = all).
    """
    si = _get_connection(target)
    return list_clusters(si, limit=limit)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def cluster_health_summary(
    target: Optional[str] = None,
    cluster_filter: Optional[str] = None,
    include_vms: bool = True,
    top_n: int = 10,
) -> dict:
    """[READ] One-glance health rollup for every cluster — "is anything on fire?".

    Start here for single-vCenter triage. Batches hosts, VM power state, live
    CPU/memory pressure and alarms per cluster, scores each "ok"/"warn"/
    "critical", and ranks the anomalies into ``top_issues``. Use this instead of
    stitching list_all_clusters + list_esxi_hosts + get_alarms yourself.

    Returns {totals, top_issues, issues_total, clusters, snapshot,
    customization_hint} — not the list envelope. Lead with ``top_issues`` (worst
    first), show ``clusters`` as context, always echo ``customization_hint`` last.
    Point-in-time — no trending.

    Then drill into what ``top_issues`` names with vm_investigation_bundle,
    host_investigation_bundle or datastore_investigation_bundle; use
    cross_vcenter_attention to cover every target at once. Acting on a finding
    belongs to vmware-aiops.

    Args:
        target: vCenter/ESXi target from config (default if omitted).
        cluster_filter: Case-insensitive substring; only matching clusters show
            (None = all, plus a standalone-hosts row).
        include_vms: Roll up VM counts (default True); False skips that pass.
        top_n: Cap ``top_issues`` (default 10; 0 omits it). ``issues_total`` is
            the pre-cap count.
    """
    si = _get_connection(target)
    return get_cluster_health_summary(
        si, cluster_filter=cluster_filter, include_vms=include_vms, top_n=top_n
    )


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def vm_investigation_bundle(
    vm_name: str,
    target: Optional[str] = None,
    hours: int = 24,
) -> dict:
    """[READ] "What is happening around this VM?" — one correlated drill-down.

    Use this instead of stitching vm_info + vm_list_snapshots + get_alarms +
    vm_performance + get_events yourself. Returns one correlated bundle (not the
    list envelope): the VM's state, its host, cluster and backing datastores, its
    snapshots and triggered alarms, live performance, and a merged event timeline
    across VM/host/cluster/datastore (newest first). All reads are batched.
    Explain it in operational language; do not dump it raw.

    Reach for it after cluster_health_summary names a problem VM, or when asked
    "what's going on with <vm>?". Point-in-time snapshot — no trending. Acting on
    what you find (power, migrate, delete snapshot) belongs to vmware-aiops.

    Args:
        vm_name: Exact VM name; unknown names return a teaching error. Get it from
            list_virtual_machines or cluster_health_summary first.
        target: vCenter/ESXi target from config (default if omitted).
        hours: Event-timeline look-back window in hours (default 24).
    """
    si = _get_connection(target)
    return get_vm_investigation_bundle(si, vm_name, hours=hours)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def host_investigation_bundle(
    host_name: str,
    target: Optional[str] = None,
    hours: int = 24,
) -> dict:
    """[READ] "What is happening around this ESXi host?" — one correlated drill-down.

    Use this instead of stitching list_esxi_hosts + host_performance + get_alarms
    + get_events yourself. Returns one correlated bundle (not the list envelope):
    the host's state (connection, CPU/memory pressure, version, uptime), its
    cluster, a rollup of the VMs it runs, the datastores it mounts, alarms across
    host/cluster/datastore, live performance, and a merged event timeline. All
    reads are batched. Explain it in operational language; do not dump it raw.

    Reach for it after cluster_health_summary flags a host. Point-in-time
    snapshot — no trending; for syslog lines use host_log_scan. Acting on a
    finding (maintenance mode, evacuate) belongs to vmware-aiops.

    Args:
        host_name: Exact ESXi host name; unknown names return a teaching error.
            Get it from list_esxi_hosts or cluster_health_summary first.
        target: vCenter/ESXi target from config (default if omitted).
        hours: Event-timeline look-back window in hours (default 24).
    """
    si = _get_connection(target)
    return get_host_investigation_bundle(si, host_name, hours=hours)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def datastore_investigation_bundle(
    datastore_name: str,
    target: Optional[str] = None,
    hours: int = 24,
) -> dict:
    """[READ] "What is happening around this datastore?" — one correlated drill-down.

    Use this instead of stitching list_all_datastores + datastore_capacity +
    get_alarms + get_events yourself. Returns one correlated bundle (not the list
    envelope): capacity / free space / accessibility, the hosts that mount it, a
    rollup of the VMs it backs, alarms across datastore/host, and a merged event
    timeline. All reads are batched. Explain it in operational language; do not
    dump it raw. Per-datastore latency is not included.

    Reach for it after cluster_health_summary flags storage pressure.
    Point-in-time snapshot — no trending. Freeing space (deleting snapshots,
    storage vMotion) belongs to vmware-aiops.

    Args:
        datastore_name: Exact datastore name; unknown names return a teaching
            error. Get it from list_all_datastores or datastore_capacity first.
        target: vCenter/ESXi target from config (default if omitted).
        hours: Event-timeline look-back window in hours (default 24).
    """
    si = _get_connection(target)
    return get_datastore_investigation_bundle(si, datastore_name, hours=hours)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def cross_vcenter_attention(
    cluster_filter: Optional[str] = None,
    top_n: int = 10,
) -> dict:
    """[READ] "What needs attention now?" across EVERY configured vCenter — one list.

    Start here when the estate has more than one vCenter. Rolls every configured
    target's cluster-health summary into one globally ranked ``top_issues`` list
    (worst first, each tagged with its ``vcenter``) plus a per-target rollup. Use
    this instead of calling cluster_health_summary once per target and merging
    yourself. Returns a rollup, not the list envelope; lead with ``top_issues``.

    Degrades gracefully: an unreachable target is listed under ``unreachable``
    with a reason and the rest still aggregate. Point-in-time — no trending.
    Then drill in with vm_investigation_bundle, host_investigation_bundle or
    datastore_investigation_bundle against the ``vcenter`` the issue names.

    Args:
        cluster_filter: Case-insensitive cluster substring applied to every target
            (None = all clusters).
        top_n: Cap the merged top_issues list (default 10); ``issues_total`` is
            the pre-cap count.
    """
    sessions, unreachable = _ensure_conn_mgr().connect_all()
    return get_cross_vcenter_attention(
        sessions, unreachable=unreachable, cluster_filter=cluster_filter, top_n=top_n
    )


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def list_all_networks(
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] List networks with name, attached VM count, and accessibility.

    Returns the list envelope with a real ``total``. Name, vm_count and accessible
    only — no VLAN, uplink or NSX overlay detail. Use this to resolve a port-group
    name, then vm_info for the NICs of one VM. NSX segments live in vmware-nsx.

    Args:
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max networks to return (None = all).
    """
    si = _get_connection(target)
    return list_networks(si, limit=limit)


# ---------------------------------------------------------------------------
# Health tools (read-only)
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def get_alarms(
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] Get active/triggered alarms across the VMware inventory.

    Returns the list envelope with a real ``total``. Each alarm carries
    suggested_actions naming the companion skill and tool for remediation. Empty
    ``items`` with ``truncated`` False means there genuinely are no active alarms —
    never report "no data" otherwise.

    Use this for the raw alarm list; prefer cluster_health_summary when you want
    alarms folded into a whole-cluster verdict. Then drill into the flagged
    object with vm_investigation_bundle or host_investigation_bundle.

    Args:
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max alarms to return (None = all).
    """
    si = _get_connection(target)
    return get_active_alarms(si, limit=limit)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def get_events(
    hours: int = 24,
    severity: Literal["critical", "warning", "info"] = "warning",
    target: Optional[str] = None,
) -> dict:
    """[READ] Get recent vCenter/ESXi events filtered by severity.

    Returns the list envelope. No row limit is applied, so ``truncated`` is False;
    ``total`` is null because vCenter's event collector applies its own bounds —
    widen ``hours`` if you need to be sure nothing older is being missed.

    Severity is this skill's own ranking where it has one, and otherwise
    vCenter's published event catalogue. An event neither can rank comes back
    with severity "unknown" (it is returned, not filtered out) and is counted in
    the envelope's ``unclassified``, with ``classification_note`` explaining. An
    empty ``items`` alongside a non-zero ``unclassified`` does NOT mean the
    window was quiet — read the note before reporting all-clear.

    Use this for an inventory-wide event sweep. When you already know the object,
    prefer vm_investigation_bundle / host_investigation_bundle instead: they
    return the same events correlated with that object's state in one call. ESXi
    syslog lines are not events — use host_log_scan for those.

    Args:
        hours: How many hours back to query (default 24).
        severity: Minimum severity: "critical", "warning", or "info".
        target: vCenter/ESXi target from config (default if omitted).
    """
    si = _get_connection(target)
    return get_recent_events(si, hours=hours, severity=severity)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def get_host_sensors(
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] Get hardware sensor status (temperature, voltage, fan, ...) for all hosts.

    Returns the list envelope with a real ``total``; each row has host, sensor_name,
    type, reading, unit and status (green/yellow/red). Empty ``items`` means no host
    exposes sensor data (e.g. nested ESXi), not that the query failed.

    Use this for physical hardware only — for CPU/memory load use
    host_performance, and follow up on a red sensor with
    host_investigation_bundle to see the alarms and events around that host.

    Args:
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max sensor rows to return (None = all).
    """
    si = _get_connection(target)
    return get_host_hardware_status(si, limit=limit)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def get_host_services(
    host_name: Optional[str] = None,
    target: Optional[str] = None,
) -> dict:
    """[READ] Get host service status (running state and startup policy).

    Returns the list envelope; each row has host, service key, label, running (bool)
    and policy (on/off/automatic). Every matching host is enumerated, so
    ``truncated`` is always False.

    Use this to check whether SSH, NTP or the firewall service is in the expected
    state. For NTP specifically prefer ntp_status, which also reports the
    configured servers. Starting or stopping a service is a write — this skill
    cannot do it; use vmware-aiops.

    Args:
        host_name: Filter to a single host by exact name (None = all hosts).
        target: vCenter/ESXi target from config (default if omitted).
    """
    si = _get_connection(target)
    return _ops_get_host_services(si, host_name=host_name)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def host_log_scan(
    host_name: Optional[str] = None,
    lines: int = 500,
    target: Optional[str] = None,
) -> dict:
    """[READ] Scan recent ESXi host syslog lines for error/warning patterns.

    Reads the last ``lines`` entries of the hostd/vmkernel/vpxa logs via the
    diagnostic system and returns only the lines matching known trouble patterns
    (error, fail, critical, panic, lost access, timeout, …). Returns the list
    envelope {items, returned, limit, total, truncated, hint}; each row has
    severity, source (``host_log:<key>``), message, time and entity. ``total`` is
    null on purpose — this is "errors within the scanned window", not all errors
    ever, and empty ``items`` means nothing matched.

    Use this when get_events or host_investigation_bundle show a host in trouble
    but not why: vCenter events and ESXi syslog are different sources. Filter
    with ``host_name`` to keep the scan fast on large clusters.

    Args:
        host_name: Filter to a single host by exact name (None = all hosts).
        lines: How many recent lines per log to scan (default 500).
        target: vCenter/ESXi target from config (default if omitted).
    """
    si = _get_connection(target)
    return _ops_scan_host_logs(si, host_name=host_name, lines=lines)


# ---------------------------------------------------------------------------
# VM info tool (read-only)
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def vm_info(vm_name: str, target: Optional[str] = None) -> dict:
    """[READ] Get detailed information about one VM (CPU, memory, disks, NICs, snapshots).

    Returns a single detail dict, not the list envelope. Static configuration
    only — for live CPU/memory use vm_performance, and for the same VM correlated
    with its host, alarms and events use vm_investigation_bundle.

    Use this when you already know the exact name; get it from
    list_virtual_machines first, since an unknown name returns a teaching error
    rather than a match. Reconfiguring the VM belongs to vmware-aiops.

    Args:
        vm_name: Exact name of the virtual machine.
        target: vCenter/ESXi target from config (default if omitted).
    """
    si = _get_connection(target)
    return get_vm_info(si, vm_name)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def vm_list_snapshots(vm_name: str, target: Optional[str] = None) -> dict:
    """[READ] List all snapshots of one VM, including the nesting hierarchy.

    Returns the list envelope, one row per snapshot with name, description, created
    timestamp, state and level (0 = root; children are level+1). Empty ``items``
    with ``truncated`` False means the VM genuinely has no snapshots. Read-only —
    creating, reverting and deleting snapshots live in vmware-aiops.

    Use this for one known VM; prefer snapshot_aging to sweep the whole inventory
    for old or sprawling snapshots. Get the name from list_virtual_machines
    first — an unknown name returns a teaching error.

    Args:
        vm_name: Exact name of the virtual machine.
        target: vCenter/ESXi target from config (default if omitted).
    """
    si = _get_connection(target)
    return list_snapshots(si, vm_name)


# ---------------------------------------------------------------------------
# Performance tools (read-only — real-time PerfManager counters)
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def host_performance(
    host_name: Optional[str] = None,
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] Real-time CPU/memory/disk/network utilisation per ESXi host.

    Returns the list envelope with a real ``total`` (hosts that reported metrics).
    Unlike list_esxi_hosts (static config: cores, total GB) this is LIVE 20-second
    PerfManager data: cpu_usage_pct, mem_usage_pct, mem_consumed_mb, disk_kbps,
    net_kbps, busiest first. Disconnected hosts and hosts without a real-time
    provider are skipped, not reported as zero. Point-in-time only — no historical
    trend.

    Use this to find which host is hot, then host_investigation_bundle to drill
    into it, or vm_performance to see which VMs on it are driving the load.

    Args:
        host_name: Filter to a single host by exact name (None = all hosts).
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max host rows to return (None = all).
    """
    si = _get_connection(target)
    return get_host_performance(si, host_name=host_name, limit=limit)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def vm_performance(
    vm_name: Optional[str] = None,
    target: Optional[str] = None,
    limit: Optional[int] = 25,
) -> dict:
    """[READ] Real-time CPU/memory/disk/network utilisation per virtual machine.

    Returns the list envelope with a real ``total`` (VMs that reported metrics) —
    with the default limit of 25, ``truncated`` tells you whether more VMs sit
    behind it. LIVE data (cpu_usage_pct, mem_usage_pct, mem_consumed_mb,
    disk_read_kbps, disk_write_kbps, net_kbps), busiest first. Only powered-on VMs
    have a real-time provider; powered-off VMs are skipped. Point-in-time only.

    Use this to rank load across VMs; for one VM's configuration use vm_info, and
    to see the same VM correlated with its host, alarms and events use
    vm_investigation_bundle.

    Args:
        vm_name: Filter to a single VM by exact name (None = all powered-on VMs).
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max VM rows to return (default 25; None = all).
    """
    si = _get_connection(target)
    return get_vm_performance(si, vm_name=vm_name, limit=limit)


# ---------------------------------------------------------------------------
# Snapshot aging (read-only — inventory-wide sprawl analysis)
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def snapshot_aging(
    age_threshold_days: int = 30,
    only_old: bool = False,
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] Sweep ALL VMs for snapshots and flag old / sprawling ones.

    Use this for fleet-wide snapshot sprawl; prefer vm_list_snapshots when you
    only care about one VM. Returns {total_snapshots, old_snapshots,
    vms_with_snapshots, threshold_days, snapshots[], hint} — a rollup, not the
    list envelope. Each row has age_days, is_old and an est_size_mb lower bound
    (snapshotData+snapshotMemory; delta-disk growth is not separable per-snapshot
    via the API). Read-only — deleting snapshots belongs to vmware-aiops.

    Args:
        age_threshold_days: Age above which a snapshot is flagged old (default 30).
        only_old: When True, return only snapshots past the threshold.
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max snapshot rows to return (None = all).
    """
    si = _get_connection(target)
    return list_snapshot_aging(
        si, age_threshold_days=age_threshold_days, only_old=only_old, limit=limit
    )



@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def vm_backup_snapshot_history(
    vm_name: str,
    days: int = 30,
    include_cycles: bool = False,
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] How long backups held a snapshot open on one VM, from task history.

    Image-level backup products (Veeam, Commvault, Rubrik, NetBackup) snapshot
    the VM, copy the frozen disks, then delete the snapshot. vCenter records
    both ends, so the backup window is recoverable without credentials for the
    backup server.

    This is a LOWER BOUND on the job, not its duration: work the product does
    before the snapshot is taken and after it is removed is invisible to
    vCenter. The returned ``basis`` says so — quote it alongside any figure.

    Returns a rollup, not the list envelope: counts, four hour statistics
    (backup_active / snapshot_present / total_window / snapshot_removal),
    latest_cycle, longest_cycle, and ``unmatched`` rows for creations with no
    removal (a backup that left its snapshot behind) or removals whose creation
    predates the window.

    Read two fields before reporting all-clear. ``history_unavailable`` non-null
    means the history could not be read at all — that is not "no backups".
    ``coverage_note`` non-null means vCenter has already expired part of the
    requested window (vpxd.task.maxAge, 30 days by default), so the counts
    describe a shorter period than ``days`` asked for.

    Duplicate VM names raise rather than resolve to one, because a duration
    attributed to the wrong same-named VM looks exactly like a correct answer.
    For snapshots that exist right now use vm_list_snapshots or snapshot_aging.

    Args:
        vm_name: Exact VM name; duplicates are refused, not guessed.
        days: How far back to look, 1 to 365 (default 30).
        include_cycles: Also return the individual cycles, newest kept on cap.
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max cycle rows when include_cycles is set (capped at 200).
    """
    si = _get_connection(target)
    return get_backup_snapshot_history(
        si, vm_name, days=days, include_cycles=include_cycles, limit=limit
    )

# ---------------------------------------------------------------------------
# Infrastructure health (read-only — certificates, licenses, NTP)
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def certificate_status(
    warn_days: int = 30,
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] Per-host ESXi management certificate expiry.

    An expired ESXi cert drops host management — this surfaces it before the
    outage. Returns the list envelope with a real ``total``; each row has host,
    not_after, days_until_expiry and an ``expiring`` flag, soonest first. Host
    certificates only — the vCenter appliance's own certificate is not covered.

    Use this alongside license_status and ntp_status for a platform-hygiene
    sweep. Renewing a certificate is a write this skill cannot do; use
    vmware-aiops or vCenter directly.

    Args:
        warn_days: Flag certs expiring within this many days (default 30).
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max host rows to return (None = all).
    """
    si = _get_connection(target)
    return get_certificate_status(si, warn_days=warn_days, limit=limit)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def license_status(target: Optional[str] = None) -> dict:
    """[READ] vCenter/ESXi license inventory with usage and expiry.

    Returns the list envelope, one row per license: name, edition_key, total/used
    units, unlimited flag (row total == 0) and expiration. Every license is
    enumerated, so ``truncated`` is always False — this is the complete inventory.

    Use this to catch over-allocation or an approaching expiry, alongside
    certificate_status and ntp_status for a platform-hygiene sweep. Which host
    consumes which license is not reported — use list_esxi_hosts for the host
    inventory. Assigning a license is a write; use vmware-aiops.

    Args:
        target: vCenter/ESXi target from config (default if omitted).
    """
    si = _get_connection(target)
    return get_license_status(si)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def ntp_status(
    host_name: Optional[str] = None,
    target: Optional[str] = None,
) -> dict:
    """[READ] Per-host NTP configuration health (servers + ntpd service state).

    Returns the list envelope; every matching host is enumerated, so ``truncated``
    is always False. Each row has host, reachable, ntp_servers, ntpd_running,
    ntpd_policy and a ``healthy`` flag (servers configured AND ntpd running). The
    SOAP API does not expose live clock offset or stratum — this is configuration
    health only; for actual offset use esxcli on the host.

    healthy/ntp_servers/ntpd_running are null — not false/empty — for a host
    vCenter could not reach or could not read. Null means nothing was observed;
    false means NTP is misconfigured. Filtering rows for `healthy == false` will
    not surface the unread ones, so check the envelope's `hosts_unreachable`
    count and `unreachable_note` before reporting the estate as healthy.

    Prefer this over get_host_services for time problems: that tool reports
    whether ntpd runs but not which servers are configured. Fixing NTP is a
    write; use vmware-aiops.

    Args:
        host_name: Filter to a single host by exact name (None = all hosts).
        target: vCenter/ESXi target from config (default if omitted).
    """
    si = _get_connection(target)
    return get_ntp_status(si, host_name=host_name)


# ---------------------------------------------------------------------------
# Capacity analytics (read-only — over-commit and resource pools)
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def datastore_capacity(
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] Per-datastore capacity with thin-provisioning over-commit.

    Returns the list envelope with a real ``total``: capacity_gb, free_gb,
    committed_gb, provisioned_gb, used_pct, overcommit_pct, riskiest first. Adds the
    risk signal list_all_datastores lacks — overcommit_pct over 100% means more
    space is promised to VMs than physically exists, so a thin datastore can fill up
    while still showing free space. Point-in-time.

    Use this for the capacity view, then datastore_investigation_bundle to drill
    into a specific datastore's hosts, VMs and alarms. Reclaiming space (delete
    snapshots, storage vMotion) belongs to vmware-aiops.

    Args:
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max datastore rows to return (None = all).
    """
    si = _get_connection(target)
    return get_datastore_capacity(si, limit=limit)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def resource_pool_usage(
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] Per-resource-pool CPU/memory reservation, limit, and current usage.

    Returns the list envelope with a real ``total``; each row has name,
    cpu_reservation_mhz, cpu_limit_mhz, cpu_usage_mhz, mem_reservation_mb,
    mem_limit_mb, mem_usage_mb, sorted by memory usage descending. A limit of -1
    means unlimited. Pool names are not cluster-qualified, so identical names in
    different clusters may look alike.

    Use this when a VM is throttled but its host is not busy — the cap is usually
    the pool. Then vm_performance to see which VMs in it are demanding, or
    cluster_health_summary for the cluster-level picture.

    Args:
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max pool rows to return (None = all).
    """
    si = _get_connection(target)
    return get_resource_pool_usage(si, limit=limit)


# ---------------------------------------------------------------------------
# Activity tracking (read-only — tasks and sessions)
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def active_tasks(
    include_recent: bool = True,
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] In-flight (and optionally just-completed) vCenter tasks.

    Answers "why is the cluster busy?". Returns the list envelope {items, returned,
    limit, total, truncated, hint} with a real ``total``; each row has name, entity,
    state, progress_pct, start_time, user, an active flag and error (for failed
    recent tasks), running/queued first. vCenter keeps only a short recent-task
    window, so an old task may simply be gone rather than absent.

    Use this before blaming load: a migration or clone in flight explains
    pressure that host_performance shows. Pair with active_sessions to see who
    started it. Read-only — cancelling a task belongs to vmware-aiops.

    Args:
        include_recent: Also include recently completed/failed tasks (default True).
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max task rows to return (None = all).
    """
    si = _get_connection(target)
    return get_active_tasks(si, include_recent=include_recent, limit=limit)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@vmware_tool(risk_level="low")
@_catch_tool_errors
def active_sessions(
    target: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """[READ] Currently authenticated vCenter/ESXi sessions (who is logged in).

    Returns the list envelope with a real ``total``; each row has user_name,
    full_name, login_time, last_active, ip_address and a ``current`` flag for this
    skill's own session. Requires the Sessions privilege; low-privilege accounts get
    a single explanatory row instead of a traceback.

    Use this to attribute a change to a person — pair it with active_tasks, which
    names the user who started each task. Read-only — terminating a session is
    not supported here.

    Args:
        target: vCenter/ESXi target from config (default if omitted).
        limit: Max session rows to return (None = all).
    """
    si = _get_connection(target)
    return get_active_sessions(si, limit=limit)


# ---------------------------------------------------------------------------
# Environment declaration
# ---------------------------------------------------------------------------


_cached_config = mtime_cached_loader("VMWARE_MONITOR_CONFIG", CONFIG_FILE, load_config)


def _environment_for(target: Optional[str]) -> str:
    """Report the environment a target declares, for policy scoping.

    Policy rules scope by environment ("irreversible work in production needs a
    second person"), and vmware-policy cannot read this skill's config itself.
    Registering this lookup is what lets those rules fire at all. Reloaded on
    config.yaml mtime change so an edit takes effect without restarting the
    server. The config is cached via :func:`vmware_policy.mtime_cached_loader`,
    so repeated tool calls pay one ``os.stat`` instead of a full YAML parse.
    """
    try:
        return _cached_config().environment_for(target)
    except Exception:  # noqa: BLE001 — an unreadable config means "undeclared"
        return ""


set_environment_resolver(_environment_for)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio."""
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")


# vSphere 9.1 read tools (memory tiering + vLCM/deployment REST) live in a focused
# module to keep this file bounded; importing it registers them on ``mcp``. Placed
# last so ``mcp`` and the tool helpers above are fully defined first.
from vmware_monitor.mcp_server import tools_vsphere91  # noqa: E402,F401

# The docstrings above are the schema. `describe_tool_parameters` copies each
# `Args:` entry into the JSON schema an agent actually reads, and closes the
# object. Without it every parameter reaches the model as a bare name and a
# type, which is how a wrong guess becomes an unfiltered result or a silent
# zero-row answer instead of an error (real-hardware round, 2026-08-30).
_DESCRIBED_PARAMS = describe_tool_parameters(mcp._tool_manager._tools)
