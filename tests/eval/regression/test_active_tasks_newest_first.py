"""list_active_tasks with a limit must keep the NEWEST tasks.

Real hardware, 2026-10-17: total 57, limit=40 dropped exactly the 17 newest --
the comment said "by start time descending" and the sort was ascending. The
tasks an operator asks about are the ones that just ran.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from vmware_monitor.ops.activity import get_active_tasks

T0 = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)


def _task(key, state, minutes):
    start = None if minutes is None else T0 + timedelta(minutes=minutes)
    return SimpleNamespace(info=SimpleNamespace(
        state=state, error=None, descriptionId=key, key=key, entityName="vm-1",
        progress=None, startTime=start, reason=None))


def _si(tasks):
    content = SimpleNamespace(taskManager=SimpleNamespace(recentTask=tasks))
    return SimpleNamespace(RetrieveContent=lambda: content)


@pytest.mark.unit
def test_a_limit_keeps_the_newest_finished_tasks() -> None:
    tasks = [_task(f"t{i}", "success", i) for i in range(57)]
    out = get_active_tasks(_si(tasks), include_recent=True, limit=40)

    names = [r["name"] for r in out["items"]]
    assert names[0] == "t56"
    assert "t56" in names and "t0" not in names
    assert out["total"] == 57 and out["truncated"] is True


@pytest.mark.unit
def test_running_tasks_stay_first_newest_first_and_unknown_starts_last() -> None:
    tasks = [_task("old-done", "success", 1), _task("new-done", "success", 9),
             _task("no-start", "success", None), _task("run-old", "running", 2),
             _task("run-new", "running", 8)]
    out = get_active_tasks(_si(tasks), include_recent=True)

    assert [r["name"] for r in out["items"]] == [
        "run-new", "run-old", "new-done", "old-done", "no-start"]
