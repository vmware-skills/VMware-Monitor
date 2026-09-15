# Setup Guide

## Install

All install methods fetch from the same source: [github.com/vmware-skills/VMware-Monitor](https://github.com/vmware-skills/VMware-Monitor) (MIT licensed). We recommend reviewing the source code before installing.

```bash
# Via PyPI (recommended for version pinning)
uv tool install vmware-monitor==1.14.0

# Via Skills.sh (fetches from GitHub)
npx skills add vmware-skills/VMware-Monitor#v1.14.0

# Via ClawHub (fetches from ClawHub registry snapshot of GitHub)
clawhub install @zw008/vmware-monitor --version 1.14.0
```

### Claude Code

`npx skills add` and `clawhub install` both place the skill in Claude Code's skills
directory. To install it manually from a clone:

```bash
mkdir -p ~/.claude/skills/vmware-monitor
cp -r skills/vmware-monitor/. ~/.claude/skills/vmware-monitor/
```

For tool access (not just skill context), register the MCP server:

```bash
claude mcp add vmware-monitor -- vmware-monitor mcp
```

## What Gets Installed

The `vmware-monitor` package installs a Python CLI binary — read-only toward vSphere — and its dependencies (pyVmomi, Click, Rich, APScheduler, python-dotenv). No background services, daemons, or system-level changes are made during installation. The scheduled scanner (`daemon start`) only runs when explicitly started by the user.

### What "read-only" covers, and what it writes locally

"Read-only" is a claim about vCenter/ESXi: no code path changes their state. On
vCenter the skill opens only its own login session (SOAP, plus a REST session for
the three vSphere 9.1 REST tools) and short-lived query handles — container views
and a task-history collector — that it releases after use.

It does write to the local machine — by default under your home directory:

| Path | Written by | When |
|---|---|---|
| `~/.vmware-monitor/config.yaml`, `~/.vmware-monitor/.env` | `vmware-monitor init` | Once, when you run it (`.env` is set to 0600) |
| `~/.vmware-monitor/.env` (rewritten in place) | Every run | Only if it holds a plaintext `*_PASSWORD`: the value is rewritten as `b64:` (see below) |
| `~/.vmware/audit.db` (SQLite; `OPS_HOME` moves it) | vmware-policy | Every MCP tool call, and every CLI command that reaches vCenter |
| `~/.vmware-monitor/audit.log` (JSON Lines) | CLI | Every CLI query command |
| `~/vmware-health/*.html` | `summary` / `investigate` / `attention` | Only with `--html` (or `--html-path <file>`) |
| `~/.vmware-monitor/daemon.pid`, `~/.vmware-monitor/scan.log` (`notify.log_file` moves the log) | Scanner daemon | Only after `daemon start` |
| The file you name | `mcp-config generate --output <file>` | Only with `--output`; otherwise it prints |

Outbound traffic beyond vCenter/ESXi: the daemon posts to your Slack/Discord
webhook, only if you configured one and a scan finds an issue it sends — any
critical issue, or an alarm/event warning (host-log warnings and `info` rows
are never sent).

## Configuration

```bash
# 1. Install
uv tool install vmware-monitor==1.14.0

# 2. Verify
vmware-monitor --version

# 3. Configure
mkdir -p ~/.vmware-monitor
cp config.example.yaml ~/.vmware-monitor/config.yaml
cp .env.example ~/.vmware-monitor/.env
chmod 600 ~/.vmware-monitor/.env
# Edit ~/.vmware-monitor/config.yaml and .env with your target details
```

### Declare `environment:` on each target

```yaml
targets:
  - name: prod-vcenter
    host: vcenter-prod.example.com
    environment: production   # production | staging | lab | <your own label>
```

`environment` is an optional label. This skill has zero write tools, so nothing
it exposes is ever gated by it — reads are never gated under any setting. It
matters for the write skills (`vmware-aiops`, `vmware-storage`, `vmware-nsx`)
pointed at the same vCenter: an environment-scoped `deny` rule in
`~/.vmware/rules.yaml` can match on the label to block their writes (e.g. freeze
`production`). A target with no label is simply not matched by such a rule.

## Development Install

```bash
git clone --branch v1.14.0 https://github.com/vmware-skills/VMware-Monitor.git
cd VMware-Monitor
uv venv && source .venv/bin/activate
uv pip install -e .
```

## Usage Mode

Choose the best mode based on your AI tool:

| Platform | Recommended Mode | Why |
|----------|-----------------|-----|
| Claude Code, Cursor | **MCP** | Structured tool calls, no interactive confirmation needed, seamless experience |
| Aider, Codex, Gemini CLI, Continue | **CLI** | Lightweight, low context overhead, universal compatibility |
| Ollama + local models | **CLI** | Minimal context usage, works with any model size |

### Calling Priority

- **MCP-native tools** (Claude Code, Cursor): MCP first, CLI fallback
- **All other tools**: CLI first (MCP not needed)

> **Tip**: If your AI tool supports MCP, check whether `vmware-monitor` MCP server is loaded (`/mcp` in Claude Code). If not, configure it first — MCP provides the best hands-free experience.

## MCP Mode Configuration

For Claude Code / Cursor users who prefer structured tool calls, add to `~/.claude/settings.json`:

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

> v1.5.15+ recommends the single-command form `vmware-monitor mcp`. Pre-1.5.15 used
> `uvx --from vmware-monitor vmware-monitor-mcp`, which still works but re-resolves from <!-- install-pin: historical -->
> PyPI on each launch and breaks behind corporate TLS proxies. The legacy
> `vmware-monitor-mcp` entry point is also kept for backward compatibility.

MCP exposes 32 read-only tools:

| Group | Tools |
|---|---|
| Inventory | `list_virtual_machines`, `list_esxi_hosts`, `list_all_datastores`, `list_all_clusters`, `list_all_networks`, `resource_pool_usage` |
| Health & triage | `get_alarms`, `get_events`, `cluster_health_summary`, `cross_vcenter_attention` |
| VM detail | `vm_info`, `vm_list_snapshots`, `vm_performance`, `snapshot_aging` |
| Host detail | `host_performance`, `host_log_scan`, `get_host_sensors`, `get_host_services`, `ntp_status` |
| Platform state | `certificate_status`, `license_status`, `active_sessions`, `active_tasks`, `datastore_capacity` |
| Investigation bundles | `vm_investigation_bundle`, `host_investigation_bundle`, `datastore_investigation_bundle` |
| Patching & vSphere 9 | `cluster_patch_compliance`, `cluster_last_apply_result`, `host_memory_tiering`, `vcenter_deployment_size` |
| Backups | `vm_backup_snapshot_history` |

All accept an optional `target` parameter except `cross_vcenter_attention`, which
sweeps every configured target by design.

List-style tools take `limit` (default 50) to keep large inventories from flooding
context; `list_virtual_machines` additionally supports `sort_by`, `power_state`,
`fields`, and `folder_filter`.

### Password obfuscation at rest

On first load, any plaintext `*_PASSWORD` value in `.env` is automatically
rewritten to a grep-safe `b64:<encoded>` form and decoded transparently at
runtime, so a casual `grep` of the file no longer reveals the password. Values
are read and written through python-dotenv's own parser, so the stored secret
never drifts from what you configured (quotes, inline comments, and trailing
whitespace are handled correctly).

> **This is obfuscation, not encryption.** Anyone who can read the file can
> still decode it. For real secrecy at rest, do not store the password in `.env`
> at all — inject it from a secret manager (HashiCorp Vault, CyberArk, AWS
> Secrets Manager, or a Kubernetes Secret) into the `*_PASSWORD` environment
> variable at process start. The code reads the env var either way.

## Security

> **Disclaimer**: This is a community-maintained open-source project and is **not affiliated with, endorsed by, or sponsored by VMware, Inc. or Broadcom Inc.** "VMware" and "vSphere" are trademarks of Broadcom.

- **Read-Only toward vSphere**: This is an independent repository with zero destructive code paths. No power off, delete, create, reconfigure, or migrate functions exist in the codebase. Enforced by [`tests/eval/regression/test_read_only_enforcement.py`](https://github.com/vmware-skills/VMware-Monitor/blob/main/tests/eval/regression/test_read_only_enforcement.py) — in the source repository, not in this skill bundle — which allowlists every vSphere method the package calls against pyVmomi's own type metadata. It is a gate on source code, not a runtime block, and this repo has no CI — for defence that does not depend on it, use a least-privilege account (next item). Local files the skill writes are listed under [What "read-only" covers](#what-read-only-covers-and-what-it-writes-locally).
- **Least Privilege**: Give the skill a dedicated vCenter service account holding the built-in **Read-Only** role (propagated from the inventory root), not an administrator. That role has no privilege to change anything, so the read-only claim no longer rests on this code. Two reads need more than it grants: `active_sessions` reads the session list, which vCenter gates on `Sessions.TerminateSession` (a privilege that also allows ending sessions — without it the tool returns a one-row explanation instead); and ESXi host-log reads (`host_log_scan` and the daemon's host-log pass) call `BrowseDiagnosticLog`, gated on `Global.Diagnostics`. Grant those only if you need those reads.
- **Source Code**: Fully open source at [github.com/vmware-skills/VMware-Monitor](https://github.com/vmware-skills/VMware-Monitor) (MIT). The `uv` installer fetches the `vmware-monitor` package from PyPI, which is built from this GitHub repository. We recommend reviewing the source code and commit history before deploying in production.
- **TLS Verification**: Enabled by default. Setting `verify_ssl: false` is solely for ESXi hosts using self-signed certificates in isolated lab/home environments. In production, always use CA-signed certificates with full TLS verification.
- **Credentials & Config**: This skill requires the following secrets, all stored in `~/.vmware-monitor/.env` (`chmod 600`, loaded via `python-dotenv`):
  - `VMWARE_<TARGET>_PASSWORD` — per-target password where `<TARGET>` is the uppercased target name from `config.yaml` (hyphens become underscores). Example: target named `vcenter-prod` uses `VMWARE_VCENTER_PROD_PASSWORD`.
  - (Optional) Webhook URLs for Slack/Discord notifications

  The config file `~/.vmware-monitor/config.yaml` stores only target hostnames, ports, and usernames — it does **not** contain passwords or tokens. The env var `VMWARE_MONITOR_CONFIG` points to this YAML file.
- **Webhook Data Scope**: Webhook notifications are **disabled by default**. When enabled, the daemon posts to **user-configured URLs only** (Slack, Discord, or any HTTP endpoint you control); no data is sent to any other service. Each payload carries critical/warning counts plus every critical issue and every alarm/event warning from that scan — host-log warnings go to `scan.log` only, and `info` rows (unreadable or partly-read host logs) are never sent. Each issue carries the entity name and one of: the alarm name, vCenter event message (sanitized, ≤500 chars), ESXi log line matching critical/panic/corrupt (sanitized, ≤200 chars), or the error text for a target the daemon could not connect to. Event, log, and error text can contain host names, IP addresses, and user names — treat the webhook destination as receiving operational data. No credentials from the skill's config or `.env` are included.
- **Prompt Injection Protection**: All vSphere-sourced content (event messages, host logs) is truncated, stripped of control characters, and wrapped in boundary markers (`[VSPHERE_EVENT]`/`[VSPHERE_HOST_LOG]`) before output to prevent prompt injection when consumed by LLM agents.

## Supported AI Platforms

| Platform | Status | Config File |
|----------|--------|-------------|
| Claude Code | Native Skill | `skills/vmware-monitor/SKILL.md` |
| Gemini CLI | Context file + MCP | `skills/vmware-monitor/SKILL.md` |
| OpenAI Codex CLI | Skill + AGENTS.md | `skills/vmware-monitor/SKILL.md` |
| Aider | Conventions | `skills/vmware-monitor/SKILL.md` |
| Continue CLI | Rules | `skills/vmware-monitor/SKILL.md` |
| Trae IDE | Rules | `skills/vmware-monitor/SKILL.md` |
| Kimi Code CLI | Skill | `skills/vmware-monitor/SKILL.md` |
| MCP Server | MCP Protocol | `vmware_monitor/mcp_server/` |
| Python CLI | Standalone | N/A |

### MCP Server — Local Agent Compatibility

The MCP server works with any MCP-compatible agent via stdio transport. All 32 tools are **read-only** toward vSphere. Config templates in `examples/mcp-configs/`:

| Agent | Local Models | Config Template |
|-------|:----------:|-----------------|
| Goose (Block) | Ollama, LM Studio | `goose.json` |
| LocalCowork (Liquid AI) | Fully offline | `localcowork.json` |
| mcp-agent (LastMile AI) | Ollama, vLLM | `mcp-agent.yaml` |
| VS Code Copilot | — | `vscode-copilot.json` |
| Cursor | — | `cursor.json` |
| Continue | Ollama | `continue.yaml` |
| Claude Code | — | `claude-code.json` |

```bash
# Example: Aider + Ollama (fully local, no cloud API)
aider --conventions skills/vmware-monitor/SKILL.md --model ollama/qwen2.5-coder:32b
```

## First Interaction: Environment Selection

When the user starts a conversation, **always ask first**:

1. **Which environment** do they want to monitor? (vCenter Server or standalone ESXi host)
2. **Which target** from their config? (e.g., `prod-vcenter`, `lab-esxi`)
3. If no config exists yet, guide them through creating `~/.vmware-monitor/config.yaml`
