"""A standalone ESXi exposes an event manager and then refuses QueryEvents.

Both investigation bundles died there with a raw ``vmodl.fault.NotImplemented``
after every other read had already succeeded — on 4 of the 5 targets configured
on the machine this was found on, and reproduced on a live 8.0.3 host here.

The wrapper already had a fault list for exactly this, and it held only
``vmodl.fault.NotSupported``. Its comment reasoned correctly about which class
*exists* ("vim.fault has no NotSupported class; vmodl.fault.NotSupported is the
one") and never checked which one ESXi actually raises. Knowing a class exists
is not knowing it is thrown.

Degrading is not the same as going quiet: an empty timeline because nothing
happened and an empty timeline because there is no event service must not look
the same to whoever reads the bundle.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pyVmomi import vmodl

from vmware_monitor.ops import _correlate
from vmware_monitor.ops.health import query_events, query_events_or_none


class _RefusingEventManager:
    """Answers QueryEvents the way a standalone ESXi host does."""

    def __init__(self, fault: type[Exception]) -> None:
        self._fault = fault

    def QueryEvents(self, _spec):  # noqa: N802 — mirrors pyVmomi's own name
        raise self._fault()


@pytest.mark.parametrize("fault", [vmodl.fault.NotImplemented, vmodl.fault.NotSupported])
def test_both_refusal_faults_are_recognised(fault) -> None:
    """NotImplemented is what ESXi actually raises; NotSupported was the guess."""
    assert query_events_or_none(_RefusingEventManager(fault), object()) is None
    assert query_events(_RefusingEventManager(fault), object()) == []


def test_a_real_failure_still_propagates() -> None:
    """Only "this endpoint has no event service" may be swallowed.

    An auth or network failure that returned [] would make a monitoring tool
    report all-clear precisely when it cannot see.
    """
    with pytest.raises(vmodl.fault.SecurityError):
        query_events_or_none(_RefusingEventManager(vmodl.fault.SecurityError), object())


def test_the_timeline_says_it_could_not_read_rather_than_showing_nothing(monkeypatch) -> None:
    monkeypatch.setattr(
        _correlate, "_entity_events", lambda mgr, ref, begin, now: None
    )
    si = type("SI", (), {"RetrieveContent": lambda self: type("C", (), {"eventManager": object()})()})()
    rows, reason = _correlate.entity_timeline(si, [("host", "esxi01", object())], hours=24)
    assert rows == []
    assert reason and "does not serve event history" in reason


def test_a_genuinely_empty_window_is_not_reported_as_unavailable(monkeypatch) -> None:
    """The other half. An honest empty result must stay honestly empty."""
    monkeypatch.setattr(_correlate, "_entity_events", lambda mgr, ref, begin, now: [])
    si = type("SI", (), {"RetrieveContent": lambda self: type("C", (), {"eventManager": object()})()})()
    rows, reason = _correlate.entity_timeline(si, [("host", "esxi01", object())], hours=24)
    assert rows == []
    assert reason is None, "an empty window was reported as a missing event service"


def test_the_bundle_carries_the_reason(monkeypatch) -> None:
    """The flag has to reach the payload, or none of the above helps a reader."""
    import inspect

    from vmware_monitor.ops import investigate_host, investigate_vm, investigate_datastore

    for mod in (investigate_host, investigate_vm, investigate_datastore):
        src = inspect.getsource(mod)
        assert '"timeline_unavailable"' in src, f"{mod.__name__} drops the reason"


def test_a_partial_refusal_is_not_swallowed_by_the_scopes_that_worked(monkeypatch) -> None:
    """One scope answering must not hide another scope being unreadable.

    A bundle asks for several scopes (vm, host, cluster, datastores). The first
    version of the fix said ``if refused and not rows`` — so the moment any one
    of them returned an event, the fact that another returned nothing readable
    was dropped, and a timeline missing a whole scope looked complete. That is
    the same failure this whole file is about, reintroduced inside its own fix.
    """
    from datetime import datetime, timezone

    def _events(mgr, ref, begin, now):
        return None if ref == "refuses" else [
            type("E", (), {"createdTime": datetime(2026, 8, 31, tzinfo=timezone.utc),
                           "fullFormattedMessage": "something happened",
                           "userName": "", "eventTypeId": "x"})()
        ]

    monkeypatch.setattr(_correlate, "_entity_events", _events)
    si = type("SI", (), {"RetrieveContent": lambda self: type("C", (), {"eventManager": object()})()})()
    rows, reason = _correlate.entity_timeline(
        si,
        [("cluster", "cl01", "answers"), ("host", "esxi01", "refuses")],
        hours=24,
    )
    assert rows, "the scope that answered should still contribute"
    assert reason is not None, "a scope that could not be read was reported as fine"
    assert "host" in reason and "incomplete" in reason
