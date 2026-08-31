"""Two thousand event descriptions must not collapse into one confident answer.

Real-hardware finding, round 3 (VCF 9.1, 2026-08-30/31): vCenter published 2328
event descriptions and the catalogue this skill built from them held 443 keys.
41 of 50 events on the estate came back ``severity="unknown"``, every one an
``esx.problem.*`` -- including ``esx.problem.visorfs.ramdisk.full`` ("The
ramdisk 'vsantraceFailover' is full."), 41 times, on esxi03.

The mechanism is pyVmomi's VMODL: ``EventDescription.EventDetail.key`` is
declared as a *type*, so a description arrives as a class object, and the
thousands of distinct ``esx.problem.*`` types share the one class
``vim.event.EventEx``. Writing them all into ``catalogue[key]`` meant the last
description parsed won, and every event under that key inherited its category.
That is not a gap, it is a wrong answer stated with confidence.

Three things are pinned here:

* an ambiguous key is dropped rather than overwritten;
* every rank says where it came from, because "vCenter called this info" and
  "nothing could rank this" are not the same fact;
* an ``esx.problem.*`` type nothing else ranks is not silently calm.

The fixtures below use real pyVmomi classes for ``key``. Mocking it as a string
is what let the v1.8.14 regression through -- validating in an environment
where the defect cannot appear (形态 #3).
"""

from __future__ import annotations

from types import SimpleNamespace

from pyVmomi import vim

from vmware_monitor.ops import health as ops


class _EventMgr:
    """EventManager stand-in whose ``eventInfo`` is a list, so keys may repeat.

    A dict of ``{key: category}`` cannot express the defect at all: the collapse
    happens because many *entries* share one key.
    """

    def __init__(self, events, entries):
        self._events = events
        self.description = SimpleNamespace(
            eventInfo=[SimpleNamespace(key=k, category=c) for k, c in entries]
        )

    def QueryEvents(self, _spec):  # noqa: N802 - mirrors pyVmomi's own name
        return self._events


def _class_key(cls):
    """The catalogue key for an entry that carries no extended id.

    Entries in this file's fixtures have no ``fullFormat``, so they still key on
    the pyVmomi class — the fallback branch of ``_catalogue_entry_key``. That
    branch is not dead: on a live 8.0.3, 441 classic descriptions take it. What
    changed is that real ``EventEx`` entries no longer land here, because their
    ids are recoverable (see test_event_catalogue_uses_event_type_ids.py); the
    ambiguity rule below still has to hold for whatever cannot be told apart.
    """
    return cls.__name__


def _si(event_mgr):
    return SimpleNamespace(
        RetrieveContent=lambda: SimpleNamespace(eventManager=event_mgr)
    )


def _ex(event_type_id, *, severity=None, message="m"):
    """An EventEx as QueryEvents returns it: identity in ``eventTypeId``."""
    ev = vim.event.EventEx()
    ev.eventTypeId = event_type_id
    ev.fullFormattedMessage = message
    ev.createdTime = __import__("datetime").datetime(2026, 8, 31)
    ev.userName = ""
    if severity is not None:
        ev.severity = severity
    return ev


def test_the_vmodl_still_declares_key_as_a_type():
    """The premise. If pyVmomi ever makes this a string, the rest is obsolete."""
    prop = {
        p.name: p.type
        for p in vim.event.EventDescription.EventDetail._GetPropertyList()
    }
    assert prop["key"] is type, (
        "EventDetail.key is no longer declared as a type -- re-derive the "
        "catalogue keying before trusting these tests"
    )


def test_disagreeing_entries_under_one_key_are_dropped_not_overwritten():
    mgr = _EventMgr(
        [],
        [
            (vim.event.EventEx, "info"),
            (vim.event.EventEx, "error"),
            (vim.event.VmPoweredOnEvent, "info"),
        ],
    )
    catalogue, coverage = ops._catalogue_and_coverage(mgr)

    assert _class_key(vim.event.EventEx) not in catalogue, (
        "the ambiguous key survived -- an esx.problem.* event would inherit "
        "whichever description happened to be parsed last"
    )
    assert catalogue[_class_key(vim.event.VmPoweredOnEvent)] == "info"
    assert coverage == {"described": 3, "usable": 1, "ambiguous": 1}


def test_agreeing_duplicates_are_still_usable():
    """Dropping every repeated key would throw away answers we actually have."""
    mgr = _EventMgr([], [(vim.event.EventEx, "error"), (vim.event.EventEx, "error")])
    catalogue, coverage = ops._catalogue_and_coverage(mgr)
    assert catalogue[_class_key(vim.event.EventEx)] == "error"
    assert coverage["ambiguous"] == 0


def test_a_ramdisk_full_event_is_not_reported_as_calm():
    """The exact event from the estate, with nothing able to rank it."""
    mgr = _EventMgr(
        [_ex("esx.problem.visorfs.ramdisk.full", message="The ramdisk is full.")],
        [(vim.event.EventEx, "info"), (vim.event.EventEx, "error")],
    )
    out = ops.get_recent_events(_si(mgr), severity="warning")

    assert out["returned"] == 1, "the estate's real problem event was filtered out"
    row = out["items"][0]
    assert row["severity"] == "warning"
    assert row["severity_source"] == "name_prefix"


def test_the_events_own_severity_wins_over_the_catalogue():
    mgr = _EventMgr(
        [_ex("esx.problem.net.connectivity.lost", severity="error")],
        [(vim.event.EventEx, "info")],
    )
    out = ops.get_recent_events(_si(mgr), severity="critical")
    assert out["items"][0]["severity"] == "critical"
    assert out["items"][0]["severity_source"] == "event"


def test_an_unrankable_non_problem_event_stays_unknown():
    """The name-prefix rule must not become a blanket guess."""
    mgr = _EventMgr([_ex("com.vendor.SomethingNobodyDescribed")], [])
    out = ops.get_recent_events(_si(mgr), severity="info")
    assert out["items"][0]["severity"] == "unknown"
    assert out["items"][0]["severity_source"] == "unclassified"
    assert out["unclassified"] == 1


def test_discarded_descriptions_are_reported_not_hidden():
    mgr = _EventMgr(
        [_ex("esx.problem.x")],
        [(vim.event.EventEx, "info"), (vim.event.EventEx, "error")],
    )
    out = ops.get_recent_events(_si(mgr), severity="info")
    note = out.get("catalogue_coverage", "")
    assert "2" in note and "discarded" in note, (
        "a caller reading a short unclassified count cannot tell a quiet estate "
        "from a blind one unless the discarded descriptions are stated"
    )


def test_a_clean_catalogue_says_nothing():
    """A note printed on every run is a note nobody reads on the run that matters."""
    mgr = _EventMgr([_ex("esx.problem.x")], [(vim.event.EventEx, "error")])
    out = ops.get_recent_events(_si(mgr), severity="info")
    assert "catalogue_coverage" not in out
