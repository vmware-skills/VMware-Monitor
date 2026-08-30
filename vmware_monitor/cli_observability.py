"""Observability CLI commands: performance, capacity, infra health, activity.

Read-only command groups added on top of the original inventory/health set:

    perf       hosts | vms           real-time PerfManager utilisation
    capacity   datastores | pools    over-commit + resource-pool usage
    infra      certs | licenses | ntp certificate expiry, licensing, NTP config
    snapshots  aging                 inventory-wide snapshot sprawl
    activity   tasks | sessions      in-flight tasks, active sessions

All commands are strictly read-only — no power, create, delete, snapshot
mutation, clone, or migrate exists here or anywhere in vmware-monitor.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from vmware_monitor.cli_base import (
    ConfigOption,
    TargetOption,
    audit,
    cli_errors,
    console,
    get_all_connections,
    get_connection,
)

perf_app = typer.Typer(help="Real-time performance counters (read-only).")
capacity_app = typer.Typer(help="Capacity analytics: over-commit, resource pools (read-only).")
infra_app = typer.Typer(help="Infra health: certificates, licenses, NTP (read-only).")
snapshots_app = typer.Typer(help="Inventory-wide snapshot aging/sprawl (read-only).")
activity_app = typer.Typer(help="Live activity: tasks and sessions (read-only).")
investigate_app = typer.Typer(help="Object-centered investigation bundles (read-only).")

LimitOption = Annotated[int | None, typer.Option("--limit", "-n", help="Max rows to show")]


def _pct_style(value: float, warn: float = 70, crit: float = 85) -> str:
    return "red" if value > crit else "yellow" if value > warn else "green"


# ─── perf ──────────────────────────────────────────────────────────────────


@perf_app.command("hosts")
@cli_errors
def perf_hosts(
    host: Annotated[str | None, typer.Option("--host", help="Single host by exact name")] = None,
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: LimitOption = None,
) -> None:
    """Real-time CPU/memory/disk/network per ESXi host."""
    from vmware_monitor.ops.performance import get_host_performance

    si, _, tgt = get_connection(target, config)
    rows = get_host_performance(si, host_name=host, limit=limit)["items"]
    audit.log_query(target=tgt, resource="host_performance", query_type="get_host_performance")
    if not rows:
        console.print("[yellow]No connected host exposed real-time metrics.[/]")
        return
    table = Table(title="Host Performance (real-time, ~20s interval)")
    table.add_column("Host", style="cyan")
    table.add_column("CPU %", justify="right")
    table.add_column("Mem %", justify="right")
    table.add_column("Mem (MB)", justify="right")
    table.add_column("Disk KB/s", justify="right")
    table.add_column("Net KB/s", justify="right")
    for r in rows:
        cpu = r.get("cpu_usage_pct", 0)
        mem = r.get("mem_usage_pct", 0)
        table.add_row(
            r["host"],
            f"[{_pct_style(cpu)}]{cpu}[/]",
            f"[{_pct_style(mem)}]{mem}[/]",
            str(r.get("mem_consumed_mb", "-")),
            str(r.get("disk_kbps", "-")),
            str(r.get("net_kbps", "-")),
        )
    console.print(table)


@perf_app.command("vms")
@cli_errors
def perf_vms(
    vm: Annotated[str | None, typer.Option("--vm", help="Single VM by exact name")] = None,
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: LimitOption = 25,
) -> None:
    """Real-time CPU/memory/disk/network per powered-on VM (top 25 by default)."""
    from vmware_monitor.ops.performance import get_vm_performance

    si, _, tgt = get_connection(target, config)
    rows = get_vm_performance(si, vm_name=vm, limit=limit)["items"]
    audit.log_query(target=tgt, resource="vm_performance", query_type="get_vm_performance")
    if not rows:
        console.print("[yellow]No powered-on VM exposed real-time metrics.[/]")
        return
    table = Table(title="VM Performance (real-time, ~20s interval)")
    table.add_column("VM", style="cyan")
    table.add_column("CPU %", justify="right")
    table.add_column("Mem %", justify="right")
    table.add_column("Mem (MB)", justify="right")
    table.add_column("Disk R/W KB/s", justify="right")
    table.add_column("Net KB/s", justify="right")
    for r in rows:
        cpu = r.get("cpu_usage_pct", 0)
        mem = r.get("mem_usage_pct", 0)
        rw = f"{r.get('disk_read_kbps', '-')}/{r.get('disk_write_kbps', '-')}"
        table.add_row(
            r["name"],
            f"[{_pct_style(cpu)}]{cpu}[/]",
            f"[{_pct_style(mem)}]{mem}[/]",
            str(r.get("mem_consumed_mb", "-")),
            rw,
            str(r.get("net_kbps", "-")),
        )
    console.print(table)


# ─── capacity ──────────────────────────────────────────────────────────────


@capacity_app.command("datastores")
@cli_errors
def capacity_datastores(
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: LimitOption = None,
) -> None:
    """Datastore capacity with thin-provisioning over-commit."""
    from vmware_monitor.ops.capacity import get_datastore_capacity

    si, _, tgt = get_connection(target, config)
    rows = get_datastore_capacity(si, limit=limit)["items"]
    audit.log_query(target=tgt, resource="datastore_capacity", query_type="get_datastore_capacity")
    table = Table(title="Datastore Capacity & Over-commit")
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Capacity GB", justify="right")
    table.add_column("Used %", justify="right")
    table.add_column("Provisioned GB", justify="right")
    table.add_column("Over-commit %", justify="right")
    for r in rows:
        oc = r["overcommit_pct"]
        oc_style = "red" if oc > 100 else "yellow" if oc > 80 else "green"
        table.add_row(
            r["name"],
            r["type"],
            f"{r['capacity_gb']}",
            f"[{_pct_style(r['used_pct'])}]{r['used_pct']}[/]",
            f"{r['provisioned_gb']}",
            f"[{oc_style}]{oc}[/]",
        )
    console.print(table)


@capacity_app.command("pools")
@cli_errors
def capacity_pools(
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: LimitOption = None,
) -> None:
    """Resource-pool reservation, limit, and current usage."""
    from vmware_monitor.ops.capacity import get_resource_pool_usage

    si, _, tgt = get_connection(target, config)
    rows = get_resource_pool_usage(si, limit=limit)["items"]
    audit.log_query(target=tgt, resource="resource_pools", query_type="get_resource_pool_usage")
    if not rows:
        console.print("[yellow]No resource pools found.[/]")
        return
    table = Table(title="Resource Pool Usage")
    table.add_column("Pool", style="cyan")
    table.add_column("CPU rsv/lim MHz", justify="right")
    table.add_column("CPU used MHz", justify="right")
    table.add_column("Mem rsv/lim MB", justify="right")
    table.add_column("Mem used MB", justify="right")
    for r in rows:
        cpu_rl = f"{r['cpu_reservation_mhz']}/{r['cpu_limit_mhz']}"
        mem_rl = f"{r['mem_reservation_mb']}/{r['mem_limit_mb']}"
        table.add_row(r["name"], cpu_rl, str(r["cpu_usage_mhz"]), mem_rl, str(r["mem_usage_mb"]))
    console.print(table)


# ─── infra ─────────────────────────────────────────────────────────────────


@infra_app.command("certs")
@cli_errors
def infra_certs(
    warn_days: Annotated[int, typer.Option("--warn-days", help="Flag certs within N days")] = 30,
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: LimitOption = None,
) -> None:
    """ESXi host management certificate expiry."""
    from vmware_monitor.ops.infra_health import get_certificate_status

    si, _, tgt = get_connection(target, config)
    rows = get_certificate_status(si, warn_days=warn_days, limit=limit)["items"]
    audit.log_query(target=tgt, resource="certificates", query_type="get_certificate_status")
    table = Table(title=f"ESXi Certificates (warn < {warn_days}d)")
    table.add_column("Host", style="cyan")
    table.add_column("Expires")
    table.add_column("Days Left", justify="right")
    for r in rows:
        days = r["days_until_expiry"]
        style = "red" if r["expiring"] else "green"
        table.add_row(r["host"], r["not_after"], f"[{style}]{days if days is not None else '?'}[/]")
    console.print(table)


@infra_app.command("licenses")
@cli_errors
def infra_licenses(target: TargetOption = None, config: ConfigOption = None) -> None:
    """vCenter/ESXi license inventory with usage and expiry."""
    from vmware_monitor.ops.infra_health import get_license_status

    si, _, tgt = get_connection(target, config)
    rows = get_license_status(si)["items"]
    audit.log_query(target=tgt, resource="licenses", query_type="get_license_status")
    if not rows:
        console.print("[yellow]No licenses returned.[/]")
        return
    table = Table(title="Licenses")
    table.add_column("Name", style="cyan")
    table.add_column("Used/Total", justify="right")
    table.add_column("Expiration")
    for r in rows:
        total = "∞" if r["unlimited"] else str(r["total"])
        table.add_row(r["name"], f"{r['used']}/{total}", r["expiration"])
    console.print(table)


@infra_app.command("ntp")
@cli_errors
def infra_ntp(
    host: Annotated[str | None, typer.Option("--host", help="Single host by exact name")] = None,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Per-host NTP configuration health (config + ntpd state; not live offset)."""
    from vmware_monitor.ops.infra_health import get_ntp_status

    si, _, tgt = get_connection(target, config)
    result = get_ntp_status(si, host_name=host)
    rows = result["items"]
    audit.log_query(target=tgt, resource="ntp", query_type="get_ntp_status")
    table = Table(title="NTP Configuration (live offset not exposed by SOAP API)")
    table.add_column("Host", style="cyan")
    table.add_column("Servers")
    table.add_column("ntpd")
    table.add_column("Policy")
    table.add_column("Healthy")
    for r in rows:
        # None is a third state everywhere in this row: the host was not read.
        # Rendering it with the same falsy branch as False put the verdict the
        # ops layer had just refused to make straight back on the screen.
        if r["ntp_servers"] is None:
            servers = "[yellow]not read[/]"
        else:
            servers = ", ".join(r["ntp_servers"]) or "[red]none[/]"
        if r["ntpd_running"] is None:
            run = "[yellow]not read[/]"
        else:
            run = "[green]running[/]" if r["ntpd_running"] else "[red]stopped[/]"
        if r["healthy"] is None:
            healthy = "[yellow]unknown[/]"
        else:
            healthy = "[green]yes[/]" if r["healthy"] else "[red]no[/]"
        table.add_row(r["host"], servers, run, r["ntpd_policy"], healthy)
    console.print(table)
    note = result.get("unreachable_note")
    if note:
        # Under the table, where someone scanning the Healthy column for red
        # will still meet it. Without this the yellow rows are the only clue.
        console.print(f"[yellow]{note}[/]")


