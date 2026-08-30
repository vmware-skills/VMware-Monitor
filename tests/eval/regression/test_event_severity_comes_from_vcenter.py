"""Event severity must come from vCenter, not from a 23-name list in this file.

Real-hardware finding, 2026-08-30 (VCF 9.1): `health events` reported "No events
above 'warning' in the last 24h" on an estate that had five warning-level events
in that window.

The classifier was three hardcoded sets of pyVmomi class names — 23 of them —
with `else: sev = "info"`. vSphere defines on the order of a thousand event
types, so almost everything fell through to the default and was then filtered
out by the default `severity=warning`. The green "No events" line is the part
that makes it expensive: the tool did not fail, it reported calm.

Two compounding details, both fixed here:

* Modern vSphere emits most events as ``EventEx``/``ExtendedEvent``, whose real
  identity is ``eventTypeId`` (e.g. ``esx.problem.scsi.device.io.latency.high``)
  — ``type(event).__name__`` is the literal string "EventEx" for all of them, so
  they could not have matched any name in the table even in principle.
* vCenter publishes the authoritative mapping itself, as
  ``EventManager.description.eventInfo`` — ``(key, category)`` for every type it
  knows, categories being info/warning/error/user. It was never consulted.

The three sets survive as deliberate *overrides* — this skill calls
HostShutdownEvent critical where vCenter files it as info, and that judgement is
the product, not a bug.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vmware_monitor.ops import health as ops


class _Ref:
    """Stands in for the EventManager managed object."""

    def __init__(self, events, catalogue):
        self._events = events
        self.description = SimpleNamespace(
            eventInfo=[
                SimpleNamespace(key=key, category=category)
                for key, category in catalogue.items()
            ]
        )

    def QueryEvents(self, _spec):  # noqa: N802 - mirrors pyVmomi's own method name
        return self._events


def _si(event_mgr):
    return SimpleNamespace(RetrieveContent=lambda: SimpleNamespace(eventManager=event_mgr))


def _event(cls_name: str, message: str, **extra):
    """A pyVmomi-shaped event whose Python class name is ``cls_name``."""
    kind = type(cls_name, (SimpleNamespace,), {})
    return kind(
        fullFormattedMessage=message,
        createdTime="2026-08-03T05:49:00Z",
        userName="root",
        **extra,
    )


@pytest.mark.unit
def test_a_warning_vcenter_knows_about_is_not_dropped_as_info():
    """The reported failure. This type is in none of the three hardcoded sets."""
    mgr = _Ref(
        [_event("VmDiskConsolidationNeededEvent", "disk consolidation needed")],
        {"VmDiskConsolidationNeededEvent": "warning"},
    )

    out = ops.get_recent_events(_si(mgr), hours=24, severity="warning")

    assert len(out["items"]) == 1, (
        "vCenter classifies this type as a warning; it was dropped because the "
        "local table had never heard of it"
    )
    assert out["items"][0]["severity"] == "warning"


@pytest.mark.unit
def test_an_eventex_is_identified_by_its_event_type_id():
    """All EventEx instances share one Python class name, so the type table
    could never have matched any of them."""
    mgr = _Ref(
        [
            _event(
                "EventEx",
                "device latency high",
                eventTypeId="esx.problem.scsi.device.io.latency.high",
            )
        ],
        {"esx.problem.scsi.device.io.latency.high": "error"},
    )

    out = ops.get_recent_events(_si(mgr), hours=24, severity="warning")

    assert len(out["items"]) == 1
    row = out["items"][0]
    assert row["severity"] == "critical", "vCenter category 'error' ranks as critical"
    assert row["event_type"] == "esx.problem.scsi.device.io.latency.high", (
        "reporting 'EventEx' tells the reader nothing and cannot be looked up"
    )


@pytest.mark.unit
def test_this_skills_own_severity_judgements_still_win():
    """The control that stops the fix from becoming a rewrite.

    vCenter files HostShutdownEvent under 'info'. This skill deliberately ranks
    it critical, and that judgement is the product.
    """
    mgr = _Ref(
        [_event("HostShutdownEvent", "Shut down of esxi05.knight.com")],
        {"HostShutdownEvent": "info"},
    )

    out = ops.get_recent_events(_si(mgr), hours=24, severity="warning")

    assert [r["severity"] for r in out["items"]] == ["critical"]


@pytest.mark.unit
def test_an_event_nobody_can_rank_is_surfaced_not_swallowed():
    """Unknown is not calm.

    A type absent from both the overrides and vCenter's own catalogue used to
    become 'info' and vanish. In a monitoring tool an event that cannot be
    ranked is not evidence that nothing happened.
    """
    mgr = _Ref([_event("SomeVendorPluginEvent", "vendor said something")], {})

    out = ops.get_recent_events(_si(mgr), hours=24, severity="warning")

    assert out["unclassified"] == 1
    assert len(out["items"]) == 1
    assert out["items"][0]["severity"] == "unknown"
    note = (out.get("classification_note") or "").lower()
    assert "could not be ranked" in note and "1 event" in note


@pytest.mark.unit
def test_a_genuinely_quiet_window_is_still_reported_as_quiet():
    """The other control. A fix that surfaces everything would pass the tests
    above and turn every routine hour into an incident."""
    mgr = _Ref(
        [
            _event("UserLoginSessionEvent", "user logged in"),
            _event("VmPoweredOnEvent", "vm powered on"),
        ],
        {"UserLoginSessionEvent": "user", "VmPoweredOnEvent": "info"},
    )

    out = ops.get_recent_events(_si(mgr), hours=24, severity="warning")

    assert out["items"] == []
    assert out["unclassified"] == 0
    assert "classification_note" not in out


@pytest.mark.unit
def test_a_vcenter_without_a_catalogue_still_returns_events():
    """Older or restricted vCenters may not expose description.eventInfo.
    Losing the catalogue must degrade to the overrides, not to an exception."""
    mgr = _Ref([_event("HostConnectionLostEvent", "lost")], {})
    mgr.description = None

    out = ops.get_recent_events(_si(mgr), hours=24, severity="warning")

    assert [r["severity"] for r in out["items"]] == ["critical"]


# ── the shape a real vCenter actually sends ────────────────────────────────
#
# Everything above mocks `EventDetail.key` as a string, and that is why v1.8.14
# shipped broken. pyVmomi's own VMODL metadata declares it as a **type**:
#
#     vim.event.EventDescription.EventDetail  ->  key: <class 'type'>
#                                                 category: <class 'str'>
#
# So on a live vCenter `key` is a pyVmomi class, `str(key)` renders
# "<class 'pyVmomi.VmomiSupport.vim.event.VmPoweredOnEvent'>", and no lookup
# against `type(event).__name__` can ever match. Every event fell through to
# "unknown" — which ranks alongside warning, so instead of hiding events the
# fix flooded: 1000 of 1000 unclassified, 998 rows returned, 89% of them login
# noise (real VCF 9.1 estate, 2026-08-30).
#
# Validating in an environment where the defect could not appear (形态 #3),
# committed while fixing that very class of bug.


class _RealisticDetail:
    """An EventDetail whose `key` is a type, as pyVmomi declares it."""

    def __init__(self, event_cls, category):
        self.key = event_cls
        self.category = category


def _real_mgr(events, pairs):
    """`pairs` is [(event_class, category)] — key carried as the class itself."""
    mgr = _Ref(events, {})
    mgr.description = SimpleNamespace(
        eventInfo=[_RealisticDetail(cls, cat) for cls, cat in pairs]
    )
    return mgr


@pytest.mark.unit
def test_the_catalogue_key_is_a_type_not_a_string():
    """The regression that shipped. Reproduces pyVmomi's declared shape."""
    warning_cls = type("VmDiskConsolidationNeededEvent", (SimpleNamespace,), {})
    event = warning_cls(
        fullFormattedMessage="disk consolidation needed",
        createdTime="2026-08-03T05:49:00Z",
        userName="root",
    )
    mgr = _real_mgr([event], [(warning_cls, "warning")])

    out = ops.get_recent_events(_si(mgr), hours=24, severity="warning")

    assert out["unclassified"] == 0, (
        "the catalogue was keyed by str(<class ...>) and matched nothing, so "
        "every event on a real vCenter ranked as unknown"
    )
    assert [r["severity"] for r in out["items"]] == ["warning"]


@pytest.mark.unit
def test_routine_events_are_still_filtered_out_on_the_real_shape():
    """The half that turned a hiding bug into a flooding one.

    With the catalogue unusable, everything became `unknown`, which ranks with
    warning and therefore passes the default filter. A login storm was returned
    in full.
    """
    # Deliberately NOT a name in CRITICAL/WARNING/INFO_EVENTS: those overrides
    # run before the catalogue, so using one would let this test pass without
    # ever exercising the lookup it exists to check (形态 #4 — it did, on the
    # first draft, via UserLoginSessionEvent).
    login = type("AccountCreatedEvent", (SimpleNamespace,), {})
    assert "AccountCreatedEvent" not in (
        ops.CRITICAL_EVENTS | ops.WARNING_EVENTS | ops.INFO_EVENTS
    )
    events = [
        login(
            fullFormattedMessage=f"user {i} logged in",
            createdTime="2026-08-03T05:49:00Z",
            userName="root",
        )
        for i in range(50)
    ]
    mgr = _real_mgr(events, [(login, "user")])

    out = ops.get_recent_events(_si(mgr), hours=24, severity="warning")

    assert out["items"] == [], "50 routine logins were returned as warnings"
    assert out["unclassified"] == 0
