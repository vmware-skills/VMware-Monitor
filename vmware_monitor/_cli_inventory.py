"""Cli Inventory callbacks using the existing facade runtime."""

from __future__ import annotations


from typing import Annotated

import typer


from vmware_monitor.cli_base import (
    ConfigOption,
    TargetOption,
)


def register_callbacks(runtime):
    @runtime.inventory_app.command("vms")
    @runtime._cli_errors
    @runtime.audited("list_virtual_machines")
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

        si, _, tgt = runtime._get_connection(target, config)
        result = list_vms(
            si,
            limit=limit,
            sort_by=sort_by,
            power_state=power_state,
            folder_filter=folder_filter,
        )
        runtime._audit.log_query(target=tgt, resource="virtual_machines", query_type="list_vms")
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
        table = runtime.Table(title=title)
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
        runtime.console.print(table)
        if hint:
            runtime.console.print(f"[yellow]ℹ {hint}[/yellow]")

    @runtime.inventory_app.command("hosts")
    @runtime._cli_errors
    @runtime.audited("list_esxi_hosts")
    def inventory_hosts(target: TargetOption = None, config: ConfigOption = None) -> None:
        """List all ESXi hosts."""
        from vmware_monitor.ops.inventory import list_hosts

        si, _, tgt = runtime._get_connection(target, config)
        hosts = list_hosts(si)["items"]
        runtime._audit.log_query(target=tgt, resource="esxi_hosts", query_type="list_hosts")
        table = runtime.Table(title="ESXi Hosts")
        table.add_column("Name", style="cyan")
        table.add_column("State")
        table.add_column("CPU Cores", justify="right")
        table.add_column("Memory (GB)", justify="right")
        table.add_column("VMs", justify="right")
        for h in hosts:
            # Red only for a host vCenter reports as down; a state this account
            # cannot read ("N/A") is unknown, not an outage.
            state_style = {"connected": "green", "disconnected": "red", "notResponding": "red"}.get(
                h["connection_state"], "yellow"
            )
            table.add_row(
                h["name"],
                f"[{state_style}]{h['connection_state']}[/]",
                str(h["cpu_cores"]),
                str(h["memory_gb"]),
                str(h["vm_count"]),
            )
        runtime.console.print(table)

    @runtime.inventory_app.command("datastores")
    @runtime._cli_errors
    @runtime.audited("list_all_datastores")
    def inventory_datastores(target: TargetOption = None, config: ConfigOption = None) -> None:
        """List all datastores."""
        from vmware_monitor.ops.inventory import list_datastores

        si, _, tgt = runtime._get_connection(target, config)
        stores = list_datastores(si)["items"]
        runtime._audit.log_query(target=tgt, resource="datastores", query_type="list_datastores")
        table = runtime.Table(title="Datastores")
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
        runtime.console.print(table)

    @runtime.inventory_app.command("clusters")
    @runtime._cli_errors
    @runtime.audited("list_all_clusters")
    def inventory_clusters(target: TargetOption = None, config: ConfigOption = None) -> None:
        """List all clusters."""
        from vmware_monitor.ops.inventory import list_clusters

        si, _, tgt = runtime._get_connection(target, config)
        clusters = list_clusters(si)["items"]
        runtime._audit.log_query(target=tgt, resource="clusters", query_type="list_clusters")
        table = runtime.Table(title="Clusters")
        table.add_column("Name", style="cyan")
        table.add_column("MoID")
        table.add_column("Hosts", justify="right")
        table.add_column("DRS")
        table.add_column("HA")
        for c in clusters:
            table.add_row(
                c["name"],
                c.get("moid") or "",
                str(c["host_count"]),
                "[green]ON[/]" if c["drs_enabled"] else "[red]OFF[/]",
                "[green]ON[/]" if c["ha_enabled"] else "[red]OFF[/]",
            )
        runtime.console.print(table)

    @runtime.inventory_app.command("networks")
    @runtime._cli_errors
    @runtime.audited("list_all_networks")
    def inventory_networks(target: TargetOption = None, config: ConfigOption = None) -> None:
        """List all networks."""
        from vmware_monitor.ops.inventory import list_networks

        si, _, tgt = runtime._get_connection(target, config)
        networks = list_networks(si)["items"]
        runtime._audit.log_query(target=tgt, resource="networks", query_type="list_networks")
        table = runtime.Table(title="Networks")
        table.add_column("Name", style="cyan")
        table.add_column("VMs", justify="right")
        table.add_column("Accessible")
        for n in networks:
            table.add_row(
                n["name"],
                str(n["vm_count"]),
                "[green]yes[/]" if n["accessible"] else "[red]no[/]",
            )
        runtime.console.print(table)

    return (
        inventory_vms,
        inventory_hosts,
        inventory_datastores,
        inventory_clusters,
        inventory_networks,
    )
