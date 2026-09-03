# CLI Reference

Full command reference for `vmware-monitor`.

> **Output shape**: the CLI renders tables, so command output is unchanged. The
> equivalent MCP tools wrap their rows in the list envelope
> (`{items, returned, limit, total, truncated, hint}`) — see
> [`capabilities.md`](capabilities.md#list-result-envelope).

## Diagnostics

```bash
vmware-monitor doctor [--skip-auth]
```

Checks config file, connectivity, authentication, and pyVmomi version. Use `--skip-auth` to test config parsing without connecting.

## MCP Config Generator

```bash
vmware-monitor mcp-config generate --agent <goose|cursor|claude-code|continue|vscode-copilot|localcowork|mcp-agent>
vmware-monitor mcp-config list
```

Generates MCP configuration files for supported agents. `list` shows all available agent templates.

## Cluster Health Summary

```bash
vmware-monitor summary [--top <n>] [--cluster <substring>] [--no-vms] [--html] [--html-path <file>] [--target <name>]
```

- One aggregated read across all clusters. Leads with a ranked **top-N issues** focus list (the individual anomalies — disconnected hosts, triggered alarms, capacity/HA — worst first, each with a drill-down hint), then a per-cluster table: hosts connected/total, VM power rollup, live CPU/memory %, HA/DRS, alarm counts, and an opinionated `status` (ok/warn/critical) with `attention` reasons.
- `--top`: size of the focus list (default 10; `--top 5` tighter, `--top 0` hides it and shows only the table). The header shows "Top N (of TOTAL)" when truncated.
- `--cluster`: case-insensitive substring; show only matching clusters (also suppresses the standalone-hosts bucket).
- `--no-vms`: skip the VM rollup pass (faster on very large fleets when only host/alarm/capacity signals are needed).
- `--html`: write a self-contained, offline HTML snapshot (no external CSS/JS/fonts — nothing leaves the machine) to `~/vmware-health/cluster-health-<vc>-<YYYYMMDD-HHMMSS>.html`. The timestamped filename turns a folder of snapshots into a browsable point-in-time history. It is a snapshot, not a live page — re-run to refresh.
- `--html-path <file>`: write the HTML snapshot to an explicit path instead of the auto-timestamped default (implies `--html`).
- The rendered view is customizable — columns, thresholds, and layout live in [`health-summary-template.md`](health-summary-template.md). MCP tool: `cluster_health_summary`.

## Object Investigation (drill-down)

```bash
vmware-monitor investigate vm <name>        [--hours <n>] [--html] [--html-path <file>] [--target <name>]
vmware-monitor investigate host <name>      [--hours <n>] [--html] [--html-path <file>] [--target <name>]
vmware-monitor investigate datastore <name> [--hours <n>] [--html] [--html-path <file>] [--target <name>]
```

- "What is happening around this object?" — one call *correlates* the object with its surrounding infrastructure and recent history, so the model explains an aggregated result instead of stitching several tools. Read-only; all cross-object reads are batched (cheap on large fleets).
- **vm**: VM state, the host it runs on, its cluster context, backing datastores, snapshots, alarms (across VM/host/cluster/datastore), live performance, and a merged newest-first **event timeline** correlating the VM/host/cluster/datastore.
- **host**: host state (connection, CPU/memory, ESXi version, uptime), cluster context, a rollup of the VMs it runs, mounted datastores, alarms, performance, correlated timeline.
- **datastore**: capacity/free/accessibility, the hosts mounting it, a rollup of the VMs it backs, alarms, correlated timeline (per-datastore latency is a separate perf report, not included).
- `--hours`: event-timeline look-back window (default 24).
- `--html` / `--html-path`: self-contained offline snapshot to `~/vmware-health/investigate-<kind>-<object>-<timestamp>.html`; drill-down sections collapse/expand natively (no JS), nothing uploaded.
- Unknown object names return a *teaching* error naming how to list objects. MCP tools: `vm_investigation_bundle`, `host_investigation_bundle`, `datastore_investigation_bundle`.

## Cross-vCenter Attention

```bash
vmware-monitor attention [--top <n>] [--cluster <substring>] [--html] [--html-path <file>]
```

- "What needs attention now?" across **every** configured vCenter — one globally-ranked `top_issues` list (each tagged with its `vcenter`) plus a per-target table. Use it instead of running `summary` once per target and merging by hand.
- Degrades gracefully: a target that can't be reached (or errors mid-summary) is listed as `unreachable` with a reason, and the rest still aggregate — one dead vCenter never sinks the view.
- `--top`: size of the merged focus list (default 10). `--cluster`: case-insensitive substring applied to every target.
- `--html` / `--html-path`: offline snapshot to `~/vmware-health/attention-<timestamp>.html`. MCP tool: `cross_vcenter_attention`.

## Inventory

```bash
vmware-monitor inventory vms [--target <name>] [--limit <n>] [--sort-by name|cpu|memory_mb|power_state|folder_path] [--power-state poweredOn|poweredOff] [--folder-filter <pattern>]
vmware-monitor inventory hosts [--target <name>]
vmware-monitor inventory datastores [--target <name>]
vmware-monitor inventory clusters [--target <name>]
vmware-monitor inventory networks [--target <name>]
```

- `--target`: Named target from `config.yaml` (default: first target)
- `--limit`: Max VMs to return (default: unlimited)
- `--sort-by`: Sort field for VM listing — `name` | `cpu` | `memory_mb` | `power_state` | `folder_path`
- `--power-state`: Filter VMs by power state
- `--folder-filter`: Case-insensitive substring match against `folder_path` (e.g. `--folder-filter "Production"` returns VMs anywhere under a folder containing "production", including nested subfolders like `/Datacenters/Production/Web Tier`).

**Output fields** (CLI and MCP): each VM entry includes `folder_path` — the vCenter inventory folder path (e.g. `/Datacenters/Production/Web Tier`). Present in both compact and full modes.

## Health

```bash
vmware-monitor health alarms [--target <name>]
vmware-monitor health events [--hours 24] [--severity warning] [--target <name>]
vmware-monitor health sensors [--target <name>]
vmware-monitor health services [--host <esxi-name>] [--target <name>]
```

- `--hours`: Time range for event query (default: 24)
- `--severity`: Minimum severity filter — `info`, `warning`, `error`, `critical` (default: `warning`)
- `--host` (services only): Filter service status to a single host by exact name (default: all hosts)

## VM Info (Read-Only)

```bash
vmware-monitor vm info <vm-name> [--target <name>]
vmware-monitor vm snapshot-list <vm-name> [--target <name>]
```

Returns detailed VM information: CPU, memory, disks, NICs, guest OS, IP, VMware Tools status, and snapshots.

`snapshot-list` lists existing snapshots with name and creation time. No create, revert, or delete operations exist. The same data is exposed via the MCP tool `vm_list_snapshots`.

```bash
vmware-monitor snapshots backup-window <vm> [--days 30] [--cycles]
```

Infers backup windows for one VM from its snapshot task history: image-level
backup products snapshot the VM, copy the frozen disks, then delete the
snapshot, and vCenter records both ends. Reports four hour statistics
(backup active / snapshot present / total window / removal itself) plus
creations that never got a matching removal.

Two lines in the output change what the numbers mean, and both are printed
rather than left to the reader: a note when vCenter could not be read at all
(which is not "no backups"), and a note when part of the requested window has
already aged out of task history (`task.maxAge`, 30 days by default — the
option is not called `vpxd.task.maxAge`; that name raises `vim.fault.InvalidName`).
Duplicate VM names are refused rather than resolved to one. The same data is
exposed via the MCP tool `vm_backup_snapshot_history`.

## vSphere 9.1 (read-only)

```bash
vmware-monitor memory tiering [--host <esxi>] [--limit <n>] [--target <name>]
vmware-monitor patch compliance <cluster-moid> [--target <name>]
vmware-monitor patch last-apply <cluster-moid> [--target <name>]
vmware-monitor deployment-size [--target <name>]
```

- `memory tiering`: Per-host memory tiering (DRAM/NVMe tiers) and NVMe uplift ratio via pyVmomi `hardware.memoryTierInfo`. Requires ESXi/vCenter **8.0U3+**; on older targets the command fails with a teaching error naming the missing property. Same data as the MCP tool `host_memory_tiering`.
- `patch compliance`: vLCM software (patch) compliance for one cluster over vSphere Automation REST. `<cluster-moid>` is the cluster **MoID** (e.g. `domain-c123`) — get it from `inventory clusters`, not the display name. When vCenter is mid-patch it answers 503 and the command prints a "state unavailable, retry shortly" line instead of crashing; `non-compliant` shows `?` when the host-status field is unknown (never a false 0). MCP tool: `cluster_patch_compliance`.
- `patch last-apply`: Result (status + end time) of the last vLCM remediation on one cluster. `<cluster-moid>` as above. Reports the outcome only; it never runs a remediation. MCP tool: `cluster_last_apply_result`.
- `deployment-size`: vCenter appliance deployment size class over REST (NEW in vSphere 9.1). MCP tool: `vcenter_deployment_size`.

> These four commands resolve config **without opening a SOAP session** and only ever GET, so they still degrade gracefully when vCenter is mid-patch (answering 503). The three REST commands' endpoints are spec-verified, but their JSON field names are read defensively pending a live 9.1 vCenter replay — each result carries a `note` that self-labels the parse as best-effort.

## Scanning & Daemon

```bash
vmware-monitor scan now [--target <name>]
vmware-monitor daemon start
vmware-monitor daemon stop
vmware-monitor daemon status
```

- `scan now`: Run a one-time scan of alarms, events, and host logs
- `daemon start`: Start APScheduler-based background scanner (default: every 15 min)
- `daemon stop`: Stop the background scanner
- `daemon status`: Check if the daemon is running

## Setup

```bash
mkdir -p ~/.vmware-monitor
cp config.example.yaml ~/.vmware-monitor/config.yaml
cp .env.example ~/.vmware-monitor/.env
chmod 600 ~/.vmware-monitor/.env
```

Copies the `config.yaml` and `.env` templates into `~/.vmware-monitor/`; then edit them with your target details.

## Architecture

```
User (Natural Language)
  |
AI Tool (Claude Code / Aider / Gemini / Codex / Cursor / Trae / Kimi)
  |
  +-- CLI mode (default): vmware-monitor CLI --> pyVmomi --> vSphere API
  |
  +-- MCP mode (optional): MCP Server (stdio) --> pyVmomi --> vSphere API
  |
vCenter Server --> ESXi Clusters --> VMs
    or
ESXi Standalone --> VMs
```
