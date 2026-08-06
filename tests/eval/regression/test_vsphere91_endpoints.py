"""Regression — vSphere 9.1 read tools touch ONLY verified endpoints (踩坑 #36).

A sibling skill once shipped hallucinated REST endpoints, half of which 404'd at
customer sites. These tests pin the guard for this skill's 9.1 surface:

* the pyVmomi memory-tiering chains resolve against the installed pyVmomi, and
  ``ops/memory_tiering.py`` reads no ``hardware.*`` chain that is not in the spec;
* every REST path literal in ``ops/patching.py`` is a verified spec template, and
  none contains a spec-forbidden substring (the hallucinated maintenance path);
* memory-tiering math (uplift, DRAM/NVMe split) and defensive parsing behave;
* the REST tools tolerate a 503 as "busy", never as a crash (踩坑 #37);
* the four new MCP tools register, avoid PEP 604 in their signatures (踩坑 #33),
  and return the envelope/dict shape (never a bare list).
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyVmomi import vim

from tests.eval.spec import vsphere91_endpoints as spec
from vmware_monitor.ops import memory_tiering as mt
from vmware_monitor.ops import patching

_OPS_DIR = Path(mt.__file__).resolve().parent


# ---------------------------------------------------------------------------
# 1. pyVmomi memory-tiering chains are real, and ops uses only spec chains
# ---------------------------------------------------------------------------


def _resolve(start: str) -> object:
    obj = vim
    for part in start.split("."):
        obj = getattr(obj, part)
    return obj


def _props(t: object) -> dict:
    out: dict = {}
    for klass in getattr(t, "__mro__", []):
        for p in vars(klass).get("_propList") or []:
            out.setdefault(p.name, p.type)
    return out


def _chain_resolves(start: str, chain: str) -> bool:
    t = _resolve(start)
    for attr in chain.split("."):
        t = getattr(t, "Item", t)  # unwrap array element type
        props = _props(t)
        if attr not in props:
            return False
        t = props[attr]
    return True


@pytest.mark.parametrize("start,chain", spec.PYVMOMI_MEMORY_TIERING)
def test_memory_tiering_chain_resolves(start: str, chain: str) -> None:
    assert _chain_resolves(start, chain), f"vim.{start} :: {chain} does not resolve"


def test_ops_reads_only_spec_hostsystem_chains() -> None:
    """No hardware.* property chain in memory_tiering.py is off-spec."""
    allowed = {chain for st, chain in spec.PYVMOMI_MEMORY_TIERING if st == "HostSystem"}
    source = (_OPS_DIR / "memory_tiering.py").read_text(encoding="utf-8")
    used = set(re.findall(r"hardware\.[A-Za-z][A-Za-z0-9.]*", source))
    assert used, "expected memory_tiering.py to read hardware.* chains"
    assert used <= allowed, f"off-spec hardware chains: {sorted(used - allowed)}"


# ---------------------------------------------------------------------------
# 2. REST path literals are verified spec templates, nothing hallucinated
# ---------------------------------------------------------------------------


def test_patching_path_constants_match_spec() -> None:
    assert patching.DEPLOYMENT_SIZE_PATH in spec.REST_READ_PATHS
    assert patching._COMPLIANCE_TMPL in spec.REST_READ_PATHS
    assert patching._LAST_APPLY_TMPL in spec.REST_READ_PATHS


def _rest_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/api/")
    ]


def test_every_rest_literal_is_spec_allowed() -> None:
    for literal in _rest_literals(_OPS_DIR / "patching.py"):
        # Resolve a {cluster} template to a concrete id before matching.
        concrete = literal.format(cluster="domain-c1") if "{cluster}" in literal else literal
        assert spec.rest_path_is_allowed(concrete), f"off-spec REST path literal: {literal}"


def test_no_forbidden_substring_in_rest_layer() -> None:
    for fname in ("patching.py",):
        text = (_OPS_DIR / fname).read_text(encoding="utf-8").lower()
        for bad in spec.FORBIDDEN_REST_SUBSTRINGS:
            # Allowed in prose ("no maintenance endpoint"); forbidden inside a path.
            for line in text.splitlines():
                if "/api/" in line:
                    assert bad not in line, f"forbidden '{bad}' in a path line: {line.strip()}"


def test_spec_rejects_a_hallucinated_path() -> None:
    """The allow-list actually rejects the maintenance hallucination it names."""
    assert not spec.rest_path_is_allowed("/api/vcenter/deployment/maintenance")
    assert not spec.rest_path_is_allowed("/api/esx/settings/clusters/domain-c1/maintenance")
    assert spec.rest_path_is_allowed("/api/esx/settings/clusters/domain-c1/software/compliance")


# ---------------------------------------------------------------------------
# 3. memory-tiering math + envelope + defensive parsing
# ---------------------------------------------------------------------------

_ENVELOPE_KEYS = ("items", "returned", "limit", "total", "truncated", "hint")
_GB = 1024**3


def _tier(kind: str, gb: float) -> SimpleNamespace:
    return SimpleNamespace(name=f"{kind}-tier", type=kind, size=int(gb * _GB))


def _host_row(name: str, tiering_type: str, tiers: list) -> tuple:
    return (
        SimpleNamespace(name=name),
        {
            "name": name,
            "hardware.memoryTieringType": tiering_type,
            "hardware.memoryTierInfo": tiers,
        },
    )


def _si() -> SimpleNamespace:
    return SimpleNamespace(RetrieveContent=lambda: None)


def _run(monkeypatch, rows, **kw) -> dict:
    monkeypatch.setattr(mt, "_collect", lambda si, obj_type, paths: rows)
    return mt.get_memory_tiering(_si(), **kw)


def test_uplift_and_split_are_computed(monkeypatch) -> None:
    rows = [_host_row("esx-01", "softwareTiering", [_tier("DRAM", 100), _tier("NVMe", 50)])]
    out = _run(monkeypatch, rows)
    assert set(_ENVELOPE_KEYS) <= out.keys()
    r = out["items"][0]
    assert r["dram_gb"] == 100.0
    assert r["nvme_gb"] == 50.0
    assert r["total_tiered_gb"] == 150.0
    assert r["uplift_ratio"] == 1.5
    assert r["tiering_active"] is True
    assert len(r["tiers"]) == 2


def test_tiering_off_reads_as_a_real_none_not_a_gap(monkeypatch) -> None:
    rows = [_host_row("esx-02", "noTiering", [_tier("DRAM", 64)])]
    out = _run(monkeypatch, rows)
    r = out["items"][0]
    assert r["tiering_type"] == "noTiering"
    assert r["tiering_active"] is False
    assert r["nvme_gb"] == 0.0
    assert r["uplift_ratio"] == 1.0  # total == dram


def test_missing_tier_fields_do_not_crash(monkeypatch) -> None:
    """Absent memoryTierInfo / unset type → empty, never an exception (形态 #1)."""
    rows = [
        _host_row("esx-03", None, None),  # property entirely unset
        (SimpleNamespace(name="esx-04"), {"name": "esx-04"}),  # keys absent
    ]
    out = _run(monkeypatch, rows)
    assert out["total"] == 2
    for r in out["items"]:
        assert r["tiering_type"] == "unknown"
        assert r["uplift_ratio"] is None  # no DRAM → cannot divide
        assert r["nvme_gb"] == 0.0


def test_host_filter_and_limit_and_total(monkeypatch) -> None:
    rows = [
        _host_row("esx-a", "hardwareTiering", [_tier("DRAM", 100), _tier("NVMe", 100)]),
        _host_row("esx-b", "noTiering", [_tier("DRAM", 100)]),
    ]
    filtered = _run(monkeypatch, rows, host_name="esx-a")
    assert filtered["total"] == 1 and filtered["items"][0]["host"] == "esx-a"

    limited = _run(monkeypatch, rows, limit=1)
    assert limited["total"] == 2 and limited["returned"] == 1
    assert limited["truncated"] is True and limited["hint"]
    # Highest uplift first: esx-a (2.0x) precedes esx-b.
    assert limited["items"][0]["host"] == "esx-a"


def test_empty_inventory_is_an_envelope(monkeypatch) -> None:
    out = _run(monkeypatch, [])
    assert out["items"] == [] and out["returned"] == 0 and out["truncated"] is False


# ---------------------------------------------------------------------------
# 4. REST tools tolerate 503 as "busy", parse defensively otherwise
# ---------------------------------------------------------------------------


class _FakeRest:
    def __init__(self, *, payload=None, raise_not_ready=False) -> None:
        self._payload = payload
        self._raise = raise_not_ready

    def get_json(self, path: str):
        if self._raise:
            raise patching.RestNotReadyError("vCenter returned 503 for " + path)
        return self._payload


_TARGET = SimpleNamespace(
    host="vc.example", port=443, verify_ssl=False, username="u", password="p"
)


def _patch_rest(monkeypatch, **kw) -> None:
    monkeypatch.setattr(patching, "VsphereRest", lambda target: _FakeRest(**kw))


@pytest.mark.parametrize(
    "fn,args",
    [
        (patching.get_deployment_size, ()),
        (patching.get_patch_compliance, ("domain-c1",)),
        (patching.get_last_apply_result, ("domain-c1",)),
    ],
)
def test_503_is_tolerated_as_unavailable(monkeypatch, fn, args) -> None:
    _patch_rest(monkeypatch, raise_not_ready=True)
    out = fn(_TARGET, *args)
    assert out["available"] is False
    assert "503" in out["reason"]


def test_compliance_counts_non_compliant_hosts(monkeypatch) -> None:
    payload = {
        "status": "NON_COMPLIANT",
        "scan_time": "2026-08-06T00:00:00Z",
        "hosts": {
            "host-1": {"compliance_status": "COMPLIANT"},
            "host-2": {"compliance_status": "NON_COMPLIANT"},
        },
    }
    _patch_rest(monkeypatch, payload=payload)
    out = patching.get_patch_compliance(_TARGET, "domain-c1")
    assert out["available"] is True
    assert out["status"] == "NON_COMPLIANT"
    assert out["hosts_total"] == 2
    assert out["non_compliant_hosts"] == 1


def test_deployment_size_passes_scalar_fields(monkeypatch) -> None:
    _patch_rest(monkeypatch, payload={"current_size": "SMALL", "hosts": 100, "nested": {"x": 1}})
    out = patching.get_deployment_size(_TARGET)
    assert out["available"] is True
    assert out["fields"]["current_size"] == "SMALL"
    assert out["fields"]["hosts"] == 100
    assert "nested" not in out["fields"]  # only scalars passed through


def test_weird_shape_does_not_crash(monkeypatch) -> None:
    _patch_rest(monkeypatch, payload=["unexpected", "list"])
    out = patching.get_patch_compliance(_TARGET, "domain-c1")
    assert out["available"] is True
    assert out["hosts_total"] is None
    assert out["fields"] == {}


# ---------------------------------------------------------------------------
# 5. REST error translation carries teaching text (leak-free)
# ---------------------------------------------------------------------------


def test_rest_errors_are_teaching_and_are_value_errors() -> None:
    from vmware_monitor.rest import RestAuthError, RestNotFoundError

    # Both subclass ValueError so the MCP _safe_error allowlist passes them through.
    assert issubclass(RestAuthError, ValueError)
    assert issubclass(RestNotFoundError, ValueError)


# ---------------------------------------------------------------------------
# 6. MCP surface: registered, no PEP 604, no bare-list return
# ---------------------------------------------------------------------------

_NEW_TOOLS = {
    "host_memory_tiering",
    "cluster_patch_compliance",
    "cluster_last_apply_result",
    "vcenter_deployment_size",
}


def test_new_tools_register_with_fastmcp() -> None:
    import vmware_monitor.mcp_server.server as srv

    registered = {t.name for t in asyncio.run(srv.mcp.list_tools())}
    missing = sorted(_NEW_TOOLS - registered)
    assert not missing, f"9.1 tools missing from the MCP surface: {missing}"


def test_tools_module_avoids_pep604_union() -> None:
    """FastMCP reflects these signatures; PEP 604 X|None crashes older paths (踩坑 #33).

    Checks the parameter *annotations* via AST (not raw text) so a ``| None`` that
    appears only in prose/docstrings does not false-positive — the annotations are
    the thing FastMCP actually reflects.
    """
    import vmware_monitor.mcp_server.tools_vsphere91 as tools

    src = Path(tools.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for arg in node.args.args + node.args.kwonlyargs:
            ann = arg.annotation
            # PEP 604 union renders as `ast.BinOp` with a `|` (BitOr) operator.
            if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
                offenders.append(f"{node.name}({arg.arg})")
    assert not offenders, f"use Optional[X], not PEP 604 X | None: {offenders}"
    assert "Optional[" in src


def test_new_tools_do_not_return_bare_list() -> None:
    import inspect

    import vmware_monitor.mcp_server.tools_vsphere91 as tools

    for name in _NEW_TOOLS:
        fn = getattr(tools, name)
        ret = str(inspect.signature(fn).return_annotation)
        assert not ret.startswith("list"), f"{name} returns a bare list"
