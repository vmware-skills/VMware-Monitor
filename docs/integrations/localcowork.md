# Using vmware-monitor with LocalCowork

[LocalCowork](https://github.com/liquid-ai/LocalCowork) is an AI collaboration platform by Liquid AI. This guide shows how to add `vmware-monitor` as an MCP server for safe, read-only VMware monitoring.

> **Code-level safety**: vmware-monitor contains zero destructive code — no power off, delete, create, or reconfigure functions in the codebase.

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

## Adding to LocalCowork

Edit the LocalCowork MCP config JSON (`~/.localcowork/mcp_config.json` or via the UI):

```json
{
  "vmware-monitor": {
    "command": "vmware-monitor",
    "args": ["mcp"],
    "env": {
      "VMWARE_MONITOR_CONFIG": "~/.vmware-monitor/config.yaml"
    }
  }
}
```

Requires `uv tool install vmware-monitor` (v1.5.15+).

A ready-to-use template is available at `examples/mcp-configs/localcowork.json`.

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

**Example 1: Capacity planning check**
```
You: Which datastores are above 80% usage?

LocalCowork: [calls list_all_datastores]
2 datastores above 80%:
- datastore-ssd01: 84% (1.6TB used / 1.9TB total) ← WARNING
- datastore-hdd02: 91% (9.1TB used / 10TB total)  ← CRITICAL
Recommendation: Expand or migrate VMs from datastore-hdd02.
```

**Example 2: Host health check**
```
You: Show all ESXi hosts and their status

LocalCowork: [calls list_esxi_hosts]
esxi-01: ✓ CPU 42% | RAM 67% | 12 VMs | ESXi 8.0.2 | Up 47d
esxi-02: ✓ CPU 38% | RAM 71% | 10 VMs | ESXi 8.0.2 | Up 47d
esxi-03: ⚠ CPU 88% | RAM 94% | 14 VMs | ESXi 8.0.2 | Up 47d ← HIGH LOAD
```

**Example 3: Recent events audit**
```
You: Show the last 20 events for any VM deletions or power-offs

LocalCowork: [calls get_events]
2026-03-11 14:22 - vm-temp-01 powered off by admin@vsphere.local
2026-03-11 12:08 - vm-test-03 deleted by admin@vsphere.local
2026-03-10 18:45 - vm-staging powered off (scheduled)
```
