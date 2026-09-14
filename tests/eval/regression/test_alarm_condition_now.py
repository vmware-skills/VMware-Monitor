"""Regression — a triggered alarm says whether its condition still holds.

Found 2026-09-14 on a lab vCenter 8.0.3. "Host connection and power state" had
been red on 192.168.60.15 since 2026-09-03 while the host was connected,
powered on and running nine VMs. Its definition is

    AND(HostSystem.runtime.connectionState isEqual notResponding,
        HostSystem.runtime.powerState      isUnequal standBy)

and the first half has been false for days: vCenter never reset the alarm when
the host came back. `get_alarms` / `health alarms` could not say so — the only
way to find out was hand-written pyVmomi.

Each alarm row now carries ``condition_now``: ``holds`` / ``cleared`` /
``unknown``. Only state-based parts of a definition are re-evaluated against
the entity's current properties; an event- or metric-triggered part is never
guessed at, and makes the verdict ``unknown`` unless the state parts decide it
on their own. The CLI shows who acknowledged an alarm and when, which the MCP
row already carried only as a bool.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vmware_monitor.ops import alarm_condition as ac
from vmware_monitor.ops import health


def _state(path, op, red=None, yellow=None, type_=vim.HostSystem):
    return vim.alarm.StateAlarmExpression(
        type=type_, statePath=path, operator=op, red=red, yellow=yellow
    )


def _event(event_type_id="com.vmware.vc.host.TPMAttestationFailedEvent"):
    return vim.alarm.EventAlarmExpression(
        eventType=vim.event.EventEx, eventTypeId=event_type_id, status="red"
    )


_CONNECTION = vim.alarm.AndAlarmExpression(
    expression=[
        _state("runtime.connectionState", "isEqual", red="notResponding"),
        _state("runtime.powerState", "isUnequal", red="standBy"),
    ]
)
_HOST = vim.HostSystem("host-15")


@pytest.mark.unit
def test_the_lab_alarm_is_reported_as_cleared():
    verdict = ac.evaluate(
        _CONNECTION,
        "red",
        _HOST,
        {"runtime.connectionState": "connected", "runtime.powerState": "poweredOn"},
    )
    assert verdict.state == ac.CLEARED
    assert "runtime.connectionState" in verdict.note and "connected" in verdict.note
    assert "reset" in verdict.note.lower()


@pytest.mark.unit
def test_a_condition_that_still_holds_says_so():
    verdict = ac.evaluate(
        _CONNECTION,
        "red",
        _HOST,
        {"runtime.connectionState": "notResponding", "runtime.powerState": "poweredOn"},
    )
    assert verdict.state == ac.HOLDS and verdict.note is None


@pytest.mark.unit
def test_an_unreadable_property_is_unknown_not_cleared():
    verdict = ac.evaluate(_CONNECTION, "red", _HOST, {"runtime.powerState": "poweredOn"})
    assert verdict.state == ac.UNKNOWN
    assert "runtime.connectionState" in verdict.note


@pytest.mark.unit
def test_an_event_triggered_alarm_is_never_guessed():
    verdict = ac.evaluate(vim.alarm.OrAlarmExpression(expression=[_event()]), "red", _HOST, {})
    assert verdict.state == ac.UNKNOWN
    assert "TPMAttestationFailedEvent" in verdict.note, "the note must name the event"
    assert "reset" in verdict.note


@pytest.mark.unit
def test_mixed_definitions_decide_only_when_the_state_part_is_decisive():
    true_state = _state("runtime.powerState", "isEqual", red="poweredOn")
    false_state = _state("runtime.powerState", "isEqual", red="standBy")
    values = {"runtime.powerState": "poweredOn"}
    or_, and_ = vim.alarm.OrAlarmExpression, vim.alarm.AndAlarmExpression
    assert (
        ac.evaluate(or_(expression=[true_state, _event()]), "red", _HOST, values).state == ac.HOLDS
    )
    assert (
        ac.evaluate(or_(expression=[false_state, _event()]), "red", _HOST, values).state
        == ac.UNKNOWN
    )
    assert (
        ac.evaluate(and_(expression=[false_state, _event()]), "red", _HOST, values).state
        == ac.CLEARED
    )
    assert (
        ac.evaluate(and_(expression=[true_state, _event()]), "red", _HOST, values).state
        == ac.UNKNOWN
    )


@pytest.mark.unit
def test_a_yellow_alarm_is_judged_against_its_yellow_threshold():
    expr = _state("runtime.connectionState", "isEqual", red="notResponding", yellow="disconnected")
    assert (
        ac.evaluate(expr, "yellow", _HOST, {"runtime.connectionState": "disconnected"}).state
        == ac.HOLDS
    )
    assert (
        ac.evaluate(expr, "yellow", _HOST, {"runtime.connectionState": "connected"}).state
        == ac.CLEARED
    )


@pytest.mark.unit
def test_a_definition_for_another_entity_type_is_unknown():
    expr = _state("runtime.powerState", "isEqual", red="poweredOff", type_=vim.VirtualMachine)
    assert ac.evaluate(expr, "red", _HOST, {"runtime.powerState": "poweredOff"}).state == ac.UNKNOWN


@pytest.mark.unit
def test_state_paths_are_collected_from_the_whole_tree():
    tree = vim.alarm.OrAlarmExpression(expression=[_CONNECTION, _event()])
    assert ac.state_paths(tree) == {"runtime.connectionState", "runtime.powerState"}


# ── get_active_alarms ───────────────────────────────────────────────────────


def _alarm_rows(monkeypatch, *, host_values):
    connection = vim.alarm.Alarm("alarm-conn")
    tpm = vim.alarm.Alarm("alarm-tpm")
    acked_at = datetime(2026, 3, 3, 2, 38, tzinfo=timezone.utc)
    states = [
        SimpleNamespace(
            overallStatus="red",
            entity=_HOST,
            alarm=connection,
            time="2026-09-03 14:08",
            acknowledged=False,
            acknowledgedByUser=None,
            acknowledgedTime=None,
        ),
        SimpleNamespace(
            overallStatus="red",
            entity=_HOST,
            alarm=tpm,
            time="2026-02-26 14:58",
            acknowledged=True,
            acknowledgedByUser="VSPHERE.LOCAL\\Administrator",
            acknowledgedTime=acked_at,
        ),
    ]
    calls = []

    def fake_collect(si, obj_type, paths):
        if obj_type[0] is vim.HostSystem:
            return [(_HOST, {"name": "192.168.60.15", "triggeredAlarmState": states})]
        return []

    def fake_collect_objects(si, objs, obj_type, paths):
        calls.append((obj_type, tuple(sorted(paths)), len(set(objs))))
        if obj_type is vim.alarm.Alarm:
            info = {
                connection: ("Host connection and power state", _CONNECTION),
                tpm: (
                    "Host TPM attestation alarm",
                    vim.alarm.OrAlarmExpression(expression=[_event()]),
                ),
            }
            return [
                (ref, {"info.name": info[ref][0], "info.expression": info[ref][1]})
                for ref in set(objs)
            ]
        if obj_type is vim.HostSystem:
            return [(_HOST, dict(host_values))]
        return []

    monkeypatch.setattr(health, "_collect", fake_collect)
    monkeypatch.setattr(health, "_collect_objects", fake_collect_objects)
    si = SimpleNamespace(
        RetrieveContent=lambda: SimpleNamespace(rootFolder=SimpleNamespace(triggeredAlarmState=[]))
    )
    return health.get_active_alarms(si), calls


@pytest.mark.unit
def test_get_alarms_marks_the_stale_alarm_and_says_how_many(monkeypatch):
    out, calls = _alarm_rows(
        monkeypatch,
        host_values={"runtime.connectionState": "connected", "runtime.powerState": "poweredOn"},
    )
    rows = {r["alarm_name"]: r for r in out["items"]}
    conn, tpm = rows["Host connection and power state"], rows["Host TPM attestation alarm"]
    assert conn["condition_now"] == "cleared" and "connected" in conn["condition_note"]
    assert conn["suggested_actions"][0].startswith("vmware-aiops: reset_vcenter_alarm")
    assert tpm["condition_now"] == "unknown"
    assert tpm["acknowledged"] is True
    assert tpm["acknowledged_by"] == "VSPHERE.LOCAL\\Administrator"
    assert tpm["acknowledged_at"].startswith("2026-03-03")
    assert conn["acknowledged_by"] is None and conn["acknowledged_at"] is None
    assert out["stale_alarms"] == 1 and "1 alarm" in out["stale_note"]
    # One batched read per managed-object type, not one per alarm.
    assert [c[0] for c in calls].count(vim.alarm.Alarm) == 1
    assert [c[0] for c in calls].count(vim.HostSystem) == 1


@pytest.mark.unit
def test_no_stale_note_when_every_condition_holds(monkeypatch):
    out, _ = _alarm_rows(
        monkeypatch,
        host_values={"runtime.connectionState": "notResponding", "runtime.powerState": "poweredOn"},
    )
    assert out["stale_alarms"] == 0 and "stale_note" not in out


@pytest.mark.unit
def test_the_cli_shows_acknowledgement_and_the_verdict(monkeypatch):
    from rich.console import Console
    from typer.testing import CliRunner

    from vmware_monitor import cli

    out, _ = _alarm_rows(
        monkeypatch,
        host_values={"runtime.connectionState": "connected", "runtime.powerState": "poweredOn"},
    )
    monkeypatch.setattr(health, "get_active_alarms", lambda si, limit=None: out)
    monkeypatch.setattr(
        cli, "_get_connection", lambda target, config: (object(), None, "home-vcenter")
    )
    monkeypatch.setattr(cli, "console", Console(width=220, color_system=None))
    result = CliRunner().invoke(cli.app, ["health", "alarms"])
    assert result.exit_code == 0, result.output
    assert "cleared" in result.output and "Administrator" in result.output
    assert "no longer holds" in result.output
