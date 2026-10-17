"""Backup history on a standalone ESXi host, and the except clause that hid it.

Real hardware, 2026-10-17 (ESXi 192.168.1.118): ``CreateCollectorForTasks``
raises ``vmodl.fault.NotSupported`` on an ESXi host. The handler below the
``NoPermission`` one named ``vim.fault.InvalidArgument`` -- a class pyVmomi does
not have (it is ``vmodl.fault.InvalidArgument``). Except clauses are evaluated
when an exception is being matched, so the lookup itself raised
``AttributeError: InvalidArgument`` and the user saw "AttributeError: operation
failed." instead of an answer (handbook pitfall #40, same shape).
"""

from __future__ import annotations

import pytest
from pyVmomi import vmodl

from vmware_monitor.ops import backup_window as bw

from .test_backup_window import _SI, patched  # noqa: F401 -- fixture reuse


@pytest.mark.unit
def test_esxi_not_supported_is_history_unavailable_not_a_crash(patched) -> None:  # noqa: F811
    si = _SI(["vm-a"], create_raises=vmodl.fault.NotSupported())
    r = bw.get_backup_snapshot_history(si, "vm-a", days=7)

    note = r["history_unavailable"]
    assert note is not None
    assert "vCenter" in note and "ESXi" in note
    assert r["complete_cycles"] == 0
    assert r["coverage_note"] is None
    assert r["window_fully_covered"] is None


@pytest.mark.unit
def test_rejected_filter_is_history_unavailable(patched) -> None:  # noqa: F811
    si = _SI(["vm-a"], create_raises=vmodl.fault.InvalidArgument(msg="bad filter"))
    r = bw.get_backup_snapshot_history(si, "vm-a", days=7)

    assert r["history_unavailable"] is not None
    assert "bad filter" in r["history_unavailable"]
