"""Log scanner: queries vCenter/ESXi events and classifies issues.

Security: All vSphere-sourced content (event messages, host log lines) is
sanitized before output to prevent prompt injection attacks.  Sanitization
includes truncation, control-character removal, and explicit boundary markers
so that downstream consumers (including LLM agents) can distinguish trusted
output from untrusted vSphere data.
"""

from __future__ import annotations

import http.client
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from xml.parsers.expat import ExpatError

from pyVmomi import vim, vmodl
from vmware_policy import paginated, sanitize

from vmware_monitor.config import ScannerConfig
from vmware_monitor.ops._collect import _collect
from vmware_monitor.ops.health import (
    CRITICAL_EVENTS,
    WARNING_EVENTS,
    query_events,
    short_event_type,
)
from vmware_monitor.ops.investigate_host import HostNotFoundError

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

_log = logging.getLogger("vmware-monitor.log-scanner")

_ERROR_PATTERNS = (
    "error", "fail", "critical", "panic", "lost access",
    "cannot", "timeout", "refused", "corrupt",
)
_CRITICAL_PATTERNS = ("critical", "panic", "corrupt")

#: ESXi writes a level on every syslog line, e.g. ``Er(163)``. A finding's
#: severity follows it: a lab scan (2026-09-14) reported 353 lines, all
#: "warning", many of them ``In(166)`` informational lines that merely contain a
#: trouble word. A keyword in _CRITICAL_PATTERNS still wins; a line with no level
#: token keeps the keyword rule.
_LEVEL_TOKEN = re.compile(r"\b(Em|Al|Cr|Er|Wa|No|In|Db)\(\d+\)")
_LEVEL_SEVERITY = {
    "Em": "critical", "Al": "critical", "Cr": "critical",
    "Er": "warning", "Wa": "warning",
    "No": "info", "In": "info", "Db": "info",
}
_LOG_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s*")
_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


def _pattern(line: str) -> str:
    """A line with what varies between repeats (times, ids, numbers) masked out."""
    text = _LOG_TIME.sub("", line)
    text = re.sub(r"\b(opID|sid|user)=\S+", r"\1=*", text)
    text = re.sub(r"0x[0-9a-fA-F]+", "0x#", text)
    text = re.sub(r"\([0-9a-fA-F]{6,}\)", "(#)", text)
    text = re.sub(r"\d+", "#", text)
    return re.sub(r"\s+", " ", text).strip()[:160]


def group_findings(rows: list[dict]) -> list[dict]:
    """Collapse findings that are the same line repeated into one row each.

    A group is one pattern in one log: ``count``, the ``hosts`` it came from,
    the earliest and latest log time seen, the worst severity, and one sample.
    Ordered worst severity first, then most frequent.
    """
    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["source"], row.get("pattern") or row["message"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "severity": row["severity"],
                "log_level": row.get("log_level"),
                "source": row["source"],
                "pattern": key[1],
                "count": 0,
                "hosts": set(),
                "first_seen": None,
                "last_seen": None,
                "sample": row["message"],
            }
        group["count"] += 1
        group["hosts"].add(row["entity"])
        # No severity merge: the level token and any critical keyword are part of
        # the pattern, so every row in a group already has the same severity.
        seen = row.get("log_time")
        if seen:
            group["first_seen"] = min(filter(None, (group["first_seen"], seen)))
            group["last_seen"] = max(filter(None, (group["last_seen"], seen)))
    ordered = sorted(
        groups.values(),
        key=lambda g: (_SEVERITY_RANK.get(g["severity"], 9), -g["count"], g["pattern"]),
    )
    return [{**g, "hosts": sorted(g["hosts"])} for g in ordered]

# Asking for a line past the end returns no text and the log's last line
# number in ``lineEnd`` — how the scan learns where the end is.
_PAST_THE_END = 999999999

