"""Regression — the event override sets match what pyVmomi really hands back.

Found 2026-09-14 on a lab vCenter 8.0.3, in the middle of another fix. The sets
that rank events — CRITICAL_EVENTS, WARNING_EVENTS, INFO_EVENTS, the routine set
and the suggestion map — are written as bare class names ("HostConnectionLostEvent").
A real pyVmomi event's class is named ``vim.event.HostConnectionLostEvent``. So
on a live vCenter none of them ever matched:

* ``_correlate._classify`` returned "info" for every event, so every
  investigation timeline showed a host that stopped responding as INFO;
* the daemon's event scan (``scanner/log_scanner.scan_logs``) keeps only
  critical and warning events, found none, and reported a quiet estate;
* ``get_events`` fell through to vCenter's catalogue for every event, so the
  skill's own judgement (HostShutdownEvent is critical, not info) never applied
  and no ``suggested_actions`` were attached;
* routine logins were not folded.

Every test before this one used ``type("HostConnectionLostEvent", ...)`` stand-ins
whose ``__name__`` is the bare name — the shape the defect cannot appear in.
These use real pyVmomi event objects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vmware_monitor.ops import _correlate, health
from vmware_monitor.scanner import log_scanner

_T = datetime(2026, 9, 3, 14, 8, 46, tzinfo=timezone.utc)


def _real(cls, key):
    return cls(
        key=key, createdTime=_T, fullFormattedMessage=f"{cls.__name__} {key}", userName="root"
    )


@pytest.mark.unit
def test_real_class_names_are_the_dotted_ones():
    """The premise, so the rest cannot pass by accident."""
    assert (
        type(_real(vim.event.HostConnectionLostEvent, 1)).__name__
        == "vim.event.HostConnectionLostEvent"
    )


@pytest.mark.unit
def test_the_timeline_ranks_a_real_lost_host_as_critical():
    assert _correlate._classify(_real(vim.event.HostConnectionLostEvent, 1)) == "critical"
    assert _correlate._classify(_real(vim.event.BadUsernameSessionEvent, 2)) == "warning"


def _si(events):
    mgr = SimpleNamespace(description=SimpleNamespace(eventInfo=[]))
    return SimpleNamespace(RetrieveContent=lambda: SimpleNamespace(eventManager=mgr)), events


@pytest.mark.unit
def test_get_events_applies_the_overrides_suggestions_and_folding(monkeypatch):
    events = [
        _real(vim.event.HostShutdownEvent, 1),
        _real(vim.event.HostConnectionLostEvent, 2),
        _real(vim.event.UserLoginSessionEvent, 3),
    ]
    si, _ = _si(events)
    monkeypatch.setattr(
        health,
        "read_events",
        lambda mgr, spec, max_events=None: health.EventRead(tuple(events), False),
    )
    out = health.get_recent_events(si, severity="info")
    rows = {r["event_type"]: r for r in out["items"]}
    shutdown = rows["vim.event.HostShutdownEvent"]
    assert (shutdown["severity"], shutdown["severity_source"]) == ("critical", "override")
    assert rows["vim.event.HostConnectionLostEvent"]["suggested_actions"], "no suggestion attached"
    assert "vim.event.UserLoginSessionEvent" not in rows
    assert sum(out["routine_folded"].values()) == 1


@pytest.mark.unit
def test_the_daemon_event_scan_finds_a_real_critical_event(monkeypatch):
    events = [
        _real(vim.event.HostConnectionLostEvent, 1),
        _real(vim.event.UserLoginSessionEvent, 2),
    ]
    monkeypatch.setattr(log_scanner, "query_events", lambda mgr, spec: list(events))
    si = SimpleNamespace(RetrieveContent=lambda: SimpleNamespace(eventManager=object()))
    cfg = SimpleNamespace(lookback_hours=1, severity_threshold="warning")
    issues = log_scanner.scan_logs(si, cfg)
    assert [i["severity"] for i in issues] == ["critical"], issues
