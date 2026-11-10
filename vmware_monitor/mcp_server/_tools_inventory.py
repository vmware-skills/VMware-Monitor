"""Inventory MCP registrations sharing the server runtime.

The facade owns connection, error and Policy state; this module creates no second
server or connection manager. Returned aliases preserve the original Python API.
"""

from typing import Optional


def register_tools(mcp, runtime):
    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def list_virtual_machines(
        target: Optional[str] = None,
        limit: Optional[int] = None,
        sort_by: str = "name",
        power_state: Optional[str] = None,
        fields: Optional[list[str]] = None,
        folder_filter: Optional[str] = None,
        name_filter: Optional[str] = None,
    ) -> dict:
        """[READ] List virtual machines, with filtering, sorting, and field selection.

        Returns the family list envelope {items, returned, limit, total, truncated,
        hint}; ``total`` is the real post-filter count, so read ``truncated`` before
        summarising. Over 50 VMs with no limit/fields, only the first five fields
        below come back (``mode`` says which). ``vms`` is a deprecated pre-1.8.0
        alias of ``items``, removed in 2.0 — read ``items``.

        Use this to resolve an exact VM name, then get_vm_info for detail or
        investigate_vm to drill into one; for a fleet-wide view start at
        get_cluster_health_summary instead.

        Args:
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max VMs (None = all).
            sort_by: name | cpu | memory_mb | power_state | folder_path.
            power_state: poweredOn | poweredOff | suspended.
            fields: Any of name, power_state, cpu, memory_mb, folder_path, guest_os,
                ip_address, host, uuid, tools_status (None = auto).
            name_filter: Case-insensitive substring of the VM name. Use this to find
                one VM by name on a large estate — it is what the "VM not found"
                errors elsewhere tell you to come here for.
            folder_filter: Case-insensitive folder_path substring; nested folders
                match.
        """
        si = runtime._get_connection(target)
        return runtime.list_vms(
            si,
            limit=limit,
            sort_by=sort_by,
            power_state=power_state,
            fields=fields,
            folder_filter=folder_filter,
            name_filter=name_filter,
        )

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def list_esxi_hosts(
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] List ESXi hosts with CPU cores, memory, version, VM count, and uptime.

        Returns the list envelope; ``total`` is the real host count. Static config only
        (cores, total GB) — use this to resolve a host name, then get_host_performance for
        live load or investigate_host to drill into one host.

        Args:
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max hosts to return (None = all).
        """
        si = runtime._get_connection(target)
        return runtime.list_hosts(si, limit=limit)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def list_all_datastores(
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] List datastores with capacity, free space, type, and VM count.

        Returns the list envelope with a real ``total``. Raw free/used only — use this
        to resolve a datastore name, then get_datastore_capacity for thin-provisioning
        over-commit risk or investigate_datastore to drill into one datastore.

        Args:
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max datastores to return (None = all).
        """
        si = runtime._get_connection(target)
        return runtime.list_datastores(si, limit=limit)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def list_all_clusters(
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] List clusters with host count, DRS/HA status, and resource totals.

        Returns the list envelope with a real ``total``. Each row's ``moid`` (e.g.
        domain-c123) is the cluster id the vLCM patch tools take. Static topology only
        — use this to resolve a cluster name, then get_cluster_health_summary, which supersedes
        stitching this with list_esxi_hosts and get_alarms yourself.

        Args:
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max clusters to return (None = all).
        """
        si = runtime._get_connection(target)
        return runtime.list_clusters(si, limit=limit)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        meta=runtime.apps.ui_meta(runtime.apps.CLUSTER_HEALTH_URI),
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def get_cluster_health_summary(
        target: Optional[str] = None,
        cluster_filter: Optional[str] = None,
        include_vms: bool = True,
        top_n: int = 10,
    ) -> dict:
        """[READ] One-glance health rollup for every cluster — "is anything on fire?".

        Start here for single-vCenter triage. Batches hosts, VM power state, live
        CPU/memory pressure and alarms per cluster, scores each "ok"/"warn"/
        "critical", and ranks the anomalies into ``top_issues``. Use this instead of
        stitching list_all_clusters + list_esxi_hosts + get_alarms yourself.

        Returns {totals, top_issues, issues_total, clusters, snapshot,
        customization_hint, endpoint} — not the list envelope. ``endpoint`` gives
        api_type/version/build of this connection, distinct from inventory hosts.
        Lead with ``top_issues`` (worst first), show ``clusters`` as context,
        always echo ``customization_hint`` last.
        Point-in-time — no trending. ``top_issues`` includes datastores thin-provisioned
        past 100% of capacity (kind capacity, scope datastore), attributed to the
        cluster of a host that mounts them; get_datastore_capacity has the full table.
        Alarm issues carry ``condition_now`` and ``acknowledged_days``. Only a
        ``cleared`` alarm (its condition is read to be false now) ranks after live
        issues; an ``unknown`` one is not re-checked and keeps its severity rank,
        however long ago it was acknowledged — report it as possibly still live.

        Then drill into what ``top_issues`` names with investigate_vm,
        investigate_host or investigate_datastore; use
        get_cross_vcenter_attention to cover every target at once. Acting on a finding
        belongs to vmware-aiops.

        Args:
            target: vCenter/ESXi target from config (default if omitted).
            cluster_filter: Case-insensitive substring; only matching clusters show
                (None = all, plus a standalone-hosts row).
            include_vms: Roll up VM counts (default True); False skips that pass.
            top_n: Cap ``top_issues`` (default 10; 0 omits it). ``issues_total`` is
                the pre-cap count.
        """
        si = runtime._get_connection(target)
        return runtime._ops_get_cluster_health_summary(
            si, cluster_filter=cluster_filter, include_vms=include_vms, top_n=top_n
        )

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def investigate_vm(
        vm_name: str,
        target: Optional[str] = None,
        hours: int = 24,
    ) -> dict:
        """[READ] "What is happening around this VM?" — one correlated drill-down.

        Use this instead of stitching get_vm_info + list_vm_snapshots + get_alarms +
        get_vm_performance + get_events yourself. Returns one correlated bundle (not the
        list envelope): the VM's state, its host, cluster and backing datastores, its
        snapshots and triggered alarms, live performance, and a merged event timeline
        across VM/host/cluster/datastore (newest first). All reads are batched.
        Explain it in operational language; do not dump it raw.

        Reach for it after get_cluster_health_summary names a problem VM, or when asked
        "what's going on with <vm>?". Point-in-time snapshot — no trending. Acting on
        what you find (power, migrate, delete snapshot) belongs to vmware-aiops.

        Args:
            vm_name: Exact VM name; unknown names return a teaching error. Get it from
                list_virtual_machines or get_cluster_health_summary first.
            target: vCenter/ESXi target from config (default if omitted).
            hours: Event-timeline look-back window in hours (default 24).
        """
        si = runtime._get_connection(target)
        return runtime.get_vm_investigation_bundle(si, vm_name, hours=hours)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def investigate_host(
        host_name: str,
        target: Optional[str] = None,
        hours: int = 24,
    ) -> dict:
        """[READ] "What is happening around this ESXi host?" — one correlated drill-down.

        Use this instead of stitching list_esxi_hosts + get_host_performance + get_alarms
        + get_events yourself. Returns one correlated bundle (not the list envelope):
        the host's state (connection, CPU/memory pressure, version, uptime), its
        cluster, a rollup of the VMs it runs, the datastores it mounts, alarms across
        host/cluster/datastore, live performance, and a merged event timeline. All
        reads are batched. Explain it in operational language; do not dump it raw.

        Reach for it after get_cluster_health_summary flags a host. Point-in-time
        snapshot — no trending; for syslog lines use scan_host_logs. Acting on a
        finding (maintenance mode, evacuate) belongs to vmware-aiops.

        Args:
            host_name: Exact ESXi host name; unknown names return a teaching error.
                Get it from list_esxi_hosts or get_cluster_health_summary first.
            target: vCenter/ESXi target from config (default if omitted).
            hours: Event-timeline look-back window in hours (default 24).
        """
        si = runtime._get_connection(target)
        return runtime.get_host_investigation_bundle(si, host_name, hours=hours)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def investigate_datastore(
        datastore_name: str,
        target: Optional[str] = None,
        hours: int = 24,
    ) -> dict:
        """[READ] "What is happening around this datastore?" — one correlated drill-down.

        Use this instead of stitching list_all_datastores + get_datastore_capacity +
        get_alarms + get_events yourself. Returns one correlated bundle (not the list
        envelope): capacity / free space / accessibility, the hosts that mount it, a
        rollup of the VMs it backs, alarms across datastore/host, and a merged event
        timeline. All reads are batched. Explain it in operational language; do not
        dump it raw. Per-datastore latency is not included.

        Reach for it after get_cluster_health_summary flags storage pressure.
        Point-in-time snapshot — no trending. Freeing space (deleting snapshots,
        storage vMotion) belongs to vmware-aiops.

        Args:
            datastore_name: Exact datastore name; unknown names return a teaching
                error. Get it from list_all_datastores or get_datastore_capacity first.
            target: vCenter/ESXi target from config (default if omitted).
            hours: Event-timeline look-back window in hours (default 24).
        """
        si = runtime._get_connection(target)
        return runtime.get_datastore_investigation_bundle(si, datastore_name, hours=hours)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def get_cross_vcenter_attention(
        cluster_filter: Optional[str] = None,
        top_n: int = 10,
    ) -> dict:
        """[READ] "What needs attention now?" across EVERY configured vCenter — one list.

        Start here when the estate has more than one vCenter. Rolls every configured
        target's cluster-health summary into one globally ranked ``top_issues`` list
        (worst first, each tagged with its ``vcenter``) plus a per-target rollup. Use
        this instead of calling get_cluster_health_summary once per target and merging
        yourself. Returns a rollup, not the list envelope; lead with ``top_issues``.

        Degrades gracefully: an unreachable target is listed under ``unreachable``
        with a reason and the rest still aggregate, but it is never counted as
        healthy: ``totals.worst_status`` is at least ``warn`` (``critical`` when no
        target answered) and ``totals.status_reason`` says how many were not read.
        Point-in-time — no trending.
        Overlap is removed by identity: a host reached through a vCenter and as its
        own ESXi target counts once (hardware UUID), a datastore seen through both is
        one issue with the other view's figure under ``also_seen_via`` (datastore
        URL), and ``totals`` separates ``vcenters``, ``esxi_targets`` and
        ``unidentified_targets``.
        Then drill in with investigate_vm, investigate_host or
        investigate_datastore against the ``vcenter`` the issue names.

        Args:
            cluster_filter: Case-insensitive cluster substring applied to every target
                (None = all clusters).
            top_n: Cap the merged top_issues list (default 10); ``issues_total`` is
                the pre-cap count.
        """
        sessions, unreachable = runtime._ensure_conn_mgr().connect_all()
        return runtime._ops_get_cross_vcenter_attention(
            sessions, unreachable=unreachable, cluster_filter=cluster_filter, top_n=top_n
        )

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    @runtime.vmware_tool(risk_level="low")
    @runtime._catch_tool_errors
    def list_all_networks(
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] List networks with name, attached VM count, and accessibility.

        Returns the list envelope with a real ``total``. Name, vm_count and accessible
        only — no VLAN, uplink or NSX overlay detail. Use this to resolve a port-group
        name, then get_vm_info for the NICs of one VM. NSX segments live in vmware-nsx.

        Args:
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max networks to return (None = all).
        """
        si = runtime._get_connection(target)
        return runtime.list_networks(si, limit=limit)

    return (
        list_virtual_machines,
        list_esxi_hosts,
        list_all_datastores,
        list_all_clusters,
        get_cluster_health_summary,
        investigate_vm,
        investigate_host,
        investigate_datastore,
        get_cross_vcenter_attention,
        list_all_networks,
    )