# ─── snapshots ─────────────────────────────────────────────────────────────


@snapshots_app.command("aging")
@cli_errors
def snapshots_aging(
    threshold: Annotated[int, typer.Option("--threshold", help="Age (days) to flag as old")] = 30,
    only_old: Annotated[bool, typer.Option("--only-old", help="Show only old snapshots")] = False,
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: LimitOption = None,
) -> None:
    """Inventory-wide snapshot aging and sprawl."""
    from vmware_monitor.ops.snapshots import list_snapshot_aging

    si, _, tgt = get_connection(target, config)
    result = list_snapshot_aging(si, age_threshold_days=threshold, only_old=only_old, limit=limit)
    audit.log_query(target=tgt, resource="snapshot_aging", query_type="list_snapshot_aging")
    console.print(
        f"[bold]{result['total_snapshots']}[/] snapshots on "
        f"[bold]{result['vms_with_snapshots']}[/] VMs; "
        f"[red]{result['old_snapshots']}[/] older than {result['threshold_days']}d."
    )
    if not result["snapshots"]:
        console.print("[green]No snapshots match.[/]")
        return
    table = Table(title="Snapshot Aging (oldest first)")
    table.add_column("VM", style="cyan")
    table.add_column("Snapshot")
    table.add_column("Age (days)", justify="right")
    table.add_column("Est. Size MB", justify="right")
    for s in result["snapshots"]:
        age = s["age_days"]
        style = "red" if s["is_old"] else "white"
        size = s.get("est_size_mb", 0) or "-"
        table.add_row(
            s["vm_name"],
            s["snapshot_name"],
            f"[{style}]{age if age is not None else '?'}[/]",
            str(size),
        )
    console.print(table)
    if result["hint"]:
        console.print(f"[yellow]ℹ {result['hint']}[/yellow]")


