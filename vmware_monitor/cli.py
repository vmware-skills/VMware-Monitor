"""CLI entry point for VMware Monitor (read-only).

This CLI contains ONLY read-only commands. No destructive operations exist:
- No power-on/off, reset, suspend
- No create, delete, reconfigure VM
- No snapshot-create, snapshot-revert, snapshot-delete
- No clone, migrate
- No destructive safety helpers (double-confirm, state-preview, param-validation)
"""

from __future__ import annotations

import signal
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from vmware_monitor import cli_observability, cli_vsphere91
from vmware_monitor.cli_base import (
    ConfigOption,
    TargetOption,
    console,
)
from vmware_monitor.cli_base import (
    audit as _audit,
)
from vmware_monitor.cli_base import (
    cli_errors as _cli_errors,
)
from vmware_monitor.cli_base import (
    get_connection as _get_connection,
)
from vmware_monitor.config import CONFIG_DIR

app = typer.Typer(
    name="vmware-monitor",
    help="VMware vCenter/ESXi read-only monitoring. No destructive operations.",
    no_args_is_help=True,
)

# Sub-commands (read-only only)
inventory_app = typer.Typer(help="Query vCenter/ESXi inventory (read-only).")
health_app = typer.Typer(help="Health checks: alarms, events, sensors, services (read-only).")
vm_app = typer.Typer(help="VM info and snapshot list (read-only).")
scan_app = typer.Typer(help="Log and alarm scanning (read-only).")
daemon_app = typer.Typer(help="Scanner daemon management.")

app.add_typer(inventory_app, name="inventory")
app.add_typer(health_app, name="health")
app.add_typer(vm_app, name="vm")
app.add_typer(scan_app, name="scan")
app.add_typer(daemon_app, name="daemon")

# Observability command groups (perf/capacity/infra/snapshots/activity).
cli_observability.register(app)

# vSphere 9.1 read command groups (memory tiering / vLCM patch / deployment size).
cli_vsphere91.register(app)


# ─── Inventory ────────────────────────────────────────────────────────────────


@inventory_app.command("vms")
@_cli_errors
def inventory_vms(
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="Max VMs to show")] = None,
    sort_by: Annotated[
        str,
        typer.Option("--sort-by", help="name|cpu|memory_mb|power_state|folder_path"),
    ] = "name",
    power_state: Annotated[
        str | None,
        typer.Option("--power-state", help="Filter: poweredOn|poweredOff|suspended"),
    ] = None,
    folder_filter: Annotated[
        str | None,
        typer.Option(
            "--folder-filter",
            help="Case-insensitive substring match on folder_path (e.g. 'Production')",
        ),
    ] = None,
) -> None:
    """List virtual machines."""
    from vmware_monitor.ops.inventory import list_vms

    si, _, tgt = _get_connection(target, config)
    result = list_vms(
        si,
        limit=limit,
        sort_by=sort_by,
        power_state=power_state,
        folder_filter=folder_filter,
    )
    _audit.log_query(target=tgt, resource="virtual_machines", query_type="list_vms")
    vms = result["items"]
    total = result["total"]
    mode = result["mode"]
    hint = result["hint"]
    title = f"Virtual Machines ({total} total"
    if mode == "compact":
        title += ", compact mode"
    title += ")"
    if power_state:
        title += f" [{power_state}]"
    if folder_filter:
        title += f" [folder~{folder_filter}]"
    if limit:
        title += f" (top {limit})"
    table = Table(title=title)
    table.add_column("Name", style="cyan")
    table.add_column("Power")
    table.add_column("CPUs", justify="right")
    table.add_column("Memory (MB)", justify="right")
    if mode == "full":
        table.add_column("Guest OS")
        table.add_column("IP Address")
    for vm in vms:
        power_style = "green" if vm["power_state"] == "poweredOn" else "red"
        row = [
            vm["name"],
            f"[{power_style}]{vm['power_state']}[/]",
            str(vm.get("cpu", "-")),
            str(vm.get("memory_mb", "-")),
        ]
        if mode == "full":
            row.append(vm.get("guest_os", "-"))
            row.append(vm.get("ip_address") or "-")
        table.add_row(*row)
    console.print(table)
    if hint:
        console.print(f"[yellow]ℹ {hint}[/yellow]")


