"""Render an investigation bundle as a self-contained, offline HTML file.

Takes a bundle dict (``get_vm_investigation_bundle`` and, later, the host/datastore
equivalents) and returns one standalone document — no external CSS/JS/fonts, so it
opens from ``file://`` with nothing leaving the machine. Drill-down detail lives in
native ``<details>`` sections (Timeline / Alarms / Snapshots / Performance): the
reader clicks to expand, with **zero JavaScript**, so the page stays a pure offline
snapshot. Point-in-time — not a live page.

Every vSphere-originated value (names, event messages) is ``html.escape``-d here;
``sanitize`` upstream strips control characters but does not neutralise markup, so a
crafted VM/datastore name cannot inject into the page.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from vmware_monitor.ops._html_base import BASE_CSS, SEV_CLASS, SEV_LABEL, clamp, meter_class

# Component CSS specific to the bundle layout; appended after the shared base so the
# palette/severity colours stay identical to the cluster-health snapshot.
_BUNDLE_CSS = """
  .grid2{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px;margin:8px 0 28px}
  details{background:var(--surface);border:1px solid var(--border);border-radius:12px;
    box-shadow:var(--shadow);margin-bottom:14px;overflow:hidden}
  details>summary{cursor:pointer;padding:14px 16px;font-weight:680;font-size:15px;
    list-style:none;display:flex;align-items:center;gap:10px}
  details>summary::-webkit-details-marker{display:none}
  details>summary::before{content:"\\25B8";color:var(--accent);font-size:12px;transition:transform .15s}
  details[open]>summary::before{transform:rotate(90deg)}
  summary .count{margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--faint);font-weight:500}
  .dbody{padding:0 16px 14px}
  .tl{display:flex;flex-direction:column}
  .tl .row{display:grid;grid-template-columns:150px 78px 1fr;gap:12px;padding:9px 0;
    border-top:1px solid var(--border);align-items:baseline}
  .tl .row:first-child{border-top:none}
  .tl .t{font-family:var(--mono);font-size:12px;color:var(--muted)}
  .tl .msg{font-size:13px}.tl .msg .src{font-family:var(--mono);font-size:11px;color:var(--faint)}
  .rowtable{width:100%;border-collapse:collapse;font-size:13px}
  .rowtable td,.rowtable th{text-align:left;padding:8px 10px;border-top:1px solid var(--border)}
  .rowtable th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);font-weight:600}
  .rowtable tr:first-child td{border-top:none}
  .empty{padding:10px 0;color:var(--muted);font-size:13px}
  @media(max-width:640px){.tl .row{grid-template-columns:1fr}}
