"""An EventManager stand-in that behaves the way vCenter 8.0.3 was measured to.

Measured live on 2026-09-14 (lab vCenter 8.0.3, ~24 events an hour of routine
login noise on one host):

* ``QueryEvents`` returns at most 1000 events and they are the **oldest** 1000
  in the window: a 24 h query came back 14:16 → 22:28 of the previous day and
  missed the latest 16 hours; a 2 h query (382 events) was complete.
* ``CreateCollectorForEvents`` + ``SetCollectorPageSize(1000)`` makes
  ``latestPage`` the newest ≤1000 events, newest first.
* ``ResetCollector`` positions the cursor just before that page;
  ``ReadPreviousEvents(n)`` then returns the next-older ≤n events, **oldest
  first within the page**, with no gap or overlap against ``latestPage``.
* A standalone ESXi 8.0.3 raises ``vmodl.fault.NotImplemented`` from
  ``QueryEvents`` but serves the collector (609 events in 24 h).

The stand-in keeps events oldest → newest in list order, so fixtures without a
``key`` or a real ``createdTime`` still have an order.
"""

from __future__ import annotations


class FakeEventCollector:
    def __init__(self, events: list, owner: FakeEventManager) -> None:
        self._events = list(events)
        self._owner = owner
        self._page_size = 10
        self._cursor: int | None = None
        self.destroyed = False

    def SetCollectorPageSize(self, maxCount):  # noqa: N802,N803 — pyVmomi names
        self._page_size = maxCount

    @property
    def latestPage(self):  # noqa: N802
        return list(reversed(self._events[-self._page_size:])) if self._page_size else []

    def ResetCollector(self):  # noqa: N802
        self._cursor = max(0, len(self._events) - self._page_size)

    def ReadPreviousEvents(self, maxCount):  # noqa: N802,N803
        if self._owner.read_fault is not None:
            raise self._owner.read_fault
        end = self._cursor if self._cursor is not None else 0
        start = max(0, end - maxCount)
        self._cursor = start
        return self._events[start:end]

    def DestroyCollector(self):  # noqa: N802
        self.destroyed = True


class FakeEventManager:
    """``events`` oldest → newest. ``fault`` is raised by CreateCollectorForEvents."""

    def __init__(self, events: list | None = None, fault: Exception | None = None,
                 read_fault: Exception | None = None, description=None) -> None:
        self._events = list(events or [])
        self.fault = fault
        self.read_fault = read_fault
        self.collectors: list[FakeEventCollector] = []
        if description is not None:
            self.description = description

    def CreateCollectorForEvents(self, filter):  # noqa: N802,A002 — pyVmomi names
        if self.fault is not None:
            raise self.fault
        collector = FakeEventCollector(self._events, self)
        self.collectors.append(collector)
        return collector

    def QueryEvents(self, _spec):  # noqa: N802
        """What vCenter really does: the oldest 1000 in the window."""
        if self.fault is not None:
            raise self.fault
        return self._events[:1000]
