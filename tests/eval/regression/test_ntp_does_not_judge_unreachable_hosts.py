"""``infra ntp`` must not report an unreachable host as misconfigured.

Real-hardware finding, 2026-08-30 (VCF 9.1, 4 of 8 hosts ``notResponding``): the
four unreachable hosts came back with ``ntp_servers: []``, ``ntpd_running:
false`` and ``healthy: false`` — the same row a genuinely misconfigured host
produces. An operator reading it would go and configure NTP on four machines
that may already have it.

Nothing in the read failed loudly. ``config.dateTimeInfo`` is absent for such a
host and its ``HostServiceSystem`` cannot be reached, so the defaults —
``servers = []`` and ``running = False`` — supplied the verdict. Every default
here was a guess written as a measurement.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vmware_monitor.ops import infra_health as ops


def _svc(running: bool = True, policy: str = "on"):
    return SimpleNamespace(
        service=[SimpleNamespace(key="ntpd", running=running, policy=policy)]
    )


def _dt(servers):
    return SimpleNamespace(ntpConfig=SimpleNamespace(server=list(servers)))


def _install(monkeypatch, rows, service_info):
    """``rows`` is ``[(name, state, dateTimeInfo, svc_ref)]``."""
    collected = [
        (
            object(),
            {
                "name": name,
                "runtime.connectionState": state,
                "config.dateTimeInfo": dt,
                "configManager.serviceSystem": ref,
            },
        )
        for name, state, dt, ref in rows
    ]
    monkeypatch.setattr(ops, "_collect", lambda si, types, paths: collected)
    monkeypatch.setattr(
        ops,
        "_collect_objects",
        lambda si, refs, kind, paths: [
            (ref, {"serviceInfo": service_info[ref]}) for ref in refs
        ],
    )


@pytest.mark.unit
def test_an_unreachable_host_is_unknown_not_unhealthy(monkeypatch):
    good_ref, gone_ref = object(), object()
    _install(
        monkeypatch,
        [
            ("esx-01", "connected", _dt(["10.0.0.1"]), good_ref),
            ("esx-gone", "notResponding", None, gone_ref),
        ],
        {good_ref: _svc(), gone_ref: None},
    )

    rows = {r["host"]: r for r in ops.get_ntp_status(None)["items"]}

    gone = rows["esx-gone"]
    assert gone["reachable"] is False
    assert gone["healthy"] is None, (
        "False is a verdict — it says NTP is misconfigured on this host, which "
        "was never observed"
    )
    assert gone["ntp_servers"] is None, (
        "[] reads as 'no NTP servers are configured', a claim about the host"
    )
    assert gone["ntpd_running"] is None
    assert "notResponding" in gone["note"]


@pytest.mark.unit
def test_a_reachable_host_still_gets_a_verdict(monkeypatch):
    """The control: a fix that reports everything as unknown would pass the test
    above and make the tool useless."""
    ok, bad = object(), object()
    _install(
        monkeypatch,
        [
            ("esx-01", "connected", _dt(["10.0.0.1"]), ok),
            ("esx-02", "connected", _dt([]), bad),
        ],
        {ok: _svc(), bad: _svc(running=False, policy="off")},
    )

    rows = {r["host"]: r for r in ops.get_ntp_status(None)["items"]}

    assert rows["esx-01"]["reachable"] is True
    assert rows["esx-01"]["healthy"] is True
    assert rows["esx-01"]["ntp_servers"] == ["10.0.0.1"]
    # A connected host with no NTP servers really is misconfigured. This is the
    # finding the tool exists to produce and it must survive the fix.
    assert rows["esx-02"]["healthy"] is False
    assert rows["esx-02"]["ntp_servers"] == []


@pytest.mark.unit
def test_the_envelope_says_how_many_hosts_went_unread(monkeypatch):
    """A caller scanning for ``healthy: false`` sees nothing wrong on an estate
    where half the hosts were never asked. The count has to be on the envelope,
    not only inside the rows."""
    a, b, c = object(), object(), object()
    _install(
        monkeypatch,
        [
            ("esx-01", "connected", _dt(["10.0.0.1"]), a),
            ("esx-02", "notResponding", None, b),
            ("esx-03", "disconnected", None, c),
        ],
        {a: _svc(), b: None, c: None},
    )

    out = ops.get_ntp_status(None)

    assert out["hosts_unreachable"] == 2
    # Its own key: the envelope's `hint` means "this page was truncated", and
    # overloading it would make a complete page look like a partial one.
    assert "2" in out["unreachable_note"]
    assert "unreachable" in out["unreachable_note"].lower()


@pytest.mark.unit
def test_a_fully_reachable_estate_carries_no_warning(monkeypatch):
    a = object()
    _install(monkeypatch, [("esx-01", "connected", _dt(["10.0.0.1"]), a)], {a: _svc()})

    out = ops.get_ntp_status(None)

    assert out["hosts_unreachable"] == 0
    assert "unreachable_note" not in out, (
        "a warning printed on every clean run is a warning nobody reads on the "
        "run that matters"
    )
