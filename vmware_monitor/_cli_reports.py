"""Cli Reports callbacks using the existing facade runtime."""

from __future__ import annotations


from pathlib import Path

from typing import Annotated

import typer


from vmware_monitor.cli_base import (
    ConfigOption,
    TargetOption,
)


from vmware_monitor.cli_report_options import (
    HoursOption as HoursOption,
    HtmlFlag as HtmlFlag,
    HtmlPathOption as HtmlPathOption,
)


def register_callbacks(runtime):
    @runtime.cli_errors
    @runtime.audited("get_cluster_health_summary")
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

        si, _, tgt = runtime.get_connection(target, config)
        data = get_cluster_health_summary(
            si, cluster_filter=cluster, include_vms=not no_vms, top_n=top
        )
        runtime.audit.log_query(
            target=tgt, resource="clusters", query_type="get_cluster_health_summary"
        )

        if html or html_path is not None:
            runtime.write_html_snapshot(data, tgt, html_path)
            return

        runtime.render_summary_console(data, top)

    @runtime.cli_errors
    @runtime.audited("get_cross_vcenter_attention")
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

        sessions, unreachable = runtime.get_all_connections(config)
        data = get_cross_vcenter_attention(
            sessions, unreachable=unreachable, cluster_filter=cluster, top_n=top
        )
        runtime.audit.log_query(
            target="*", resource="all-vcenters", query_type="get_cross_vcenter_attention"
        )

        if html or html_path is not None:
            runtime.write_attention_html_snapshot(data, html_path)
            return
        runtime.render_attention_console(data)

    return (
        cluster_summary_cmd,
        attention_cmd,
    )
