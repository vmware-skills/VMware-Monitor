"""Does a triggered vCenter alarm's condition still hold right now?

vCenter keeps an alarm red until something resets it. For a state-based alarm
that is usually the state changing back — but not always: on a lab vCenter
8.0.3 "Host connection and power state" stayed red for eleven days on a host
that was connected and running VMs (2026-09-03 → 2026-09-14). An operator
reading the alarm list cannot tell that alarm from a live one.

This module re-evaluates the *state* parts of an alarm definition against the
entity's current properties and returns one of three verdicts:

* ``holds``   — the condition is true now; the alarm is live.
* ``cleared`` — the condition is false now; vCenter has not reset the alarm.
* ``unknown`` — it cannot be decided from here. Event- and metric-based parts
  are never guessed at: an event alarm stays until a matching event or a reset,
  and whether that is still "true" cannot be read back. A property that could
  not be read is unknown too, never cleared.

Pure: the caller fetches the definition and the property values (batched) and
passes them in.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyVmomi import vim

HOLDS = "holds"
CLEARED = "cleared"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Verdict:
    state: str
    note: str | None


def _children(expr: object) -> list:
    if isinstance(expr, (vim.alarm.AndAlarmExpression, vim.alarm.OrAlarmExpression)):
        return list(expr.expression or [])
    return []


def state_paths(expr: object) -> set[str]:
    """Every entity property path the state parts of ``expr`` compare."""
    if isinstance(expr, vim.alarm.StateAlarmExpression):
        return {expr.statePath} if expr.statePath else set()
    paths: set[str] = set()
    for child in _children(expr):
        paths |= state_paths(child)
    return paths


def _eval_state(expr, level: str, entity: object, values: dict) -> tuple[bool | None, list, list]:
    wanted = getattr(expr, "type", None)
    if wanted is not None and not isinstance(entity, wanted):
        return (
            None,
            [
                f"its state condition is defined for {wanted.__name__} but the alarm is on "
                f"{type(entity).__name__}"
            ],
            [],
        )
    thresholds = [expr.red] if level == "red" else [expr.yellow, expr.red]
    thresholds = [str(t) for t in thresholds if t is not None]
    if not thresholds:
        return None, [f"its state condition sets no {level} threshold"], []
    path = expr.statePath
    if path not in values:
        return None, [f"{path} could not be read on the object"], []
    value = str(values[path])
    if expr.operator == "isEqual":
        if any(value == t for t in thresholds):
            return True, [], []
        return False, [], [f"{path} is {value}, not {' or '.join(thresholds)}"]
    if expr.operator == "isUnequal":
        if any(value != t for t in thresholds):
            return True, [], []
        return False, [], [f"{path} is {value}"]
    return None, [f"its state operator {expr.operator!r} is not one this tool evaluates"], []


def _eval(expr, level: str, entity: object, values: dict) -> tuple[bool | None, list, list]:
    """``(truth, unknown_reasons, false_descriptions)``; truth None = undecidable."""
    if expr is None:
        return None, ["the alarm definition could not be read"], []
    if isinstance(expr, vim.alarm.StateAlarmExpression):
        return _eval_state(expr, level, entity, values)
    if isinstance(expr, (vim.alarm.AndAlarmExpression, vim.alarm.OrAlarmExpression)):
        parts = [_eval(child, level, entity, values) for child in _children(expr)]
        truths = [p[0] for p in parts]
        reasons = [r for p in parts for r in p[1]]
        false_bits = [d for p in parts if p[0] is False for d in p[2]]
        if not parts:
            return None, ["the alarm definition has no conditions"], []
        if isinstance(expr, vim.alarm.AndAlarmExpression):
            if False in truths:
                return False, [], false_bits
            return (True, [], []) if all(t is True for t in truths) else (None, reasons, [])
        if True in truths:
            return True, [], []
        return (False, [], false_bits) if all(t is False for t in truths) else (None, reasons, [])
    if isinstance(expr, vim.alarm.EventAlarmExpression):
        event = getattr(expr, "eventTypeId", None) or getattr(
            getattr(expr, "eventType", None), "__name__", "an event"
        )
        return (
            None,
            [
                f"it is raised by an event ({event}); vCenter keeps it until a matching event "
                f"clears it or someone resets it, and whether it still applies cannot be read back"
            ],
            [],
        )
    if isinstance(expr, vim.alarm.MetricAlarmExpression):
        return None, ["it is based on a performance metric, which this tool does not re-sample"], []
    return None, [f"its {type(expr).__name__} condition is not re-evaluated here"], []


def evaluate(expr: object, level: str, entity: object, values: dict) -> Verdict:
    """Verdict for one triggered alarm.

    Args:
        expr: The alarm definition's ``info.expression``.
        level: The alarm's ``overallStatus`` — ``"red"`` or ``"yellow"``.
        entity: The entity the alarm triggered on (type-checked against the
            state parts' declared type).
        values: ``{property path: current value}`` for that entity.
    """
    truth, reasons, false_bits = _eval(expr, str(level), entity, values)
    if truth is True:
        return Verdict(HOLDS, None)
    if truth is False:
        return Verdict(
            CLEARED,
            "Still triggered, but its condition no longer holds: "
            + "; ".join(false_bits)
            + ". vCenter has not reset this alarm — confirm on the object, then reset "
            "it to green (vSphere Client, or vmware-aiops reset_vcenter_alarm).",
        )
    return Verdict(
        UNKNOWN, "Not re-evaluated: " + (reasons[0] if reasons else "no reason recorded") + "."
    )
