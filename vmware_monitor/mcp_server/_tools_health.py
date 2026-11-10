"""Health MCP registrations sharing the server runtime.

The facade owns connection, error and Policy state; this module creates no second
server or connection manager. Returned aliases preserve the original Python API.
"""

from typing import Literal, Optional


def register_tools(mcp, runtime):
    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def get_alarms(
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] Get active/triggered alarms across the VMware inventory.

        Returns the list envelope with a real ``total``. Each alarm carries
        suggested_actions naming the companion skill and tool for remediation, who
        acknowledged it and when (``acknowledged_by`` / ``acknowledged_at``), and
        ``condition_now``: ``holds`` (live), ``cleared`` (vCenter still shows it but
        its state condition is false now — a stale alarm; see ``condition_note``), or
        ``unknown`` (event/metric-based or unreadable — never guessed). The envelope's
        ``stale_alarms`` counts the cleared ones. An expired-vCenter-license alarm is
        re-checked against this vCenter's own license assignment. ``object_label``
        names alarms about the vCenter appliance itself (e.g. "vCenter appliance
        192.168.60.16") that sit on the inventory root; ``entity_name`` stays the
        object vmware-aiops resolves. Do not report a ``cleared`` alarm as
        a live problem; do not report an ``unknown`` one as resolved. Empty
        ``items`` with ``truncated`` False means there genuinely are no active alarms —
        never report "no data" otherwise.

        Use this for the raw alarm list; prefer get_cluster_health_summary when you want
        alarms folded into a whole-cluster verdict. Then drill into the flagged
        object with investigate_vm or investigate_host.

        Args:
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max alarms to return (None = all).
        """
        si = runtime._get_connection(target)
        return runtime.get_active_alarms(si, limit=limit)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def get_events(
        hours: int = 24,
        severity: Literal["critical", "warning", "info"] = "warning",
        target: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        include_routine: bool = False,
    ) -> dict:
        """[READ] Get recent vCenter/ESXi events filtered by severity.

        Returns the list envelope, newest first. Up to 5000 events in the window are
        read; when more matched, ``read_truncated`` is true and ``read_note`` says how
        far back the read got — the oldest events were not examined, so narrow
        ``hours`` before concluding nothing happened earlier in the window.

        Severity is this skill's own ranking where it has one, and otherwise
        vCenter's published event catalogue. An event neither can rank comes back
        with severity "unknown" (it is returned, not filtered out) and is counted in
        the envelope's ``unclassified``, with ``classification_note`` explaining. An
        empty ``items`` alongside a non-zero ``unclassified`` does NOT mean the
        window was quiet — read the note before reporting all-clear.

        Use this for an inventory-wide event sweep. When you already know the object,
        prefer investigate_vm / investigate_host instead: they
        return the same events correlated with that object's state in one call. ESXi
        syslog lines are not events — use scan_host_logs for those.

        Args:
            hours: How many hours back to query (default 24). Ignored when both
                start and end are given; with end alone, how far back from end.
            severity: Minimum severity: "critical", "warning", or "info".
            target: vCenter/ESXi target from config (default if omitted).
            start: Window start, ISO 8601 (e.g. "2026-09-03T12:00:00Z"; no zone = UTC).
                Use it to ask about a specific day rather than the last N hours.
            end: Window end, ISO 8601; defaults to now.
            include_routine: List routine login/logout session events too. By default
                they are folded and counted in ``routine_folded`` — one local agent's
                logins can outnumber everything else in a window.
        """
        si = runtime._get_connection(target)
        return runtime.get_recent_events(
            si,
            hours=hours,
            severity=severity,
            start=start,
            end=end,
            include_routine=include_routine,
        )

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def get_host_sensors(
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] Get hardware sensor status (temperature, voltage, fan, ...) for all hosts.

        Returns the list envelope with a real ``total``; each row has host, sensor_name,
        type, reading, unit and status (green/yellow/red). No rows for a host is not
        "hardware is fine": every connected host that reports no sensors is listed in
        ``hosts_without_sensors`` with ``cim_server_running`` / ``cim_server_policy``
        (the CIM Server, sfcbd-watchdog, supplies the sensors; null = not read), and
        ``sensors_note`` says what that means — quote it.

        ``interpretation_note`` bounds what a red aggregate memory sensor proves:
        it does not identify a DIMM/slot, establish ECC errors, or predict failure.
        Keep observed status separate from a component diagnosis.

        Use this for physical hardware only — for CPU/memory load use
        get_host_performance, and follow up on a red sensor with
        investigate_host to see the alarms and events around that host.

        Args:
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max sensor rows to return (None = all).
        """
        si = runtime._get_connection(target)
        return runtime.get_host_hardware_status(si, limit=limit)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def get_host_services(
        host_name: Optional[str] = None,
        target: Optional[str] = None,
    ) -> dict:
        """[READ] Get host service status (running state and startup policy).

        Returns the list envelope; each row has host, service key, label, running (bool)
        and policy (on/off/automatic). Every matching host is enumerated, so
        ``truncated`` is always False.

        Use this to check whether SSH, NTP or the firewall service is in the expected
        state. For NTP specifically prefer get_ntp_status, which also reports the
        configured servers. Starting or stopping a service is a write — this skill
        cannot do it; use vmware-aiops.

        Args:
            host_name: Filter to a single host by exact name (None = all hosts).
            target: vCenter/ESXi target from config (default if omitted).
        """
        si = runtime._get_connection(target)
        return runtime._ops_get_host_services(si, host_name=host_name)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def scan_host_logs(
        host_name: Optional[str] = None,
        lines: int = 500,
        target: Optional[str] = None,
        group: bool = True,
        log_keys: Optional[list[str]] = None,
    ) -> dict:
        """[READ] Scan recent ESXi host syslog lines for error/warning patterns.

        Reads the last ``lines`` entries of selected logs (default hostd/vmkernel/vpxa) via the
        diagnostic system and returns only the lines matching known trouble patterns
        (error, fail, critical, panic, lost access, timeout, …). Severity follows the
        level ESXi wrote on the line (Cr/Er/Wa/In…, raw token in ``log_level``); a
        critical keyword still wins. By default findings are grouped by pattern —
        each item has count, hosts, first_seen/last_seen (log time), severity,
        source (``host_log:<key>``), pattern and one sample; ``lines_matched`` is the
        ungrouped count. ``group=false`` returns one row per line (severity, source,
        message, time, observed_at, entity, log_level, log_time). ``time`` is the log
        timestamp (null when absent); ``observed_at`` is when this scan read the line.
        Returns the list envelope {items,
        returned, limit, total, truncated, hint}. ``total`` is
        null on purpose — this is "errors within the scanned window", not all errors
        ever. ``logs_unavailable`` lists every host/log that could NOT be read, with
        the reason (e.g. the account lacks Global.Diagnostics); empty ``items`` means
        nothing matched only when ``logs_unavailable`` is empty too.

        Use this when get_events or investigate_host show a host in trouble
        but not why: vCenter events and ESXi syslog are different sources. Filter
        with ``host_name`` to keep the scan fast on large clusters; a name that
        matches no host returns an error (get the exact name from list_esxi_hosts),
        not an empty result. Every call reads the last ``lines`` lines afresh.

        Args:
            host_name: Filter to a single host by exact name (None = all hosts).
            lines: How many recent lines per log to scan (default 500, at least 1).
            target: vCenter/ESXi target from config (default if omitted).
            log_keys: 1–16 distinct diagnostic keys, e.g. ["hostd", "vobd"].
                Omit for defaults. Availability depends on the target; unsupported
                keys appear in logs_unavailable. File paths are not accepted.
            group: Group repeated lines by pattern (default true). One call on a lab
                returned 353 lines, 195 of them one statistics-provider message.
        """
        keys = runtime.validate_log_keys(log_keys)
        si = runtime._get_connection(target)
        return runtime._ops_scan_host_logs(
            si, host_name=host_name, lines=lines, group=group, log_keys=keys
        )

    return (
        get_alarms,
        get_events,
        get_host_sensors,
        get_host_services,
        scan_host_logs,
    )
