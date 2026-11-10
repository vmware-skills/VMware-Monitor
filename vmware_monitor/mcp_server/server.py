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

import sys as _sys
import functools
import json
import logging
import ssl
import threading
from collections.abc import Callable
from typing import Any, Literal, Optional  # noqa: F401 — compatibility/runtime export

# MCP SDK — Model Context Protocol server framework
from mcp.server.fastmcp import FastMCP
from pyVmomi import vim
from vmware_policy import (  # noqa: F401 — compatibility/runtime export
    describe_tool_parameters,
    mtime_cached_loader,
    sanitize,
    set_environment_resolver,
    vmware_tool,
    skill_name,
)

from vmware_monitor import __version__

# Internal VMware monitoring modules (all read-only operations)
from vmware_monitor.config import CONFIG_FILE, ConfigError, UnknownTargetError, load_config
from vmware_monitor.connection import (
    NOT_AUTHENTICATED_HINT,
    ConnectionManager,
    NoVSphereRoleError,
)
from vmware_monitor.mcp_server import apps
from vmware_monitor.ops.activity import get_active_sessions, get_active_tasks  # noqa: F401 — compatibility/runtime export
from vmware_monitor.ops.attention import (  # noqa: F401 — compatibility/runtime export
    get_cross_vcenter_attention as _ops_get_cross_vcenter_attention,
)
from vmware_monitor.ops.capacity import (  # noqa: F401 — compatibility/runtime export
    get_datastore_capacity as _ops_get_datastore_capacity,
    get_resource_pool_usage as _ops_get_resource_pool_usage,
)
from vmware_monitor.ops.cluster_summary import (  # noqa: F401 — compatibility/runtime export
    get_cluster_health_summary as _ops_get_cluster_health_summary,
)
from vmware_monitor.ops.health import (  # noqa: F401 — compatibility/runtime export
    get_active_alarms,
    get_host_hardware_status,
    get_recent_events,
)
from vmware_monitor.ops.health import get_host_services as _ops_get_host_services  # noqa: F401 — compatibility/runtime export
from vmware_monitor.ops.infra_health import (  # noqa: F401 — compatibility/runtime export
    get_certificate_status as _ops_get_certificate_status,
    get_license_status as _ops_get_license_status,
    get_ntp_status as _ops_get_ntp_status,
)
from vmware_monitor.ops.inventory import (  # noqa: F401 — compatibility/runtime export
    list_clusters,
    list_datastores,
    list_hosts,
    list_networks,
    list_vms,
)
from vmware_monitor.ops.investigate_datastore import (  # noqa: F401 — compatibility/runtime export
    DatastoreNotFoundError,
    get_datastore_investigation_bundle,
)
from vmware_monitor.ops.investigate_host import (  # noqa: F401 — compatibility/runtime export
    HostNotFoundError,
    get_host_investigation_bundle,
)
from vmware_monitor.ops.investigate_vm import get_vm_investigation_bundle  # noqa: F401 — compatibility/runtime export
from vmware_monitor.ops.performance import (  # noqa: F401 — compatibility/runtime export
    get_host_performance as _ops_get_host_performance,
    get_vm_performance as _ops_get_vm_performance,
)
from vmware_monitor.ops.backup_window import (  # noqa: F401 — compatibility/runtime export
    AmbiguousVMError,
    get_backup_snapshot_history,
)
from vmware_monitor.ops.snapshots import list_snapshot_aging  # noqa: F401 — compatibility/runtime export
from vmware_monitor.ops.vm_info import VMNotFoundError, list_snapshots  # noqa: F401 — compatibility/runtime export
from vmware_monitor.ops.vm_info import get_vm_info as _ops_get_vm_info  # noqa: F401 — compatibility/runtime export
from vmware_monitor.rest import RestAuthError
from vmware_monitor.scanner.log_scanner import scan_host_logs as _ops_scan_host_logs  # noqa: F401 — compatibility/runtime export
from vmware_monitor.scanner.log_scanner import validate_log_keys  # noqa: F401 — compatibility/runtime export

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
    # AIops#47. The role message is authored from the target name and fixed
    # text, and is longer than the 300-character cap that would cut off its
    # remedy; NotAuthenticated is a vmodl fault, which would otherwise be
    # reduced to its class name with a hint about credentials.
    if isinstance(exc, NoVSphereRoleError):
        return sanitize(str(exc), 600)
    if isinstance(exc, vim.fault.NotAuthenticated):
        return f"NotAuthenticated: {NOT_AUTHENTICATED_HINT}"
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
#: For the two failures where connectivity and credentials are fine (AIops#47).
_ROLE_HINT = (
    "Run 'vmware-monitor doctor': it checks every target's login and whether "
    "vCenter gives the account a session."
)


