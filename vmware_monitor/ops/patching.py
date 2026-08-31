"""vSphere 9.1 lifecycle/patch reads over vSphere Automation REST (read-only).

Three READ surfaces that live only on the REST API (not SOAP/pyVmomi), all
VERIFIED in the family VCF 9.1 endpoint spec §D:

    GET /api/vcenter/deployment/size                                (NEW in 9.1)
    GET /api/esx/settings/clusters/{cluster}/software/compliance
    GET /api/esx/settings/clusters/{cluster}/software/reports/last-apply-result

Read-only: these only *report* vLCM compliance / the last apply result / the
appliance size. Running a remediation (``?action=apply``) is a write owned by
vSphere admin tooling and is deliberately not implemented here.

Maintenance awareness: vSphere 9.1 exposes no patch-in-progress status endpoint
(the ``X-VC-Maintenance`` headers are a hallucination — spec §D). When vCenter is
mid-patch it answers 503; :class:`~vmware_monitor.rest.RestNotReadyError` is caught
here and turned into a structured ``{"available": False, ...}`` result so the read
degrades instead of crashing (踩坑 #37: a health read tolerates 5xx as a state).

Response-shape honesty: the exact JSON field names below were taken from the 9.1
OpenAPI but have NOT been replayed against a live 9.1 vCenter from this skill, so
every field is read defensively (``.get`` / ``getattr``, absent → omitted, never a
crash — 踩坑 形态 #1). The verified thing is the *endpoint*; the parse is
best-effort and self-labels as such via the ``note`` field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vmware_policy import sanitize
from vmware_policy.compat import Requires

from vmware_monitor.rest import RestNotReadyError, VsphereRest

if TYPE_CHECKING:
    from vmware_monitor.config import TargetConfig

# Verified paths (spec §D). Kept as literals here so the regression test can assert
# they match the spec module exactly.
DEPLOYMENT_SIZE_PATH = "/api/vcenter/deployment/size"

#: Spec §D annotates this endpoint "NEW in 9.1" — that annotation is the
#: evidence, not a recollection. The other two paths here are not annotated as
#: new, so they carry no floor: a version branch on a call that behaves the same
#: across 8.x and 9.x is only somewhere for a later reader to introduce a
#: difference by accident.
REQUIRES_DEPLOYMENT_SIZE = Requires(
    product="vCenter",
    minimum=(9, 1),
    feature="Appliance deployment-size read",
)
_COMPLIANCE_TMPL = "/api/esx/settings/clusters/{cluster}/software/compliance"
_LAST_APPLY_TMPL = "/api/esx/settings/clusters/{cluster}/software/reports/last-apply-result"

#: Replayed against a live VCF 9.1 vCenter on 2026-08-31: both endpoints
#: returned 200 and the fields below parsed as written, on two clusters
#: (status/impact/commit/stage_status, and the four host buckets). The note it
#: replaces said "pending live 9.1 vCenter" -- leaving that in after the replay
#: would tell the next reader to go and verify what has been verified.
_VERIFIED = "endpoint and field parse verified against a live VCF 9.1 vCenter (2026-08-31)"


def _not_ready(exc: RestNotReadyError, resource: str) -> dict:
    """Structured 'busy, no ETA' result for a tolerated 5xx/timeout."""
    return {
        "available": False,
        "resource": resource,
        "reason": sanitize(str(exc)),
        "note": "vSphere exposes no maintenance-ETA endpoint; retry shortly.",
    }


def _scalar_fields(data: Any) -> dict:
    """Sanitised top-level scalar fields of a JSON object (defensive passthrough).

    Non-dict input (an unexpected shape) yields ``{}`` rather than raising.
    """
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, bool) or value is None:
            out[str(key)] = value
        elif isinstance(value, (int, float)):
            out[str(key)] = value
        elif isinstance(value, str):
            out[str(key)] = sanitize(value)
    return out


def get_deployment_size(target: TargetConfig) -> dict:
    """[READ] vCenter appliance deployment size (NEW in vSphere 9.1).

    Returns ``{available, note, fields}`` — ``fields`` is a defensive passthrough
    of the endpoint's top-level scalar values (e.g. current/target size class).
    ``available: False`` means vCenter answered 5xx (mid-patch); nothing crashed.
    """
    rest = VsphereRest(target)
    try:
        data = rest.get_json(DEPLOYMENT_SIZE_PATH, requires=REQUIRES_DEPLOYMENT_SIZE)
    except RestNotReadyError as exc:
        return _not_ready(exc, "deployment_size")
    return {"available": True, "note": _VERIFIED, "fields": _scalar_fields(data)}


# Per-host compliance key names. The vLCM HostCompliance *path* is spec-verified,
# but the field name inside each host row is NOT yet replayed against a live 9.1
# vCenter — the two plausible schema spellings are tried in order.
#: The four host buckets vLCM reports at the top level of a compliance response,
#: mapped to the key each appears under. Read directly rather than derived: a
#: live 9.1 payload carries `compliant_hosts`, `non_compliant_hosts`,
#: `unavailable_hosts` and `incompatible_hosts` as sibling arrays, so vCenter
#: has already made the distinction and there is nothing to infer.
_HOST_BUCKETS = {
    "compliant": "compliant_hosts",
    "non_compliant": "non_compliant_hosts",
    "unavailable": "unavailable_hosts",
    "incompatible": "incompatible_hosts",
}


#: Per-host status value -> bucket, for payloads that carry only a `hosts` map.
#: Everything unrecognised lands in "unknown", which is reported as its own
#: number and never added to non_compliant.
_STATUS_BUCKET = {
    "COMPLIANT": "compliant",
    "NON_COMPLIANT": "non_compliant",
    "UNAVAILABLE": "unavailable",
    "INCOMPATIBLE": "incompatible",
}

#: Keys a per-host row may use for its status, in preference order.
_HOST_STATUS_KEYS = ("compliance_status", "status")


def _buckets_from_hosts(hosts: Any) -> dict[str, int] | None:
    """Fallback: classify a per-host map when the top-level arrays are absent.

    Kept so a payload shaped only as ``{"hosts": {...}}`` still yields counts
    rather than a shrug — dropping to "unknown" there would be a capability
    regression dressed as caution.

    The bug being fixed is not *where* the count came from, it is that every
    status other than COMPLIANT was added together:

        sum(1 for s in statuses if s.upper() not in ("", "COMPLIANT"))

    so four UNAVAILABLE hosts were reported as four hosts needing patches. Here
    each state keeps its own tally and nothing is merged.
    """
    if not isinstance(hosts, dict):
        return None
    counts = {label: 0 for label in _HOST_BUCKETS}
    counts["unknown"] = 0
    seen = False
    for row in hosts.values():
        if not isinstance(row, dict):
            continue
        status = ""
        for key in _HOST_STATUS_KEYS:
            if key in row:
                status = str(row.get(key) or "")
                break
        else:
            continue
        seen = True
        counts[_STATUS_BUCKET.get(status.upper(), "unknown")] += 1
    return counts if seen else None


def _host_buckets(data: dict) -> dict[str, int | None]:
    """Per-bucket host counts. ``None`` for any bucket nothing could establish.

    A live 9.1 compliance payload states the answer outright, as four sibling
    arrays, so the first choice is to read what vCenter said rather than derive
    it. On the cluster this was caught with, vCenter reported
    ``non_compliant_hosts: []`` -- zero -- and ``unavailable_hosts`` with four
    entries, while the tool reported four hosts needing patches.

    The function it replaces guarded the opposite error, and its docstring said
    so: "a patch-compliance tool must never silently report a false all
    compliant". It did guard that, and then made the mirror-image mistake. For a
    patch tool both are expensive: one hides work, the other invents a
    maintenance window for hosts that were merely unreachable.
    """
    out: dict[str, int | None] = {}
    stated = False
    for label, key in _HOST_BUCKETS.items():
        value = data.get(key)
        if isinstance(value, (list, dict)):
            out[label] = len(value)
            stated = True
        else:
            out[label] = None
    out["unknown"] = None

    if stated:
        return out

    derived = _buckets_from_hosts(data.get("hosts"))
    return {**out, **derived} if derived else out


def get_patch_compliance(target: TargetConfig, cluster: str) -> dict:
    """[READ] vLCM software compliance for one cluster.

    ``cluster`` is the vCenter cluster MoID (e.g. ``domain-c123``), as the REST API
    requires; get it from 'vmware-monitor inventory clusters'. Returns
    ``{available, cluster, status, hosts_total, non_compliant_hosts, note, fields}``
    with every field read defensively; ``available: False`` on a tolerated 503.
    """
    rest = VsphereRest(target)
    path = _COMPLIANCE_TMPL.format(cluster=cluster)
    try:
        data = rest.get_json(path)
    except RestNotReadyError as exc:
        out = _not_ready(exc, "patch_compliance")
        out["cluster"] = sanitize(cluster)
        return out
    data = data if isinstance(data, dict) else {}
    hosts = data.get("hosts")
    hosts_total = len(hosts) if isinstance(hosts, (dict, list)) else None
    buckets = _host_buckets(data)
    return {
        "available": True,
        "cluster": sanitize(cluster),
        "status": sanitize(str(data.get("status", "unknown"))),
        "hosts_total": hosts_total,
        # vCenter's own count, not a derived one. None means this payload did not
        # report the bucket -- which is "unknown", never "zero".
        "non_compliant_hosts": buckets["non_compliant"],
        "compliant_hosts": buckets["compliant"],
        # Kept separate and never folded into non_compliant: a host that could
        # not be scanned has not been found to need anything.
        "unavailable_hosts": buckets["unavailable"],
        "incompatible_hosts": buckets["incompatible"],
        # A status this tool does not recognise is its own number too, so a
        # schema change shows up as "unknown", not as compliant or as work.
        "unknown_status_hosts": buckets["unknown"],
        "scan_time": sanitize(str(data.get("scan_time", ""))) or None,
        "fields": _scalar_fields(data),
    }


def get_last_apply_result(target: TargetConfig, cluster: str) -> dict:
    """[READ] Result of the last vLCM remediation (apply) on one cluster.

    ``cluster`` is the cluster MoID (see get_patch_compliance). Returns
    ``{available, cluster, status, end_time, note, fields}``; a cluster that was
    never remediated may 404 → an authored 'not found' teaching error. A tolerated
    503 yields ``available: False``.
    """
    rest = VsphereRest(target)
    path = _LAST_APPLY_TMPL.format(cluster=cluster)
    try:
        data = rest.get_json(path)
    except RestNotReadyError as exc:
        out = _not_ready(exc, "last_apply_result")
        out["cluster"] = sanitize(cluster)
        return out
    data = data if isinstance(data, dict) else {}
    return {
        "available": True,
        "cluster": sanitize(cluster),
        "status": sanitize(str(data.get("status", "unknown"))),
        "end_time": sanitize(str(data.get("end_time", ""))) or None,
        "note": _VERIFIED,
        "fields": _scalar_fields(data),
    }
