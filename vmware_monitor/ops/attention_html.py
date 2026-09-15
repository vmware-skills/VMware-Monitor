"""Render the cross-vCenter "what needs attention now?" view as offline HTML.

Takes the dict from ``ops.attention.get_cross_vcenter_attention`` and returns a
single standalone document — no external CSS/JS/fonts, so it opens from ``file://``
with nothing leaving the machine (unlike a cloud dashboard, which would upload
internal vCenter/host/cluster names). Point-in-time snapshot, not a live page.

Shares the theme-aware palette and severity/status maps with the cluster-health and
object-investigation renderers via ``_html_base``. Every vSphere/target-originated
value is ``html.escape``-d here.
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
)
from vmware_monitor.ops.cluster_summary import cli_drilldown


def _issue_row(rank: int, issue: dict) -> str:
    sev = issue.get("severity", "warning")
    cls = SEV_CLASS.get(sev, "warn")
    label = SEV_LABEL.get(sev, sev.title())
    detail = escape(str(issue.get("detail", "")))
    obj = escape(str(issue.get("object", "")))
    vcenter = escape(str(issue.get("vcenter", "")))
    cluster = escape(str(issue.get("cluster") or ""))
    scope = issue.get("scope", "cluster")
    loc = f"{escape(scope)} <b>{obj}</b>"
    if cluster and scope != "cluster":
        loc += f" · cluster {cluster}"
    # Written by the CLI, read by a person: name CLI commands, not MCP tools.
    nxt = escape(cli_drilldown(issue))
    return (
        f'<div class="issue {cls}"><div class="rank">{rank}</div>'
        f'<div class="chip {cls}">{label}</div>'
        f'<div class="what"><div class="prob">{detail}</div>'
        f'<div class="obj">vCenter <b>{vcenter}</b> · {loc}</div></div>'
        f'<div class="next"><span class="arrow">&rarr;</span> {nxt}</div></div>'
    )


def _target_card(t: dict) -> str:
    status = t.get("worst_status", "ok")
    cls = STATUS_CLASS.get(status, "ok")
    al = t.get("alarms", {})
    return (
        f'<div class="card {cls}"><div class="top"><span class="cname">{escape(str(t.get("vcenter", "")))}</span>'
        f'<span class="pill {cls}">{STATUS_LABEL.get(status, status)}</span></div><div class="body">'
        f'<div class="kv"><span class="tag">clusters <b>{t.get("clusters", 0)}</b></span>'
        f'<span class="tag">hosts <b>{t.get("hosts_connected", 0)}</b>/{t.get("hosts_total", 0)}</span>'
        f'<span class="tag">alarms <b>{al.get("critical", 0)}</b>c / <b>{al.get("warning", 0)}</b>w</span>'
        "</div></div></div>"
    )


def render_attention_html(data: dict, generated_at: datetime, filename: str = "") -> str:
    """Render the cross-vCenter attention dict as a standalone offline HTML document.

    Args:
        data: Return value of ``get_cross_vcenter_attention``.
        generated_at: Snapshot time.
        filename: Optional file name recorded in the footer (provenance).

    Returns:
        A complete ``<!doctype html>`` string with all CSS inlined and every dynamic
        value HTML-escaped. No external requests.
    """
    totals = data.get("totals", {})
    worst = totals.get("worst_status", "ok")
    stamp = generated_at.strftime("%Y-%m-%d %H:%M %Z") or generated_at.strftime("%Y-%m-%d %H:%M")

    al = totals.get("alarms", {})
    cells = [
        f'<div class="cell"><div class="k">vCenters</div><div class="v">{totals.get("vcenters", 0)}</div></div>',
        # A directly-reached ESXi host is not a vCenter; show it as its own count.
        *(
            [
                f'<div class="cell"><div class="k">ESXi targets</div>'
                f'<div class="v">{totals["esxi_targets"]}</div></div>'
            ]
            if totals.get("esxi_targets")
            else []
        ),
        f'<div class="cell"><div class="k">Clusters</div><div class="v">{totals.get("clusters", 0)}</div></div>',
        f'<div class="cell"><div class="k">Hosts connected</div><div class="v">'
        f"{totals.get('hosts_connected', 0)}<small>/{totals.get('hosts_total', 0)}</small></div></div>",
        f'<div class="cell"><div class="k">Active alarms</div><div class="v">'
        f'<span class="c">{al.get("critical", 0)}</span><small> crit</small> · '
        f'<span class="w">{al.get("warning", 0)}</span><small> warn</small></div></div>',
    ]

    unreachable = data.get("unreachable", [])
    unreachable_block = ""
    if unreachable:
        items = ", ".join(
            f"{escape(str(u.get('vcenter', '')))} ({escape(str(u.get('reason', '')))})"
            for u in unreachable
        )
        unreachable_block = (
            f'<div class="issue warn" style="margin-bottom:24px"><div class="rank">!</div>'
            f'<div class="chip warn">Skipped</div><div class="what"><div class="prob">'
            f"{len(unreachable)} vCenter(s) unreachable</div>"
            f'<div class="obj">{items}</div></div><div class="next"></div></div>'
        )

    issues = data.get("top_issues", [])
    issues_total = data.get("issues_total", len(issues))
    if issues:
        count = f"{len(issues)} shown · {issues_total} total"
        rows = "".join(_issue_row(i + 1, iss) for i, iss in enumerate(issues))
        issues_block = (
            f'<div class="section-label">Top issues across all vCenters <span class="count">{count}</span></div>'
            f'<div class="issues">{rows}</div>'
        )
    else:
        issues_block = '<div class="clean-all">No issues detected — every vCenter is OK.</div>'

    cards = "".join(_target_card(t) for t in data.get("targets", []))
    hint = escape(str(data.get("customization_hint", "")))
    snapshot = escape(str(data.get("snapshot", "point-in-time snapshot")))
    fname = escape(filename) if filename else ""
    meta_bits = [f"<span>{snapshot}</span>"]
    if fname:
        meta_bits.append(f"<span>file: {fname}</span>")
    meta_bits.append("<span>generated by vmware-monitor attention --html</span>")

    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>What needs attention — {escape(stamp)}</title>"
        f"<style>{BASE_CSS}</style></head><body>"
        '<div class="wrap"><header><div style="width:100%">'
        '<div class="eyebrow">VMware cross-vCenter triage · attention snapshot</div>'
        '<h1>What needs attention <span class="vc">now</span></h1></div>'
        f'<div class="stamp">Snapshot {escape(stamp)}</div>'
        f'<div class="overall {STATUS_CLASS.get(worst, "ok")}"><span class="dot"></span>'
        f"{STATUS_LABEL.get(worst, worst)}</div></header>"
        f'<div class="totals" style="--tc:4">{"".join(cells)}</div>'
        f"{unreachable_block}"
        f"{issues_block}"
        '<div class="section-label">vCenters <span class="count">worst status first</span></div>'
        f'<div class="cards">{cards}</div>'
        f'<footer><div class="hint">{hint}</div>'
        f'<div class="meta">{"".join(meta_bits)}</div></footer>'
        "</div></body></html>"
    )
