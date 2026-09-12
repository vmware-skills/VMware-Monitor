"""Regression — every list-returning tool speaks the family list envelope.

Source: VMware-AIops issue #31. An operator running Llama 3.3 70B reported that
"with long tool responses, it may omit existing information or incorrectly
state that no data was returned." A bare ``list[dict]`` gives a model no way to
tell a complete answer from page one, so it guesses — and a guess that reads
"no data" looks like a finding.

These tests pin the four properties that remove the guess:

1. all 20 list tools return the six envelope keys (explicit nulls, never a
   missing key — a missing key is exactly what a model invents a value for);
2. a page cut short by ``limit`` reports ``truncated=True`` plus a hint;
3. a short result reports ``truncated=False`` and ``hint=None``;
4. a page filled exactly to the limit whose *real* total matches is NOT flagged
   truncated — the payoff for passing an honest ``total``;

and the counterpart honesty guard: the two tools that cannot know their full
collection size report ``total=None`` rather than a fabricated number.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vmware_monitor.ops import (
    activity,
    capacity,
    health,
    infra_health,
    inventory,
    performance,
    vm_info,
)
from vmware_monitor.scanner import log_scanner

ENVELOPE_KEYS = ("items", "returned", "limit", "total", "truncated", "hint")


# ---------------------------------------------------------------------------
# Fixture plumbing — enough fake vSphere to make each op emit exactly N rows
# ---------------------------------------------------------------------------


def _si(content: object = None) -> SimpleNamespace:
    return SimpleNamespace(RetrieveContent=lambda: content)


class _Ref:
    """Hashable stand-in for a managed-object reference."""

    def __init__(self, name: str = "ref") -> None:
        self.name = name


def _collect_rows(rows):
    return lambda *a, **k: rows


# ── inventory ──────────────────────────────────────────────────────────────


def _f_list_vms(mp, n):
    """VM rows only; the host lookup and folder map see an empty inventory.

    ``list_vms`` calls ``_collect`` five times (hosts, three folder-tree passes,
    then VMs), so the stub dispatches on the requested type instead of replaying
    one row set into all five.
    """
    rows = [(_Ref(), {"name": f"vm-{i:03d}"}) for i in range(n)]
    mp.setattr(
        inventory,
        "_collect",
        lambda si, obj_type, paths: rows if obj_type[0] is vim.VirtualMachine else [],
    )
    return lambda limit=None: inventory.list_vms(_si(), limit=limit)


def _f_list_hosts(mp, n):
    mp.setattr(
        inventory,
        "_collect",
        _collect_rows([(_Ref(), {"name": f"esx-{i:03d}"}) for i in range(n)]),
    )
    return lambda limit=None: inventory.list_hosts(_si(), limit=limit)


def _f_list_datastores(mp, n):
    mp.setattr(
        inventory,
        "_collect",
        _collect_rows([(_Ref(), {"name": f"ds-{i:03d}"}) for i in range(n)]),
    )
    return lambda limit=None: inventory.list_datastores(_si(), limit=limit)


def _f_list_clusters(mp, n):
    mp.setattr(
        inventory,
        "_collect",
        _collect_rows([(_Ref(), {"name": f"cl-{i:03d}"}) for i in range(n)]),
    )
    return lambda limit=None: inventory.list_clusters(_si(), limit=limit)


def _f_list_networks(mp, n):
    mp.setattr(
        inventory,
        "_collect",
        _collect_rows([(_Ref(), {"name": f"net-{i:03d}"}) for i in range(n)]),
    )
    return lambda limit=None: inventory.list_networks(_si(), limit=limit)


# ── health ─────────────────────────────────────────────────────────────────


def _alarm_state(i):
    return SimpleNamespace(
        overallStatus="red",
        entity=_Ref(f"esx-{i:03d}"),
        alarm=SimpleNamespace(info=SimpleNamespace(name=f"alarm-{i:03d}")),
        time="2026-07-18 00:00:00",
        acknowledged=False,
    )


def _f_get_active_alarms(mp, n):
    mp.setattr(health, "_collect", _collect_rows([]))
    content = SimpleNamespace(
        rootFolder=SimpleNamespace(triggeredAlarmState=[_alarm_state(i) for i in range(n)])
    )
    return lambda limit=None: health.get_active_alarms(_si(content), limit=limit)


class BadUsernameSessionEvent(SimpleNamespace):
    """Type name is load-bearing: health.py classifies events by class name."""


def _f_get_recent_events(mp, n):
    events = [
        BadUsernameSessionEvent(
            fullFormattedMessage=f"failed login {i}",
            createdTime=f"2026-07-18 00:00:{i:02d}",
            userName="root",
        )
        for i in range(n)
    ]
    mp.setattr(health, "query_events", lambda mgr, spec: events)
    return lambda limit=None: health.get_recent_events(_si(SimpleNamespace(eventManager=_Ref())))


def _sensor(i):
    return SimpleNamespace(
        name=f"sensor-{i:03d}",
        sensorType="temperature",
        currentReading=40,
        baseUnits="C",
        healthState=SimpleNamespace(key="green"),
    )


def _f_get_host_hardware_status(mp, n):
    runtime = SimpleNamespace(
        systemHealthInfo=SimpleNamespace(numericSensorInfo=[_sensor(i) for i in range(n)])
    )
    mp.setattr(
        health,
        "_collect",
        _collect_rows([(_Ref(), {"name": "esx-01", "runtime.healthSystemRuntime": runtime})]),
    )
    return lambda limit=None: health.get_host_hardware_status(_si(), limit=limit)


def _svc(i):
    return SimpleNamespace(key=f"svc-{i:03d}", label=f"Service {i}", running=True, policy="on")


def _f_get_host_services(mp, n):
    ref = _Ref("hss-1")
    mp.setattr(
        health,
        "_collect",
        _collect_rows([(_Ref(), {"name": "esx-01", "configManager.serviceSystem": ref})]),
    )
    info = SimpleNamespace(service=[_svc(i) for i in range(n)])
    mp.setattr(health, "_collect_objects", lambda *a, **k: [(ref, {"serviceInfo": info})])
    return lambda limit=None: health.get_host_services(_si())


# ── activity ───────────────────────────────────────────────────────────────


def _task(i):
    return SimpleNamespace(
        info=SimpleNamespace(
            state="running",
            error=None,
            descriptionId=f"task-{i:03d}",
            key=f"task-{i}",
            entityName=f"vm-{i:03d}",
            progress=10,
            startTime=f"2026-07-18 00:00:{i:02d}",
            reason=None,
        )
    )


def _f_get_active_tasks(mp, n):
    content = SimpleNamespace(taskManager=SimpleNamespace(recentTask=[_task(i) for i in range(n)]))
    return lambda limit=None: activity.get_active_tasks(_si(content), limit=limit)


def _session(i):
    return SimpleNamespace(
        key=f"sess-{i}",
        userName=f"user-{i:03d}",
        fullName=f"User {i}",
        loginTime=f"2026-07-18 00:00:{i:02d}",
        lastActiveTime=f"2026-07-18 01:00:{i:02d}",
        ipAddress="10.0.0.1",
    )


def _f_get_active_sessions(mp, n):
    mgr = SimpleNamespace(
        currentSession=SimpleNamespace(key="cur"),
        sessionList=[_session(i) for i in range(n)],
    )
    return lambda limit=None: activity.get_active_sessions(
        _si(SimpleNamespace(sessionManager=mgr)), limit=limit
    )


# ── capacity ───────────────────────────────────────────────────────────────


def _f_get_datastore_capacity(mp, n):
    rows = [
        (
            _Ref(),
            {
                "name": f"ds-{i:03d}",
                "summary.type": "VMFS",
                "summary.capacity": 1000,
                "summary.freeSpace": 400,
                "summary.uncommitted": 100 + i,
            },
        )
        for i in range(n)
    ]
    mp.setattr(capacity, "_collect", _collect_rows(rows))
    return lambda limit=None: capacity.get_datastore_capacity(_si(), limit=limit)


def _f_get_resource_pool_usage(mp, n):
    alloc = SimpleNamespace(reservation=0, limit=-1)
    rows = [
        (
            _Ref(),
            {
                "name": f"pool-{i:03d}",
                "config.cpuAllocation": alloc,
                "config.memoryAllocation": alloc,
                "summary.quickStats": SimpleNamespace(overallCpuUsage=i, guestMemoryUsage=i),
            },
        )
        for i in range(n)
    ]
    mp.setattr(capacity, "_collect", _collect_rows(rows))
    return lambda limit=None: capacity.get_resource_pool_usage(_si(), limit=limit)


# ── infra_health ───────────────────────────────────────────────────────────


def _f_get_certificate_status(mp, n):
    refs = [_Ref(f"cm-{i}") for i in range(n)]
    mp.setattr(
        infra_health,
        "_collect",
        _collect_rows(
            [
                (_Ref(), {"name": f"esx-{i:03d}", "configManager.certificateManager": refs[i]})
                for i in range(n)
            ]
        ),
    )
    mp.setattr(
        infra_health,
        "_collect_objects",
        lambda *a, **k: [(r, {"certificateInfo": SimpleNamespace(notAfter=None)}) for r in refs],
    )
    return lambda limit=None: infra_health.get_certificate_status(_si(), limit=limit)


def _license(i):
    return SimpleNamespace(
        name=f"lic-{i:03d}",
        editionKey=f"edition-{i}",
        total=10,
        used=1,
        properties=[],
    )


def _f_get_license_status(mp, n):
    content = SimpleNamespace(
        licenseManager=SimpleNamespace(licenses=[_license(i) for i in range(n)])
    )
    return lambda limit=None: infra_health.get_license_status(_si(content))


def _f_get_ntp_status(mp, n):
    refs = [_Ref(f"hss-{i}") for i in range(n)]
    dt = SimpleNamespace(ntpConfig=SimpleNamespace(server=["pool.ntp.org"]))
    mp.setattr(
        infra_health,
        "_collect",
        _collect_rows(
            [
                (
                    _Ref(),
                    {
                        "name": f"esx-{i:03d}",
                        "config.dateTimeInfo": dt,
                        "configManager.serviceSystem": refs[i],
                    },
                )
                for i in range(n)
            ]
        ),
    )
    info = SimpleNamespace(service=[SimpleNamespace(key="ntpd", running=True, policy="on")])
    mp.setattr(
        infra_health,
        "_collect_objects",
        lambda *a, **k: [(r, {"serviceInfo": info}) for r in refs],
    )
    return lambda limit=None: infra_health.get_ntp_status(_si())


# ── performance ────────────────────────────────────────────────────────────


def _f_get_host_performance(mp, n):
    mp.setattr(performance, "_counter_map", lambda perf: {})
    mp.setattr(performance, "_sample_entity", lambda *a, **k: {"cpu_usage_pct": 1.0})
    mp.setattr(
        performance,
        "_collect",
        _collect_rows(
            [
                (_Ref(), {"name": f"esx-{i:03d}", "runtime.connectionState": "connected"})
                for i in range(n)
            ]
        ),
    )
    return lambda limit=None: performance.get_host_performance(
        _si(SimpleNamespace(perfManager=_Ref())), limit=limit
    )


def _f_get_vm_performance(mp, n):
    mp.setattr(performance, "_counter_map", lambda perf: {})
    mp.setattr(performance, "_sample_entity", lambda *a, **k: {"cpu_usage_pct": 1.0})
    mp.setattr(
        performance,
        "_collect",
        _collect_rows(
            [
                (_Ref(), {"name": f"vm-{i:03d}", "runtime.powerState": "poweredOn"})
                for i in range(n)
            ]
        ),
    )
    return lambda limit=None: performance.get_vm_performance(
        _si(SimpleNamespace(perfManager=_Ref())), limit=limit
    )


# ── vm_info ────────────────────────────────────────────────────────────────


def _snap(i):
    return SimpleNamespace(
        name=f"snap-{i:03d}",
        description="nightly",
        createTime="2026-07-18 00:00:00",
        state="poweredOff",
        childSnapshotList=[],
    )


def _f_list_snapshots(mp, n):
    vm = SimpleNamespace(snapshot=SimpleNamespace(rootSnapshotList=[_snap(i) for i in range(n)]))
    mp.setattr(vm_info, "_require_vm", lambda si, name: vm)
    return lambda limit=None: vm_info.list_snapshots(_si(), "vm-01")


# ── scanner ────────────────────────────────────────────────────────────────


def _f_scan_host_logs(mp, n):
    log = SimpleNamespace(
        lineEnd=n,
        lineText=[f"error: disk {i} degraded" for i in range(n)],
    )
    # The method lives on content.diagnosticManager. This fixture used to put it
    # on the host's diagnosticSystem — the one shape pyVmomi never produces,
    # which is how host_log_scan returned nothing for years with this test green.
    diag = SimpleNamespace(BrowseDiagnosticLog=lambda key, start, **scope: log)
    content = SimpleNamespace(
        diagnosticManager=diag, about=SimpleNamespace(apiType="VirtualCenter")
    )
    mp.setattr(log_scanner, "_collect", _collect_rows([(_Ref(), {"name": "esx-01"})]))
    return lambda limit=None: log_scanner.scan_host_logs(_si(content), log_keys=("hostd",))


# ---------------------------------------------------------------------------
# The 20 tools in scope, and what each one honestly knows
# ---------------------------------------------------------------------------

# name → (factory, accepts a row limit, reports a real total)
TOOLS: dict[str, tuple] = {
    "list_virtual_machines": (_f_list_vms, True, True),
    "list_esxi_hosts": (_f_list_hosts, True, True),
    "list_all_datastores": (_f_list_datastores, True, True),
    "list_all_clusters": (_f_list_clusters, True, True),
    "list_all_networks": (_f_list_networks, True, True),
    "get_alarms": (_f_get_active_alarms, True, True),
    "get_events": (_f_get_recent_events, False, False),
    "get_host_sensors": (_f_get_host_hardware_status, True, True),
    "get_host_services": (_f_get_host_services, False, True),
    "host_log_scan": (_f_scan_host_logs, False, False),
    "active_tasks": (_f_get_active_tasks, True, True),
    "active_sessions": (_f_get_active_sessions, True, True),
    "datastore_capacity": (_f_get_datastore_capacity, True, True),
    "resource_pool_usage": (_f_get_resource_pool_usage, True, True),
    "certificate_status": (_f_get_certificate_status, True, True),
    "license_status": (_f_get_license_status, False, True),
    "ntp_status": (_f_get_ntp_status, False, True),
    "host_performance": (_f_get_host_performance, True, True),
    "vm_performance": (_f_get_vm_performance, True, True),
    "vm_list_snapshots": (_f_list_snapshots, False, True),
}

ALL = sorted(TOOLS)
WITH_LIMIT = sorted(n for n, (_f, lim, _t) in TOOLS.items() if lim)
WITH_REAL_TOTAL = sorted(n for n, (_f, _l, tot) in TOOLS.items() if tot)
WITHOUT_REAL_TOTAL = sorted(n for n, (_f, _l, tot) in TOOLS.items() if not tot)


def test_scope_is_the_twenty_tools():
    assert len(TOOLS) == 20


# ---------------------------------------------------------------------------
# 1. Shape — six keys, always, for every tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ALL)
def test_every_tool_returns_the_six_envelope_keys(monkeypatch, tool):
    run = TOOLS[tool][0](monkeypatch, 3)
    out = run(limit=None)
    assert isinstance(out, dict), f"{tool} must return the envelope, not a bare list"
    for key in ENVELOPE_KEYS:
        assert key in out, f"{tool} envelope is missing '{key}'"
    assert isinstance(out["items"], list)
    assert out["returned"] == len(out["items"]) == 3


@pytest.mark.parametrize("tool", ALL)
def test_empty_result_is_still_an_envelope_not_a_silence(monkeypatch, tool):
    """Zero rows must read as "checked, found none" — not as a failed call."""
    run = TOOLS[tool][0](monkeypatch, 0)
    out = run(limit=None)
    assert out["items"] == []
    assert out["returned"] == 0
    assert out["truncated"] is False


# ---------------------------------------------------------------------------
# 2 & 3. Truncation is stated, never inferred
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", WITH_LIMIT)
def test_full_page_with_more_behind_it_is_flagged_truncated(monkeypatch, tool):
    run = TOOLS[tool][0](monkeypatch, 5)
    out = run(limit=3)
    assert out["returned"] == 3
    assert out["limit"] == 3
    assert out["truncated"] is True, f"{tool} hid a truncated page"
    assert out["hint"], f"{tool} truncated without telling the agent what to do"
    assert "limit" in out["hint"].lower()


@pytest.mark.parametrize("tool", ALL)
def test_short_result_is_complete_and_carries_no_hint(monkeypatch, tool):
    accepts_limit = TOOLS[tool][1]
    run = TOOLS[tool][0](monkeypatch, 2)
    out = run(limit=10 if accepts_limit else None)
    assert out["returned"] == 2
    assert out["truncated"] is False
    assert out["hint"] is None


# ---------------------------------------------------------------------------
# 4. The payoff of an honest total: a full page that IS the whole collection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(set(WITH_LIMIT) & set(WITH_REAL_TOTAL)))
def test_full_page_matching_a_real_total_is_not_flagged_truncated(monkeypatch, tool):
    """limit == total is the one case a real total disambiguates.

    Without ``total`` the envelope must assume a page filled to the limit may
    have more behind it. With it, the tool can say "that's all of them" — which
    is precisely the claim a model otherwise has to invent.
    """
    run = TOOLS[tool][0](monkeypatch, 3)
    out = run(limit=3)
    assert out["returned"] == 3
    assert out["total"] == 3, f"{tool} did not report the real total it knows"
    assert out["truncated"] is False, f"{tool} cried truncation on a complete result"
    assert out["hint"] is None


# ---------------------------------------------------------------------------
# The honesty counterpart: no invented totals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", WITH_REAL_TOTAL)
def test_real_total_is_reported_when_known(monkeypatch, tool):
    run = TOOLS[tool][0](monkeypatch, 4)
    out = run(limit=None)
    assert out["total"] == 4


@pytest.mark.parametrize("tool", WITHOUT_REAL_TOTAL)
def test_unknowable_total_stays_null_rather_than_guessed(monkeypatch, tool):
    """get_events and host_log_scan read a *window*, not a collection.

    vCenter's event collector applies its own bounds, and the log scan only
    reads the last N lines per log. Reporting ``total = len(rows)`` there would
    turn "here is what the window showed" into "here is everything", which is
    the confident-lie failure mode this envelope exists to remove.
    """
    run = TOOLS[tool][0](monkeypatch, 4)
    out = run(limit=None)
    assert out["returned"] == 4
    assert out["total"] is None, f"{tool} claimed a total it cannot know"
    assert out["truncated"] is False


# ---------------------------------------------------------------------------
# MCP surface — annotations and registration
# ---------------------------------------------------------------------------


def test_no_mcp_tool_is_annotated_as_a_bare_list():
    """A ``-> list[dict]`` annotation is the shape this work removed."""
    import vmware_monitor.mcp_server.server as srv

    offenders = []
    for name in ALL:
        fn = getattr(srv, name)
        ret = inspect.signature(fn).return_annotation
        if str(ret).startswith("list"):
            offenders.append(name)
    assert not offenders, f"tools still returning a bare list: {offenders}"


def test_all_twenty_tools_are_registered_with_fastmcp():
    import vmware_monitor.mcp_server.server as srv

    registered = {t.name for t in asyncio.run(srv.mcp.list_tools())}
    missing = sorted(set(ALL) - registered)
    assert not missing, f"envelope tools missing from the MCP surface: {missing}"


def test_mcp_error_payload_is_a_plain_dict(monkeypatch):
    """Errors share the dict shape, so a failure never reads as an empty page."""
    from vmware_monitor.mcp_server.server import _catch_tool_errors

    def boom() -> dict:
        raise RuntimeError("nope")

    out = _catch_tool_errors(boom)()
    assert isinstance(out, dict)
    assert "error" in out and "hint" in out
    assert "items" not in out, "an error must not masquerade as an empty result"
