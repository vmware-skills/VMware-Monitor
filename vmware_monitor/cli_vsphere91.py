"""vSphere 9.1 read-only CLI commands: memory tiering + vLCM/deployment.

    memory tiering              per-host memory tiering + NVMe uplift (pyVmomi)
    patch compliance CLUSTER    vLCM cluster software compliance (REST)
    patch last-apply CLUSTER    last vLCM apply result (REST)
    deployment-size             vCenter appliance deployment size (REST, 9.1-new)

All read-only. The REST commands authenticate with the same per-target credentials
as the SOAP path and only ever GET.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from vmware_monitor.cli_base import (
    ConfigOption,
    TargetOption,
    audit,
    cli_errors,
    console,
    get_connection,
)

memory_app = typer.Typer(help="Memory tiering (vSphere 9.1, read-only).")
patch_app = typer.Typer(help="vLCM patch compliance / last-apply (read-only).")

LimitOption = Annotated[int | None, typer.Option("--limit", "-n", help="Max rows to show")]
ClusterArg = Annotated[str, typer.Argument(help="Cluster MoID, e.g. domain-c123")]


# ─── memory tiering ──────────────────────────────────────────────────────────


@memory_app.command("tiering")
@cli_errors
def memory_tiering(
    host: Annotated[str | None, typer.Option("--host", help="Single host by exact name")] = None,
    target: TargetOption = None,
    config: ConfigOption = None,
    limit: LimitOption = None,
) -> None:
    """Per-host memory tiering (DRAM/NVMe tiers) and NVMe uplift ratio."""
    from vmware_monitor.ops.memory_tiering import get_memory_tiering

    si, _, tgt = get_connection(target, config)
    rows = get_memory_tiering(si, host_name=host, limit=limit)["items"]
    audit.log_query(target=tgt, resource="memory_tiering", query_type="get_memory_tiering")
    if not rows:
        console.print("[yellow]No hosts found.[/]")
        return
    table = Table(title="Host Memory Tiering (vSphere 9.1)")
    table.add_column("Host", style="cyan")
    table.add_column("Mode")
    table.add_column("DRAM GB", justify="right")
    table.add_column("NVMe GB", justify="right")
    table.add_column("Total GB", justify="right")
    table.add_column("Uplift", justify="right")
    for r in rows:
        active = r["tiering_active"]
        mode_style = "green" if active else "dim"
        uplift = r["uplift_ratio"]
        table.add_row(
            r["host"],
            f"[{mode_style}]{r['tiering_type']}[/]",
            str(r["dram_gb"] if r["dram_gb"] is not None else "-"),
            str(r["nvme_gb"] if r["nvme_gb"] is not None else "-"),
            str(r["total_tiered_gb"] if r["total_tiered_gb"] is not None else "-"),
            f"{uplift}x" if uplift is not None else "-",
        )
    console.print(table)


# ─── vLCM patch (REST) ───────────────────────────────────────────────────────


def _target_config(target: str | None, config):
    """Resolve the TargetConfig the REST client needs WITHOUT a SOAP session.

    These REST reads deliberately tolerate a mid-patch vCenter answering 503 and
    degrade to ``{available: False}`` (踩坑 #37). Doing a pyVmomi SmartConnect here
    just to resolve config would make the SOAP login fail on that same 503 and kill
    the command before the degradation ever runs. So read config directly — the
    same thing the MCP path does (``server._get_target_config``) — and never open a
    ServiceInstance.
    """
    from vmware_monitor.config import load_config

    cfg = load_config(config)
    tcfg = cfg.get_target(target) if target else cfg.default_target
    return tcfg, tcfg.name


@patch_app.command("compliance")
@cli_errors
def patch_compliance(
    cluster: ClusterArg,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """vLCM software compliance for one cluster (needs the cluster MoID)."""
    from vmware_monitor.ops.patching import get_patch_compliance

    tcfg, tgt = _target_config(target, config)
    result = get_patch_compliance(tcfg, cluster)
    audit.log_query(target=tgt, resource=cluster, query_type="get_patch_compliance")
    if not result.get("available", False):
        console.print(f"[yellow]{result.get('reason', 'cluster patch state unavailable')}[/]")
        return
    console.print(
        f"[bold]Cluster {result['cluster']}[/] — status: {result['status']}"
        f"  ·  hosts: {result.get('hosts_total', '?')}"
        f"  ·  non-compliant: {result.get('non_compliant_hosts', '?')}"
    )
    console.print(f"[dim]{result['note']}[/]")


@patch_app.command("last-apply")
@cli_errors
def patch_last_apply(
    cluster: ClusterArg,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Result of the last vLCM remediation (apply) on one cluster."""
    from vmware_monitor.ops.patching import get_last_apply_result

    tcfg, tgt = _target_config(target, config)
    result = get_last_apply_result(tcfg, cluster)
    audit.log_query(target=tgt, resource=cluster, query_type="get_last_apply_result")
    if not result.get("available", False):
        console.print(f"[yellow]{result.get('reason', 'last-apply result unavailable')}[/]")
        return
    console.print(
        f"[bold]Cluster {result['cluster']}[/] — last apply: {result['status']}"
        f"  ·  ended: {result.get('end_time') or 'n/a'}"
    )
    console.print(f"[dim]{result['note']}[/]")


# ─── deployment size (REST, 9.1-new) ─────────────────────────────────────────


@cli_errors
def deployment_size_cmd(
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """vCenter appliance deployment size (NEW in vSphere 9.1)."""
    from vmware_monitor.ops.patching import get_deployment_size

    tcfg, tgt = _target_config(target, config)
    result = get_deployment_size(tcfg)
    audit.log_query(target=tgt, resource="deployment_size", query_type="get_deployment_size")
    if not result.get("available", False):
        console.print(f"[yellow]{result.get('reason', 'deployment size unavailable')}[/]")
        return
    fields = result.get("fields", {})
    if not fields:
        console.print("[yellow]vCenter returned no scalar deployment-size fields.[/]")
    else:
        table = Table(title="vCenter Deployment Size")
        table.add_column("Field", style="cyan")
        table.add_column("Value")
        for key, value in fields.items():
            table.add_row(key, str(value))
        console.print(table)
    console.print(f"[dim]{result['note']}[/]")


def register(app: typer.Typer) -> None:
    """Attach the vSphere 9.1 read command groups to the root CLI."""
    app.add_typer(memory_app, name="memory")
    app.add_typer(patch_app, name="patch")
    app.command("deployment-size")(deployment_size_cmd)
