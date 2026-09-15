"""Regression — attention counts a host once and an ESXi target as an ESXi target.

Found 2026-09-15 on the lab. Two targets are configured: ``home-vcenter``
(192.168.60.16) and ``home-esxi`` (192.168.60.15) — and .15 is also a host that
``home-vcenter`` manages. ``vmware-monitor attention`` said::

    What needs attention — 2 vCenters, 0 clusters, 3/3 hosts connected

There is one vCenter, one directly-reached ESXi host, and two hosts. datastore1
was listed twice, at 215.9% and 136.3%, as if it were two datastores.

Identity is read, never inferred from names (the same host is "192.168.60.15" in
vCenter and "localhost.localdomain" on the host itself):

* endpoint kind from ``ServiceInstance.content.about.apiType``
  (``VirtualCenter`` / ``HostAgent``);
* a host by ``summary.hardware.uuid``;
* a datastore by ``summary.url``.

What cannot be read is not guessed: a target whose kind is unreadable is counted
as unidentified (not as a vCenter), and hosts or datastores without a readable
identity are never merged. An all-zero hardware UUID — common on white-box
boards — is not an identity.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyVmomi import vim
from rich.console import Console

from vmware_monitor import cli_observability
from vmware_monitor.ops import attention

U15 = "4c4c4544-0031-3510-8058-b1c04f4e3232"
U56 = "4c4c4544-0056-3510-8058-b1c04f4e3232"
ZERO = "00000000-0000-0000-0000-000000000000"
DS1 = "ds:///vmfs/volumes/6523f0a1-aaaaaaaa-0000-000c29000001/"
DS2 = "ds:///vmfs/volumes/6523f0a1-bbbbbbbb-0000-000c29000002/"


class _SI:
    """Hashable (the fakes key on it) stand-in with an ``about.apiType``."""

    def __init__(self, api_type: str | None) -> None:
        self.content = SimpleNamespace(about=SimpleNamespace(apiType=api_type))


def _summary(hosts_total, hosts_connected, issues, worst="warn"):
    return {
        "totals": {
            "clusters": 0,
            "hosts_total": hosts_total,
            "hosts_connected": hosts_connected,
            "alarms": {"critical": 0, "warning": 0},
            "worst_status": worst,
        },
        "top_issues": issues,
        "issues_total": len(issues),
        "clusters": [],
    }


def _ds_issue(name, pct, provisioned):
    return {
        "severity": "warning",
        "kind": "capacity",
        "object": name,
        "scope": "datastore",
        "cluster": "(standalone hosts)",
        "detail": (
            f"thin-provisioned to {pct}% of capacity ({provisioned} of 803.2 GB promised) "
            "— can fill while it shows free space"
        ),
        "drilldown": "datastore_capacity, then datastore_investigation_bundle",
    }


def _identity(endpoint, hosts, datastores):
    return {"endpoint": endpoint, "hosts": hosts, "datastore_urls": datastores}


def _install(monkeypatch, summaries, identities):
    monkeypatch.setattr(
        attention,
        "get_cluster_health_summary",
        lambda si, cluster_filter=None, top_n=10: summaries[si],
    )
    monkeypatch.setattr(attention, "target_identity", lambda si: identities[si])


def _lab(monkeypatch, *, esxi_uuid=U15, esxi_url=DS1, esxi_endpoint="esxi"):
    vc, esxi = _SI("VirtualCenter"), _SI("HostAgent")
    _install(
        monkeypatch,
        {
            vc: _summary(2, 2, [_ds_issue("datastore1", 215.9, 1734.0)], worst="critical"),
            esxi: _summary(1, 1, [_ds_issue("datastore1", 136.3, 1094.9)]),
        },
        {
            vc: _identity("vcenter", {U15: True, U56: True}, {"datastore1": DS1}),
            esxi: _identity(esxi_endpoint, {esxi_uuid: True}, {"datastore1": esxi_url}),
        },
    )
    # ESXi listed first on purpose: which view wins must not depend on config order.
    return [("home-esxi", esxi), ("home-vcenter", vc)]


@pytest.mark.unit
def test_the_lab_estate_is_one_vcenter_one_esxi_target_and_two_hosts(monkeypatch):
    data = attention.get_cross_vcenter_attention(_lab(monkeypatch), top_n=10)
    t = data["totals"]
    assert t["vcenters"] == 1, t
    assert t["esxi_targets"] == 1, t
    assert t["hosts_total"] == 2, t
    assert t["hosts_connected"] == 2, t
    rows = {r["vcenter"]: r for r in data["targets"]}
    assert rows["home-esxi"]["endpoint"] == "ESXi host"
    assert rows["home-esxi"]["shared_hosts"] == 1
    assert rows["home-vcenter"]["endpoint"] == "vCenter"


@pytest.mark.unit
def test_a_datastore_seen_through_both_targets_is_one_issue(monkeypatch):
    data = attention.get_cross_vcenter_attention(_lab(monkeypatch), top_n=10)
    ds = [i for i in data["top_issues"] if i["object"] == "datastore1"]
    assert len(ds) == 1, ds
    assert ds[0]["vcenter"] == "home-vcenter", "the vCenter's view is the one kept"
    also = ds[0]["also_seen_via"]
    assert [a["vcenter"] for a in also] == ["home-esxi"]
    assert "136.3%" in also[0]["detail"], "the other view's number must not vanish"
    assert data["issues_total"] == 1


@pytest.mark.unit
def test_the_host_spells_the_same_datastore_url_differently(monkeypatch):
    """Observed on the lab, 2026-09-15, reading ``summary.url`` for datastore1:

        vCenter  ds:///vmfs/volumes/684fa75c-b3abf3b2-04da-48210b335794/
        ESXi     /vmfs/volumes/684fa75c-b3abf3b2-04da-48210b335794

    Same volume; the first version compared the strings and listed it twice.
    """
    host_form = DS1.removeprefix("ds://").rstrip("/")
    data = attention.get_cross_vcenter_attention(_lab(monkeypatch, esxi_url=host_form), top_n=10)
    assert len([i for i in data["top_issues"] if i["object"] == "datastore1"]) == 1


@pytest.mark.unit
def test_same_name_different_datastore_is_not_merged(monkeypatch):
    data = attention.get_cross_vcenter_attention(_lab(monkeypatch, esxi_url=DS2), top_n=10)
    assert len([i for i in data["top_issues"] if i["object"] == "datastore1"]) == 2


@pytest.mark.unit
def test_an_all_zero_hardware_uuid_is_not_an_identity(monkeypatch):
    vc, esxi = _SI("VirtualCenter"), _SI("HostAgent")
    _install(
        monkeypatch,
        {vc: _summary(1, 1, []), esxi: _summary(1, 1, [])},
        {
            vc: _identity("vcenter", {ZERO: True}, {}),
            esxi: _identity("esxi", {ZERO: True}, {}),
        },
    )
    data = attention.get_cross_vcenter_attention([("vc", vc), ("esx", esxi)], top_n=10)
    assert data["totals"]["hosts_total"] == 2


PLACEHOLDER = "03000200-0400-0500-0006-000700080009"
NFS = "ds:///vmfs/volumes/nfs-export-shared/"


@pytest.mark.unit
def test_two_vcenters_whose_distinct_hosts_share_a_placeholder_uuid_count_four_hosts(
    monkeypatch,
):
    """Review 2026-09-15 (M2): white-box boards report placeholder UUIDs. A host
    cannot be connected to two vCenters, so a UUID on both is not an identity."""
    a, b = _SI("VirtualCenter"), _SI("VirtualCenter")
    other = "4c4c4544-0000-1111-2222-333344445555"
    _install(
        monkeypatch,
        {a: _summary(2, 2, []), b: _summary(2, 2, [])},
        {
            a: _identity("vcenter", {"a1-" + other: True, "0badc0de-" + other: True}, {}),
            b: _identity("vcenter", {"0badc0de-" + other: True, "b2-" + other: True}, {}),
        },
    )
    data = attention.get_cross_vcenter_attention([("vc-a", a), ("vc-b", b)], top_n=10)
    assert data["totals"]["hosts_total"] == 4


@pytest.mark.unit
def test_the_known_placeholder_uuid_is_never_an_identity(monkeypatch):
    vc, esxi = _SI("VirtualCenter"), _SI("HostAgent")
    _install(
        monkeypatch,
        {vc: _summary(2, 2, []), esxi: _summary(1, 1, [])},
        {
            vc: _identity("vcenter", {PLACEHOLDER: True, U56: True}, {}),
            esxi: _identity("esxi", {PLACEHOLDER: True}, {}),
        },
    )
    data = attention.get_cross_vcenter_attention([("vc", vc), ("esx", esxi)], top_n=10)
    assert data["totals"]["hosts_total"] == 3


@pytest.mark.unit
def test_a_uuid_on_two_hosts_of_one_target_is_not_an_identity(monkeypatch):
    h1, h2 = vim.HostSystem("host-1"), vim.HostSystem("host-2")
    dup = "5a5a5a5a-1234-5678-9abc-def012345678"

    def fake_collect(si, obj_type, paths):
        if obj_type[0] is vim.HostSystem:
            return [
                (h1, {"summary.hardware.uuid": dup, "runtime.connectionState": "connected"}),
                (h2, {"summary.hardware.uuid": dup, "runtime.connectionState": "connected"}),
            ]
        return []

    monkeypatch.setattr(attention, "_collect", fake_collect)
    assert attention.target_identity(_SI("VirtualCenter"))["hosts"] == {}


@pytest.mark.unit
def test_two_vcenters_sharing_an_nfs_datastore_keep_both_issues(monkeypatch):
    """Review 2026-09-15 (M3): the first version merged on URL alone and kept the
    first vCenter's 110% while the other's 400% sat hidden under also_seen_via.
    Only a real overlap — an ESXi target whose host a vCenter target manages —
    is merged."""
    a, b = _SI("VirtualCenter"), _SI("VirtualCenter")
    _install(
        monkeypatch,
        {
            a: _summary(1, 1, [_ds_issue("nfs01", 110.0, 883.5)]),
            b: _summary(1, 1, [_ds_issue("nfs01", 400.0, 3212.8)]),
        },
        {
            a: _identity("vcenter", {U15: True}, {"nfs01": NFS}),
            b: _identity("vcenter", {U56: True}, {"nfs01": NFS}),
        },
    )
    data = attention.get_cross_vcenter_attention([("vc-a", a), ("vc-b", b)], top_n=10)
    ds = [i for i in data["top_issues"] if i["object"] == "nfs01"]
    assert len(ds) == 2, ds
    assert not any(i.get("also_seen_via") for i in ds)


@pytest.mark.unit
def test_an_esxi_target_not_managed_by_the_vcenter_keeps_its_datastore_issue(monkeypatch):
    vc, esxi = _SI("VirtualCenter"), _SI("HostAgent")
    _install(
        monkeypatch,
        {
            vc: _summary(1, 1, [_ds_issue("nfs01", 110.0, 883.5)]),
            esxi: _summary(1, 1, [_ds_issue("nfs01", 400.0, 3212.8)]),
        },
        {
            vc: _identity("vcenter", {U56: True}, {"nfs01": NFS}),
            esxi: _identity("esxi", {U15: True}, {"nfs01": NFS}),
        },
    )
    data = attention.get_cross_vcenter_attention([("vc", vc), ("esx", esxi)], top_n=10)
    assert len([i for i in data["top_issues"] if i["object"] == "nfs01"]) == 2


@pytest.mark.unit
def test_an_unreadable_identity_is_not_counted_as_a_vcenter_or_merged(monkeypatch):
    a, b = _SI(None), _SI(None)
    _install(
        monkeypatch,
        {a: _summary(1, 1, [_ds_issue("datastore1", 150.0, 1204.8)]),
         b: _summary(1, 1, [_ds_issue("datastore1", 150.0, 1204.8)])},
        {a: None, b: None},
    )
    data = attention.get_cross_vcenter_attention([("a", a), ("b", b)], top_n=10)
    t = data["totals"]
    assert t["vcenters"] == 0 and t["esxi_targets"] == 0
    assert t["unidentified_targets"] == 2
    assert t["hosts_total"] == 2
    assert len(data["top_issues"]) == 2


@pytest.mark.unit
def test_a_cluster_filter_does_not_subtract_hosts_the_filter_already_left_out(monkeypatch):
    vc, esxi = _SI("VirtualCenter"), _SI("HostAgent")
    _install(
        monkeypatch,
        {vc: _summary(2, 2, []), esxi: _summary(0, 0, [])},
        {
            vc: _identity("vcenter", {U15: True, U56: True}, {}),
            esxi: _identity("esxi", {U15: True}, {}),
        },
    )
    data = attention.get_cross_vcenter_attention(
        [("vc", vc), ("esx", esxi)], cluster_filter="prod", top_n=10
    )
    assert data["totals"]["hosts_total"] == 2


@pytest.mark.unit
def test_target_identity_reads_kind_hosts_and_datastores(monkeypatch):
    host, ds = vim.HostSystem("host-15"), vim.Datastore("datastore-1")

    def fake_collect(si, obj_type, paths):
        if obj_type[0] is vim.HostSystem:
            return [(host, {"summary.hardware.uuid": U15, "runtime.connectionState": "connected"})]
        if obj_type[0] is vim.Datastore:
            return [(ds, {"name": "datastore1", "summary.url": DS1})]
        return []

    monkeypatch.setattr(attention, "_collect", fake_collect)
    ident = attention.target_identity(_SI("HostAgent"))
    assert ident == _identity("esxi", {U15: True}, {"datastore1": DS1})


@pytest.mark.unit
def test_target_identity_degrades_per_part(monkeypatch):
    def fake_collect(si, obj_type, paths):
        raise RuntimeError("PropertyCollector refused")

    monkeypatch.setattr(attention, "_collect", fake_collect)
    ident = attention.target_identity(_SI("VirtualCenter"))
    assert ident["endpoint"] == "vcenter"
    assert ident["hosts"] is None and ident["datastore_urls"] is None


@pytest.mark.unit
def test_the_cli_header_does_not_call_an_esxi_host_a_vcenter(monkeypatch):
    data = attention.get_cross_vcenter_attention(_lab(monkeypatch), top_n=10)
    narrow = Console(width=120, record=True, force_terminal=False, color_system=None)
    monkeypatch.setattr(cli_observability, "console", narrow)
    cli_observability.render_attention_console(data)
    text = narrow.export_text()
    assert "2 vCenters" not in text
    assert "1 vCenter" in text and "1 ESXi" in text
    assert "2/2 hosts" in text
    assert "136.3%" in text, "the direct view's differing number is not shown"
