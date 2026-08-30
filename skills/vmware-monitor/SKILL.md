---
name: vmware-monitor
description: >
  Use this skill for safe, read-only queries of VMware infrastructure — no destructive operations exist in the codebase; a test enforces it.
  Directly handles: a one-glance cross-cluster health summary, object-centered VM/host/datastore investigation drill-downs (correlating surrounding infrastructure + recent events), a cross-vCenter "what needs attention now?" rollup, list VMs/hosts/datastores/clusters, active alarms, recent events, VM details.
  Always use vmware-monitor when the user asks to "list VMs", "check vSphere alarms", "show host status", "is anything on fire", "what needs attention now", "what is happening around this VM/host/datastore", "investigate this VM" — or needs read-only VMware info before making changes.
  Do NOT use for any write operations — this skill is read-only and has no code path that creates, modifies, or deletes a resource.
  For VM modifications use vmware-aiops, for networking use vmware-nsx, for metrics/capacity use vmware-aria. For load balancing/AVI/AKO use vmware-avi.
installer:
  kind: uv
  package: vmware-monitor
allowed-tools:
  - Bash
metadata: {"openclaw":{"requires":{"env":["VMWARE_MONITOR_CONFIG"],"bins":["vmware-monitor"],"config":["~/.vmware-monitor/config.yaml","~/.vmware-monitor/.env"]},"optional":{"env":["VMWARE_TARGET_PASSWORD","VMWARE_<TARGET>_USERNAME","SLACK_WEBHOOK_URL","DISCORD_WEBHOOK_URL","VMWARE_AUDIT_APPROVED_BY"],"bins":["vmware-policy"]},"primaryEnv":"VMWARE_MONITOR_CONFIG","homepage":"https://github.com/vmware-skills/VMware-Monitor","emoji":"📊","os":["macos","linux"]}}
compatibility: >
  vmware-policy auto-installed as Python dependency (provides @vmware_tool decorator and audit logging). All operations audited to ~/.vmware/audit.db.
  Credentials: Each vCenter/ESXi target requires a per-target password env var in ~/.vmware-monitor/.env following the pattern VMWARE_<TARGET_NAME_UPPER>_PASSWORD (e.g., target "vcenter-prod" → VMWARE_VCENTER_PROD_PASSWORD). SLACK_WEBHOOK_URL and DISCORD_WEBHOOK_URL are optional — disabled by default, user-configured only, used solely by the opt-in daemon scanner. Daemon: the background scanner (vmware-monitor daemon start) is user-initiated only, never auto-started. Webhook payloads contain only aggregated alert metadata (alarm counts, event types) — no credentials, IPs, or PII.
---

# VMware Monitor (Read-Only)

