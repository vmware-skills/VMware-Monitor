# Capabilities (Read-Only)

Detailed feature tables for `vmware-monitor`.

## List result envelope

The 20 list-returning MCP tools — `list_virtual_machines`, `list_esxi_hosts`,
`list_all_datastores`,
`list_all_clusters`, `list_all_networks`, `get_alarms`, `get_events`,
`get_host_sensors`, `get_host_services`, `host_log_scan`, `active_tasks`,
`active_sessions`, `datastore_capacity`, `resource_pool_usage`,
`certificate_status`, `license_status`, `ntp_status`, `host_performance`,
`vm_performance`, `vm_list_snapshots` — return the family list envelope rather
than a bare array:

```json
{"items": [...], "returned": 50, "limit": 50, "total": 213,
 "truncated": true, "hint": "Showing 50 of 213. Raise limit or narrow the query..."}
```

| Key | Meaning |
|-----|---------|
| `items` | The rows, in the tool's documented order |
| `returned` | `len(items)` — check this before claiming "no data" |
| `limit` | The limit that produced this page; `null` when unlimited |
| `total` | Real collection size, or `null` when the backing API does not report one |
| `truncated` | `true` = more rows exist behind this page; `false` = this is complete |
| `hint` | What to do about truncation; `null` when complete |

Two tools report `total: null` on purpose: `get_events` (vCenter's event
collector applies its own bounds) and `host_log_scan` (only the last N lines
per log are read). Everywhere else the total is a real count taken before the
limit was applied, which is what lets a full page be recognised as complete
instead of flagged as possibly-truncated.

