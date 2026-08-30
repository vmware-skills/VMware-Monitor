"""Infrastructure health: certificates, licenses, NTP/time config (read-only).

These three areas cause silent, scheduled outages: an expired ESXi certificate
drops host management, an expired license disables features, and unsynced time
breaks Kerberos / vCenter SSO / log correlation. None are covered by inventory
or perf monitoring.

All read-only. Remediation (renew cert, assign license, fix NTP) is a write
operation owned by vmware-aiops / vSphere admin tooling, not this skill.

Honesty note on NTP: the vSphere SOAP API exposes NTP *configuration* (which
servers, is ntpd running) but NOT the live clock offset / stratum — that
requires `esxcli system ntp test` or host SSH, which this read-only skill does
not do. We report configuration health and say so, rather than inventing an
offset number.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pyVmomi import vim
from vmware_policy import paginated, sanitize

from vmware_monitor.ops._collect import _collect, _collect_objects

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

# Days-until-expiry below which a certificate/license is flagged.
CERT_WARN_DAYS = 30


#: ``runtime.connectionState`` for a host vCenter is actually talking to.
_CONNECTED = "connected"


def _days_until(when: datetime | None, now: datetime) -> int | None:
    if when is None:
        return None
    w = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return round((w - now).total_seconds() / 86400)


def get_certificate_status(
    si: ServiceInstance,
    warn_days: int = CERT_WARN_DAYS,
    limit: int | None = None,
) -> dict:
    """Per-host ESXi management certificate expiry.

    Returns the family list envelope with a real ``total`` (every host is
    collected before ``limit`` is applied).
    Uses the API-native ``certificateManager.certificateInfo`` (no PEM parsing,
    no extra dependency). Each row has host, not_after, days_until_expiry, and an
    ``expiring`` flag (True when within warn_days or already expired). Sorted
    soonest-to-expire first.

    Args:
        si: vSphere ServiceInstance.
        warn_days: Flag certs expiring within this many days.
        limit: Max number of host rows to return (None = all).
    """
    now = datetime.now(tz=timezone.utc)
    results: list[dict] = []
    # Pass 1: batch name + the certificateManager reference for every host in one
    # PropertyCollector call (issue #31 class). certificateInfo lives on the
    # HostCertificateManager managed object, which a HostSystem container view
    # cannot cross.
    hosts: list[tuple[str, object]] = []
    cert_refs: list[object] = []
    for _obj, p in _collect(si, [vim.HostSystem], ["name", "configManager.certificateManager"]):
        cert_mgr = p.get("configManager.certificateManager")
        hosts.append((p.get("name", ""), cert_mgr))
        if cert_mgr:
            cert_refs.append(cert_mgr)
    # Pass 2: batch certificateInfo for every certificateManager ref in ONE more
    # call, instead of one lazy read per host.
    info_by_ref = {
        ref: props.get("certificateInfo")
        for ref, props in _collect_objects(
            si, cert_refs, vim.HostCertificateManager, ["certificateInfo"]
        )
    }
    for name, cert_mgr in hosts:
        info = info_by_ref.get(cert_mgr) if cert_mgr else None
        not_after = getattr(info, "notAfter", None) if info else None
        days = _days_until(not_after, now)
        results.append(
            {
                "host": sanitize(name),
                "not_after": str(not_after) if not_after else "unknown",
                "days_until_expiry": days,
                "expiring": bool(days is not None and days <= warn_days),
            }
        )
    results.sort(key=lambda x: (x["days_until_expiry"] is None, x["days_until_expiry"] or 0))
    total = len(results)
    if limit is not None:
        results = results[:limit]
    return paginated(results, limit=limit, total=total)


def get_license_status(si: ServiceInstance) -> dict:
    """vCenter/ESXi license inventory with usage and expiry.

    Returns the family list envelope. No row limit exists here and the whole
    licenseManager collection is enumerated, so ``total`` is real and
    ``truncated`` is False — "this is every license".

    Each row: name, edition_key, total/used units, and any expiration property
    the server exposes. Row ``total = 0`` means an unlimited license (not to be
    confused with the envelope's own ``total``).
    """
    content = si.RetrieveContent()
    lic_mgr = content.licenseManager
    results: list[dict] = []
    for lic in lic_mgr.licenses:
        props = {p.key: p.value for p in (lic.properties or [])}
        expiry = props.get("expirationDate") or props.get("expirationHours")
        results.append(
            {
                "name": sanitize(lic.name),
                "edition_key": sanitize(str(lic.editionKey)) if lic.editionKey else "N/A",
                "total": lic.total,
                "used": lic.used if lic.used is not None else 0,
                "unlimited": lic.total == 0,
                "expiration": sanitize(str(expiry)) if expiry else "never",
            }
        )
    results.sort(key=lambda x: x["name"])
    return paginated(results, total=len(results))


def get_ntp_status(
    si: ServiceInstance,
    host_name: str | None = None,
) -> dict:
    """Per-host NTP configuration health (config + service state).

    Returns the family list envelope. No row limit exists here and every
    matching host is enumerated, so ``total`` is real and ``truncated`` is
    False. Each row has host, reachable, ntp_servers (configured), ntpd_running,
    ntpd_policy, and a ``healthy`` flag (servers configured AND ntpd running).
    The live clock offset is NOT included — see module docstring; the SOAP API
    does not expose it. A healthy=False here means "NTP is misconfigured", which
    is the actionable signal users actually need.

    **A host vCenter cannot reach gets ``healthy: None``, not ``False``.** Its
    ``config.dateTimeInfo`` is absent and its HostServiceSystem unreadable, so
    the old defaults — no servers, service not running — produced exactly the
    row a genuinely misconfigured host produces, and four unreachable hosts were
    reported as needing NTP configured (VCF 9.1, 2026-08-30). ``ntp_servers`` and
    ``ntpd_running`` are ``None`` there for the same reason: ``[]`` and ``False``
    are claims about the host. The envelope carries ``hosts_unreachable`` so a
    caller filtering rows for ``healthy is False`` still learns that half the
    estate went unasked.

    Args:
        si: vSphere ServiceInstance.
        host_name: Filter to a single host by exact name (None = all hosts).
    """
    results: list[dict] = []
    # Pass 1: batch name + dateTimeInfo + the serviceSystem reference for every
    # host in one PropertyCollector call (issue #31 class). Fetching
    # config.dateTimeInfo as a narrow path avoids pulling the whole (large) host
    # config; serviceInfo lives on the HostServiceSystem managed object, which a
    # HostSystem container view cannot cross.
    ntp_props = [
        "name",
        "runtime.connectionState",
        "config.dateTimeInfo",
        "configManager.serviceSystem",
    ]
    hosts: list[tuple[str, str, object, object]] = []
    svc_refs: list[object] = []
    for _obj, p in _collect(si, [vim.HostSystem], ntp_props):
        name = p.get("name", "")
        if host_name and name != host_name:
            continue
        state = str(p.get("runtime.connectionState") or "unknown")
        svc_system = p.get("configManager.serviceSystem")
        hosts.append((name, state, p.get("config.dateTimeInfo"), svc_system))
        # No point asking for the service state of a host nobody can reach; the
        # row is unknown either way and the call is a round trip that can hang.
        if svc_system and state == _CONNECTED:
            svc_refs.append(svc_system)
    # Pass 2: batch serviceInfo for every serviceSystem ref in ONE more call,
    # instead of one lazy read per matched host.
    info_by_ref = {
        ref: props.get("serviceInfo")
        for ref, props in _collect_objects(
            si, svc_refs, vim.HostServiceSystem, ["serviceInfo"]
        )
    }
    unreachable = 0
    for name, state, dt_info, svc_system in hosts:
        if state != _CONNECTED:
            unreachable += 1
            results.append(
                {
                    "host": sanitize(name),
                    "reachable": False,
                    "ntp_servers": None,
                    "ntpd_running": None,
                    "ntpd_policy": "unknown",
                    "healthy": None,
                    "note": (
                        f"vCenter cannot reach this host (connectionState="
                        f"{sanitize(state)}), so its NTP state was not read. This "
                        f"is not a report that NTP is unconfigured — reconnect the "
                        f"host and re-run to find out."
                    ),
                }
            )
            continue

        # Each input is separately either read or not. `dateTimeInfo` present
        # with no ntpConfig is a measurement — no NTP servers are configured —
        # while `dateTimeInfo` absent means nobody looked, and the two produced
        # the same `[]` before.
        servers_known = dt_info is not None
        ntp_cfg = getattr(dt_info, "ntpConfig", None) if dt_info else None
        servers = list(getattr(ntp_cfg, "server", []) or []) if ntp_cfg else []

        running = None
        policy = "unknown"
        svc_info = info_by_ref.get(svc_system) if svc_system else None
        if svc_info:
            for svc in svc_info.service:
                if svc.key == "ntpd":
                    running = svc.running
                    policy = svc.policy
                    break

        if servers_known and not servers:
            # Definitive without the service state: a host with no NTP server
            # configured is not synchronising, whatever ntpd is doing.
            healthy = False
        elif servers_known and running is not None:
            healthy = bool(servers and running)
        else:
            healthy = None

        results.append(
            {
                "host": sanitize(name),
                "reachable": True,
                "ntp_servers": [sanitize(s) for s in servers] if servers_known else None,
                "ntpd_running": running,
                "ntpd_policy": policy,
                "healthy": healthy,
                "note": (
                    "live clock offset not exposed by SOAP API; reports config only"
                    if healthy is not None
                    else "host is reachable but its NTP configuration could not be "
                         "read (no dateTimeInfo and/or no serviceSystem); this is "
                         "not a report that NTP is misconfigured"
                ),
            }
        )
    results.sort(key=lambda x: x["host"])
    extra: dict = {"hosts_unreachable": unreachable}
    if unreachable:
        # Its own key rather than the envelope's ``hint``: that one has a fixed
        # family meaning (this page was truncated, here is how to get the rest)
        # and is None when it was not. Only when there is something to say — a
        # banner on every clean run is a banner nobody reads on the run that
        # matters.
        extra["unreachable_note"] = (
            f"{unreachable} of {len(results)} host(s) are unreachable and were "
            f"not read; their healthy field is null, not false. Filtering for "
            f"healthy == false will not show them."
        )
    return paginated(results, total=len(results), **extra)
