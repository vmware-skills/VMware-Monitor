"""Regression — session liveness probe must evict dead cached sessions.

Locks ConnectionManager.connect() against two dead-session shapes surfaced by
an external fork report (VMware-AIops PR #32, 2026-07; Monitor shared the
identical probe):

1. The probe handler was ``except (vmodl.fault.NotAuthenticated, Exception)``
   — but ``vmodl.fault.NotAuthenticated`` does not exist in pyVmomi (the real
   class is ``vim.fault.NotAuthenticated``), and except-tuples are evaluated
   at catch time. The handler itself raised ``AttributeError``, eviction never
   ran, and every later call on the dead session permafailed until restart.
2. An expired token can make ``sessionManager.currentSession`` return ``None``
   without raising — a dead session that a raise-only probe treats as live.

Three shapes pinned: probe raises -> evict + reconnect; probe returns None ->
evict + reconnect; live session -> cache preserved (no reconnect).

The cache is seeded through connect() itself (the manager keeps its sessions in
a vmware_policy SessionPool since D-13(2)), and the evicted session must be
closed: its atexit cleanup taken back and Disconnect called.
"""

from unittest.mock import MagicMock, patch

from vmware_monitor.config import AppConfig, TargetConfig
from vmware_monitor.connection import ConnectionManager


def _seed(mgr: ConnectionManager, si: MagicMock) -> None:
    """Make ``si`` the target's cached session the way production does: connect once."""
    with patch.object(mgr, "_create_connection", return_value=si):
        assert mgr.connect("vc1") is si


def _mgr() -> ConnectionManager:
    target = TargetConfig(name="vc1", host="vc.example.com", config_username="admin@vsphere.local")
    return ConnectionManager(AppConfig(targets=(target,)))


def _live_si() -> MagicMock:
    si = MagicMock()
    si.content.sessionManager.currentSession = MagicMock()
    return si


def test_probe_exception_evicts_and_reconnects():
    mgr = _mgr()
    dead_si = MagicMock()
    dead_si.content.sessionManager.currentSession = MagicMock()
    _seed(mgr, dead_si)
    type(dead_si).content = property(
        lambda self: (_ for _ in ()).throw(Exception("session dead"))
    )

    fresh_si = _live_si()
    with patch.object(mgr, "_create_connection", return_value=fresh_si) as create, \
         patch("pyVim.connect.Disconnect") as disconnect:
        result = mgr.connect("vc1")

    assert result is fresh_si
    create.assert_called_once()
    disconnect.assert_called_once_with(dead_si)
    with patch.object(mgr, "_create_connection") as again:
        assert mgr.connect("vc1") is fresh_si
    again.assert_not_called()


def test_probe_none_session_evicts_and_reconnects():
    mgr = _mgr()
    dead_si = MagicMock()
    dead_si.content.sessionManager.currentSession = MagicMock()
    _seed(mgr, dead_si)
    dead_si.content.sessionManager.currentSession = None

    fresh_si = _live_si()
    with patch.object(mgr, "_create_connection", return_value=fresh_si) as create, \
         patch("pyVim.connect.Disconnect") as disconnect:
        result = mgr.connect("vc1")

    assert result is fresh_si
    create.assert_called_once()
    disconnect.assert_called_once_with(dead_si)
    with patch.object(mgr, "_create_connection") as again:
        assert mgr.connect("vc1") is fresh_si
    again.assert_not_called()


def test_live_session_reused_without_reconnect():
    mgr = _mgr()
    live_si = _live_si()
    _seed(mgr, live_si)

    with patch.object(mgr, "_create_connection") as create:
        result = mgr.connect("vc1")

    assert result is live_si
    create.assert_not_called()
