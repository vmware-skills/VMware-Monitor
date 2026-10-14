# Using vmware-monitor with Goose

[Goose](https://github.com/block/goose) is an open-source AI agent by Block that runs locally on your machine. This guide shows how to add `vmware-monitor` as an MCP extension for safe, read-only VMware monitoring.

> **Code-level safety**: vmware-monitor is an independent repository with zero destructive code paths — no power off, delete, create, or reconfigure functions exist in the codebase.

## Prerequisites

1. **Install vmware-monitor**
   ```bash
   uv tool install vmware-monitor
   ```

2. **Configure credentials**
   ```bash
   mkdir -p ~/.vmware-monitor
   cat > ~/.vmware-monitor/config.yaml << 'EOF'
   targets:
     my-vcenter:
       host: vcenter.example.com
       username: readonly-user@vsphere.local
       password_env: VMWARE_MONITOR_PASSWORD
       verify_ssl: false
   EOF

   echo "VMWARE_MONITOR_PASSWORD=your_password" > ~/.vmware-monitor/.env
   chmod 600 ~/.vmware-monitor/.env
   ```

3. **Verify setup**
   ```bash
   vmware-monitor doctor
   ```

## Adding to Goose

### Option A: goose configure (Interactive)

```bash
goose configure
# Select: Add Extension → MCP Server
# Name: vmware-monitor
# Command: vmware-monitor mcp     # v1.5.15+; legacy: uvx --from vmware-monitor vmware-monitor-mcp
# Env: VMWARE_MONITOR_CONFIG=~/.vmware-monitor/config.yaml
```

### Option B: config.yaml (Manual)

Add to `~/.config/goose/config.yaml`:

```yaml
extensions:
  vmware-monitor:
    type: stdio
    cmd: vmware-monitor
    args:
      - mcp
    env:
      VMWARE_MONITOR_CONFIG: ~/.vmware-monitor/config.yaml
    enabled: true
    description: VMware vCenter/ESXi read-only monitoring (code-level safe)
```

A ready-to-use template is available at `examples/mcp-configs/goose.json`.

## Available MCP Tools (27 read-only tools)

| Tool | Description |
|------|-------------|
| `list_virtual_machines` | List VMs with power state, CPU, RAM, IP. Supports `limit`, `sort_by`, `power_state`, `fields` |
| `list_esxi_hosts` | List hosts with CPU cores, memory, ESXi version, uptime |
| `list_all_datastores` | List datastores with capacity, free space, type |
| `list_all_clusters` | List clusters with host count, DRS/HA status |
| `get_alarms` | Get active alarms with severity and description |
| `get_events` | Get recent vCenter/ESXi events |
| `get_vm_info` | Get detailed VM info including snapshot list |

The table above is the most-used subset. The server exposes 27 read-only tools in
total — performance, host health, platform state, and the investigation bundles are
listed in [`skills/vmware-monitor/references/capabilities.md`](../../skills/vmware-monitor/references/capabilities.md).

All tools accept an optional `target` parameter to switch between environments, except
`get_cross_vcenter_attention`, which sweeps every configured target by design.

## Usage Examples

**Example 1: Infrastructure health overview**
```
You: Show all active alarms grouped by severity

Goose: [calls get_alarms]
CRITICAL (1):
  - vm-db-primary: Disk I/O latency > 50ms

WARNING (3):
  - esxi-host02: CPU ready time high
  - datastore-ssd: 81% capacity used
  - vm-web03: Memory balloon active

INFO (2): ...
```

**Example 2: VM inventory snapshot**
```
You: List all powered-on VMs sorted by memory usage

Goose: [calls list_virtual_machines with power_state=on, sort_by=memory]
vm-db-primary    32GB / 31.8GB used  (99%) ← high
vm-analytics01   16GB / 14.2GB used  (89%)
vm-api-gw        8GB  / 6.1GB used   (76%)
...
```

**Example 3: Investigate a specific VM**
```
You: Tell me everything about vm-web01 including its snapshots

Goose: [calls get_vm_info]
vm-web01:
  Power: ON | 4 vCPU | 8GB RAM (6.2GB used)
  Guest OS: Ubuntu 22.04 | IP: 10.0.1.22
  Datastore: ssd-ds01 | Uptime: 18 days
  Snapshots (2):
    - pre-patch-2026-02-20 (21 days old, 4.2GB)
    - before-nginx-upgrade (8 days old, 1.1GB)
```

## Local Model Support (Ollama)

`vmware-monitor` works with local models via Goose + Ollama:

```yaml
# ~/.config/goose/config.yaml
provider: ollama
model: qwen2.5:32b

extensions:
  vmware-monitor:
    type: stdio
    cmd: vmware-monitor
    args: [mcp]
    env:
      VMWARE_MONITOR_CONFIG: ~/.vmware-monitor/config.yaml
```

See [examples/ollama-local-setup.md](../../examples/ollama-local-setup.md) for full local model setup.
