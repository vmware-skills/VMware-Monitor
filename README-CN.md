<!-- mcp-name: io.github.vmware-skills/vmware-monitor -->
# VMware Monitor

> **作者**: Wei Zhou, VMware by Broadcom — wei-wz.zhou@broadcom.com
> 本项目由 VMware 工程师维护的社区项目，非 VMware 官方产品。
> VMware 官方开发者工具请访问 [developer.broadcom.com](https://developer.broadcom.com)。

[English](README.md) | 中文

**只读** VMware vCenter/ESXi 监控 — 27 个工具，代码级安全保障。代码库中不存在任何破坏性操作。

> **为什么独立仓库？** VMware Monitor 完全独立于 [VMware-AIops](https://github.com/vmware-skills/VMware-AIops)。安全性在**代码级别**保障：代码库中不存在关机、删除、创建、调整配置、快照创建/恢复/删除、克隆、迁移等函数。不仅仅是提示词约束 — 而是零破坏性代码路径。

[![ClawHub](https://img.shields.io/badge/ClawHub-vmware--monitor-orange)](https://clawhub.ai/skills/vmware-monitor)
[![Skills.sh](https://img.shields.io/badge/Skills.sh-Install-blue)](https://skills.sh/vmware-skills/VMware-Monitor)
[![Claude Code Marketplace](https://img.shields.io/badge/Claude_Code-Marketplace-blueviolet)](https://github.com/vmware-skills/VMware-Monitor)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

### 配套技能

| 技能 | 范围 | 工具数 | 安装 |
|------|------|:-----:|------|
| **[vmware-aiops](https://github.com/vmware-skills/VMware-AIops)** ⭐ 统一入口 | VM 生命周期、部署、Guest Ops、集群 | 49 | `uv tool install vmware-aiops` |
| **[vmware-storage](https://github.com/vmware-skills/VMware-Storage)** | 数据存储、iSCSI、vSAN | 11 | `uv tool install vmware-storage` |
| **[vmware-vks](https://github.com/vmware-skills/VMware-VKS)** | Tanzu 命名空间、TKC 集群生命周期 | 20 | `uv tool install vmware-vks` |
| **[vmware-nsx](https://github.com/vmware-skills/VMware-NSX)** | NSX 网络：段、网关、NAT、IPAM | 33 | `uv tool install vmware-nsx-mgmt` |
| **[vmware-nsx-security](https://github.com/vmware-skills/VMware-NSX-Security)** | DFW 微分段、安全组、Traceflow | 21 | `uv tool install vmware-nsx-security` |
| **[vmware-aria](https://github.com/vmware-skills/VMware-Aria)** | Aria Ops 指标、告警、容量规划 | 28 | `uv tool install vmware-aria` |
| **[vmware-avi](https://github.com/vmware-skills/VMware-AVI)** | AVI (NSX ALB) 负载均衡、Kubernetes AKO | 28 | `uv tool install vmware-avi` |
| **[vmware-harden](https://github.com/vmware-skills/VMware-Harden)** | 合规基线、Drift 检测（只读） | 6 | `uv tool install vmware-harden` |
| **[vmware-log-insight](https://github.com/vmware-skills/VMware-Log-Insight)** | 集中日志检索、聚合、告警 | 7 | `uv tool install vmware-log-insight` |
| **[vmware-debug](https://github.com/vmware-skills/VMware-Debug)** | 故障时间线关联、根因定位 | 2 | `uv tool install vmware-debug` |
| **[vmware-pilot](https://github.com/vmware-skills/VMware-Pilot)** | 多步骤工作流编排、审批门控 | 13 | `uv tool install vmware-pilot` |

## ⚡ 快速调查报告

五个有主见的只读报告，直接回答运维最关心的问题——每个都在**服务端聚合并关联**数据，返回高信号结果（绝不把 raw inventory 灌给模型）。每个报告都支持 `--html` 生成**自包含离线 HTML 快照**（零外链、内网主机名不外泄；下钻细节用原生 `<details>` 折叠，零 JavaScript）。

| 问题 | 命令 | 关联什么 |
|------|------|---------|
| **"有什么着火了？"**（全集群） | `vmware-monitor summary` | 每个集群的主机 + VM 电源 + 实时 CPU/内存 + 告警 → Top-N 异常榜 + 每集群状态 |
| **"现在该关注什么？"**（全 vCenter） | `vmware-monitor attention` | 所有 vCenter 合并成一个全局排序的异常榜；连不上的目标优雅降级 |
| **"这个 VM 周围在发生什么？"** | `vmware-monitor investigate vm <名>` | VM 状态 + 所在主机 + 集群 + 背后数据存储 + 快照 + 告警 + 性能 + 合并事件时间线 |
| **"这个主机周围在发生什么？"** | `vmware-monitor investigate host <名>` | 主机状态 + 集群 + 其上的 VM + 挂载的数据存储 + 告警 + 性能 + 关联时间线 |
| **"这个数据存储周围在发生什么？"** | `vmware-monitor investigate datastore <名>` | 容量/剩余 + 挂载它的主机 + 落在其上的 VM + 告警 + 关联时间线 |

```bash
# 先总览，再下钻到它指出的对象：
vmware-monitor attention                          # 现在该关注什么，全 vCenter
vmware-monitor summary --top 5                    # 有什么着火了，单 vCenter
vmware-monitor investigate vm web-01 --hours 72   # 一个 VM 的全貌，72 小时事件窗口
vmware-monitor investigate vm web-01 --html       # → 离线快照写入 ~/vmware-health/
```

对象名不存在时返回**教学性错误**，明确告诉你如何列出对象。MCP 下对应工具为 `cluster_health_summary`、`cross_vcenter_attention`、`vm_investigation_bundle`、`host_investigation_bundle`、`datastore_investigation_bundle`——模型调用后用运维语言解释聚合结果。完整参数见 [`references/cli-reference.md`](skills/vmware-monitor/references/cli-reference.md)。

### 快速安装（推荐）

支持 Claude Code、Cursor、Codex、Gemini CLI、Trae 等 30+ AI 工具：

```bash
# 通过 Skills.sh 安装
npx skills add vmware-skills/VMware-Monitor

# 通过 ClawHub 安装
clawhub install @zw008/vmware-monitor
```

### PyPI 安装（无需访问 GitHub）

```bash
# 通过 uv 安装（推荐）
uv tool install vmware-monitor

# 或通过 pip 安装
pip install vmware-monitor

# 国内镜像加速
pip install vmware-monitor -i https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## 功能总览（只读）

### 架构

```
用户 (自然语言)
  ↓
AI CLI 工具 (Claude Code / Gemini / Codex / Aider / Continue / Trae / Kimi)
  ↓ 读取 SKILL.md / AGENTS.md / rules 指令
  ↓
vmware-monitor CLI（只读）
  ↓ pyVmomi (vSphere SOAP API)
  ↓
vCenter Server ──→ ESXi 集群 ──→ VM
    或
ESXi 独立主机 ──→ VM
```

### 版本兼容性

| vSphere 版本 | 支持状态 | 说明 |
|-------------|---------|------|
| 8.0 / 8.0U1-U3 | ✅ 完全支持 | pyVmomi 8.0.3+ |
| 7.0 / 7.0U1-U3 | ✅ 完全支持 | 所有只读 API 正常工作 |
| 6.7 | ✅ 兼容 | 向后兼容，已测试 |
| 6.5 | ✅ 兼容 | 向后兼容，已测试 |

### 1. 资源清单

| 功能 | vCenter | ESXi | 说明 |
|------|:-------:|:----:|------|
| 列出虚拟机 | ✅ | ✅ | 名称、电源状态、CPU、内存、操作系统、IP、`folder_path`（vCenter 清单文件夹路径，如 `/Datacenters/Production/Web Tier`）；MCP `list_virtual_machines` 支持 `folder_filter` 参数进行大小写不敏感的文件夹树搜索 |
| 列出主机 | ✅ | ⚠️ 仅自身 | CPU 核数、内存、版本、VM 数、在线时间 |
| 列出数据存储 | ✅ | ✅ | 容量、已用/可用、类型、使用率 |
| 列出集群 | ✅ | ❌ | 主机数、DRS/HA 状态 |
| 列出网络 | ✅ | ✅ | 网络名、关联 VM 数、可访问性 — CLI `inventory networks`，MCP `list_all_networks` |

### 2. 健康监控

| 功能 | vCenter | ESXi | 说明 |
|------|:-------:|:----:|------|
| 活跃告警 | ✅ | ✅ | 严重级别、告警名、实体、时间 |
| 事件日志查询 | ✅ | ✅ | 按时间、严重级别过滤，识别 50+ 事件类型 |
| 硬件传感器 | ✅ | ✅ | 每个传感器的 `type`（温度/电压/风扇等）、读数、单位及健康 `status`（green/yellow/red） — CLI `health sensors`，MCP `get_host_sensors` |
| 主机服务状态 | ✅ | ✅ | 服务运行/停止状态 — CLI `health services`，MCP `get_host_services` |

### 3. VM 信息与快照列表（只读）

| 功能 | 说明 |
|------|------|
| VM 详情 | 名称、电源状态、操作系统、CPU、内存、IP、VMware Tools、磁盘、网卡、`folder_path` |
| 快照列表 | 列出已有快照名称和创建时间（无创建/恢复/删除）— CLI `vm snapshot-list`，MCP 工具 `vm_list_snapshots` |

### 4. 定时扫描与通知

| 功能 | 说明 |
|------|------|
| 守护进程 | 基于 APScheduler，可配置间隔（默认 15 分钟） |
| 多目标扫描 | 依次扫描所有配置的 vCenter/ESXi 目标 |
| 日志分析 | 正则匹配：error, fail, critical, panic, timeout, corrupt |
| 结构化日志 | JSONL 输出到 `~/.vmware-monitor/scan.log` |
| Webhook 通知 | 支持 Slack、Discord 或任意 HTTP 端点 |

### 5. 安全特性

| 功能 | 说明 |
|------|------|
| **代码级隔离** | 独立仓库 — 代码中零破坏性函数 |
| **审计日志** | 所有查询记录到 `~/.vmware-monitor/audit.log`（JSONL） |
| **密码保护** | 通过 `.env` 加载密码并检查文件权限（warn if not 600） |
| **配置文件内容** | `config.yaml` 仅存储主机名、端口和 `.env` 引用路径，**不含密码或 Token** |
| **SSL 自签名** | 仅用于 ESXi 自签名证书的隔离实验环境；生产环境应使用 CA 签名证书 |
| **Prompt 注入防护** | vSphere 事件消息和主机日志在输出前进行截断、控制字符清理和边界标记（`[VSPHERE_EVENT]`/`[VSPHERE_HOST_LOG]`）包裹 |
| **Webhook 数据范围** | **默认禁用**。启用后仅向用户自配置的 URL 发送告警摘要，payload 不含凭据、IP 或 PII |
| **生产环境推荐** | AI Agent 可能误解上下文并执行非预期的破坏性操作 — 已有真实案例表明 AI 驱动工具删除了生产数据库和整个环境。VMware-Monitor 消除此风险：代码中不存在任何破坏性代码路径。仅在开发/实验环境使用 [VMware-AIops](https://github.com/vmware-skills/VMware-AIops) |

### 不包含的操作（设计如此）

以下操作在本仓库中**不存在**：

- ❌ 开关机、重置、挂起
- ❌ 创建、删除、调整配置
- ❌ 创建/恢复/删除快照
- ❌ 克隆、迁移

需要这些操作请使用 [VMware-AIops](https://github.com/vmware-skills/VMware-AIops)。

---

使用本地/小模型运行？参见 [`skills/vmware-monitor/references/agent-guardrails.md`](skills/vmware-monitor/references/agent-guardrails.md)。

---

## 常用工作流

### 日常健康检查

1. 检查告警：`vmware-monitor health alarms --target prod-vcenter`
2. 查看近期事件：`vmware-monitor health events --hours 24 --severity warning`
3. 列出主机：`vmware-monitor inventory hosts` — 检查连接状态和内存使用

### 排查特定虚拟机

1. 查找 VM：`vmware-monitor inventory vms --power-state poweredOff`
2. 获取详情：`vmware-monitor vm info problem-vm`
3. 查看相关事件：`vmware-monitor health events --hours 48`

### 设置持续监控

1. 在 `~/.vmware-monitor/config.yaml` 中配置 webhook
2. 启动守护进程：`vmware-monitor daemon start`
3. 守护进程每 15 分钟扫描一次，向 Slack/Discord 发送告警

---

## 故障排查

### 告警返回为空但 vCenter 显示有告警

`get_alarms` 工具在根文件夹级别查询已触发的告警。部分告警是实体级别的 — 尝试改用事件查询：`vmware-monitor health events --hours 1 --severity info`。

### "Connection refused" 错误

1. 运行 `vmware-monitor doctor` 进行诊断
2. 检查 `config.yaml` 中的目标主机名/IP 和端口（443）
3. 自签名证书场景：设置 `verify_ssl: false`

### 事件返回过多

使用严重级别过滤：`--severity warning`（默认）会过滤掉 info 级别事件。使用 `--hours 4` 缩小时间范围。

### VM 信息显示 "guest_os: unknown"

客户机中未安装或未运行 VMware Tools。安装/启动 VMware Tools 以获取操作系统检测、IP 地址和客户机系列信息。

### Doctor 通过但命令超时

vCenter 可能负载过高。尝试直接连接特定 ESXi 主机而非 vCenter，或在 `config.yaml` 中增加连接超时时间。

---

## 支持的 AI 平台

| 平台 | 状态 | 配置文件 | AI 模型 |
|------|------|---------|---------|
| **Claude Code** | ✅ 原生技能 | `skills/vmware-monitor/SKILL.md` | Anthropic Claude |
| **Gemini CLI** | ✅ 上下文文件 + MCP | `skills/vmware-monitor/SKILL.md` | Google Gemini |
| **Codex CLI** | ✅ Skill + AGENTS.md | `skills/vmware-monitor/SKILL.md` | OpenAI GPT |
| **Aider** | ✅ 约定文件 | `skills/vmware-monitor/SKILL.md` | 任意（云端 + 本地） |
| **Continue CLI** | ✅ 规则文件 | `skills/vmware-monitor/SKILL.md` | 任意（云端 + 本地） |
| **Trae IDE** | ✅ Rules | `skills/vmware-monitor/SKILL.md` | Claude/DeepSeek/GPT-4o |
| **Kimi Code CLI** | ✅ Skill | `skills/vmware-monitor/SKILL.md` | Moonshot Kimi |
| **MCP Server** | ✅ MCP 协议 | `vmware_monitor/mcp_server/` | 任意 MCP 客户端 |
| **Python CLI** | ✅ 独立运行 | N/A | N/A |

### MCP Server 集成（本地 Agent）

vmware-monitor MCP Server 可接入**任何 MCP 兼容的 Agent 或工具**。配置模板见 [`examples/mcp-configs/`](examples/mcp-configs/)。所有 27 个工具均为**只读** — 代码级安全保障。

| Agent / 工具 | 本地模型支持 | 配置模板 | 集成指南 |
|-------------|:----------:|---------|---------|
| **[Goose](https://github.com/block/goose)** | ✅ Ollama, LM Studio | [`goose.json`](examples/mcp-configs/goose.json) | [指南](docs/integrations/goose.md) |
| **[LocalCowork](https://github.com/Liquid4All/localcowork)** | ✅ 完全离线 | [`localcowork.json`](examples/mcp-configs/localcowork.json) | [指南](docs/integrations/localcowork.md) |
| **[mcp-agent](https://github.com/lastmile-ai/mcp-agent)** | ✅ Ollama, vLLM | [`mcp-agent.yaml`](examples/mcp-configs/mcp-agent.yaml) | [指南](docs/integrations/mcp-agent.md) |
| **VS Code Copilot** | — | [`vscode-copilot.json`](examples/mcp-configs/vscode-copilot.json) | [指南](docs/integrations/vscode-copilot.md) |
| **Cursor** | — | [`cursor.json`](examples/mcp-configs/cursor.json) | — |
| **Continue** | ✅ Ollama | [`continue.yaml`](examples/mcp-configs/continue.yaml) | [指南](docs/integrations/continue.md) |
| **Claude Code** | — | [`claude-code.json`](examples/mcp-configs/claude-code.json) | — |

**完全本地运行**（无需云端 API）：

```bash
# Aider + Ollama + vmware-monitor（通过 SKILL.md）
aider --conventions skills/vmware-monitor/SKILL.md --model ollama/qwen2.5-coder:32b
```

---

## 安装

### 第 1 步：安装

```bash
git clone https://github.com/vmware-skills/VMware-Monitor.git
cd VMware-Monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 第 2 步：配置

```bash
mkdir -p ~/.vmware-monitor
cp config.example.yaml ~/.vmware-monitor/config.yaml
# 编辑 config.yaml，填入你的 vCenter/ESXi 目标信息
```

通过 `.env` 文件设置密码（推荐）：

```bash
cp .env.example ~/.vmware-monitor/.env
chmod 600 ~/.vmware-monitor/.env
# 编辑并填入真实密码
```

> **安全提示**：推荐使用 `.env` 文件而非命令行 `export`，避免密码出现在 shell 历史记录中。

密码环境变量命名规则：`VMWARE_{目标名大写}_PASSWORD`

### 第 3 步：连接 AI 工具

#### Claude Code（推荐）

```bash
# 方式一：由安装器放置 skill（推荐）
npx skills add vmware-skills/VMware-Monitor
# 或
clawhub install @zw008/vmware-monitor

# 方式二：手工安装 skill
git clone https://github.com/vmware-skills/VMware-Monitor.git
cd VMware-Monitor
mkdir -p ~/.claude/skills/vmware-monitor
cp -r skills/vmware-monitor/. ~/.claude/skills/vmware-monitor/
```

若需工具调用（而非仅 skill 上下文），再注册 MCP Server：

```bash
claude mcp add vmware-monitor -- vmware-monitor mcp
```

#### Codex / Aider / Continue

```bash
# 云端
aider --conventions skills/vmware-monitor/SKILL.md
# 本地 Ollama
aider --conventions skills/vmware-monitor/SKILL.md --model ollama/qwen2.5-coder:32b
```

#### MCP 服务器（v1.5.15+ 推荐方式）

完成 `uv tool install vmware-monitor` 后，**一条命令启动 MCP**：

```bash
vmware-monitor mcp

# 指定配置路径
VMWARE_MONITOR_CONFIG=/path/to/config.yaml vmware-monitor mcp
```

> 备选：`uvx --from vmware-monitor vmware-monitor mcp`（不安装临时运行）或 legacy 入口
> `vmware-monitor-mcp`（仍可用）。公司 TLS 代理网络下 uvx 可能报
> `invalid peer certificate: UnknownIssuer` — 推荐用上面的 `vmware-monitor mcp`，
> 或设置 `export UV_NATIVE_TLS=true`。

#### 独立 CLI（无需 AI）

```bash
source .venv/bin/activate
vmware-monitor inventory vms --target home-esxi
vmware-monitor health alarms --target home-esxi
```

---

## CLI 命令参考

```bash
# 环境诊断
vmware-monitor doctor                   # 检查环境、配置、连通性
vmware-monitor doctor --skip-auth       # 跳过 vSphere 认证检查（更快）

# MCP 配置生成
vmware-monitor mcp-config generate --agent goose        # 生成 Goose 配置
vmware-monitor mcp-config generate --agent claude-code  # 生成 Claude Code 配置
vmware-monitor mcp-config list                          # 列出所有支持的 Agent

# 资源清单
vmware-monitor inventory vms|hosts|datastores|clusters [--target <name>]
vmware-monitor inventory vms --limit 10 --sort-by memory_mb  # 按内存排序 Top 10
vmware-monitor inventory vms --power-state poweredOn         # 只显示开机 VM
vmware-monitor inventory vms --sort-by folder_path           # 按清单文件夹分组排序
# 所有 `inventory vms` 结果均包含 `folder_path` 字段（如 `/Datacenters/Production/Web Tier`）。
# MCP 工具 `list_virtual_machines` 额外支持 `folder_filter="Production"`，按文件夹路径大小写不敏感子串过滤。

# 健康检查
vmware-monitor health alarms [--target <name>]
vmware-monitor health events --hours 24 --severity warning [--target <name>]

# VM 信息（只读）
vmware-monitor vm info <vm-name>
vmware-monitor vm snapshot-list <vm-name>

# 扫描与守护进程
vmware-monitor scan now [--target <name>]
vmware-monitor daemon start|stop|status
```

---

## 项目结构

```
VMware-Monitor/
├── vmware_monitor/                # Python 后端（仅只读）
│   ├── config.py                  # 配置管理
│   ├── connection.py              # 多目标连接（pyVmomi）
│   ├── cli.py                     # CLI（仅只读命令）
│   ├── ops/                       # 查询操作
│   │   ├── inventory.py           # 资源清单
│   │   ├── health.py              # 健康检查
│   │   └── vm_info.py             # VM 信息、快照列表（只读）
│   ├── scanner/                   # 日志扫描守护进程
│   ├── notify/                    # 通知（JSONL + Webhook）
│   └── mcp_server/                # MCP 服务器（仅只读工具）
├── skills/vmware-monitor/         # Skills.sh 索引
│   ├── SKILL.md
│   └── references/                # 按需加载的详细文档
├── examples/mcp-configs/          # MCP 客户端配置模板
├── tests/                         # 测试
├── smithery.yaml                  # Smithery 市场配置
├── config.example.yaml
└── pyproject.toml
```

## 相关项目

| Skill | 范围 | 工具数 | 安装 |
|-------|------|:-----:|------|
| **[vmware-monitor](https://github.com/vmware-skills/VMware-Monitor)** | 只读监控、告警、事件 | 27 | `uv tool install vmware-monitor` |
| **[vmware-aiops](https://github.com/vmware-skills/VMware-AIops)** | VM 生命周期、部署、Guest Ops、集群 | 49 | `uv tool install vmware-aiops` |
| **[vmware-storage](https://github.com/vmware-skills/VMware-Storage)** | 数据存储、iSCSI、vSAN | 11 | `uv tool install vmware-storage` |
| **[vmware-vks](https://github.com/vmware-skills/VMware-VKS)** | Tanzu 命名空间、TKC 集群生命周期 | 20 | `uv tool install vmware-vks` |

## 问题反馈与贡献

如果遇到任何报错或问题，请将错误信息、日志或截图发送至 **zhouwei008@gmail.com**。欢迎贡献！

## 许可证

MIT
