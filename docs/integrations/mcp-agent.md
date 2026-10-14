# Using vmware-monitor with mcp-agent

[mcp-agent](https://github.com/lastmile-ai/mcp-agent) is an MCP-native agent framework by LastMile AI. This guide shows how to configure `vmware-monitor` for read-only VMware monitoring in mcp-agent workflows.

> **Code-level safety**: vmware-monitor contains zero destructive code — safe to use in automated pipelines without risk of accidental infrastructure changes.

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

## Adding to mcp-agent

Add to your `mcp_agent.config.yaml`:

```yaml
mcp:
  servers:
    vmware-monitor:
      command: vmware-monitor
      args:
        - mcp
      env:
        VMWARE_MONITOR_CONFIG: ~/.vmware-monitor/config.yaml
```

Requires `uv tool install vmware-monitor` (v1.5.15+). A ready-to-use template is available at `examples/mcp-configs/mcp-agent.yaml`.

### Full example config

```yaml
# mcp_agent.config.yaml
execution_engine: asyncio

mcp:
  servers:
    vmware-monitor:
      command: vmware-monitor
      args: [mcp]
      env:
        VMWARE_MONITOR_CONFIG: ~/.vmware-monitor/config.yaml

anthropic:
  model: claude-sonnet-4-6
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

**Example 1: Scheduled health report pipeline**
```python
# daily_report.py
from mcp_agent.app import MCPApp
from mcp_agent.agents.agent import Agent

app = MCPApp(name="vmware-daily-report")

async with app.run() as vmware_app:
    agent = Agent(
        name="monitor",
        instruction="You are a VMware monitoring agent. Generate concise health reports.",
        server_names=["vmware-monitor"],
    )
    async with agent:
        report = await agent.send(
            "Generate a daily health summary: "
            "active alarms, top 5 VMs by memory usage, "
            "datastores above 75% capacity"
        )
        print(report)
```

**Example 2: Automated alarm triage**
```
Agent: [calls get_alarms] → 5 active alarms
Agent: [calls get_vm_info for each alarmed VM]
Agent: Summary:
  - 2 CRITICAL: disk latency on vm-db-01, vm-db-02 (same datastore → storage issue)
  - 2 WARNING: memory pressure on vm-analytics (expected, batch job running)
  - 1 WARNING: CPU on esxi-03 (14 VMs, consider rebalancing)
```

**Example 3: Capacity planning data collection**
```python
result = await agent.send(
    "Collect capacity data: "
    "total VMs by host, datastore utilization percentages, "
    "cluster DRS/HA status. Format as JSON for reporting."
)
```
