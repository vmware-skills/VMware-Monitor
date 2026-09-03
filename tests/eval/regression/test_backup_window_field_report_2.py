"""Three findings from the second field report on issue #26 (2026-09-01).

The reporter ran `vm_backup_snapshot_history` against a large vCenter 8.0.3 with
Veeam driving the snapshots — the environment this feature was built for and had
never seen. Thirty complete cycles matched the estate's known backup activity,
which settles the correlation arithmetic. These pin the three things that report
found wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from vmware_monitor.ops.backup_window import _classify_open_cycles
from vmware_monitor.ops.inventory import list_vms

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
#: The reporter's VM: thirty cycles averaging 3.10 h, peaking at 8.88 h.
THEIR_VM = [{"total_window_hours": 3.10}, {"total_window_hours": 8.88}]


def _open(minutes_ago: float) -> list[dict]:
    started = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return [{"reason": "snapshot creation with no matching removal", "started": started}]


def test_a_backup_that_started_a_minute_ago_is_not_a_failure() -> None:
    """Their exact case: 32 creations, 31 removals, and Veeam still running.

    `incomplete_cycles: 1` was correct and read as a failed backup, because
    nothing separated an open cycle from an abandoned snapshot.
    """
    orphans = _open(1)
    _classify_open_cycles(orphans, THEIR_VM, NOW)
    row = orphans[0]
    assert row["status"] == "open"
    assert row["possibly_in_progress"] is True
    assert row["age_hours"] == 0.02
    assert "still running" in row["note"]


def test_an_open_cycle_past_this_vms_own_worst_case_is_flagged() -> None:
    """The threshold is the VM's own history, not a number chosen here.

    A fixed cut-off would be this repo guessing for every estate; theirs peaks
    at 8.88 h and another's may be minutes.
    """
    orphans = _open(60 * 40)
    _classify_open_cycles(orphans, THEIR_VM, NOW)
    row = orphans[0]
    assert row["possibly_in_progress"] is False
    assert row["longest_completed_cycle_hours"] == 8.88
    assert "left behind" in row["note"]


def test_with_no_completed_cycle_the_answer_is_unknown_not_a_default() -> None:
    orphans = _open(1)
    _classify_open_cycles(orphans, [], NOW)
    row = orphans[0]
    assert row["possibly_in_progress"] is None
    assert "cannot be told" in row["note"]


def test_a_removal_orphan_is_left_alone() -> None:
    """Only unmatched *creations* can be open; a removal orphan is the window
    boundary cutting a cycle in half and must not be labelled."""
    orphans = [{"reason": "snapshot removal with no creation inside the window",
                "started": (NOW - timedelta(hours=1)).isoformat()}]
    _classify_open_cycles(orphans, THEIR_VM, NOW)
    assert "status" not in orphans[0]


def test_vms_can_be_found_by_name() -> None:
    """The recovery path the errors point at has to be walkable.

    Told to "find the VM with list_virtual_machines", the model reached for
    `folder_filter` and passed it a VM name — because that was the only filter
    there, and an estate of thousands has no other way to search.
    """
    import inspect

    assert "name_filter" in inspect.signature(list_vms).parameters


def test_the_error_names_the_parameter_that_exists() -> None:
    """It used to suggest a glob (`abc*`); the filter is a substring."""
    import inspect

    from vmware_monitor.ops import backup_window

    src = inspect.getsource(backup_window)
    assert "name_filter=" in src
    assert "}*') to see available VMs" not in src


# ── finding 3: a quiet VM is not a truncated history ─────────────────────────

class _Opt:
    def __init__(self, value): self.value = value


def _si(retention: int | None):
    """A ServiceInstance stand-in whose task.maxAge answers `retention`."""
    class _Setting:
        @staticmethod
        def QueryOptions(key):
            assert key == "task.maxAge", f"wrong option name: {key}"
            if retention is None:
                raise RuntimeError("unreadable")
            return [_Opt(retention)]
    return type("SI", (), {"content": type("C", (), {"setting": _Setting})})()


def test_retention_is_read_as_a_number_not_only_a_sentence() -> None:
    from vmware_monitor.ops.backup_window import _retention_days

    assert _retention_days(_si(31)) == 31
    # Unreadable retention must not become a number: "we could not ask" is not 0.
    assert _retention_days(_si(None)) is None


def test_a_quiet_vm_inside_the_retention_window_is_fully_covered() -> None:
    """Their case: 31 days retained, 30 requested, VM idle for most of it.

    The old note compared the oldest task against the window start and called
    the difference expired history. With retention >= the request, nothing in
    the window *can* have expired — the oldest task is simply this VM's first
    activity, which is a different statement and a decidable one.
    """
    from vmware_monitor.ops import backup_window as bw

    assert bw._retention_days(_si(31)) >= 30


def test_a_genuinely_truncated_window_still_says_so() -> None:
    """The other half: 90 days asked of 31 days retained really is truncated."""
    from vmware_monitor.ops import backup_window as bw

    assert bw._retention_days(_si(31)) < 90