# What a log read may raise that means "this log could not be read": any
# vSphere fault (NoPermission, CannotAccessFile, InvalidRequest, …) and the
# transport failures pyVmomi's SOAP adapter lets through (socket/SSL errors
# are OSError; a non-200/500 reply is HTTPException; a garbled body is
# ExpatError). Deliberately NOT ``Exception``: AttributeError / TypeError /
# NameError are bugs in this code, and catching them is exactly how the scan
# read nothing for years while reporting every log as merely unreadable.
_UNREADABLE = (vmodl.MethodFault, OSError, http.client.HTTPException, ExpatError)


@dataclass(frozen=True)
class HostLogPass:
    """One pass over the host logs, and how far each log was read.

    ``positions`` maps ``(host name, log key)`` to the last line number read,
    carried forward from the ``since`` the pass was given for any log it could
    not read this time. ``lines_skipped`` rows are ``{host, log, skipped}``,
    where ``skipped`` is ``None`` when the count is unknown (the log rotated).
    """

    items: tuple[dict, ...]
    logs_unavailable: tuple[dict, ...]
    lines_skipped: tuple[dict, ...]
    positions: Mapping[tuple[str, str], int]
    hosts_matched: int


@dataclass(frozen=True)
class _LogRead:
    text: tuple[str, ...]
    position: int
    skipped: int | None


def scan_logs(
    si: ServiceInstance,
    scanner_config: ScannerConfig,
) -> list[dict]:
    """Scan recent events/logs and return issues above severity threshold.

    Returns a list of issue dicts with keys: severity, source, message, time.
    """
    content = si.RetrieveContent()
    event_mgr = content.eventManager

    now = datetime.now(tz=timezone.utc)
    begin = now - timedelta(hours=scanner_config.lookback_hours)

    filter_spec = vim.event.EventFilterSpec(
        time=vim.event.EventFilterSpec.ByTime(beginTime=begin, endTime=now)
    )

    # Shared guard: NotSupported (standalone ESXi) → no events; auth/network
    # failures re-raise so the scan cycle reports them instead of "all clear".
    events = query_events(event_mgr, filter_spec)
    threshold = scanner_config.severity_threshold
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    min_rank = severity_rank.get(threshold, 1)

    issues: list[dict] = []
    for event in events:
        event_type = type(event).__name__

        if short_event_type(event_type) in CRITICAL_EVENTS:
            severity = "critical"
        elif short_event_type(event_type) in WARNING_EVENTS:
            severity = "warning"
        else:
            continue  # Skip info-level for scanner

        if severity_rank.get(severity, 2) > min_rank:
            continue

        # Sanitize event message: truncate, strip ALL control characters,
        # and wrap in boundary markers to prevent prompt injection from
        # attacker-controlled vSphere event content.
        raw_msg = event.fullFormattedMessage or str(event)
        safe_msg = sanitize(raw_msg, max_len=500)

        issues.append({
            "severity": severity,
            "source": "event",
            "event_type": event_type,
            "message": f"[VSPHERE_EVENT]{safe_msg}[/VSPHERE_EVENT]",
            "time": str(event.createdTime),
            "entity": _safe_entity_name(event),
        })

    return issues


def scan_host_logs(
    si: ServiceInstance,
    host_name: str | None = None,
    log_keys: tuple[str, ...] = ("hostd", "vmkernel", "vpxa"),
    lines: int = 500,
    group: bool = False,
) -> dict:
    """Scan the last ``lines`` lines of each ESXi host log for error patterns.

    This is the interactive read (the ``host_log_scan`` tool): every call
    reads the tail of each log, remembering nothing between calls. The daemon
    uses :func:`scan_host_logs_since`, which reads only what is new.

    Returns the family list envelope. ``total`` is deliberately ``None``: only
    the last ``lines`` entries of each log are read, so how many matching lines
    exist in the full log is not something this code knows. No row limit is
    applied, so ``truncated`` stays False; the null total is what stops the
    result from being read as "these are all the errors on the host".

    Raises:
        ValueError: ``lines`` is below 1.
        HostNotFoundError: ``host_name`` matched no host — otherwise a typo
            would come back in exactly the shape of a clean host.
    """
    if lines < 1:
        raise ValueError(f"lines must be at least 1 (got {lines}); the default is 500.")
    result = _scan_pass(si, host_name, log_keys, lines, since=None)
    if host_name and result.hosts_matched == 0:
        raise HostNotFoundError(
            f"Host not found. Run list_esxi_hosts to see available hosts and copy "
            f"an exact name. Requested: '{host_name}'"
        )
    rows = list(result.items)
    items = group_findings(rows) if group else rows
    return paginated(
        items,
        logs_unavailable=list(result.logs_unavailable),
        lines_matched=len(rows),
        grouped=group,
    )


