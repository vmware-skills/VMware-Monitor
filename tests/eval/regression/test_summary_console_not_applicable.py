"""Regression — the summary table must not print HA/DRS OFF for rows with no cluster.

The (standalone hosts) and (vCenter-level) rows carry ``ha_enabled=None`` and
``drs_enabled=None``: HA and DRS are cluster settings and these rows have no
cluster. The table printed them as a red "OFF" (live, vCenter 8.0.3, 2026-09-13).
"""

from __future__ import annotations

from rich.console import Console

from vmware_monitor import cli_observability


def _row(name, ha, drs):
    return {
        "name": name,
        "status": "ok",
        "hosts_connected": 2,
        "hosts_total": 2,
        "vms_on": 1,
        "vms_total": 2,
        "cpu_used_pct": 5.0,
        "mem_used_pct": 40.0,
        "ha_enabled": ha,
        "drs_enabled": drs,
        "alarms": {"critical": 0, "warning": 0},
        "attention": [],
    }


def test_rows_without_a_cluster_print_n_a(monkeypatch):
    console = Console(record=True, width=200, color_system=None)
    monkeypatch.setattr(cli_observability, "console", console)
    data = {
        "totals": {
            "clusters": 1,
            "hosts_total": 4,
            "hosts_connected": 4,
            "vms_total": 4,
            "vms_on": 2,
            "alarms": {"critical": 0, "warning": 0},
            "worst_status": "ok",
        },
        "top_issues": [],
        "issues_total": 0,
        "clusters": [_row("(standalone hosts)", None, None), _row("prod", False, False)],
        "customization_hint": "hint",
    }
    cli_observability.render_summary_console(data, 10)
    lines = {
        ln.split("│")[2].strip(): ln
        for ln in console.export_text().splitlines()
        if ln.count("│") > 3
    }
    standalone, prod = lines["(standalone hosts)"], lines["prod"]
    assert "OFF" not in standalone and "off" not in standalone, standalone
    assert standalone.count("n/a") == 2, standalone
    assert "OFF" in prod and "off" in prod, prod
