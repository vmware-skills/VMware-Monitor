"""Backup windows inferred from snapshot task history (GitHub issue #26).

The request (juanpf-ha, 2026-08-27) asked for a read-only tool estimating how
long a backup holds a snapshot open, and proposed matching the operation on
several TaskInfo fields "because their representation can vary between vCenter
versions". That instinct was right, and one of the fields it names is a trap:

  ``TaskInfo.name`` is declared by pyVmomi as a **ManagedMethod**, not a string.
  ``str()`` on it yields ``<pyVmomi.VmomiSupport.ManagedMethod object at 0x...>``.

This skill has already shipped that mistake once, on a sibling field:
``EventDescription.EventDetail.key`` is declared as a *type*, was read as a
string in v1.8.14, and every event on real hardware fell through to "unknown".
A string comparison against a stringified object does not raise — it simply
never matches — so the failure is silent in both cases.

The tests below pin the three field shapes, and pin the two ways this tool can
report calm when it should not: history it could not read, and a window vCenter
has already partly expired.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vmware_monitor.ops import backup_window as bw

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _task(kind_ids: dict, start: datetime, end: datetime | None, state: str = "success"):
    """A TaskInfo stand-in carrying only the identifiers the caller supplies."""
    return SimpleNamespace(
        descriptionId=kind_ids.get("descriptionId"),
        name=kind_ids.get("name"),
        description=kind_ids.get("description"),
        state=state,
        startTime=start,
        completeTime=end,
    )


def _create(start, end, **ids):
    return _task(ids or {"descriptionId": "VirtualMachine.createSnapshot"}, start, end)


def _remove(start, end, **ids):
    return _task(ids or {"descriptionId": "VirtualMachineSnapshot.remove"}, start, end)


class _Collector:
    def __init__(self, pages, fail_on_read=None):
        self._pages = list(pages)
        self._fail = fail_on_read
        self.destroyed = False
        self.rewound = False

    def RewindCollector(self):
        self.rewound = True

    def ReadNextTasks(self, maxCount):
        if self._fail:
            raise self._fail
        return self._pages.pop(0) if self._pages else []

    def DestroyCollector(self):
        self.destroyed = True


class _SI:
    """ServiceInstance stand-in: one VM inventory plus a task collector."""

    def __init__(self, vm_names, collector=None, create_raises=None):
        self.vms = [
            (vim.VirtualMachine(f"vm-{i}", None), n) for i, n in enumerate(vm_names)
        ]
        self.collector = collector
        self.create_raises = create_raises

    def RetrieveContent(self):
        outer = self

        class _TM:
            def CreateCollectorForTasks(self, filter):
                if outer.create_raises:
                    raise outer.create_raises
                return outer.collector

        return SimpleNamespace(taskManager=_TM())


@pytest.fixture
def patched(monkeypatch):
    """Route _collect at the VM inventory of whichever _SI is passed in."""

    def fake_collect(si, types, props):
        return [(mor, {"name": name}) for mor, name in si.vms]

    class _FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(bw, "_collect", fake_collect)
    monkeypatch.setattr(bw, "datetime", _FrozenClock)
    return None


# ---------------------------------------------------------------------------
# The field shapes. These are the issue's own "multiple fields" point, pinned.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_taskinfo_name_is_a_managed_method_not_a_string() -> None:
    """The SDK fact this module is built on. If pyVmomi ever changes it, fail here."""
    declared = {p.name: p.type for p in vim.TaskInfo._GetPropertyList()}
    assert declared["name"].__name__ == "ManagedMethod", (
        "TaskInfo.name is no longer a ManagedMethod; _task_ids reads .info.wsdlName off it"
    )
    assert declared["descriptionId"] is str
    # And the shape that makes a naive string match silently useless:
    assert "ManagedMethod object at" in str(vim.VirtualMachine.CreateSnapshot)


@pytest.mark.unit
def test_classifies_from_managed_method_when_description_id_is_absent() -> None:
    """A vCenter that populates only TaskInfo.name must still be understood."""
    info = _task({"name": vim.VirtualMachine.CreateSnapshot}, NOW, NOW)
    assert bw._classify(info) == "create"
    info = _task({"name": vim.vm.Snapshot.Remove}, NOW, NOW)
    assert bw._classify(info) == "remove"


@pytest.mark.unit
def test_classifies_from_localizable_message_key() -> None:
    info = _task({"description": SimpleNamespace(key="CreateSnapshot_Task")}, NOW, NOW)
    assert bw._classify(info) == "create"


@pytest.mark.unit
def test_localised_prose_is_never_matched_on() -> None:
    """`description.message` is localised; matching it would break on non-English vCenter."""
    info = _task(
        {"description": SimpleNamespace(key="Foo.bar", message="Create virtual machine snapshot")},
        NOW,
        NOW,
    )
    assert bw._classify(info) is None


@pytest.mark.unit
def test_unrelated_tasks_are_not_snapshot_operations() -> None:
    assert bw._classify(_task({"descriptionId": "VirtualMachine.powerOn"}, NOW, NOW)) is None


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pairs_creation_with_the_following_removal(patched) -> None:
    t0 = NOW - timedelta(hours=10)
    tasks = [
        _create(t0, t0 + timedelta(minutes=6)),
        _remove(t0 + timedelta(hours=3, minutes=12), t0 + timedelta(hours=3, minutes=19)),
    ]
    si = _SI(["vm-a"], collector=_Collector([tasks]))
    r = bw.get_backup_snapshot_history(si, "vm-a", days=30)

    assert r["complete_cycles"] == 1
    assert r["incomplete_cycles"] == 0
    c = r["latest_cycle"]
    # creation completed at +0:06, removal started at +3:12  →  3.1h
    assert c["backup_active_hours"] == pytest.approx(3.1, abs=0.01)
    # ...removal completed at +3:19                          →  3.22h
    assert c["snapshot_present_hours"] == pytest.approx(3.22, abs=0.01)
    # ...measured from creation start                        →  3.32h
    assert c["total_window_hours"] == pytest.approx(3.32, abs=0.01)
    assert c["snapshot_removal_hours"] == pytest.approx(0.12, abs=0.01)


@pytest.mark.unit
def test_creation_without_removal_is_reported_not_dropped(patched) -> None:
    """A backup that left its snapshot behind is the interesting case, not noise."""
    t0 = NOW - timedelta(hours=5)
    si = _SI(["vm-a"], collector=_Collector([[_create(t0, t0 + timedelta(minutes=3))]]))
    r = bw.get_backup_snapshot_history(si, "vm-a", days=30)

    assert r["complete_cycles"] == 0
    assert r["incomplete_cycles"] == 1
    assert "no matching removal" in r["unmatched"][0]["reason"]
    assert r["snapshot_creations"] == 1


@pytest.mark.unit
def test_removal_whose_creation_predates_the_window_is_labelled(patched) -> None:
    t0 = NOW - timedelta(hours=2)
    si = _SI(["vm-a"], collector=_Collector([[_remove(t0, t0 + timedelta(minutes=8))]]))
    r = bw.get_backup_snapshot_history(si, "vm-a", days=30)
    assert r["incomplete_cycles"] == 1
    assert "no creation inside the window" in r["unmatched"][0]["reason"]


@pytest.mark.unit
def test_failed_operations_do_not_form_a_window(patched) -> None:
    """A creation that errored opened nothing; counting it would invent a backup."""
    t0 = NOW - timedelta(hours=6)
    tasks = [
        _task({"descriptionId": "VirtualMachine.createSnapshot"}, t0, t0, state="error"),
        _remove(t0 + timedelta(hours=1), t0 + timedelta(hours=1, minutes=5)),
    ]
    si = _SI(["vm-a"], collector=_Collector([tasks]))
    r = bw.get_backup_snapshot_history(si, "vm-a", days=30)
    assert r["snapshot_creations"] == 0
    assert r["complete_cycles"] == 0


@pytest.mark.unit
def test_statistics_span_every_cycle(patched) -> None:
    tasks = []
    for i, hours in enumerate((1.0, 4.0, 2.0)):
        base = NOW - timedelta(days=3 - i)
        tasks.append(_create(base, base))
        tasks.append(_remove(base + timedelta(hours=hours), base + timedelta(hours=hours)))
    si = _SI(["vm-a"], collector=_Collector([tasks]))
    r = bw.get_backup_snapshot_history(si, "vm-a", days=30, include_cycles=True)

    assert r["complete_cycles"] == 3
    st = r["backup_active_hours"]
    assert (st["minimum"], st["maximum"], st["samples"]) == (1.0, 4.0, 3)
    assert st["average"] == pytest.approx(2.33, abs=0.01)
    assert r["longest_cycle"]["backup_active_hours"] == 4.0
    assert len(r["cycles"]) == 3


# ---------------------------------------------------------------------------
# The two ways this tool could report calm when it should not
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unreadable_history_is_not_reported_as_no_backups(patched) -> None:
    """NoPermission must not arrive as zero cycles with nothing said."""
    si = _SI(["vm-a"], create_raises=vim.fault.NoPermission())
    r = bw.get_backup_snapshot_history(si, "vm-a", days=30)

    assert r["history_unavailable"] is not None
    assert "not an empty history" in r["history_unavailable"]
    assert r["complete_cycles"] == 0
    # ...and the coverage note must not also fire, or the caller gets two
    # different explanations for one condition.
    assert r["coverage_note"] is None


@pytest.mark.unit
def test_expired_task_history_is_declared_not_silently_shortened(patched) -> None:
    """90 days requested against 30 days of retention is not 'backups stopped'."""
    t0 = NOW - timedelta(days=29)
    tasks = [_create(t0, t0), _remove(t0 + timedelta(hours=2), t0 + timedelta(hours=2))]
    si = _SI(["vm-a"], collector=_Collector([tasks]))
    r = bw.get_backup_snapshot_history(si, "vm-a", days=90)

    assert r["coverage_note"] is not None
    assert "aged out of task history" in r["coverage_note"]
    assert "29" in r["coverage_note"]


@pytest.mark.unit
def test_a_fully_covered_window_says_nothing(patched) -> None:
    """The note must not cry wolf when the history really does reach back."""
    t0 = NOW - timedelta(days=29, hours=12)
    tasks = [_create(t0, t0), _remove(t0 + timedelta(hours=1), t0 + timedelta(hours=1))]
    si = _SI(["vm-a"], collector=_Collector([tasks]))
    r = bw.get_backup_snapshot_history(si, "vm-a", days=30)
    assert r["coverage_note"] is None


@pytest.mark.unit
def test_no_tasks_at_all_is_distinguished_from_no_backups(patched) -> None:
    si = _SI(["vm-a"], collector=_Collector([[]]))
    r = bw.get_backup_snapshot_history(si, "vm-a", days=30)
    assert "not specifically no backups" in r["coverage_note"]


# ---------------------------------------------------------------------------
# Resolution and resources
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_duplicate_vm_names_refuse_rather_than_pick_one(patched) -> None:
    """A duration attributed to the wrong same-named VM looks like a valid answer."""
    si = _SI(["vm-a", "vm-a"], collector=_Collector([[]]))
    with pytest.raises(bw.AmbiguousVMError) as exc:
        bw.get_backup_snapshot_history(si, "vm-a", days=30)
    assert "refuses to guess" in str(exc.value)


@pytest.mark.unit
def test_missing_vm_names_the_tool_that_lists_valid_names(patched) -> None:
    si = _SI(["other"], collector=_Collector([[]]))
    with pytest.raises(bw.VMNotFoundError) as exc:
        bw.get_backup_snapshot_history(si, "vm-a", days=30)
    assert "list_virtual_machines" in str(exc.value)


@pytest.mark.unit
def test_collector_is_destroyed_even_when_reading_fails(patched) -> None:
    """vCenter allows ~32 collectors per session; leaking one per call exhausts it."""
    col = _Collector([], fail_on_read=RuntimeError("boom"))
    si = _SI(["vm-a"], collector=col)
    with pytest.raises(RuntimeError):
        bw.get_backup_snapshot_history(si, "vm-a", days=30)
    assert col.destroyed, "collector leaked on the error path"


@pytest.mark.unit
def test_collector_is_destroyed_on_the_happy_path(patched) -> None:
    col = _Collector([[]])
    bw.get_backup_snapshot_history(_SI(["vm-a"], collector=col), "vm-a", days=30)
    assert col.destroyed and col.rewound


@pytest.mark.unit
@pytest.mark.parametrize("days", [0, -1, 366, 1.5, "30"])
def test_out_of_range_windows_are_refused(patched, days) -> None:
    si = _SI(["vm-a"], collector=_Collector([[]]))
    with pytest.raises(ValueError):
        bw.get_backup_snapshot_history(si, "vm-a", days=days)


@pytest.mark.unit
def test_paging_stops_and_does_not_spin(patched) -> None:
    """An always-full collector must terminate, not loop until the session dies."""
    t0 = NOW - timedelta(hours=1)
    page = [_create(t0, t0)]
    col = _Collector([page] * (bw._MAX_PAGES + 50))
    r = bw.get_backup_snapshot_history(_SI(["vm-a"], collector=col), "vm-a", days=30)
    assert r["snapshot_creations"] == bw._MAX_PAGES


@pytest.mark.unit
def test_the_qualifier_travels_with_the_numbers(patched) -> None:
    """The issue's own 'important limitation'. It must be in the payload, not the docs."""
    si = _SI(["vm-a"], collector=_Collector([[]]))
    r = bw.get_backup_snapshot_history(si, "vm-a", days=30)
    assert "lower bound" in r["basis"]
    assert "Not the official job duration" in r["basis"]