def scan_host_logs_since(
    si: ServiceInstance,
    since: Mapping[tuple[str, str], int],
    log_keys: tuple[str, ...] = ("hostd", "vmkernel", "vpxa"),
    lines: int = 500,
) -> HostLogPass:
    """Read only the host-log lines written since the previous pass (the daemon).

    ``since`` is the previous pass's ``positions``. A log with no entry is
    read from its last ``lines`` lines. A log whose line count went down has
    rotated: its last ``lines`` lines are read, and a ``lines_skipped`` row with
    an unknown count says that whatever was written between the previous read
    and the rotation was not scanned. More than ``lines`` new lines: the newest
    ``lines`` are read and a ``lines_skipped`` row carries the count. A rotation
    that has already grown past the previous position looks like growth, not
    rotation — the line count is all this API reports.
    """
    return _scan_pass(si, None, log_keys, lines, since=since)


def _scan_pass(
    si: ServiceInstance,
    host_name: str | None,
    log_keys: tuple[str, ...],
    lines: int,
    since: Mapping[tuple[str, str], int] | None,
) -> HostLogPass:
    # BrowseDiagnosticLog lives on content.diagnosticManager, NOT on the host's
    # configManager.diagnosticSystem (vim.host.DiagnosticSystem has no such
    # method). The old code called it there; every call raised AttributeError,
    # the except swallowed it, and from v0.1.0 to 2026-09-11 this scan never
    # read a line. Through vCenter the call names the host; a standalone ESXi
    # rejects host= with vmodl.fault.InvalidRequest (both verified live on
    # 8.0.3; pyVmomi has no vim.fault.InvalidRequest).
    content = si.RetrieveContent()
    diag_mgr = content.diagnosticManager
    on_vcenter = getattr(getattr(content, "about", None), "apiType", "") == "VirtualCenter"

    items: list[dict] = []
    unavailable: list[dict] = []
    skipped: list[dict] = []
    positions = dict(since or {})
    hosts_matched = 0
    # Names for every host in one PropertyCollector call, narrowed to host_name
    # early (issue #31 class).
    for host_ref, props in _collect(si, [vim.HostSystem], ["name"]):
        raw_name = props.get("name", "")
        if host_name and raw_name != host_name:
            continue
        hosts_matched += 1
        # The host name is vSphere text too, and it goes into every message.
        name = sanitize(raw_name, max_len=200)
        scope = {"host": host_ref} if on_vcenter else {}
        for log_key in log_keys:
            prior = None if since is None else since.get((raw_name, log_key))
            try:
                read = _read_log(diag_mgr, log_key, scope, lines, prior)
            except _UNREADABLE as exc:
                # A log we could not read is not a clean log. Report it, so
                # "no findings" can be told apart from "nothing was read".
                _log_read_failure(name, log_key, exc)
                unavailable.append({"host": name, "log": log_key, "reason": _read_failure(exc)})
                continue
            positions[(raw_name, log_key)] = read.position
            if read.skipped != 0:
                skipped.append({"host": name, "log": log_key, "skipped": read.skipped})
            items.extend(_findings(read.text, name, log_key))

    return HostLogPass(
        items=tuple(items),
        logs_unavailable=tuple(unavailable),
        lines_skipped=tuple(skipped),
        positions=positions,
        hosts_matched=hosts_matched,
    )


