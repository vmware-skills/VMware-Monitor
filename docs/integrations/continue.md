# Using vmware-monitor with Continue

[Continue](https://github.com/continuedev/continue) is an open-source AI code assistant for VS Code and JetBrains. This guide shows how to add `vmware-monitor` as an MCP server for safe, read-only VMware monitoring.

> **Code-level safety**: vmware-monitor contains zero destructive code — no risk of accidental changes to your infrastructure.

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

## Adding to Continue

Add to `~/.continue/config.yaml`:

```yaml
mcpServers:
  - name: vmware-monitor
    command: vmware-monitor
    args:
      - mcp
    env:
      VMWARE_MONITOR_CONFIG: ~/.vmware-monitor/config.yaml
```

Requires `uv tool install vmware-monitor` (v1.5.15+). A ready-to-use template is available at `examples/mcp-configs/continue.yaml`.

### With Ollama (Local Model)

```yaml
# ~/.continue/config.yaml
models:
  - title: Qwen2.5 32B (local)
    provider: ollama
    model: qwen2.5:32b

mcpServers:
  - name: vmware-monitor
    command: vmware-monitor
    args: [mcp]
    env:
      VMWARE_MONITOR_CONFIG: ~/.vmware-monitor/config.yaml
```

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

**Example 1: Quick cluster status while coding**
```
You: @vmware-monitor How many VMs are running on each host?

Continue: [calls list_esxi_hosts]
esxi-01: 12 VMs (CPU 42%, RAM 67%)
esxi-02: 10 VMs (CPU 38%, RAM 71%)
esxi-03: 14 VMs (CPU 88%, RAM 94%) ← overloaded
```

**Example 2: Check VM before deploying code**
```
You: @vmware-monitor Is vm-staging healthy? Enough disk?

Continue: [calls get_vm_info]
vm-staging: ON | 8 vCPU | 16GB RAM (11.2GB used, 70%)
Datastore: ssd-ds01 — 450GB free ✓
No active alarms ✓
```

**Example 3: Audit recent changes**
```
You: @vmware-monitor Show events from the last hour

Continue: [calls get_events]
14:52 - vm-prod-web01 vMotion to esxi-02 (DRS rebalance)
14:38 - Snapshot created: vm-db-01/pre-maintenance
14:15 - vm-temp-test powered off by devops@company.com
```
