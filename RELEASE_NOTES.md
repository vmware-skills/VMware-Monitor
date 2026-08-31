## v1.9.2 — the retention setting the coverage note named does not exist

First live run of the backup-window tool from issue #26, against vCenter 8.0.3.
The feature works end to end -- the teaching error for an unknown VM, the bounds
check, and the coverage note that separates "no backups ran" from "vCenter has
no record of this VM at all" all behaved as designed.

One thing did not. When task history had aged out, the note told the operator the
setting responsible was `vpxd.task.maxAge`. That name answers
`vim.fault.InvalidName` on vCenter 8.0.3 -- `vpxd.task` is what the documentation
calls the owning service, and the option is `task.maxAge`. An operator following
the hint got an error instead of a number.

The note now reads the real value rather than quoting the documented default at
someone whose retention was changed: `vCenter keeps 30 days of task history --
task.maxAge`. It falls back to naming the setting alone when the read fails,
because a wrong number is worse than no number.

Verified on live hardware: `task.maxAge = 30`, `task.maxAgeEnabled = True`;
`QueryOptions("vpxd.task.maxAge")` raises.

The read-only gate flagged the new `QueryOptions` call and it was allowlisted
with a stated reason, as designed.

## v1.9.1 — 41 of 50 real events ranked "unknown" — the catalogue was collapsing 2328 descriptions into 443


**Event severity: the catalogue was silently misclassifying, not merely missing.**
On a real VCF 9.1 estate 41 of 50 events came back `severity="unknown"`, every one
an `esx.problem.*` -- including `esx.problem.visorfs.ramdisk.full` ("The ramdisk
'vsantraceFailover' is full."), 41 times on one host.

vCenter published 2328 event descriptions and the catalogue built from them held
443 keys. pyVmomi's VMODL declares `EventDescription.EventDetail.key` as a *type*,
and the thousands of distinct `esx.problem.*` types all arrive as the one class
`vim.event.EventEx`, so writing them into one dict key meant the last description
parsed won and every event under that key inherited its category. That is a
confident wrong rank, which is worse than no rank.

A key whose descriptions disagree is now discarded rather than overwritten, and
every row carries `severity_source` -- `override`, `event`, `catalogue`,
`name_prefix` or `unclassified`. A caller that cannot tell "vCenter called this
info" from "nothing could rank it" cannot tell a quiet estate from a blind one.
An `esx.problem.*` type nothing else ranks is reported as `warning` from the
naming convention, with the source saying so. When descriptions had to be
discarded the envelope carries `catalogue_coverage` naming how many.

**The suite now runs on a cp936 machine.** Round 3 of the VCF 9 field testing ran
on Windows Server 2025 with locale cp936. Across the family four repos' suites --
1687 tests -- never executed at all, dying at collection reading our own UTF-8
sources, and 101 more failed the same way. Most of those were the tests that
verify the destructive-operation guardrails: the guardrails were fine, the tests
that check them could not open a file. On the UTF-8 CI every one of them was
green. A security test that cannot run is not a security test.

Every text read and write here names its encoding now, `tests/` included -- the
previous round fixed only the package, which is why this came back. A gate in
`family_smoke` scans both trees by AST, and the whole family's suites were re-run
under an ASCII locale to confirm: 15 of 15 green, from 1 of 15.

**`--help` no longer dies on a console that cannot encode it.** On any console
whose encoding cannot carry the characters in our own help text, `--help` exited
with a `UnicodeEncodeError` traceback -- unavailable exactly on the machines
where it is most needed. Four repos were affected; the handler is now relaxed in
all fifteen so a glyph degrades instead of killing the command.

**Its environment resolver no longer answers for other skills.**
`set_environment_resolver` wrote one process-global slot and twelve servers
registered into it at import time, so the last one won for all of them --
measured taking a `freeze-production-writes` rule from DENY to ALLOW on another
skill's production target. Registration is keyed by skill now (requires
vmware-policy 1.12.0).

**The `.env` permission check stopped being permanently red on Windows.** It was
POSIX-only, and `chmod 600` there exits 0 without changing any bits -- so
`doctor` printed a failure on every run with a remedy that could not clear it.
Three states now, via `vmware_policy.fsperms`: only a demonstrated exposure
fails, and "this platform cannot answer" says so and offers `icacls`.

**Unknown tool arguments are refused instead of dropped.** The schema declared
`additionalProperties: false` and the runtime accepted them anyway, so a filter
argument whose name a model guessed wrong returned the *unfiltered* result with
nothing to indicate anything had been discarded. Fixed in vmware-policy 1.12.0
and in force here.

Requires vmware-policy 1.12.0.

## v1.9.0 — backup windows from snapshot task history (issue #26)

New read-only tool `vm_backup_snapshot_history` (31 → 32 tools), requested by
@juanpf-ha. Image-level backup products snapshot a VM, copy the frozen disks,
then delete the snapshot; vCenter records both ends as tasks, so the period a
backup held the snapshot open is recoverable without credentials for the backup
server. Four hour statistics — backup active, snapshot present, total window,
removal itself — plus the cycles that never got a matching removal.

It is a **lower bound on the job, not its duration**: work the product does
before the snapshot is taken and after it is removed is invisible to vCenter.
The qualifier ships in the payload's `basis` field rather than only in the docs,
so a caller quoting the figure cannot lose it.

Two ways this tool could have reported calm when it should not, both closed:

* `history_unavailable` is set when the task history could not be read at all
  (no permission, no task manager). Zero cycles then means "could not look",
  not "no backups ran", and the two must not arrive looking identical.
* `coverage_note` is set when vCenter has already expired part of the requested
  window. Task history ages out on `vpxd.task.maxAge` — 30 days by default — so
  a 90-day request can silently describe 30 days. It now says so.

Duplicate VM names raise `AmbiguousVMError` instead of resolving to the first
match: a duration attributed to the wrong same-named VM is indistinguishable
from a correct answer once it leaves the tool.

The issue proposed matching `TaskInfo.Name`, and pyVmomi declares that field as
a **ManagedMethod, not a string** — the same shape that made v1.8.14 flood. The
wire name is read off `name.info.wsdlName`, alongside `descriptionId` and
`description.key`; the localised `description.message` is never matched on. A
test pins all three shapes against the SDK.

Also: `get_events`'s `severity` is now `Literal["critical", "warning", "info"]`,
so the values reach the MCP schema. Previously an unrecognised severity was
ranked by `SEVERITY_ORDER.get(sev, 1)` — a typo filtered at warning level and
returned a plausible, wrong list without raising. A new gate pins the schema's
enum against the implementation's own set.

The four vSphere calls the collector needs (`CreateCollectorForTasks`,
`RewindCollector`, `ReadNextTasks`, `DestroyCollector`) are recorded in the
read-only allowlist with stated reasons and receiver pins.

## v1.8.15 — the event catalogue key is a type, and v1.8.14 flooded because of it

v1.8.14 replaced 23 hardcoded event-class names with vCenter's own published
catalogue. The reasoning held; the type did not. pyVmomi's VMODL declares
`EventDescription.EventDetail.key` as a **type**, not a string, so on a live
vCenter `str(key)` renders "<class 'pyVmomi...VmPoweredOnEvent'>" and matches no
event's class name. Every event fell through to "unknown" — which ranks with
warning so that an unrankable event is not hidden — and a fix meant to stop
*hiding* events flooded instead: 1000 of 1000 unclassified, 998 rows returned,
89% of them login noise. It shipped because the tests mocked that key as a
string, validating where the defect could not appear.

The read-only claim is also now enforced by something that can enforce it. The
old check was 33 string literals shell-grepped at the source; injecting 18 real
destructive pyVmomi calls left it green. It is an AST gate now, with the
allowlist checked against pyVmomi's own type metadata (195 managed types, 1386
methods) and a deny half derived from vSphere's RBAC privilege data. Same
experiment re-run: the old check killed 13 of 29 injections, the new one kills
27, and the two survivors are declared blind spots pinned as tests asserting
they are still blind.

**The `vmware-policy` floor moves to >=1.11.0.** Policy 1.11.0 stops the engine
failing open: on a host whose locale is not UTF-8, reading `rules.yaml` raised a
decode error that was swallowed, and a `freeze-production-writes` rule came back
ALLOW. No new API is used here, so the floor could have stayed — it is raised
because leaving it low means a user resolving 1.10.0 keeps the permissive engine
and the fix never reaches them. One behaviour travels with it: on a host whose
rules file cannot be read, operations move from all-allowed to all-denied.
`VMWARE_POLICY_DISABLED=1` is checked above the rules, so the escape hatch does
not itself depend on them loading.

Also in this release: the suite no longer appends to the operator's real
`~/.vmware/audit.db`. It held over 30,000 rows dominated by tool names nobody
had invoked, including 1,400 entries for a destructive operation that never
happened — an audit trail carrying test fiction cannot answer the question it is
kept for.

## v1.8.14 — the schema an agent reads now carries the descriptions

Parameter descriptions reach the JSON schema for the first time. An MCP client
sees the schema, not the docstring, and this repo's coverage of `description`
and `additionalProperties` was 0% — while nearly every parameter was already
described in an `Args:` block no client ever receives.

Measured on a real VCF 9.1 estate, the gap produced a silent failure with no
error at any stage: a parameter name guessed wrong is discarded and the tool
returns the full unfiltered result; a value guessed wrong (`power_state=
"running"`) returns 0 rows where there were 11.

vmware-policy 1.10.0's `describe_tool_parameters` copies what is already
written, so the docstring is now load-bearing and the two cannot drift apart. It
removes the `Args:` block from the description once copied — both travel in
every `tools/list` response, so leaving it bills the same sentences twice
against the manifest's token budget. `additionalProperties` is closed: an open
schema is room for a model to invent arguments that are then silently
discarded, which is the other half of the same failure.

**The `vmware-policy` floor moves to >=1.10.0.** Older releases have no
`describe_tool_parameters`, and resolving one gives an ImportError at server
start rather than a missing feature.

Also in this release: the read-only claim is now enforced by something that can
enforce it. `test_no_destructive_code.py` was 33 string literals shell-grepped
against the source, under a docstring calling itself "the most critical test".
Injecting 18 real destructive pyVmomi calls left it green — `PowerOnVM_Task(`
was not in the list at all, and the one pattern that did fire is bypassed by a
space before the parenthesis.

