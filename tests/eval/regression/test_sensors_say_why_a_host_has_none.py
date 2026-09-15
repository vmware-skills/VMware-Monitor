"""``health sensors`` says why a host reports no sensors.

2026-09-15, lab vCenter 8.0.3: ``health sensors`` printed "No hardware sensor
data available." in green, and stopped. Host events reported sensors every six
hours, so the data existed somewhere. ``health services --host 192.168.60.15``
showed the reason one command away: ``sfcbd-watchdog`` (CIM Server), policy
``on``, running ``no``. ESXi reads hardware sensors through its CIM providers,
so a stopped CIM Server leaves ``numericSensorInfo`` empty.

A green "no data" line reads as "nothing wrong". The answer now names each host
with no sensors and whether its CIM Server is running.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vmware_monitor.ops import health


def _svc_info(running: bool, policy: str = "on"):
    return SimpleNamespace(
        service=[
            SimpleNamespace(key="ntpd", running=True, policy="on"),
            SimpleNamespace(key="sfcbd-watchdog", running=running, policy=policy),
        ]
    )


def _sensor():
    return SimpleNamespace(
        name="CPU1 Temp", sensorType="temperature", currentReading=45, baseUnits="C",
        healthState=SimpleNamespace(key="green"),
    )


def _runtime(sensors):
    return SimpleNamespace(systemHealthInfo=SimpleNamespace(numericSensorInfo=list(sensors)))


def _install(monkeypatch, hosts, service_info):
    """``hosts`` is ``[(name, connectionState, runtime_or_None, svc_ref)]``."""
    rows = [
        (
            object(),
            {
                "name": name,
                "runtime.connectionState": state,
                "runtime.healthSystemRuntime": runtime,
                "configManager.serviceSystem": ref,
            },
        )
        for name, state, runtime, ref in hosts
    ]
    monkeypatch.setattr(health, "_collect", lambda si, types, paths: rows)
    monkeypatch.setattr(
        health,
        "_collect_objects",
        lambda si, refs, kind, paths: [(ref, {"serviceInfo": service_info.get(ref)}) for ref in refs],
    )


@pytest.mark.unit
def test_a_host_with_no_sensors_says_its_cim_server_is_stopped(monkeypatch):
    lab, other = object(), object()
    _install(
        monkeypatch,
        [
            ("192.168.60.15", "connected", _runtime([]), lab),
            ("192.168.60.56", "connected", _runtime([_sensor()]), other),
        ],
        {lab: _svc_info(running=False), other: _svc_info(running=True)},
    )
    out = health.get_host_hardware_status(None)

    assert [r["host"] for r in out["items"]] == ["192.168.60.56"]
    assert out["hosts_without_sensors"] == [
        {"host": "192.168.60.15", "cim_server_running": False, "cim_server_policy": "on"}
    ]
    note = out["sensors_note"]
    assert "192.168.60.15" in note and "sfcbd-watchdog" in note and "not running" in note


@pytest.mark.unit
def test_cim_running_with_no_sensors_points_at_the_hardware(monkeypatch):
    ref = object()
    _install(monkeypatch, [("esx-01", "connected", None, ref)], {ref: _svc_info(running=True)})
    out = health.get_host_hardware_status(None)
    assert out["hosts_without_sensors"][0]["cim_server_running"] is True
    assert "ipmi" in out["sensors_note"].lower()


@pytest.mark.unit
def test_an_unreadable_service_list_is_unknown_not_stopped(monkeypatch):
    ref = object()
    _install(monkeypatch, [("esx-01", "connected", _runtime([]), ref)], {ref: None})
    row = health.get_host_hardware_status(None)["hosts_without_sensors"][0]
    assert row["cim_server_running"] is None
    assert row["cim_server_policy"] is None


@pytest.mark.unit
def test_a_disconnected_host_is_not_reported_as_having_no_sensors(monkeypatch):
    ref = object()
    _install(monkeypatch, [("esx-gone", "notResponding", None, ref)], {ref: None})
    out = health.get_host_hardware_status(None)
    assert out["hosts_without_sensors"] == []
    assert out.get("sensors_note") is None


@pytest.mark.unit
def test_every_host_reporting_sensors_carries_no_note(monkeypatch):
    ref = object()
    _install(monkeypatch, [("esx-01", "connected", _runtime([_sensor()]), ref)], {ref: _svc_info(True)})
    out = health.get_host_hardware_status(None)
    assert out["hosts_without_sensors"] == []
    assert out.get("sensors_note") is None


@pytest.mark.unit
def test_cli_prints_why_instead_of_a_green_no_data_line(monkeypatch):
    from typer.testing import CliRunner

    from vmware_monitor import cli

    lab = object()
    _install(monkeypatch, [("192.168.60.15", "connected", _runtime([]), lab)], {lab: _svc_info(False)})
    monkeypatch.setattr(cli, "_get_connection", lambda *_a, **_k: (object(), None, "t"))
    monkeypatch.setenv("COLUMNS", "300")
    result = CliRunner().invoke(cli.app, ["health", "sensors"])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "192.168.60.15" in flat and "sfcbd-watchdog" in flat


# ── review, 2026-09-15 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_cim_server_state_is_one_batched_read(monkeypatch):
    refs = [object() for _ in range(5)]
    _install(monkeypatch, [(f"esx-{i}", "connected", _runtime([]), r) for i, r in enumerate(refs)],
             {r: _svc_info(False) for r in refs})
    calls = []
    real = health._collect_objects
    monkeypatch.setattr(health, "_collect_objects", lambda *a, **k: calls.append(a) or real(*a, **k))
    health.get_host_hardware_status(None)
    assert len(calls) == 1 and len(calls[0][1]) == 5


@pytest.mark.unit
def test_an_unknown_cim_state_is_named_in_the_note(monkeypatch):
    ref = object()
    _install(monkeypatch, [("esx-01", "connected", _runtime([]), ref)], {ref: None})
    note = health.get_host_hardware_status(None)["sensors_note"]
    assert "esx-01" in note and "could not be read" in note


@pytest.mark.unit
def test_a_failed_service_read_keeps_the_sensor_rows(monkeypatch):
    ok, bare = object(), object()
    _install(monkeypatch, [("esx-01", "connected", _runtime([_sensor()]), ok),
                           ("esx-02", "connected", _runtime([]), bare)], {})

    def boom(*_a, **_k):
        raise RuntimeError("serviceSystem unreachable")

    monkeypatch.setattr(health, "_collect_objects", boom)
    out = health.get_host_hardware_status(None)
    assert [r["host"] for r in out["items"]] == ["esx-01"]
    assert out["hosts_without_sensors"][0]["cim_server_running"] is None


@pytest.mark.unit
def test_a_fleet_without_sensors_is_one_capped_note(monkeypatch):
    refs = [object() for _ in range(60)]
    _install(monkeypatch, [(f"esx-{i:02d}", "connected", None, r) for i, r in enumerate(refs)],
             {r: _svc_info(False) for r in refs})
    out = health.get_host_hardware_status(None)
    assert len(out["hosts_without_sensors"]) == 50
    assert out["hosts_without_sensors_total"] == 60
    assert "and 50 more" in out["sensors_note"]
    assert out["sensors_note"].count("sfcbd-watchdog") == 1


@pytest.mark.unit
def test_a_service_with_no_policy_reports_none_not_the_string(monkeypatch):
    ref = object()
    info = SimpleNamespace(service=[SimpleNamespace(key="sfcbd-watchdog", running=False, policy=None)])
    _install(monkeypatch, [("esx-01", "connected", _runtime([]), ref)], {ref: info})
    assert health.get_host_hardware_status(None)["hosts_without_sensors"][0]["cim_server_policy"] is None
