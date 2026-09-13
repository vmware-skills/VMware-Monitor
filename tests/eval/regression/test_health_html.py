"""Regression — offline HTML snapshot rendering for cluster health.

Locks the properties that make the ``--html`` snapshot safe and correct:

- It is a complete, self-contained document (``<!doctype html>`` … ``</html>``)
  with the CSS inlined and NO external requests (offline / ``file://`` safe).
- Every value that comes from vSphere is HTML-escaped — a crafted cluster/host/
  alarm name cannot inject markup or script into the page (the ops-layer
  ``sanitize`` strips control chars but does not neutralise ``<`` / ``>``).
- Structural pieces render from the data: totals strip, top-issues list (or the
  all-clear block), and one card per cluster with the right status class.
- ``include_vms=False`` data omits the VMs cells without crashing.
"""

from __future__ import annotations

from datetime import datetime

from vmware_monitor.ops.health_html import render_cluster_health_html

_NOW = datetime(2026, 7, 13, 15, 30)


def _base_data() -> dict:
    return {
        "totals": {
            "clusters": 2,
            "hosts_total": 5,
            "hosts_connected": 4,
            "vms_total": 100,
            "vms_on": 80,
            "alarms": {"critical": 1, "warning": 2},
            "worst_status": "critical",
        },
        "top_issues": [
            {
                "severity": "critical",
                "kind": "host_down",
                "object": "esx-1",
                "scope": "host",
                "cluster": "prod",
                "detail": "host notResponding",
                "drilldown": "inventory hosts",
            },
        ],
        "issues_total": 3,
        "clusters": [
            {
                "name": "prod",
                "hosts_total": 3,
                "hosts_connected": 2,
                "vms_total": 60,
                "vms_on": 55,
                "cpu_used_pct": 91,
                "mem_used_pct": 50,
                "ha_enabled": True,
                "drs_enabled": False,
                "alarms": {"critical": 1, "warning": 1},
                "status": "critical",
                "attention": ["1 host disconnected"],
            },
            {
                "name": "dev",
                "hosts_total": 2,
                "hosts_connected": 2,
                "vms_total": 40,
                "vms_on": 25,
                "cpu_used_pct": 10,
                "mem_used_pct": 20,
                "ha_enabled": True,
                "drs_enabled": True,
                "alarms": {"critical": 0, "warning": 1},
                "status": "ok",
                "attention": [],
            },
        ],
        "snapshot": "point-in-time; not a trend",
        "customization_hint": "Ask to reshape.",
    }


def test_standalone_document_and_structure():
    doc = render_cluster_health_html(_base_data(), "prod-vcenter", _NOW, filename="x.html")
    assert doc.startswith("<!doctype html>")
    assert doc.rstrip().endswith("</html>")
    # Self-contained: CSS inlined, no external references.
    assert "<style>" in doc
    for scheme in ("http://", "https://", "//cdn", "src="):
        assert scheme not in doc, f"unexpected external reference: {scheme}"
    # Structural pieces render from the data.
    for marker in (
        'class="totals"',
        "Top issues",
        'class="issue crit"',
        'class="card crit"',
        'class="card ok"',
        "No issues — cluster healthy",  # empty-attention cluster
        "prod-vcenter",
        "x.html",  # filename in footer
    ):
        assert marker in doc, marker


def test_all_dynamic_values_are_escaped():
    data = _base_data()
    data["clusters"][0]["name"] = "prod<b>x</b>"
    data["top_issues"][0]["detail"] = "<script>alert(1)</script>"
    doc = render_cluster_health_html(data, "vc&<test>", _NOW)
    assert "<script>alert(1)</script>" not in doc
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in doc
    assert "prod<b>x</b>" not in doc
    assert "prod&lt;b&gt;x&lt;/b&gt;" in doc
    assert "vc&amp;&lt;test&gt;" in doc


def test_empty_issues_renders_all_clear():
    data = _base_data()
    data["top_issues"] = []
    data["issues_total"] = 0
    data["totals"]["worst_status"] = "ok"
    doc = render_cluster_health_html(data, "vc", _NOW)
    assert "every cluster is OK" in doc
    assert 'class="issue ' not in doc


def test_include_vms_false_omits_vm_cells():
    data = _base_data()
    for c in data["clusters"]:
        c.pop("vms_total", None)
        c.pop("vms_on", None)
    data["totals"].pop("vms_total")
    data["totals"].pop("vms_on")
    doc = render_cluster_health_html(data, "vc", _NOW)
    assert "VMs powered on" not in doc
    assert "VMs on" not in doc
    # Three totals cells now (no VMs) → --tc:3.
    assert "--tc:3" in doc


def test_ha_and_drs_render_not_applicable_for_rows_without_a_cluster():
    """None means the row has no cluster to carry the setting; rendering it as
    "off" claims a setting was turned off that never existed."""
    data = _base_data()
    data["clusters"][0].update(name="(standalone hosts)", ha_enabled=None, drs_enabled=None)
    data["clusters"][1].update(ha_enabled=False, drs_enabled=False)
    doc = render_cluster_health_html(data, "vc", _NOW)
    assert doc.count("HA <b>n/a</b>") == 1 and doc.count("DRS <b>n/a</b>") == 1
    assert doc.count("HA <b>off</b>") == 1 and doc.count("DRS <b>off</b>") == 1
