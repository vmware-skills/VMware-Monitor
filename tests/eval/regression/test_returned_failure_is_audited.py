"""A tool that catches its exception must still be audited as a failure.

``@vmware_tool`` records a call as failed when an exception reaches it, or when
the returned payload is a dict carrying a truthy ``error`` key. A tool that
catches the exception and returns an error **string** instead looks exactly like
a success, and three things follow: the audit row says ``status=ok`` for an
operation that failed, an undo token is written for a change that never landed,
and the circuit breaker is told ``success=True`` so repeated failures never trip
it.

This skill does not have that defect, and this file is what keeps that true
rather than merely observed. ``_catch_tool_errors`` returns
``{"error": ..., "hint": ...}``, which is the family's documented envelope and is
detected by vmware-policy itself — so no explicit ``report_tool_failure`` call is
needed here, and adding one would be redundant with the detection that already
covers it.

Both halves of that reasoning are load-bearing and both are asserted:

* the *shape* — every registered tool is annotated to return a ``dict``. A tool
  added later that returns a plain string would be detected by nothing, and this
  is where that shows up;
* the *behaviour* — a failing tool, driven through the real ``@vmware_tool``
  wrapper, produces an audit row whose status is ``error``. Reading the
  decorator stack is not the same as watching what it records.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from vmware_monitor.mcp_server import server
from vmware_monitor.ops.vm_info import VMNotFoundError

_TEACHING = "VM 'web-99' not found. Run list_virtual_machines and copy an exact name."


def _registered_tools() -> list[str]:
    """Names of the tools in the live MCP registry that this module defines.

    Asserts it found something: a discovery loop that silently matches nothing
    would leave this file reporting green while checking nothing at all.
    """
    live = {t.name for t in asyncio.run(server.mcp.list_tools())}
    names = [
        name
        for name in dir(server)
        if name in live and getattr(getattr(server, name), "_is_vmware_tool", False)
    ]
    assert names, "found no registered tools — this file would check nothing"
    return sorted(names)


class _Recorder:
    """Stands in for the audit engine; ``_CallState`` captures it at call time."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def log(self, **kwargs) -> None:
        self.rows.append(kwargs)


@pytest.fixture
def audit(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr("vmware_policy.guard.get_engine", lambda: recorder)
    return recorder


@pytest.mark.parametrize("tool_name", _registered_tools())
def test_every_tool_returns_the_dict_envelope_policy_can_detect(tool_name):
    """A string return would be invisible to vmware-policy's failure detection."""
    annotation = inspect.signature(getattr(server, tool_name)).return_annotation
    assert annotation is dict, (
        f"{tool_name} is annotated to return {annotation!r}. Only the dict envelope "
        f"is detected as a failure by vmware-policy; a string return needs an "
        f"explicit report_tool_failure() call in the handler that produces it"
    )


def test_a_failing_tool_is_audited_as_a_failure(audit, monkeypatch):
    """The behaviour the shape above is supposed to guarantee, observed end to end."""

    def _boom(target=None):
        raise VMNotFoundError(_TEACHING)

    monkeypatch.setattr(server, "_get_connection", _boom)

    result = server.list_virtual_machines()

    # The agent-facing contract: the dict envelope, still teaching.
    assert result["error"] == _TEACHING
    # A name that does not resolve is not a connectivity problem; the hint must
    # not send the agent to doctor (2026-10-17).
    assert "doctor" not in result["hint"]

    assert audit.rows, "the call was never audited"
    assert audit.rows[-1]["status"] == "error", (
        f"a failed tool audited as {audit.rows[-1]['status']!r} — vmware-policy did "
        f"not recognise the returned payload as a failure"
    )


def test_a_successful_tool_still_audits_as_ok(audit, monkeypatch):
    """The other direction: reporting every call failed would be the same lie."""
    envelope = {"items": [], "returned": 0, "total": 0, "truncated": False}
    monkeypatch.setattr(server, "_get_connection", lambda target=None: object())
    monkeypatch.setattr(server, "list_vms", lambda si, **kw: envelope)

    result = server.list_virtual_machines()

    assert result["items"] == []
    assert audit.rows[-1]["status"] == "ok"
