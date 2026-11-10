"""Console Reports callbacks using the existing facade runtime."""

from __future__ import annotations


from pathlib import Path


def register_callbacks(runtime):
    def _slug(text: str) -> str:
        """Filesystem-safe slug from a target name (for the snapshot filename)."""
        return runtime.re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "vcenter"

    def write_html_snapshot(data: dict, vcenter: str, explicit_path: Path | None) -> None:
        """Render the summary to a self-contained HTML file and report the path.

        Default location is ~/vmware-health/cluster-health-<vc>-<YYYYMMDD-HHMMSS>.html
        so a folder of snapshots becomes a browsable point-in-time history. The file
        is fully offline (no external CSS/JS/fonts) — nothing leaves the machine.
        """
        from vmware_monitor.ops.health_html import render_cluster_health_html

        now = runtime.datetime.now().astimezone()
        if explicit_path is not None:
            path = explicit_path.expanduser()
        else:
            ts = now.strftime("%Y%m%d-%H%M%S")
            path = (
                runtime.Path.home()
                / "vmware-health"
                / f"cluster-health-{runtime._slug(vcenter)}-{ts}.html"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        document = render_cluster_health_html(data, vcenter, now, filename=path.name)
        path.write_text(document, encoding="utf-8")
        runtime.console.print(f"[green]Wrote cluster-health snapshot →[/] {path}")
        runtime.console.print(f"[dim]Open it: open '{path}'  (offline file, nothing uploaded)[/]")

    def _render_top_issues(data: dict, top: int) -> None:
        """Print the ranked top-N anomaly focus list (the large-fleet headline)."""
        if top <= 0:
            return
        issues = data.get("top_issues", [])
        total = data.get("issues_total", 0)
        if not issues:
            runtime.console.print("[green]No issues detected — every cluster is OK.[/]\n")
            return
        from rich.markup import escape

        from vmware_monitor.ops.cluster_summary import cli_drilldown

        shown = len(issues)
        title = f"Top {shown} issues" + (f" (of {total})" if total > shown else "")
        tbl = runtime.Table(title=title)
        tbl.add_column("#", justify="right", style="dim")
        tbl.add_column("Severity")
        # Four columns, not six: at an 80-column terminal six squeezed Problem and
        # Next step until words were cut ("thin-provi…"). The cluster rides under
        # the object and the next step under the problem, so the text that matters
        # gets the width. The next step names CLI commands — ``drilldown`` in the
        # data names MCP tools, which a terminal user cannot run.
        tbl.add_column("Object", max_width=28)
        tbl.add_column("Problem · next step")
        for n, i in enumerate(issues, 1):
            sev = i["severity"]
            hint = cli_drilldown(i)
            # The same datastore reached through another target: its own figure can
            # differ legitimately (see `capacity datastores`), so it is shown, not dropped.
            also = "".join(
                f"\n[dim]also via {escape(str(a['vcenter']))}: "
                f"{escape(str(a['detail']).split(' — ')[0])}[/]"
                for a in i.get("also_seen_via", [])
            )
            tbl.add_row(
                str(n),
                f"[{runtime._SEV_STYLE.get(sev, 'white')}]{sev.upper()}[/]",
                f"[cyan]{escape(str(i['object']))}[/]\n[dim]{escape(str(i.get('cluster') or '—'))}[/]",
                escape(str(i["detail"])) + also + (f"\n[dim]→ {escape(hint)}[/]" if hint else ""),
            )
        runtime.console.print(tbl)

    def _setting_cell(value: bool | None, off: str) -> str:
        """ON / the given off cell / n/a when the row has no cluster (None)."""
        if value is None:
            return "[dim]n/a[/]"
        return "[green]ON[/]" if value else off

    def render_summary_console(data: dict, top: int) -> None:
        """Render a cluster-health summary dict to the terminal (header + top-N + table).

        Public so a companion skill (e.g. vmware-aiops, which delegates to this same
        aggregation via the vmware-monitor library) can present an identical view
        without duplicating the rendering.
        """
        t = data["totals"]
        worst = t["worst_status"]
        runtime.console.print(
            f"[bold]Cluster health[/] — {t['clusters']} clusters, "
            f"{t['hosts_connected']}/{t['hosts_total']} hosts connected"
            + (f" ({t['hosts_unreadable']} unreadable)" if t.get("hosts_unreadable") else "")
            + (f", {t.get('vms_on', 0)}/{t.get('vms_total', 0)} VMs on" if "vms_total" in t else "")
            + f"  ·  overall: [{runtime._STATUS_STYLE[worst]}]{worst.upper()}[/]"
        )

        endpoint = data.get("endpoint", {})
        if endpoint.get("unavailable"):
            runtime.console.print(endpoint["unavailable"], markup=False)
        elif any(endpoint.values()):
            identity = " · ".join(
                str(endpoint.get(k) or "unknown") for k in ("api_type", "version", "build")
            )
            runtime.console.print(f"Endpoint (API / version / build): {identity}", markup=False)

        runtime._render_top_issues(data, top)

        table = runtime.Table(title="Cluster Health Summary")
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
            style = runtime._STATUS_STYLE.get(c["status"], "white")
            row = [
                f"[{style}]{c['status'].upper()}[/]",
                c["name"],
                f"{c['hosts_connected']}/{c['hosts_total']}"
                + (f" ({c['hosts_unreadable']} unreadable)" if c.get("hosts_unreadable") else ""),
            ]
            if "vms_total" in t:
                row.append(f"{c.get('vms_on', 0)}/{c.get('vms_total', 0)}")
            row += [
                f"[{runtime._pct_style(c['cpu_used_pct'])}]{c['cpu_used_pct']}[/]",
                f"[{runtime._pct_style(c['mem_used_pct'])}]{c['mem_used_pct']}[/]",
                runtime._setting_cell(c["ha_enabled"], "[red]OFF[/]"),
                runtime._setting_cell(c["drs_enabled"], "[dim]off[/]"),
                f"{c['alarms']['critical']}/{c['alarms']['warning']}",
                "; ".join(c["attention"]) or "[dim]—[/]",
            ]
            table.add_row(*row)
        runtime.console.print(table)
        runtime.console.print(f"[dim]{data['customization_hint']}[/]")

    def write_bundle_html_snapshot(
        bundle: dict, kind: str, vcenter: str, explicit_path: Path | None
    ) -> None:
        """Render an investigation bundle to a self-contained HTML file, report the path.

        Default location mirrors the cluster-health snapshot:
        ``~/vmware-health/investigate-<kind>-<object>-<YYYYMMDD-HHMMSS>.html`` — fully
        offline (no external CSS/JS/fonts), nothing leaves the machine.
        """
        from vmware_monitor.ops.investigate_html import render_bundle_html

        now = runtime.datetime.now().astimezone()
        obj_name = runtime._slug(str(bundle.get("object", {}).get("name", kind)))
        if explicit_path is not None:
            path = explicit_path.expanduser()
        else:
            ts = now.strftime("%Y%m%d-%H%M%S")
            path = (
                runtime.Path.home() / "vmware-health" / f"investigate-{kind}-{obj_name}-{ts}.html"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        document = render_bundle_html(bundle, kind, vcenter, now, filename=path.name)
        path.write_text(document, encoding="utf-8")
        runtime.console.print(f"[green]Wrote {kind} investigation snapshot →[/] {path}")
        runtime.console.print(f"[dim]Open it: open '{path}'  (offline file, nothing uploaded)[/]")

    def render_bundle_console(bundle: dict, kind: str) -> None:
        """Print an investigation bundle to the terminal (context + timeline + detail).

        Public so vmware-aiops can present the identical view via the delegated library
        without duplicating rendering.
        """
        obj = bundle.get("object", {})
        runtime.console.print(
            f"[bold]{kind.upper()} investigation[/] — {obj.get('name', '')}"
            f"  ·  status: {obj.get('status', 'gray')}"
        )
        host = bundle.get("host")
        if host:
            runtime.console.print(
                f"  host: [cyan]{host['name']}[/] ({host['connection']}, "
                f"CPU {host['cpu_pct']}% / Mem {host['mem_pct']}%)"
            )
        cluster = bundle.get("cluster")
        if cluster:
            runtime.console.print(
                f"  cluster: [cyan]{cluster['name']}[/] "
                f"(HA {'on' if cluster['ha_enabled'] else 'off'}, "
                f"DRS {'on' if cluster['drs_enabled'] else 'off'})"
            )
        for h in bundle.get("hosts", []):
            runtime.console.print(
                f"  host: [cyan]{h['name']}[/] ({h['connection']}, "
                f"CPU {h['cpu_pct']}% / Mem {h['mem_pct']}%)"
            )
        for ds in bundle.get("datastores", []):
            runtime.console.print(f"  datastore: [cyan]{ds['name']}[/] ({ds['free_pct']}% free)")
        vms = bundle.get("vms")
        if vms is not None:
            sample = ", ".join(vms.get("sample", []))
            extra = f" — {sample}" if sample else ""
            runtime.console.print(f"  VMs: {vms['powered_on']}/{vms['total']} powered on{extra}")

        alarms = bundle.get("alarms", [])
        if alarms:
            atbl = runtime.Table(title=f"Alarms ({len(alarms)})")
            atbl.add_column("Severity")
            atbl.add_column("Alarm", style="cyan")
            atbl.add_column("On")
            for a in alarms:
                atbl.add_row(
                    f"[{runtime._SEV_STYLE.get(a['severity'], 'white')}]{a['severity'].upper()}[/]",
                    a["name"],
                    f"{a['scope']} {a['object']}",
                )
            runtime.console.print(atbl)

        timeline = bundle.get("timeline", [])
        ttl = runtime.Table(
            title=f"Event timeline · last {bundle.get('hours', 24)}h ({len(timeline)})"
        )
        ttl.add_column("Time", style="dim")
        ttl.add_column("Sev")
        ttl.add_column("Source")
        ttl.add_column("Message")
        for e in timeline:
            ttl.add_row(
                e["time"],
                f"[{runtime._SEV_STYLE.get(e['severity'], 'white')}]{e['severity'][:4].upper()}[/]",
                f"{e['scope']} {e['entity']}",
                e["message"],
            )
        runtime.console.print(ttl)
        for key in ("timeline_unavailable", "timeline_note"):
            if bundle.get(key):
                runtime.console.print(f"[yellow]{bundle[key]}[/]")
        runtime.console.print(f"[dim]{bundle.get('customization_hint', '')}[/]")

    def render_attention_console(data: dict) -> None:
        """Print the cross-vCenter attention view to the terminal.

        Public so vmware-aiops can present the identical view via the delegated library.
        """
        from rich.markup import escape

        t = data["totals"]
        runtime.console.print(
            f"[bold]What needs attention[/] — {runtime._target_counts(t)}, "
            f"{t['clusters']} clusters, {t['hosts_connected']}/{t['hosts_total']} hosts connected"
            + (f" ({t['hosts_unreadable']} unreadable)" if t.get("hosts_unreadable") else "")
            + f"  ·  overall: [{runtime._STATUS_STYLE[t['worst_status']]}]{t['worst_status'].upper()}[/]"
            + (f" ({escape(t['status_reason'])})" if t.get("status_reason") else "")
        )
        for u in data.get("unreachable", []):
            runtime.console.print(f"[yellow]unreachable:[/] {u['vcenter']} ({u['reason']})")

        runtime._render_top_issues(runtime._with_vcenter_cluster(data), data.get("issues_total", 0))

        table = runtime.Table(title="Targets (worst status first)")
        table.add_column("Status")
        table.add_column("Target", style="cyan")
        table.add_column("Type")
        table.add_column("Clusters", justify="right")
        table.add_column("Hosts", justify="right")
        table.add_column("Alarms C/W", justify="right")
        for tg in data["targets"]:
            style = runtime._STATUS_STYLE.get(tg["worst_status"], "white")
            shared = tg.get("shared_hosts", 0)
            table.add_row(
                f"[{style}]{tg['worst_status'].upper()}[/]",
                tg["vcenter"],
                tg.get("endpoint", "unknown"),
                str(tg["clusters"]),
                f"{tg['hosts_connected']}/{tg['hosts_total']}"
                + (f" ({tg['hosts_unreadable']} unreadable)" if tg.get("hosts_unreadable") else "")
                + (f" [dim]({shared} counted via another target)[/]" if shared else ""),
                f"{tg['alarms']['critical']}/{tg['alarms']['warning']}",
            )
        runtime.console.print(table)
        runtime.console.print(f"[dim]{data.get('customization_hint', '')}[/]")

    def _plural(n: int, word: str) -> str:
        return f"{n} {word}" + ("" if n == 1 else "s")

    def _target_counts(totals: dict) -> str:
        """ "1 vCenter, 1 ESXi target" — an ESXi host reached directly is not a vCenter."""
        parts = [
            runtime._plural(totals.get("vcenters", 0), "vCenter"),
            runtime._plural(totals.get("esxi_targets", 0), "ESXi target")
            if totals.get("esxi_targets")
            else "",
            runtime._plural(totals.get("unidentified_targets", 0), "unidentified target")
            if totals.get("unidentified_targets")
            else "",
        ]
        return ", ".join(p for p in parts if p)

    def _with_vcenter_cluster(data: dict) -> dict:
        """Fold each issue's vCenter into its cluster label so the shared top-issues
        table (which prints a 'Cluster' column) shows which estate it belongs to."""
        issues = []
        for i in data.get("top_issues", []):
            j = dict(i)
            j["cluster"] = f"{i.get('vcenter', '')} · {i.get('cluster') or '—'}"
            issues.append(j)
        return {"top_issues": issues, "issues_total": data.get("issues_total", 0)}

    def write_attention_html_snapshot(data: dict, explicit_path: Path | None) -> None:
        """Render the attention view to a self-contained HTML file and report the path."""
        from vmware_monitor.ops.attention_html import render_attention_html

        now = runtime.datetime.now().astimezone()
        if explicit_path is not None:
            path = explicit_path.expanduser()
        else:
            ts = now.strftime("%Y%m%d-%H%M%S")
            path = runtime.Path.home() / "vmware-health" / f"attention-{ts}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_attention_html(data, now, filename=path.name), encoding="utf-8")
        runtime.console.print(f"[green]Wrote cross-vCenter attention snapshot →[/] {path}")
        runtime.console.print(f"[dim]Open it: open '{path}'  (offline file, nothing uploaded)[/]")

    return (
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
    )