@inventory_app.command("hosts")
@_cli_errors
def inventory_hosts(target: TargetOption = None, config: ConfigOption = None) -> None:
    """List all ESXi hosts."""
    from vmware_monitor.ops.inventory import list_hosts

    si, _, tgt = _get_connection(target, config)
    hosts = list_hosts(si)["items"]
    _audit.log_query(target=tgt, resource="esxi_hosts", query_type="list_hosts")
    table = Table(title="ESXi Hosts")
    table.add_column("Name", style="cyan")
    table.add_column("State")
    table.add_column("CPU Cores", justify="right")
    table.add_column("Memory (GB)", justify="right")
    table.add_column("VMs", justify="right")
    for h in hosts:
        state_style = "green" if h["connection_state"] == "connected" else "red"
        table.add_row(
            h["name"],
            f"[{state_style}]{h['connection_state']}[/]",
            str(h["cpu_cores"]),
            str(h["memory_gb"]),
            str(h["vm_count"]),
        )
    console.print(table)


@inventory_app.command("datastores")
@_cli_errors
def inventory_datastores(target: TargetOption = None, config: ConfigOption = None) -> None:
    """List all datastores."""
    from vmware_monitor.ops.inventory import list_datastores

    si, _, tgt = _get_connection(target, config)
    stores = list_datastores(si)["items"]
    _audit.log_query(target=tgt, resource="datastores", query_type="list_datastores")
    table = Table(title="Datastores")
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Free (GB)", justify="right")
    table.add_column("Total (GB)", justify="right")
    table.add_column("Usage %", justify="right")
    for ds in stores:
        pct = ((ds["total_gb"] - ds["free_gb"]) / ds["total_gb"] * 100) if ds["total_gb"] else 0
        pct_style = "red" if pct > 85 else "yellow" if pct > 70 else "green"
        table.add_row(
            ds["name"],
            ds["type"],
            f"{ds['free_gb']:.1f}",
            f"{ds['total_gb']:.1f}",
            f"[{pct_style}]{pct:.1f}%[/]",
        )
    console.print(table)


@inventory_app.command("clusters")
@_cli_errors
def inventory_clusters(target: TargetOption = None, config: ConfigOption = None) -> None:
    """List all clusters."""
    from vmware_monitor.ops.inventory import list_clusters

    si, _, tgt = _get_connection(target, config)
    clusters = list_clusters(si)["items"]
    _audit.log_query(target=tgt, resource="clusters", query_type="list_clusters")
    table = Table(title="Clusters")
    table.add_column("Name", style="cyan")
    table.add_column("Hosts", justify="right")
    table.add_column("DRS")
    table.add_column("HA")
    for c in clusters:
        table.add_row(
            c["name"],
            str(c["host_count"]),
            "[green]ON[/]" if c["drs_enabled"] else "[red]OFF[/]",
            "[green]ON[/]" if c["ha_enabled"] else "[red]OFF[/]",
        )
    console.print(table)


@inventory_app.command("networks")
@_cli_errors
def inventory_networks(target: TargetOption = None, config: ConfigOption = None) -> None:
    """List all networks."""
    from vmware_monitor.ops.inventory import list_networks

    si, _, tgt = _get_connection(target, config)
    networks = list_networks(si)["items"]
    _audit.log_query(target=tgt, resource="networks", query_type="list_networks")
    table = Table(title="Networks")
    table.add_column("Name", style="cyan")
    table.add_column("VMs", justify="right")
    table.add_column("Accessible")
    for n in networks:
        table.add_row(
            n["name"],
            str(n["vm_count"]),
            "[green]yes[/]" if n["accessible"] else "[red]no[/]",
        )
    console.print(table)


# ─── Health ───────────────────────────────────────────────────────────────────