@snapshots_app.command("backup-window")
@cli_errors
def snapshots_backup_window(
    vm: Annotated[str, typer.Argument(help="Exact VM name")],
    days: Annotated[int, typer.Option("--days", help="How far back to look (1-365)")] = 30,
    cycles: Annotated[bool, typer.Option("--cycles", help="Show every cycle, not just totals")] = False,
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: LimitOption = None,
) -> None:
    """Backup windows for a VM, inferred from its snapshot task history."""
    from vmware_monitor.ops.backup_window import get_backup_snapshot_history

    si, _, tgt = get_connection(target, config)
    result = get_backup_snapshot_history(si, vm, days=days, include_cycles=cycles, limit=limit)
    audit.log_query(target=tgt, resource=vm, query_type="get_backup_snapshot_history")

    if result["history_unavailable"]:
        # Not "no backups" -- the history could not be read at all, and the
        # difference is the whole answer.
        console.print(f"[red]{result['history_unavailable']}[/]")
        return

    console.print(
        f"[bold]{result['vm']}[/] over {result['period_days']}d: "
        f"[bold]{result['complete_cycles']}[/] complete cycle(s), "
        f"[yellow]{result['incomplete_cycles']}[/] unmatched."
    )
    if result["coverage_note"]:
        console.print(f"[yellow]{result['coverage_note']}[/]")

    stats = result["backup_active_hours"]
    if stats:
        table = Table(title="Backup window (hours)")
        table.add_column("Measure", style="cyan")
        table.add_column("Avg", justify="right")
        table.add_column("Min", justify="right")
        table.add_column("Max", justify="right")
        for label, key in (
            ("Backup active", "backup_active_hours"),
            ("Snapshot present", "snapshot_present_hours"),
            ("Total window", "total_window_hours"),
            ("Removal itself", "snapshot_removal_hours"),
        ):
            st = result[key]
            if st:
                table.add_row(label, str(st["average"]), str(st["minimum"]), str(st["maximum"]))
        console.print(table)
    else:
        console.print("[green]No complete snapshot cycles in the window.[/]")

    if cycles and result.get("cycles"):
        ct = Table(title="Cycles (oldest first)")
        ct.add_column("Snapshot created", style="cyan")
        ct.add_column("Removal started")
        ct.add_column("Active h", justify="right")
        for c in result["cycles"]:
            ct.add_row(
                str(c["snapshot_created"]),
                str(c["snapshot_removal_started"]),
                str(c["backup_active_hours"]),
            )
        console.print(ct)
        if result.get("cycles_truncated"):
            console.print("[yellow]Cycle list truncated; raise --limit for more.[/]")

    for row in result["unmatched"]:
        console.print(f"[yellow]! {row['reason']} at {row['started']}[/]")

    console.print(f"[dim]{result['basis']}[/dim]")


