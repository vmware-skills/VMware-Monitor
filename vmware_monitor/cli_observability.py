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

import sys as _sys
import sys as _sys
import re  # noqa: F401 — compatibility/runtime export
from datetime import datetime  # noqa: F401 — compatibility/runtime export
from pathlib import Path  # noqa: F401 — compatibility/runtime export
from typing import Annotated

import typer
from rich.table import Table

from vmware_monitor.cli_base import (  # noqa: F401 — compatibility/runtime export
    ConfigOption,
    TargetOption,
    audit,
    cli_errors,
    console,
    get_all_connections,
    get_connection,
)
from vmware_policy import audited

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
@audited("get_host_performance")
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
@audited("get_vm_performance")
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
    table.add_column("Active mem %", justify="right")
    table.add_column("Consumed MB", justify="right")
    table.add_column("Balloon MB", justify="right")
    table.add_column("Swap MB", justify="right")
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
            _pressure_cell(r.get("mem_ballooned_mb")),
            _pressure_cell(r.get("mem_swapped_mb")),
            rw,
            str(r.get("net_kbps", "-")),
        )
    console.print(table)
    console.print(
        "[dim]Active mem % = mem.usage.average (active guest memory / configured — "
        "not consumed). Balloon = mem.vmmemctl.average, Swap = mem.swapped.average; "
        "above 0 means the host is reclaiming this VM's memory.[/]"
    )


def _pressure_cell(value: float | None) -> str:
    """Balloon/swap cell: '-' when unreported, yellow when the host reclaimed memory."""
    if value is None:
        return "-"
    return f"[yellow]{value}[/]" if value > 0 else str(value)


# ─── capacity ──────────────────────────────────────────────────────────────


@capacity_app.command("datastores")
@cli_errors
@audited("get_datastore_capacity")
def capacity_datastores(
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: LimitOption = None,
) -> None:
    """Datastore capacity with thin-provisioning over-commit."""
    from vmware_monitor.ops.capacity import DATASTORE_OVERCOMMIT_WARN_PCT, get_datastore_capacity

    si, _, tgt = get_connection(target, config)
    result = get_datastore_capacity(si, limit=limit)
    rows = result["items"]
    audit.log_query(target=tgt, resource="datastore_capacity", query_type="get_datastore_capacity")
    table = Table(title=f"Datastore Capacity & Over-commit ({result.get('view', 'unknown')} view)")
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Capacity GB", justify="right")
    table.add_column("Used %", justify="right")
    table.add_column("Provisioned GB", justify="right")
    table.add_column("VMs", justify="right")
    table.add_column("Over-commit %", justify="right")
    for r in rows:
        oc = r["overcommit_pct"]
        oc_style = "red" if oc > DATASTORE_OVERCOMMIT_WARN_PCT else "yellow" if oc > 80 else "green"
        table.add_row(
            r["name"],
            r["type"],
            f"{r['capacity_gb']}",
            f"[{_pct_style(r['used_pct'])}]{r['used_pct']}[/]",
            f"{r['provisioned_gb']}",
            str(r.get("vm_count", "-")),
            f"[{oc_style}]{oc}[/]",
        )
    console.print(table)
    for r in rows:
        if r.get("vms_not_connected"):
            console.print(
                f"[yellow]{r['name']}: provisioned space includes VM(s) not reported as "
                f"connected — {', '.join(r['vms_not_connected'])}[/]"
            )
    if result.get("view_note"):
        console.print(f"[dim]{result['view_note']}[/]")


@capacity_app.command("pools")
@cli_errors
@audited("get_resource_pool_usage")
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
@audited("get_certificate_status")
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
@audited("get_license_status")
def infra_licenses(target: TargetOption = None, config: ConfigOption = None) -> None:
    """vCenter/ESXi license inventory with usage and expiry."""
    from vmware_monitor.ops.infra_health import get_license_status

    si, _, tgt = get_connection(target, config)
    result = get_license_status(si)
    rows = result["items"]
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
    assignments = result.get("assignments")
    if assignments:
        assigned = Table(title="Assigned to")
        for column in ("Asset", "Kind", "License", "Expiration", "Expired"):
            assigned.add_column(column)
        for a in assignments:
            expired = {True: "[red]yes[/]", False: "no"}.get(a["expired"], "unknown")
            assigned.add_row(
                a["asset"], a["kind"], a["license_name"] or "", a["expiration"], expired
            )
        console.print(assigned)
    for key in ("assignments_note", "assignments_expired_note"):
        if result.get(key):
            console.print(f"[yellow]{result[key]}[/]")


@infra_app.command("ntp")
@cli_errors
@audited("get_ntp_status")
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
    for key in ("unreachable_note", "unreadable_note"):
        # Under the table, where someone scanning the Healthy column for red
        # will still meet it. Without this the yellow rows are the only clue.
        if result.get(key):
            console.print(f"[yellow]{result[key]}[/]")
    if result.get("ntp_sources_note"):
        console.print(f"[yellow]{result['ntp_sources_note']}[/]")


# ─── snapshots ─────────────────────────────────────────────────────────────


