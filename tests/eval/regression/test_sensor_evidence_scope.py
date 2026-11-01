"""An aggregate memory sensor is evidence of a reported condition, not a DIMM diagnosis.

Regression for vmware-skills organization discussion #43. The reported local
model turned a red aggregate sensor into a specific DIMM/ECC failure and a
prediction of imminent host failure, none of which the tool had measured.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from vmware_monitor.ops import health


@pytest.mark.unit
def test_red_aggregate_memory_sensor_exposes_its_evidence_boundary(monkeypatch):
    sensor = SimpleNamespace(
        name="Memory Red",
        sensorType="memory",
        currentReading=1,
        baseUnits="state",
        healthState=SimpleNamespace(key="red"),
    )
    runtime = SimpleNamespace(
        systemHealthInfo=SimpleNamespace(numericSensorInfo=[sensor])
    )
    monkeypatch.setattr(
        health,
        "_collect",
        lambda *_args: [
            (
                object(),
                {
                    "name": "lab-esx",
                    "runtime.connectionState": "connected",
                    "runtime.healthSystemRuntime": runtime,
                    "configManager.serviceSystem": object(),
                },
            )
        ],
    )

    result = health.get_host_hardware_status(None)

    assert result["items"][0]["status"] == "red"
    assert result["items"][0]["sensor_name"] == "Memory Red"
    note = result["interpretation_note"].lower()
    assert "aggregate" in note and "dimm" in note and "ecc" in note
    assert "does not" in note and "predict" in note
    assert "component_identified" not in result["items"][0]


@pytest.mark.unit
def test_mcp_description_tells_the_agent_to_keep_status_and_diagnosis_apart():
    from vmware_monitor.mcp_server.server import get_host_sensors

    description = get_host_sensors.__doc__ or ""
    assert "interpretation_note" in description
    assert "DIMM/slot" in description


@pytest.mark.unit
def test_mcp_result_delivers_the_note_for_a_red_aggregate_sensor(monkeypatch):
    import vmware_monitor.mcp_server.server as srv

    sensor = SimpleNamespace(
        name="Memory Red", sensorType="memory", currentReading=1,
        baseUnits="state", healthState=SimpleNamespace(key="red"),
    )
    runtime = SimpleNamespace(
        systemHealthInfo=SimpleNamespace(numericSensorInfo=[sensor])
    )
    monkeypatch.setattr(srv, "_get_connection", lambda _target: None)
    monkeypatch.setattr(
        health, "_collect",
        lambda *_args: [(object(), {
            "name": "lab-esx", "runtime.connectionState": "connected",
            "runtime.healthSystemRuntime": runtime,
            "configManager.serviceSystem": object(),
        })],
    )

    result = asyncio.run(srv.mcp.call_tool("get_host_sensors", {"target": "lab", "limit": 1}))

    assert "interpretation_note" in str(result)
    assert "A red aggregate memory sensor alone does not identify a DIMM" in str(result)