#: For an error this skill authored that is not about reaching the endpoint:
#: the message already names the fix, and doctor would report green (2026-10-17).
_FIX_IN_MESSAGE_HINT = "The error message says what to change; connectivity is not the problem."

#: Authored errors whose cause is the request, not the connection. ssl.SSLError
#: subclasses ValueError and is excluded explicitly below.
_REQUEST_ERRORS = (
    ValueError,
    UnknownTargetError,
    VMNotFoundError,
    AmbiguousVMError,
    HostNotFoundError,
    DatastoreNotFoundError,
)

#: ValueErrors whose text was not authored as a remedy: a body that was not
#: JSON (a reverse proxy answering HTML, AIops#47) or not UTF-8. Review
#: 2026-10-17: these got "connectivity is not the problem", the opposite of true.
_UNAUTHORED_VALUE_ERRORS = (ssl.SSLError, json.JSONDecodeError, UnicodeError)


def _is_request_error(exc: Exception) -> bool:
    return isinstance(exc, _REQUEST_ERRORS) and not isinstance(exc, _UNAUTHORED_VALUE_ERRORS)


def _hint_for(exc: Exception) -> str:
    if isinstance(exc, (NoVSphereRoleError, vim.fault.NotAuthenticated)):
        return _ROLE_HINT
    # RestAuthError is a ValueError, but credentials and a missing REST API are
    # exactly what doctor checks.
    if _is_request_error(exc) and not isinstance(exc, RestAuthError):
        return _FIX_IN_MESSAGE_HINT
    return _DOCTOR_HINT


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
            return {"error": _safe_error(e, fn.__name__), "hint": _hint_for(e)}

    return wrapper


_BASE_INSTRUCTIONS = (
    "VMware vCenter/ESXi read-only monitoring. "
    "Query inventory, check health/alarms, and view VM info. "
    "No destructive operations — code-level enforced."
)

_TARGET_RULE = (
    " Choosing a target: every tool that reads vSphere takes `target`. Choose it "
    "from what the user asked. A vCenter, the whole environment, clusters or "
    "several hosts: a vcenter target. One ESXi host the user names: the vcenter "
    "target that manages it, because vCenter holds that host's alarms, events and "
    "tasks; use the host's own esxi target only when the user asks about the host "
    "directly or no vCenter manages it. If the request does not say which, and "
    "targets of different types could answer differently, ask the user which one "
    "before querying. Every result names the `target` that answered; say it in "
    "the answer."
)


def _target_instructions() -> str:
    """Server instructions that name the configured targets and how to choose one.

    Before this, the model was never told which targets exist. With a standalone
    ESXi host listed first, every tool it called without ``target`` answered from
    that host, and it reported "vCenter has 9 VMs" (read from the ESXi host) and "no
    vCenter is configured" (2026-09-15 conversation tests). Never raises: a
    missing or broken config must not stop the server from starting — the tools
    report that error themselves.

    A config it could not read is still said out loud, under the same heading.
    Dropping the line was this function's own version of the bug it exists to
    prevent: on a machine that has not run ``init`` yet — every customer, on day
    one — the client saw no listing at all, and "this skill has no targets worth
    naming" reads exactly like "this skill could not read them" (2026-09-20).
    """
    try:
        cfg = load_config()
        # Asked only when there is something to be the default: an empty
        # targets list makes `default_target` raise, and "could not be read"
        # is a different fact from "you have not added one yet".
        default_name = cfg.default_target.name if cfg.targets else None
    except Exception as exc:  # noqa: BLE001 — instructions are advisory, startup is not
        # Only the exception's type: its text quotes the config path.
        detail = f"could not be read ({type(exc).__name__}) — run `vmware-monitor doctor`"
    else:
        listed = "; ".join(
            f"{t.name} ({t.type}, {t.host}{', default' if t.name == default_name else ''})"
            for t in cfg.targets
        )
        detail = listed or (
            "none yet — add one under `targets:` in ~/.vmware-monitor/config.yaml"
        )
    return f"{_BASE_INSTRUCTIONS} Configured targets: {detail}.{_TARGET_RULE}"


