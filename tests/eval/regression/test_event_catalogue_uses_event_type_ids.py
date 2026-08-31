"""Extended event descriptions must key on their id, not their pyVmomi class.

``EventManager.description.eventInfo`` publishes two populations. Classic VMODL
events carry a distinct class in ``key``. Extended events — 1755 of the 2196
entries on a live vCenter 8.0.3 — all carry the *same* class
(``vim.event.EventEx`` / ``vim.event.ExtendedEvent``), so keying on the class
name collapsed 1755 distinct descriptions onto 2 buckets and threw away 80% of
what vCenter said about itself.

Their real id sits ahead of a ``|`` in ``fullFormat``, and it is the same value
``Event.eventTypeId`` reports, which is what :func:`_event_key` already looks up.
"""

from __future__ import annotations

from vmware_monitor.ops.health import _catalogue_and_coverage, _catalogue_entry_key


class _Detail:
    def __init__(self, key_name: str | None, category: str, full: str) -> None:
        self.key = type(key_name, (), {}) if key_name else None
        self.category = category
        self.fullFormat = full


class _Mgr:
    def __init__(self, details: list[_Detail]) -> None:
        self.description = type("D", (), {"eventInfo": details})()


def test_an_extended_entry_keys_on_its_id_not_its_class() -> None:
    detail = _Detail(
        "vim.event.EventEx",
        "error",
        "esx.problem.scsi.device.io.latency.high|Device {1} performance has deteriorated.",
    )
    assert _catalogue_entry_key(detail) == "esx.problem.scsi.device.io.latency.high"


def test_a_classic_entry_still_keys_on_its_class() -> None:
    """441 classic entries have prose in fullFormat and no pipe at all.

    Applying the pipe rule to them would break the half that already worked.
    """
    detail = _Detail(
        "vim.event.AccountCreatedEvent",
        "info",
        "Account {spec.id} was created on host {host.name}",
    )
    assert _catalogue_entry_key(detail) == "vim.event.AccountCreatedEvent"


def test_prose_containing_a_pipe_is_not_mistaken_for_an_id() -> None:
    """The guard is "no spaces": vCenter ids are dotted tokens, never sentences."""
    detail = _Detail("vim.event.SomeEvent", "info", "Either A | or B happened")
    assert _catalogue_entry_key(detail) == "vim.event.SomeEvent"


def test_extended_entries_no_longer_collapse_onto_one_key() -> None:
    """The whole point: distinct descriptions must stay distinct.

    Before, both of these landed on "vim.event.EventEx", disagreed on category,
    and were therefore dropped as ambiguous — so a real esx.problem.* event got
    no rank from the catalogue at all.
    """
    details = [
        _Detail("vim.event.EventEx", "error", "esx.problem.a.b|Thing {1} broke."),
        _Detail("vim.event.EventEx", "info", "com.vmware.cis.CreatePermission|Made {User}."),
    ]
    catalogue, coverage = _catalogue_and_coverage(_Mgr(details))
    assert catalogue == {"esx.problem.a.b": "error", "com.vmware.cis.CreatePermission": "info"}
    assert coverage == {"described": 2, "usable": 2, "ambiguous": 0}


def test_an_entry_with_no_usable_id_still_falls_back_rather_than_vanishing() -> None:
    detail = _Detail(None, "info", "no pipe here")
    assert _catalogue_entry_key(detail) == ""
    catalogue, coverage = _catalogue_and_coverage(_Mgr([detail]))
    assert catalogue == {} and coverage["described"] == 0
