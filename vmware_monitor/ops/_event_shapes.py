"""Event catalogue normalization and severity evidence; no remote calls."""

from __future__ import annotations


from datetime import datetime, timezone

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    pass


CRITICAL_EVENTS = {
    "VmFailedToPowerOnEvent",
    "HostConnectionLostEvent",
    "HostShutdownEvent",
    "VmDiskFailedEvent",
    "DasHostFailedEvent",
    "DatastoreRemovedOnHostEvent",
}


WARNING_EVENTS = {
    "VmFailoverFailed",
    "DrsVmMigratedEvent",
    "DrsSoftRuleViolationEvent",
    "VmFailedToRebootGuestEvent",
    "DVPortgroupReconfiguredEvent",
    "VmGuestShutdownEvent",
    "HostIpChangedEvent",
    "BadUsernameSessionEvent",
}


INFO_EVENTS = {
    "VmPoweredOnEvent",
    "VmPoweredOffEvent",
    "VmMigratedEvent",
    "VmReconfiguredEvent",
    "UserLoginSessionEvent",
    "UserLogoutSessionEvent",
    "VmCreatedEvent",
    "VmRemovedEvent",
    "VmClonedEvent",
}


_VIM_EVENT_PREFIX = "vim.event."


def short_event_type(name: str) -> str:
    """An event type as the override sets spell it.

    A real pyVmomi event's class is named ``vim.event.HostConnectionLostEvent``;
    CRITICAL_EVENTS, WARNING_EVENTS, INFO_EVENTS, ROUTINE_EVENT_TYPES and the
    suggestion map say ``HostConnectionLostEvent``. Comparing the two directly
    matched nothing on a live vCenter (found 2026-09-14): every timeline row was
    "info" and the daemon's event scan never found a critical event. Extended ids
    (``esx.problem.*``, ``com.vmware.*``) are returned unchanged.
    """
    text = str(name)
    return text[len(_VIM_EVENT_PREFIX) :] if text.startswith(_VIM_EVENT_PREFIX) else text


def parse_event_time(value: str, name: str) -> datetime:
    """An ISO 8601 time as an aware UTC datetime; a time with no zone is UTC.

    Raises a ``ValueError`` that says what was expected, so a caller passing
    "yesterday" learns the format instead of getting a parser trace.
    """
    text = str(value).strip()
    # Python 3.10 (still allowed by requires-python) cannot parse a trailing "Z";
    # 3.11+ can, which is why a test run on 3.12 cannot tell this line is needed.
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{name} must be an ISO 8601 time such as 2026-09-03T12:00:00Z (got {value!r})."
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_VC_CATEGORY_RANK = {
    "error": "critical",
    "warning": "warning",
    "info": "info",
    "user": "info",
}


def _event_catalogue(event_mgr: object) -> dict[str, str]:
    """vCenter's ``event type key -> category`` map, minus the ambiguous keys.

    ``EventManager.description.eventInfo`` is vCenter telling us how it ranks
    every event type it knows. On a real VCF 9.1 vCenter it publishes 2328
    entries -- and they collapse to 443 distinct keys, because pyVmomi's VMODL
    declares ``EventDetail.key`` as a *type* and the thousands of distinct
    ``esx.problem.*`` types all arrive as the one class ``vim.event.EventEx``.

    The 1885 lost entries were not merely lost. Writing them into the same dict
    key meant the last one won, so every ``esx.problem.*`` event took the
    category of whichever description happened to be parsed last -- a confident
    wrong rank, which is worse than no rank. A key whose entries disagree is
    therefore dropped: ranking falls through to the event's own ``severity``,
    then to the name-prefix rule, then to "unknown", each of which says what it
    is (see ``severity_source``).

    Returns ``{}`` rather than raising when the property is missing (older or
    permission-restricted vCenter): the overrides still work, and losing the
    catalogue must not lose the events.
    """
    return _catalogue_and_coverage(event_mgr)[0]


def _catalogue_and_coverage(
    event_mgr: object,
) -> tuple[dict[str, str], dict[str, int]]:
    """The catalogue plus what had to be discarded to build it.

    The counts are reported to the caller rather than kept private: "vCenter
    described 2328 event types and we could use 443 of them" is the difference
    between a rank we do not have and a rank we silently guessed.
    """
    info = getattr(getattr(event_mgr, "description", None), "eventInfo", None)
    seen: dict[str, set[str]] = {}
    described = 0
    for detail in info or []:
        key = _catalogue_entry_key(detail)
        category = getattr(detail, "category", None)
        if not key or not category:
            continue
        described += 1
        seen.setdefault(key, set()).add(str(category).lower())
    catalogue = {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}
    coverage = {
        "described": described,
        "usable": len(catalogue),
        "ambiguous": len(seen) - len(catalogue),
    }
    return catalogue, coverage


