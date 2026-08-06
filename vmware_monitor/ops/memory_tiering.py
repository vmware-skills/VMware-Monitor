"""vSphere 9.1 host memory tiering (read-only).

VERIFIED pyVmomi paths (VCF 9.1 endpoint spec §D):

    HostSystem.hardware.memoryTieringType  -> "noTiering" | "hardwareTiering" | "softwareTiering"
    HostSystem.hardware.memoryTierInfo[]   -> host.MemoryTierInfo{name, type, size}
    host.MemoryTierInfo.type               -> "DRAM" | "NVMe"
    host.MemoryTierInfo.size               -> bytes (long)

Memory tiering (introduced 8.0U3, continued in 9.x) lets NVMe back a colder memory
tier so a host advertises more usable RAM than its DRAM alone. This reports, per
host, the tiering mode and each tier's byte size, and derives the *uplift ratio*
(total tiered bytes / DRAM bytes) so an operator can see how much of a host's
apparent memory actually rides on NVMe. It is the only path that returns measured
tier byte sizes (the ESXCLI namespace changed between 9.0 and 9.1 and is not a
read-API anyway).

All read-only. Enabling/resizing a tier is a host write owned by vSphere admin
tooling, not this skill.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyVmomi import vim, vmodl
from vmware_policy import paginated, sanitize

from vmware_monitor.ops._collect import _collect

if TYPE_CHECKING:
    from pyVmomi.vim import ServiceInstance

# Tiering modes for which a per-host row is "tiering is active" (NVMe in play).
_ACTIVE_TIERING = frozenset({"hardwareTiering", "softwareTiering"})

_BYTES_PER_GB = 1024**3


def _to_gb(num_bytes: int | None) -> float | None:
    """Bytes → GB rounded to 2 dp; ``None`` (absent property) stays ``None``."""
    if not num_bytes:
        return None if num_bytes is None else 0.0
    return round(num_bytes / _BYTES_PER_GB, 2)


def _tier_rows(tier_info: list | None) -> tuple[list[dict], int, int]:
    """Normalise memoryTierInfo[] to rows plus (dram_bytes, total_bytes).

    Defensive: ``tier_info`` may be missing (older host, property unset) or carry
    entries with absent fields — every access degrades to a safe default instead
    of raising (踩坑 形态 #1: an absent field is empty, never a crash).
    """
    rows: list[dict] = []
    dram_bytes = 0
    total_bytes = 0
    for tier in tier_info or []:
        kind = getattr(tier, "type", None) or "unknown"
        size = getattr(tier, "size", None)
        size_int = int(size) if isinstance(size, (int, float)) else 0
        total_bytes += size_int
        if str(kind).upper() == "DRAM":
            dram_bytes += size_int
        rows.append(
            {
                "name": sanitize(str(getattr(tier, "name", "") or "")),
                "type": sanitize(str(kind)),
                "size_gb": _to_gb(size_int),
            }
        )
    return rows, dram_bytes, total_bytes


def get_memory_tiering(
    si: ServiceInstance,
    host_name: str | None = None,
    limit: int | None = None,
) -> dict:
    """Per-host memory-tiering configuration and NVMe uplift ratio.

    Returns the family list envelope {items, returned, limit, total, truncated,
    hint}; ``total`` is the real host count (every host collected before ``limit``
    is applied). Uses a single PropertyCollector batch for name + tiering type +
    tier list across all hosts (no per-host lazy round-trip — issue #31 class).

    Each row:
        host              host name (readable, not a MoID)
        tiering_type      noTiering | hardwareTiering | softwareTiering | unknown
        tiering_active    True when a non-DRAM tier is actually in use
        dram_gb           DRAM tier size (GB) or None if the host reports none
        nvme_gb           NVMe tier size (GB); 0.0 when tiering is off
        total_tiered_gb   sum of all tier sizes (GB)
        uplift_ratio      total_tiered / dram (e.g. 1.5 = 50% more RAM via NVMe);
                          None when DRAM size is unknown/zero (cannot divide)
        tiers             per-tier breakdown [{name, type, size_gb}]

    A host that predates memory tiering, or has it off, comes back with
    ``tiering_type`` "noTiering" (or "unknown" if the property is unset) and
    ``nvme_gb`` 0.0 — that is a real answer ("checked, none"), not a gap.

    Args:
        si: vSphere ServiceInstance.
        host_name: Filter to a single host by exact name (None = all hosts).
        limit: Max host rows to return (None = all).
    """
    results: list[dict] = []
    paths = ["name", "hardware.memoryTieringType", "hardware.memoryTierInfo"]
    try:
        collected = _collect(si, [vim.HostSystem], paths)
    except vmodl.query.InvalidProperty as exc:
        # Pre-8.0U3 vCenter/ESXi has no hardware.memoryTieringType in its VMODL, so
        # PropertyCollector rejects the path with InvalidProperty. Translate to a
        # teaching error instead of letting _safe_error flatten it to opaque text.
        # (NB: the real class is vmodl.query.InvalidProperty, NOT vmodl.fault.* —
        # the latter does not exist and would itself crash on lookup, 踩坑 #40.)
        bad = getattr(exc, "name", None) or "hardware.memoryTieringType"
        raise ValueError(
            f"Memory tiering requires vCenter/ESXi 8.0U3+ — this target does not "
            f"expose the property '{bad}'. Upgrade the host/vCenter, or omit this "
            f"read on older versions."
        ) from exc
    for _obj, p in collected:
        name = p.get("name", "")
        if host_name and name != host_name:
            continue
        tiering_type = p.get("hardware.memoryTieringType") or "unknown"
        tiers, dram_bytes, total_bytes = _tier_rows(p.get("hardware.memoryTierInfo"))
        nvme_bytes = total_bytes - dram_bytes
        uplift = round(total_bytes / dram_bytes, 3) if dram_bytes else None
        results.append(
            {
                "host": sanitize(name),
                "tiering_type": sanitize(str(tiering_type)),
                "tiering_active": str(tiering_type) in _ACTIVE_TIERING,
                "dram_gb": _to_gb(dram_bytes) if dram_bytes else None,
                "nvme_gb": _to_gb(nvme_bytes),
                "total_tiered_gb": _to_gb(total_bytes),
                "uplift_ratio": uplift,
                "tiers": tiers,
            }
        )
    # Tiered hosts first (most uplift at the top), then by name for stability.
    results.sort(key=lambda r: (-(r["uplift_ratio"] or 0), r["host"]))
    total = len(results)
    if limit is not None:
        results = results[:limit]
    return paginated(results, limit=limit, total=total)
