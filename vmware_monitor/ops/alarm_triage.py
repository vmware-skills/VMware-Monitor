"""Alarm triage shared by the alarm list and the health summary (read-only).

Two things live here:

* ``license_evidence`` — reads the license assignments that decide an
  expired-vCenter-license alarm, only when such an alarm is present (one
  ``QueryAssignedLicenses`` call).
* ``build_alarm_issues`` — turns the summary's stashed triggered alarms into
  ranked issues that say whether the condition still holds, name the vCenter
  appliance when that is what the alarm is about, and carry how long ago an
  alarm was acknowledged. ``staleness_rank`` moves only alarms whose condition
  is positively known to be gone (``cleared``) below live problems; an alarm
  that cannot be re-checked keeps its severity rank, acknowledged or not.

Found 2026-09-15 on a lab vCenter 8.0.3: ``attention`` ranked a months-old
"Expired vCenter Server license" (the vCenter's license valid to 2027-06-28)
beside live problems, and labelled the appliance's memory alarm "Datacenters".
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timezone

from pyVmomi import vim
from vmware_policy import sanitize

from vmware_monitor.ops import alarm_condition

_log = logging.getLogger(__name__)

#: An alarm whose condition cannot be re-checked and that was acknowledged at
#: least this many days ago says so in its ``detail``. Information only: it does
#: not change the alarm's rank (unknown is not a verdict).
ACKNOWLEDGED_STALE_DAYS = 7


def needs_license_evidence(expressions: Iterable) -> bool:
    """True when any of these definitions is raised by a license-expiry event."""
    wanted = set(alarm_condition.LICENSE_EXPIRED_EVENTS)
    return any(alarm_condition.event_type_ids(e) & wanted for e in expressions)


def license_evidence(
    content: object, expressions: Iterable
) -> alarm_condition.LicenseEvidence | None:
    """License evidence for these alarm definitions, or ``None`` if none needs it."""
    if not needs_license_evidence(expressions):
        return None
    from vmware_monitor.ops.infra_health import _license_assignments

    try:
        rows, why = _license_assignments(content)
    except Exception as exc:  # noqa: BLE001 — unreadable evidence leaves the verdict unknown
        _log.warning("Could not read license assignments: %s", exc)
        rows, why = None, type(exc).__name__
    try:
        uuid = str(content.about.instanceUuid or "") or None
    except Exception:  # noqa: BLE001 — without it, this vCenter's row cannot be picked out
        uuid = None
    return alarm_condition.LicenseEvidence(vcenter_uuid=uuid, assignments=rows, why=why)


def _content_or_none(si: object) -> object | None:
    try:
        return si.content
    except Exception:  # noqa: BLE001 — unreadable content leaves license verdicts unknown
        return None


def acknowledged_days(state: object, now: datetime) -> int | None:
    """Whole days since the alarm was acknowledged; ``None`` if not acknowledged or unknown."""
    if not getattr(state, "acknowledged", False):
        return None
    when = getattr(state, "acknowledgedTime", None)
    if not isinstance(when, datetime):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, (now - when).days)


def staleness_rank(issue: dict) -> int:
    """1 when the alarm's condition is positively known to be gone, else 0.

    Only ``cleared`` — backed by evidence read now — may sort below live issues.
    An ``unknown`` alarm keeps its severity rank however long ago it was
    acknowledged: an acknowledgement is a person's note, not evidence that the
    condition went away. Review 2026-09-15: demoting those let a CRITICAL "Host
    TPM attestation alarm" (unknown, acknowledged 196 days ago) rank below a
    warning, and drop out of ``top_issues`` behind ten of them.
    """
    return 1 if issue.get("condition_now") == alarm_condition.CLEARED else 0


def _detail(name: str, verdict_state: str, days: int | None) -> str:
    if verdict_state == alarm_condition.CLEARED:
        return f"{name} — condition no longer holds; vCenter has not reset it"
    if verdict_state == alarm_condition.UNKNOWN and days is not None and (
        days >= ACKNOWLEDGED_STALE_DAYS
    ):
        return f"{name} — acknowledged {days} days ago; condition not re-checked"
    return name


def build_alarm_issues(
    si: object,
    raw_alarms: list,
    collect_objects: Callable,
    drilldown: str,
) -> list[dict]:
    """Issues for ``(alarm_ref, severity, scope_type, scope_name, cluster, state)`` tuples.

    One batched read for every alarm's name and definition, one per entity type
    for state-based conditions, and one license read only when a license alarm
    is present.
    """
    from vmware_monitor.ops.health import _entity_state_values, _ref_key

    refs = [r[0] for r in raw_alarms if r[0] is not None]
    rows = collect_objects(si, refs, vim.alarm.Alarm, ["info.name", "info.expression"])
    definitions = {
        _ref_key(ref): (props.get("info.name"), props.get("info.expression"))
        for ref, props in rows
    }
    stateful = [
        r[5]
        for r in raw_alarms
        if r[0] is not None
        and alarm_condition.state_paths(definitions.get(_ref_key(r[0]), (None, None))[1])
    ]
    values = _entity_state_values(si, stateful, definitions) if stateful else {}
    expressions = [d[1] for d in definitions.values()]
    # si.content is only touched when a license alarm needs it, and a vCenter that
    # will not answer for it must not take the summary down — the summary is run
    # precisely when something is wrong.
    licenses = (
        license_evidence(_content_or_none(si), expressions)
        if needs_license_evidence(expressions)
        else None
    )
    now = datetime.now(timezone.utc)

    issues: list[dict] = []
    for ref, sev, scope_type, scope_name, cluster_name, state in raw_alarms:
        name, expr = (
            definitions.get(_ref_key(ref), (None, None)) if ref is not None else (None, None)
        )
        entity = getattr(state, "entity", None)
        verdict = alarm_condition.evaluate(
            expr,
            str(getattr(state, "overallStatus", "")),
            entity,
            values.get(_ref_key(entity), {}) if entity is not None else {},
            licenses=licenses,
        )
        label = alarm_condition.appliance_label(expr) if scope_type == "vcenter" else None
        days = acknowledged_days(state, now)
        issues.append(
            {
                "severity": sev,
                "kind": "alarm",
                "object": label or scope_name,
                "scope": scope_type,
                "cluster": cluster_name,
                "detail": _detail(sanitize(str(name or "alarm")), verdict.state, days),
                "condition_now": verdict.state,
                "acknowledged_days": days,
                "drilldown": drilldown,
            }
        )
    return issues
