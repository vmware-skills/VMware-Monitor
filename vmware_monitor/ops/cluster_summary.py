"""Cluster health summary: one aggregated read for fast cross-cluster triage.

Answers the operator's first question — *"is anything on fire?"* — in a single
call, instead of the agent stitching ``list_clusters`` + ``list_hosts`` +
``get_alarms`` + capacity together (which a smaller local model often mis-orders,
GitHub issue #31 discussion). The aggregation runs server-side via the batched
PropertyCollector helpers and comes back as high-signal per-cluster rows; the
model renders the table and explains it in operational language — it never sees
raw inventory.

Read-only. Point-in-time snapshot, not a trend — no growth rate or days-until-full
is invented here (same honesty note as ``capacity.py``).

The rendered view is intentionally opinionated: three signals an operator looks
at first — Problems (alarms / disconnected hosts), Capacity (CPU / memory
pressure), Health (HA / DRS posture) — compressed into one ``status`` per
cluster. What the table shows is customizable and the columns are meant to be
added to or trimmed; the layout, thresholds, and an "extend here" example live
in ``references/health-summary-template.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import sanitize

from vmware_monitor.ops._collect import _collect, _collect_objects

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

# ── Opinionated thresholds (named, not magic numbers) ───────────────────────
# CPU/memory utilisation at or above these percentages flag a cluster.
CPU_MEM_WARN_PCT = 85
CPU_MEM_CRIT_PCT = 95

# Alarm severity keys as vSphere reports them (overallStatus).
_RED = "red"
_YELLOW = "yellow"

# Bucket name for hosts that belong to no cluster (standalone hosts).
_STANDALONE = "(standalone hosts)"

#: Bucket for alarms raised above any cluster or host — datacenter and vCenter
#: root. Its own scope because attributing "expired vCenter license" to a host
#: would send the operator to the wrong machine.
_VCENTER_SCOPE = "(vCenter-level)"

# Default size of the "top issues" focus list.
DEFAULT_TOP_N = 10

# Ordering within a severity band: which kind of problem an operator should look
# at first. Lower rank surfaces higher in the top-issues list.
_KIND_RANK = {"host_down": 0, "alarm": 1, "capacity": 2, "config": 3}
_SEV_RANK = {"critical": 0, "warning": 1}

# Per-kind drill-down hint so each top issue points at the next tool to run.
_DRILLDOWN = {
    "host_down": "vmware-monitor inventory hosts / health alarms; reconnect via vmware-aiops",
    "alarm": "vmware-monitor get_alarms for detail; remediate via vmware-aiops",
    "capacity": "vmware-monitor perf hosts / capacity datastores on this cluster",
    "config": "enable vSphere HA on this cluster (vSphere admin)",
}

# One friendly line rendered under every summary so the operator always knows
# they can reshape it. Kept here (not in the CLI) so the MCP tool returns it
# too and the model can echo it verbatim.
CUSTOMIZATION_HINT = (
    "Want a different view? Just say so — e.g. "
    '"add datastore free space", "drop the DRS column", '
    '"only show clusters that need attention", "add per-VM CPU ready", '
    'or "render this as an HTML page". '
    "Columns, thresholds, and grouping are all adjustable."
)


def _pct(used: float, total: float) -> float:
    """Utilisation percentage, guarding divide-by-zero. Rounded to 1 dp."""
    if not total:
        return 0.0
    return round(used / total * 100, 1)


def _root_alarm_states(si: ServiceInstance) -> list:
    """Every triggered alarm in the inventory, read from the root folder.

    The root folder accumulates descendants' alarms, so this one read sees all
    of them — each still carrying the entity it was actually raised on.

    Returns ``(states, error)``. A vCenter that will not answer for its root
    folder must not take the whole summary down with it — the summary is what
    gets run precisely when something is wrong — but the failure is returned so
    the caller can surface it, because an empty list here is indistinguishable
    from "nothing is wrong" and this read covers the alarms nothing else sees.
    """
    try:
        return list(getattr(si.content.rootFolder, "triggeredAlarmState", None) or []), None
    except Exception as exc:
        # Degrade, but never silently. Returning a bare [] here would say
        # "there are no vCenter-level alarms" — and this read is the ONLY thing
        # that sees the alarms no other pass covers, so a failure has to become
        # a visible issue rather than a clean bill of health.
        return [], f"{type(exc).__name__}: {exc}"


def _empty_cluster(name: str, ha: bool, drs: bool) -> dict:
    """A fresh per-cluster accumulator with all counters zeroed."""
    return {
        "name": sanitize(name),
        "hosts_total": 0,
        "hosts_connected": 0,
        "vms_total": 0,
        "vms_on": 0,
        "_cpu_used_mhz": 0.0,
        "_cpu_total_mhz": 0.0,
        "_mem_used_mb": 0.0,
        "_mem_total_bytes": 0.0,
        "ha_enabled": ha,
        "drs_enabled": drs,
        "alarms": {"critical": 0, "warning": 0},
    }


def _severity(state: object) -> str | None:
    """Map an AlarmState overallStatus to 'critical'/'warning' (None if green)."""
    status = str(getattr(state, "overallStatus", ""))
    if status == _RED:
        return "critical"
    if status == _YELLOW:
        return "warning"
    return None


def _scan_alarms(
    triggered: list | None,
    bucket: dict,
    scope_type: str,
    scope_name: str,
    cluster_name: str | None,
    raw: list,
) -> None:
    """Count a triggeredAlarmState list into a cluster AND stash each alarm.

    Counts feed the per-cluster column; the stashed ``(alarm_ref, severity,
    scope_type, scope_name, cluster_name)`` tuples feed the top-issues list.
    Alarm *names* are resolved later in one batched call (never a lazy read per
    alarm — issue #31 class).
    """
    for state in triggered or []:
        sev = _severity(state)
        if sev is None:
            continue
        bucket["alarms"]["critical" if sev == "critical" else "warning"] += 1
        raw.append((getattr(state, "alarm", None), sev, scope_type, scope_name, cluster_name))


def _rollup_status(rec: dict) -> tuple[str, list[str]]:
    """Compress a cluster's signals into (status, human-readable reasons).

    status is one of ``critical`` | ``warn`` | ``ok`` — the single opinionated
    verdict an operator scans first. ``attention`` explains *why* in operational
    language (empty when ok).
    """
    reasons: list[str] = []
    status = "ok"

    disconnected = rec["hosts_total"] - rec["hosts_connected"]
    if disconnected > 0:
        status = "critical"
        reasons.append(f"{disconnected} host(s) disconnected")

    crit = rec["alarms"]["critical"]
    warn = rec["alarms"]["warning"]
    if crit:
        status = "critical"
        reasons.append(f"{crit} critical alarm(s)")

    cpu = rec["cpu_used_pct"]
    mem = rec["mem_used_pct"]
    for label, val in (("CPU", cpu), ("memory", mem)):
        if val >= CPU_MEM_CRIT_PCT:
            status = "critical"
            reasons.append(f"{label} at {val}%")
        elif val >= CPU_MEM_WARN_PCT:
            if status != "critical":
                status = "warn"
            reasons.append(f"{label} at {val}%")

    if warn:
        if status == "ok":
            status = "warn"
        reasons.append(f"{warn} warning alarm(s)")

    if rec["hosts_total"] > 1 and not rec["ha_enabled"]:
        if status == "ok":
            status = "warn"
        reasons.append("HA disabled on a multi-host cluster")

    return status, reasons


def get_cluster_health_summary(
    si: ServiceInstance,
    cluster_filter: str | None = None,
    include_vms: bool = True,
    top_n: int = DEFAULT_TOP_N,
) -> dict:
    """Aggregated per-cluster health for a fast "is anything on fire?" glance.

    One batched pass each over clusters, hosts, and (optionally) VMs — three
    server-side ``RetrievePropertiesEx`` calls total, not one per object (plus
    one more batched call to resolve alarm names). Rolls host utilisation, VM
    power state, and triggered alarms up to the owning cluster, assigns an
    opinionated ``status``, and flattens the individual anomalies into a ranked
    ``top_issues`` focus list — the headline for large fleets, where scanning
    per-cluster rows is too slow.

    Args:
        si: vSphere ServiceInstance.
        cluster_filter: Case-insensitive substring; only clusters whose name
            matches are returned (None = all). Standalone hosts are included
            only when the filter is None.
        include_vms: Roll up VM total/powered-on counts. Set False to skip the
            VM inventory pass entirely (faster on very large fleets where you
            only care about host/alarm/capacity signals).
        top_n: Cap the ``top_issues`` focus list at this many entries (default
            10). ``issues_total`` always reports the pre-cap count so truncation
            is visible. Use 0 to omit the list.

    Returns:
        dict with:
          - ``totals``: cross-cluster rollup (clusters, hosts, vms, alarms,
            worst_status).
          - ``top_issues``: ranked list (worst first, capped at top_n) of the
            individual anomalies — disconnected hosts, triggered alarms, and
            capacity/HA problems — each with severity, object, cluster, detail,
            and a drill-down hint.
          - ``issues_total``: total anomalies found before the top_n cap.
          - ``clusters``: list of per-cluster rows sorted worst-status-first,
            each with hosts_connected/total, vms_on/total, cpu_used_pct,
            mem_used_pct, ha_enabled, drs_enabled, alarms, status, attention.
          - ``snapshot``: honesty note that this is point-in-time.
          - ``customization_hint``: the friendly "reshape this view" line to
            echo to the operator.
    """
    raw_alarms: list = []
    host_issues: list[dict] = []
    # Pass 1 — clusters. Build the accumulators and a host_ref → cluster_name map
    # from each cluster's own host list (cheaper and clearer than reading every
    # host's parent).
    cluster_props = [
        "name",
        "host",
        "summary.totalCpu",
        "summary.totalMemory",
        "configuration.dasConfig.enabled",
        "configuration.drsConfig.enabled",
        "triggeredAlarmState",
    ]
    needle = cluster_filter.lower() if cluster_filter else None
    clusters: dict[str, dict] = {}
    host_to_cluster: dict[object, str] = {}
    for _obj, p in _collect(si, [vim.ClusterComputeResource], cluster_props):
        name = p.get("name", "")
        if needle and needle not in name.lower():
            continue
        rec = _empty_cluster(
            name,
            ha=bool(p.get("configuration.dasConfig.enabled")),
            drs=bool(p.get("configuration.drsConfig.enabled")),
        )
        rec["_cpu_total_mhz"] = float(p.get("summary.totalCpu") or 0)
        rec["_mem_total_bytes"] = float(p.get("summary.totalMemory") or 0)
        _scan_alarms(p.get("triggeredAlarmState"), rec, "cluster", name, name, raw_alarms)
        clusters[name] = rec
        for host_ref in p.get("host") or []:
            host_to_cluster[host_ref] = name

    # Standalone bucket only when not filtering to a named cluster.
    if needle is None:
        clusters[_STANDALONE] = _empty_cluster(_STANDALONE, ha=False, drs=False)

    # Pass 1b — alarms raised ABOVE any cluster or host.
    #
    # Measured on a live vCenter, 2026-08-29:
    #
    #   rootFolder "Datacenters"  -> 3 alarms, each tagged with its true .entity
    #   Datacenter  "home"        -> 1, the HOST's alarm propagated up
    #   HostSystem  192.168.60.15 -> 1, its own
    #
    # Two things follow. Alarms propagate upward, so reading a parent's list
    # wholesale double-counts what the host and cluster passes already have.
    # And the alarms that belong to nobody else — "Expired vCenter Server
    # license", "Root user password expired." — sit on the root FOLDER, which
    # this function never read. Live, that hid two of three criticals behind a
    # count that looked authoritative, and had those two been the only alarms
    # present the answer would have been "no issues detected".
    #
    # So: read the root folder, which holds every alarm, and keep only the ones
    # whose entity is a Folder or Datacenter. Hosts and clusters are owned by
    # the passes below; anything lower (a VM, say) is not this scope's to claim.
    if needle is None:
        root_states, root_error = _root_alarm_states(si)
        if root_error:
            host_issues.append({
                "severity": "warning",
                "kind": "alarm",
                "object": "vCenter",
                "scope": "vcenter",
                "cluster": _VCENTER_SCOPE,
                "detail": (
                    f"vCenter-level alarms could not be read ({root_error}) — "
                    f"this view is incomplete, not clear"
                ),
                "drilldown": _DRILLDOWN["alarm"],
            })
        for state in root_states:
            entity = getattr(state, "entity", None)
            if not isinstance(entity, (vim.Folder, vim.Datacenter)):
                continue
            rec = clusters.get(_VCENTER_SCOPE)
            if rec is None:
                rec = _empty_cluster(_VCENTER_SCOPE, ha=False, drs=False)
                clusters[_VCENTER_SCOPE] = rec
            scope_name = sanitize(getattr(entity, "name", None) or "vCenter")
            _scan_alarms([state], rec, "vcenter", scope_name, _VCENTER_SCOPE, raw_alarms)

    # Pass 2 — hosts. Roll connection state, live CPU/memory usage, and
    # host-level alarms up to the owning cluster.
    host_props = [
        "name",
        "runtime.connectionState",
        "summary.quickStats.overallCpuUsage",
        "summary.quickStats.overallMemoryUsage",
        # A cluster reports its own totals; a standalone host has no cluster to
        # report them, so its capacity has to come from the host itself. Without
        # these the standalone bucket divides by zero and every percentage reads
        # 0.0 — which is indistinguishable from an idle host, and was reported
        # as one on a real ESXi sitting at 70% memory.
        "summary.hardware.cpuMhz",
        "summary.hardware.numCpuCores",
        "summary.hardware.memorySize",
        "triggeredAlarmState",
    ]
    for obj, p in _collect(si, [vim.HostSystem], host_props):
        cname = host_to_cluster.get(obj, _STANDALONE if needle is None else None)
        rec = clusters.get(cname) if cname else None
        if rec is None:
            continue
        host_name = sanitize(p.get("name", ""))
        rec["hosts_total"] += 1
        conn = str(p.get("runtime.connectionState"))
        if conn == "connected":
            rec["hosts_connected"] += 1
        else:
            host_issues.append(
                {
                    "severity": "critical",
                    "kind": "host_down",
                    "object": host_name,
                    "scope": "host",
                    "cluster": cname,
                    "detail": f"host {conn}",
                    "drilldown": _DRILLDOWN["host_down"],
                }
            )
        rec["_cpu_used_mhz"] += float(p.get("summary.quickStats.overallCpuUsage") or 0)
        rec["_mem_used_mb"] += float(p.get("summary.quickStats.overallMemoryUsage") or 0)
        if cname == _STANDALONE:
            # Only for the standalone bucket. A cluster's own totals already
            # account for its hosts, and adding them again would double-count.
            rec["_cpu_total_mhz"] += float(p.get("summary.hardware.cpuMhz") or 0) * float(
                p.get("summary.hardware.numCpuCores") or 0
            )
            rec["_mem_total_bytes"] += float(p.get("summary.hardware.memorySize") or 0)
        _scan_alarms(p.get("triggeredAlarmState"), rec, "host", host_name, cname, raw_alarms)

    # Pass 3 — VMs (optional). Minimal props: power state + host ref, mapped up
    # to the cluster. Skipped entirely when include_vms is False.
    if include_vms:
        for _obj, p in _collect(si, [vim.VirtualMachine], ["runtime.powerState", "runtime.host"]):
            # Same fallback pass 2 uses for hosts. Without it every VM on a
            # host outside a cluster was silently skipped, so a standalone ESXi
            # reported "0/0 VMs on" no matter how many were running.
            cname = host_to_cluster.get(
                p.get("runtime.host"), _STANDALONE if needle is None else None
            )
            rec = clusters.get(cname) if cname else None
            if rec is None:
                continue
            rec["vms_total"] += 1
            if str(p.get("runtime.powerState")) == "poweredOn":
                rec["vms_on"] += 1

    return _finalize(si, list(clusters.values()), include_vms, raw_alarms, host_issues, top_n)


def _alarm_issues(si: ServiceInstance, raw_alarms: list) -> list[dict]:
    """Resolve stashed alarm refs to named issues in ONE batched call.

    ``raw_alarms`` holds (alarm_ref, severity, scope_type, scope_name,
    cluster_name). The alarm's display name lives on the referenced Alarm managed
    object; reading it per alarm would be a lazy round-trip each (issue #31
    class), so all refs are fetched together via ``_collect_objects``.
    """
    refs = [r[0] for r in raw_alarms if r[0] is not None]
    name_by_ref = {
        ref: (props.get("info.name") or "alarm")
        for ref, props in _collect_objects(si, refs, vim.alarm.Alarm, ["info.name"])
    }
    issues: list[dict] = []
    for ref, sev, scope_type, scope_name, cluster_name in raw_alarms:
        issues.append(
            {
                "severity": sev,
                "kind": "alarm",
                "object": scope_name,
                "scope": scope_type,
                "cluster": cluster_name,
                "detail": sanitize(str(name_by_ref.get(ref, "alarm"))),
                "drilldown": _DRILLDOWN["alarm"],
            }
        )
    return issues


def _capacity_issues(rec: dict) -> list[dict]:
    """Capacity/HA anomalies for one finalized cluster row (pct fields set)."""
    out: list[dict] = []
    for label, val in (("CPU", rec["cpu_used_pct"]), ("memory", rec["mem_used_pct"])):
        if val >= CPU_MEM_WARN_PCT:
            out.append(
                {
                    "severity": "critical" if val >= CPU_MEM_CRIT_PCT else "warning",
                    "kind": "capacity",
                    "object": rec["name"],
                    "scope": "cluster",
                    "cluster": rec["name"],
                    "detail": f"{label} at {val}%",
                    "_mag": val,
                    "drilldown": _DRILLDOWN["capacity"],
                }
            )
    if rec["hosts_total"] > 1 and not rec["ha_enabled"]:
        out.append(
            {
                "severity": "warning",
                "kind": "config",
                "object": rec["name"],
                "scope": "cluster",
                "cluster": rec["name"],
                "detail": "HA disabled on a multi-host cluster",
                "drilldown": _DRILLDOWN["config"],
            }
        )
    return out


def _rank_issues(issues: list[dict], top_n: int) -> tuple[list[dict], int]:
    """Sort anomalies worst-first and cap to top_n. Returns (list, total)."""

    def key(i: dict):
        # Hotter capacity first within its kind; other kinds share magnitude 0.
        return (
            _SEV_RANK.get(i["severity"], 9),
            _KIND_RANK.get(i["kind"], 9),
            -i.get("_mag", 0),
            i["object"],
        )

    ordered = sorted(issues, key=key)
    for i in ordered:
        i.pop("_mag", None)
    total = len(ordered)
    if top_n <= 0:
        return [], total
    return ordered[:top_n], total


def _finalize(
    si: ServiceInstance,
    records: list[dict],
    include_vms: bool,
    raw_alarms: list,
    host_issues: list[dict],
    top_n: int,
) -> dict:
    """Compute derived percentages/status, drop internals, build the envelope."""
    _status_order = {"critical": 0, "warn": 1, "ok": 2}
    rows: list[dict] = []
    issues: list[dict] = list(host_issues) + _alarm_issues(si, raw_alarms)
    totals = {
        "clusters": 0,
        "hosts_total": 0,
        "hosts_connected": 0,
        "vms_total": 0,
        "vms_on": 0,
        "alarms": {"critical": 0, "warning": 0},
        "worst_status": "ok",
    }
    for rec in records:
        # Drop empty standalone bucket so it doesn't clutter the table.
        if rec["name"] == _STANDALONE and rec["hosts_total"] == 0:
            continue
        rec["cpu_used_pct"] = _pct(rec.pop("_cpu_used_mhz"), rec.pop("_cpu_total_mhz"))
        mem_used_bytes = rec.pop("_mem_used_mb") * 1024 * 1024
        rec["mem_used_pct"] = _pct(mem_used_bytes, rec.pop("_mem_total_bytes"))
        status, attention = _rollup_status(rec)
        rec["status"] = status
        rec["attention"] = attention
        issues.extend(_capacity_issues(rec))
        if not include_vms:
            rec.pop("vms_total", None)
            rec.pop("vms_on", None)

        # The vCenter-level bucket is an alarm scope, not a compute scope — no
        # hosts, no VMs, no capacity. Counting it made an estate with one host
        # and no clusters report "2 clusters". Its alarms still roll up below;
        # only the cluster tally skips it.
        if rec["name"] != _VCENTER_SCOPE:
            totals["clusters"] += 1
        totals["hosts_total"] += rec["hosts_total"]
        totals["hosts_connected"] += rec["hosts_connected"]
        totals["vms_total"] += rec.get("vms_total", 0)
        totals["vms_on"] += rec.get("vms_on", 0)
        totals["alarms"]["critical"] += rec["alarms"]["critical"]
        totals["alarms"]["warning"] += rec["alarms"]["warning"]
        if _status_order[status] < _status_order[totals["worst_status"]]:
            totals["worst_status"] = status
        rows.append(rec)

    if not include_vms:
        totals.pop("vms_total")
        totals.pop("vms_on")

    rows.sort(key=lambda r: (_status_order[r["status"]], r["name"]))
    top_issues, issues_total = _rank_issues(issues, top_n)
    return {
        "totals": totals,
        "top_issues": top_issues,
        "issues_total": issues_total,
        "clusters": rows,
        "snapshot": "point-in-time; not a trend (no history retained)",
        "customization_hint": CUSTOMIZATION_HINT,
    }
