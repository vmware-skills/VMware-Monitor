"""Render a cluster-health snapshot as a self-contained, offline HTML file.

Takes the dict from ``ops.cluster_summary.get_cluster_health_summary`` and returns
a single standalone HTML document — no external CSS/JS/fonts, so it opens from
``file://`` with nothing leaving the machine (unlike a cloud-hosted artifact,
which would upload internal host/cluster names). This is the "make it tangible
for stakeholders" view; it is a **point-in-time snapshot**, not a live page.

Every value that originates from vSphere (cluster/host names, alarm names) is
HTML-escaped here — the ops layer's ``sanitize`` strips control characters but
does not neutralise markup, so escaping at render time is what prevents a crafted
VM/cluster name from injecting into the page.

The theme-aware palette and severity/status maps are shared with the object
investigation bundles via ``_html_base`` so the two renderers never drift apart.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from vmware_monitor.ops._html_base import (
    BASE_CSS,
    SEV_CLASS,
    SEV_LABEL,
    STATUS_CLASS,
    STATUS_LABEL,
    clamp,
    meter_class,
)
from vmware_monitor.ops.cluster_summary import cli_drilldown

# Local aliases keep the render body below unchanged after the palette moved to
# the shared base module.
_CSS = BASE_CSS
_SEV_LABEL = SEV_LABEL
_SEV_CLASS = SEV_CLASS
_STATUS_CLASS = STATUS_CLASS
_STATUS_LABEL = STATUS_LABEL
_meter_class = meter_class
_clamp = clamp


def _issue_row(rank: int, issue: dict) -> str:
    sev = issue.get("severity", "warning")
    cls = _SEV_CLASS.get(sev, "warn")
    label = _SEV_LABEL.get(sev, sev.title())
    detail = escape(str(issue.get("detail", "")))
    if issue.get("kind") == "alarm":
        detail += ' <span style="color:var(--faint);font-weight:400">(alarm)</span>'
    obj = escape(str(issue.get("object", "")))
    cluster = escape(str(issue.get("cluster") or ""))
    if issue.get("scope") in ("host", "datastore"):
        loc = f"{issue['scope']} <b>{obj}</b>" + (f" · cluster {cluster}" if cluster else "")
    else:
        loc = f"cluster <b>{obj}</b>"
    # Written by the CLI, read by a person: name CLI commands, not MCP tools.
    nxt = escape(cli_drilldown(issue))
    return (
        f'<div class="issue {cls}"><div class="rank">{rank}</div>'
        f'<div class="chip {cls}">{label}</div>'
        f'<div class="what"><div class="prob">{detail}</div><div class="obj">{loc}</div></div>'
        f'<div class="next"><span class="arrow">&rarr;</span> {nxt}</div></div>'
    )


def _metric(label: str, width: float, klass: str, value: str) -> str:
    return (
        f'<div class="metric"><span class="ml">{label}</span>'
        f'<div class="meter {klass}"><span style="width:{_clamp(width):.0f}%"></span></div>'
        f'<span class="mv">{escape(value)}</span></div>'
    )


def _cluster_card(c: dict, has_vms: bool) -> str:
    status = c.get("status", "ok")
    cls = _STATUS_CLASS.get(status, "ok")
    name = escape(str(c.get("name", "")))
    hosts_t = c.get("hosts_total", 0)
    hosts_c = c.get("hosts_connected", 0)
    hosts_pct = (hosts_c / hosts_t * 100) if hosts_t else 0
    parts = [
        f'<div class="card {cls}"><div class="top"><span class="cname">{name}</span>'
        f'<span class="pill {cls}">{_STATUS_LABEL.get(status, status)}</span></div><div class="body">',
        _metric(
            "Hosts", hosts_pct, "mok" if hosts_c == hosts_t else "mcrit", f"{hosts_c}/{hosts_t}"
        ),
    ]
    if has_vms:
        vms_t = c.get("vms_total", 0)
        vms_on = c.get("vms_on", 0)
        vms_pct = (vms_on / vms_t * 100) if vms_t else 0
        parts.append(_metric("VMs on", vms_pct, "macc", f"{vms_on}/{vms_t}"))
    cpu = c.get("cpu_used_pct", 0)
    mem = c.get("mem_used_pct", 0)
    parts.append(_metric("CPU", cpu, _meter_class(cpu), f"{cpu:g}%"))
    parts.append(_metric("Memory", mem, _meter_class(mem), f"{mem:g}%"))

    # None = the row has no cluster (standalone hosts, vCenter-level alarms),
    # so the setting does not apply; "off" would claim it was turned off.
    ha = "n/a" if c.get("ha_enabled") is None else ("on" if c["ha_enabled"] else "off")
    drs = "n/a" if c.get("drs_enabled") is None else ("on" if c["drs_enabled"] else "off")
    al = c.get("alarms", {})
    parts.append(
        f'<div class="kv"><span class="tag {"dim" if ha == "n/a" else ha}">HA <b>{ha}</b></span>'
        f'<span class="tag {"on" if drs == "on" else "dim"}">DRS <b>{drs}</b></span>'
        f'<span class="tag">alarms <b>{al.get("critical", 0)}</b>c / <b>{al.get("warning", 0)}</b>w</span></div>'
    )
    attention = c.get("attention") or []
    if attention:
        rows = "".join(f'<div class="a">{escape(str(a))}</div>' for a in attention)
        parts.append(f'<div class="attn">{rows}</div>')
    else:
        parts.append('<div class="attn"><div class="clean">No issues — cluster healthy</div></div>')
    parts.append("</div></div>")
    return "".join(parts)


def render_cluster_health_html(
    data: dict,
    vcenter: str,
    generated_at: datetime,
    filename: str = "",
) -> str:
    """Render a cluster-health summary dict as a standalone HTML document.

    Args:
        data: Return value of ``get_cluster_health_summary``.
        vcenter: Target name, shown in the title/header.
        generated_at: Snapshot time, shown in the header and footer.
        filename: Optional file name to record in the footer (for provenance).

    Returns:
        A complete ``<!doctype html>`` document string with all CSS inlined and
        every dynamic value HTML-escaped. No external requests.
    """
    totals = data.get("totals", {})
    clusters = data.get("clusters", [])
    has_vms = "vms_total" in totals
    worst = totals.get("worst_status", "ok")
    vc = escape(str(vcenter))
    stamp = generated_at.strftime("%Y-%m-%d %H:%M %Z") or generated_at.strftime("%Y-%m-%d %H:%M")

    # Totals strip
    cells = [
        f'<div class="cell"><div class="k">Clusters</div><div class="v">{totals.get("clusters", 0)}</div></div>',
        f'<div class="cell"><div class="k">Hosts connected</div><div class="v">'
        f"{totals.get('hosts_connected', 0)}<small>/{totals.get('hosts_total', 0)}</small></div></div>",
    ]
    if has_vms:
        cells.append(
            f'<div class="cell"><div class="k">VMs powered on</div><div class="v">'
            f"{totals.get('vms_on', 0)}<small>/{totals.get('vms_total', 0)}</small></div></div>"
        )
    al = totals.get("alarms", {})
    cells.append(
        f'<div class="cell"><div class="k">Active alarms</div><div class="v">'
        f'<span class="c">{al.get("critical", 0)}</span><small> crit</small> · '
        f'<span class="w">{al.get("warning", 0)}</span><small> warn</small></div></div>'
    )
    tc = len(cells)

    # Top issues
    issues = data.get("top_issues", [])
    issues_total = data.get("issues_total", len(issues))
    if issues:
        count = f"{len(issues)} shown · {issues_total} total"
        rows = "".join(_issue_row(i + 1, iss) for i, iss in enumerate(issues))
        issues_block = (
            f'<div class="section-label">Top issues <span class="count">{count}</span></div>'
            f'<div class="issues">{rows}</div>'
        )
    else:
        issues_block = '<div class="clean-all">No issues detected — every cluster is OK.</div>'

    cards = "".join(_cluster_card(c, has_vms) for c in clusters)
    hint = escape(str(data.get("customization_hint", "")))
    snapshot = escape(str(data.get("snapshot", "point-in-time snapshot")))
    fname = escape(filename) if filename else ""
    meta_bits = [f"<span>{snapshot}</span>"]
    if fname:
        meta_bits.append(f"<span>file: {fname}</span>")
    meta_bits.append("<span>generated by vmware-monitor summary --html</span>")

    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Cluster Health — {vc} — {escape(stamp)}</title>"
        f"<style>{_CSS}</style></head><body>"
        '<div class="wrap"><header><div style="width:100%">'
        '<div class="eyebrow">VMware cluster health · triage snapshot</div>'
        f'<h1>What needs attention on <span class="vc">{vc}</span></h1></div>'
        f'<div class="stamp">Snapshot {escape(stamp)}</div>'
        f'<div class="overall {_STATUS_CLASS.get(worst, "ok")}"><span class="dot"></span>'
        f"{_STATUS_LABEL.get(worst, worst)}</div></header>"
        f'<div class="totals" style="--tc:{tc}">{"".join(cells)}</div>'
        f"{issues_block}"
        '<div class="section-label">Clusters <span class="count">worst status first</span></div>'
        f'<div class="cards">{cards}</div>'
        f'<footer><div class="hint">{hint}</div>'
        f'<div class="meta">{"".join(meta_bits)}</div></footer>'
        "</div></body></html>"
    )
