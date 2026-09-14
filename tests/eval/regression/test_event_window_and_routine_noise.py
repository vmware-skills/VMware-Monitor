"""Regression — events for a chosen window, and routine logins folded, not listed.

Found 2026-09-14 on a lab vCenter 8.0.3, answering "what happened to
192.168.60.15 on 2026-09-03?":

* ``get_events`` and the bundles only took "the last N hours". A day eleven
  days back could not be asked for.
* 1174 events in 48 h were almost all ``UserLoginSessionEvent`` /
  ``UserLogoutSessionEvent`` from one local agent every five minutes. They
  filled the investigation bundles' 50-row timeline: ``investigate vm
  test-llm`` showed 50 host logins and not one event of the VM.

``get_events`` now takes ``start`` / ``end`` (ISO 8601) and folds routine
session events by default — counted in ``routine_folded`` with a note, listed
with ``include_routine=true``. The bundles fold them too and say how many.
Failed logins (``BadUsernameSessionEvent``) are not routine and stay.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from vmware_monitor.ops import _correlate, health

_T0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _ev(cls: str, i: int, minutes: int, msg: str = ""):
    kind = type(cls, (SimpleNamespace,), {})
    return kind(
        key=i,
        createdTime=_T0 + timedelta(minutes=minutes),
        fullFormattedMessage=msg or f"{cls} {i}",
        userName="root",
    )


def _si():
    mgr = SimpleNamespace(description=SimpleNamespace(eventInfo=[]))
    return SimpleNamespace(RetrieveContent=lambda: SimpleNamespace(eventManager=mgr))


def _patch_read(monkeypatch, events, seen=None):
    def fake(mgr, spec, max_events=None):
        if seen is not None:
            seen.append(spec)
        return health.EventRead(tuple(reversed(events)), False)

    monkeypatch.setattr(health, "read_events", fake)


@pytest.mark.unit
def test_an_explicit_window_is_what_is_queried(monkeypatch):
    seen = []
    _patch_read(monkeypatch, [], seen)
    out = health.get_recent_events(
        _si(),
        hours=1,
        severity="info",
        start="2026-09-03T12:00:00Z",
        end="2026-09-03T16:00:00+00:00",
    )
    t = seen[0].time
    assert t.beginTime == datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    assert t.endTime == datetime(2026, 9, 3, 16, tzinfo=timezone.utc)
    assert out["window"] == {
        "start": "2026-09-03T12:00:00+00:00",
        "end": "2026-09-03T16:00:00+00:00",
    }


@pytest.mark.unit
def test_start_alone_runs_to_now_and_a_naive_time_is_utc(monkeypatch):
    seen = []
    _patch_read(monkeypatch, [], seen)
    health.get_recent_events(_si(), severity="info", start="2026-09-03 12:00")
    t = seen[0].time
    assert t.beginTime == datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    assert t.endTime > t.beginTime + timedelta(days=5)


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs,words",
    [
        ({"start": "yesterday"}, "ISO 8601"),
        ({"start": "2026-09-03T16:00Z", "end": "2026-09-03T12:00Z"}, "before"),
    ],
)
def test_a_bad_window_is_refused_with_how_to_fix_it(monkeypatch, kwargs, words):
    _patch_read(monkeypatch, [])
    with pytest.raises(ValueError, match=words):
        health.get_recent_events(_si(), severity="info", **kwargs)


@pytest.mark.unit
def test_routine_logins_are_folded_and_counted(monkeypatch):
    events = [_ev("UserLoginSessionEvent", i, i) for i in range(10)]
    events += [
        _ev("UserLogoutSessionEvent", 100, 11),
        _ev("BadUsernameSessionEvent", 101, 12),
        _ev("VmPoweredOnEvent", 102, 13),
    ]
    _patch_read(monkeypatch, events)
    out = health.get_recent_events(_si(), severity="info")
    types = [e["event_type"] for e in out["items"]]
    assert sorted(types) == ["BadUsernameSessionEvent", "VmPoweredOnEvent"]
    assert out["routine_folded"] == {"UserLoginSessionEvent": 10, "UserLogoutSessionEvent": 1}
    assert "11" in out["routine_note"] and "include_routine" in out["routine_note"]

    listed = health.get_recent_events(_si(), severity="info", include_routine=True)
    assert len(listed["items"]) == 13
    assert "routine_note" not in listed and listed["routine_folded"] == {}


@pytest.mark.unit
def test_nothing_is_reported_folded_when_the_filter_would_drop_it_anyway(monkeypatch):
    _patch_read(monkeypatch, [_ev("UserLoginSessionEvent", i, i) for i in range(5)])
    out = health.get_recent_events(_si(), severity="warning")
    assert out["routine_folded"] == {} and "routine_note" not in out


@pytest.mark.unit
def test_the_vm_event_is_not_crowded_out_by_host_logins(monkeypatch):
    """The reproduction: 60 newer host logins, one older VM event."""
    host_logins = [_ev("UserLoginSessionEvent", i, 100 + i) for i in range(60)]
    vm_event = _ev("VmPoweredOnEvent", 999, 1, "test-llm on 192.168.60.15 is powered on")
    reads = {"vm": [vm_event], "host": host_logins}
    monkeypatch.setattr(
        _correlate,
        "_entity_events",
        lambda mgr, ref, begin, now: health.EventRead(tuple(reversed(reads[ref])), False),
    )
    tl, _unavailable, note = _correlate.entity_timeline(
        _si(), [("vm", "test-llm", "vm"), ("host", "192.168.60.15", "host")], hours=24
    )
    assert [e["event_type"] for e in tl] == ["VmPoweredOnEvent"]
    assert note and "60 routine" in note


@pytest.mark.unit
def test_mcp_get_events_takes_the_window_and_the_switch():
    from vmware_monitor.mcp_server import server

    params = inspect.signature(server.get_events).parameters
    assert {"start", "end", "include_routine"} <= set(params)
    doc = inspect.getdoc(server.get_events)
    for name in ("start", "end", "include_routine"):
        assert f"{name}:" in doc, f"{name} is not documented"


@pytest.mark.unit
def test_cli_passes_the_window_through_and_prints_the_note(monkeypatch):
    from rich.console import Console
    from typer.testing import CliRunner

    from vmware_monitor import cli

    seen = {}

    def fake_events(si, **kwargs):
        seen.update(kwargs)
        return health.paginated(
            [],
            unclassified=0,
            read_truncated=False,
            routine_folded={"UserLoginSessionEvent": 3},
            routine_note="3 routine login/logout events were folded.",
        )

    monkeypatch.setattr(health, "get_recent_events", fake_events)
    monkeypatch.setattr(cli, "_get_connection", lambda target, config: (object(), None, "vc"))
    monkeypatch.setattr(cli, "console", Console(width=200, color_system=None))
    result = CliRunner().invoke(
        cli.app,
        [
            "health",
            "events",
            "--start",
            "2026-09-03T12:00Z",
            "--end",
            "2026-09-03T16:00Z",
            "--include-routine",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["start"] == "2026-09-03T12:00Z" and seen["end"] == "2026-09-03T16:00Z"
    assert seen["include_routine"] is True
    assert "3 routine" in result.output
