"""``infra ntp`` notices when hosts take time from different sources.

2026-09-14, lab vCenter 8.0.3: both hosts were reported Healthy, and they were —
each had servers configured and ntpd running. But 192.168.60.15 synchronised
from 192.168.60.74 and 192.168.60.56 from 0/1.pool.ntp.org. Per-host health
cannot see that, and two hosts under one vCenter setting their clocks from
different sources is how clocks drift apart without any host looking wrong.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vmware_monitor.ops import infra_health as ops


def _svc():
    return SimpleNamespace(service=[SimpleNamespace(key="ntpd", running=True, policy="on")])


def _dt(servers):
    return SimpleNamespace(ntpConfig=SimpleNamespace(server=list(servers)))


def _install(monkeypatch, rows):
    """``rows`` is ``[(name, state, servers_or_None)]``."""
    refs = {name: object() for name, _s, _v in rows}
    collected = [
        (
            object(),
            {
                "name": name,
                "runtime.connectionState": state,
                "config.dateTimeInfo": None if servers is None else _dt(servers),
                "configManager.serviceSystem": refs[name],
            },
        )
        for name, state, servers in rows
    ]
    monkeypatch.setattr(ops, "_collect", lambda si, types, paths: collected)
    monkeypatch.setattr(
        ops, "_collect_objects", lambda si, r, kind, paths: [(ref, {"serviceInfo": _svc()}) for ref in r]
    )


@pytest.mark.unit
def test_hosts_on_different_sources_are_flagged(monkeypatch):
    _install(
        monkeypatch,
        [
            ("192.168.60.15", "connected", ["192.168.60.74"]),
            ("192.168.60.56", "connected", ["0.pool.ntp.org", "1.pool.ntp.org"]),
        ],
    )
    out = ops.get_ntp_status(None)
    assert all(r["healthy"] is True for r in out["items"])  # each host alone is fine
    assert out["ntp_sources_consistent"] is False
    note = out["ntp_sources_note"]
    for text in ("192.168.60.15", "192.168.60.74", "192.168.60.56", "0.pool.ntp.org"):
        assert text in note


@pytest.mark.unit
def test_the_same_servers_in_another_order_or_case_agree(monkeypatch):
    _install(
        monkeypatch,
        [
            ("esx-01", "connected", ["ntp1.corp", "ntp2.corp"]),
            ("esx-02", "connected", ["NTP2.corp", "ntp1.corp"]),
        ],
    )
    out = ops.get_ntp_status(None)
    assert out["ntp_sources_consistent"] is True
    assert out.get("ntp_sources_note") is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "rows",
    [
        [("esx-01", "connected", ["ntp1.corp"])],
        # Unreadable and unconfigured hosts are not a second source to compare.
        [("esx-01", "connected", ["ntp1.corp"]), ("esx-02", "notResponding", None)],
        [("esx-01", "connected", ["ntp1.corp"]), ("esx-02", "connected", [])],
    ],
)
def test_fewer_than_two_hosts_with_servers_is_not_a_comparison(monkeypatch, rows):
    _install(monkeypatch, rows)
    out = ops.get_ntp_status(None)
    assert out["ntp_sources_consistent"] is None
    assert out.get("ntp_sources_note") is None


@pytest.mark.unit
def test_cli_prints_the_mismatch(monkeypatch):
    from typer.testing import CliRunner

    from vmware_monitor import cli, cli_observability

    _install(
        monkeypatch,
        [("esx-01", "connected", ["192.168.60.74"]), ("esx-02", "connected", ["0.pool.ntp.org"])],
    )
    monkeypatch.setattr(cli_observability, "get_connection", lambda *_a, **_k: (None, None, "t"))
    monkeypatch.setenv("COLUMNS", "300")
    result = CliRunner().invoke(cli.app, ["infra", "ntp"])
    assert result.exit_code == 0, result.output
    assert "different NTP sources" in " ".join(result.output.split())