"""

# vSphere overallStatus -> (overall pill class, label).
_OBJ_STATUS = {
    "red": ("crit", "Critical"),
    "yellow": ("warn", "Warning"),
    "green": ("ok", "Healthy"),
    "gray": ("ok", "Unknown"),
}

_KIND_EYEBROW = {
    "vm": "VMware VM investigation · object-centered drill-down",
    "host": "VMware host investigation · object-centered drill-down",
    "datastore": "VMware datastore investigation · object-centered drill-down",
}


def _metric(label: str, width: float, klass: str, value: str) -> str:
    return (
        f'<div class="metric"><span class="ml">{escape(label)}</span>'
        f'<div class="meter {klass}"><span style="width:{clamp(width):.0f}%"></span></div>'
        f'<span class="mv">{escape(value)}</span></div>'
    )


def _host_card(host: dict) -> str:
    cpu = host.get("cpu_pct", 0)
    mem = host.get("mem_pct", 0)
    conn = host.get("connection", "unknown")
    conn_cls = "on" if conn == "connected" else "off"
    return (
        '<div class="card"><div class="top" style="border-top-color:var(--accent)">'
        f'<span class="cname">{escape(host.get("name", ""))}</span>'
        f'<span class="tag {conn_cls}">{escape(conn)}</span></div><div class="body">'
        + _metric("CPU", cpu, meter_class(cpu), f"{cpu:g}%")
        + _metric("Memory", mem, meter_class(mem), f"{mem:g}%")
        + "</div></div>"
    )


def _cluster_card(cl: dict) -> str:
    ha = "on" if cl.get("ha_enabled") else "off"
    drs = "on" if cl.get("drs_enabled") else "off"
    return (
        '<div class="card"><div class="top" style="border-top-color:var(--accent)">'
        f'<span class="cname">{escape(cl.get("name", ""))}</span></div><div class="body">'
        f'<div class="kv"><span class="tag {ha}">HA <b>{ha}</b></span>'
        f'<span class="tag {"on" if drs == "on" else "dim"}">DRS <b>{drs}</b></span>'
        f'<span class="tag">hosts <b>{cl.get("host_count", 0)}</b></span></div>'
        "</div></div>"
    )


def _datastore_card(ds: dict) -> str:
    free = ds.get("free_pct", 0)
    # Low free space is the risk here, so colour the *used* portion by threshold.
    used = clamp(100 - free)
    return (
        '<div class="card"><div class="top" style="border-top-color:var(--accent)">'
        f'<span class="cname">{escape(ds.get("name", ""))}</span>'
        f'<span class="tag dim">{escape(ds.get("type", ""))}</span></div><div class="body">'
        + _metric("Free", free, meter_class(used), f"{free:g}%")
        + f'<div class="kv"><span class="tag">{ds.get("free_gb", 0):g} / '
        f"{ds.get('capacity_gb', 0):g} GB free</span></div></div></div>"
    )


def _vms_card(vms: dict) -> str:
    total = vms.get("total", 0)
    on = vms.get("powered_on", 0)
    pct = (on / total * 100) if total else 0
    sample = vms.get("sample", [])
    tags = "".join(f'<span class="tag">{escape(str(n))}</span>' for n in sample)
    more = total - len(sample)
    if more > 0:
        tags += f'<span class="tag dim">+{more} more</span>'
    body = _metric("VMs on", pct, "macc", f"{on}/{total}")
    if tags:
        body += f'<div class="kv">{tags}</div>'
    return (
        '<div class="card"><div class="top" style="border-top-color:var(--accent)">'
        '<span class="cname">Virtual machines</span></div>'
        f'<div class="body">{body}</div></div>'
    )


def _context_cards(bundle: dict) -> str:
    """Render whatever related objects the bundle carries (kind-agnostic)."""
    cards: list[str] = []
    if bundle.get("host"):
        cards.append(_host_card(bundle["host"]))
    if bundle.get("cluster"):
        cards.append(_cluster_card(bundle["cluster"]))
    for h in bundle.get("hosts", []):
        cards.append(_host_card(h))
    for ds in bundle.get("datastores", []):
        cards.append(_datastore_card(ds))
    if bundle.get("vms") is not None:
        cards.append(_vms_card(bundle["vms"]))
    if not cards:
        return ""
    return f'<div class="grid2">{"".join(cards)}</div>'


def _stats_strip(stats: list[dict]) -> str:
    """The object's headline numbers as a totals strip (reuses the shared grid)."""
    if not stats:
        return ""
    cells = "".join(
        f'<div class="cell"><div class="k">{escape(str(s.get("k", "")))}</div>'
        f'<div class="v">{escape(str(s.get("v", "")))}</div></div>'
        for s in stats
    )
    return f'<div class="totals" style="--tc:{len(stats)}">{cells}</div>'


def _details(title: str, count: int, body: str, *, open_: bool) -> str:
    attr = " open" if open_ else ""
    cnt = f'<span class="count">{count}</span>' if count else ""
    return f'<details{attr}><summary>{escape(title)}{cnt}</summary><div class="dbody">{body}</div></details>'


def _timeline_section(timeline: list[dict], hours: int, note: str | None = None) -> str:
    note_html = f'<div class="empty">{escape(note)}</div>' if note else ""
    if not timeline:
        body = note_html or f'<div class="empty">No events in the last {hours}h.</div>'
        return _details("Event timeline", 0, body, open_=True)
    rows = []
    for e in timeline:
        cls = SEV_CLASS.get(e.get("severity", "info"), "info")
        chip = SEV_LABEL.get(e.get("severity", "info"), "Info")
        rows.append(
            '<div class="row">'
            f'<div class="t">{escape(str(e.get("time", "")))}</div>'
            f'<div><span class="chip {cls}">{chip}</span></div>'
            f'<div class="msg">{escape(str(e.get("message", "")))}'
            f'<div class="src">{escape(str(e.get("event_type", "")))} · '
            f"{escape(str(e.get('scope', '')))} {escape(str(e.get('entity', '')))}</div></div>"
            "</div>"
        )
    return _details(
        f"Event timeline · last {hours}h",
        len(timeline),
        f'{note_html}<div class="tl">{"".join(rows)}</div>',
        open_=True,
    )


def _alarms_section(alarms: list[dict]) -> str:
    if not alarms:
        body = '<div class="empty">No triggered alarms on this object or its related infrastructure.</div>'
        return _details("Alarms", 0, body, open_=False)
    rows = ["<tr><th>Severity</th><th>Alarm</th><th>On</th></tr>"]
    for a in alarms:
        cls = SEV_CLASS.get(a.get("severity", "warning"), "warn")
        label = SEV_LABEL.get(a.get("severity", "warning"), "Warning")
        rows.append(
            f'<tr><td><span class="chip {cls}">{label}</span></td>'
            f"<td>{escape(str(a.get('name', '')))}</td>"
            f"<td>{escape(str(a.get('scope', '')))} {escape(str(a.get('object', '')))}</td></tr>"
        )
    return _details(
        "Alarms", len(alarms), f'<table class="rowtable">{"".join(rows)}</table>', open_=True
    )


