"""Cross-vCenter "what needs attention now?" — one ranked list across all targets.

The top level of the object-investigation family (GitHub issue #31 follow-up):
instead of a per-vCenter glance, roll every configured vCenter's cluster-health
summary into a single, globally-ranked ``top_issues`` list — the "where do I look
first, anywhere in the estate?" view. Aggregation happens in the tool; the model
explains the ranked result in operational language and never sees raw inventory.

One dead vCenter must not sink the roll-up: the connection layer resolves targets
into reachable sessions + an ``unreachable`` list (see ``ConnectionManager.
connect_all``), and this aggregator additionally tolerates a target that connects
but errors mid-summary — it is moved to ``unreachable`` and the rest proceed.

Targets can overlap. An ESXi host can be configured as its own target *and* be
managed by a configured vCenter (the lab, 2026-09-15: "2 vCenters · 3/3 hosts"
for one vCenter, one ESXi target and two hosts, with datastore1 listed twice).
Identity is read, never inferred from names — the same host is "192.168.60.15" in
vCenter and "localhost.localdomain" on itself: the endpoint kind from
``about.apiType``, a host by ``summary.hardware.uuid``, a datastore by
``summary.url``. What cannot be read is not merged and not guessed.

Read-only. Point-in-time — no trend is invented.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyVmomi import vim

from vmware_monitor.ops._collect import _collect
from vmware_monitor.ops.cluster_summary import _rank_issues, get_cluster_health_summary

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

# Per-target we want ALL anomalies (not a per-target top-N) so the global ranking
# is faithful; this cap only guards against a pathologically huge single target.
_PER_TARGET_ISSUE_CAP = 100000

_STATUS_ORDER = {"critical": 0, "warn": 1, "ok": 2}

#: ``AboutInfo.apiType`` → endpoint kind. Anything else is unidentified.
_API_TYPE_ENDPOINT = {"VirtualCenter": "vcenter", "HostAgent": "esxi"}
_ENDPOINT_LABEL = {"vcenter": "vCenter", "esxi": "ESXi host", None: "unknown"}
#: vCenter views claim shared hosts and datastores first: they are the managed
#: view, and the order targets happen to be configured in must not decide it.
_ENDPOINT_ORDER = {"vcenter": 0, "esxi": 1, None: 2}

CUSTOMIZATION_HINT = (
    "Want a different cut? Just ask — e.g. "
    '"only critical issues", "group by vCenter", "just the prod cluster", '
    'or "render this as an HTML page". Scope, ranking, and grouping are adjustable.'
)


#: Hardware UUIDs white-box boards ship unprogrammed (SMBIOS defaults). Not
#: exhaustive — the uniqueness rules in ``_host_overlap`` catch the rest.
_PLACEHOLDER_UUIDS = frozenset({"03000200-0400-0500-0006-000700080009"})


def _is_identity(uuid: str) -> bool:
    """False for an absent or known-placeholder hardware UUID.

    White-box boards commonly report all zeros, all f, or an SMBIOS default such
    as ``03000200-0400-0500-0006-000700080009``; merging on one would fold
    distinct hosts together.
    """
    value = str(uuid or "").lower()
    digits = set(value.replace("-", ""))
    return bool(digits) and digits not in ({"0"}, {"f"}) and value not in _PLACEHOLDER_UUIDS


def target_identity(si: ServiceInstance) -> dict:
    """What a target *is*: endpoint kind, host hardware UUIDs, datastore URLs.

    Each part degrades on its own to ``None`` (unreadable), never to an empty
    collection that would read as "nothing to merge". Two batched reads.

    Returns:
        ``{"endpoint": "vcenter" | "esxi" | None,
           "hosts": {uuid: connected} | None,
           "datastore_urls": {name: url} | None}`` — a hardware UUID reported by
        two hosts of one target, and a datastore name that occurs twice, are left
        out: neither can identify one object.
    """
    try:
        endpoint = _API_TYPE_ENDPOINT.get(str(si.content.about.apiType))
    except Exception:  # noqa: BLE001 — unreadable kind is unidentified, not a vCenter
        endpoint = None

    hosts: dict[str, bool] | None
    try:
        rows = _collect(si, [vim.HostSystem], ["summary.hardware.uuid", "runtime.connectionState"])
        seen_hosts: dict[str, bool | None] = {}
        for _obj, p in rows:
            uuid = str(p.get("summary.hardware.uuid") or "").lower()
            if not uuid:
                continue
            connected = str(p.get("runtime.connectionState")) == "connected"
            # A second host with the same UUID makes it a placeholder, not an id.
            seen_hosts[uuid] = None if uuid in seen_hosts else connected
        hosts = {u: c for u, c in seen_hosts.items() if c is not None}
    except Exception:  # noqa: BLE001 — degrade to "cannot de-duplicate hosts"
        hosts = None

    urls: dict[str, str] | None
    try:
        seen: dict[str, str | None] = {}
        for _obj, p in _collect(si, [vim.Datastore], ["name", "summary.url"]):
            name, url = p.get("name"), p.get("summary.url")
            if not name or not url:
                continue
            seen[name] = None if name in seen else str(url)
        urls = {n: u for n, u in seen.items() if u is not None}
    except Exception:  # noqa: BLE001 — degrade to "cannot de-duplicate datastores"
        urls = None

    return {"endpoint": endpoint, "hosts": hosts, "datastore_urls": urls}


def _datastore_key(url: str | None) -> str | None:
    """One spelling of a datastore URL, so two endpoints' readings compare equal.

    Observed on the lab, 2026-09-15, for the same VMFS volume:
    vCenter ``ds:///vmfs/volumes/684fa75c-…/`` and the ESXi host
    ``/vmfs/volumes/684fa75c-…``. The volume path is the identity; the scheme and
    trailing slash are presentation.
    """
    if not url:
        return None
    return str(url).removeprefix("ds://").rstrip("/") or None


def _safe_identity(si: ServiceInstance) -> dict | None:
    try:
        return target_identity(si)
    except Exception:  # noqa: BLE001 — identity is an enrichment, never a failure
        return None


def _valid_hosts(identity: dict | None) -> dict[str, bool]:
    return {u: c for u, c in ((identity or {}).get("hosts") or {}).items() if _is_identity(u)}


def _host_overlap(collected: list[tuple[str, dict, dict | None]]) -> dict[str, dict]:
    """Which ESXi targets are hosts a configured vCenter already manages.

    The only real overlap is an ESXi target whose host a vCenter target reports.
    A UUID reported by two vCenter targets is not an identity — a host is
    connected to one vCenter — so it matches nothing (review 2026-09-15, M2:
    two vCenters whose distinct white-box hosts shared a placeholder UUID
    counted 3 hosts instead of 4). vCenter and unidentified targets never shed
    hosts.

    Returns ``{esxi target name: {"vcenter", "shared", "shared_connected"}}``;
    an ESXi target whose hosts point at more than one vCenter is left out.
    """
    owners: dict[str, set[str]] = {}
    for name, _data, identity in collected:
        if (identity or {}).get("endpoint") == "vcenter":
            for uuid in _valid_hosts(identity):
                owners.setdefault(uuid, set()).add(name)

    overlap: dict[str, dict] = {}
    for name, _data, identity in collected:
        if (identity or {}).get("endpoint") != "esxi":
            continue
        matched = {
            uuid: (next(iter(owners[uuid])), connected)
            for uuid, connected in _valid_hosts(identity).items()
            if len(owners.get(uuid, ())) == 1
        }
        vcenters = {vc for vc, _c in matched.values()}
        if len(vcenters) == 1:
            overlap[name] = {
                "vcenter": vcenters.pop(),
                "shared": len(matched),
                "shared_connected": sum(int(bool(c)) for _vc, c in matched.values()),
            }
    return overlap


def get_cross_vcenter_attention(
    sessions: list[tuple[str, ServiceInstance]],
    unreachable: list[tuple[str, str]] | None = None,
    cluster_filter: str | None = None,
    top_n: int = 10,
) -> dict:
    """Merge every target's cluster-health into one globally-ranked attention view.

    Args:
        sessions: ``[(target_name, si)]`` for reachable, connected targets.
        unreachable: ``[(target_name, reason)]`` for targets that failed to connect
            (from ``ConnectionManager.connect_all``); merged into the result so the
            gaps are visible.
        cluster_filter: Case-insensitive cluster substring passed through to each
            target's summary (None = all clusters). With a filter set, hosts are
            not de-duplicated: a direct ESXi target has no clusters, so the filter
            has already left its host out.
        top_n: Cap the merged ``top_issues`` focus list (default 10). ``issues_total``
            always reports the pre-cap count.

    Returns:
        dict with ``targets`` (per-target rollup rows, each with ``endpoint`` and
        ``shared_hosts``), ``top_issues`` (merged, globally ranked, each tagged with
        its ``vcenter``; a datastore seen through two targets is one issue with the
        other view under ``also_seen_via``), ``issues_total``, ``totals``
        (estate-wide, hosts counted once; ``vcenters`` / ``esxi_targets`` /
        ``unidentified_targets``), ``unreachable``, ``snapshot`` and
        ``customization_hint``.
    """
    unreachable_out = [{"vcenter": n, "reason": r} for n, r in (unreachable or [])]
    collected: list[tuple[str, dict, dict | None]] = []
    for name, si in sessions:
        try:
            data = get_cluster_health_summary(
                si, cluster_filter=cluster_filter, top_n=_PER_TARGET_ISSUE_CAP
            )
        except Exception as e:  # noqa: BLE001 — a mid-summary failure degrades, not fails
            unreachable_out.append({"vcenter": name, "reason": type(e).__name__})
            continue
        collected.append((name, data, _safe_identity(si)))

    collected.sort(key=lambda c: _ENDPOINT_ORDER[(c[2] or {}).get("endpoint")])

    overlap = _host_overlap(collected) if cluster_filter is None else {}
    urls_by_target = {
        name: {
            n: _datastore_key(u) for n, u in ((identity or {}).get("datastore_urls") or {}).items()
        }
        for name, _data, identity in collected
    }

    targets: list[dict] = []
    kept: list[dict] = []
    #: (vCenter name, datastore key) -> index in ``kept`` of that vCenter's issue.
    vc_datastore_issue: dict[tuple[str, str], int] = {}
    totals = {
        "vcenters": 0,
        "esxi_targets": 0,
        "unidentified_targets": 0,
        "clusters": 0,
        "hosts_total": 0,
        "hosts_connected": 0,
        "alarms": {"critical": 0, "warning": 0},
        "worst_status": "ok",
    }

    for name, data, identity in collected:
        endpoint = (identity or {}).get("endpoint")
        t = data["totals"]
        worst = t.get("worst_status", "ok")
        over = overlap.get(name, {})
        shared, shared_connected = over.get("shared", 0), over.get("shared_connected", 0)
        targets.append(
            {
                "vcenter": name,
                "endpoint": _ENDPOINT_LABEL[endpoint],
                "worst_status": worst,
                "clusters": t.get("clusters", 0),
                "hosts_connected": t.get("hosts_connected", 0),
                "hosts_total": t.get("hosts_total", 0),
                "shared_hosts": shared,
                "alarms": t.get("alarms", {"critical": 0, "warning": 0}),
            }
        )

        # A datastore issue is merged only across a real overlap: this ESXi target
        # is a host the named vCenter manages, and both report the same volume.
        # Two vCenters sharing an NFS export are two views of real risk, each kept
        # (review 2026-09-15, M3: the first version hid a 400% under a 110%).
        managed_by = over.get("vcenter")
        for issue in data.get("top_issues", []):
            key = (
                urls_by_target[name].get(issue.get("object"))
                if issue.get("scope") == "datastore"
                else None
            )
            if endpoint == "vcenter" and key:
                vc_datastore_issue.setdefault((name, key), len(kept))
            elif managed_by and key and (managed_by, key) in vc_datastore_issue:
                primary = kept[vc_datastore_issue[(managed_by, key)]]
                primary["also_seen_via"] = [
                    *primary.get("also_seen_via", []),
                    {"vcenter": name, "detail": issue["detail"]},
                ]
                continue
            kept.append({**issue, "vcenter": name})

        key = {"vcenter": "vcenters", "esxi": "esxi_targets"}.get(endpoint, "unidentified_targets")
        totals[key] += 1
        totals["clusters"] += t.get("clusters", 0)
        totals["hosts_total"] += max(0, t.get("hosts_total", 0) - shared)
        totals["hosts_connected"] += max(0, t.get("hosts_connected", 0) - shared_connected)
        totals["alarms"]["critical"] += t.get("alarms", {}).get("critical", 0)
        totals["alarms"]["warning"] += t.get("alarms", {}).get("warning", 0)
        if _STATUS_ORDER.get(worst, 2) < _STATUS_ORDER.get(totals["worst_status"], 2):
            totals["worst_status"] = worst

    # Global re-rank across every target's anomalies (reuses the summary ranking).
    top_issues, issues_total = _rank_issues(kept, top_n)
    targets.sort(key=lambda r: (_STATUS_ORDER.get(r["worst_status"], 2), r["vcenter"]))

    return {
        "targets": targets,
        "top_issues": top_issues,
        "issues_total": issues_total,
        "totals": totals,
        "unreachable": unreachable_out,
        "snapshot": "point-in-time; not a trend (no history retained)",
        "customization_hint": CUSTOMIZATION_HINT,
    }