# ─── activity ──────────────────────────────────────────────────────────────


@activity_app.command("tasks")
@cli_errors
def activity_tasks(
    all_recent: Annotated[
        bool, typer.Option("--all-recent/--active-only", help="Include completed")
    ] = True,
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: LimitOption = None,
) -> None:
    """In-flight (and recently completed) vCenter tasks."""
    from vmware_monitor.ops.activity import get_active_tasks

    si, _, tgt = get_connection(target, config)
    rows = get_active_tasks(si, include_recent=all_recent, limit=limit)["items"]
    audit.log_query(target=tgt, resource="tasks", query_type="get_active_tasks")
    if not rows:
        console.print("[green]No tasks.[/]")
        return
    table = Table(title="Tasks")
    table.add_column("Task", style="cyan")
    table.add_column("Entity")
    table.add_column("State")
    table.add_column("Progress", justify="right")
    table.add_column("User")
    for r in rows:
        state_style = "yellow" if r["active"] else ("red" if r["error"] else "green")
        table.add_row(
            r["name"],
            r["entity"],
            f"[{state_style}]{r['state']}[/]",
            f"{r['progress_pct']}%",
            r["user"],
        )
    console.print(table)


@activity_app.command("sessions")
@cli_errors
def activity_sessions(
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: LimitOption = None,
) -> None:
    """Currently authenticated vCenter/ESXi sessions."""
    from vmware_monitor.ops.activity import get_active_sessions

    si, _, tgt = get_connection(target, config)
    rows = get_active_sessions(si, limit=limit)["items"]
    audit.log_query(target=tgt, resource="sessions", query_type="get_active_sessions")
    if rows and rows[0].get("user_name") == "N/A" and "note" in rows[0]:
        console.print(f"[yellow]{rows[0]['note']}[/]")
        return
    table = Table(title="Active Sessions")
    table.add_column("User", style="cyan")
    table.add_column("Full Name")
    table.add_column("Last Active")
    table.add_column("IP")
    for r in rows:
        marker = " [green](this)[/]" if r.get("current") else ""
        table.add_row(r["user_name"] + marker, r["full_name"], r["last_active"], r["ip_address"])
    console.print(table)


# ─── summary (opinionated cross-cluster triage) ──────────────────────────────

_STATUS_STYLE = {"critical": "red", "warn": "yellow", "ok": "green"}
_SEV_STYLE = {"critical": "red", "warning": "yellow"}


