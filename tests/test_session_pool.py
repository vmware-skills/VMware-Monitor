"""D-13(2) acceptance: calls on one target never share a pyVmomi session.

pyVmomi interleaves SOAP on one connection, so once an MCP server runs calls in
worker threads side by side, two calls on one target must hold two sessions.
These tests drive the real ``ConnectionManager`` (its ``SessionPool``) with the
session factory replaced by fakes; nothing touches the network.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from vmware_policy.pool import call_scope

from vmware_monitor import connection as conn_mod
from vmware_monitor.config import AppConfig, TargetConfig
from vmware_monitor.connection import ConnectionManager

pytestmark = pytest.mark.unit

_WAIT = 5.0


class _FakeSI:
    """A session that records whether two calls ever use it at once."""

    def __init__(self, name: str) -> None:
        self.target = name
        self.alive = True
        self.content = MagicMock()
        self.content.sessionManager.currentSession = object()
        self._users = 0
        self._lock = threading.Lock()
        self.overlaps = 0

    def use(self, seconds: float) -> None:
        with self._lock:
            self._users += 1
            if self._users > 1:
                self.overlaps += 1
        time.sleep(seconds)
        with self._lock:
            self._users -= 1


def _manager(monkeypatch: pytest.MonkeyPatch, size: int = 2) -> tuple[ConnectionManager, list]:
    monkeypatch.setenv("VMWARE_MCP_POOL_SIZE", str(size))
    targets = (
        TargetConfig(name="vc1", host="vc1.example.com", config_username="svc"),
        TargetConfig(name="vc2", host="vc2.example.com", config_username="svc"),
    )
    mgr = ConnectionManager(AppConfig(targets=targets))
    created: list[_FakeSI] = []
    lock = threading.Lock()

    def _create(target: TargetConfig) -> _FakeSI:
        si = _FakeSI(target.name)
        with lock:
            created.append(si)
        return si

    monkeypatch.setattr(mgr, "_create_connection", _create)
    monkeypatch.setattr("pyVim.connect.Disconnect", lambda si: None)
    return mgr, created


def _in_call(
    mgr: ConnectionManager,
    target: str | None,
    out: list,
    hold: float = 0.0,
    barrier: threading.Barrier | None = None,
) -> None:
    with call_scope():
        si = mgr.connect(target)
        assert mgr.connect(target) is si  # one call keeps one session per target
        if barrier is not None:
            barrier.wait(_WAIT)
        si.use(hold)
        out.append(si)


def _run(*threads: threading.Thread) -> None:
    for t in threads:
        t.start()
    for t in threads:
        t.join(_WAIT * 2)
        assert not t.is_alive(), "a call never finished"


def test_two_concurrent_calls_on_one_target_get_two_sessions(monkeypatch):
    mgr, created = _manager(monkeypatch)
    out: list[_FakeSI] = []
    barrier = threading.Barrier(2)
    _run(
        *(threading.Thread(target=_in_call, args=(mgr, "vc1", out, 0.2, barrier)) for _ in range(2))
    )
    assert len(out) == 2
    assert out[0] is not out[1], "two calls at once were handed the same session"
    assert all(si.overlaps == 0 for si in created), "a session carried two calls at once"


def test_many_calls_never_share_a_session(monkeypatch):
    mgr, created = _manager(monkeypatch)
    out: list[_FakeSI] = []
    _run(*(threading.Thread(target=_in_call, args=(mgr, "vc1", out, 0.05)) for _ in range(8)))
    assert len(out) == 8
    assert len(created) <= 2, "the pool opened more sessions than its size"
    assert all(si.overlaps == 0 for si in created)


def test_a_long_call_leaves_the_other_session_usable(monkeypatch):
    mgr, _ = _manager(monkeypatch)
    started, release = threading.Event(), threading.Event()
    held: list[_FakeSI] = []

    def long_call() -> None:
        with call_scope():
            held.append(mgr.connect("vc1"))
            started.set()
            release.wait(_WAIT)

    t = threading.Thread(target=long_call)
    t.start()
    assert started.wait(_WAIT)
    try:
        for _ in range(3):  # short calls keep getting served while the long one holds
            out: list[_FakeSI] = []
            begin = time.monotonic()
            _run(threading.Thread(target=_in_call, args=(mgr, "vc1", out)))
            assert time.monotonic() - begin < 1.0, "a short call waited on the long one"
            assert out[0] is not held[0]
    finally:
        release.set()
        t.join(_WAIT)


def test_calls_on_different_targets_overlap(monkeypatch):
    mgr, created = _manager(monkeypatch, size=1)
    out: list[_FakeSI] = []
    barrier = threading.Barrier(2)  # both must hold their session at the same moment
    _run(
        threading.Thread(target=_in_call, args=(mgr, "vc1", out, 0.05, barrier)),
        threading.Thread(target=_in_call, args=(mgr, "vc2", out, 0.05, barrier)),
    )
    assert sorted(si.target for si in out) == ["vc1", "vc2"]
    assert not barrier.broken


def test_outside_a_call_scope_one_session_is_reused(monkeypatch):
    mgr, created = _manager(monkeypatch)
    first = mgr.connect("vc1")
    assert mgr.connect("vc1") is first
    assert len(created) == 1
    assert mgr.list_connected() == ["vc1"]


def test_no_target_pools_under_the_default_targets_name(monkeypatch):
    mgr, _ = _manager(monkeypatch)
    out: list[_FakeSI] = []
    _run(threading.Thread(target=_in_call, args=(mgr, None, out)))
    assert out[0].target == "vc1"
    assert mgr.list_connected() == ["vc1"]
    assert mgr.connect(None) is mgr.connect("vc1")


def test_a_dead_session_is_closed_unregistered_and_replaced(monkeypatch):
    """Through the real _create_connection: only SmartConnect/Disconnect are faked."""
    monkeypatch.setenv("VMWARE_VC1_PASSWORD", "test-only")
    monkeypatch.setenv("VMWARE_MCP_POOL_SIZE", "2")
    target = TargetConfig(name="vc1", host="vc1.example.com", config_username="svc")
    mgr = ConnectionManager(AppConfig(targets=(target,)))

    def _smart_connect(**_kw):
        si = MagicMock()
        si.content.sessionManager.currentSession = object()
        return si

    with (
        patch("pyVim.connect.SmartConnect", side_effect=_smart_connect),
        patch("pyVim.connect.Disconnect") as disconnect,
        patch("atexit.unregister") as unregister,
    ):
        with call_scope():
            dead = mgr.connect("vc1")
        handler = conn_mod._SI_ATEXIT[id(dead)]
        dead.content.sessionManager.currentSession = None  # token expired
        with call_scope():
            fresh = mgr.connect("vc1")
        assert fresh is not dead
        disconnect.assert_any_call(dead)
        unregister.assert_any_call(handler)
        assert id(dead) not in conn_mod._SI_ATEXIT
        mgr.disconnect_all()
    assert mgr.list_connected() == []


def test_first_calls_at_once_build_one_manager(monkeypatch):
    """Two first calls racing must not build two managers (and two pools)."""
    from vmware_monitor.mcp_server import server

    target = TargetConfig(name="vc1", host="vc1.example.com", config_username="svc")

    def _slow_load(*_a, **_kw):
        time.sleep(0.1)
        return AppConfig(targets=(target,))

    monkeypatch.setattr(server, "_conn_mgr", None)
    monkeypatch.setattr(server, "load_config", _slow_load)
    got: list[ConnectionManager] = []
    _run(
        *(threading.Thread(target=lambda: got.append(server._ensure_conn_mgr())) for _ in range(4))
    )
    assert len(got) == 4
    assert all(m is got[0] for m in got), "racing first calls built more than one manager"
