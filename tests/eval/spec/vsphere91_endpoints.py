"""VCF / vSphere 9.1 verified endpoints & attributes — section D (monitor scope).

The single source of truth for what the vSphere-9.1 read tools in this skill are
allowed to touch. Do not add a path here unless it is verified against an
official source; the whole point (踩坑 #36) is that this list is smaller and more
trustworthy than a model's memory.

Two kinds of surface:

* ``PYVMOMI_MEMORY_TIERING`` — pyVmomi property chains (VERIFIED against the
  installed pyVmomi 9.x type metadata, and against spec §D).
* ``REST_READ_PATHS`` — vSphere Automation REST GET paths (VERIFIED in spec §D
  against the Broadcom vcf-api-specs 9.1.0.0 OpenAPI). Templated segments use
  ``{cluster}`` exactly as the OpenAPI declares them.

``REST_AUTH_PATH`` is the standard vSphere Automation session-bootstrap endpoint.
It is not one of section D's *data* paths, but it is not a guess either: it is the
long-standing, documented ``POST /api/session`` Basic-auth → session-id exchange
that every vSphere Automation REST call is built on. It is listed separately so
the "ops only touches spec paths" test can allow it explicitly rather than by
accident.

Deliberately ABSENT (documented as non-existent in spec §D — never synthesise a
path for these; see ``FORBIDDEN_REST_SUBSTRINGS``):

* Any vCenter "maintenance / patch-in-progress" status endpoint. The
  ``X-VC-Maintenance*`` response headers some search engines suggest are a
  hallucination. Maintenance is observed by *tolerating* a 503, not by querying a
  path.
* A Quick Patch dedicated REST status endpoint (Quick Patch reports through VAMI
  ``/api/appliance/update``, which this read skill does not drive).
"""

from __future__ import annotations

# ── pyVmomi (memory tiering, spec §D — VERIFIED) ─────────────────────────────

# (start_type, chain) — resolved by the family's vim-conformance walker.
PYVMOMI_MEMORY_TIERING: tuple[tuple[str, str], ...] = (
    ("HostSystem", "hardware.memoryTieringType"),
    ("HostSystem", "hardware.memoryTierInfo"),
    ("host.MemoryTierInfo", "name"),
    ("host.MemoryTierInfo", "type"),
    ("host.MemoryTierInfo", "size"),
)

# Enum surfaces this skill reasons about (values, not new endpoints). Present for
# documentation/assertion; the wire type is a plain string in pyVmomi 9.x.
MEMORY_TIERING_TYPES: frozenset[str] = frozenset(
    {"noTiering", "hardwareTiering", "softwareTiering"}
)
MEMORY_TIER_KINDS: frozenset[str] = frozenset({"DRAM", "NVMe"})


# ── vSphere Automation REST (spec §D — VERIFIED GET paths) ───────────────────

# Session bootstrap (documented standard, not a section-D data path — see module
# docstring). Basic-auth POST returning a session id.
REST_AUTH_PATH = "/api/session"

#: Appliance version, used only to explain a 404 from a version-gated call.
#: Not from section D and not from memory: observed returning 200 on a live
#: vCenter 8.0.3 (2026-08-31) with the body
#:
#:     {"version": "8.0.3.00000", "build": "24022515",
#:      "summary": "VMware vCenter Server 8.0 Update 3", ...}
#:
#: A live appliance answering is stronger evidence than a document, and the
#: alternative was to keep telling operators "the running version could not be
#: read" while it sat one GET away. If it ever 404s, the reader degrades to that
#: same honest wording rather than guessing.
REST_VERSION_PATH = "/api/appliance/system/version"

# Read-only GET paths. Templated cluster id segment kept literal as {cluster}.
REST_READ_PATHS: tuple[str, ...] = (
    # vCenter appliance deployment size — NEW in 9.1.
    "/api/vcenter/deployment/size",
    # vLCM (vSphere Lifecycle Manager) cluster software compliance + last apply.
    "/api/esx/settings/clusters/{cluster}/software/compliance",
    "/api/esx/settings/clusters/{cluster}/software/reports/last-apply-result",
)

# Substrings that must never appear in a REST path this skill builds — these name
# the specific hallucinations spec §D calls out. A grep-style guard, so a future
# edit that reintroduces one fails a test instead of a customer's 404.
FORBIDDEN_REST_SUBSTRINGS: tuple[str, ...] = (
    "maintenance",  # no vCenter maintenance/patch-status path exists
    "x-vc-maintenance",  # the header family is a search-AI hallucination
)


def rest_path_is_allowed(path: str) -> bool:
    """True if ``path`` matches a verified template (``{cluster}`` wildcarded).

    Concrete ids (``domain-c123``) substituted into a ``{cluster}`` slot still
    match; anything not derived from a listed template does not.
    """
    import re

    if path == REST_AUTH_PATH:
        return True
    for template in REST_READ_PATHS:
        pattern = "^" + re.escape(template).replace(r"\{cluster\}", r"[^/]+") + "$"
        if re.match(pattern, path):
            return True
    return False