@snapshots_app.command("aging")
@cli_errors
@audited("find_aging_snapshots")
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
@audited("get_vm_backup_snapshot_history")
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
        f"{result['incomplete_cycles']} unmatched."
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

    # An open cycle is not a failure. Printing the count in yellow and every
    # unmatched row with a "!" is what made a running backup read as a broken
    # one (issue #26, field report 2026-09-01) — the payload learned to tell
    # them apart and this renderer had not.
    for row in result["unmatched"]:
        running = row.get("possibly_in_progress")
        if running is True:
            console.print(
                f"[cyan]· open {row['age_hours']}h at {row['started']} — inside this VM's "
                f"usual {row['longest_completed_cycle_hours']}h envelope, so most likely a "
                f"backup still running.[/]"
            )
        elif running is False:
            console.print(
                f"[yellow]! open {row['age_hours']}h at {row['started']} — longer than this "
                f"VM's longest completed cycle ({row['longest_completed_cycle_hours']}h). "
                f"Worth checking whether the snapshot was left behind.[/]"
            )
        elif running is None and row.get("status") == "open":
            console.print(
                f"[dim]· open at {row['started']} — no completed cycle on this VM to compare "
                f"against, so whether a backup is still running cannot be told from task "
                f"history.[/dim]"
            )
        else:
            console.print(f"[yellow]! {row['reason']} at {row['started']}[/]")

    console.print(f"[dim]{result['basis']}[/dim]")


# ─── activity ──────────────────────────────────────────────────────────────


@activity_app.command("tasks")
@cli_errors
@audited("list_active_tasks")
def activity_tasks(
    all_recent: Annotated[
        bool, typer.Option("--all-recent/--active-only", help="Include completed")
    ] = True,
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: LimitOption = None,
) -> None:
    """In-flight (and recently completed) vCenter tasks."""
    from rich.markup import escape

    from vmware_monitor.ops.activity import get_active_tasks

    si, _, tgt = get_connection(target, config)
    result = get_active_tasks(si, include_recent=all_recent, limit=limit)
    rows = result["items"]
    audit.log_query(target=tgt, resource="tasks", query_type="get_active_tasks")
    # An unread task is not an idle one: printing a green "No tasks." over 11
    # unreadable ones (lab ESXi, 2026-09-15) would say the opposite of the data.
    note = result.get("unreadable_note")
    if not rows:
        if note:
            console.print(f"[yellow]No readable tasks. {escape(note)}[/]")
        else:
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
    # A table of the readable tasks alone reads as "these are all of them"
    # (independent review, 2026-09-15), so the unread ones are said here too.
    if note:
        console.print(f"[yellow]{escape(note)}[/]")


@activity_app.command("sessions")
@cli_errors
@audited("list_active_sessions")
def activity_sessions(
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: LimitOption = None,
    include_service: Annotated[
        bool, typer.Option("--include-service", help="List vCenter's own service sessions too")
    ] = False,
) -> None:
    """Currently authenticated vCenter/ESXi sessions."""
    from vmware_monitor.ops.activity import get_active_sessions

    si, _, tgt = get_connection(target, config)
    result = get_active_sessions(si, limit=limit, include_service=include_service)
    rows = result["items"]
    audit.log_query(target=tgt, resource="sessions", query_type="get_active_sessions")
    if rows and rows[0].get("user_name") == "N/A" and "note" in rows[0]:
        console.print(f"[yellow]{rows[0]['note']}[/]")
        return
    table = Table(title="Active Sessions")
    table.add_column("User", style="cyan")
    table.add_column("Client")
    table.add_column("Calls", justify="right")
    table.add_column("Last Active")
    table.add_column("IP")
    for r in rows:
        marker = " [green](this)[/]" if r.get("current") else ""
        calls = r.get("call_count")
        table.add_row(
            r["user_name"] + marker,
            r.get("user_agent") or "",
            "" if calls is None else str(calls),
            r["last_active"],
            r["ip_address"],
        )
    console.print(table)
    if result.get("service_note"):
        console.print(f"[dim]{result['service_note']}[/]")


# ─── summary (opinionated cross-cluster triage) ──────────────────────────────

_STATUS_STYLE = {"critical": "red", "warn": "yellow", "ok": "green"}
_SEV_STYLE = {"critical": "red", "warning": "yellow"}














# ─── investigate (object-centered drill-down bundles) ────────────────────────






from vmware_monitor.cli_report_options import HoursOption as HoursOption, HtmlFlag as HtmlFlag, HtmlPathOption as HtmlPathOption


@investigate_app.command("vm")
@cli_errors
@audited("investigate_vm")
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
    audit.log_query(target=tgt, resource=vm_name, query_type="investigate_vm")

    if html or html_path is not None:
        write_bundle_html_snapshot(bundle, "vm", tgt, html_path)
        return
    render_bundle_console(bundle, "vm")


@investigate_app.command("host")
@cli_errors
@audited("investigate_host")
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
    audit.log_query(target=tgt, resource=host_name, query_type="investigate_host")

    if html or html_path is not None:
        write_bundle_html_snapshot(bundle, "host", tgt, html_path)
        return
    render_bundle_console(bundle, "host")


@investigate_app.command("datastore")
@cli_errors
@audited("investigate_datastore")
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
        target=tgt, resource=datastore_name, query_type="investigate_datastore"
    )

    if html or html_path is not None:
        write_bundle_html_snapshot(bundle, "datastore", tgt, html_path)
        return
    render_bundle_console(bundle, "datastore")


# ─── attention (cross-vCenter "what needs attention now?") ───────────────────











from vmware_monitor._console_reports import register_callbacks as _register_console_reports

(
    _slug,
    write_html_snapshot,
    _render_top_issues,
    _setting_cell,
    render_summary_console,
    write_bundle_html_snapshot,
    render_bundle_console,
    render_attention_console,
    _plural,
    _target_counts,
    _with_vcenter_cluster,
    write_attention_html_snapshot,
) = _register_console_reports(_sys.modules[__name__])



from vmware_monitor._cli_reports import register_callbacks as _register_cli_reports

(
    cluster_summary_cmd,
    attention_cmd,
) = _register_cli_reports(_sys.modules[__name__])


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
