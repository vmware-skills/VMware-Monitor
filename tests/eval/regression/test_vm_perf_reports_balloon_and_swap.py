"""Regression — VM performance reports ballooned and swapped memory, and names Mem %.

Found 2026-09-15 on a lab vCenter 8.0.3 while chasing the vCenter alarm
"Memory Exhaustion on 192". Broadcom KB 430034's first diagnostic for vCenter
appliance memory exhaustion is ballooning, and ``perf vms`` could not show it:
the table had CPU %, "Mem %", consumed MB and disk/net — no balloon, no swap —
and did not say which counter "Mem %" was. For a VM that counter is
``mem.usage.average``: *active* guest memory as a share of configured memory, so
a VM under balloon pressure can read a comfortable 19%.

Pinned: ``mem_ballooned_mb`` (``mem.vmmemctl.average``) and ``mem_swapped_mb``
(``mem.swapped.average``) on every row that reports them, a ``counters`` map in the
envelope naming the counter behind each field, the CLI columns, and the MCP
tool's docstring.
"""

from __future__ import annotations

import types

import pytest
from pyVmomi import vim
from typer.testing import CliRunner

from vmware_monitor.cli import app
from vmware_monitor.mcp_server import server
from vmware_monitor.ops import performance

runner = CliRunner()

_COUNTERS = {
    1: ("cpu", "usage", "average"),
    2: ("mem", "usage", "average"),
    3: ("mem", "consumed", "average"),
    4: ("mem", "vmmemctl", "average"),
    5: ("mem", "swapped", "average"),
}
#: Raw values as vSphere reports them: hundredths of a percent, or KB.
_RAW = {1: 639, 2: 1932, 3: 14678016, 4: 1048576, 5: 2048}


class _PerfManager:
    def __init__(self) -> None:
        self.perfCounter = [
            types.SimpleNamespace(
                groupInfo=types.SimpleNamespace(key=g),
                nameInfo=types.SimpleNamespace(key=n),
                rollupType=r,
                key=k,
            )
            for k, (g, n, r) in _COUNTERS.items()
        ]

    def QueryPerfProviderSummary(self, entity):  # noqa: N802 - pyVmomi contract
        return types.SimpleNamespace(currentSupported=True, refreshRate=20)

    def QueryPerf(self, querySpec):  # noqa: N802, N803 - pyVmomi contract
        wanted = {m.counterId for m in querySpec[0].metricId}
        metrics = [
            types.SimpleNamespace(id=types.SimpleNamespace(counterId=k), value=[v])
            for k, v in _RAW.items()
            if k in wanted
        ]
        return [types.SimpleNamespace(value=metrics)]


def _si():
    content = types.SimpleNamespace(perfManager=_PerfManager())
    return types.SimpleNamespace(RetrieveContent=lambda: content)


@pytest.fixture
def one_vm(monkeypatch):
    vm = vim.VirtualMachine("vm-vcsa")
    monkeypatch.setattr(
        performance,
        "_collect",
        lambda si, types_, paths: [(vm, {"name": "vcsa", "runtime.powerState": "poweredOn"})],
    )
    return vm


@pytest.mark.unit
def test_rows_carry_ballooned_and_swapped_memory(one_vm):
    out = performance.get_vm_performance(_si())
    row = out["items"][0]
    assert row["mem_ballooned_mb"] == 1024.0
    assert row["mem_swapped_mb"] == 2.0
    assert row["mem_usage_pct"] == 19.32


@pytest.mark.unit
def test_the_envelope_names_the_counter_behind_each_field(one_vm):
    counters = performance.get_vm_performance(_si())["counters"]
    assert counters["mem_ballooned_mb"].startswith("mem.vmmemctl.average")
    assert counters["mem_swapped_mb"].startswith("mem.swapped.average")
    assert counters["mem_usage_pct"].startswith("mem.usage.average")
    assert "active" in counters["mem_usage_pct"].lower()


@pytest.mark.unit
def test_cli_perf_vms_shows_balloon_swap_and_labels_mem(monkeypatch, one_vm):
    monkeypatch.setattr(
        "vmware_monitor.cli_observability.get_connection", lambda t, c: (_si(), None, "lab")
    )
    result = runner.invoke(app, ["perf", "vms"], terminal_width=160)
    assert result.exit_code == 0, result.output
    assert "Balloon" in result.output and "Swap" in result.output
    assert "1024.0" in result.output
    assert "mem.usage.average" in result.output, "Mem % does not say which counter it is"


@pytest.mark.unit
def test_the_mcp_tool_documents_the_new_fields():
    doc = server.vm_performance.__doc__ or ""
    for field in ("mem_ballooned_mb", "mem_swapped_mb", "mem.usage.average"):
        assert field in doc, f"vm_performance docstring does not mention {field}"
