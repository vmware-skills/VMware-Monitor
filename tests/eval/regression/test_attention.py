"""Regression — cross-vCenter attention: global ranking, graceful degradation, shape.

Locks the top level of the object-investigation family (issue #31 follow-up): merge
every target's cluster-health top_issues into one globally-ranked list, roll totals
up across the estate, and — critically — never let one dead vCenter sink the view
(connect failures AND mid-summary failures land in ``unreachable``). ``get_cluster_
health_summary`` is monkeypatched so the pure merge/rank logic runs without pyVmomi.
"""

from __future__ import annotations

from datetime import datetime, timezone

from vmware_monitor.ops import attention
from vmware_monitor.ops.attention_html import render_attention_html


def _summary(worst, clusters, hc, ht, crit, warn, issues):
    return {
        "totals": {
            "clusters": clusters,
            "hosts_connected": hc,
            "hosts_total": ht,
            "alarms": {"critical": crit, "warning": warn},
            "worst_status": worst,
        },
        "top_issues": issues,
        "issues_total": len(issues),
        "clusters": [],
    }


def _issue(sev, kind, obj, detail):
    return {
        "severity": sev,
        "kind": kind,
        "object": obj,
        "scope": "cluster",
        "cluster": obj,
        "detail": detail,
        "drilldown": "hint",
    }


def _install(monkeypatch, by_target):
    def fake_summary(si, cluster_filter=None, top_n=10):
        result = by_target[si]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(attention, "get_cluster_health_summary", fake_summary)
    # These fakes stand in for vCenters. Since 2026-09-15 the endpoint kind is read
    # (about.apiType) rather than assumed, so say what the fake is; host and
    # datastore identity stay unreadable, which disables de-duplication here.
    monkeypatch.setattr(
        attention,
        "target_identity",
        lambda si: {"endpoint": "vcenter", "hosts": None, "datastore_urls": None},
    )


def test_merges_and_globally_ranks(monkeypatch):
    prod, lab = object(), object()
    _install(
        monkeypatch,
        {
            prod: _summary(
                "critical", 3, 5, 6, 2, 1,
                [_issue("critical", "host_down", "esxi-1", "host disconnected")],
            ),
            lab: _summary(
                "warn", 1, 2, 2, 0, 1,
                [_issue("warning", "capacity", "lab-cl", "CPU at 88%")],
            ),
        },
    )
    data = attention.get_cross_vcenter_attention([("prod", prod), ("lab", lab)], top_n=10)

    assert data["totals"]["vcenters"] == 2
    assert data["totals"]["clusters"] == 4
    assert data["totals"]["hosts_connected"] == 7
    assert data["totals"]["worst_status"] == "critical"
    # global rank: the critical host_down (prod) outranks the warning capacity (lab)
    assert data["top_issues"][0]["vcenter"] == "prod"
    assert data["top_issues"][0]["severity"] == "critical"
    assert data["top_issues"][1]["vcenter"] == "lab"
    # targets sorted worst-first
    assert [t["vcenter"] for t in data["targets"]] == ["prod", "lab"]


def test_unreachable_from_connect_is_preserved(monkeypatch):
    prod = object()
    _install(monkeypatch, {prod: _summary("ok", 1, 1, 1, 0, 0, [])})
    data = attention.get_cross_vcenter_attention(
        [("prod", prod)], unreachable=[("dr-site", "TimeoutError")], top_n=10
    )
    assert {u["vcenter"]: u["reason"] for u in data["unreachable"]} == {"dr-site": "TimeoutError"}
    assert data["totals"]["vcenters"] == 1  # dead target not counted in totals


def test_mid_summary_failure_degrades_not_fails(monkeypatch):
    good, bad = object(), object()
    _install(
        monkeypatch,
        {
            good: _summary("warn", 1, 1, 1, 0, 1, [_issue("warning", "capacity", "c", "mem 90%")]),
            bad: RuntimeError("boom"),
        },
    )
    data = attention.get_cross_vcenter_attention([("good", good), ("bad", bad)], top_n=10)
    # the failing target is moved to unreachable; the good one still aggregates.
    assert data["totals"]["vcenters"] == 1
    assert any(u["vcenter"] == "bad" for u in data["unreachable"])
    assert len(data["top_issues"]) == 1


def test_html_escapes_and_lists_unreachable(monkeypatch):
    good = object()
    _install(monkeypatch, {good: _summary("ok", 1, 1, 1, 0, 0, [])})
    data = attention.get_cross_vcenter_attention(
        [("<b>prod</b>", good)], unreachable=[("dr", "SSLError")], top_n=10
    )
    html = render_attention_html(data, datetime(2026, 7, 14, tzinfo=timezone.utc))
    assert "<b>prod</b>" not in html
    assert "&lt;b&gt;prod" in html
    assert "unreachable" in html.lower()
    assert "https://" not in html and "http://" not in html  # fully offline
