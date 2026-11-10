"""Runtime MCP registrations sharing the server runtime.

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
    def get_vm_info(vm_name: str, target: Optional[str] = None) -> dict:
        """[READ] Get detailed information about one VM (CPU, memory, disks, NICs, snapshots).

        Returns a single detail dict, not the list envelope. Static configuration
        only — for live CPU/memory use get_vm_performance, and for the same VM correlated
        with its host, alarms and events use investigate_vm.

        Use this when you already know the exact name; get it from
        list_virtual_machines first, since an unknown name returns a teaching error
        rather than a match. Reconfiguring the VM belongs to vmware-aiops.

        Args:
            vm_name: Exact name of the virtual machine.
            target: vCenter/ESXi target from config (default if omitted).
        """
        si = runtime._get_connection(target)
        return runtime._ops_get_vm_info(si, vm_name)

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
    def list_vm_snapshots(vm_name: str, target: Optional[str] = None) -> dict:
        """[READ] List all snapshots of one VM, including the nesting hierarchy.

        Returns the list envelope, one row per snapshot with name, description, created
        timestamp, state and level (0 = root; children are level+1). Empty ``items``
        with ``truncated`` False means the VM genuinely has no snapshots. Read-only —
        creating, reverting and deleting snapshots live in vmware-aiops.

        Use this for one known VM; prefer find_aging_snapshots to sweep the whole inventory
        for old or sprawling snapshots. Get the name from list_virtual_machines
        first — an unknown name returns a teaching error.

        Args:
            vm_name: Exact name of the virtual machine.
            target: vCenter/ESXi target from config (default if omitted).
        """
        si = runtime._get_connection(target)
        return runtime.list_snapshots(si, vm_name)

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
    def get_host_performance(
        host_name: Optional[str] = None,
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] Real-time CPU/memory/disk/network utilisation per ESXi host.

        Returns the list envelope with a real ``total`` (hosts that reported metrics).
        Unlike list_esxi_hosts (static config: cores, total GB) this is LIVE 20-second
        PerfManager data: cpu_usage_pct, mem_usage_pct, mem_consumed_mb, disk_kbps,
        net_kbps, busiest first. Disconnected hosts and hosts without a real-time
        provider are skipped, not reported as zero. Point-in-time only — no historical
        trend.

        Use this to find which host is hot, then investigate_host to drill
        into it, or get_vm_performance to see which VMs on it are driving the load.

        Args:
            host_name: Filter to a single host by exact name (None = all hosts).
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max host rows to return (None = all).
        """
        si = runtime._get_connection(target)
        return runtime._ops_get_host_performance(si, host_name=host_name, limit=limit)

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
    def get_vm_performance(
        vm_name: Optional[str] = None,
        target: Optional[str] = None,
        limit: Optional[int] = 25,
    ) -> dict:
        """[READ] Real-time CPU/memory/disk/network utilisation per virtual machine.

        Returns the list envelope with a real ``total`` (VMs that reported metrics) —
        with the default limit of 25, ``truncated`` tells you whether more VMs sit
        behind it. LIVE data (cpu_usage_pct, mem_usage_pct, mem_consumed_mb,
        mem_ballooned_mb, mem_swapped_mb, disk_read_kbps, disk_write_kbps, net_kbps),
        busiest first. mem_usage_pct is mem.usage.average — ACTIVE guest memory over
        configured, not consumed, so it can read low on a VM under pressure; non-zero
        mem_ballooned_mb (mem.vmmemctl.average) or mem_swapped_mb (mem.swapped.average)
        is the pressure signal. ``counters`` names the counter behind every field.
        Only powered-on VMs have a real-time provider; powered-off VMs are skipped.
        Point-in-time only.

        Use this to rank load across VMs; for one VM's configuration use get_vm_info, and
        to see the same VM correlated with its host, alarms and events use
        investigate_vm.

        Args:
            vm_name: Filter to a single VM by exact name (None = all powered-on VMs).
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max VM rows to return (default 25; None = all).
        """
        si = runtime._get_connection(target)
        return runtime._ops_get_vm_performance(si, vm_name=vm_name, limit=limit)

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
    def find_aging_snapshots(
        age_threshold_days: int = 30,
        only_old: bool = False,
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] Sweep ALL VMs for snapshots and flag old / sprawling ones.

        Use this for fleet-wide snapshot sprawl; prefer list_vm_snapshots when you
        only care about one VM. Returns {total_snapshots, old_snapshots,
        vms_with_snapshots, threshold_days, snapshots[], hint} — a rollup, not the
        list envelope. Each row has age_days, is_old and an est_size_mb lower bound
        (snapshotData+snapshotMemory; delta-disk growth is not separable per-snapshot
        via the API). Read-only — deleting snapshots belongs to vmware-aiops.

        Args:
            age_threshold_days: Age above which a snapshot is flagged old (default 30).
            only_old: When True, return only snapshots past the threshold.
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max snapshot rows to return (None = all).
        """
        si = runtime._get_connection(target)
        return runtime.list_snapshot_aging(
            si, age_threshold_days=age_threshold_days, only_old=only_old, limit=limit
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
    def get_vm_backup_snapshot_history(
        vm_name: str,
        days: int = 30,
        include_cycles: bool = False,
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] How long backups held a snapshot open on one VM, from task history.

        Image-level backup products (Veeam, Commvault, Rubrik, NetBackup) snapshot
        the VM, copy the frozen disks, then delete the snapshot. vCenter records
        both ends, so the backup window is recoverable without credentials for the
        backup server.

        This is a LOWER BOUND on the job, not its duration: work the product does
        before the snapshot is taken and after it is removed is invisible to
        vCenter. The returned ``basis`` says so — quote it alongside any figure.

        Returns a rollup, not the list envelope: counts, four hour statistics
        (backup_active / snapshot_present / total_window / snapshot_removal),
        latest_cycle, longest_cycle, and ``unmatched`` rows for creations with no
        removal (a backup that left its snapshot behind) or removals whose creation
        predates the window.

        Read two fields before reporting all-clear. ``history_unavailable`` non-null
        means the history could not be read at all — that is not "no backups".
        ``coverage_note`` non-null means vCenter has already expired part of the
        requested window (``task.maxAge`` — 30 days by default; the option is NOT
        called vpxd.task.maxAge, which raises vim.fault.InvalidName), so the counts
        describe a shorter period than ``days`` asked for. ``window_fully_covered``
        is the positive form: True when retention is at least ``days`` and nothing
        in the window can have expired, so a VM whose oldest task is recent was
        simply quiet; None when retention or the history could not be read.

        An unmatched creation is not a failed backup. Each one carries ``status:
        "open"``, ``age_hours``, this VM's ``longest_completed_cycle_hours`` and
        ``possibly_in_progress`` — True while it is inside that VM's own observed
        envelope, False past it, None when there is no completed cycle to compare
        against. Do not report ``incomplete_cycles`` as failures without reading
        those.

        The creation *task* itself, during the seconds it executes, is absent
        entirely: no completion time yet, so it forms neither a cycle nor an
        ``unmatched`` row. A backup whose snapshot already exists is not this case —
        that is the open cycle described above. So no open cycle does not prove no
        backup is running.

        Duplicate VM names raise rather than resolve to one, because a duration
        attributed to the wrong same-named VM looks exactly like a correct answer.
        For snapshots that exist right now use list_vm_snapshots or find_aging_snapshots.

        Args:
            vm_name: Exact VM name; duplicates are refused, not guessed.
            days: How far back to look, 1 to 365 (default 30).
            include_cycles: Also return the individual cycles, newest kept on cap.
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max cycle rows when include_cycles is set (capped at 200).
        """
        si = runtime._get_connection(target)
        return runtime.get_backup_snapshot_history(
            si, vm_name, days=days, include_cycles=include_cycles, limit=limit
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
    def get_certificate_status(
        warn_days: int = 30,
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] Per-host ESXi management certificate expiry.

        An expired ESXi cert drops host management — this surfaces it before the
        outage. Returns the list envelope with a real ``total``; each row has host,
        not_after, days_until_expiry and an ``expiring`` flag, soonest first. Host
        certificates only — the vCenter appliance's own certificate is not covered.

        Use this alongside get_license_status and get_ntp_status for a platform-hygiene
        sweep. Renewing a certificate is a write this skill cannot do; use
        vmware-aiops or vCenter directly.

        Args:
            warn_days: Flag certs expiring within this many days (default 30).
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max host rows to return (None = all).
        """
        si = runtime._get_connection(target)
        return runtime._ops_get_certificate_status(si, warn_days=warn_days, limit=limit)

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
    def get_license_status(target: Optional[str] = None) -> dict:
        """[READ] vCenter/ESXi license inventory with usage and expiry.

        Returns the list envelope, one row per license: name, edition_key, total/used
        units, unlimited flag (row total == 0) and expiration. Every license is
        enumerated, so ``truncated`` is always False — this is the complete inventory.

        Use this to catch over-allocation or an approaching expiry, alongside
        get_certificate_status and get_ntp_status for a platform-hygiene sweep. ``assignments``
        says which license each asset (vCenter, hosts, clusters) is assigned, with
        ``expired`` per asset — use it to tell whether an "expired license" alarm still
        applies. It is null with ``assignments_note`` when the account cannot read
        assignments. License keys are never returned. Assigning a license is a write.

        Args:
            target: vCenter/ESXi target from config (default if omitted).
        """
        si = runtime._get_connection(target)
        return runtime._ops_get_license_status(si)

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
    def get_ntp_status(
        host_name: Optional[str] = None,
        target: Optional[str] = None,
    ) -> dict:
        """[READ] Per-host NTP configuration health (servers + ntpd service state).

        Returns the list envelope; every matching host is enumerated, so ``truncated``
        is always False. Each row has host, reachable, ntp_servers, ntpd_running,
        ntpd_policy and a ``healthy`` flag (servers configured AND ntpd running). The
        SOAP API does not expose live clock offset or stratum — this is configuration
        health only; for actual offset use esxcli on the host.

        healthy/ntp_servers/ntpd_running are null — not false/empty — for a host
        vCenter could not reach or could not read. Null means nothing was observed;
        false means NTP is misconfigured. Filtering rows for `healthy == false` will
        not surface the unread ones, so check the envelope's `hosts_unreachable`
        and `hosts_unreadable` counts (with `unreachable_note` / `unreadable_note`)
        before reporting the estate as healthy. Unreachable: vCenter cannot reach the
        host (reachable false). Unreadable: this account cannot read the host's state
        (reachable null) — not known to be down.

        The envelope's ``ntp_sources_consistent`` compares hosts with each other:
        false when hosts that have servers configured use different ones (each row
        can still be healthy — this is how clocks drift apart), with
        ``ntp_sources_note`` naming which host uses which servers; null when fewer
        than two hosts have servers to compare.

        Prefer this over get_host_services for time problems: that tool reports
        whether ntpd runs but not which servers are configured. Fixing NTP is a
        write; use vmware-aiops.

        Args:
            host_name: Filter to a single host by exact name (None = all hosts).
            target: vCenter/ESXi target from config (default if omitted).
        """
        si = runtime._get_connection(target)
        return runtime._ops_get_ntp_status(si, host_name=host_name)

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
    def get_datastore_capacity(
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] Per-datastore capacity with thin-provisioning over-commit.

        Returns the list envelope with a real ``total``: capacity_gb, free_gb,
        committed_gb, provisioned_gb, used_pct, overcommit_pct, riskiest first. Adds the
        risk signal list_all_datastores lacks — overcommit_pct over 100% means more
        space is promised to VMs than physically exists, so a thin datastore can fill up
        while still showing free space. Point-in-time.

        Whose figure: ``view`` (vCenter / ESXi host / unknown) and ``view_note``.
        Provisioned space is summed by the endpoint over the VMs it has registered on
        the datastore, so a vCenter and a directly-reached ESXi host can report
        different provisioned_gb for one datastore — each row's ``vm_count`` says how
        many VMs the figure covers, and ``vms_not_connected`` names those reported as
        orphaned/inaccessible/disconnected (None = could not be read). The figures can
        also differ with the same vm_count: each endpoint aggregates uncommitted
        space itself.

        Use this for the capacity view, then investigate_datastore to drill
        into a specific datastore's hosts, VMs and alarms. Reclaiming space (delete
        snapshots, storage vMotion) belongs to vmware-aiops.

        Args:
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max datastore rows to return (None = all).
        """
        si = runtime._get_connection(target)
        return runtime._ops_get_datastore_capacity(si, limit=limit)

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
    def get_resource_pool_usage(
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] Per-resource-pool CPU/memory reservation, limit, and current usage.

        Returns the list envelope with a real ``total``; each row has name,
        cpu_reservation_mhz, cpu_limit_mhz, cpu_usage_mhz, mem_reservation_mb,
        mem_limit_mb, mem_usage_mb, sorted by memory usage descending. A limit of -1
        means unlimited. Pool names are not cluster-qualified, so identical names in
        different clusters may look alike.

        Use this when a VM is throttled but its host is not busy — the cap is usually
        the pool. Then get_vm_performance to see which VMs in it are demanding, or
        get_cluster_health_summary for the cluster-level picture.

        Args:
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max pool rows to return (None = all).
        """
        si = runtime._get_connection(target)
        return runtime._ops_get_resource_pool_usage(si, limit=limit)

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
    def list_active_tasks(
        include_recent: bool = True,
        target: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """[READ] In-flight (and optionally just-completed) vCenter tasks.

        Answers "why is the cluster busy?". Returns the list envelope {items, returned,
        limit, total, truncated, hint} with a real ``total``; each row has name, entity,
        state, progress_pct, start_time, user, an active flag and error (for failed
        recent tasks), running/queued first, then newest first — so ``limit`` drops the
        oldest. vCenter keeps only a short recent-task
        window, so an old task may simply be gone rather than absent.

        Use this before blaming load: a migration or clone in flight explains
        pressure that get_host_performance shows. Pair with list_active_sessions to see who
        started it. Read-only — cancelling a task belongs to vmware-aiops.

        Args:
            include_recent: Also include recently completed/failed tasks (default True).
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max task rows to return (None = all).
        """
        si = runtime._get_connection(target)
        return runtime.get_active_tasks(si, include_recent=include_recent, limit=limit)

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
    def list_active_sessions(
        target: Optional[str] = None,
        limit: Optional[int] = None,
        include_service: bool = False,
    ) -> dict:
        """[READ] Currently authenticated vCenter/ESXi sessions (who is logged in).

        Returns the list envelope with a real ``total``; each row has user_name,
        full_name, login_time, last_active, ip_address, user_agent (which client —
        e.g. Aria's adapter shows ``VMware vim-java``), call_count, kind (user/service)
        and a ``current`` flag for this skill's own session. vCenter's own solution
        users (``vpxd-extension-<machine id>`` and similar) are folded by default and
        counted in ``service_sessions``. Requires the Sessions privilege; low-privilege
        accounts get a single explanatory row instead of a traceback.

        Use this to attribute a change to a person — pair it with list_active_tasks, which
        names the user who started each task. Read-only — terminating a session is
        not supported here.

        Args:
            target: vCenter/ESXi target from config (default if omitted).
            limit: Max session rows to return (None = all).
            include_service: List vCenter's own solution-user sessions too. On a lab
                vCenter they were 24 of 33 sessions.
        """
        si = runtime._get_connection(target)
        return runtime.get_active_sessions(si, limit=limit, include_service=include_service)

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
    def get_access_info(
        tool: Optional[str] = None,
        target: Optional[str] = None,
    ) -> dict:
        """[READ] The account this server uses on a target, and whether it holds a tool's privileges.

        When to use: a tool failed with NoPermission, returned fewer objects than
        expected, or you want to know up front what the configured account may read.
        Ask about one ``tool``, or omit it to sort every tool of this server into
        granted / not granted / undetermined.

        What it returns: {target, account, checked_on, privileges {id: state}, and
        either ``tool`` {name, requires, verdict, meaning, missing_at_root} or the
        tools_* lists}, plus the account's direct permissions (role, on, propagate).

        Gotchas: privileges are checked on the vCenter/ESXi **root folder** only.
        ``not_granted_at_root`` does not mean no access — a permission on a cluster,
        folder or VM still lets the tool work there. ``undetermined`` means the
        platform did not answer and is never to be read as granted. Permissions held
        through a group count in the verdict but are not listed by name.

        Args:
            tool: A tool name on this server, e.g. list_active_sessions (None = all tools).
            target: vCenter/ESXi target from config (default if omitted).
        """
        from vmware_policy.access import unknown_tool_message, vsphere_access_info

        from vmware_monitor.access_requirements import REQUIREMENTS

        unknown = unknown_tool_message(tool, REQUIREMENTS)  # no login for a misspelled name
        if unknown:
            return {"error": unknown}
        si = runtime._get_connection(target)
        return vsphere_access_info(
            si, target=runtime._get_target_config(target).name, requirements=REQUIREMENTS, tool=tool
        )

    return (
        get_vm_info,
        list_vm_snapshots,
        get_host_performance,
        get_vm_performance,
        find_aging_snapshots,
        get_vm_backup_snapshot_history,
        get_certificate_status,
        get_license_status,
        get_ntp_status,
        get_datastore_capacity,
        get_resource_pool_usage,
        list_active_tasks,
        list_active_sessions,
        get_access_info,
    )
