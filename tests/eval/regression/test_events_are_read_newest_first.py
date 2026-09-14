"""Regression — event reads return the newest events, and say when they stopped.

Found 2026-09-14 on a lab vCenter 8.0.3. ``health events --hours 24`` returned
exactly 1000 events spanning 13:46 → 21:53 the previous day: the latest 16 hours
were missing and nothing said so. ``--hours 168`` did the same a week back. The
investigation bundles went through the same call, so "what happened to this VM
recently?" could answer from last week while looking complete.

Cause (measured, see _fake_events.py): ``EventManager.QueryEvents`` returns at
most 1000 events and they are the oldest ones in the window. The fix reads
through an event history collector newest-first, bounds the read, and reports
when the bound cut it short.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pyVmomi import vim, vmodl

from tests.eval.regression._fake_events import FakeEventManager
from vmware_monitor.ops import _correlate, health

_T0 = datetime(2026, 9, 13, 14, 0, tzinfo=timezone.utc)


def _events(n: int, cls: str = "UserLoginSessionEvent") -> list:
    kind = type(cls, (SimpleNamespace,), {})
    return [
        kind(key=i, createdTime=_T0 + timedelta(seconds=30 * i),
             fullFormattedMessage=f"event {i}", userName="root")
        for i in range(1, n + 1)
    ]


def _si(mgr):
    return SimpleNamespace(RetrieveContent=lambda: SimpleNamespace(eventManager=mgr))


@pytest.mark.unit
def test_the_newest_event_is_read_past_a_thousand() -> None:
    mgr = FakeEventManager(_events(2500))
    read = health.read_events(mgr, filter_spec=None)
    keys = [e.key for e in read.events]
    assert keys[0] == 2500, f"newest read was {keys[0]} — the oldest-1000 bug"
    assert keys == sorted(keys, reverse=True), "not newest first"
    assert len(keys) == len(set(keys)) == 2500
    assert read.truncated is False
    assert all(c.destroyed for c in mgr.collectors) and mgr.collectors


@pytest.mark.unit
def test_past_the_bound_the_newest_are_kept_and_the_cut_is_reported() -> None:
    mgr = FakeEventManager(_events(7000))
    read = health.read_events(mgr, filter_spec=None, max_events=5000)
    keys = [e.key for e in read.events]
    assert len(keys) == 5000 and keys[0] == 7000 and keys[-1] == 2001
    assert read.truncated is True


@pytest.mark.unit
def test_exactly_the_bound_is_not_reported_as_cut() -> None:
    read = health.read_events(FakeEventManager(_events(5000)), filter_spec=None, max_events=5000)
    assert len(read.events) == 5000 and read.truncated is False


@pytest.mark.unit
def test_the_collector_is_released_when_a_read_fails() -> None:
    """vCenter allows a bounded number of collectors per session."""
    mgr = FakeEventManager(_events(2500), read_fault=RuntimeError("session dropped"))
    with pytest.raises(RuntimeError):
        health.read_events(mgr, filter_spec=None)
    assert mgr.collectors and all(c.destroyed for c in mgr.collectors)


@pytest.mark.unit
@pytest.mark.parametrize("fault", [vmodl.fault.NotImplemented(), vmodl.fault.NotSupported()])
def test_an_endpoint_without_event_history_reads_as_none(fault) -> None:
    assert health.read_events(FakeEventManager(fault=fault), filter_spec=None) is None


@pytest.mark.unit
def test_a_real_failure_propagates() -> None:
    with pytest.raises(vim.fault.NoPermission):
        health.read_events(FakeEventManager(fault=vim.fault.NoPermission()), filter_spec=None)


@pytest.mark.unit
def test_recent_events_include_the_newest_event() -> None:
    """The reproduction: a critical event after 1000 routine ones must show."""
    events = _events(1500) + _events(1, "HostConnectionLostEvent")
    events[-1].key, events[-1].createdTime = 99999, _T0 + timedelta(days=1)
    out = health.get_recent_events(_si(FakeEventManager(events)), hours=48, severity="warning")
    assert any(e["event_type"] == "HostConnectionLostEvent" for e in out["items"])
    assert out["read_truncated"] is False
    assert "read_note" not in out


@pytest.mark.unit
def test_recent_events_say_when_the_read_was_cut(monkeypatch) -> None:
    monkeypatch.setattr(health, "MAX_EVENTS_READ", 50)
    out = health.get_recent_events(_si(FakeEventManager(_events(80))), hours=24, severity="info")
    assert out["read_truncated"] is True
    note = out["read_note"]
    assert "50" in note and "24h" in note and "newest" in note


@pytest.mark.unit
def test_the_timeline_says_a_scope_was_cut_and_how_many_it_shows(monkeypatch) -> None:
    rows = _events(80, "VmPoweredOnEvent")
    monkeypatch.setattr(
        _correlate, "_entity_events",
        lambda mgr, ref, begin, now: health.EventRead(events=tuple(reversed(rows)), truncated=True),
    )
    tl, unavailable, note = _correlate.entity_timeline(
        _si(object()), [("host", "esx-01", object())], hours=24
    )
    assert unavailable is None
    assert len(tl) == _correlate.MAX_TIMELINE_EVENTS
    assert tl[0]["message"] == "event 80"
    assert note and "host" in note and f"{_correlate.MAX_TIMELINE_EVENTS} of 80" in note


@pytest.mark.unit
def test_a_complete_short_timeline_carries_no_note(monkeypatch) -> None:
    monkeypatch.setattr(
        _correlate, "_entity_events",
        lambda mgr, ref, begin, now: health.EventRead(
            events=tuple(_events(3, "VmPoweredOnEvent")), truncated=False
        ),
    )
    _tl, _unavailable, note = _correlate.entity_timeline(
        _si(object()), [("vm", "web-01", object())], hours=24
    )
    assert note is None


@pytest.mark.unit
def test_every_bundle_carries_the_timeline_note() -> None:
    import inspect

    from vmware_monitor.ops import investigate_datastore, investigate_host, investigate_vm

    for mod in (investigate_host, investigate_vm, investigate_datastore):
        assert '"timeline_note"' in inspect.getsource(mod), f"{mod.__name__} drops the note"


@pytest.mark.unit
def test_a_scope_the_endpoint_cannot_filter_by_is_reported_not_raised() -> None:
    """Found live 2026-09-14, in this fix. A standalone ESXi 8.0.3 serves the
    event collector for its host but answers a datastore-scoped filter with
    ``vmodl.fault.InvalidType`` (argument ``vim.Datastore``). Before the
    collector, QueryEvents failed first and the bundle said "no event history";
    with it, the host scope read and the datastore scope took the whole host
    bundle down. That scope is now reported as unreadable, like a refusal."""
    mgr = FakeEventManager(fault=vmodl.fault.InvalidType(argument="vim.Datastore"))
    assert _correlate._entity_events(mgr, vim.Datastore("datastore-1"), _T0, _T0) is None


@pytest.mark.unit
def test_invalid_type_is_only_forgiven_for_entity_scopes() -> None:
    """The unscoped read has no entity type to be wrong about, so the fault is real there."""
    with pytest.raises(vmodl.fault.InvalidType):
        health.read_events(FakeEventManager(fault=vmodl.fault.InvalidType()), filter_spec=None)
