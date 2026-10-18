"""A host the account cannot read is "unreadable", never "down".

2026-10-18, lab vCenter 8.0.3, account ``agt-onevm@vsphere.local`` holding
Read-only on one VM only: the VM's host 192.168.1.118 came back with its name and
nothing else (list_esxi_hosts showed connection_state "N/A"), and the health
summary ranked it ``host_down`` / critical with detail "host None" — first in
get_cross_vcenter_attention too. The host was healthy; the account simply could
not read its runtime. Unknown is not a verdict: not "down", and not "ok" either.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vmware_monitor.ops import cluster_summary

pytestmark = pytest.mark.unit

GB = 1024**3


def _si():
    folder = SimpleNamespace(name="Datacenters", triggeredAlarmState=[])
    return SimpleNamespace(content=SimpleNamespace(rootFolder=folder))


def _readable(name: str, state: str) -> dict:
    return {
        "name": name,
        "runtime.connectionState": state,
        "summary.quickStats.overallCpuUsage": 100,
        "summary.quickStats.overallMemoryUsage": 1024,
        "summary.hardware.cpuMhz": 2800,
        "summary.hardware.numCpuCores": 8,
        "summary.hardware.memorySize": 64 * GB,
        "triggeredAlarmState": [],
    }


# As the lab returned it for the restricted account: only the name is readable.
UNREADABLE = {"name": "192.168.1.118"}


def _install(monkeypatch, hosts: list[dict]) -> None:
    def fake_collect(si, obj_type, paths):
        if obj_type[0] is vim.HostSystem:
            return [(object(), h) for h in hosts]
        return []

    monkeypatch.setattr(cluster_summary, "_collect", fake_collect)
    monkeypatch.setattr(cluster_summary, "_collect_objects", lambda *a, **k: [])


def _summary(monkeypatch, hosts):
    _install(monkeypatch, hosts)
    return cluster_summary.get_cluster_health_summary(_si(), include_vms=False)


def test_unreadable_host_is_not_reported_down(monkeypatch):
    data = _summary(monkeypatch, [UNREADABLE])
    assert not [i for i in data["top_issues"] if i["kind"] == "host_down"]
    assert data["totals"]["worst_status"] != "critical"
    assert data["totals"]["hosts_unreadable"] == 1


def test_unreadable_host_is_not_ok_either(monkeypatch):
    data = _summary(monkeypatch, [UNREADABLE])
    [row] = [c for c in data["clusters"] if c["name"] == "(standalone hosts)"]
    assert row["status"] == "warn"
    assert any("cannot read" in a for a in row["attention"])
    assert data["totals"]["worst_status"] == "warn"


@pytest.mark.parametrize("state", ["disconnected", "notResponding"])
def test_a_really_disconnected_host_is_still_down(monkeypatch, state):
    data = _summary(monkeypatch, [_readable("192.168.1.117", state), UNREADABLE])
    down = [i for i in data["top_issues"] if i["kind"] == "host_down"]
    assert [i["object"] for i in down] == ["192.168.1.117"]
    assert down[0]["detail"] == f"host {state}"
    assert data["totals"]["worst_status"] == "critical"
    assert data["totals"]["hosts_unreadable"] == 1


def test_connected_hosts_unchanged(monkeypatch):
    data = _summary(monkeypatch, [_readable("192.168.1.117", "connected")])
    assert data["totals"]["hosts_unreadable"] == 0
    assert data["totals"]["hosts_connected"] == 1
    assert data["totals"]["worst_status"] == "ok"


def test_unreadable_host_is_a_visible_warning_issue(monkeypatch):
    # Review 2026-10-18 (M2): a WARN with no issue left the attention roll-up,
    # which merges top_issues only, unable to say why.
    data = _summary(monkeypatch, [UNREADABLE])
    [issue] = [i for i in data["top_issues"] if i["kind"] == "host_unreadable"]
    assert issue["severity"] == "warning" and issue["object"] == "192.168.1.118"
    assert "not known to be down" in issue["detail"]
    assert cluster_summary.cli_drilldown(issue)


def test_unreadable_host_ranks_after_a_real_outage(monkeypatch):
    data = _summary(monkeypatch, [UNREADABLE, _readable("192.168.1.117", "notResponding")])
    kinds = [i["kind"] for i in data["top_issues"]]
    assert kinds.index("host_down") < kinds.index("host_unreadable")


def test_html_card_is_amber_not_red_for_an_unreadable_host(monkeypatch):
    # Review 2026-10-18 (M1): the card said WARN while its Hosts bar was red "0/1".
    from vmware_monitor.ops import health_html

    data = _summary(monkeypatch, [UNREADABLE])
    [row] = [c for c in data["clusters"] if c["name"] == "(standalone hosts)"]
    card = health_html._cluster_card(row, has_vms=False)
    assert "mwarn" in card and "mcrit" not in card
    assert "1 unreadable" in card



def test_every_html_host_count_names_the_unreadable_host(monkeypatch):
    # Narrow review 2026-10-18 (MED-1): the totals strip and the attention cards
    # still showed a bare "0/1".
    from datetime import datetime, timezone

    from vmware_monitor.ops import attention_html, health_html

    data = _summary(monkeypatch, [UNREADABLE])
    page = health_html.render_cluster_health_html(data, "vc", datetime.now(timezone.utc))
    assert page.count("(1 unreadable)") >= 2  # the totals strip and the cluster card
    card = attention_html._target_card(
        {
            "vcenter": "vc",
            "worst_status": "warn",
            "clusters": 0,
            "hosts_connected": 0,
            "hosts_total": 1,
            "hosts_unreadable": 1,
            "alarms": {"critical": 0, "warning": 0},
        }
    )
    assert "(1 unreadable)" in card
