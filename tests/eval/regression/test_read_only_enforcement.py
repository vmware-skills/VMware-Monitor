"""Read-only enforcement: every vSphere method this package calls is allowlisted.

This replaces ``tests/test_no_destructive_code.py``, which shell-grepped 33 string
literals against the source and called itself "the most critical test -- it ensures
code-level safety". It did not. A 2026-08-30 real-hardware round injected 18 genuine
destructive pyVmomi calls into ``vmware_monitor/`` and the suite reported 4 passed,
all green. The one pattern that fired, ``ReconfigVM_Task(``, was defeated by putting
one space before the parenthesis. ``PowerOnVM_Task`` was not in the list at all, and
``PowerOn()`` matched only the exact zero-argument spelling.

A denylist of 33 strings against an API of ~1400 methods can only ever be theatre
(CLAUDE.md 形态 #4: the label promised far more than the content delivered, on a
security claim published in README / SKILL.md / server.json). So this check is
inverted and moved off text:

1.  **Parse, do not grep.** Every ``.py`` in the package is walked with ``ast``. A
    comment or docstring mentioning ``Destroy_Task`` is invisible to it; a real
    ``vm . Destroy_Task ( )`` split across three lines is not.

2.  **Ask pyVmomi what vSphere offers.** The set of vSphere methods is read out of
    pyVmomi's own type metadata (``VmomiSupport._managedDefMap``), not from a list
    written from recollection -- this family has been burned by exactly that
    (CLAUDE.md 踩坑 #36: half of vmware-aria's endpoints were invented and 404'd).

3.  **Allowlist, not denylist.** Every vSphere method the package calls must appear
    in ``ALLOWED_VSPHERE_METHODS`` below with a one-line reason it is a read. A
    method nobody reviewed fails the build, whatever it is called.

4.  **Two independent second nets**, so an allowlist entry added carelessly still
    has to get past vSphere's own opinion:
      - any method whose return type is ``vim.Task`` (mutations are overwhelmingly
        ``*_Task``), and
      - any method whose *required privilege* is not one of vSphere's read
        privileges (``System.Read`` / ``System.View`` / ``System.Anonymous``),
    must additionally carry an explicit, reasoned ``NON_READ_EXEMPTIONS`` entry.
    Two of the nine calls here genuinely need one; both are argued in place.

WHAT THIS CANNOT SEE -- stated plainly, because pretending otherwise would repeat
the original sin:

  * ``getattr(vm, "Destroy_" + "Task")()`` -- a dynamically composed method name is
    not in the syntax tree and no AST pass can recover it. Two narrow shapes of this
    *are* caught (see ``_collect_refs``): calling the result of ``getattr`` inline,
    and ``getattr`` with a string literal naming a vSphere method. Splitting it over
    two statements (``f = getattr(vm, n)`` then ``f()``), ``eval``/``exec``, a method
    reached through a C extension, or a call in a dependency rather than in this
    package, all defeat it.
  * Receiver *types*. ``view.Destroy()`` and ``vm.Destroy()`` are the same attribute
    name to the parser. The exemption for ``Destroy`` therefore also pins the
    receiver *identifier* (``view``), which narrows it but does not type-check it.
  * Anything outside ``vmware_monitor/``.

So this is a gate on the code as written, not a proof of runtime behaviour. The
behavioural half of the read-only claim is evidenced separately, by running the
read tools against a live estate and observing zero vCenter tasks.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest
from pyVmomi import VmomiSupport

PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "vmware_monitor"

# vSphere's own read privileges. A method gated on anything else is, by vCenter's
# RBAC, not a plain read -- even when it only returns data. Deliberately narrow:
# "*.View"-shaped privileges from other namespaces are NOT auto-accepted, they go
# through a human via NON_READ_EXEMPTIONS.
READ_PRIVILEGES = frozenset({"System.Read", "System.View", "System.Anonymous"})


# ---------------------------------------------------------------------------
# The reviewed allowlist. One line per method saying why it is a read.
# Adding a line here is the deliberate human act this whole file exists to force.
# ---------------------------------------------------------------------------

ALLOWED_VSPHERE_METHODS: dict[str, str] = {
    "RetrieveContent": "Fetches the ServiceContent handle; pure lookup, no state touched.",
    "CreateContainerView": (
        "Creates a server-side *view* over existing inventory. Despite the verb it "
        "creates nothing in the estate -- it is the supported way to enumerate."
    ),
    "RetrievePropertiesEx": "PropertyCollector batch property read (first page).",
    "ContinueRetrievePropertiesEx": "PropertyCollector batch property read (subsequent pages).",
    "Destroy": (
        "Releases the transient ContainerView this code created moments earlier. "
        "Sees NON_READ_EXEMPTIONS -- it is a _Task method and needs the receiver pin."
    ),
    "QueryEvents": "Reads rows out of the vCenter event history; the collector is server-side.",
    "CreateCollectorForTasks": (
        "Creates a server-side *task history collector* -- a scoped cursor over tasks that "
        "already ran. Like CreateContainerView, the verb creates a query handle, not an "
        "estate object. pyVmomi reports System.View, a read privilege."
    ),
    "RewindCollector": (
        "Moves this code's own collector cursor back to the start of its page. Sees "
        "NON_READ_EXEMPTIONS -- pyVmomi reports no privilege for it."
    ),
    "ReadNextTasks": (
        "Reads the next page of TaskInfo rows out of our own collector. Sees "
        "NON_READ_EXEMPTIONS -- pyVmomi reports no privilege for it."
    ),
    "DestroyCollector": (
        "Releases the task history collector this code created moments earlier. Sees "
        "NON_READ_EXEMPTIONS -- pyVmomi reports no privilege for it."
    ),
    "QueryOptions": (
        "Reads one vCenter advanced setting by name. Used for `task.maxAge`, so the "
        "coverage note can say how many days of task history this vCenter actually "
        "keeps instead of quoting the documented default at an operator whose "
        "retention was changed."
    ),
    "QueryPerf": "Reads sampled performance counter values for entities we already found.",
    "QueryPerfProviderSummary": (
        "Reads which counters and sampling intervals an entity's provider offers."
    ),
    "BrowseDiagnosticLog": (
        "Reads ESXi log content. Sees NON_READ_EXEMPTIONS -- vCenter gates it on "
        "Global.Diagnostics rather than System.Read."
    ),
}


@dataclass(frozen=True)
class Exemption:
    """A reviewed reason for keeping a call that one of the second nets flagged."""

    reason: str
    receivers: frozenset[str]


# Escape hatches from the two second nets. Each entry is a claim a human made.
NON_READ_EXEMPTIONS: dict[str, Exemption] = {
    "Destroy": Exemption(
        reason=(
            "vmware_monitor/ops/_collect.py releases the ContainerView it just created, "
            "in a finally: block. The object destroyed is our own transient view handle, "
            "not an inventory object -- not destroying it leaks a view on the vCenter. "
            "pyVmomi reports no privilege for this overload, and it returns vim.Task, so "
            "both nets flag it; the receiver pin below is what keeps `vm.Destroy()` out."
        ),
        receivers=frozenset({"view"}),
    ),
    "RewindCollector": Exemption(
        reason=(
            "Repositions the cursor of the TaskHistoryCollector that "
            "vmware_monitor/ops/backup_window.py created one statement earlier, so paging "
            "starts at the beginning of the filtered set rather than wherever vCenter left "
            "it. It moves our cursor, not vCenter state. pyVmomi reports no privilege for "
            "it -- HistoryCollector's own methods carry none -- so the privilege net flags "
            "it; the receiver pin is what keeps the name from being reused elsewhere."
        ),
        receivers=frozenset({"collector"}),
    ),
    "ReadNextTasks": Exemption(
        reason=(
            "Genuinely a read -- it returns vim.TaskInfo[] describing tasks that already "
            "ran -- but pyVmomi reports no privilege for it, so the privilege net flags it. "
            "Access is really gated by the System.View checked on CreateCollectorForTasks, "
            "which is allowlisted above on its own privilege. Kept honest rather than "
            "widening READ_PRIVILEGES to admit None, which would silently admit every "
            "un-annotated method in the SDK."
        ),
        receivers=frozenset({"collector"}),
    ),
    "DestroyCollector": Exemption(
        reason=(
            "Destroys the task history collector vmware_monitor/ops/backup_window.py "
            "created, in a finally: block. vCenter allows a bounded number of collectors "
            "per session (32 by default), so NOT calling this leaks one per invocation "
            "until every later call fails for an unrelated-looking reason. The object "
            "destroyed is our own cursor, never an inventory object -- the receiver pin "
            "is what enforces that reading."
        ),
        receivers=frozenset({"collector"}),
    ),
    "BrowseDiagnosticLog": Exemption(
        reason=(
            "Genuinely a read -- it returns log lines -- but vCenter gates it on "
            "Global.Diagnostics, not System.Read, so the privilege net flags it. Used by "
            "vmware_monitor/scanner/log_scanner.py to tail host logs. Kept honest rather "
            "than widening READ_PRIVILEGES, which would silently admit unrelated methods."
        ),
        receivers=frozenset({"diag_mgr"}),
    ),
}


# ---------------------------------------------------------------------------
# The vSphere method surface, straight out of pyVmomi's type metadata.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MethodFacts:
    """What pyVmomi knows about one attribute-resolvable vSphere method name."""

    returns_task: bool
    privileges: frozenset[str | None]

    @property
    def is_read_privileged(self) -> bool:
        return bool(self.privileges) and self.privileges <= READ_PRIVILEGES


def _vsphere_surface() -> dict[str, MethodFacts]:
    """Map every callable vSphere method name to what pyVmomi says about it.

    ``_managedDefMap`` holds, per managed-object type, a 6-tuple whose last element
    is the method table. Each method row is
    ``(vmodlName, wsdlName, version, params, (flags, wsdlResult, vmodlResult),
    privilege, faults)``.

    pyVmomi exposes a method on the class under its **wsdl** name and, for tasks,
    also under that name with the ``_Task`` suffix stripped (``Destroy_Task`` and
    ``Destroy`` both resolve; the lowerCamel vmodl name does not). Only the
    attribute-resolvable spellings go in, so that lowerCamel vmodl names like
    ``update`` cannot collide with ``dict.update`` and produce phantom findings.
    """
    raw = getattr(VmomiSupport, "_managedDefMap", None)
    if not raw:
        pytest.fail(
            "pyVmomi._managedDefMap is missing or empty -- this check has no idea what "
            "vSphere offers and must not be read as a pass. pyVmomi internals moved; "
            "port _vsphere_surface() before trusting any result from this file."
        )

    surface: dict[str, tuple[bool, set[str | None]]] = {}
    for definition in raw.values():
        for row in definition[5] or ():
            wsdl_name, result, privilege = row[1], row[4], row[5]
            returns_task = result[1] == "vim.Task"
            spellings = {wsdl_name}
            if wsdl_name.endswith("_Task"):
                spellings.add(wsdl_name[: -len("_Task")])
            for name in spellings:
                task, privs = surface.setdefault(name, (False, set()))
                surface[name] = (task or returns_task, privs)
                privs.add(privilege)
    return {n: MethodFacts(task, frozenset(p)) for n, (task, p) in surface.items()}


# ---------------------------------------------------------------------------
# Source analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    """One syntactic reference to a name that pyVmomi says is a vSphere method."""

    name: str
    where: str
    receiver: str
    note: str = ""


def _receiver_of(value: ast.expr) -> str:
    """Best-effort identifier for the object a method was reached through."""
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    if isinstance(value, ast.Call):
        return f"{_receiver_of(value.func)}()"
    return f"<{type(value).__name__}>"


def _collect_refs(filename: str, source: str, surface: dict[str, MethodFacts]) -> list[Ref]:
    """Every reference in *source* to a name pyVmomi knows as a vSphere method.

    Deliberately wider than "``ast.Attribute`` in call position": a bare
    ``op = vm.Destroy_Task`` followed by ``op()`` is the same mutation with the call
    moved one statement away, and costs nothing extra to catch (in this package the
    wider sweep finds zero additional names). Two ``getattr`` shapes are also caught,
    see the module docstring for the ones that are not.
    """
    refs: list[Ref] = []
    tree = ast.parse(source, filename=filename)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in surface:
            refs.append(Ref(node.attr, f"{filename}:{node.lineno}", _receiver_of(node.value)))

        if not isinstance(node, ast.Call):
            continue

        # getattr(obj, "SomeVsphereMethod") -- the name is a literal, so it is visible.
        if isinstance(node.func, ast.Name) and node.func.id in {"getattr", "hasattr"}:
            if len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and arg.value in surface:
                    refs.append(
                        Ref(
                            str(arg.value),
                            f"{filename}:{node.lineno}",
                            _receiver_of(node.args[0]),
                            note=f"reached via {node.func.id}() with a literal name",
                        )
                    )

        # getattr(obj, <anything>)() -- calling the result of getattr. The name may be
        # unrecoverable, so the *shape* is banned outright rather than guessed at.
        inner = node.func
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
            if inner.func.id == "getattr":
                refs.append(
                    Ref(
                        "<dynamic>",
                        f"{filename}:{node.lineno}",
                        _receiver_of(inner.args[0]) if inner.args else "?",
                        note=(
                            "calling the result of getattr() hides the method name from "
                            "static analysis; resolve the attribute explicitly instead"
                        ),
                    )
                )
    return refs


def _violations(refs: Iterable[Ref], surface: dict[str, MethodFacts]) -> list[str]:
    """Apply the allowlist and both second nets. Empty list means the gate passes."""
    problems: list[str] = []
    for ref in refs:
        if ref.name == "<dynamic>":
            problems.append(f"{ref.where}: dynamic method dispatch -- {ref.note}")
            continue

        if ref.name not in ALLOWED_VSPHERE_METHODS:
            problems.append(
                f"{ref.where}: vSphere method {ref.name!r} on {ref.receiver!r} is not in "
                f"ALLOWED_VSPHERE_METHODS. If it really is a read, add it there with the "
                f"reason; otherwise it does not belong in a read-only skill."
                + (f" ({ref.note})" if ref.note else "")
            )
            continue

        facts = surface[ref.name]
        needs_exemption = facts.returns_task or not facts.is_read_privileged
        exemption = NON_READ_EXEMPTIONS.get(ref.name)
        if needs_exemption and exemption is None:
            why = "returns vim.Task" if facts.returns_task else "privilege is not a read privilege"
            problems.append(
                f"{ref.where}: {ref.name!r} is allowlisted but {why} "
                f"(privileges={sorted(str(p) for p in facts.privileges)}). It needs an "
                f"explicit NON_READ_EXEMPTIONS entry stating why it is safe here."
            )
            continue

        if exemption is not None and ref.receiver not in exemption.receivers:
            problems.append(
                f"{ref.where}: {ref.name!r} is exempt only on receivers "
                f"{sorted(exemption.receivers)}, but was called on {ref.receiver!r}. "
                f"The exemption was argued for a specific object, not for the name."
            )
    return problems


def _package_sources() -> list[tuple[str, str]]:
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert files, (
        f"no .py files found under {PACKAGE_ROOT} -- an empty scan is not a pass "
        f"(CLAUDE.md 形态 #1)."
    )
    return [(p.relative_to(PACKAGE_ROOT.parent).as_posix(), p.read_text(encoding="utf-8")) for p in files]


# ---------------------------------------------------------------------------
# Positive controls: prove the machinery is loaded and awake before trusting a
# green result from it. An empty surface or an empty file list would otherwise
# make every assertion below vacuously true.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_vsphere_surface_is_populated_and_recognises_known_mutations() -> None:
    surface = _vsphere_surface()
    assert len(surface) > 1000, f"vSphere surface implausibly small ({len(surface)})"

    for landmark in ("Destroy_Task", "PowerOnVM_Task", "ReconfigVM_Task", "CreateVM_Task"):
        assert landmark in surface, f"{landmark} missing -- pyVmomi metadata changed shape"
        assert surface[landmark].returns_task

    assert surface["RetrieveContent"].is_read_privileged
    assert not surface["Destroy_Task"].is_read_privileged


@pytest.mark.unit
def test_scan_actually_reads_the_package_including_the_mcp_server() -> None:
    names = [name for name, _ in _package_sources()]
    assert len(names) > 20, f"only {len(names)} source files scanned"
    assert any("mcp_server/" in n for n in names), "mcp_server/ was not scanned"
    assert any("ops/" in n for n in names), "ops/ was not scanned"


# ---------------------------------------------------------------------------
# The gate itself.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_every_vsphere_call_in_the_package_is_an_allowlisted_read() -> None:
    surface = _vsphere_surface()
    refs = [r for name, src in _package_sources() for r in _collect_refs(name, src, surface)]
    assert refs, "no vSphere calls found at all -- the analyser is not wired to the package"
    problems = _violations(refs, surface)
    assert not problems, "vmware-monitor is read-only; these calls break that:\n" + "\n".join(
        problems
    )


@pytest.mark.unit
def test_allowlist_and_exemptions_carry_no_stale_entries() -> None:
    """A list nobody re-reads drifts away from the code it describes (形态 #6)."""
    surface = _vsphere_surface()
    used = {
        r.name for _n, src in _package_sources() for r in _collect_refs(_n, src, surface)
    } - {"<dynamic>"}

    stale = sorted(set(ALLOWED_VSPHERE_METHODS) - used)
    assert not stale, f"allowlisted but no longer called -- delete these entries: {stale}"

    stale_exempt = sorted(set(NON_READ_EXEMPTIONS) - used)
    assert not stale_exempt, f"exempted but no longer called -- delete these: {stale_exempt}"

    assert set(NON_READ_EXEMPTIONS) <= set(ALLOWED_VSPHERE_METHODS), (
        "every exemption must also be allowlisted, or the two lists disagree"
    )


@pytest.mark.unit
def test_every_allowlist_entry_states_a_reason() -> None:
    for name, reason in ALLOWED_VSPHERE_METHODS.items():
        assert len(reason.split()) >= 5, f"{name}: reason is too thin to have been reviewed"
    for name, exemption in NON_READ_EXEMPTIONS.items():
        assert len(exemption.reason.split()) >= 15, f"{name}: exemption reason is too thin"
        assert exemption.receivers, f"{name}: exemption must pin at least one receiver"


# ---------------------------------------------------------------------------
# Mutation control. A check that has never been seen to go red is a rumour.
# These are the injections from the 2026-08-30 round, kept executable so the
# check must keep proving it can fail.
# ---------------------------------------------------------------------------

MUTATION_INJECTIONS: dict[str, str] = {
    # -- spellings the old grep list did contain -------------------------------
    "old_list_reconfig": "def f(vm, spec):\n    return vm.ReconfigVM_Task(spec)\n",
    "old_list_create_snapshot": (
        "def f(vm):\n    return vm.CreateSnapshot_Task('s', '', False, True)\n"
    ),
    "old_list_revert_snapshot": "def f(snap):\n    return snap.RevertToSnapshot_Task()\n",
    "old_list_remove_snapshot": "def f(snap):\n    return snap.RemoveSnapshot_Task(True)\n",
    "old_list_migrate": "def f(vm, pool, host):\n    return vm.MigrateVM_Task(pool, host)\n",
    "old_list_relocate": "def f(vm, spec):\n    return vm.RelocateVM_Task(spec)\n",
    "old_list_reset": "def f(vm):\n    return vm.ResetVM_Task()\n",
    "old_list_suspend": "def f(vm):\n    return vm.SuspendVM_Task()\n",
    "old_list_shutdown_guest": "def f(vm):\n    return vm.ShutdownGuest()\n",
    # -- same calls, whitespace the old grep list could not survive ------------
    "space_before_paren": "def f(vm, spec):\n    return vm.ReconfigVM_Task (spec)\n",
    "split_across_lines": (
        "def f(vm, spec):\n    return vm.ReconfigVM_Task(\n        spec,\n    )\n"
    ),
    "newline_before_paren": (
        "def f(vm):\n    return (\n        vm\n        .Destroy_Task()\n    )\n"
    ),
    # -- calls the old list never contained at all -----------------------------
    "power_on": "def f(vm):\n    return vm.PowerOnVM_Task()\n",
    "power_off": "def f(vm):\n    return vm.PowerOffVM_Task()\n",
    "datastore_destroy": "def f(ds):\n    return ds.Destroy_Task()\n",
    "remove_all_snapshots": "def f(vm):\n    return vm.RemoveAllSnapshots_Task(True)\n",
    "guest_file_write": (
        "def f(mgr, vm, auth, spec):\n"
        "    return mgr.InitiateFileTransferToGuest(vm, auth, '/tmp/x', spec, 10, True)\n"
    ),
    "host_update_options": "def f(opt_mgr, values):\n    return opt_mgr.UpdateOptions(values)\n",
    "enter_maintenance_mode": "def f(host):\n    return host.EnterMaintenanceMode_Task(0)\n",
    "unregister_vm": "def f(vm):\n    return vm.UnregisterVM()\n",
    "delete_datastore_file": (
        "def f(fm, dc):\n    return fm.DeleteDatastoreFile_Task('[ds] x.vmdk', dc)\n"
    ),
    "destroy_on_wrong_receiver": "def f(vm):\n    return vm.Destroy()\n",
    # -- the method reference without the call, one statement away -------------
    "bound_method_stashed": "def f(vm):\n    op = vm.Destroy_Task\n    return op()\n",
    # -- dynamic dispatch: the literal form is caught, by name -----------------
    "getattr_literal": "def f(vm):\n    return getattr(vm, 'Destroy_Task')()\n",
    # -- dynamic dispatch: name unrecoverable, caught only by shape ------------
    "getattr_concatenated": "def f(vm):\n    return getattr(vm, 'Destroy_' + 'Task')()\n",
}

# Honest limits. Each of these IS a destructive call this check does not catch.
# They are asserted to survive, so that the day one becomes catchable the test
# fails and the docstring gets corrected rather than quietly going stale.
KNOWN_BLIND_SPOTS: dict[str, str] = {
    "two_step_dynamic": "def f(vm, n):\n    fn = getattr(vm, n)\n    return fn()\n",
    "eval_dispatch": "def f(vm):\n    return eval('vm.Destroy_Task')()\n",
}


@pytest.mark.unit
@pytest.mark.parametrize("case", sorted(MUTATION_INJECTIONS))
def test_injected_mutation_is_caught(case: str) -> None:
    surface = _vsphere_surface()
    refs = _collect_refs(f"injected/{case}.py", MUTATION_INJECTIONS[case], surface)
    assert _violations(refs, surface), f"injection {case!r} slipped through the gate"


@pytest.mark.unit
@pytest.mark.parametrize("case", sorted(KNOWN_BLIND_SPOTS))
def test_known_blind_spot_is_still_blind(case: str) -> None:
    """Documents a real gap. If this fails, the gate improved -- update the docstring."""
    surface = _vsphere_surface()
    refs = _collect_refs(f"blind/{case}.py", KNOWN_BLIND_SPOTS[case], surface)
    assert not _violations(refs, surface), (
        f"{case!r} is now caught -- good. Remove it from KNOWN_BLIND_SPOTS and update "
        f"the module docstring, which currently claims this shape is invisible."
    )


# ---------------------------------------------------------------------------
# Negative controls. An allowlist that rejects everything would satisfy every
# injection test above while breaking the build, and a check that fires on the
# word "Destroy_Task" in a comment is the text matching we just removed.
# ---------------------------------------------------------------------------

LEGITIMATE_READS = """
def collect(si, obj_type, paths):
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, obj_type, True)
    try:
        pc = content.propertyCollector
        batch = pc.RetrievePropertiesEx([], None)
        while batch is not None:
            token = getattr(batch, "token", None)
            if not token:
                break
            batch = pc.ContinueRetrievePropertiesEx(token)
    finally:
        view.Destroy()


def logs(diag_mgr, key):
    return diag_mgr.BrowseDiagnosticLog(key=key, start=1)
"""

PROSE_MENTIONING_MUTATIONS = '''
"""This module never calls Destroy_Task, PowerOnVM_Task or ReconfigVM_Task(.

vm.Clone( and CreateSnapshot_Task( appear here only as prose, which is exactly
what the previous grep-based check could not tell apart from real code.
"""
# ReconfigVM_Task(spec) -- commented out on purpose, must not fail the gate.
MESSAGE = "refusing to call ReconfigVM_Task on a read-only target"
'''


@pytest.mark.unit
def test_legitimate_read_only_code_passes() -> None:
    surface = _vsphere_surface()
    refs = _collect_refs("control/reads.py", LEGITIMATE_READS, surface)
    assert refs, "control sample produced no refs -- the analyser is not looking"
    assert not _violations(refs, surface), "the gate rejects known-good read-only code"


@pytest.mark.unit
def test_comments_docstrings_and_strings_do_not_trip_the_gate() -> None:
    surface = _vsphere_surface()
    refs = _collect_refs("control/prose.py", PROSE_MENTIONING_MUTATIONS, surface)
    assert not _violations(refs, surface), (
        "prose mentioning a mutation was treated as a call -- text matching has "
        "crept back in"
    )
