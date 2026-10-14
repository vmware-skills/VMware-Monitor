"""Regression tests — pyVmomi API misuse found in the 2026-06 vim-conformance audit.

Shared-code bugs with VMware-AIops (family rule: fix in both repos together):

- H: sensor status comes from healthState.key (green/yellow/red), not sensorType
- M: event class is DVPortgroupReconfiguredEvent (lowercase g) — typo'd names never match

See tests/eval/regression/test_vim_attribute_conformance.py for the generic
introspection net that prevents the whole bug class.
"""

from __future__ import annotations

from unittest.mock import MagicMock


# ── H: hardware sensor status must come from healthState.key ──


def test_hardware_status_reads_healthstate_not_sensortype() -> None:
    """sensor.sensorType is the category (temperature/voltage/fan...), not the
    health. The green/yellow/red status lives in sensor.healthState.key."""
    from unittest.mock import patch

    from vmware_monitor.ops.health import get_host_hardware_status

    sensor = MagicMock()
    sensor.name = "CPU1 Temp"
    sensor.sensorType = "temperature"
    sensor.healthState.key = "green"
    sensor.currentReading = 4500
    sensor.baseUnits = "C"

    runtime_health = MagicMock()
    runtime_health.systemHealthInfo.numericSensorInfo = [sensor]

    # get_host_hardware_status now batches name + runtime.healthSystemRuntime via
    # PropertyCollector instead of walking a ContainerView with lazy access.
    collected = [(MagicMock(), {"name": "esxi-1", "runtime.healthSystemRuntime": runtime_health})]
    with patch("vmware_monitor.ops.health._collect", return_value=collected):
        rows = get_host_hardware_status(MagicMock())["items"]

    assert rows, "expected one sensor row"
    assert rows[0]["status"] == "green", (
        f"status must come from healthState.key, got {rows[0]['status']!r} (sensorType?)"
    )
    assert rows[0]["type"] == "temperature", "sensorType should be kept as the 'type' column"


# ── M: every event name in the severity sets must exist in vim.event ──


def test_all_event_severity_names_exist_in_pyvmomi() -> None:
    """Typo'd event class names (e.g. DVPortGroupReconfiguredEvent with capital G)
    silently never match type(event).__name__ — events fall through to 'info'."""
    from pyVmomi import vim

    from vmware_monitor.ops.health import CRITICAL_EVENTS, INFO_EVENTS, WARNING_EVENTS

    missing = sorted(
        name
        for name in (CRITICAL_EVENTS | WARNING_EVENTS | INFO_EVENTS)
        if not hasattr(vim.event, name)
    )
    assert not missing, f"event names not found in vim.event (typo?): {missing}"


def test_mcp_exposes_snapshot_listing() -> None:
    """踩坑 #34: CLI had `vm snapshot-list` but MCP never registered the tool —
    docs claimed 8 tools while only 7 existed. Fixed 2026-06-08."""
    import asyncio

    from vmware_monitor.mcp_server.server import mcp

    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "list_vm_snapshots" in tools
    # Observability expansion (perf/snapshot-aging/infra/capacity/activity)
    # took the surface from 11 → 21. Keep CLI↔MCP parity locked so a future
    # accidental drop is caught (same guard rationale as 踩坑 #34).
    expected_new = {
        "get_host_performance", "get_vm_performance", "find_aging_snapshots",
        "get_certificate_status", "get_license_status", "get_ntp_status",
        "get_datastore_capacity", "get_resource_pool_usage",
        "list_active_tasks", "list_active_sessions",
        # v1.7.3: host syslog error scan surfaced as an MCP tool (was
        # scanner/CLI-only); requested by juanpf-ha on issue #31.
        "scan_host_logs",
        # issue #31 follow-up: opinionated cross-cluster triage rollup — the
        # "is anything on fire?" glance juanpf-ha asked for (aggregation in the
        # tool, model just renders; not an Aria replacement).
        "get_cluster_health_summary",
        # issue #31 follow-up: object-centered drill-down — "what is happening
        # around this <object>?" correlating related infra + event timeline.
        "investigate_vm",
        "investigate_host",
        "investigate_datastore",
        # issue #31 follow-up: cross-vCenter "what needs attention now?" roll-up.
        "get_cross_vcenter_attention",
        # vSphere 9.1 read tools: host memory tiering (pyVmomi) + vLCM/deployment
        # over vSphere Automation REST. All read-only.
        "get_host_memory_tiering",
        "get_cluster_patch_compliance",
        "get_cluster_last_apply_result",
        "get_vcenter_deployment_size",
    }
    missing = expected_new - tools
    assert not missing, f"observability tools missing from MCP: {sorted(missing)}"
    assert len(tools) == 32, f"expected 32 MCP tools, got {len(tools)}: {sorted(tools)}"