mcp = FastMCP("vmware-monitor", instructions=_target_instructions())
apps.register(mcp)


def _with_target(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Add ``target: {name, type}`` to the dict a target-taking tool returns.

    A result that does not say where it came from cannot be told apart: an
    answer read from a standalone ESXi host and one read from vCenter look the
    same to the model. Tools without a ``target`` parameter (those that span
    every target) are returned unchanged; so is a non-dict result, or one that
    already names its target.
    """
    import inspect

    signature = inspect.signature(fn)
    if "target" not in signature.parameters:
        return fn

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = fn(*args, **kwargs)
        if not isinstance(result, dict) or "target" in result:
            return result
        try:
            name = signature.bind_partial(*args, **kwargs).arguments.get("target")
            cfg = _ensure_conn_mgr()._config
            resolved = cfg.get_target(name) if name else cfg.default_target
        except Exception:  # noqa: BLE001 — naming the target must never break the answer
            return result
        return {"target": {"name": resolved.name, "type": resolved.type}, **result}

    wrapper._names_target = True  # type: ignore[attr-defined]
    return wrapper


_register_tool = mcp.tool


def _tool_naming_its_target(*args: Any, **kwargs: Any) -> Any:
    """``mcp.tool`` that wraps every registered function with :func:`_with_target`."""
    if args and callable(args[0]):
        return _register_tool(**kwargs)(_with_target(args[0]))
    decorator = _register_tool(*args, **kwargs)
    return lambda fn: decorator(_with_target(fn))


mcp.tool = _tool_naming_its_target  # type: ignore[method-assign]

# FastMCP takes no version argument and leaves the lowlevel server's at
# None, which makes `initialize` answer with the MCP SDK's version rather
# than ours. Set it so a client can tell which release it is talking to.
mcp._mcp_server.version = __version__

# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

_conn_mgr: Optional[ConnectionManager] = None
# Calls run in worker threads side by side (HLD §16 D-13(2)); without the lock two
# first calls could each build a manager, and so a second pool of sessions.
_conn_mgr_lock = threading.Lock()


def _ensure_conn_mgr() -> ConnectionManager:
    """Lazily build the shared ConnectionManager (does not connect anything)."""
    global _conn_mgr  # noqa: PLW0603
    if _conn_mgr is not None:
        return _conn_mgr
    with _conn_mgr_lock:
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





















from vmware_monitor.mcp_server._tools_inventory import register_tools as _register_inventory

(
    list_virtual_machines,
    list_esxi_hosts,
    list_all_datastores,
    list_all_clusters,
    get_cluster_health_summary,
    investigate_vm,
    investigate_host,
    investigate_datastore,
    get_cross_vcenter_attention,
    list_all_networks,
) = _register_inventory(mcp, _sys.modules[__name__])


# ---------------------------------------------------------------------------
# Health tools (read-only)
# ---------------------------------------------------------------------------











from vmware_monitor.mcp_server._tools_health import register_tools as _register_health

(
    get_alarms,
    get_events,
    get_host_sensors,
    get_host_services,
    scan_host_logs,
) = _register_health(mcp, _sys.modules[__name__])


# ---------------------------------------------------------------------------
# VM info tool (read-only)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Performance tools (read-only — real-time PerfManager counters)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Snapshot aging (read-only — inventory-wide sprawl analysis)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Infrastructure health (read-only — certificates, licenses, NTP)
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Capacity analytics (read-only — over-commit and resource pools)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Activity tracking (read-only — tasks and sessions)
# ---------------------------------------------------------------------------







from vmware_monitor.mcp_server._tools_runtime import register_tools as _register_runtime

(
    get_vm_info,
    list_vm_snapshots,
    get_host_performance,
    get_vm_performance,
    find_aging_snapshots,
    get_vm_backup_snapshot_history,
    get_certificate_status,
    get_license_status,
    get_ntp_status,
    get_datastore_capacity,
    get_resource_pool_usage,
    list_active_tasks,
    list_active_sessions,
    get_access_info,
) = _register_runtime(mcp, _sys.modules[__name__])


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


# Keyed by skill: the registry used to be one process-global slot, and a
# bare `import` of any sibling's server module replaced whichever resolver
# was there -- measured turning a freeze-production-writes rule from DENY
# to ALLOW. Keyed, a resolver only ever answers for its own skill, so
# registering at import time is safe again.
set_environment_resolver(_environment_for, skill=skill_name(__name__))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


#: How long a stop signal waits for the session logout before exiting anyway.
#: A logout to a vCenter that answers takes well under a second.
_STOP_LOGOUT_SECONDS = 5.0


def _exit_on_stop_signals() -> None:
    """Turn the signals a client stops this server with into a normal exit.

    Claude Code stops a stdio MCP server with SIGINT and then SIGTERM about a
    millisecond later (measured 2026-09-15). Python's default SIGTERM ends the
    process on the spot, before ``atexit`` runs, so the ``Disconnect`` the
    connection layer registered never happened: every conversation left its
    vCenter/ESXi session open (13 on one ESXi host and 11 on vCenter in about
    eleven minutes). ``SystemExit`` is not enough: raised from the handler it unwinds the event
    loop, but interpreter shutdown then waits for anyio's worker thread blocked
    reading stdin, which the client keeps open, so ``atexit`` still never ran
    (independent review, 2026-09-15; a test driving the real stdio loop hung in
    all five skills). So the first stop signal ignores the rest, runs the
    ``atexit`` callbacks, and leaves with ``os._exit`` — nothing waits on that
    thread. The callbacks run on a worker thread with a deadline: pyVmomi
    connects with ``httpConnectionTimeout=None``, so a logout to a vCenter that
    stopped answering, or one waiting on the SOAP stub lock a tool call held
    when the signal landed, otherwise left a server that ignored every stop
    signal and only SIGKILL ended (second independent review, 2026-09-15).
    """
    import atexit
    import os
    import signal
    import threading

    stop_signals = [
        getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name)
    ]

    def _stop(signum: int, _frame: object) -> None:
        for sig in stop_signals:
            signal.signal(sig, signal.SIG_IGN)
        try:
            logout = threading.Thread(
                target=atexit._run_exitfuncs, name="logout-on-stop", daemon=True
            )
            logout.start()
            logout.join(_STOP_LOGOUT_SECONDS)
            if logout.is_alive():
                # Non-blocking, straight to fd 2: a client that keeps stderr open
                # but stops reading it would otherwise park this write, and the
                # exit, on a full pipe (independent review, 2026-09-15). A
                # message that cannot be written is dropped — exiting matters more.
                message = (
                    f"Session logout did not finish within {_STOP_LOGOUT_SECONDS:.0f}s; "
                    "exiting without it. vCenter or ESXi ends the session when it "
                    "idles out.\n"
                )
                try:
                    os.set_blocking(2, False)
                    os.write(2, message.encode("utf-8", "replace"))
                except OSError:
                    pass
        finally:
            os._exit(128 + signum)

    for sig in stop_signals:
        signal.signal(sig, _stop)


def main(
    expose: Optional[str] = None,
    allow_write: Optional[str] = None,
    transport: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    cert: Optional[str] = None,
    key: Optional[str] = None,
    public_name: Optional[str] = None,
    token_file: Optional[str] = None,
) -> None:
    """Run the MCP server over stdio."""
    logging.basicConfig(level=logging.INFO)
    _exit_on_stop_signals()
    from vmware_policy.serve import run_server
    run_server(
        mcp,
        skill="monitor",
        expose=expose, allow_write=allow_write, transport=transport, host=host, port=port,
        cert=cert, key=key, public_name=public_name, token_file=token_file,
        pooled=True,
    )


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

# Role metadata for network callers (HLD §18).
import vmware_monitor.policy_roles  # noqa: E402,F401
