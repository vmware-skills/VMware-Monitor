"""One recent task whose result pyVmomi cannot deserialize must not sink active_tasks.

On a lab vCenter 8.0.3 (2026-09-15) reading ``task.info`` for one recent task
raised ``KeyError('vslmCatalogChangeResult')`` — a result type this pyVmomi does
not know — and the tool failed as a whole, so "are any tasks running?" had no
answer. The readable tasks are still returned, and the unreadable ones are
counted and named rather than silently dropped (an unread task is not "no task").
"""

from __future__ import annotations

from types import SimpleNamespace

from vmware_monitor.ops.activity import get_active_tasks


def _info(state: str, key: str) -> SimpleNamespace:
    return SimpleNamespace(
        state=state,
        error=None,
        descriptionId=f"desc.{key}",
        key=key,
        entityName="vm-1",
        progress=None,
        startTime=None,
        reason=None,
    )


class _ReadableTask:
    def __init__(self, state: str, key: str) -> None:
        self.info = _info(state, key)


class _UnreadableTask:
    @property
    def info(self):
        raise KeyError("vslmCatalogChangeResult")


class _SI:
    def __init__(self, tasks: list) -> None:
        self._content = SimpleNamespace(taskManager=SimpleNamespace(recentTask=tasks))

    def RetrieveContent(self):  # noqa: N802 — pyVmomi naming
        return self._content


def test_readable_tasks_survive_an_unreadable_one():
    si = _SI([_ReadableTask("running", "t1"), _UnreadableTask(), _ReadableTask("success", "t2")])
    result = get_active_tasks(si)
    assert [row["name"] for row in result["items"]] == ["desc.t1", "desc.t2"]
    assert result["total"] == 2
    assert result["unreadable_tasks"] == 1
    assert "vslmCatalogChangeResult" in result["unreadable_note"]


def test_an_auth_fault_is_an_error_not_an_unreadable_task():
    """An expired session raises for every task. Counting those as "unreadable"
    would answer "0 tasks" to a question the tool could not read at all."""
    from pyVmomi import vim

    class _ExpiredTask:
        @property
        def info(self):
            raise vim.fault.NotAuthenticated()

    import pytest

    with pytest.raises(vim.fault.NotAuthenticated):
        get_active_tasks(_SI([_ReadableTask("running", "t1"), _ExpiredTask()]))


def test_no_unreadable_keys_when_every_task_reads():
    result = get_active_tasks(_SI([_ReadableTask("running", "t1")]))
    assert "unreadable_tasks" not in result
    assert "unreadable_note" not in result