The envelope adds ~30 tokens to a response. It exists because a bare list gave
smaller models nothing to distinguish a complete answer from page one, and they
sometimes resolved that ambiguity as "no data was returned"
(VMware-AIops issue #31).

`list_virtual_machines` adds one extra key to the envelope, `mode` (`"full"` or
`"compact"`), and reuses `hint` for the compact-mode note; its `total` is the
count after `power_state` / `folder_filter` are applied and before `limit`.

Tools with purpose-built return objects — `vm_info`, `snapshot_aging`,
`cluster_health_summary`, `cross_vcenter_attention`, and the three
`*_investigation_bundle` tools — are unaffected.

## Automation Level Reference

Each operation is classified by autonomy level per the Enterprise Harness Engineering framework. **vmware-monitor is L1/L2 only by design** — no write operations exist in the codebase, enforced at the test level.

| Level | Meaning | Agent autonomy | Examples in this skill |
|:-:|---|---|---|
| **L1** | Read-only, raw data | Always auto-run | `list_virtual_machines`, `list_esxi_hosts`, `get_alarms`, `get_events`, `list_all_datastores`, `list_all_clusters`, `host_performance` |
| **L2** | Read + analysis / recommendation | Always auto-run | `cluster_health_summary`, `cross_vcenter_attention`, `snapshot_aging`, the three `*_investigation_bundle` tools, scheduled scan reports, log pattern matching (error/fail/critical/panic/timeout), alarm correlation, daemon-driven webhook digests |
| **L3** | Single write — user must approve | *N/A* | — *(use [vmware-aiops](https://github.com/vmware-skills/VMware-AIops) for write operations)* |
| **L4** | Multi-step plan / apply workflow | *N/A* | — *(use [vmware-pilot](https://github.com/vmware-skills/VMware-Pilot) for orchestration)* |
| **L5** | Auto-remediation from learned pattern | *N/A* | — *(remediation is out of scope by design)* |

**Notes**:
- All tools are safe for agents to call without confirmation — the skill is code-level read-only.
- Test file `test_no_destructive_operations.py` enforces this invariant on every commit.

## 0. Cluster Health Summary (triage)

CLI `summary`, MCP `cluster_health_summary`. One aggregated read for a fast
cross-cluster "is anything on fire?" glance — the operator's first look, not an
Aria Operations replacement.

| Aspect | Detail |
|--------|--------|
| Passes | 3 batched `RetrievePropertiesEx` calls (clusters, hosts, VMs) — never one per object (issue #31 class) |
| Focus list | `top_issues`: individual anomalies (disconnected hosts, triggered alarms, capacity/HA) flattened + ranked worst-first, capped at `top_n`; `issues_total` reports pre-cap count. Alarm names resolved in one batched call (no N+1) |
| Rollup | Per cluster: hosts connected/total, VM power, live CPU/mem %, HA/DRS, alarm counts (cluster + host) |
| Status | Opinionated `ok` / `warn` / `critical` + plain-language `attention` reasons; sorted worst-first |
| Thresholds | `CPU_MEM_WARN_PCT=85`, `CPU_MEM_CRIT_PCT=95` (named constants in `ops/cluster_summary.py`); disconnected host or critical alarm forces `critical` |
| Customizable | Columns/thresholds/layout in [`health-summary-template.md`](health-summary-template.md); response carries a `customization_hint` |

### `cluster_health_summary` — input parameters

| Parameter | Type | Default | Behavior |
|-----------|------|---------|----------|
| `target` | str (optional) | default target | Named vCenter/ESXi target |
| `cluster_filter` | str (optional) | None (all) | Case-insensitive substring; suppresses standalone-hosts bucket |
| `include_vms` | bool | True | Roll up VM power counts; False skips the VM pass (faster on huge fleets) |
| `top_n` | int | 10 | Cap the `top_issues` focus list; `issues_total` keeps the pre-cap count; 0 hides the list |

**Typical response tokens**: ~120–400 (one compact row per cluster + totals);
scales with cluster count, not VM count. This is the aggregation-in-the-tool
pattern — the model never sees raw inventory.

## 1. Inventory

| Feature | vCenter | ESXi | Details |
|---------|:-------:|:----:|---------|
| Cluster health summary | Y | N | Cross-cluster triage rollup with opinionated status — CLI `summary`, MCP `cluster_health_summary` |
| List VMs | Y | Y | Name, power state, CPU, memory, guest OS, IP, `folder_path` (vCenter inventory folder, e.g. `/Datacenters/Production/Web Tier`) |
| List Hosts | Y | Self only | CPU cores, memory, ESXi version, VM count, uptime |
| List Datastores | Y | Y | Capacity, free/used, type (VMFS/NFS), usage % |
| List Clusters | Y | N | Host count, DRS/HA status |
| List Networks | Y | Y | Network name, associated VM count, accessibility — CLI `inventory networks`, MCP `list_all_networks` |

### `list_virtual_machines` — input parameters

| Parameter | Type | Default | Behavior |
|-----------|------|---------|----------|
| `target` | str (optional) | default target | Named vCenter/ESXi target from `config.yaml` |
| `limit` | int (optional) | None (all) | Max VMs to return |
| `sort_by` | str | `name` | `name` \| `cpu` \| `memory_mb` \| `power_state` \| `folder_path` |
| `power_state` | str (optional) | None | `poweredOn` \| `poweredOff` \| `suspended` |
| `fields` | list[str] (optional) | auto | Subset of: `name`, `power_state`, `cpu`, `memory_mb`, `guest_os`, `ip_address`, `host`, `uuid`, `tools_status`, `folder_path` |
| `folder_filter` | str (optional) | None | Case-insensitive substring match against `folder_path` (CLI `--folder-filter`, MCP `folder_filter`). Example: `folder_filter="Production"` returns VMs anywhere under any folder whose path contains "production" (including nested subfolders like `/Datacenters/Production/Web Tier`). |

### `list_virtual_machines` — response fields

Each VM dict in the `vms` array contains:

| Field | Description |
|-------|-------------|
| `name` | VM name |
| `power_state` | `poweredOn` / `poweredOff` / `suspended` |
| `cpu` | vCPU count |
| `memory_mb` | RAM in MB |
| `guest_os` | Guest OS full name (full mode only) |
| `ip_address` | Guest IP from VMware Tools (full mode only) |
| `host` | ESXi host name (full mode only) |
| `uuid` | VM UUID (full mode only) |
| `tools_status` | VMware Tools running status (full mode only) |
| `folder_path` | vCenter inventory folder path, e.g. `/Datacenters/Production/Web Tier`. Returned in both compact and full modes. |

## 2. Health & Monitoring

| Feature | vCenter | ESXi | Details |
|---------|:-------:|:----:|---------|
| Active Alarms | Y | Y | Severity, alarm name, entity, timestamp |
| Event/Log Query | Y | Y | Filter by time range, severity; 50+ event types |
| Hardware Sensors | Y | Y | Per-sensor `type` (temperature/voltage/fan...), reading, unit, and health `status` (green/yellow/red) — CLI `health sensors`, MCP `get_host_sensors` |
| Host Services | Y | Y | hostd, vpxa running/stopped status — CLI `health services`, MCP `get_host_services` |

### Alarm/Event `suggested_actions` example

`get_alarms` and `get_events` results include a `suggested_actions` list — each
item is a ready-to-use hint pointing to the correct companion skill and tool:

```json
{
  "alarm_name": "VM CPU Ready High",
  "entity_name": "prod-db-01",
  "suggested_actions": [
    "vmware-aiops: acknowledge_vcenter_alarm(entity_name='prod-db-01', alarm_name='VM CPU Ready High')",
    "vmware-aiops: reset_vcenter_alarm(entity_name='prod-db-01', alarm_name='VM CPU Ready High')"
  ]
}
```

### Monitored Event Types

| Category | Events |
|----------|--------|
| VM Failures | `VmFailedToPowerOnEvent`, `VmDiskFailedEvent`, `VmFailoverFailed` |
| Host Issues | `HostConnectionLostEvent`, `HostShutdownEvent`, `HostIpChangedEvent` |
| Storage | `DatastoreCapacityIncreasedEvent`, SCSI high latency |
| HA/DRS | `DasHostFailedEvent`, `DrsVmMigratedEvent`, `DrsSoftRuleViolationEvent` |
| Auth | `UserLoginSessionEvent`, `BadUsernameSessionEvent` |

## 3. VM Info & Snapshot List

| Feature | Details |
|---------|---------|
| VM Info | Name, power state, guest OS, CPU, memory, IP, VMware Tools, disks, NICs, `folder_path` |
| Snapshot List | List existing snapshots with name and creation time (no create/revert/delete) — CLI `vm snapshot-list`, MCP tool `vm_list_snapshots` |

## 4. Scheduled Scanning & Notifications

| Feature | Details |
|---------|---------|
| Daemon | APScheduler-based, configurable interval (default 15 min) |
| Multi-target Scan | Sequentially scan all configured vCenter/ESXi targets |
| Scan Content | Alarms + Events + Host logs (hostd, vmkernel, vpxd) |
| Log Analysis | Regex pattern matching: error, fail, critical, panic, timeout |
| Webhook | Slack, Discord, or any HTTP endpoint |

## Safety Features

| Feature | Details |
|---------|---------|
| Code-Level Isolation | Independent repository — zero destructive functions in codebase |
| Audit Trail | All queries logged to `~/.vmware/audit.db` (SQLite WAL, via vmware-policy) |
| Password Protection | `.env` file loading with permission check (warn if not 600) |
| SSL Self-signed Support | `disableSslCertValidation` — **only** for ESXi hosts with self-signed certificates in isolated lab/home environments. Production environments should use CA-signed certificates with full TLS verification enabled. |

## FORBIDDEN Operations — DO NOT EXIST IN CODEBASE

These operations **cannot** be performed with this skill — zero destructive code paths exist:

- `vm power-on/off`, `vm reset`, `vm suspend`
- `vm create/delete/reconfigure`
- `vm snapshot-create/revert/delete`
- `vm clone/migrate`

Direct users to **VMware-AIops** (`uv tool install vmware-aiops`) for these.

## Version Compatibility

| vSphere / VCF Version | Support | Notes |
|----------------|---------|-------|
| VCF 9.1 / vSphere 9.1 | ✅ Full | Released 2026-05-12. pyVmomi `<10.0` resolves and connects via SOAP. |
| VCF 9.0 / vSphere 9.0 | ✅ Full | pyVmomi 8.0.3+ connects against vSphere 9 SOAP API. |
| 8.0 / 8.0U1-U3 | Full | pyVmomi 8.0.3+ |
| 7.0 / 7.0U1-U3 | Full | All read-only APIs supported |
| 6.7 | Compatible | Backward-compatible, tested |
| 6.5 | Compatible | Backward-compatible, tested |

> pyVmomi auto-negotiates the API version during SOAP handshake — no manual configuration needed.