def _slug(text: str) -> str:
    """Filesystem-safe slug from a target name (for the snapshot filename)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "vcenter"


def write_html_snapshot(data: dict, vcenter: str, explicit_path: Path | None) -> None:
    """Render the summary to a self-contained HTML file and report the path.

    Default location is ~/vmware-health/cluster-health-<vc>-<YYYYMMDD-HHMMSS>.html
    so a folder of snapshots becomes a browsable point-in-time history. The file
    is fully offline (no external CSS/JS/fonts) — nothing leaves the machine.
    """
    from vmware_monitor.ops.health_html import render_cluster_health_html

    now = datetime.now().astimezone()
    if explicit_path is not None:
        path = explicit_path.expanduser()
    else:
        ts = now.strftime("%Y%m%d-%H%M%S")
        path = Path.home() / "vmware-health" / f"cluster-health-{_slug(vcenter)}-{ts}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = render_cluster_health_html(data, vcenter, now, filename=path.name)
    path.write_text(document, encoding="utf-8")
    console.print(f"[green]Wrote cluster-health snapshot →[/] {path}")
    console.print(f"[dim]Open it: open '{path}'  (offline file, nothing uploaded)[/]")


def _render_top_issues(data: dict, top: int) -> None:
    """Print the ranked top-N anomaly focus list (the large-fleet headline)."""
    if top <= 0:
        return
    issues = data.get("top_issues", [])
    total = data.get("issues_total", 0)
    if not issues:
        console.print("[green]No issues detected — every cluster is OK.[/]\n")
        return
    shown = len(issues)
    title = f"Top {shown} issues" + (f" (of {total})" if total > shown else "")
    tbl = Table(title=title)
    tbl.add_column("#", justify="right", style="dim")
    tbl.add_column("Severity")
    tbl.add_column("Object", style="cyan")
    tbl.add_column("Cluster")
    tbl.add_column("Problem")
    tbl.add_column("Next step", style="dim")
    for n, i in enumerate(issues, 1):
        sev = i["severity"]
        tbl.add_row(
            str(n),
            f"[{_SEV_STYLE.get(sev, 'white')}]{sev.upper()}[/]",
            i["object"],
            i.get("cluster") or "—",
            i["detail"],
            i.get("drilldown", ""),
        )
    console.print(tbl)


@cli_errors
def cluster_summary_cmd(
    cluster: Annotated[
        str | None,
        typer.Option("--cluster", help="Show only clusters matching this substring"),
    ] = None,
    no_vms: Annotated[
        bool,
        typer.Option("--no-vms", help="Skip the VM rollup pass (faster on huge fleets)"),
    ] = False,
    top: Annotated[
        int,
        typer.Option("--top", help="Size of the top-issues focus list (0 to hide)"),
    ] = 10,
    html: Annotated[
        bool,
        typer.Option(
            "--html", help="Write an offline HTML snapshot to ~/vmware-health/ (timestamped)"
        ),
    ] = False,
    html_path: Annotated[
        Path | None,
        typer.Option(
            "--html-path", help="Write the HTML snapshot to this exact path (implies --html)"
        ),
    ] = None,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """One-glance cluster health: is anything on fire?

    Leads with the top-N individual anomalies (the focus list), then an
    opinionated per-cluster table (Problems / Capacity / Health → one status).
    Adjust it in references/health-summary-template.md, or just ask the assistant
    to add or drop columns. Pass --html for an offline, shareable snapshot file
    (timestamped filename so a folder of them becomes a browsable history).
    """
    from vmware_monitor.ops.cluster_summary import get_cluster_health_summary

    si, _, tgt = get_connection(target, config)
    data = get_cluster_health_summary(si, cluster_filter=cluster, include_vms=not no_vms, top_n=top)
    audit.log_query(target=tgt, resource="clusters", query_type="cluster_health_summary")

    if html or html_path is not None:
        write_html_snapshot(data, tgt, html_path)
        return

    render_summary_console(data, top)


def render_summary_console(data: dict, top: int) -> None:
    """Render a cluster-health summary dict to the terminal (header + top-N + table).

    Public so a companion skill (e.g. vmware-aiops, which delegates to this same
    aggregation via the vmware-monitor library) can present an identical view
    without duplicating the rendering.
    """
    t = data["totals"]
    worst = t["worst_status"]
    console.print(
        f"[bold]Cluster health[/] — {t['clusters']} clusters, "
        f"{t['hosts_connected']}/{t['hosts_total']} hosts connected"
        + (f", {t.get('vms_on', 0)}/{t.get('vms_total', 0)} VMs on" if "vms_total" in t else "")
        + f"  ·  overall: [{_STATUS_STYLE[worst]}]{worst.upper()}[/]"
    )

    _render_top_issues(data, top)

    table = Table(title="Cluster Health Summary")
    table.add_column("Status")
    table.add_column("Cluster", style="cyan")
    table.add_column("Hosts", justify="right")
    if "vms_total" in t:
        table.add_column("VMs on", justify="right")
    table.add_column("CPU%", justify="right")
    table.add_column("Mem%", justify="right")
    table.add_column("HA")
    table.add_column("DRS")
    table.add_column("Alarms C/W", justify="right")
    table.add_column("Attention")
    for c in data["clusters"]:
        style = _STATUS_STYLE.get(c["status"], "white")
        row = [
            f"[{style}]{c['status'].upper()}[/]",
            c["name"],
            f"{c['hosts_connected']}/{c['hosts_total']}",
        ]
        if "vms_total" in t:
            row.append(f"{c.get('vms_on', 0)}/{c.get('vms_total', 0)}")
        row += [
            f"[{_pct_style(c['cpu_used_pct'])}]{c['cpu_used_pct']}[/]",
            f"[{_pct_style(c['mem_used_pct'])}]{c['mem_used_pct']}[/]",
            "[green]ON[/]" if c["ha_enabled"] else "[red]OFF[/]",
            "[green]ON[/]" if c["drs_enabled"] else "[dim]off[/]",
            f"{c['alarms']['critical']}/{c['alarms']['warning']}",
            "; ".join(c["attention"]) or "[dim]—[/]",
        ]
        table.add_row(*row)
    console.print(table)
    console.print(f"[dim]{data['customization_hint']}[/]")


# ─── investigate (object-centered drill-down bundles) ────────────────────────


def write_bundle_html_snapshot(
    bundle: dict, kind: str, vcenter: str, explicit_path: Path | None
) -> None:
    """Render an investigation bundle to a self-contained HTML file, report the path.

    Default location mirrors the cluster-health snapshot:
    ``~/vmware-health/investigate-<kind>-<object>-<YYYYMMDD-HHMMSS>.html`` — fully
    offline (no external CSS/JS/fonts), nothing leaves the machine.
    """
    from vmware_monitor.ops.investigate_html import render_bundle_html

    now = datetime.now().astimezone()
    obj_name = _slug(str(bundle.get("object", {}).get("name", kind)))
    if explicit_path is not None:
        path = explicit_path.expanduser()
    else:
        ts = now.strftime("%Y%m%d-%H%M%S")
        path = Path.home() / "vmware-health" / f"investigate-{kind}-{obj_name}-{ts}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = render_bundle_html(bundle, kind, vcenter, now, filename=path.name)
    path.write_text(document, encoding="utf-8")
    console.print(f"[green]Wrote {kind} investigation snapshot →[/] {path}")
    console.print(f"[dim]Open it: open '{path}'  (offline file, nothing uploaded)[/]")


def render_bundle_console(bundle: dict, kind: str) -> None:
    """Print an investigation bundle to the terminal (context + timeline + detail).

    Public so vmware-aiops can present the identical view via the delegated library
    without duplicating rendering.
    """
    obj = bundle.get("object", {})
    console.print(
        f"[bold]{kind.upper()} investigation[/] — {obj.get('name', '')}"
        f"  ·  status: {obj.get('status', 'gray')}"
    )
    host = bundle.get("host")
    if host:
        console.print(
            f"  host: [cyan]{host['name']}[/] ({host['connection']}, "
            f"CPU {host['cpu_pct']}% / Mem {host['mem_pct']}%)"
        )
    cluster = bundle.get("cluster")
    if cluster:
        console.print(
            f"  cluster: [cyan]{cluster['name']}[/] "
            f"(HA {'on' if cluster['ha_enabled'] else 'off'}, "
            f"DRS {'on' if cluster['drs_enabled'] else 'off'})"
        )
    for h in bundle.get("hosts", []):
        console.print(
            f"  host: [cyan]{h['name']}[/] ({h['connection']}, "
            f"CPU {h['cpu_pct']}% / Mem {h['mem_pct']}%)"
        )
    for ds in bundle.get("datastores", []):
        console.print(f"  datastore: [cyan]{ds['name']}[/] ({ds['free_pct']}% free)")
    vms = bundle.get("vms")
    if vms is not None:
        sample = ", ".join(vms.get("sample", []))
        extra = f" — {sample}" if sample else ""
        console.print(f"  VMs: {vms['powered_on']}/{vms['total']} powered on{extra}")

    alarms = bundle.get("alarms", [])
    if alarms:
        atbl = Table(title=f"Alarms ({len(alarms)})")
        atbl.add_column("Severity")
        atbl.add_column("Alarm", style="cyan")
        atbl.add_column("On")
        for a in alarms:
            atbl.add_row(
                f"[{_SEV_STYLE.get(a['severity'], 'white')}]{a['severity'].upper()}[/]",
                a["name"],
                f"{a['scope']} {a['object']}",
            )
        console.print(atbl)

    timeline = bundle.get("timeline", [])
    ttl = Table(title=f"Event timeline · last {bundle.get('hours', 24)}h ({len(timeline)})")
    ttl.add_column("Time", style="dim")
    ttl.add_column("Sev")
    ttl.add_column("Source")
    ttl.add_column("Message")
    for e in timeline:
        ttl.add_row(
            e["time"],
            f"[{_SEV_STYLE.get(e['severity'], 'white')}]{e['severity'][:4].upper()}[/]",
            f"{e['scope']} {e['entity']}",
            e["message"],
        )
    console.print(ttl)
    console.print(f"[dim]{bundle.get('customization_hint', '')}[/]")


HoursOption = Annotated[int, typer.Option("--hours", help="Event-timeline look-back window")]
HtmlFlag = Annotated[
    bool, typer.Option("--html", help="Write an offline HTML snapshot to ~/vmware-health/")
]
HtmlPathOption = Annotated[
    Path | None,
    typer.Option("--html-path", help="Write the HTML snapshot to this exact path (implies --html)"),
]


@investigate_app.command("vm")
@cli_errors
def investigate_vm_cmd(
    vm_name: Annotated[str, typer.Argument(help="Exact VM name to investigate")],
    hours: HoursOption = 24,
    html: HtmlFlag = False,
    html_path: HtmlPathOption = None,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """"What is happening around this VM?" — correlated drill-down.

    Aggregates the VM's state, its host/cluster/datastore context, snapshots,
    alarms, live performance, and a merged event timeline correlating recent
    events from the VM, host, cluster and datastores. Pass --html for an offline,
    shareable snapshot (drill-down detail in collapsible sections).
    """
    from vmware_monitor.ops.investigate_vm import get_vm_investigation_bundle

    si, _, tgt = get_connection(target, config)
    bundle = get_vm_investigation_bundle(si, vm_name, hours=hours)
    audit.log_query(target=tgt, resource=vm_name, query_type="vm_investigation_bundle")

    if html or html_path is not None:
        write_bundle_html_snapshot(bundle, "vm", tgt, html_path)
        return
    render_bundle_console(bundle, "vm")


@investigate_app.command("host")
@cli_errors
def investigate_host_cmd(
    host_name: Annotated[str, typer.Argument(help="Exact ESXi host name to investigate")],
    hours: HoursOption = 24,
    html: HtmlFlag = False,
    html_path: HtmlPathOption = None,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """"What is happening around this ESXi host?" — correlated drill-down.

    Aggregates the host's state, its cluster context, the VMs it runs, the
    datastores it mounts, alarms, live performance, and a merged event timeline
    correlating the host, cluster and datastores. Pass --html for an offline
    shareable snapshot.
    """
    from vmware_monitor.ops.investigate_host import get_host_investigation_bundle

    si, _, tgt = get_connection(target, config)
    bundle = get_host_investigation_bundle(si, host_name, hours=hours)
    audit.log_query(target=tgt, resource=host_name, query_type="host_investigation_bundle")

    if html or html_path is not None:
        write_bundle_html_snapshot(bundle, "host", tgt, html_path)
        return
    render_bundle_console(bundle, "host")


@investigate_app.command("datastore")
@cli_errors
def investigate_datastore_cmd(
    datastore_name: Annotated[str, typer.Argument(help="Exact datastore name to investigate")],
    hours: HoursOption = 24,
    html: HtmlFlag = False,
    html_path: HtmlPathOption = None,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """"What is happening around this datastore?" — correlated drill-down.

    Aggregates the datastore's capacity/free space, the hosts that mount it, the
    VMs it backs, alarms, and a merged event timeline correlating the datastore
    and its hosts. Pass --html for an offline shareable snapshot.
    """
    from vmware_monitor.ops.investigate_datastore import get_datastore_investigation_bundle

    si, _, tgt = get_connection(target, config)
    bundle = get_datastore_investigation_bundle(si, datastore_name, hours=hours)
    audit.log_query(
        target=tgt, resource=datastore_name, query_type="datastore_investigation_bundle"
    )

    if html or html_path is not None:
        write_bundle_html_snapshot(bundle, "datastore", tgt, html_path)
        return
    render_bundle_console(bundle, "datastore")


# ─── attention (cross-vCenter "what needs attention now?") ───────────────────


def render_attention_console(data: dict) -> None:
    """Print the cross-vCenter attention view to the terminal.

    Public so vmware-aiops can present the identical view via the delegated library.
    """
    t = data["totals"]
    console.print(
        f"[bold]What needs attention[/] — {t['vcenters']} vCenters, "
        f"{t['clusters']} clusters, {t['hosts_connected']}/{t['hosts_total']} hosts connected"
        f"  ·  overall: [{_STATUS_STYLE[t['worst_status']]}]{t['worst_status'].upper()}[/]"
    )
    for u in data.get("unreachable", []):
        console.print(f"[yellow]unreachable:[/] {u['vcenter']} ({u['reason']})")

    _render_top_issues(_with_vcenter_cluster(data), data.get("issues_total", 0))

    table = Table(title="vCenters (worst status first)")
    table.add_column("Status")
    table.add_column("vCenter", style="cyan")
    table.add_column("Clusters", justify="right")
    table.add_column("Hosts", justify="right")
    table.add_column("Alarms C/W", justify="right")
    for tg in data["targets"]:
        style = _STATUS_STYLE.get(tg["worst_status"], "white")
        table.add_row(
            f"[{style}]{tg['worst_status'].upper()}[/]",
            tg["vcenter"],
            str(tg["clusters"]),
            f"{tg['hosts_connected']}/{tg['hosts_total']}",
            f"{tg['alarms']['critical']}/{tg['alarms']['warning']}",
        )
    console.print(table)
    console.print(f"[dim]{data.get('customization_hint', '')}[/]")


def _with_vcenter_cluster(data: dict) -> dict:
    """Fold each issue's vCenter into its cluster label so the shared top-issues
    table (which prints a 'Cluster' column) shows which estate it belongs to."""
    issues = []
    for i in data.get("top_issues", []):
        j = dict(i)
        j["cluster"] = f"{i.get('vcenter', '')}/{i.get('cluster') or '—'}"
        issues.append(j)
    return {"top_issues": issues, "issues_total": data.get("issues_total", 0)}


def write_attention_html_snapshot(data: dict, explicit_path: Path | None) -> None:
    """Render the attention view to a self-contained HTML file and report the path."""
    from vmware_monitor.ops.attention_html import render_attention_html

    now = datetime.now().astimezone()
    if explicit_path is not None:
        path = explicit_path.expanduser()
    else:
        ts = now.strftime("%Y%m%d-%H%M%S")
        path = Path.home() / "vmware-health" / f"attention-{ts}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_attention_html(data, now, filename=path.name), encoding="utf-8")
    console.print(f"[green]Wrote cross-vCenter attention snapshot →[/] {path}")
    console.print(f"[dim]Open it: open '{path}'  (offline file, nothing uploaded)[/]")


@cli_errors
def attention_cmd(
    cluster: Annotated[
        str | None, typer.Option("--cluster", help="Show only clusters matching this substring")
    ] = None,
    top: Annotated[int, typer.Option("--top", help="Size of the merged top-issues list")] = 10,
    html: HtmlFlag = False,
    html_path: HtmlPathOption = None,
    config: ConfigOption = None,
) -> None:
    """Cross-vCenter "what needs attention now?" — one ranked list across all targets.

    Rolls every configured vCenter's cluster health into a single globally-ranked
    top-issues list plus a per-vCenter table. A target that can't be reached is
    listed as unreachable and the rest still aggregate. Pass --html for an offline
    shareable snapshot.
    """
    from vmware_monitor.ops.attention import get_cross_vcenter_attention

    sessions, unreachable = get_all_connections(config)
    data = get_cross_vcenter_attention(
        sessions, unreachable=unreachable, cluster_filter=cluster, top_n=top
    )
    audit.log_query(target="*", resource="all-vcenters", query_type="cross_vcenter_attention")

    if html or html_path is not None:
        write_attention_html_snapshot(data, html_path)
        return
    render_attention_console(data)


def register(app: typer.Typer) -> None:
    """Attach all observability sub-apps to the root CLI."""
    app.add_typer(perf_app, name="perf")
    app.add_typer(capacity_app, name="capacity")
    app.add_typer(infra_app, name="infra")
    app.add_typer(snapshots_app, name="snapshots")
    app.add_typer(activity_app, name="activity")
    app.add_typer(investigate_app, name="investigate")
    app.command("summary")(cluster_summary_cmd)
    app.command("attention")(attention_cmd)