It is now an AST gate: every vSphere method the package calls must be on a
reviewed allowlist, with the set of what vSphere offers read out of pyVmomi own
type metadata (195 managed types, 1386 methods) rather than hand-written. Two
second nets are derived from vSphere own RBAC data — an allowlisted method that
returns `vim.Task`, or that vCenter gates outside System.Read/View, needs an
explicit reasoned exemption. The whole package calls nine methods; two are
exempted and argued in place.

Re-running the same experiment: the old check killed 13 of 29 injections, the
new one kills 27. The two survivors are pre-declared blind spots — a method name
composed across two statements, and eval dispatch — pinned as tests asserting
they are *still* blind, so the day one becomes catchable the suite fails rather
than the docstring going quietly stale.

The documentation was corrected to match. "Cannot modify, create, or delete any
resource" became "has no code path that does": there is no runtime interceptor,
and every place claiming enforcement now names the mechanism, states the two
limits, and points at a read-only vCenter account as the backstop that does not
depend on this repo. The Chinese README also said 27 tools where there are 31.

## v1.8.13 — five things that answered when they should have said "I don't know"

Found by running the family against a real VCF 9.1 estate, where four of eight
ESXi hosts were `notResponding`. Every defect here passed the existing suite.

**`infra ntp` reported unreachable hosts as misconfigured.** vCenter has no
`config.dateTimeInfo` for a host it cannot reach, and the HostServiceSystem is
unreadable, so the defaults — no servers, service not running — supplied the
verdict: byte for byte the row a genuinely misconfigured host produces. An
operator would have gone and configured NTP on four machines that may already
have it. Unreachable hosts now report `reachable: false` with null facts and the
`connectionState` in the note, and the envelope carries `hosts_unreachable`,
because a caller filtering for `healthy is False` otherwise learns nothing about
an estate half of which went unasked. The same defect one level down is fixed
with it: a *connected* host whose config could not be read was also reported
misconfigured.

**`health events` printed a green "No events above warning" over five real
warnings.** Severity came from three hardcoded sets of pyVmomi class names — 23
of them, against vSphere's thousand-odd event types — ending in `else: "info"`,
which the default filter then dropped. It now asks vCenter's own published event
catalogue, and identifies `EventEx` by its `eventTypeId`: every one of those
shares the class name "EventEx", so they could never have matched by name even
in principle. What neither can rank is returned as `unknown` and counted, not
demoted to `info` and hidden.

**`doctor` cleared an estate it had not checked.** It authenticated
`default_target` and stopped, so five targets with three wrong passwords came
back "All checks passed" — and the failure that followed told the user to run
the doctor that had just cleared them. Every target is now attempted, and one
bad credential no longer hides the state of the others.

**The CLI, the doctor and the MCP server opened different config files.** With
`VMWARE_MONITOR_CONFIG` set, the server read that file while `load_config()`
read the default — the agent and the human on different vCenters, with the
doctor reporting on the human's. The precedence now lives in one
`resolve_config_path`, and a structural test keeps the callers from drifting
apart again.

**`server.json` never started the MCP server.** It carried only the package
identifier, so a registry client composed `uvx vmware-monitor`, which runs the
CLI and exits. It now declares the `mcp` subcommand and the config variable.

## v1.8.12 — three defects a mock could not have shown

Every one of these was found by pointing the tool at a real ESXi host and a real
vCenter, and every one had passed the full suite.

- **A standalone ESXi reported 0 VMs, 0% usage, and "no issues detected".** The
  host was at 70.7% memory with three VMs running. The VM pass had no fallback
  to the standalone bucket, so every VM outside a cluster was silently skipped;
  and capacity totals were only ever read from cluster properties, so the
  standalone bucket divided by zero and reported 0.0% — indistinguishable from
  an idle host. Any ESXi without vCenter — normal for labs, edge sites and small
  installs — got an all-clear regardless of its actual state. All five existing
  tests built a cluster, so the path could not fail in any of them.
- **Two of three critical alarms were invisible to the health summary.** A live
  vCenter held an expired licence, an expired root password, and a host TPM
  alarm. `get_active_alarms` returned all three; `cluster_health_summary`
  counted one, because it scanned clusters and hosts and nothing above them.
  Alarms raised on the root folder belonged to no bucket. Had the two
  vCenter-level ones been the only alarms present, "is anything on fire?" would
  have answered no. Measured before fixing: alarms propagate upward, so reading
  a parent's list wholesale double-counts — the first attempt did exactly that.
- **The SSL config key in every doc was one the code has never read.**
  SKILL.md, README, README-CN, SECURITY.md, capabilities and setup-guide all
  said to set `disableSslCertValidation: true`; `config.py` reads `verify_ssl`.
  Following our own instruction produced a config the code ignores and a failure
  naming a key the docs never mention.

Two error messages also stopped being unhelpful. `vmodl.fault.NotImplemented` —
what a standalone ESXi answers for vCenter-only services such as the event query
— was met with "run doctor to verify connectivity and credentials", and doctor
passes. And a vCenter still booting made pyVim raise a bare `Exception` that no
clause caught, so twenty lines of library internals reached the terminal.

## v1.8.11 — two wrong numbers: the server's own version, and the advertised tool count

Both defects were invisible to the test suites and both were user-facing.

- **The MCP server reported the SDK's version as its own.** `FastMCP` accepts no
  `version` argument and leaves the lowlevel server's at `None`; with it `None`
  the SDK answers `initialize` with its OWN version. Every skill in the family
  therefore told its client it was mcp 1.29.1 — a number that exists for no
  package here, and one that would change with an SDK bump and no code change of
  ours. Verified end to end rather than by reading: unset the field and a probe
  server reports the installed SDK's version; set it and it reports ours.
- **server.json advertised a stale tool count.** That number is what MCP Registry
  publishes and what the plugin manifest and marketplace copy, so one stale
  integer was wrong in three public places. Corrected against the registered
  tools: 27 advertised, 31 real. README and SKILL.md were already right.

Also new: this repo is installable as a Claude Code plugin
(`/plugin install vmware-monitor@vmware-skills`). The skill and its MCP server arrive in
one step; nothing is duplicated, the manifest points at the existing `skills/`
tree. family_smoke gained three gates — the server's reported version, the plugin
manifest's agreement with pyproject, and the advertised tool count against the
live registration.

## v1.8.10 — vSphere 9.1 read surface: memory tiering + vLCM patch compliance + deployment size (27 → 31 tools)

Four new **read-only** tools for vSphere / VCF 9.1. This skill stays code-level
non-destructive: every one of these only *reports* — none can enable a tier, run a
remediation, or resize the appliance.

### Added — 4 vSphere 9.1 read tools (27 → 31, all read-only)