def _snapshots_section(snapshots: list[dict]) -> str:
    if not snapshots:
        body = '<div class="empty">No snapshots.</div>'
        return _details("Snapshots", 0, body, open_=False)
    rows = ["<tr><th>Name</th><th>Created</th><th>State</th></tr>"]
    for s in snapshots:
        indent = "&nbsp;&nbsp;" * int(s.get("level", 0))
        rows.append(
            f"<tr><td>{indent}{escape(str(s.get('name', '')))}</td>"
            f"<td>{escape(str(s.get('created', '')))}</td>"
            f"<td>{escape(str(s.get('state', '')))}</td></tr>"
        )
    return _details(
        "Snapshots", len(snapshots), f'<table class="rowtable">{"".join(rows)}</table>', open_=False
    )


def _performance_section(perf: dict) -> str:
    if perf.get("note"):
        return _details(
            "Live performance",
            0,
            f'<div class="empty">{escape(str(perf["note"]))}</div>',
            open_=False,
        )
    rows = [
        f"<tr><td>{escape(str(k))}</td><td>{escape(str(v))}</td></tr>"
        for k, v in perf.items()
        if k not in ("vm", "host")
    ]
    return _details(
        "Live performance", 0, f'<table class="rowtable">{"".join(rows)}</table>', open_=False
    )


def render_bundle_html(
    bundle: dict,
    kind: str,
    vcenter: str,
    generated_at: datetime,
    filename: str = "",
) -> str:
    """Render an investigation bundle dict as a standalone offline HTML document.

    Args:
        bundle: Return value of a ``get_*_investigation_bundle`` function.
        kind: ``"vm"`` | ``"host"`` | ``"datastore"`` — drives the header wording.
        vcenter: Target name, shown in the header/footer.
        generated_at: Snapshot time.
        filename: Optional file name recorded in the footer (provenance).

    Returns:
        A complete ``<!doctype html>`` string with all CSS inlined, drill-down
        detail in ``<details>`` sections, and every dynamic value HTML-escaped.
    """
    obj = bundle.get("object", {})
    name = escape(str(obj.get("name", "")))
    vc = escape(str(vcenter))
    stamp = generated_at.strftime("%Y-%m-%d %H:%M %Z") or generated_at.strftime("%Y-%m-%d %H:%M")
    status_cls, status_label = _OBJ_STATUS.get(str(obj.get("status", "gray")), ("ok", "Unknown"))
    hours = bundle.get("hours", 24)
    eyebrow = escape(_KIND_EYEBROW.get(kind, "VMware investigation"))

    # Sections are conditional on what the bundle carries: snapshots are a
    # VM-only concept, so a host/datastore bundle (no "snapshots" key) omits it.
    note = " ".join(str(bundle[k]) for k in ("timeline_unavailable", "timeline_note") if bundle.get(k))
    sections = _timeline_section(bundle.get("timeline", []), hours, note or None) + _alarms_section(
        bundle.get("alarms", [])
    )
    if "snapshots" in bundle:
        sections += _snapshots_section(bundle.get("snapshots", []))
    if "performance" in bundle:
        sections += _performance_section(bundle.get("performance", {}))

    hint = escape(str(bundle.get("customization_hint", "")))
    snapshot = escape(str(bundle.get("snapshot", "point-in-time snapshot")))
    fname = escape(filename) if filename else ""
    meta_bits = [f"<span>{snapshot}</span>"]
    if fname:
        meta_bits.append(f"<span>file: {fname}</span>")
    meta_bits.append(f"<span>generated by vmware-monitor investigate {escape(kind)} --html</span>")

    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(kind.upper())} investigation — {name} — {escape(stamp)}</title>"
        f"<style>{BASE_CSS}{_BUNDLE_CSS}</style></head><body>"
        '<div class="wrap"><header><div style="width:100%">'
        f'<div class="eyebrow">{eyebrow}</div>'
        f'<h1>What is happening around <span class="vc">{name}</span></h1></div>'
        f'<div class="stamp">On {vc} · snapshot {escape(stamp)}</div>'
        f'<div class="overall {status_cls}"><span class="dot"></span>{escape(status_label)}</div>'
        "</header>"
        f"{_stats_strip(bundle.get('stats', []))}"
        f"{_context_cards(bundle)}"
        f"{sections}"
        f'<footer><div class="hint">{hint}</div>'
        f'<div class="meta">{"".join(meta_bits)}</div></footer>'
        "</div></body></html>"
    )