def _window(total: int, prior: int | None, lines: int) -> tuple[int, int, int | None]:
    """Which lines to read: ``(start, count, skipped)``; skipped None = unknown."""
    tail_start = max(1, total - lines + 1)
    if prior is None:
        return tail_start, lines, 0
    if total < prior:
        return tail_start, lines, None  # rotated
    new = total - prior
    if new > lines:
        return tail_start, lines, new - lines
    return prior + 1, new, 0


def _read_log(
    diag_mgr: object,
    log_key: str,
    scope: dict,
    lines: int,
    prior: int | None,
) -> _LogRead:
    """Probe for the last line number, then read the window after ``prior``."""
    probe = diag_mgr.BrowseDiagnosticLog(key=log_key, start=_PAST_THE_END, **scope)
    total = getattr(probe, "lineEnd", 0) or 0
    start, count, skipped = _window(total, prior, lines)
    if count <= 0:
        return _LogRead(text=(), position=total, skipped=skipped)
    data = diag_mgr.BrowseDiagnosticLog(key=log_key, start=start, lines=count, **scope)
    text = tuple(data.lineText or ()) if data else ()
    position = start + len(text) - 1 if text else total
    return _LogRead(text=text, position=position, skipped=skipped)


def _findings(text: tuple[str, ...], name: str, log_key: str) -> list[dict]:
    """The lines matching a trouble pattern, as issue rows."""
    rows: list[dict] = []
    for line in text:
        line_lower = line.lower()
        if not any(pattern in line_lower for pattern in _ERROR_PATTERNS):
            continue
        level_match = _LEVEL_TOKEN.search(line)
        level = level_match.group(1) if level_match else None
        if any(p in line_lower for p in _CRITICAL_PATTERNS):
            severity = "critical"
        elif level:
            severity = _LEVEL_SEVERITY[level]
        else:
            severity = "warning"
        time_match = _LOG_TIME.match(line.strip())
        # Sanitize host log lines: truncate, strip ALL control characters,
        # and wrap in boundary markers to prevent prompt injection from
        # attacker-controlled content.
        safe_line = sanitize(line.strip(), max_len=200)
        rows.append({
            "severity": severity,
            "source": f"host_log:{log_key}",
            "message": f"[VSPHERE_HOST_LOG]{name}: {safe_line}[/VSPHERE_HOST_LOG]",
            "time": str(datetime.now(tz=timezone.utc)),
            "entity": name,
            "log_level": level,
            "log_time": time_match.group(1) if time_match else None,
            "pattern": _pattern(sanitize(line.strip(), max_len=500)),
        })
    return rows


def _log_read_failure(name: str, log_key: str, exc: Exception) -> None:
    """Server-log record of an unreadable log, at a level the daemon and the
    MCP server actually print (both run at INFO) — _read_failure points here."""
    detail = sanitize(str(exc), max_len=300).replace("\n", " ")
    _log.warning(
        "Could not read the %s log on host %s: %s: %s",
        log_key, name, type(exc).__name__, detail,
    )
    _log.debug("Traceback for the %s log on %s", log_key, name, exc_info=True)


def _read_failure(exc: Exception) -> str:
    """Why a log could not be read, in words an operator can act on.

    Authored, not quoted: the fault text can carry host names and paths, and
    this reaches the agent verbatim.
    """
    if isinstance(exc, vim.fault.NoPermission):
        return ("NoPermission — reading host logs needs the Global.Diagnostics "
                "privilege, which vCenter's Read-Only role does not include")
    return (f"{type(exc).__name__} — the log could not be read; the server log "
            "has a warning with the fault detail")


def _safe_entity_name(event) -> str:
    """Safely extract entity name from event."""
    try:
        if hasattr(event, "vm") and event.vm:
            return event.vm.name
        if hasattr(event, "host") and event.host:
            return event.host.name
        if hasattr(event, "ds") and event.ds:
            return event.ds.name
    except Exception:
        _log.debug("Failed to extract entity name from event", exc_info=True)
    return "N/A"
