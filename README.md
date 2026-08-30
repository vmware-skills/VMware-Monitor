<!-- mcp-name: io.github.vmware-skills/vmware-monitor -->
# VMware Monitor

> **Author**: Wei Zhou, VMware by Broadcom — wei-wz.zhou@broadcom.com
> This is a community-driven project by a VMware engineer, not an official VMware product.
> For official VMware developer tools see [developer.broadcom.com](https://developer.broadcom.com).

English | [中文](README-CN.md)

**Read-only** VMware vCenter/ESXi monitoring — 32 tools. No destructive operations exist in this codebase, and a test enforces that.

> **Why a separate repository?** VMware Monitor is fully independent from [VMware-AIops](https://github.com/vmware-skills/VMware-AIops). No power off, delete, create, reconfigure, snapshot-create/revert/delete, clone, or migrate functions exist in this codebase — not a prompt constraint, an absence.
>
> **How that is enforced, precisely.** [`tests/eval/regression/test_read_only_enforcement.py`](tests/eval/regression/test_read_only_enforcement.py) parses every source file with `ast` and requires each vSphere method the package calls to appear on a reviewed allowlist, cross-checked against pyVmomi's own type metadata: anything returning a `vim.Task`, or gated by vCenter on a non-read privilege, fails unless a human wrote down why. Today that allowlist is nine methods. The check is a gate on the code as written — it cannot see a method name assembled at runtime, and nothing runs it automatically, so it holds only as far as someone runs the test suite. For a guarantee that does not depend on this repository at all, point the skill at a vCenter account with read-only permissions.

[![ClawHub](https://img.shields.io/badge/ClawHub-vmware--monitor-orange)](https://clawhub.ai/skills/vmware-monitor)
[![Skills.sh](https://img.shields.io/badge/Skills.sh-Install-blue)](https://skills.sh/vmware-skills/VMware-Monitor)
[![Claude Code Marketplace](https://img.shields.io/badge/Claude_Code-Marketplace-blueviolet)](https://github.com/vmware-skills/VMware-Monitor)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

### Companion Skills

| Skill | Scope | Tools | Install |
|-------|-------|:-----:|---------|
| **[vmware-aiops](https://github.com/vmware-skills/VMware-AIops)** ⭐ entry point | VM lifecycle, deployment, guest ops, clusters | 49 | `uv tool install vmware-aiops` |
| **[vmware-storage](https://github.com/vmware-skills/VMware-Storage)** | Datastores, iSCSI, vSAN | 11 | `uv tool install vmware-storage` |
| **[vmware-vks](https://github.com/vmware-skills/VMware-VKS)** | Tanzu Namespaces, TKC cluster lifecycle | 20 | `uv tool install vmware-vks` |
| **[vmware-nsx](https://github.com/vmware-skills/VMware-NSX)** | NSX networking: segments, gateways, NAT, IPAM | 33 | `uv tool install vmware-nsx-mgmt` |
| **[vmware-nsx-security](https://github.com/vmware-skills/VMware-NSX-Security)** | DFW microsegmentation, security groups, Traceflow | 21 | `uv tool install vmware-nsx-security` |
| **[vmware-aria](https://github.com/vmware-skills/VMware-Aria)** | Aria Ops metrics, alerts, capacity planning | 28 | `uv tool install vmware-aria` |
| **[vmware-avi](https://github.com/vmware-skills/VMware-AVI)** | AVI (NSX ALB) load balancing, AKO on Kubernetes | 28 | `uv tool install vmware-avi` |
| **[vmware-harden](https://github.com/vmware-skills/VMware-Harden)** | Compliance baselines, drift detection (read-only) | 6 | `uv tool install vmware-harden` |
| **[vmware-log-insight](https://github.com/vmware-skills/VMware-Log-Insight)** | Centralized syslog search, aggregation, alerts | 7 | `uv tool install vmware-log-insight` |
| **[vmware-debug](https://github.com/vmware-skills/VMware-Debug)** | Incident timeline correlation, root cause | 2 | `uv tool install vmware-debug` |
| **[vmware-pilot](https://github.com/vmware-skills/VMware-Pilot)** | Multi-step workflow orchestration, approval gates | 13 | `uv tool install vmware-pilot` |

## ⚡ Quick Investigation Reports

Five opinionated, read-only reports that answer an operator's real questions — each **aggregates and correlates server-side** and hands back a high-signal result (never raw inventory). Every report also renders a **self-contained offline HTML snapshot** with `--html` (no external assets, nothing leaves the machine; drill-down detail collapses in native `<details>` sections, zero JavaScript).

| Question | Command | What it correlates |
|----------|---------|--------------------|
| **"Is anything on fire?"** across all clusters | `vmware-monitor summary` | Every cluster's hosts + VM power + live CPU/mem + alarms → ranked top-N issues + per-cluster status |
| **"What needs attention now?"** across all vCenters | `vmware-monitor attention` | Every configured vCenter merged into one globally-ranked issue list; unreachable targets degrade gracefully |
| **"What's happening around this VM?"** | `vmware-monitor investigate vm <name>` | VM state + host it runs on + cluster + backing datastores + snapshots + alarms + performance + a merged event timeline |
| **"What's happening around this host?"** | `vmware-monitor investigate host <name>` | Host state + cluster + the VMs it runs + mounted datastores + alarms + performance + correlated timeline |
| **"What's happening around this datastore?"** | `vmware-monitor investigate datastore <name>` | Capacity/free + mounting hosts + VMs it backs + alarms + correlated timeline |

```bash
# Triage the estate, then drill into whatever it flags:
vmware-monitor attention                         # what needs attention now, all vCenters
vmware-monitor summary --top 5                   # is anything on fire, one vCenter
vmware-monitor investigate vm web-01 --hours 72  # everything around a VM, 72h event window
vmware-monitor investigate vm web-01 --html      # → offline snapshot in ~/vmware-health/
```

Unknown object names return a **teaching error** naming exactly how to list objects. Via MCP these are the tools `cluster_health_summary`, `cross_vcenter_attention`, `vm_investigation_bundle`, `host_investigation_bundle`, `datastore_investigation_bundle` — the model calls them and explains the aggregated result in operational language. Full flags: [`references/cli-reference.md`](skills/vmware-monitor/references/cli-reference.md).

### Quick Install (Recommended)

Works with Claude Code, Cursor, Codex, Gemini CLI, Trae, and 30+ AI agents:

```bash
# Via Skills.sh
npx skills add vmware-skills/VMware-Monitor

# Via ClawHub
clawhub install @zw008/vmware-monitor
```

### PyPI Install (No GitHub Access Required)

```bash
# Install via uv (recommended)
uv tool install vmware-monitor

# Or via pip
pip install vmware-monitor

# China mainland mirror (faster)
pip install vmware-monitor -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### Offline / Air-Gapped Install (from source)

This project uses the modern PEP 517 build system (hatchling), so there is **no
`setup.py`** by design — that is expected, not a missing file. If you cloned the
source and hit `ERROR: File "setup.py" or "setup.cfg" not found ... editable mode
currently requires a setuptools-based build`, your `pip` is older than 21.3 and
cannot do an *editable* (`-e`) install with a non-setuptools backend. Editable
mode is a developer convenience, not needed to run the tool — do one of:

```bash
# From the source tree — a normal (non-editable) install builds a wheel:
pip install .              # NOT  pip install -e .

# ...or upgrade pip first, and editable works too:
pip install --upgrade pip && pip install -e .
```

For a **truly air-gapped host**, build the wheels on a connected machine and copy
them over — the target then needs no network:

```bash
# On a connected machine, collect this package + its dependencies as wheels:
pip wheel . -w dist        # → dist/*.whl   (or: uv build, for just this package)

# Copy dist/ to the air-gapped host, then install offline:
pip install --no-index --find-links dist vmware-monitor
```

---

## Capabilities (Read-Only)

### Architecture

```
User (Natural Language)
  ↓
AI CLI Tool (Claude Code / Gemini / Codex / Aider / Continue / Trae / Kimi)
  ↓ Reads SKILL.md / AGENTS.md / rules
  ↓
vmware-monitor CLI (read-only)
  ↓ pyVmomi (vSphere SOAP API)
  ↓
vCenter Server ──→ ESXi Clusters ──→ VMs
    or
ESXi Standalone ──→ VMs
```

### Version Compatibility

| vSphere / VCF Version | Support | Notes |
|----------------|---------|-------|
| VCF 9.1 / vSphere 9.1 | ✅ Full | Released 2026-05-12. pyVmomi `<10.0` resolves and connects via SOAP. |
| VCF 9.0 / vSphere 9.0 | ✅ Full | pyVmomi 8.0.3+ connects against vSphere 9 SOAP API. |
| 8.0 / 8.0U1-U3 | ✅ Full | pyVmomi 8.0.3+ |
| 7.0 / 7.0U1-U3 | ✅ Full | All read-only APIs supported |
| 6.7 | ✅ Compatible | Backward-compatible, tested |
| 6.5 | ✅ Compatible | Backward-compatible, tested |

#### Official Broadcom References

- **SDKs**: <https://developer.broadcom.com/sdks> — VCF Python SDK (recommended for VCF 9+, bundles pyVmomi + vSAN SDK), vSphere Automation SDK for Python
- **REST APIs**: <https://developer.broadcom.com/xapis> — vSphere Automation API, VCF API
- **CLI Tools**: <https://developer.broadcom.com/tools> — PowerCLI 9.1, ESXCLI, OVF Tool

### 1. Inventory

| Feature | vCenter | ESXi | Details |
|---------|:-------:|:----:|---------|
| List VMs | ✅ | ✅ | Name, power state, CPU, memory, guest OS, IP, `folder_path` (vCenter inventory folder, e.g. `/Datacenters/Production/Web Tier`); MCP `list_virtual_machines` supports `folder_filter` for case-insensitive folder-tree search |
| List Hosts | ✅ | ⚠️ Self only | CPU cores, memory, ESXi version, VM count, uptime |
| List Datastores | ✅ | ✅ | Capacity, free/used, type (VMFS/NFS), usage % |
| List Clusters | ✅ | ❌ | Host count, DRS/HA status |
| List Networks | ✅ | ✅ | Network name, associated VM count, accessibility — CLI `inventory networks`, MCP `list_all_networks` |

### 2. Health & Monitoring

| Feature | vCenter | ESXi | Details |
|---------|:-------:|:----:|---------|
| Active Alarms | ✅ | ✅ | Severity, alarm name, entity, timestamp |
| Event/Log Query | ✅ | ✅ | Filter by time range, severity; 50+ event types |
| Hardware Sensors | ✅ | ✅ | Per-sensor `type` (temperature/voltage/fan...), reading, unit, and health `status` (green/yellow/red) — CLI `health sensors`, MCP `get_host_sensors` |
| Host Services | ✅ | ✅ | hostd, vpxa running/stopped status — CLI `health services`, MCP `get_host_services` |

**Monitored Event Types:**

| Category | Events |
|----------|--------|
| VM Failures | `VmFailedToPowerOnEvent`, `VmDiskFailedEvent`, `VmFailoverFailed` |
| Host Issues | `HostConnectionLostEvent`, `HostShutdownEvent`, `HostIpChangedEvent` |
| Storage | `DatastoreCapacityIncreasedEvent`, SCSI high latency |
| HA/DRS | `DasHostFailedEvent`, `DrsVmMigratedEvent`, `DrsSoftRuleViolationEvent` |
| Auth | `UserLoginSessionEvent`, `BadUsernameSessionEvent` |

### 3. VM Info & Snapshot List (Read-Only)

| Feature | Details |
|---------|---------|
| VM Info | Name, power state, guest OS, CPU, memory, IP, VMware Tools, disks, NICs, `folder_path` |
| Snapshot List | List existing snapshots with name and creation time (no create/revert/delete) — CLI `vm snapshot-list`, MCP tool `vm_list_snapshots` |
| Backup Window | How long backups held a snapshot open on a VM, from vCenter task history — CLI `snapshots backup-window`, MCP tool `vm_backup_snapshot_history`. A lower bound on the backup job, never its official duration |

### 4. Scheduled Scanning & Notifications

| Feature | Details |
|---------|---------|
| Daemon | APScheduler-based, configurable interval (default 15 min) |
| Multi-target Scan | Sequentially scan all configured vCenter/ESXi targets |
| Scan Content | Alarms + Events + Host logs (hostd, vmkernel, vpxd) |
| Log Analysis | Regex pattern matching: error, fail, critical, panic, timeout |
| Structured Log | JSONL output to `~/.vmware-monitor/scan.log` |
| Webhook | Slack, Discord, or any HTTP endpoint |
| Daemon Management | `daemon start/stop/status`, PID file, graceful shutdown |

### 5. Safety Features

| Feature | Details |
|---------|---------|
| **Code-Level Isolation** | Independent repository — zero destructive functions in codebase, checked by an AST allowlist gate over every vSphere call ([`tests/eval/regression/test_read_only_enforcement.py`](tests/eval/regression/test_read_only_enforcement.py)) |
| **Audit Trail** | All queries logged to `~/.vmware-monitor/audit.log` (JSONL) |
| **Password Protection** | `.env` file loading with permission check (warn if not 600) |
| **SSL Self-signed Support** | `verify_ssl: false` — only for ESXi with self-signed certs in isolated labs; production should use CA-signed certificates |
| **Prompt Injection Protection** | vSphere event messages and host logs are truncated, sanitized, and wrapped in boundary markers |
| **Webhook Data Scope** | Sends monitoring summaries to user-configured URLs only — no third-party services by default |
| **Production Recommended** | AI agents can misinterpret context and execute unintended destructive operations — real-world incidents have shown AI-driven tools deleting production databases and entire environments. VMware-Monitor removes that class of risk from its own code: no destructive code paths exist, and the allowlist gate fails the build if one is added. Pair it with a read-only vCenter account for defence that does not rely on this codebase. Use [VMware-AIops](https://github.com/vmware-skills/VMware-AIops) only in dev/lab environments |

### What's NOT Included (By Design)

These operations **do not exist** in this repository:

- ❌ Power on/off, reset, suspend VMs
- ❌ Create, delete, reconfigure VMs
- ❌ Create, revert, delete snapshots
- ❌ Clone or migrate VMs
- ❌ `_double_confirm`, `_show_state_preview`, `_validate_vm_params`

For these operations, use the full [VMware-AIops](https://github.com/vmware-skills/VMware-AIops) repository.

---

Running with local or small models? See [`skills/vmware-monitor/references/agent-guardrails.md`](skills/vmware-monitor/references/agent-guardrails.md).

---

## Common Workflows

### Daily Health Check

1. Check alarms: `vmware-monitor health alarms --target prod-vcenter`
2. Review recent events: `vmware-monitor health events --hours 24 --severity warning`
3. List hosts: `vmware-monitor inventory hosts` — check connection state and memory usage

### Investigate a Specific Object (drill-down)

One call correlates the object with its surrounding infrastructure and recent history — see [⚡ Quick Investigation Reports](#-quick-investigation-reports) above.

1. Start from triage: `vmware-monitor attention` (all vCenters) or `vmware-monitor summary` (one)
2. Drill into what it flags: `vmware-monitor investigate vm <name>` (or `host` / `datastore`)
3. Widen the event window with `--hours 72`; share it with `--html` (offline snapshot)
4. **If the name is unknown** → the teaching error names how to list objects (`inventory vms/hosts`, `list_all_datastores`)

### Set Up Continuous Monitoring

1. Configure webhook in `~/.vmware-monitor/config.yaml`
2. Start daemon: `vmware-monitor daemon start`
3. Daemon scans every 15 min, sends alerts to Slack/Discord

---

## Troubleshooting

### Alarms returns empty but vCenter shows alarms

The `get_alarms` tool queries triggered alarms at the root folder level. Some alarms are entity-specific — try checking events instead: `vmware-monitor health events --hours 1 --severity info`.

### "Connection refused" error

1. Run `vmware-monitor doctor` to diagnose
2. Verify target hostname/IP and port (443) in `config.yaml`
3. For self-signed certs: set `verify_ssl: false`

### Events returns too many results

Use severity filter: `--severity warning` (default) filters out info-level events. Use `--hours 4` to narrow the time range.

### VM info shows "guest_os: unknown"

VMware Tools not installed or not running in the guest. Install/start VMware Tools for guest OS detection, IP address, and guest family info.

### Doctor passes but commands fail with timeout

vCenter may be under heavy load. Try targeting a specific ESXi host directly instead of vCenter, or increase connection timeout in `config.yaml`.

---

## Supported AI Platforms

| Platform | Status | Config File | AI Model |
|----------|--------|-------------|----------|
| **Claude Code** | ✅ Native Skill | `skills/vmware-monitor/SKILL.md` | Anthropic Claude |
| **Gemini CLI** | ✅ Context file + MCP | `skills/vmware-monitor/SKILL.md` | Google Gemini |
| **OpenAI Codex CLI** | ✅ Skill + AGENTS.md | `skills/vmware-monitor/SKILL.md` | OpenAI GPT |
| **Aider** | ✅ Conventions | `skills/vmware-monitor/SKILL.md` | Any (cloud + local) |
| **Continue CLI** | ✅ Rules | `skills/vmware-monitor/SKILL.md` | Any (cloud + local) |
| **Trae IDE** | ✅ Rules | `skills/vmware-monitor/SKILL.md` | Claude/DeepSeek/GPT-4o |
| **Kimi Code CLI** | ✅ Skill | `skills/vmware-monitor/SKILL.md` | Moonshot Kimi |
| **MCP Server** | ✅ MCP Protocol | `vmware_monitor/mcp_server/` | Any MCP client |
| **Python CLI** | ✅ Standalone | N/A | N/A |

### Platform Comparison

| Feature | Claude Code | Gemini CLI | Codex CLI | Aider | Continue | Trae IDE | Kimi CLI |
|---------|-------------|------------|-----------|-------|----------|----------|----------|
| Cloud AI | Anthropic | Google | OpenAI | Any | Any | Multi | Moonshot |
| Local models | — | — | — | Ollama | Ollama | — | — |
| Skill system | SKILL.md | Context file | SKILL.md | — | Rules | Rules | SKILL.md |
| MCP support | Native | Native | Via Skills | Third-party | Native | — | — |
| Free tier | — | 60 req/min | — | Self-hosted | Self-hosted | — | — |

### MCP Server Integrations

The vmware-monitor MCP server works with **any MCP-compatible agent or tool**. Ready-to-use configuration templates are in [`examples/mcp-configs/`](examples/mcp-configs/). All 32 tools are **read-only**, enforced by the allowlist gate described above.

| Agent / Tool | Local Model Support | Config Template | Integration Guide |
|-------------|:-------------------:|-----------------|-------------------|
| **[Xiaoguai (小怪)](https://github.com/xiaoguai-agent/xiaoguai)** | ✅ Self-hosted, any LLM | [MCP setup](https://github.com/xiaoguai-agent/xiaoguai/blob/main/docs/book/src/api/mcp.md) | [Guide](https://github.com/xiaoguai-agent/xiaoguai) |
| **[Goose](https://github.com/block/goose)** | ✅ Ollama, LM Studio | [`goose.json`](examples/mcp-configs/goose.json) | [Guide](docs/integrations/goose.md) |
| **[LocalCowork](https://github.com/Liquid4All/localcowork)** | ✅ Fully offline | [`localcowork.json`](examples/mcp-configs/localcowork.json) | [Guide](docs/integrations/localcowork.md) |
| **[mcp-agent](https://github.com/lastmile-ai/mcp-agent)** | ✅ Ollama, vLLM | [`mcp-agent.yaml`](examples/mcp-configs/mcp-agent.yaml) | [Guide](docs/integrations/mcp-agent.md) |
| **VS Code Copilot** | — | [`vscode-copilot.json`](examples/mcp-configs/vscode-copilot.json) | [Guide](docs/integrations/vscode-copilot.md) |
| **Cursor** | — | [`cursor.json`](examples/mcp-configs/cursor.json) | — |
| **Continue** | ✅ Ollama | [`continue.yaml`](examples/mcp-configs/continue.yaml) | [Guide](docs/integrations/continue.md) |
| **Claude Code** | — | [`claude-code.json`](examples/mcp-configs/claude-code.json) | — |

> **[Xiaoguai (小怪)](https://github.com/xiaoguai-agent/xiaoguai)** — a self-hostable, audit-first agent platform (Rust, single binary + embedded SQLite) from the same maintainer. It runs the read-only vmware-monitor MCP server as one of its toolboxes; being both an MCP *consumer* and an MCP *server*, its HMAC-chained audit log pairs naturally with this skill's read-only design — every query is logged, and no code path here mutates. See its [MCP integration guide](https://github.com/xiaoguai-agent/xiaoguai/blob/main/docs/book/src/api/mcp.md).

**Fully local operation** (no cloud API required):

```bash
# Aider + Ollama + vmware-monitor (via SKILL.md)
aider --conventions skills/vmware-monitor/SKILL.md --model ollama/qwen2.5-coder:32b

# Any MCP agent + local model + vmware-monitor MCP server
# See examples/mcp-configs/ for your agent's config format
```

---

## Installation

### Step 0: Prerequisites

```bash
# Python 3.10+ required
python3 --version

# Node.js 18+ required for Gemini CLI and Codex CLI
node --version
```

### Step 1: Clone & Install Python Backend

```bash
git clone https://github.com/vmware-skills/VMware-Monitor.git
cd VMware-Monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Step 2: Configure

```bash
mkdir -p ~/.vmware-monitor
cp config.example.yaml ~/.vmware-monitor/config.yaml
# Edit config.yaml with your vCenter/ESXi targets
```

Set passwords via `.env` file (recommended):
```bash
cp .env.example ~/.vmware-monitor/.env
chmod 600 ~/.vmware-monitor/.env
# Edit and fill in your passwords
```

> **Security note**: Prefer `.env` file over command-line `export` to avoid passwords appearing in shell history. `config.yaml` stores only hostnames, ports, and a reference to the `.env` file — it does **not** contain passwords or tokens. All secrets are stored exclusively in `.env` (`chmod 600`). Webhook notifications are disabled by default; when enabled, payloads contain no credentials, IPs, or PII — only aggregated alert metadata sent to user-configured URLs only. We recommend using a least-privilege read-only vCenter service account.

Password environment variable naming convention:
```
VMWARE_{TARGET_NAME_UPPER}_PASSWORD
# Replace hyphens with underscores, UPPERCASE
# Example: target "home-esxi" → VMWARE_HOME_ESXI_PASSWORD
# Example: target "prod-vcenter" → VMWARE_PROD_VCENTER_PASSWORD
```

### Step 3: Connect Your AI Tool

Choose one (or more) of the following:

---

#### Option A: Claude Code

**Method 1: Skills.sh or ClawHub (recommended)**

Either installer places the skill in Claude Code's skills directory for you:

```bash
npx skills add vmware-skills/VMware-Monitor
# or
clawhub install @zw008/vmware-monitor
```

**Method 2: Manual skill install**

```bash
git clone https://github.com/vmware-skills/VMware-Monitor.git
cd VMware-Monitor

# Copy the skill into Claude Code's personal skills directory
mkdir -p ~/.claude/skills/vmware-monitor
cp -r skills/vmware-monitor/. ~/.claude/skills/vmware-monitor/
```

For tool access (not just skill context), also register the MCP server:

```bash
claude mcp add vmware-monitor -- vmware-monitor mcp
```

Restart Claude Code, then:
```
> Show me all VMs on esxi-lab.example.com
```

---

#### Option B: Gemini CLI

```bash
# Install Gemini CLI
npm install -g @google/gemini-cli

# Load the skill as project context (Gemini CLI reads GEMINI.md on startup)
cp skills/vmware-monitor/SKILL.md ./GEMINI.md
```

For tool access (not just context), register the MCP server in `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "vmware-monitor": {
      "command": "vmware-monitor",
      "args": ["mcp"],
      "env": { "VMWARE_MONITOR_CONFIG": "~/.vmware-monitor/config.yaml" }
    }
  }
}
```

Then start Gemini CLI:
```
gemini
> Show me all VMs on my ESXi host
```

---

#### Option C: OpenAI Codex CLI

```bash
# Install Codex CLI
npm i -g @openai/codex
# Or on macOS:
# brew install --cask codex

# Copy skill to Codex skills directory
mkdir -p ~/.codex/skills/vmware-monitor
cp skills/vmware-monitor/SKILL.md ~/.codex/skills/vmware-monitor/SKILL.md

# Copy AGENTS.md to project root
cp skills/vmware-monitor/SKILL.md ./AGENTS.md
```

Then start Codex CLI:
```bash
codex --enable skills
> List all VMs on my ESXi
```

---

#### Option D: Aider (supports local models)

```bash
# Install Aider
pip install aider-chat

# Install Ollama for local models (optional)
# macOS:
brew install ollama
ollama pull qwen2.5-coder:32b

# Run with cloud API
aider --conventions skills/vmware-monitor/SKILL.md

# Or with local model via Ollama
aider --conventions skills/vmware-monitor/SKILL.md \
  --model ollama/qwen2.5-coder:32b
```

---

#### Option E: Continue CLI (supports local models)

```bash
# Install Continue CLI
npm i -g @continuedev/cli

# Copy rules file
mkdir -p .continue/rules
cp skills/vmware-monitor/SKILL.md .continue/rules/vmware-monitor.md
```

Configure `~/.continue/config.yaml` for local model:
```yaml
models:
  - name: local-coder
    provider: ollama
    model: qwen2.5-coder:32b
```

Then:
```bash
cn
> Check ESXi health and alarms
```

---

#### Option F: Trae IDE

Copy the rules file to your project's `.trae/rules/` directory:

```bash
mkdir -p .trae/rules
cp skills/vmware-monitor/SKILL.md .trae/rules/project_rules.md
```

Trae IDE's Builder Mode reads `.trae/rules/` Markdown files at startup.

> **Note**: You can also install Claude Code extension in Trae IDE and use `.claude/skills/` format directly.

---

#### Option G: Kimi Code CLI

```bash
# Copy skill file to Kimi skills directory
mkdir -p ~/.kimi/skills/vmware-monitor
cp skills/vmware-monitor/SKILL.md ~/.kimi/skills/vmware-monitor/SKILL.md
```

---

#### Option H: MCP Server (Glama / Claude Desktop)

The MCP server exposes VMware read-only monitoring as tools via the [Model Context Protocol](https://modelcontextprotocol.io). Works with any MCP-compatible client (Claude Desktop, Cursor, etc.).

**After `uv tool install vmware-monitor`, start the MCP server with one command** (v1.5.15+):

```bash
# Recommended — single command, no network re-resolve
vmware-monitor mcp

# With a custom config path
VMWARE_MONITOR_CONFIG=/path/to/config.yaml vmware-monitor mcp
```

**Claude Desktop config** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "vmware-monitor": {
      "command": "vmware-monitor",
      "args": ["mcp"],
      "env": {
        "VMWARE_MONITOR_CONFIG": "/path/to/config.yaml"
      }
    }
  }
}
```

<details>
<summary>Alternative: uvx (no install) or legacy entry point</summary>

```bash
# Run without installing (requires PyPI access each launch)
uvx --from vmware-monitor vmware-monitor mcp

# Legacy entry point (still works, kept for backward compatibility)
vmware-monitor-mcp
```

> **Behind a corporate TLS proxy?** uvx may fail with `invalid peer certificate: UnknownIssuer`.
> Use the recommended `vmware-monitor mcp` form above (no network needed), or set `UV_NATIVE_TLS=true`.

</details>


---

#### Option I: Standalone CLI (no AI)

```bash
# Already installed in Step 1
source .venv/bin/activate

vmware-monitor inventory vms --target home-esxi
vmware-monitor health alarms --target home-esxi
vmware-monitor vm info my-vm --target home-esxi
```

---

## Update / Upgrade

Already installed? Re-run the install command for your channel to get the latest version:

| Install Channel | Update Command |
|----------------|----------------|
| ClawHub | `clawhub install @zw008/vmware-monitor` |
| Skills.sh | `npx skills add vmware-skills/VMware-Monitor` |
| Git clone | `cd VMware-Monitor && git pull origin main && uv pip install -e .` |
| uv | `uv tool install vmware-monitor --force` |

Check your current version: `vmware-monitor --version`

---

## Chinese Cloud Models

For users in China who prefer domestic cloud APIs or have limited access to overseas services.

### DeepSeek

```bash
export DEEPSEEK_API_KEY="your-key"
aider --conventions skills/vmware-monitor/SKILL.md \
  --model deepseek/deepseek-coder
```

### Qwen (Alibaba Cloud)

```bash
export DASHSCOPE_API_KEY="your-key"
aider --conventions skills/vmware-monitor/SKILL.md \
  --model qwen/qwen-coder-plus
```

### Local Models (Aider + Ollama)

For fully offline operation — no cloud API, no internet, full privacy.

```bash
brew install ollama
ollama pull qwen2.5-coder:32b
ollama serve

aider --conventions skills/vmware-monitor/SKILL.md \
  --model ollama/qwen2.5-coder:32b
```

---

## CLI Reference

```bash
# Diagnostics
vmware-monitor doctor                   # Check environment, config, connectivity
vmware-monitor doctor --skip-auth       # Skip vSphere auth check (faster)

# MCP Config Generator
vmware-monitor mcp-config generate --agent goose        # Generate config for Goose
vmware-monitor mcp-config generate --agent claude-code  # Generate config for Claude Code
vmware-monitor mcp-config list                          # List all supported agents

# Inventory
vmware-monitor inventory vms [--target <name>]
vmware-monitor inventory vms --limit 10 --sort-by memory_mb   # Top 10 VMs by memory
vmware-monitor inventory vms --power-state poweredOn           # Only powered-on VMs
vmware-monitor inventory vms --sort-by folder_path             # Group VMs by inventory folder
# All `inventory vms` results include a `folder_path` field (e.g. `/Datacenters/Production/Web Tier`).
# MCP tool `list_virtual_machines` additionally supports `folder_filter="Production"` for case-insensitive folder-tree search.
vmware-monitor inventory hosts [--target <name>]
vmware-monitor inventory datastores [--target <name>]
vmware-monitor inventory clusters [--target <name>]

# Health
vmware-monitor health alarms [--target <name>]
vmware-monitor health events [--hours 24] [--severity warning]

# VM Info (read-only)
vmware-monitor vm info <vm-name>
vmware-monitor vm snapshot-list <vm-name>

# Scanning & Daemon
vmware-monitor scan now [--target <name>]
vmware-monitor daemon start
vmware-monitor daemon stop
vmware-monitor daemon status
```

---

## Configuration

See `config.example.yaml` for all options.

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| targets | name | — | Friendly name |
| targets | host | — | vCenter/ESXi hostname or IP |
| targets | type | vcenter | `vcenter` or `esxi` |
| targets | port | 443 | Connection port |
| targets | verify_ssl | true | Verify the target's TLS certificate (set false only for self-signed lab hosts) |
| scanner | interval_minutes | 15 | Scan frequency |
| scanner | severity_threshold | warning | Min severity: critical/warning/info |
| scanner | lookback_hours | 1 | How far back to scan |
| notify | log_file | ~/.vmware-monitor/scan.log | JSONL log output |
| notify | webhook_url | — | Webhook endpoint (Slack, Discord, etc.) |

---

## Project Structure

```
VMware-Monitor/
├── skills/                        # Skills index (npx skills add)
│   └── vmware-monitor/
│       ├── SKILL.md
│       └── references/            # Detailed docs loaded on-demand
├── vmware_monitor/                # Python backend (read-only only)
│   ├── config.py                  # YAML + .env config
│   ├── connection.py              # Multi-target pyVmomi
│   ├── cli.py                     # Typer CLI (read-only commands only)
│   ├── ops/
│   │   ├── inventory.py           # VMs, hosts, datastores, clusters
│   │   ├── health.py              # Alarms, events, sensors
│   │   └── vm_info.py             # VM info, snapshot list (read-only)
│   ├── scanner/                   # Log scanning daemon
│   ├── notify/                    # Notifications (JSONL + webhook)
│   └── mcp_server/                # MCP server (read-only tools only)
├── examples/mcp-configs/          # MCP client config templates
├── tests/                         # Test suite
├── smithery.yaml                  # Smithery marketplace config
├── RELEASE_NOTES.md
├── config.example.yaml
└── pyproject.toml
```

## Related Projects

| Skill | Scope | Tools | Install |
|-------|-------|:-----:|---------|
| **[vmware-monitor](https://github.com/vmware-skills/VMware-Monitor)** | Read-only monitoring, alarms, events | 27 | `uv tool install vmware-monitor` |
| **[vmware-aiops](https://github.com/vmware-skills/VMware-AIops)** | VM lifecycle, deployment, guest ops, clusters | 49 | `uv tool install vmware-aiops` |
| **[vmware-storage](https://github.com/vmware-skills/VMware-Storage)** | Datastores, iSCSI, vSAN | 11 | `uv tool install vmware-storage` |
| **[vmware-vks](https://github.com/vmware-skills/VMware-VKS)** | Tanzu Namespaces, TKC cluster lifecycle | 20 | `uv tool install vmware-vks` |

---

## Troubleshooting & Contributing

If you encounter any errors or issues, please send the error message, logs, or screenshots to **zhouwei008@gmail.com**. Contributions are welcome!

## License

MIT