def _catalogue_entry_key(detail: object) -> str:
    """The event type id this catalogue entry describes.

    Two shapes arrive in ``EventManager.description.eventInfo``, and they must be
    read differently. Measured on a live vCenter 8.0.3, 2196 entries:

    * 441 classic VMODL events. ``key`` is the pyVmomi class
      (``vim.event.AccountCreatedEvent``) and ``fullFormat`` is prose —
      "Account {spec.id} was created on host {host.name}". **None** of these
      contains a ``|``.
    * 1755 extended events. ``key`` is the single class ``vim.event.EventEx`` or
      ``vim.event.ExtendedEvent`` for *all* of them, so reading the class name
      collapsed 1755 distinct descriptions onto 2 keys — 80% of everything
      vCenter published about itself, discarded. **All** of these put the real
      id in ``fullFormat`` ahead of a ``|``::

          com.vmware.cis.CreatePermission|Permission created for user {User}...
          esx.problem.scsi.device.io.latency.high|Device {1} performance...

      which is exactly the value ``Event.eventTypeId`` carries, so the catalogue
      and :func:`_event_key` finally speak the same language.

    The ``|`` split is unambiguous because the two populations do not overlap:
    0 of 441 classic entries have one, 1755 of 1755 extended entries do, the
    1755 extracted ids are distinct (zero collisions) and none contains a space.

    ``key`` remains the fallback: an entry with no parseable id keeps the old
    behaviour rather than being dropped.

    Verified on 8.0.3 only. That the ids match what live events report was NOT
    confirmed — the lab produced 10 events, none of them extended — so the
    catalogue's own consistency is the evidence here, not a round trip.
    """
    full = getattr(detail, "fullFormat", None)
    if isinstance(full, str) and "|" in full:
        candidate = full.split("|", 1)[0].strip()
        # An id, not a sentence: vCenter's ids are dotted tokens. A prose
        # fullFormat that happens to contain a pipe must not become a key.
        if candidate and " " not in candidate:
            return candidate

    key = getattr(detail, "key", None)
    if key is None:
        return ""
    name = getattr(key, "__name__", None)
    return str(name) if name else str(key)


def _event_key(event: object) -> str:
    """What this event actually is.

    Modern vSphere emits most events as ``EventEx``/``ExtendedEvent``, where the
    Python class name is the literal string "EventEx" for all of them and the
    identity lives in ``eventTypeId`` (``esx.problem.scsi.device.io.latency.high``
    and the like). Keying on the class name collapsed every one of them into a
    single unmatched bucket — and reported "EventEx" to the user, which is not a
    thing anyone can look up.
    """
    return str(getattr(event, "eventTypeId", None) or type(event).__name__)


_PROBLEM_PREFIX = "esx.problem."


def _event_severity(event: object, key: str, catalogue: dict[str, str]) -> str:
    """Rank one event. See :func:`_event_severity_with_source` for the reasoning."""
    return _event_severity_with_source(event, key, catalogue)[0]


def _event_severity_with_source(
    event: object, key: str, catalogue: dict[str, str]
) -> tuple[str, str]:
    """Rank one event, and say where the rank came from.

    Order: this skill's judgement, then the event's own ``severity``, then
    vCenter's catalogue, then the ``esx.problem.`` naming convention, then
    "unknown".

    The override sets come first on purpose. vCenter files HostShutdownEvent
    under "info"; for a monitoring skill a host that shut down is critical, and
    that disagreement is the product rather than a defect.

    ``"unknown"`` is a real fifth answer and is NOT folded into "info". An event
    nobody can rank is not evidence that nothing happened, and defaulting it to
    the quietest rank is what let five warning-level events be reported as
    "No events above warning" (VCF 9.1, 2026-08-30).

    The source is returned because the five answers are not interchangeable. On
    the same estate 41 of 50 events ranked "unknown", every one of them an
    ``esx.problem.*`` -- and one of those was a full ramdisk. A caller that
    cannot tell "vCenter called this info" from "nothing would rank it" cannot
    tell a quiet estate from a blind one.
    """
    name = short_event_type(key)
    if name in CRITICAL_EVENTS:
        return "critical", "override"
    if name in WARNING_EVENTS:
        return "warning", "override"
    if name in INFO_EVENTS:
        return "info", "override"
    # EventEx carries its own severity inline; prefer it over the catalogue
    # lookup because a vendor-defined type may not be in the catalogue at all.
    # ExtendedEvent has no such field, which is why the fallbacks below exist.
    own = getattr(event, "severity", None)
    if own:
        rank = _VC_CATEGORY_RANK.get(str(own).lower())
        if rank:
            return rank, "event"
    from_catalogue = _VC_CATEGORY_RANK.get(catalogue.get(key, ""))
    if from_catalogue:
        return from_catalogue, "catalogue"
    if key.startswith(_PROBLEM_PREFIX):
        return "warning", "name_prefix"
    return "unknown", "unclassified"
