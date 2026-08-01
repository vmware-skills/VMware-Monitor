# Fully Local VMware Monitoring with Ollama

Run VMware monitoring using a local LLM — no cloud API keys, no destructive operations.

## Prerequisites

- **Ollama** installed: https://ollama.com
- **vmware-monitor** installed: `uv tool install vmware-monitor`
- **VMware config** ready: `~/.vmware-monitor/config.yaml` + `~/.vmware-monitor/.env`

## Step 1: Pull a local model

```bash
# Recommended for monitoring queries
ollama pull qwen2.5-coder:14b

# Lightweight alternative
ollama pull qwen2.5-coder:7b
```

## Step 2: Run with Aider + Ollama

```bash
aider --conventions skills/vmware-monitor/SKILL.md --model ollama/qwen2.5-coder:14b
```

Example queries:
```
> List all VMs and their power state
> Show active alarms on my vCenter
> What's the datastore capacity usage?
> Show details for VM "web-server-01"
```

## Step 3: Run with Goose

Edit `~/.config/goose/config.yaml`:

```yaml
extensions:
  vmware-monitor:
    name: VMware Monitor
    cmd: vmware-monitor
    args: [mcp]
    enabled: true
    type: stdio
    timeout: 300
    envs:
      VMWARE_MONITOR_CONFIG: ~/.vmware-monitor/config.yaml
```

## Safety

All 27 MCP tools are read-only. No power-off, delete, create, clone, or migrate functions exist in the codebase. Safe to use with any local model — the worst case is a failed query, never a destructive action.

## Need full operations?

Use [vmware-aiops](https://github.com/vmware-skills/VMware-AIops) in dev/lab environments only.