> **Disclaimer**: This is a community-maintained open-source project and is **not affiliated with, endorsed by, or sponsored by VMware, Inc. or Broadcom Inc.** "VMware" and "vSphere" are trademarks of Broadcom. Source code is publicly auditable at [github.com/vmware-skills/VMware-Monitor](https://github.com/vmware-skills/VMware-Monitor) under the MIT license.

Read-only VMware vCenter/ESXi monitoring — 31 MCP tools, zero destructive code.

> **Read-only by construction**: This skill contains NO power, create, delete, snapshot, or modify operations. Not disabled — they don't exist in the codebase. Enforced by [`tests/eval/regression/test_read_only_enforcement.py`](../../tests/eval/regression/test_read_only_enforcement.py): it parses every source file and requires each vSphere method called to be on a reviewed allowlist, checked against pyVmomi's own type metadata. That gate reads the code as written — it cannot see a method name composed at runtime, and no CI runs it. For a guarantee independent of this repo, connect with a read-only vCenter account.
> **Companion skills**: [vmware-aiops](https://github.com/vmware-skills/VMware-AIops) (VM lifecycle), [vmware-storage](https://github.com/vmware-skills/VMware-Storage) (iSCSI/vSAN), [vmware-vks](https://github.com/vmware-skills/VMware-VKS) (Tanzu Kubernetes), [vmware-nsx](https://github.com/vmware-skills/VMware-NSX) (NSX networking), [vmware-nsx-security](https://github.com/vmware-skills/VMware-NSX-Security) (DFW/firewall), [vmware-aria](https://github.com/vmware-skills/VMware-Aria) (metrics/alerts/capacity), [vmware-avi](https://github.com/vmware-skills/VMware-AVI) (AVI/ALB/AKO), [vmware-harden](https://github.com/vmware-skills/VMware-Harden) (compliance baselines).
> | [vmware-pilot](../vmware-pilot/SKILL.md) (workflow orchestration) | [vmware-policy](../vmware-policy/SKILL.md) (audit/policy)

## What This Skill Does

All 31 tools are **read-only**.

| Category | Capabilities |
|----------|-------------|
| **Cluster Triage** | One-glance `cluster_health_summary` — cross-cluster Problems/Capacity/Health rollup with an opinionated status; customizable view |
| **Object Investigation** | "What is happening around this VM / host / datastore?" — one correlated drill-down bundle per object, plus `cross_vcenter_attention` — one ranked "what needs attention now?" list across every configured vCenter |
| **Inventory** | List VMs, ESXi hosts, datastores, clusters, networks |
| **Health** | Active alarms, recent events (filter by severity/time), hardware sensors, host services |
| **Performance** | Real-time host & VM CPU/memory/disk/network utilisation (PerfManager) |
| **Capacity** | Datastore thin-provisioning over-commit, resource-pool reservation/usage |
| **Infra Health** | ESXi certificate expiry, license usage/expiry, NTP configuration health |
| **Snapshots** | Inventory-wide snapshot aging & sprawl (flag old snapshots) |
| **Activity** | In-flight tasks, active login sessions |
| **VM Details** | CPU, memory, disks, NICs, snapshots, guest OS, IP |
| **Scanning** | Scheduled alarm/log scanning with Slack/Discord webhooks |
| **vSphere 9.1** | Host memory tiering (DRAM/NVMe uplift), vLCM cluster patch compliance & last-apply result, vCenter deployment size |

## Quick Install

```bash
uv tool install vmware-monitor
vmware-monitor doctor
```

## When to Use This Skill

- List or search VMs, hosts, datastores, clusters
- Check active alarms or recent events
- Get detailed info about a specific VM
- Set up scheduled monitoring with webhook alerts
- Any read-only VMware query where safety is paramount

### Alarm/Event Output: `suggested_actions` Field

`get_alarms` and `get_events` results include a `suggested_actions` list. Each
item is a ready-to-use hint naming the correct companion skill and tool call
(e.g. `"vmware-aiops: acknowledge_vcenter_alarm(entity_name=..., alarm_name=...)"`),
so agents — especially smaller local models — can follow them directly without
reasoning about skill routing. Example payload: `references/capabilities.md`.

**Use companion skills for**:
- Power on/off, deploy, clone, migrate --> `vmware-aiops`
- iSCSI, vSAN, datastore management --> `vmware-storage`
- Tanzu Kubernetes clusters --> `vmware-vks`
- Load balancing, AVI/ALB, AKO, Ingress --> `vmware-avi`

## Related Skills — Skill Routing

| User Intent | Recommended Skill |
|-------------|------------------|
| Read-only vSphere monitoring, zero risk | **vmware-monitor** ← this skill |
| Storage: iSCSI, vSAN, datastores | **vmware-storage** |
| VM lifecycle, deployment, guest ops | **vmware-aiops** |
| Tanzu Kubernetes (vSphere 8.x+) | **vmware-vks** |
| NSX networking: segments, gateways, NAT | **vmware-nsx** |
| NSX security: DFW rules, security groups | **vmware-nsx-security** |
| Aria Ops: metrics, alerts, capacity planning | **vmware-aria** |
| Multi-step workflows with approval | **vmware-pilot** |
| Compliance baselines (CIS / 等保 / PCI-DSS), drift detection, LLM remediation advisor | **vmware-harden** (`uv tool install vmware-harden`) |
| Load balancer, AVI, ALB, AKO, Ingress | **vmware-avi** (`uv tool install vmware-avi`) |
| Audit log query | **vmware-policy** (`vmware-audit` CLI) |

## Common Workflows

> **Diagnostic investigations**: Before running any "why is X failing / down / abnormal" workflow, follow [`references/investigation-protocol.md`](references/investigation-protocol.md). It enforces the four root-cause completeness criteria (falsifiability / sufficiency / necessity / mechanism) and the up-to-three-rounds deepening loop. Since vmware-monitor is read-only, it serves as the data source — actuation belongs to companion skills like vmware-aiops.

### Cluster Health Check ("is anything on fire?" / "what's wrong right now?")

**Judgment**: this is the 5-second triage glance, not an Aria replacement. One call rolls every cluster's hosts, VM power, live CPU/memory and alarms up, flattens the individual anomalies into a ranked **top-N focus list** (`top_issues`), and gives each cluster an opinionated `status`. On a big fleet, lead with the focus list — scanning per-cluster rows is too slow.

1. One glance --> `vmware-monitor summary` (MCP: `cluster_health_summary`). Read `top_issues` first (worst first, each with a drill-down `next step`); the per-cluster table is context. `issues_total` shows how many anomalies existed before the top-N cap
2. Tighten or widen the focus --> `--top 5` for the 5 most urgent, `--top 20` for more, `--top 0` to hide the list and just see the table
3. Drill into what the list points at --> e.g. a `host_down` row → `inventory hosts`; an `alarm` row → `get_alarms`; a `capacity` row → `perf hosts` / `capacity datastores`; scope with `--cluster prod-a`
4. Reshape the view on request --> the output ends with a friendly hint; the operator can say "add datastore free space", "drop the DRS column", "only show clusters needing attention", or "save this as an HTML page". Default layout, columns, and thresholds live in [`references/health-summary-template.md`](references/health-summary-template.md) and are meant to be edited
5. Save an offline snapshot --> `vmware-monitor summary --html` writes a self-contained HTML file (no external assets, nothing uploaded) to `~/vmware-health/cluster-health-<vc>-<timestamp>.html`; `--html-path <file>` for an explicit path. The timestamped filename means a folder of them becomes a browsable point-in-time history. It is a snapshot, not a live page — re-run to refresh
6. **On a very large fleet** --> add `--no-vms` to skip the VM rollup pass when you only need host/alarm/capacity signals
7. **If it returns zero clusters** --> the target may be a standalone ESXi host (no clusters); use `inventory hosts` + `health alarms` instead

### Daily Health Check

**Judgment**: alarms tell you what vCenter has decided is wrong, events tell you what happened. They diverge — an event burst with no alarms often signals a metric threshold miscalibration, not "everything is fine." Read both.

1. Check alarms --> `vmware-monitor health alarms --target prod-vcenter` — focus on Red severity AND alarms older than 1 hour (transient ones self-clear)
2. Review recent events --> `vmware-monitor health events --hours 24 --severity warning` — look for repeated events from the same entity (a single event is noise; 50 events in an hour is a pattern)
3. List hosts --> `vmware-monitor inventory hosts` — flag hosts disconnected, in maintenance mode unexpectedly, or memory > 90%
4. **If connection fails** --> run `vmware-monitor doctor` to diagnose config/network issues

### Object-Centered Investigation ("what is happening around this VM / host / datastore?")

**Judgment**: this is the drill-down the operator wants after triage points at a problem — one call *correlates* the object with its surrounding infrastructure and recent history, so you explain the aggregated result in operational language instead of stitching five tools yourself. The tool aggregates; you never dump raw inventory into the conversation.

Offer the levels progressively — do **not** ask for details the environment already fixes:
1. **Start at the top** --> `cross_vcenter_attention` (CLI: `vmware-monitor attention`) for "what needs attention now?" across every vCenter. If only one vCenter is configured, skip straight to its `cluster_health_summary` — no need to ask which target
2. **Offer to drill into an object** the top-issues list points at. Ask *which level* only when it is genuinely ambiguous:
   - a VM --> `vm_investigation_bundle` (CLI: `vmware-monitor investigate vm <name>`) → VM state, recent events, snapshots, alarms & recent changes, the host it runs on, the cluster context, the datastores backing it, performance signals, and a correlated event timeline
   - a host --> `host_investigation_bundle` (CLI: `investigate host <name>`) → host state, cluster context, the VMs it runs, mounted datastores, alarms, performance, correlated timeline
   - a datastore --> `datastore_investigation_bundle` (CLI: `investigate datastore <name>`) → capacity/free, mounting hosts, VMs it backs, alarms, correlated timeline
3. **Widen or narrow** on request --> `--hours 72` for a longer event window; the bundle ends with a hint listing what is adjustable
4. **Make it tangible** --> add `--html` to any `investigate`/`attention` command for a self-contained offline snapshot (drill-down sections collapse/expand natively, no JS, nothing uploaded) written to `~/vmware-health/`
5. **If the object name is unknown** --> the tool returns a *teaching* error naming exactly how to list the objects (`list_virtual_machines` / `list_esxi_hosts` / `list_all_datastores`); get the exact name and retry
6. **If a vCenter is unreachable** (attention only) --> it is listed under `unreachable` with a reason and the rest still aggregate — surface the gap, don't fail the whole view

### Performance Triage ("the cluster feels slow")
**Judgment**: inventory shows *configured* capacity (cores, GB); it cannot tell you what is actually hot. Use the real-time perf tools, then narrow.
1. Rank hosts --> `vmware-monitor perf hosts` — the busiest host floats to the top (sorted by CPU%)
2. Rank VMs on the suspect --> `vmware-monitor perf vms --limit 25` — find the noisy neighbour
3. Check for hidden storage pressure --> `vmware-monitor capacity datastores` — over-commit % > 100 means a thin datastore can fill mid-run even with "free" space showing
4. Rule out snapshot drag --> `vmware-monitor snapshots aging --only-old` — old snapshots silently degrade I/O
5. **If perf tools return empty** --> the host/VM may be disconnected or powered off (no real-time provider); confirm with `inventory hosts` / `inventory vms`

### Scheduled-Outage Pre-flight (certs, licenses, time)
1. Cert expiry --> `vmware-monitor infra certs --warn-days 60` — an expired ESXi cert drops host management
2. License headroom --> `vmware-monitor infra licenses` — catch over-allocation before it disables features
3. Time sync --> `vmware-monitor infra ntp` — `healthy: no` breaks SSO/Kerberos/log correlation (note: live offset is not exposed by the SOAP API, only config health)

### Set Up Continuous Monitoring
1. Configure webhook in `~/.vmware-monitor/config.yaml`
2. Start daemon --> `vmware-monitor daemon start`
3. Daemon scans every 15 min, sends alerts to Slack/Discord

## Usage Mode

| Scenario | Recommended | Why |
|----------|:-----------:|-----|
| Local/small models (Ollama, Qwen) | **CLI** | ~2K tokens vs ~8K for MCP |
| Cloud models (Claude, GPT-4o) | Either | MCP gives structured JSON I/O |
| Automated pipelines | **MCP** | Type-safe parameters, structured output |

## MCP Tools (31 — all read-only)

| Tool | Description |
|------|------------|
| `list_virtual_machines` | List VMs with filtering (power state, sort, limit, `folder_filter`); each VM includes `folder_path` |
| `list_esxi_hosts` | ESXi hosts with CPU, memory, version, uptime |
| `list_all_datastores` | Datastores with capacity, free space, type |
| `list_all_clusters` | Clusters with host count, DRS/HA status |
| `cluster_health_summary` | One-glance triage across all clusters — ranked `top_issues` focus list + per-cluster rollup with opinionated `status`. Params: `cluster_filter`, `include_vms`, `top_n`. Render per `references/health-summary-template.md` |
| `vm_investigation_bundle` | "What is happening around this VM?" — correlated drill-down with a merged, newest-first **event timeline** (contents listed in the workflow above). Params: `vm_name`, `hours`. Aggregated in the tool; explain, don't dump raw |
| `host_investigation_bundle` | Same correlated drill-down around an ESXi host. Params: `host_name`, `hours` |
| `datastore_investigation_bundle` | Same correlated drill-down around a datastore. Params: `datastore_name`, `hours` |
| `cross_vcenter_attention` | "What needs attention now?" across **every** configured vCenter — one globally-ranked `top_issues` list (each tagged with its `vcenter`) + per-target rollup; unreachable targets degrade gracefully. Params: `cluster_filter`, `top_n` |
| `list_all_networks` | Networks with attached VM count and accessibility |
| `get_alarms` | All active/triggered alarms — includes `suggested_actions` remediation hints |
| `get_events` | Recent events filtered by severity and time — includes `suggested_actions` hints |
| `get_host_sensors` | Hardware sensor status (temperature/voltage/fan) per host with green/yellow/red health |
| `get_host_services` | Host service status (running state and startup policy), optionally filtered by host |
| `vm_info` | Detailed VM info (CPU, memory, disks, NICs, snapshots) |
| `vm_list_snapshots` | Snapshot list for one VM with nesting hierarchy (read-only) |
| `host_performance` | **Real-time** host CPU/mem/disk/net utilisation (PerfManager); busiest first |
| `vm_performance` | **Real-time** VM CPU/mem/disk/net utilisation (top 25 by default); powered-on only |
| `snapshot_aging` | Inventory-wide snapshot sweep with age + sprawl; flags snapshots older than N days |
| `certificate_status` | Per-host ESXi management certificate expiry (days until expiry, expiring flag) |
| `license_status` | vCenter/ESXi license inventory with used/total and expiration |
| `ntp_status` | Per-host NTP config health (servers + ntpd state); live offset not in SOAP API |
| `datastore_capacity` | Datastore over-commit (provisioned vs capacity); thin-provisioning risk |
| `resource_pool_usage` | Resource-pool CPU/memory reservation, limit, and current usage |
| `active_tasks` | In-flight (and recently completed) vCenter tasks with progress/errors |
| `active_sessions` | Currently authenticated vCenter/ESXi sessions (who is logged in) |
| `host_log_scan` | Scan recent ESXi host syslog (hostd/vmkernel/vpxa) for error/warning patterns; returns only matching lines, optionally filtered to one host |
| `host_memory_tiering` | **vSphere 9.1** — per-host memory tiering (DRAM/NVMe tiers) + NVMe uplift ratio (pyVmomi `hardware.memoryTierInfo`, needs ESXi 8.0U3+). Params: `host_name`, `limit`. Returns the list envelope |
| `cluster_patch_compliance` | **vSphere 9.1** — vLCM software (patch) compliance for one cluster over vSphere Automation REST. Param: `cluster` (MoID, e.g. `domain-c123`). `available:false` = vCenter answered 503 (likely mid-patch), not an error; `non_compliant_hosts` is `null` when the host-status field is unknown, never a false 0 |
| `cluster_last_apply_result` | **vSphere 9.1** — result of the last vLCM remediation (apply) on one cluster (REST). Param: `cluster` (MoID). Reports outcome only; never runs a remediation |
| `vcenter_deployment_size` | **vSphere 9.1** — vCenter appliance deployment size class (REST, NEW in 9.1). `available:false` on 503 |

> **vSphere 9.1 field-parse honesty**: the 3 REST tools' *endpoints* are spec-verified, but their JSON field names have NOT yet been replayed against a live 9.1 vCenter — every field is read defensively and each result self-labels via its `note` (`endpoint verified; field parse best-effort pending live 9.1 vCenter`). `host_memory_tiering` requires vCenter/ESXi 8.0U3+; older targets raise a teaching error naming the missing property.

All tools are **read-only**. No tool can modify, create, or delete any resource.
Performance/capacity readings are point-in-time samples — this skill retains no
history, so it never reports a fabricated "trend" or runway date.

### List result shape

The 21 row-listing tools above (including `host_memory_tiering`) return the family list envelope
`{items, returned, limit, total, truncated, hint}`, not a bare array. Read
`truncated` before summarising: `true` means more rows exist — never call
`items` the whole picture; `false` states the result is complete — including
when `items` is empty, which means "checked, found none", not "the call
failed". A `null` `total` (`get_events`, `host_log_scan`) is deliberate.
Aggregate tools return purpose-built objects instead — field table and
example payload: `references/capabilities.md`.

## Read-Only by Design

All 31 tools here are reads — there is no write, create, or delete surface at
all. Running with local or small models? See
[`references/agent-guardrails.md`](references/agent-guardrails.md).

## CLI Quick Reference

```bash
vmware-monitor summary [--top 10] [--cluster <substr>] [--html] [--target <t>]
vmware-monitor inventory vms|hosts|datastores|clusters|networks [--target <t>]
vmware-monitor health alarms|events|sensors|services [--target <t>]
vmware-monitor perf hosts|vms [--target <t>]
vmware-monitor capacity datastores|pools [--target <t>]
vmware-monitor infra certs|licenses|ntp [--target <t>]
vmware-monitor snapshots aging [--only-old] [--target <t>]
vmware-monitor vm info <vm-name> [--target <t>]
vmware-monitor memory tiering [--host <esxi>] [--target <t>]        # vSphere 9.1
vmware-monitor patch compliance|last-apply <cluster-moid> [--target <t>]   # vSphere 9.1 (vLCM)
vmware-monitor deployment-size [--target <t>]                      # vSphere 9.1
vmware-monitor scan now | daemon start|stop|status | doctor [--skip-auth]
```

> Full CLI reference (all flags + activity/tasks/sessions): see `references/cli-reference.md`

## Troubleshooting

### Alarms returns empty but vCenter shows alarms
The `get_alarms` tool queries triggered alarms at the root folder level. Some alarms are entity-specific — try checking events instead: `get_events --hours 1 --severity info`.

### "Connection refused" error
1. Run `vmware-monitor doctor` to diagnose
2. Verify target hostname/IP and port (443) in config.yaml
3. For self-signed certs: set `verify_ssl: false`

### Events returns too many results
Use severity filter: `--severity warning` (default) filters out info-level events. Use `--hours 4` to narrow time range.

### VM info shows "guest_os: unknown"
VMware Tools not installed or not running in the guest. Install/start VMware Tools for guest OS detection, IP address, and guest family info.

### Doctor passes but commands fail with timeout
vCenter may be under heavy load. Try targeting a specific ESXi host directly instead of vCenter, or increase connection timeout in config.yaml.

### Should I set `environment:` on a read-only skill?
You can — add `environment: production` (or `staging`, `lab`, your own label)
to each target in `~/.vmware-monitor/config.yaml`. It's an optional label; this
skill has zero write tools, so nothing it exposes is ever gated by it — reads
are never gated. It matters for the write skills (`vmware-aiops`,
`vmware-storage`, `vmware-nsx`) pointed at the same vCenter: an
environment-scoped `deny` rule in `~/.vmware/rules.yaml` can match on the label
to block their writes (e.g. freeze `production`). A target with no label is
simply not matched by such a rule. Config example: `references/setup-guide.md`.

## Setup

```bash
uv tool install vmware-monitor
vmware-monitor init      # guided: prompts for host/user/password, writes config + .env (chmod 600), then verifies
```

`init` stores the password grep-safe (obfuscated `b64:`, never plaintext) and
locks `.env` to 0600. Prefer it over hand-editing; manual steps:
`references/setup-guide.md`.

> All tools are automatically audited via vmware-policy. Audit logs: `vmware-audit log --last 20`

> Full setup guide, security details, and AI platform compatibility: see `references/setup-guide.md`

## Audit & Safety

All operations are automatically audited via vmware-policy (`@vmware_tool` decorator):
- Every tool call logged to `~/.vmware/audit.db` (SQLite, framework-agnostic)
- Policy rules enforced via `~/.vmware/rules.yaml` (deny rules, maintenance windows, risk levels)
- Risk classification: each tool tagged as low/medium/high/critical
- View recent operations: `vmware-audit log --last 20`
- View denied operations: `vmware-audit log --status denied`

vmware-policy is automatically installed as a dependency — no manual setup needed.

## License

MIT — [github.com/vmware-skills/VMware-Monitor](https://github.com/vmware-skills/VMware-Monitor)
