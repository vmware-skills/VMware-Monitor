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

from vmware_monitor.rest import RestNotReadyError, VsphereRest

if TYPE_CHECKING:
    from vmware_monitor.config import TargetConfig

# Verified paths (spec §D). Kept as literals here so the regression test can assert
# they match the spec module exactly.
DEPLOYMENT_SIZE_PATH = "/api/vcenter/deployment/size"
_COMPLIANCE_TMPL = "/api/esx/settings/clusters/{cluster}/software/compliance"
_LAST_APPLY_TMPL = "/api/esx/settings/clusters/{cluster}/software/reports/last-apply-result"

_BEST_EFFORT = "endpoint verified (spec §D); field parse best-effort pending live 9.1 vCenter"


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
        data = rest.get_json(DEPLOYMENT_SIZE_PATH)
    except RestNotReadyError as exc:
        return _not_ready(exc, "deployment_size")
    return {"available": True, "note": _BEST_EFFORT, "fields": _scalar_fields(data)}


# Per-host compliance key names. The vLCM HostCompliance *path* is spec-verified,
# but the field name inside each host row is NOT yet replayed against a live 9.1
# vCenter — the two plausible schema spellings are tried in order.
_HOST_STATUS_KEYS = ("compliance_status", "status")


def _count_non_compliant(hosts: Any) -> int | None:
    """Count non-compliant host rows, or ``None`` when the schema key is absent.

    Reads whichever of ``_HOST_STATUS_KEYS`` a host row carries. Critically: if NO
    host row carries *either* key (a wrong schema guess), returns ``None`` (unknown)
    rather than ``0`` — a patch-compliance tool must never silently report a false
    "all compliant / non_compliant: 0" just because it looked for the wrong field
    (踩坑 形态 #1: an empty/unmatched read is 'unknown', not 'none').
    """
    if not isinstance(hosts, dict):
        return None
    statuses: list[str] = []
    for h in hosts.values():
        if not isinstance(h, dict):
            continue
        for key in _HOST_STATUS_KEYS:
            if key in h:
                statuses.append(str(h.get(key, "")))
                break
    if not statuses:
        return None
    return sum(1 for s in statuses if s.upper() not in ("", "COMPLIANT"))


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
    non_compliant = _count_non_compliant(hosts)
    return {
        "available": True,
        "cluster": sanitize(cluster),
        "status": sanitize(str(data.get("status", "unknown"))),
        "hosts_total": hosts_total,
        "non_compliant_hosts": non_compliant,
        "scan_time": sanitize(str(data.get("scan_time", ""))) or None,
        "note": _BEST_EFFORT,
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
        "note": _BEST_EFFORT,
        "fields": _scalar_fields(data),
    }
