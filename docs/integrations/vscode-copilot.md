# Using vmware-monitor with VS Code Copilot

VS Code's GitHub Copilot supports MCP servers via `.vscode/mcp.json`. This guide shows how to add `vmware-monitor` for safe, read-only VMware monitoring from within VS Code.

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

## Adding to VS Code Copilot

### Option A: Workspace config (`.vscode/mcp.json`)

Create or edit `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "vmware-monitor": {
      "type": "stdio",
      "command": "vmware-monitor",
      "args": ["mcp"],
      "env": {
        "VMWARE_MONITOR_CONFIG": "${env:HOME}/.vmware-monitor/config.yaml"
      }
    }
  }
}
```

### Option B: User-level config (`settings.json`)

Add to VS Code `settings.json` (`Cmd+Shift+P` → "Open User Settings JSON"):

```json
{
  "github.copilot.chat.mcp.enabled": true,
  "mcp": {
    "servers": {
      "vmware-monitor": {
        "type": "stdio",
        "command": "vmware-monitor",
        "args": ["mcp"],
        "env": {
          "VMWARE_MONITOR_CONFIG": "/Users/your-name/.vmware-monitor/config.yaml"
        }
      }
    }
  }
}
```

A ready-to-use template is available at `examples/mcp-configs/vscode-copilot.json`.

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

**Example 1: Check environment before deployment**
```
You: @vmware-monitor Is the staging environment healthy?

Copilot: [calls get_alarms + get_vm_info for vm-staging]
vm-staging: ✓ ON | 8 vCPU | 16GB (11GB used)
No active alarms | Datastore: 450GB free
Ready for deployment ✓
```

**Example 2: Inventory during incident response**
```
You: @vmware-monitor Which VMs are on esxi-03 and what's their status?

Copilot: [calls list_virtual_machines with fields filtering]
esxi-03 (14 VMs — CPU 88%, RAM 94%):
- vm-db-primary: ON, 32GB RAM, 2 active alarms
- vm-db-replica: ON, 32GB RAM, healthy
- vm-web01..vm-web08: ON, 8GB RAM each, healthy
- vm-batch01: ON, 64GB RAM — likely causing high load
```

**Example 3: Snapshot audit**
```
You: @vmware-monitor Find VMs with snapshots older than 7 days

Copilot: [calls list_virtual_machines → get_vm_info for each]
3 VMs with stale snapshots:
- vm-db-01: snapshot "pre-patch" — 21 days old (4.2GB)
- vm-web03: snapshot "before-upgrade" — 14 days old (1.8GB)
- vm-test-02: snapshot "initial" — 45 days old (8.1GB) ← consider removing
```