@health_app.command("alarms")
@_cli_errors
def health_alarms(target: TargetOption = None, config: ConfigOption = None) -> None:
    """Show active alarms."""
    from vmware_monitor.ops.health import get_active_alarms

    si, _, tgt = _get_connection(target, config)
    alarms = get_active_alarms(si)["items"]
    _audit.log_query(target=tgt, resource="alarms", query_type="get_active_alarms")
    if not alarms:
        console.print("[green]No active alarms.[/]")
        return
    table = Table(title="Active Alarms")
    table.add_column("Severity")
    table.add_column("Alarm", style="cyan")
    table.add_column("Entity")
    table.add_column("Time")
    for a in alarms:
        sev_style = {"red": "red", "yellow": "yellow"}.get(a["severity"], "white")
        table.add_row(
            f"[{sev_style}]{a['severity']}[/]",
            a["alarm_name"],
            a["entity_name"],
            a["time"],
        )
    console.print(table)


@health_app.command("events")
@_cli_errors
def health_events(
    hours: Annotated[int, typer.Option(help="Lookback hours")] = 24,
    severity: Annotated[str, typer.Option(help="Min severity: info/warning/error")] = "warning",
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Show recent events."""
    from vmware_monitor.ops.health import get_recent_events

    si, _, tgt = _get_connection(target, config)
    result = get_recent_events(si, hours=hours, severity=severity)
    events = result["items"]
    _audit.log_query(target=tgt, resource="events", query_type="get_recent_events")
    note = result.get("classification_note")
    if not events:
        # Green only when the quiet is established. The window may also be quiet
        # because nothing in it could be ranked, and painting that green is how
        # this line came to announce calm over five real warnings.
        if note:
            console.print(f"[yellow]{note}[/]")
        else:
            console.print(f"[green]No events above '{severity}' in the last {hours}h.[/]")
        return
    table = Table(title=f"Events (last {hours}h, >= {severity})")
    table.add_column("Severity")
    table.add_column("Time")
    table.add_column("Type", style="cyan")
    table.add_column("Message")
    for e in events:
        style = {"critical": "red", "warning": "yellow", "unknown": "yellow"}.get(
            e["severity"], "white"
        )
        table.add_row(
            f"[{style}]{e['severity']}[/]", e["time"], e["event_type"], e["message"][:120]
        )
    console.print(table)
    if note:
        console.print(f"[yellow]{note}[/]")


@health_app.command("sensors")
@_cli_errors
def health_sensors(target: TargetOption = None, config: ConfigOption = None) -> None:
    """Show hardware sensor status for all hosts."""
    from vmware_monitor.ops.health import get_host_hardware_status

    si, _, tgt = _get_connection(target, config)
    sensors = get_host_hardware_status(si)["items"]
    _audit.log_query(target=tgt, resource="hardware_sensors", query_type="get_host_hardware_status")
    if not sensors:
        console.print("[green]No hardware sensor data available.[/]")
        return
    table = Table(title="Hardware Sensors")
    table.add_column("Host", style="cyan")
    table.add_column("Sensor")
    table.add_column("Type")
    table.add_column("Reading", justify="right")
    table.add_column("Status")
    for s in sensors:
        status_style = {"green": "green", "yellow": "yellow", "red": "red"}.get(
            s["status"], "white"
        )
        table.add_row(
            s["host"],
            s["sensor_name"],
            s["type"],
            f"{s['reading']} {s['unit']}",
            f"[{status_style}]{s['status']}[/]",
        )
    console.print(table)


@health_app.command("services")
@_cli_errors
def health_services(
    host: Annotated[
        str | None,
        typer.Option("--host", help="Filter to a single host by exact name"),
    ] = None,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Show host service status (running/policy)."""
    from vmware_monitor.ops.health import get_host_services

    si, _, tgt = _get_connection(target, config)
    services = get_host_services(si, host_name=host)["items"]
    _audit.log_query(target=tgt, resource="host_services", query_type="get_host_services")
    if not services:
        console.print("[yellow]No host services found.[/]")
        return
    table = Table(title="Host Services")
    table.add_column("Host", style="cyan")
    table.add_column("Service")
    table.add_column("Label")
    table.add_column("Running")
    table.add_column("Policy")
    for s in services:
        run_style = "green" if s["running"] else "red"
        table.add_row(
            s["host"],
            s["service"],
            s["label"],
            f"[{run_style}]{'yes' if s['running'] else 'no'}[/]",
            s["policy"],
        )
    console.print(table)


# ─── VM (read-only: info and snapshot-list only) ─────────────────────────────


@vm_app.command("info")
@_cli_errors
def vm_info(
    name: str,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Show detailed info for a VM (read-only)."""
    from vmware_monitor.ops.vm_info import get_vm_info

    si, _, tgt = _get_connection(target, config)
    info = get_vm_info(si, name)
    _audit.log_query(target=tgt, resource=name, query_type="get_vm_info")
    for k, v in info.items():
        console.print(f"  [cyan]{k}:[/] {v}")


@vm_app.command("snapshot-list")
@_cli_errors
def vm_snapshot_list(
    vm_name: str,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """List VM snapshots (read-only)."""
    from vmware_monitor.ops.vm_info import list_snapshots

    si, _, tgt = _get_connection(target, config)
    snaps = list_snapshots(si, vm_name)["items"]
    _audit.log_query(target=tgt, resource=vm_name, query_type="list_snapshots")
    if not snaps:
        console.print("[yellow]No snapshots found.[/]")
        return
    for s in snaps:
        prefix = "  " * s["level"]
        console.print(f"{prefix}[cyan]{s['name']}[/] ({s['created']}) - {s['description']}")


# ─── Scan ─────────────────────────────────────────────────────────────────────


@scan_app.command("now")
@_cli_errors
def scan_now(target: TargetOption = None, config: ConfigOption = None) -> None:
    """Run a one-time scan of alarms and events."""
    from vmware_monitor.scanner.alarm_scanner import scan_alarms
    from vmware_monitor.scanner.log_scanner import scan_logs

    si, cfg, tgt = _get_connection(target, config)
    _audit.log_query(target=tgt, resource="scan", query_type="scan_now")
    console.print("[bold]Running scan...[/]")
    alarm_results = scan_alarms(si)
    log_results = scan_logs(si, cfg.scanner)
    total = len(alarm_results) + len(log_results)
    if total == 0:
        console.print("[green]All clear. No issues found.[/]")
    else:
        console.print(f"[yellow]Found {total} issue(s).[/]")
        for r in alarm_results + log_results:
            sev_style = {"critical": "red", "warning": "yellow"}.get(r["severity"], "white")
            console.print(f"  [{sev_style}][{r['severity'].upper()}][/] {r['message']}")


# ─── Daemon ───────────────────────────────────────────────────────────────────


@daemon_app.command("start")
@_cli_errors
def daemon_start(config: ConfigOption = None) -> None:
    """Start the scanner daemon."""
    from vmware_monitor.scanner.scheduler import start_scheduler

    console.print("[bold]Starting scanner daemon...[/]")
    start_scheduler(config)


@daemon_app.command("status")
def daemon_status() -> None:
    """Check scanner daemon status."""
    import os as _os

    pid_file = CONFIG_DIR / "daemon.pid"
    if not pid_file.exists():
        console.print("[yellow]Daemon not running.[/]")
        return
    raw = pid_file.read_text().strip()
    try:
        pid = int(raw)
    except ValueError:
        console.print(
            f"[yellow]Corrupt PID file ({raw!r}). Run 'vmware-monitor daemon stop' to clean up.[/]"
        )
        return
    try:
        _os.kill(pid, 0)  # Signal 0 = liveness probe only; sends nothing
        console.print(f"[green]Daemon running (PID: {pid})[/]")
    except ProcessLookupError:
        console.print(f"[yellow]Daemon not running (stale PID file, PID: {pid}).[/]")
    except PermissionError:
        # Process exists but is owned by another user
        console.print(f"[green]Daemon running (PID: {pid})[/]")


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the scanner daemon."""
    import os as _os

    pid_file = CONFIG_DIR / "daemon.pid"
    if not pid_file.exists():
        console.print("[yellow]Daemon not running.[/]")
        return

    raw = pid_file.read_text().strip()
    try:
        pid = int(raw)
    except ValueError:
        console.print(f"[yellow]Corrupt PID file ({raw!r}). Cleaning up.[/]")
        pid_file.unlink(missing_ok=True)
        return
    try:
        _os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Daemon (PID: {pid}) stopped.[/]")
    except ProcessLookupError:
        console.print(f"[yellow]Daemon process (PID: {pid}) not found. Cleaning up.[/]")
    except OSError as e:
        console.print(f"[red]Failed to stop daemon: {e}[/]")
        return
    pid_file.unlink(missing_ok=True)


@app.command("init")
def init_cmd(
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing config without asking")
    ] = False,
    skip_test: Annotated[
        bool, typer.Option("--skip-test", help="Don't run a connection test after writing config")
    ] = False,
) -> None:
    """Interactive first-run setup: write config.yaml + .env, then verify."""
    from vmware_monitor.init_wizard import run_init

    raise typer.Exit(run_init(force=force, skip_test=skip_test))


@app.command("doctor")
def doctor_cmd(
    skip_auth: Annotated[
        bool,
        typer.Option("--skip-auth", help="Skip vSphere authentication check (faster)"),
    ] = False,
) -> None:
    """Check environment, config, connectivity, and daemon status."""
    from vmware_monitor.doctor import run_doctor

    raise typer.Exit(run_doctor(skip_auth=skip_auth))


# ─── MCP Config Generator ────────────────────────────────────────────────────

mcp_config_app = typer.Typer(help="Generate MCP server config for local AI agents.")
app.add_typer(mcp_config_app, name="mcp-config")

_AGENT_TEMPLATES = {
    "goose": "goose.json",
    "cursor": "cursor.json",
    "claude-code": "claude-code.json",
    "continue": "continue.yaml",
    "vscode-copilot": "vscode-copilot.json",
    "localcowork": "localcowork.json",
    "mcp-agent": "mcp-agent.yaml",
}

_TEMPLATES_DIR = Path(__file__).parent.parent / "examples" / "mcp-configs"


@mcp_config_app.command("generate")
def mcp_config_generate(
    agent: Annotated[
        str,
        typer.Option(
            "--agent",
            "-a",
            help="Target agent: goose, cursor, claude-code, continue, vscode-copilot, localcowork, mcp-agent",
        ),
    ],
    install_path: Annotated[
        str | None,
        typer.Option("--path", help="Absolute path to VMware-Monitor install dir"),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write config to this file path"),
    ] = None,
) -> None:
    """Generate MCP server config for a local AI agent.

    Example:
        vmware-monitor mcp-config generate --agent goose
    """
    agent_lower = agent.lower()
    if agent_lower not in _AGENT_TEMPLATES:
        available = ", ".join(sorted(_AGENT_TEMPLATES.keys()))
        console.print(f"[red]Unknown agent '{agent}'. Available: {available}[/]")
        raise typer.Exit(1)

    template_file = _TEMPLATES_DIR / _AGENT_TEMPLATES[agent_lower]
    if not template_file.exists():
        console.print(f"[red]Template file not found: {template_file}[/]")
        raise typer.Exit(1)

    content = template_file.read_text()

    if install_path:
        content = content.replace("/path/to/VMware-Monitor", str(Path(install_path).resolve()))
    else:
        pkg_dir = Path(__file__).parent.parent.resolve()
        if (pkg_dir / "pyproject.toml").exists():
            content = content.replace("/path/to/VMware-Monitor", str(pkg_dir))

    if output:
        output.write_text(content)
        console.print(f"[green]Config written to: {output}[/]")
    else:
        console.print(content)


@mcp_config_app.command("list")
def mcp_config_list() -> None:
    """List all supported agents."""
    table = Table(title="Supported Agents")
    table.add_column("Agent", style="cyan")
    table.add_column("Template File")
    for agent_name, template in sorted(_AGENT_TEMPLATES.items()):
        table.add_row(agent_name, template)
    console.print(table)


@app.command("mcp")
def mcp_cmd() -> None:
    """Start the MCP server (stdio transport).

    Single-command entry point for MCP clients (Claude Desktop, Cursor, etc.):
        vmware-monitor mcp

    Equivalent to the legacy `vmware-monitor-mcp` console script.
    """
    import sys

    if sys.version_info < (3, 10):
        msg = (
            f"ERROR: vmware-monitor MCP server requires Python >= 3.10 "
            f"(got {sys.version_info.major}.{sys.version_info.minor}).\n"
            f"Interpreter: {sys.executable}\n"
            "Fix: uv python install 3.12 && "
            "uv tool install --python 3.12 --force vmware-monitor"
        )
        typer.echo(msg, err=True)
        raise typer.Exit(2)

    from vmware_monitor.mcp_server.server import main as _mcp_main

    _mcp_main()


if __name__ == "__main__":
    app()
