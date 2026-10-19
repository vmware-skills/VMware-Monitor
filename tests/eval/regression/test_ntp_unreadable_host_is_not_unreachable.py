"""NTP: a host this account cannot read is not "unreachable".

Lab vCenter 8.0.3, 2026-10-18, account with Read-only on one VM only: the VM's
host came back with no runtime.connectionState. get_ntp_status said "vCenter
cannot reach this host ... reconnect the host" — sending people to reconnect a
healthy host. Unknown is not a verdict: not reachable, not unreachable.
"""

from __future__ import annotations

import pytest

from vmware_monitor.ops import infra_health

pytestmark = pytest.mark.unit


def _install(monkeypatch, rows):
    def fake_collect(si, obj_type, props):
        return [(object(), r) for r in rows]

    monkeypatch.setattr(infra_health, "_collect", fake_collect)
    monkeypatch.setattr(infra_health, "_collect_objects", lambda *a, **k: [])


def test_unreadable_host_is_not_called_unreachable(monkeypatch):
    _install(monkeypatch, [{"name": "192.168.1.118"}])
    out = infra_health.get_ntp_status(object())
    [row] = out["items"]
    assert row["reachable"] is None and row["healthy"] is None
    assert "cannot read" in row["note"] and "reconnect" not in row["note"]
    assert out["hosts_unreadable"] == 1 and out["hosts_unreachable"] == 0
    assert "unreadable_note" in out and "unreachable_note" not in out


def test_really_disconnected_host_is_still_unreachable(monkeypatch):
    _install(monkeypatch, [{"name": "192.168.1.117", "runtime.connectionState": "notResponding"}])
    out = infra_health.get_ntp_status(object())
    [row] = out["items"]
    assert row["reachable"] is False and "reconnect" in row["note"]
    assert out["hosts_unreachable"] == 1 and out["hosts_unreadable"] == 0
