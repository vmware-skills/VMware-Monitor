# Using vmware-monitor with Cursor

[Cursor](https://www.cursor.com) is an AI-powered code editor with native MCP support. This guide shows how to add `vmware-monitor` (read-only) as an MCP server for safe daily VMware monitoring in Cursor.

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
       username: administrator@vsphere.local
       password_env: VMWARE_PASSWORD
       verify_ssl: false
   EOF

   echo "VMWARE_PASSWORD=your_password" > ~/.vmware-monitor/.env
   chmod 600 ~/.vmware-monitor/.env
   ```

3. **Verify setup**
   ```bash
   vmware-monitor doctor
   ```

## Adding to Cursor

### Option A: Auto-install (recommended)

```bash
vmware-monitor mcp-config install --agent cursor
```

### Option B: Manual — mcp.json

Add to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "vmware-monitor": {
      "command": "vmware-monitor",
      "args": ["mcp"],
      "env": {
        "VMWARE_MONITOR_CONFIG": "~/.vmware-monitor/config.yaml"
      }
    }
  }
}
```

## Available MCP Tools (27 read-only tools)

| Tool | Description |
|------|-------------|
| `list_virtual_machines` | List VMs with power state, CPU, memory |
| `list_esxi_hosts` | List ESXi hosts with health status |
| `list_all_datastores` | List datastores with capacity info |
| `list_all_clusters` | List clusters with DRS/HA status |
| `get_alarms` | Get active alarms |
| `get_events` | Get recent events |
| `get_vm_info` | Get detailed VM information |
| `get_cluster_health_summary` | Cross-cluster triage rollup with Top-N issues |

The table above is the most-used subset. The server exposes 27 read-only tools in
total — performance, host health, platform state, and the investigation bundles are
listed in [`skills/vmware-monitor/references/capabilities.md`](../../skills/vmware-monitor/references/capabilities.md).

All tools are **read-only** — no destructive operations exist in the codebase.

## Usage Examples

**Example 1: Daily health check**
```
You: Show me any active alarms in my vCenter

Cursor: [calls get_alarms]
3 active alarms:
- vm-db01: Memory balloon (Warning)
- esxi-03: CPU ready > 5% (Warning)
- datastore02: 87% capacity used (Critical)
```

**Example 2: Inventory overview**
```
You: How many VMs are in each cluster?

Cursor: [calls list_all_clusters, list_virtual_machines]
Cluster-Prod: 28 VMs (24 on, 4 off)
Cluster-Dev: 15 VMs (10 on, 5 off)
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| MCP server not listed | Reload Cursor window after editing mcp.json |
| Auth failure | Run `vmware-monitor doctor` to verify connectivity |
| `cwd` path error | Use absolute path in mcp.json |
