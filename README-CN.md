<!-- mcp-name: io.github.vmware-skills/vmware-monitor -->

# VMware-Monitor

<a id="readme-overview"></a>

沿着证据，找到 VMware 基础设施中需要关注的对象。

[English](https://github.com/vmware-skills/VMware-Monitor/blob/main/README.md) · 简体中文

社区维护 · MIT 开源 · 非 VMware/Broadcom 官方产品。

[![24 秒调查示意演示](https://raw.githubusercontent.com/vmware-skills/marketplace/main/assets/demos/vmware-skills-zh.gif)](https://github.com/vmware-skills/marketplace/releases/download/demos-2026-11-13-operations/vmware-skills-zh.mp4)

GIF 使用合成数据；视频包含脱敏的历史截图。[下载家族 AI 配音介绍（约 3 分钟）](https://github.com/vmware-skills/marketplace/releases/download/demos-2026-11-13-operations/vmware-skills-zh.mp4)

<a id="readme-capabilities-and-workflows"></a>

## 能解决什么问题？

- 汇总集群与跨 vCenter 的关注优先级。
- 关联 VM、主机、数据存储和事件观测。
- 明确展示不可读取对象与证据缺口。

<a id="readme-quick-start"></a>

## 快速开始

先从一个 vCenter 取得一次有用的只读结果：

```sh
uv tool install vmware-monitor==2.6.2
vmware-monitor init
vmware-monitor doctor
vmware-monitor attention
```

需要 uv 与 Python >=3.10。`init` 在本地询问目标与凭据、写入配置，并提供连接测试。使用授予 vCenter **Read-Only** 角色的专用账号，保持 TLS 验证开启并信任组织的 CA。已有配置会保留，除非你选择覆盖。

`doctor` 通过后，确认 `attention` 为预期目标返回可读取的事实。目标不可达或权限不足表示接入受阻，不代表环境健康。[配置与排障](https://github.com/vmware-skills/VMware-Monitor/blob/main/skills/vmware-monitor/references/setup-guide.md#configuration)。

Agent 宿主安装见本模块的[配置指南](https://github.com/vmware-skills/VMware-Monitor/blob/main/skills/vmware-monitor/references/setup-guide.md)，以及 [Cursor、Codex、Gemini CLI、Cline 与本地模型示例](https://github.com/vmware-skills/marketplace/blob/main/docs/agent-hosts-CN.md)。

选模型可用[共享模型自测流程](https://github.com/vmware-skills/marketplace/blob/main/model-fit/RUNNER-CN.md)：统一任务、复核质量与安全，再按任务组选择最小已测通过配置；不代表生产质量保证。

<a id="readme-safety-and-audit"></a>

## 使用边界

不提供修改 VMware 基础设施的工具。本地配置、审计与报告仍会写入文件；可选 daemon 和 webhook 行为另有说明。

<a id="readme-mcp-configuration"></a> <a id="readme-troubleshooting"></a> <a id="readme-reference"></a>

## 详细文档

- [能力范围](https://github.com/vmware-skills/VMware-Monitor/blob/main/skills/vmware-monitor/references/capabilities.md)
- [CLI 用法](https://github.com/vmware-skills/VMware-Monitor/blob/main/skills/vmware-monitor/references/cli-reference.md)
- [Agent 使用约束](https://github.com/vmware-skills/VMware-Monitor/blob/main/skills/vmware-monitor/references/agent-guardrails.md)
- [详细用法与架构](https://github.com/vmware-skills/VMware-Monitor/blob/main/skills/vmware-monitor/references/detailed-guide-CN.md)
- [MCP 启动配置](https://github.com/vmware-skills/VMware-Monitor/blob/main/.mcp.json)
- [唯一技能指令正本](https://github.com/vmware-skills/VMware-Monitor/blob/main/skills/vmware-monitor/SKILL.md)

<a id="readme-companion-skills"></a>

属于 [VMware Skills 家族](https://github.com/vmware-skills/marketplace/blob/main/README-CN.md)：15 个 skill、14 个 MCP server 与一个共享 Policy 库。如果对你有用，欢迎 Star 家族门户。

<a id="readme-license"></a>

[分享接入进度或首次结果](https://github.com/vmware-skills/marketplace/issues/new?template=setup-feedback.yml) · [报告问题](https://github.com/vmware-skills/VMware-Monitor/issues) · [MIT](https://github.com/vmware-skills/VMware-Monitor/blob/main/LICENSE)