- **`host_memory_tiering`** — per-host memory tiering (DRAM/NVMe tiers) and the
  derived NVMe *uplift ratio* (total tiered bytes ÷ DRAM), so an operator can see
  how much of a host's apparent RAM actually rides on NVMe. Reads pyVmomi
  `HostSystem.hardware.memoryTieringType` / `hardware.memoryTierInfo` in a single
  PropertyCollector batch (no per-host round-trip — issue #31 class). Requires
  ESXi/vCenter **8.0U3+**; a host with tiering off reads back as `tiering_type`
  "noTiering", `nvme_gb` 0.0 — a real "checked, none", not a gap. Returns the
  family list envelope.
- **`cluster_patch_compliance`** — vLCM software (patch) compliance for one cluster
  over the vSphere Automation REST API. Takes the cluster **MoID** (e.g.
  `domain-c123`), not the display name.
- **`cluster_last_apply_result`** — result (status + end time) of the last vLCM
  remediation on one cluster (REST). Reports the outcome only; never runs an apply.
- **`vcenter_deployment_size`** — vCenter appliance deployment size class (REST,
  **new in 9.1**).

The three REST tools authenticate with the **same per-target credentials** as the
SOAP path and only ever GET. HTTP errors are translated **centrally** in the new
`rest.py` (踩坑 #37): 4xx → an authored, leak-free teaching error (a 404 names how
to get the right cluster MoID); 5xx / timeout → a "not ready" signal the tool turns
into `{"available": false, …}` instead of crashing — because vSphere 9.1 exposes
**no** patch-in-progress status endpoint, a 503 *is* the mid-patch signal, tolerated
as a state rather than raised as a fault.

CLI parity: `memory tiering`, `patch compliance <moid>`, `patch last-apply <moid>`,
`deployment-size` (see `references/cli-reference.md`).

### Honest beta caveats — endpoints verified, field names not yet replayed live

**Read this before trusting a field value.** The verification done today is at the
**path/endpoint level only**, not against a live 9.1 appliance:

- The three REST **paths** are verified in the family VCF 9.1 endpoint spec (§D,
  cross-checked against the Broadcom vcf-api-specs 9.1.0.0 OpenAPI) and pinned by a
  regression test that rejects any off-spec or hallucinated path. The pyVmomi
  memory-tiering **property chains** are verified against the installed pyVmomi 9.x
  type metadata.
- But the **JSON field names** those REST endpoints return have **not** been
  replayed against a live 9.1 vCenter from this skill. Every field is therefore read
  **defensively** (`.get` / `getattr`, absent → omitted, never a crash), and each
  REST result **self-labels** via its `note`:
  `"endpoint verified (spec §D); field parse best-effort pending live 9.1 vCenter"`.
  Where the schema spelling is genuinely uncertain — the per-host compliance status
  key — both plausible names (`compliance_status` / `status`) are tried, and if
  *neither* is present `non_compliant_hosts` comes back **`null` (unknown), never a
  false `0`**.
- The session-bootstrap `POST /api/session` is the long-standing documented
  vSphere Automation Basic-auth → session-id exchange (not a §D *data* path, listed
  separately in the spec so the guard allows it explicitly rather than by accident).
- No `X-VC-Maintenance*` header family and no `…/maintenance` patch-status path are
  used — the spec documents them as non-existent, and a test forbids reintroducing
  either as a path substring.

There is no PromQL / PAIS / collector-pending surface in this skill; the only
"inferred" thing here is the REST field parse described above, and it is labelled in
every result rather than hidden.

### Fixed — Fable5 review (three code fixes + a doc guard)

- **`_target_config` (CLI) resolves config WITHOUT opening a SOAP session** — the
  REST commands are meant to tolerate a mid-patch vCenter (503) and degrade to
  `{available: false}`. The pre-fix path did a pyVmomi `SmartConnect` just to
  resolve the target, so the SOAP login would fail on that same 503 and kill the
  command *before* the degradation could run. Config is now read directly (the same
  thing the MCP path does), no `ServiceInstance` opened.
- **`patch compliance` returns `non_compliant_hosts=None` (unknown), never a false
  `0`, when the host-status schema key is unmatched** (踩坑 形态 #1: an unmatched
  read is "unknown", not "none" — a compliance tool must not silently claim
  all-compliant because it looked for the wrong field).
- **`host_memory_tiering` on a pre-8.0U3 target raises a teaching `ValueError`
  naming the version**, instead of leaking an opaque `vmodl.query.InvalidProperty`.
  (The handler catches the real class `vmodl.query.InvalidProperty`, *not* a
  non-existent `vmodl.fault.*` — that lookup would itself crash, 踩坑 #40.)
- **Doc guard** — a regression test now asserts SKILL.md's advertised MCP tool count
  and the four new tool names track the live `mcp.list_tools()` registry (踩坑 #34).
  Verified this release: live count **31**, SKILL.md header and full tools table both
  read 31 with no phantom entries.

Housekeeping: MCP-server modules are pinned against pyupgrade's `UP007`/`UP045`
so the reflected tool signatures stay `Optional[X]` and never become PEP 604
`X | None` (踩坑 #33); an unused `VmomiJSONEncoder` import was dropped.

## v1.8.9 — moved to vmware-skills org + MCP Registry namespace io.github.vmware-skills/vmware-monitor

Repo transferred from github.com/zw008 to github.com/vmware-skills (redirects preserve old links).
MCP Registry server renamed to `io.github.vmware-skills/*`; the old `io.github.zw008/*` entry is deprecated.
All in-repo links updated. No functional code change on this line beyond the org move.

## v1.8.8 — test suite validated against the shipped Policy; docstring correction

Two developer-facing fixes:
- the `environment` config-field docstring now describes it accurately (an optional
  label a `deny` rule may scope to; the "warn now / refuse next major" gate was
  removed in 1.8.7)
- the regression suite now runs against vmware-policy 1.8.x's current shape (a stale
  test asserting the removed declared-environment enforcement is retired)

No runtime behavior change; this read-only skill is unaffected by the family's CLI
`@guarded` rollout.

## v1.8.7 (2026-07-21) — the skill-level read-only switch is removed; read/write authorization is the vCenter account's job (RBAC)

### Removed: `VMWARE_READ_ONLY` / `read_only:` — give the agent a read-only service account instead

The skill-level read-only switch is gone. It was enforced only on the MCP tool
registry, and any agent with a shell (every SKILL.md grants `allowed-tools: Bash`)
could reach the same change one CLI command away — so it withheld the *tool*, not
the *capability*. It was never a real boundary.

To run an agent read-only, give it a **read-only vCenter/NSX service account
(RBAC)**. Writes are then refused at the platform, un-bypassably, regardless of
surface or shell — the one place read/write control cannot be stepped around. A
config still carrying `read_only: true` is ignored, with a one-time warning that
names the replacement (no silent behavior change).

### Removed: approval tiers and the declared-environment gate (via vmware-policy)

The graduated-autonomy approval tiers (`confirm`/`dual`/`review`) and the "declare
an environment or be refused" baseline are removed — they only ever fired on the
rarest configuration while carrying the family's most complex machinery. Opt-in
`deny` rules and the maintenance window remain, and apply identically wherever a
tool runs.

### Added: offline / air-gapped install docs

The README now covers installing from source without editable mode (for older
`pip`) and building wheels to carry onto an air-gapped host — the modern PEP 517
layout has no `setup.py` by design, which is expected, not a missing file.

This release also carries the accumulated fixes staged since 1.8.5.

## v1.8.6 (unreleased) — `vms` comes back as a deprecated alias

### Fixed — a v1.8.0 rename that broke callers without telling them

v1.8.0 said every `[READ]` list tool now returns the family envelope "instead of
a bare array". For `list_virtual_machines` that was not true: it had returned a
keyed dict, `{total, mode, vms, hint}`, and the conversion renamed `vms` to
`items`.

The distinction matters because it decides whether you find out. Replacing a
bare array with a dict breaks loudly — `result[0]` raises. Renaming a key inside
a dict that was already there breaks quietly: `result.get("vms", [])` kept
returning a value, just always `[]`. **In a monitoring tool an empty VM list
does not read as an error. It reads as "there are no VMs."**

The v1.8.0 notes made this worse rather than surfacing it. A reader who used
`list_virtual_machines`, checked the sentence, saw that their payload was a
keyed dict and not a bare array, would correctly conclude the described change
was not theirs — and ship the bug. The v1.8.0 entry below now carries a
correction saying so explicitly.

**`vms` is restored as a deprecated alias**, pointing at the *same list object*
as `items` rather than a copy, so the two cannot drift. `items` remains the
primary key and the documented one. **`vms` is removed in 2.0** — migrate.

Pinned by `tests/eval/regression/test_deprecated_key_aliases.py`, which asserts
a pre-v1.8.0 caller still sees its VMs, that the alias is the same object
(verified by mutating one and reading the other), and that it survives both
truncation and auto-compact — auto-compact rebuilds the row list, so an alias
captured too early would point at the discarded full-field rows.

Three sibling skills shipped the same shape and are fixed in the same release:
`list_tkc_clusters` and `list_namespace_storage_usage` in vmware-vks, and
`list_vm_tags` in vmware-nsx-security.

---

## v1.8.5 (2026-07-20) — the two fixes v1.8.4 announced now actually work

Four adversarial reviews of v1.8.4 found that both of its headline fixes were
incomplete in ways the release notes did not reflect. This release makes them
real. If you are on 1.8.4, this is the one to take.

### Fixed — a failure that was *returned* was still audited as a success

vmware-policy 1.8.4 added `report_tool_failure()` for tools that catch an
exception and return an error payload instead of raising. **No skill called it.**

Every string-returning tool therefore kept doing exactly what 1.8.4 said it had
stopped doing: writing `status=ok` to `~/.vmware/audit.db` for an operation that
failed, recording an undo token for a change that never happened, and telling the
circuit breaker the call succeeded so repeated failures never tripped it.

The surface this covered is not marginal:

| Skill | What was mis-audited |
|---|---|
| vmware-aiops | 25 of 49 tools, including **every undo-bearing write** — a failed `vm_power_on` left an undo token saying "power it back off" |
| vmware-avi | all 28 tools, including `vs_toggle` and `ako_restart` |
| vmware-storage | all 4 write tools |
| vmware-nsx | the 5 delete tools |

vmware-avi is worth calling out: before 1.8.4 its exceptions propagated and the
audit was correct. 1.8.4 caught them and returned a string, so **that release made
its audit trail worse than it had been.**

Skills whose tools already return dict payloads (vmware-monitor, vmware-vks,
vmware-aria, vmware-log-insight, vmware-harden, vmware-debug, vmware-pilot) were
already detected correctly. They gained a test proving it rather than a redundant
call.

### Fixed — narrowing `OSError` did not close the leak it was meant to close

1.8.4 narrowed the `_safe_error` passthrough because bare `OSError` let TLS and
DNS failures reach the agent with hostnames and certificate subjects in them.
That narrowing had no effect on the error it was written for:

```
ssl.SSLCertVerificationError → ssl.SSLError → OSError, ValueError
```

`ValueError` has been on every allowlist since long before 1.8.4, so a
certificate failure kept passing through — the commonest self-signed-certificate
failure in this family, carrying the hostname it was checked against. An
allowlist structurally cannot express "not this one".

Where `ssl.SSLError` can actually surface — the pyVmomi skills — it is now
reduced *ahead* of the allowlist. In the httpx skills TLS arrives wrapped as
`httpx.ConnectError`, and in vmware-avi as `requests.exceptions.SSLError`, so the
guard cannot fire there; in those skills the leak was the raw exception
interpolated into an already-allowlisted `*ApiError`, and that is now authored
text naming the config target and `verify_ssl` instead of the exception.

The missing-password error — this family's most common first-run failure, whose
entire remedy is the environment variable name it carries — keeps its message
through a narrow `ConfigError(OSError)` rather than the base class. Connection
failures are translated at the connection layer into an authored remedy that
names the target and the setting to change, with the raw detail left on
`__cause__` for the server log.

### Also fixed

- **vmware-vks**: the quickstart documented a password variable the code never
  reads — following `README.md` verbatim produced "Password not found". Five
  places, plus six references to a `doctor` command this CLI has never had, two
  descriptions promising fields the tools do not return, and eight teaching
  messages that `RuntimeError` was masking.
- **vmware-nsx**: an error cited `--route-advertisement`; the flag is `--advertise`.
- **vmware-pilot**: `get_workflow_status` told the model to call `approve` — a
  tool the read-only gate withholds — as the required next step; and a hint
  pointed at a filename that could never appear in that message.
- **vmware-aiops**: `vm_task_status` polling a *failed task* returned
  `{"state": "error", "error": ...}` from a successful read, which the new
  detection read as the call itself failing. The field is now `task_error`.
  **This is a breaking change for anything parsing that payload.**
- Several remedies that were still being cut by the 300-character cap the 1.8.4
  notes claimed to have addressed.

### Known and not fixed

`ConnectionError` remains one type from two sources in several skills — a
skill's own authored message and urllib3's `HTTPSConnectionPool(host=..., port=...)`
share it, and an allowlist cannot separate them. vmware-vks is converted; the
rest need their own domain type and are deferred rather than half-done.

## v1.8.4 (2026-07-20) — errors that teach, and tool descriptions a small model can route from

A capability eval was rolled out across the family and asked two open questions:
when a call fails, is the model told enough to fix it, and can it pick the right
tool from the description alone? Both answers were worse than anyone thought, and
in several places the reason was that the measurement was looking somewhere other
than where the model reads.

### Fixed — teaching messages were being discarded on the way to the agent

`_safe_error` reduces unrecognised exceptions to `"<Class>: operation failed."`
so raw API text, credentials in URLs and internal paths cannot reach an agent.
Its allowlist held only the builtin validation errors — so this skill's **own**
domain exceptions, the ones that exist precisely to carry a corrected next step,
had their messages replaced by their class names.

The effect was invisible from the CLI, which prints those messages in full.

The worst case was shared by nine skills: `config.py` raises exactly one
`OSError`, the missing-password error, whose entire remedy is the environment
variable name it names. An agent hitting an unconfigured target received
`OSError: operation failed.` and had nothing to act on. That is the family's most
common first-run failure, and it landed one release after the documented variable
names were corrected — so the message that would have unstuck the operator was
the one being thrown away.

The rule is now the property it always meant: **every exception this skill raises
on purpose passes through**, and only genuinely unplanned ones are reduced.
`RuntimeError` stays reduced — it is the generic catch-all and in several skills
carries raw upstream text.

### Fixed — error messages now carry the correction

Every message that reported a failure without saying how to recover was
rewritten: it names the offending value, gives an imperative remedy, and names
something concrete to act on — a tool that exists, a real CLI command, a config
file, an environment variable. Recovery becomes an instruction-following problem
rather than an inference one, which is what a weak model can still do.

Three classes of defect surfaced while doing it:

- **Remedies that were never delivered.** `_safe_error` truncates with no
  ellipsis, so a message longer than the cap loses its closing sentence
  silently. One message had been shipping at 396 characters against a 300-char
  cap — its remedy had never once reached an agent. Messages now lead with the
  remedy so a long interpolated value truncates the expendable detail instead.
- **Commands that do not exist.** One skill's error hints named a `doctor`
  subcommand it does not have.
- **Tools that do not exist.** A tool description pointed at two sibling-skill
  tools that had been renamed, and another named a tool that had moved to a
  different skill entirely.

### Improved — tool descriptions state when to use them and what to call next

The description is the API for a small model: an unstated routing rule is a
routing rule that does not exist, and a tool with no stated next hop is one the
model stops at. Descriptions now say when to prefer this tool over a sibling,
what shape comes back, the caveat that bites, and which tool to call after.

**Manifest size did not grow.** Descriptions load into every session, so the
routing clauses were paid for by cutting duplicated reference material —
repeated boilerplate, examples that restated the parameter list, and prose
copies of the pagination contract.

### Note

Every tool and CLI command named anywhere in this release was verified against
the live MCP registry and the live command tree, not against documentation.

## v1.8.3 (2026-07-20) — credentials resolve as a pair; documented env vars now exist

### Added — the per-target username can come from the environment

Adapted from [VMware-AIops#33](https://github.com/vmware-skills/VMware-AIops/pull/33) by
@wright-bench, with thanks. The password already resolved from an env var; the
username did not, so a deployment injecting credentials from a secret store
(systemd `EnvironmentFile`, container secrets, a vault sidecar) could externalise
only half of the pair — and a config-file username paired with an env password
from a different account logs in as nobody.

`<PASSWORD-KEY-PREFIX>_USERNAME` now overrides the `username:` in config.yaml,
using that skill's own password-key convention. Absent, config.yaml still wins;
nothing changes for anyone not setting it.

**Resolved on every access, like the password.** The contributed version read the
username once at load time while the password stayed a property, which
reintroduces exactly the split the override exists to prevent: a sidecar rotating
both halves mid-process moves the password and leaves the username behind. A test
pins that both halves resolve at the same moment.

### Fixed — documented credential variables that the code never read

Rolling the above across the family surfaced a separate defect: four skills
documented a password variable their own loader does not look up. An operator
following the documentation exactly — correct file, correct place, correct-looking
name — got "Password not found".

| Skill | Documented | Actually read |
|---|---|---|
| vmware-nsx | `VMWARE_NSX_<TARGET>_PASSWORD` for target `nsx-prod` → `VMWARE_NSX_PROD_PASSWORD` | `VMWARE_NSX_NSX_PROD_PASSWORD` |
| vmware-nsx-security | `VMWARE_<TARGET>_PASSWORD` | `VMWARE_NSX_SECURITY_<TARGET>_PASSWORD` |
| vmware-aria | `VMWARE_<TARGET>_PASSWORD` | `VMWARE_ARIA_<TARGET>_PASSWORD` |
| vmware-vks | `VMWARE_<TARGET>_PASSWORD` | `VMWARE_VKS_<TARGET>_PASSWORD` |
| vmware-avi | three different forms across three files | `<CONTROLLER>_PASSWORD` |

The prefixes genuinely differ per skill, so nothing could be fixed by
standardising a pattern — each repo's docs were corrected against its own code.
The code was left alone: changing a key would break every existing deployment.

`family_smoke.sh` now compares the credential variables named in each repo's docs
against the ones that repo's code builds, so the two cannot drift apart again.

## v1.8.2 (2026-07-20) — the MCP server moves into the package namespace

### Fixed — co-installing two skills broke all but the last one

Every skill shipped its MCP server as a **top-level `mcp_server` package**. Python
has one top-level namespace, so installing any two of them into one environment let
the second overwrite the first — silently, with no error and no warning.

    uv tool install vmware-aiops   ->  49 tools   (correct)
    uv pip  install vmware-aiops   ->  27 tools   (Monitor's read-only server)

vmware-aiops depends on vmware-monitor, so this was not an edge case: **every pip
install hit it**, and the operator got 27 read-only tools where 49 were expected,
with all 35 write tools missing. Docker images, shared MCP hosts and CI runners that
install more than one skill were affected the same way.

The server now lives at `vmware_<skill>/mcp_server/`, a name only this package can
claim. Introduced 2026-02-26; it survived 70 releases because every test ran against
a single package in its own repo, where the local directory shadows site-packages —
the conflict was invisible by construction.

**Migration.** Console scripts are unchanged: `vmware-<skill>` and
`vmware-<skill>-mcp` work exactly as before, as does `"command": "vmware-<skill>",
"args": ["mcp"]` in an MCP client config. Only a direct `python -m mcp_server`
breaks; use `python -m vmware_<skill>.mcp_server`.

### Added — `references/agent-guardrails.md` in every skill

The operating rules for local and small models (Llama 3.3 70B, Qwen, Mistral via
Goose / Ollama / OpenShift AI) existed in two skills. They now ship in all 13, each
with its own tool counts and failure modes, and are linked from every SKILL.md.

### Fixed — error messages now name a tool that exists

`VM '...' not found. Run list_vms ...` named an internal ops function, not an MCP
tool. A model following that instruction called something that does not exist, on
the one path where it was already recovering from a failure. `family_smoke.sh` now
checks every tool name in every error message against the whole family's live
registry — the capability eval grades an error's *shape*, and shape does not imply
truth.

### Fixed — documentation that pointed at files which do not exist

93 install instructions across Monitor and AIops told operators to copy
`codex-skill/AGENTS.md`, `gemini-extension/GEMINI.md`, `trae-rules/project_rules.md`
or `kimi-skill/SKILL.md`. None of those directories exist in any repo, and CLAUDE.md
forbids recreating them — so every one of those commands failed. Where a platform
reads plain markdown (Codex, Kimi, Trae, Aider, Continue) they now point at the real
`skills/vmware-<skill>/SKILL.md`; Gemini CLI, which needs a manifest none of the
repos ship, was downgraded to the context-file and MCP paths that do work.

`/plugin marketplace add` was removed from both repos and from three setup guides:
the `plugins/` source directory it needs was deleted in March and is on CLAUDE.md's
forbidden list, so that path could not be made to work either.

### Removed — the stale `.agents/` copy

A 24-line subset of the maintained 305-line SKILL.md, missing `summary`,
`investigate`, `attention` and `cluster_health_summary`.

## v1.8.1 (2026-07-19) — read-only mode reaches the surfaces that teach it

v1.8.0 put read-only mode in the code and documented it in the README only.
Every other layer was empty, and each serves a different reader: SKILL.md is what
the agent loads, setup-guide is what an operator reads while configuring, `doctor`
is where they verify it took. The gap had two concrete costs.

An agent read SKILL.md, called a write tool the gate had withheld, and got nothing
back — with no way to learn that the absence was a deliberate lockdown rather than
a fault. It reads as a broken tool, so the model retries or hunts for a workaround.

An operator who set the switch had no way to confirm it. The only signal was a line
in the MCP server's start-up log.

### Added — the feature is now documented where each reader looks

- **SKILL.md** — a short section telling the agent that a missing write tool is a
  lockdown, not a fault: name the blocked operation, do not retry, do not route
  around it.
- **references/setup-guide.md** — the operator's view: how to enable it, the
  precedence chain, and how to verify.
- **references/capabilities.md** — which tools the gate withholds.

### Added — `doctor` reports the read-only state

`vmware-monitor doctor` now shows whether read-only mode is on, **which** of the three
switches decided it, and the value as written. A typo'd value (`ture`) is called
out as a typo rather than reported as a confident ON — it resolves to on, which is
fail-closed but almost never what was meant.

The resolution runs through `vmware_policy.read_only_status()` rather than a local
copy of the precedence chain: a doctor that disagrees with the gate it reports on is
worse than no doctor. Requires `vmware-policy>=1.8.1`.

## v1.8.0 (2026-07-18) — read-only mode, working policy defaults, declared environments

Family release driven by [VMware-AIops#31](https://github.com/vmware-skills/VMware-AIops/issues/31),
where an operator running Llama 3.3 70B (Goose / OpenShift AI, on-prem H100) had to
hand-write 17 prompt guardrails to make tool calling reliable. A prompt is advisory — a
model can ignore it. Every guardrail that could move into the harness has.

### Added
- **Read-only mode.** Set `VMWARE_READ_ONLY=true` (or `VMWARE_<SKILL>_READ_ONLY`, or
  `read_only: true` in config.yaml) and every write tool is removed from the MCP registry
  at start-up. `list_tools()` never offers them, so the model cannot call what it cannot
  see. **Off by default** — nothing changes unless you turn it on. Fail-closed: if the
  mode is requested but cannot be guaranteed, the server refuses to start rather than
  running open.
- **`environment:` on each config target**, declaring which environment it is
  (production / staging / lab). Policy rules scope by this value.

### Added — list results now state whether they are complete

Every `[READ]` list tool returns the family envelope instead of a bare array:

    {"items": [...], "returned": 50, "limit": 50, "total": 213,
     "truncated": true, "hint": "Showing 50 of 213. Raise limit or narrow the query..."}

This closes the reported failure where long responses were summarised as "no data
returned": a bare list gives a model no way to tell a complete answer from page one, so
it guessed. `truncated: false` now positively states completeness — including when
`items` is empty, which means "checked, found none", not "the call failed".

> **Correction (v1.8.6).** The sentence "instead of a bare array" above is true
> of most tools converted family-wide, and false of exactly four. One of them
> is in this repo.
>
> (The four were established by an AST key-loss diff over every returning
> function in all twelve repos, mutation-verified in both directions. The
> total converted count is not restated here because it was never verified to
> the same standard — and a correction is the wrong place for a number
> nobody checked.)
>
> **`list_virtual_machines` (ops: `list_vms`) was never a bare array.** Before
> v1.8.0 it returned a keyed dict — `{total, mode, vms, hint}` — and the
> conversion renamed `vms` to `items`. That is a different kind of break from
> the other 51. A bare-array caller doing `result[0]` gets an immediate
> `KeyError` on a dict and finds out at once; a caller doing
> `result.get("vms", [])` kept running and silently saw zero VMs. In a
> monitoring tool an empty VM list does not read as a failure — it reads as
> "there are no VMs".
>
> If you read this section when v1.8.0 shipped, checked `list_virtual_machines`,
> saw a keyed dict and concluded the change did not apply to you: it did. The
> count in the bullet below is the tell — 19 tools are listed as converted, but
> 20 ops functions return the envelope. `list_vms` is the one missing from the
> count, and it was left out of the prose for the same reason.
>
> **v1.8.6 restores `vms`** as a deprecated alias pointing at the *same list
> object* as `items` (not a copy — copies drift). It is removed in 2.0. Migrate
> to `items`.
>
> The other three, for anyone running more of the family: `list_tkc_clusters`
> (`clusters` → `items`) and `list_namespace_storage_usage` (`pvcs` → `items`,
> `pvc_count` deleted outright) in vmware-vks, and `list_vm_tags`
> (`tags` → `items`) in vmware-nsx-security.

- **19 tool(s) converted** across ops, MCP and CLI. 17 of 19 report a real `total` (the collection is enumerated, then sliced).
  `get_events` and `host_log_scan` deliberately report `total: null` — vCenter's event
  collector and the log tail each apply their own bounds, so the full size is genuinely
  unknown. A null total is the honest answer; inventing one would turn an uncertainty
  into a confident claim.

### Changed — migration, read this
- **Approval tiers now actually run.** They shipped in v1.6.0 but the engine only ever
  read `~/.vmware/rules.yaml`, and a fresh install has no such file — so every deny rule,
  maintenance window and approval tier had been inert on every install that never
  hand-authored one. A packaged baseline now loads when you have written no rules of your
  own. Writes at medium risk and above are stamped with their tier in the audit log;
  irreversible work and guest execution against a target declared `production` require a
  named approver via `VMWARE_AUDIT_APPROVED_BY`.
- **`environment:` will become required for writes.** Today a state-changing operation
  against a target that declares none still runs and logs a warning. **The next major
  release refuses it.** Declare it now and that upgrade is a no-op:

      targets:
        prod-vc01:
          host: vc01.corp.local
          environment: production

  Read-only operations are never affected, in this release or the next. Check what applies
  to your targets before upgrading: `vmware-audit policy --operation vm_delete --env <env>`.

### Fixed
- **Policy glob patterns with a leading wildcard silently matched nothing.** A rule written
  `operations: ["*_delete"]` parsed fine, read correctly, and never fired — only a trailing
  `*` was honoured. Now full glob matching, for operations and environments alike.
- Config-path overrides (`VMWARE_<SKILL>_CONFIG`) are honoured when reading `read_only`
  and `environment`, so a setting in a custom config file is no longer silently ignored.

### Notes
- Requires `vmware-policy>=1.8.0`; publish that package first.
- `vmware-audit policy` reports which rules are in force and where they came from —
  including the case where your rules file exists but failed to parse, which previously
  looked identical to "policy is working".

### Added
- `references/agent-guardrails.md` — the canonical guide to operating the family
  with a local or small model: which guardrails the harness now enforces for you,
  a copy-pasteable system prompt for the rest, and the observed failure modes of
  Llama 3.3 70B against these tools. Adapted with thanks from the reporter's own
  working configuration.
- This skill has no write tools, so the read-only gate withholds nothing — it is
  wired up anyway so "zero write tools" is provable rather than merely documented.

### Fixed — pre-release review (2026-07-19)

- **Two tool descriptions named tools that do not exist.** `vm_investigation_bundle`
  told the model to "get the name from `list_vms`" and described itself as composing
  `get_vm_info + list_snapshots + get_alarms + get_vm_performance`. Those are internal
  ops function names; the callable tools are `list_virtual_machines`, `vm_info`,
  `vm_list_snapshots`, `get_alarms` and `vm_performance`. A teaching error that names an
  uncallable tool teaches the wrong thing.

## v1.7.7 (2026-07-17) — session-probe eviction fix + mcp 1.28.1

Family fix pack — no new tools, no schema changes.

### Fixed
- **Dead cached vCenter sessions were never evicted** (external fork report,
  VMware-AIops PR #32). The liveness probe's handler was
  `except (vmodl.fault.NotAuthenticated, Exception)` — but
  `vmodl.fault.NotAuthenticated` does not exist in pyVmomi (the real class
  lives under `vim.fault`), and except-tuples are evaluated at catch time, so
  the handler raised `AttributeError` instead of evicting. A long-running MCP
  server whose session idled out then permafailed every call until restart,
  surfacing the misleading `AttributeError: NotAuthenticated` instead of the
  real error. The probe now also treats a `None` `currentSession`
  (expired-token shape) as dead. Three regression tests pin the probe shapes
  (raise → evict + reconnect, None → evict + reconnect, live → cache reuse),
  and family_smoke gained a static check banning the nonexistent class.

### Security
- Lockfile bumps `mcp` to **1.28.1**, clearing three GHSA HIGH advisories
  against the MCP Python SDK (WebSocket Host/Origin validation, HTTP
  transport principal verification, experimental task-handler cross-client
  access). stdio-only servers are not directly exposed, and installs resolve
  `mcp` fresh from PyPI — this mainly matters for from-source checkouts.

## v1.7.6 (2026-07-14) — object-centered investigation bundles + cross-vCenter attention

Extends the issue #31 triage work from "is anything on fire?" to "what is happening
around *this* object?" — the object-centered drill-down juanpf-ha asked for.

### Added
- **Object investigation bundles** (read-only; surface 23 → 27). Three new MCP tools —
  `vm_investigation_bundle`, `host_investigation_bundle`, `datastore_investigation_bundle`
  — each answers "what is happening around this object?" in ONE call that *correlates*
  the object with its surrounding infrastructure and recent history: state, the
  host/cluster/datastores around it, snapshots, alarms (across every related scope),
  live performance, and a merged, newest-first **event timeline** stitched from the
  object AND its host/cluster/datastores via per-entity `EventFilterSpec.ByEntity`
  queries. Aggregation happens in the tool — the model explains a high-signal result,
  never raw inventory. All cross-object reads are batched (no per-object round-trips,
  issue #31 class).
- **`cross_vcenter_attention` MCP tool** — "what needs attention now?" across EVERY
  configured vCenter, merged into one globally-ranked `top_issues` list (each tagged
  with its `vcenter`) plus a per-target rollup. Degrades gracefully: an unreachable
  target is listed under `unreachable` with a reason and the rest still aggregate.
- **CLI**: `vmware-monitor investigate vm|host|datastore <name>` and
  `vmware-monitor attention`, each with `--hours` and `--html` / `--html-path`. The
  `--html` snapshot is self-contained and offline (no external assets), with drill-down
  detail in native `<details>` sections — collapse/expand with zero JavaScript.

### Changed
- Extracted the shared offline-HTML palette into `ops/_html_base.py` so the
  cluster-health, investigation, and attention renderers stay visually identical.

### Notes
- Point-in-time snapshots — no trend is invented. Strictly read-only.
## v1.7.5 (2026-07-13) — cluster health triage: one-glance summary, top-N issues, offline HTML snapshots

### Added
- **`cluster_health_summary` MCP tool** (read-only; surface 22 → 23). One aggregated
  read answers "is anything on fire?": three batched PropertyCollector passes roll
  hosts, VM power state, live CPU/memory pressure, and triggered alarms up to the
  owning cluster, assign an opinionated `status` (ok/warn/critical) with
  plain-language `attention` reasons, and flatten the individual anomalies into a
  ranked **`top_issues`** focus list (severity → kind → magnitude; capped by
  `top_n`, default 10, with an honest pre-cap `issues_total`). Alarm names are
  resolved in one extra batched call — no per-alarm round-trips (issue #31 class).
  Born from the issue #31 triage discussion.
- **`vmware-monitor summary` CLI** — `--top N`, `--cluster <substr>`, `--no-vms`,
  and `--html` / `--html-path <file>`: renders the same data as an **offline,
  self-contained HTML snapshot** (all CSS inlined, no external assets, light/dark
  aware, every vSphere-sourced value HTML-escaped). Default location
  `~/vmware-health/cluster-health-<vc>-<YYYYMMDD-HHMMSS>.html` — timestamped
  filenames turn the folder into a browsable point-in-time history.
- **Editable display template** `references/health-summary-template.md` — the
  default columns/thresholds plus a worked "add a column" example; every rendered
  view ends with a friendly customization hint.
- Public render helpers (`render_summary_console`, `write_html_snapshot`) so
  companion skills (vmware-aiops) can present the identical view without
  duplicating code.

## v1.7.4 (2026-07-13) — host-check boundary reads batched (issue #31 tail)

### Fixed
- **NTP / services / certificate checks at scale.** `get_ntp_status`,
  `get_host_services`, and `get_certificate_status` already batched each host's
  name + `configManager` reference in one PropertyCollector call, but then read
  the property they actually needed — `serviceInfo` / `certificateInfo`, which
  lives on the referenced `HostServiceSystem` / `HostCertificateManager` managed
  object that a `HostSystem` container view cannot cross — lazily, i.e. one extra
  SOAP round-trip per host (the issue #31 class, one hop removed). A new
  `ops/_collect._collect_objects` helper fetches the boundary property for every
  reference in a single second `RetrievePropertiesEx`, collapsing the per-host N
  into 2 calls total. Output shape unchanged.

## v1.7.3 (2026-07-03) — host_log_scan MCP tool

### Added
- **`host_log_scan` MCP tool** (read-only). The ESXi syslog error scan — recent
  hostd/vmkernel/vpxa lines matching error/warning patterns (error, fail,
  critical, panic, lost access, timeout, …) — was previously only reachable as a
  scanner/CLI path. It's now surfaced as a proper MCP tool with an optional
  single-host filter and a `lines` window; it returns only the matching lines
  (sanitized, injection-guarded), so output stays small even on large clusters.
  Requested by @juanpf-ha on issue #31. MCP tool surface 21 → 22.

## v1.7.2 (2026-07-02) — read-path scale beyond inventory (issue #31 follow-up)

### Fixed
- **All monitoring read paths at scale.** The v1.7.1 PropertyCollector fix
  covered inventory only. Snapshot aging (heavy per-VM `layoutEx`), VM/host
  performance, active alarms, host hardware/services status, certificate/NTP
  status, datastore/resource-pool capacity, and host-log scan each walked a
  container view reading lazy properties per object — N+1 SOAP round-trips that
  timed out on large vCenters. All now batch via a shared `PropertyCollector`
  helper (`ops/_collect.py`). Output shape unchanged.

## v1.7.1 (2026-07-02) — large-inventory scale fix (PropertyCollector, issue #31)

### Fixed
- **Large-inventory scale (GitHub issue #31 on VMware-AIops).** Read-only
  inventory tools (`list_virtual_machines`, `list_esxi_hosts`,
  `list_all_datastores`, `list_all_clusters`, `list_all_networks`, and
  `find_vm_by_name`) walked a container view and then read pyVmomi *lazy*
  properties per object (`vm.config.hardware.numCPU`, `vm.runtime.host.name`,
  `len(host.vm)`, per-VM `folder_path` walk …) — each a separate SOAP round-trip.
  On large vCenters (thousands of VMs / hundreds of hosts) this meant tens of
  thousands of round-trips, so even `limit=20` queries timed out. All of these
  now fetch every needed property in a single `PropertyCollector.RetrievePropertiesEx`
  call (paged via continuation tokens); `folder_path` is resolved from a batched
  folder map. Output shape is unchanged. Same root cause reported against
  VMware-AIops by juanpf-ha (~8,000-VM / ~340-host environment); fixed here in
  the sibling read-only skill too.

## v1.7.0 (2026-06-27) — guided onboarding + teaching auth errors

### Added
- **`vmware-monitor init` — interactive first-run setup wizard.** Prompts for host /
  username / password and writes `config.yaml` + `.env` for you. The password is
  stored grep-safe (`b64:`, never plaintext on disk) and `.env` is locked to
  0600, then the connection is verified. Replaces the manual "mkdir + cp
  config.example.yaml + edit YAML + chmod 600" dance.
- **10 new read-only monitoring tools (MCP 11 → 21).** Real-time host/VM
  performance (PerfManager: CPU/mem/disk/net), inventory-wide snapshot aging &
  sprawl, ESXi certificate expiry, license usage/expiry, NTP config health,
  datastore thin-provisioning over-commit, resource-pool usage, in-flight tasks,
  and active sessions — all strictly read-only. Honest about limits: no
  fabricated trends, and NTP live-offset is documented as not exposed by the
  SOAP API.

### Changed
- `doctor` now points to `vmware-monitor init` when config/credentials are missing
  (previously suggested a command that did not exist), keeping the manual steps
  as a fallback.
- Authentication and TLS failures now print a teaching message naming the exact
  file and env var to fix (`~/.vmware-monitor/.env` password var, `config.yaml`
  username) plus a `verify_ssl: false` hint for self-signed labs.

## v1.6.1 (2026-06-24)

### Added
- **`.env` passwords are auto-obfuscated to a grep-safe `b64:` form** on first
  load and decoded transparently at runtime — plaintext no longer sits in
  `~/.<skill>/.env` for a casual `grep` to find. Values are read/written through
  python-dotenv's own parser, so the stored secret never drifts from the
  configured one (handles quotes, inline comments, trailing whitespace, and a
  password that literally starts with `b64:`). **Obfuscation, not encryption** —
  for real at-rest secrecy, inject the password from a secret manager instead of
  storing `.env`. New regression suite (10 cases) covers dotenv parity, the
  `b64:`-prefixed edge case, idempotency, and 0600 preservation.

## v1.6.0 (2026-06-22) — family alignment + harness trust architecture

No skill code changes. Aligns to the v1.6.0 family release and automatically picks up the
vmware-policy 1.6.0 governance upgrades (token/runaway budget guard, audit accountability fields,
graduated-autonomy risk tiers) on next install. Read-only skill — no undo tokens applicable.

## v1.5.39 (2026-06-22) — family version alignment

No code changes. Version bump to stay aligned with the v1.5.39 family release
(AIops snapshot-delete async + honest-timeout token-burn fix; Storage datastore-browse timeout fix).

## v1.5.38 (2026-06-12) — release alignment

No functional changes — version bumped to keep the VMware skill family aligned at v1.5.38.

## v1.5.37 (2026-06-12) — backlog: wire up advertised health tools

### Added
- `health sensors`, `health services`, and `inventory networks` CLI commands + read-only MCP tools — these
  were implemented but unreachable while the help text advertised them. Tool count 8 → 11 (all read-only). (#16)

### Fixed
- Docs no longer reference a nonexistent `vmware-monitor init`; setup instructions corrected. (#17)
- Removed two orphan lookup helpers with no callers.

## v1.5.36 (2026-06-12) — MCP error-shape fix, resilient daemon, no false "all clear"

### Fixed
- **Five MCP tools lost their teaching hint to structured-output validation** — list-returning tools
  returned an error dict, which FastMCP rejected as a ToolError; error shapes now match annotations so
  the hint survives.
- **`get_recent_events` no longer reports "no events" on a real failure** — only the standalone-ESXi
  `NotSupported` case maps to empty; auth/network errors are raised.
- **Daemon PID/scan ordering fixed** — a crash in the first scan no longer leaves a stale PID file
  that makes `daemon status` lie; one bad log write no longer discards a scan cycle.
- **Audit-write failure degrades to a stderr warning** instead of killing read commands.
- `doctor` no longer recommends a nonexistent `init` command; CLI commands print teaching errors
  instead of tracebacks; atexit Disconnect guarded.

## v1.5.35 (2026-06-10) — security hardening: safe errors, webhook & PID hygiene

### Fixed
- **MCP tools route errors through `_safe_error()`** (no raw exception text to the agent).
- **Audit** dir 0700 / log 0600; **PID-file** directory created 0700.
- **Webhook** response bodies CR/LF-stripped before logging.

This release aligns the whole family back to a single version (1.5.35); vmware-policy and vmware-pilot return to the shared number after sitting at 1.5.22.

## v1.5.32 (2026-06-08) — Sensor health fix + missing snapshot-list MCP tool added

### Fixed
- Hardware sensors: health from `healthState.key` (green/yellow/red) — the
  previous code reported the sensor category as its status. Sensor type kept
  as a separate column.
- Event severity sets: `DVPortgroupReconfiguredEvent` casing (never matched).

### Added
- `vm_list_snapshots` MCP tool — the CLI had `vm snapshot-list` but the MCP
  server never registered it; docs claimed 8 tools while 7 existed (CLI/MCP
  parity). All 8 tools remain strictly read-only.

### Tests
- vim-attribute conformance regression (shared family defense); stale
  hardcoded repo path in the zero-destructive-code test fixed.

## v1.5.30 (2026-06-07) — Family version alignment

No functional changes. Version-alignment release with the v1.5.30 family
(tool description quality + NSX fixes in sibling skills).

## v1.5.29 (2026-05-29) — `--folder-filter` CLI flag (CLI/MCP Parity)

### New
- `vmware-monitor inventory vms --folder-filter <pattern>` now exposes the case-insensitive folder-path filter that has been available via MCP since v1.5.21. Closes the CLI/MCP exposure asymmetry flagged in CLAUDE.md 踩坑 #34.
- Table title shows `[folder~<pattern>]` marker when filter is active.

### Tests
- 7 new unit tests in `tests/test_folder_filter.py` covering: no-filter pass-through, prefix/case-insensitive/substring matches, no-match empty result, intersection with `power_state`, and CLI `--help` smoke containing `--folder-filter`.

### Documentation
- `references/cli-reference.md` drops the "MCP-only parameter" caveat and documents `--folder-filter` flag.
- `references/capabilities.md` updates the `folder_filter` parameter row to "CLI `--folder-filter`, MCP `folder_filter`".

## v1.5.28 (2026-05-20)

**Fix `subclass() arg 1 must be a class` in goose/old mcp environments** —
v1.5.25–1.5.27 replaced `X | None` with `Optional[X]` but kept
`from __future__ import annotations` at the top of `mcp_server/server.py`.
Under mcp 1.10–1.13 (which Goose and some sandboxes pin), `Tool.from_function`
calls `issubclass(param.annotation, Context)` without resolving forward refs,
so string annotations crash the entire server load. Removed
`from __future__ import annotations` from `mcp_server/server.py` so annotations
are real classes; verified all tools load under mcp 1.10 and 1.14.

Traceback location: `mcp/server/fastmcp/tools/base.py:67`. CLAUDE.md 踩坑 #33
updated. family_smoke.sh Check 4b now installs `mcp==1.10.0` to catch this
regression class.

## v1.5.27 (2026-05-20)

**Loosen Python requirement: now supports Python >= 3.10** — v1.5.25/26 fixed
the PEP 604 root cause in MCP tool signatures (Optional[X] instead of X | None),
but kept `requires-python = ">=3.11"` and a 3.11 hard guard in `mcp_cmd`. Both
relaxed to 3.10 so users on Python 3.10 (e.g. Goose default sandbox, Ubuntu
22.04 system python) can install and run directly without a Python upgrade.

- `pyproject.toml`: `requires-python = ">=3.10"` (was `>=3.11`; VMware-VKS
  was `>=3.12`, now also `>=3.10` for family alignment).
- `<pkg>/cli.py` `mcp_cmd()`: version guard now triggers on `< (3, 10)`.
- Behavior on Python 3.10 matches 3.11/3.12 — the Optional[X] fix from v1.5.25
  is what actually enables this; this release just stops blocking installs.

---

## v1.5.26

**Family-wide MCP server fix — Python 3.10 compatibility (踩坑 #33)** — `vmware-monitor mcp`
crashed at decorator time on Python 3.10 with `subclass() arg 1 must be a class`.
Root cause: `mcp_server/server.py` used PEP 604 `X | None` in tool signatures
plus `from __future__ import annotations`; on Python 3.10 + older mcp/pydantic
combos, `typing.get_type_hints()` evaluates `"str | None"` to a
`types.UnionType` instance, which FastMCP/Pydantic then feeds to `issubclass()`.
Reported by a goose user (qwen3.6:27, Python 3.10).

- `mcp_server/server.py`: all `X | None` → `Optional[X]`; ops layer untouched.
- `<pkg>/cli.py` `mcp_cmd()`: hard guard — exits with installation fix command
  if Python < 3.11 (defense in depth, our actual lower bound).
- `pyproject.toml`: `mcp[cli]>=1.10,<2.0` (was `>=1.0`) so uv doesn't pick
  an ancient version that has the same issubclass bug.

**Tooling — family smoke gains MCP schema-build check** — `scripts/family_smoke.sh`
new Check 4b runs `asyncio.run(mcp.list_tools())` per skill, forcing FastMCP to
build Pydantic models for every declared tool. Supports both module-level `mcp`
and `build_server()` factory patterns.

**Docs — CLAUDE.md gains 踩坑 #33 (PEP 604 / Python 3.10) and #34 (CLI/MCP exposure parity).**

---

## v1.5.24 (2026-05-19)

**Family version alignment** — no code changes in this skill. Bumped together
with VMware-AIops and VMware-VKS, which received a pyVmomi 8.x `ManagedObject`
setattr fix (踩坑 #32). `family_smoke.sh` now enforces the no-setattr rule
across all 9 skills.

## v1.5.23 (2026-05-19)

**VCF 9.0 / 9.1 compatibility declared** — family-wide docs sync.

- **docs:** README + `references/capabilities.md` version-compatibility tables now explicitly list vSphere 9.0 / 9.0U1 / 9.1 as ✅ Full. pyVmomi 8.0.3+ (currently pinned `<10.0`) continues to work against vSphere 9 SOAP API; all 8 read-only tools remain functional.
- **docs:** Added `Official Broadcom References` pointer to [VCF Python SDK](https://developer.broadcom.com/sdks), [REST API portal](https://developer.broadcom.com/xapis), and [CLI tools](https://developer.broadcom.com/tools).
- **align:** Family v1.5.23 — all 9 skills tracking VCF 9.0 / 9.1 compatibility declaration.

## v1.5.22 (2026-05-08)

**Family alignment** — no source changes in this skill.

- **align:** Tracks v1.5.22 family bump driven by Smithery onboarding for vmware-avi / vmware-harden / vmware-pilot.

## v1.5.21 (2026-05-08)

**Feature**: Added `folder_path` field to VM inventory and `folder_filter` parameter for case-insensitive folder-tree search (community contribution from @gavinlinasd, PR #11).

- **feat:** `list_vms()` and `get_vm_info()` now return a `folder_path` field showing each VM's vCenter inventory folder path (e.g. `/Colocation/Colo - ISER`). The path is also added to compact-mode output.
- **feat:** New `folder_filter` parameter on `list_vms()` performs case-insensitive substring matching against `folder_path` — e.g. `folder_filter="Colocation"` returns VMs anywhere under any Colocation folder, including nested subfolders.
- **feat:** `folder_path` added to `_VM_SORT_KEYS` so VMs can be sorted by folder.
- **test:** 5 new unit tests for the `folder_path` helper covering root, single-level, nested, datacenter-name handling, and no-parent edge cases.
- **deps:** Bumped `python-multipart` 0.0.26 → 0.0.27 (transitive, fixes GHSA HIGH DoS via unbounded multipart headers).

## v1.5.20 (2026-05-08)

**Family alignment** — no source changes in this skill.

- **align:** Tracks v1.5.20 family bump driven by vmware-nsx-security and vmware-aria PyPI README `mcp-name:` ownership marker fix required by MCP Registry validation. Other 7 skills already had the marker; this release re-publishes them to keep the family version aligned per CLAUDE.md policy.
- **registry:** All 9 skills now registered on registry.modelcontextprotocol.io as `isLatest=true`.

## v1.5.19 (2026-05-06)

**Family alignment** — no source changes in this skill.

- **build:** Bumped `requires-python` from `>=3.10` to `>=3.11` (regression eval uses `tomllib`).
- **smoke:** Family `scripts/family_smoke.sh` adds Check 3b — recursive `--help` on every subcommand to surface broken lazy imports (yjs review 2026-05-06; 踩坑 #27).
- **align:** Tracks v1.5.19 fixes in vmware-nsx (CRITICAL CLI imports), vmware-vks (ApiClient leak), vmware-harden (Twin indexes + LEFT JOIN), vmware-policy (approval gate + singleton lock).

## v1.5.18 (2026-05-02)

**Family alignment + tooling normalization** — no source changes in this skill.

- **dev:** Migrated `[project.optional-dependencies] dev` → `[dependency-groups] dev` (PEP 735) so `uv sync --group dev` works uniformly across the family. Canonical set: `pytest>=8.0,<10.0`, `pytest-cov`, `ruff`.
- **test:** New `tests/eval/regression/test_release_blockers.py` (5 evals) catches the v1.5.x release blockers — missing `mcp_server` in wheel, AST-detected unimported runtime names, Typer app load failure, module import errors. Run via `pytest tests/eval/regression/`.
- **align:** Family version bump to v1.5.18.

## v1.5.17 (2026-05-01)

**Family alignment** — no source changes in this skill.

This release tracks vmware-pilot v1.5.17 (new `investigate_alert` template + `review_workflow` MCP tool + `parallel_group` step type) and vmware-policy v1.5.17 (L5 pattern matcher integrated into `@vmware_tool`). Both work with the existing skill MCP surface unchanged.

- **align:** Family version bump to v1.5.17.

## v1.5.16 (2026-04-30)

**Enterprise Harness Engineering alignment** — adapted from the Linkloud × addxai framework articles ([part 1](https://mp.weixin.qq.com/s/hz4W7ILHJ1yz_pG0Z1xP-A), [part 2](https://mp.weixin.qq.com/s/F3qYbyB3S8oIqx-Y4BrWNQ)).

- **docs:** New `references/investigation-protocol.md` — causal-chain root cause analysis protocol shared with aiops/aria. Monitor serves as the read-only data source for diagnostic chains.
- **docs:** "Automation Level Reference" section in `references/capabilities.md` — clarifies that monitor is L1/L2 only by code-level design.
- **docs:** Common Workflows in `SKILL.md` enriched with judgment ("alarms tell you what vCenter decided is wrong, events tell you what happened — they diverge").
- **align:** Family version bump to v1.5.16.

## v1.5.15 (2026-04-29)

**UX improvements from real user feedback**

- **feat:** New top-level CLI subcommand `vmware-monitor mcp` starts the MCP server. Single command, single binary on PATH after `uv tool install vmware-monitor` — no more `uvx --from`, no PyPI re-resolve, no TLS-proxy issues.
- **feat:** Default `verify_ssl: true` on new targets (was `false`). Self-signed cert environments must now opt in explicitly with `verify_ssl: false` in `config.yaml`.
- **docs:** README, SKILL.md, setup-guide.md, and all `examples/mcp-configs/*.json` switched to `command: "vmware-monitor"`, `args: ["mcp"]`. uvx form moved to fallback section with TLS-proxy troubleshooting note.
- **compat:** Legacy `vmware-monitor-mcp` console script kept — existing user configs continue to work unchanged.

## v1.5.14 (2026-04-21)

**Bug fixes from code review by @yjs-2026 (follow-up)**

- **fix:** `health.py` — container views in `get_active_alarms`, `get_host_hardware_status`, `get_host_services` now wrapped in try/finally to prevent resource leaks on exception

## v1.5.13 (2026-04-21)

**Bug fixes from code review 2026-04-20**

- **fix:** `log_scanner.py` — `BrowseDiagnosticLog` now probes total line count first, then reads last N lines correctly (was passing line count as start offset)

## v1.5.12 (2026-04-17)

- Align with VMware skill family v1.5.12 (security & bug fixes from code review by @yjs-2026)

## v1.5.11 (2026-04-17)

- Align with VMware skill family v1.5.11 (AVI 22.x fixes from @timwangbc)

## v1.5.10 (2026-04-16)

- Security: bump python-multipart 0.0.22→0.0.26 (DoS via large multipart preamble/epilogue)
- Align with VMware skill family v1.5.10

## v1.5.8 (2026-04-15)

- Align with VMware skill family v1.5.8 (NSX/AVI/Aria/AIops bug fixes)

## v1.5.7 (2026-04-15)

- Align with VMware skill family v1.5.7 (Pilot `__from_step_N__` fix + VKS SSL/timeout fix)

## v1.5.6 (2026-04-15)

- Fix: CRITICAL — `mcp_server` module missing from PyPI wheel (ModuleNotFoundError when running vmware-monitor-mcp). Added hatch packages config to pyproject.toml
- Align with VMware skill family v1.5.6

## v1.5.5 (2026-04-15)

- Align with VMware skill family v1.5.5

## v1.5.4 (2026-04-14)

### Security Updates

- **pytest CVE-2025-71176**: Upgraded pytest 9.0.2 → 9.0.3 (insecure tmpdir handling)
- **Dependencies**: Updated rich version constraint from <15.0 to <16.0 for compatibility
- **Alignment**: Sync with VMware skill family v1.5.4 release

## v1.5.0 (2026-04-12)

### Anthropic Best Practices Integration

- **[READ]/[WRITE] tool prefixes**: All MCP tool descriptions now start with [READ] or [WRITE] to clearly indicate operation type
- **Read/write split counts**: SKILL.md MCP Tools section header shows exact read vs write tool counts
- **Negative routing**: Description frontmatter includes "Do NOT use when..." clause to prevent misrouting
- **Broadcom author attestation**: README.md, README-CN.md, and pyproject.toml include VMware by Broadcom author identity (wei-wz.zhou@broadcom.com) to resolve Snyk E005 brand warnings

### Monitor-specific

- **limit parameters**: list_esxi_hosts, list_all_datastores, list_all_clusters, get_alarms now support limit parameter
- **Workflow failure branches**: Daily health check and VM investigation workflows include error handling steps

## v1.4.9 (2026-04-11)

- Fix: require explicit VMware/vSphere context in skill routing triggers (prevent false triggers on generic "clone", "deploy", "alarms" etc.)
- Fix: clarify vmware-policy compatibility field (Python transitive dep, not a required standalone binary)

## v1.4.8 (2026-04-09)

- Security: bump cryptography 46.0.6→46.0.7 (CVE-2026-39892, buffer overflow)
- Security: bump urllib3 2.3.0→2.6.3 (multiple CVEs) [VMware-VKS]
- Security: bump requests 2.32.5→2.33.0 (medium CVE) [VMware-VKS]

## v1.4.7 (2026-04-08)

- Fix: align openclaw metadata with actual runtime requirements
- Fix: standardize audit log path to ~/.vmware/audit.db across all docs
- Fix: update credential env var docs to correct VMWARE_<TARGET>_PASSWORD convention
- Fix: declare .env config and vmware-policy optional dependency in metadata

# Release Notes

## v1.4.5 — 2026-04-03

- **Security**: bump pygments 2.19.2 → 2.20.0 (fix ReDoS CVE in GUID matching regex)
- **Infrastructure**: add uv.lock for reproducible builds and Dependabot security tracking


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.4.0 — 2026-03-29

### Architecture: Unified Audit & Policy

- **vmware-policy integration**: All MCP tools now wrapped with `@vmware_tool` decorator
- **Unified audit logging**: Operations logged to `~/.vmware/audit.db` (SQLite WAL), replacing per-skill JSON Lines logs
- **Policy enforcement**: `check_allowed()` with rules.yaml, maintenance windows, risk-level gating
- **Sanitize consolidation**: Replaced local `_sanitize()` with shared `vmware_policy.sanitize()`
- **Risk classification**: Each tool tagged with risk_level (low/medium/high) for confirmation gating
- **Agent detection**: Audit logs identify calling agent (Claude/Codex/local)
- **New family members**: vmware-policy (audit/policy infrastructure) + vmware-pilot (workflow orchestration)


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.3.1 — 2026-03-27

### Family expansion: NSX, NSX-Security, Aria

- Added vmware-nsx, vmware-nsx-security, vmware-aria to companion skills routing table
- README updated with complete 7-skill family table
- vmware-aiops is now the family entry point (`vmware-aiops hub status`)


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.3.0 — 2026-03-26

### Docs / Skill optimization

- SKILL.md restructured with progressive disclosure (3-level loading)
- Created `references/` directory: cli-reference.md, capabilities.md, setup-guide.md
- Added trigger phrases to YAML description for better skill auto-loading
- Added Common Workflows section (Daily Health Check, Investigate VM, Continuous Monitoring)
- Added Troubleshooting section (5 common issues)
- README.md and README-CN.md updated with Companion Skills, Workflows, Troubleshooting


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.2.3 (2026-03-22)

### Docs / SKILL.md restructure

- Reorder SKILL.md: tool table and Quick Install first, routing table last — improves Skills.sh/ClawHub page readability.


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.2.1 (2026-03-22)

### Skill Routing / Skill 智能路由推荐

- SKILL.md 新增 **Related Skills — Skill Routing** 路由表：遇到存储相关请求推荐 vmware-storage，遇到 VM 操作需求推荐 vmware-aiops。
- Added **Related Skills** routing table to SKILL.md: recommends vmware-storage for storage tasks, vmware-aiops for VM lifecycle operations.


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.2.0 (2026-03-21)

### mcp-config install — Auto-write Agent Config / 自动写入 Agent 配置

- **`vmware-monitor mcp-config install --agent <name>`** — Directly writes MCP server config into the target agent's config file.
  直接将 MCP server 配置写入目标 Agent 的配置文件，无需手动编辑 JSON/YAML。
  - Supports: claude-code, cursor, goose, continue, vscode, localcowork, mcp-agent / 支持 7 种 Agent
  - JSON merge (non-destructive) + auto-backup on conflict / JSON 合并（非破坏性）+ 冲突时自动备份

### Docker One-Command Launch / Docker 一键启动

- **Dockerfile + docker-compose.yml** — Run MCP server without installing Python or venv.
  无需安装 Python 或 venv，一条命令启动 MCP Server。
  ```bash
  docker compose up -d
  ```

### Cursor Integration Guide / Cursor 集成文档

- **`docs/integrations/cursor.md`** — Full guide for using vmware-monitor as a read-only Cursor MCP server.
  完整的 Cursor 集成指南，包含自动安装、手动配置、8 个只读工具说明和排障指南。

**PyPI**: `uv tool install vmware-monitor==1.2.0`


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.1.0 (2026-03-21)

> **Version unification release / 版本统一发布**
> All platforms (PyPI, GitHub Release, MCP Registry, Skills.sh, ClawHub, Smithery) now share the same version number starting from v1.1.0.
> 所有平台从 v1.1.0 起统一版本号。

### Doctor & MCP Config Generator / 诊断与配置生成

- `vmware-monitor doctor` — 8-check environment diagnostic / 8 项环境诊断
- `vmware-monitor mcp-config generate --agent <name>` — Generate config for 7 local AI agents / 为 7 种本地 AI Agent 生成配置

### Inventory Enhancements / 资源清单增强

- `list_vms` with limit/sort_by/power_state/fields filtering / 支持过滤、排序、字段选择
- Auto-tiered response for large inventories (>50 VMs) / 大规模环境自动精简返回

### Security Hardening / 安全加固

- Prompt injection protection with boundary markers / Prompt 注入防护（边界标记）
- Bandit security scan: 0 issues / Bandit 安全扫描零问题

### Platform & Integration / 平台与集成

- MCP Registry, Skills.sh, ClawHub, Smithery, Glama, mcp.so, Cline Marketplace published
- Local agent config templates for 7 agents (Claude Code, Cursor, Goose, LocalCowork, mcp-agent, Continue, VS Code Copilot)
- Ollama end-to-end setup guide

**PyPI**: `uv tool install vmware-monitor==1.1.0`


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v0.1.2 (2026-03-05)

### Usage Mode Optimization

- **Platform-aware calling priority**: Claude Code and Cursor users get MCP-first experience (structured tool calls, no interactive confirmation needed). Aider, Codex, Gemini CLI, and local models (Ollama) default to CLI mode for lower context overhead and universal compatibility.

- **Install order update**: Skills.sh (`npx skills add`) is now the primary install method; ClawHub as secondary option.

- **MCP load tip**: Added tip for MCP-native tools to check MCP server status (`/mcp`) before use.

**Files updated**: `skills/vmware-monitor/SKILL.md`, `plugins/.../SKILL.md`, `README.md`, `README-CN.md`


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v0.1.1 (2026-03-03)

### Security Hardening: Prompt Injection Protection

- **Boundary markers**: All vSphere-sourced content (event messages, host logs) is now wrapped in explicit boundary markers (`[VSPHERE_EVENT]...[/VSPHERE_EVENT]`, `[VSPHERE_HOST_LOG]...[/VSPHERE_HOST_LOG]`) so downstream LLM agents can distinguish trusted output from untrusted vSphere data.

- **Comprehensive control character sanitization**: Replaced simple null-byte removal with regex-based stripping of all C0/C1 control characters (except `
` and `	`). Prevents prompt injection via embedded control sequences in vSphere event messages.

- **MCP server documentation**: Added comprehensive module docstring to `mcp_server/server.py` with security considerations (all-read-only tool classification, credential handling, transport security) to resolve Socket "Obfuscated File" audit flag.

- **Security section in SKILL.md**: Added explicit Security section covering read-only design, TLS verification, credential handling, webhook data scope, prompt injection protection, and code review guidance.

- **README safety table updates**: Added Prompt Injection Protection and Webhook Data Scope rows to safety features table in both English and Chinese READMEs.

**Files updated**: `vmware_monitor/scanner/log_scanner.py`, `mcp_server/server.py`, `skills/vmware-monitor/SKILL.md`, `plugins/.../SKILL.md`, `README.md`, `README-CN.md`


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v0.1.0 (2026-02-28)

**Initial release — Read-only VMware monitoring with code-level safety.**

Extracted from [VMware-AIops](https://github.com/vmware-skills/VMware-AIops) as an independent repository. Zero destructive code paths — no power, create, delete, reconfigure, snapshot mutate, clone, or migrate operations exist in the codebase.

### Features

- **Inventory**: List VMs, ESXi hosts, datastores, clusters
- **Health & Monitoring**: Active alarms, recent events (50+ event types), hardware sensors, host services
- **VM Info**: Detailed VM information and snapshot listing (read-only)
- **Scheduled Scanning**: APScheduler daemon with configurable intervals, alarm + event + host log scanning
- **Notifications**: JSONL structured logs, Slack/Discord webhook alerts
- **Audit Trail**: All queries logged to `~/.vmware-monitor/audit.log` (JSONL)
- **Multi-target**: Sequential scanning across all configured vCenter/ESXi targets
- **MCP Server**: 7 read-only tools via Model Context Protocol (FastMCP)
- **CLI**: `vmware-monitor` with inventory, health, vm, scan, daemon subcommands

### AI Platform Support

- Claude Code (native plugin + marketplace)
- OpenAI Codex CLI (AGENTS.md)
- Aider / Continue CLI (AGENTS.md)
- Gemini CLI (AGENTS.md)
- Trae IDE (AGENTS.md)
- Kimi Code CLI (AGENTS.md)
- MCP Server (Smithery / Claude Desktop / Cursor)

### Safety

- **Code-level isolation**: No destructive functions or pyVmomi write API calls in the codebase
- **Automated verification**: `test_no_destructive_code.py` checks 40+ destructive patterns
- **PR template**: Read-Only Verification checklist required for all pull requests
- **Credential security**: `.env` with `chmod 600`, config-based connections, zero password exposure

### Configuration

- Independent config directory: `~/.vmware-monitor/`
- YAML-based multi-target configuration
- Environment variable passwords: `VMWARE_{TARGET_NAME}_PASSWORD`
- SSL self-signed certificate support

### Compatibility

| vSphere Version | Support |
|----------------|---------|
| 8.0 / 8.0U1-U3 | Full |
| 7.0 / 7.0U1-U3 | Full |
| 6.7 | Compatible |
| 6.5 | Compatible |

Requires Python >= 3.10, pyVmomi >= 8.0.3.0.