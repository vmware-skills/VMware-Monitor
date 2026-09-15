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

One event-raised condition is decided anyway, because the skill reads the
evidence that settles it: an expired-vCenter-license alarm is checked against the
license this vCenter is assigned (``LicenseEvidence``). On the lab (2026-09-15)
"Expired vCenter Server license" had been red since 2026-06-01 while the vCenter
ran on a license valid to 2027-06-28. Without positive evidence it stays unknown.

Pure: the caller fetches the definition, the property values (batched) and any
evidence, and passes them in.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyVmomi import vim
from vmware_policy import sanitize

HOLDS = "holds"
CLEARED = "cleared"
UNKNOWN = "unknown"

#: Event-raised alarms decidable from license assignments: event id → asset kind.
#: Observed on vCenter 8.0.3 (systemName ``alarm.LicenseExpiredVc``). Only ids
#: seen on a real definition are listed — a guessed id would never match, or
#: worse, match something else.
LICENSE_EXPIRED_EVENTS = {"com.vmware.license.VcLicenseExpiredEvent": "vcenter"}

#: Events vCenter raises about its own appliance, observed on 8.0.3 (2026-09-15)
#: in the definitions of "Memory Exhaustion on <short hostname>" and "Root user
#: password expired.", both attached to the inventory root ("Datacenters").
APPLIANCE_EVENTS = frozenset(
    {
        "vim.event.ResourceExhaustionStatusChangedEvent",
        "com.vmware.vc.system.RootPasswordExpiredEvent",
    }
)

#: The comparison a vCenter appliance-health definition uses to pin the source.
_SOURCE_HOST_ATTRIBUTE = "_sourcehost_"


@dataclass(frozen=True)
class Verdict:
    state: str
    note: str | None


@dataclass(frozen=True)
class LicenseEvidence:
    """What decides an expired-vCenter-license alarm.

    ``vcenter_uuid`` is this vCenter's ``about.instanceUuid`` (the assignment's
    ``entityId``); ``assignments`` are ``infra_health`` assignment rows, ``None``
    when unreadable, with ``why`` saying so.
    """

    vcenter_uuid: str | None
    assignments: list[dict] | None
    why: str | None = None


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


def _event_expressions(expr: object) -> list:
    if isinstance(expr, vim.alarm.EventAlarmExpression):
        return [expr]
    return [e for child in _children(expr) for e in _event_expressions(child)]


def event_type_ids(expr: object) -> set[str]:
    """Every event type id the event parts of ``expr`` are raised by."""
    return {str(e.eventTypeId) for e in _event_expressions(expr) if e.eventTypeId}


def appliance_label(expr: object) -> str | None:
    """"vCenter appliance <address>" for an alarm about the vCenter appliance itself.

    Recognised from the definition's event types, never from the alarm's name
    (vCenter names the memory alarm after the appliance's short hostname — "on
    192" for 192.168.60.16). The address comes from the definition's
    ``_sourcehost_`` comparison when it has one. ``None`` for any other alarm.
    """
    events = _event_expressions(expr)
    if not any(str(e.eventTypeId or "") in APPLIANCE_EVENTS for e in events):
        return None
    for event in events:
        for comparison in event.comparisons or []:
            if comparison.attributeName == _SOURCE_HOST_ATTRIBUTE and comparison.value:
                return f"vCenter appliance {sanitize(str(comparison.value))}"
    return "vCenter appliance"


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


def _has_expiry_evidence(row: dict) -> bool:
    """A readable expiry date, or a perpetual license that is not evaluation mode."""
    if row.get("expiration") not in (None, "never"):
        return True
    edition = row.get("edition_key")
    return edition is not None and "eval" not in str(edition).lower()


def _eval_license(expr, licenses: LicenseEvidence) -> tuple[bool | None, list, list]:
    event = expr.eventTypeId
    if licenses.assignments is None:
        why = f": {licenses.why}" if licenses.why else ""
        return (
            None,
            [f"it is raised by an event ({event}) and the license assignments that would "
             f"decide it could not be read{why}"],
            [],
        )
    if not licenses.vcenter_uuid:
        return (
            None,
            [f"it is raised by an event ({event}) and this vCenter's instance UUID could not "
             f"be read, so its own license assignment cannot be picked out"],
            [],
        )
    rows = [a for a in licenses.assignments if a.get("asset_id") == licenses.vcenter_uuid]
    if not rows:
        return (
            None,
            [f"it is raised by an event ({event}) and no license assignment was returned for "
             f"this vCenter"],
            [],
        )
    if any(a.get("expired") is True for a in rows):
        return True, [], []
    if all(a.get("expired") is False and _has_expiry_evidence(a) for a in rows):
        return (
            False,
            [],
            [
                f"this vCenter is assigned '{a.get('license_name')}', "
                + (
                    f"valid until {a['expiration']}"
                    if a.get("expiration") not in (None, "never")
                    else "which does not expire"
                )
                for a in rows
            ],
        )
    return (
        None,
        [f"it is raised by an event ({event}) and the expiry of this vCenter's license could "
         f"not be established"],
        [],
    )


def _eval(
    expr, level: str, entity: object, values: dict, licenses: LicenseEvidence | None = None
) -> tuple[bool | None, list, list]:
    """``(truth, unknown_reasons, false_descriptions)``; truth None = undecidable."""
    if expr is None:
        return None, ["the alarm definition could not be read"], []
    if isinstance(expr, vim.alarm.StateAlarmExpression):
        return _eval_state(expr, level, entity, values)
    if isinstance(expr, (vim.alarm.AndAlarmExpression, vim.alarm.OrAlarmExpression)):
        parts = [_eval(child, level, entity, values, licenses) for child in _children(expr)]
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
        if (
            licenses is not None
            and str(expr.eventTypeId or "") in LICENSE_EXPIRED_EVENTS
            and str(getattr(expr, "status", "")) in ("red", "yellow")
        ):
            return _eval_license(expr, licenses)
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


def evaluate(
    expr: object,
    level: str,
    entity: object,
    values: dict,
    licenses: LicenseEvidence | None = None,
) -> Verdict:
    """Verdict for one triggered alarm.

    Args:
        expr: The alarm definition's ``info.expression``.
        level: The alarm's ``overallStatus`` — ``"red"`` or ``"yellow"``.
        entity: The entity the alarm triggered on (type-checked against the
            state parts' declared type).
        values: ``{property path: current value}`` for that entity.
        licenses: License evidence for expired-vCenter-license conditions; omit
            it and those stay ``unknown``.
    """
    truth, reasons, false_bits = _eval(expr, str(level), entity, values, licenses)
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
