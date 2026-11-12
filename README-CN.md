<!-- mcp-name: io.github.vmware-skills/vmware-monitor -->

# VMware-Monitor

<a id="readme-overview"></a>

沿着证据，找到 VMware 基础设施中需要关注的对象。

[English](README.md) · 简体中文

社区维护 · MIT 开源 · 非 VMware/Broadcom 官方产品。

[![18 秒调查示意演示](https://raw.githubusercontent.com/vmware-skills/marketplace/main/assets/demos/vmware-skills-zh.gif)](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-zh.mp4)

使用合成数据的示意动画，非实时录屏。[观看家族 AI 配音介绍（约 2 分钟）](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-zh.mp4)

<a id="readme-capabilities-and-workflows"></a>

## 能解决什么问题？

- 汇总集群与跨 vCenter 的关注优先级。
- 关联 VM、主机、数据存储和事件观测。
- 明确展示不可读取对象与证据缺口。

<a id="readme-quick-start"></a>

## 快速开始

安装已发布 CLI，并查看可用命令：

```sh
uv tool install vmware-monitor==2.6.2
vmware-monitor --help
```

需要 uv 与 Python >=3.10。远程查询前，在本地配置目标与凭据。[配置指南](skills/vmware-monitor/references/setup-guide.md)。

Claude Code 及其他宿主安装方式见[家族门户](https://github.com/vmware-skills/marketplace/blob/main/README-CN.md)。

<a id="readme-safety-and-audit"></a>

## 使用边界

不提供修改 VMware 基础设施的工具。本地配置、审计与报告仍会写入文件；可选 daemon 和 webhook 行为另有说明。

<a id="readme-mcp-configuration"></a> <a id="readme-troubleshooting"></a> <a id="readme-reference"></a>

## 详细文档

- [能力范围](skills/vmware-monitor/references/capabilities.md)
- [CLI 用法](skills/vmware-monitor/references/cli-reference.md)
- [Agent 使用约束](skills/vmware-monitor/references/agent-guardrails.md)
- [详细用法与架构](skills/vmware-monitor/references/detailed-guide-CN.md)
- [MCP 启动配置](.mcp.json)
- [唯一技能指令正本](skills/vmware-monitor/SKILL.md)

<a id="readme-companion-skills"></a>

属于 [VMware Skills 家族](https://github.com/vmware-skills/marketplace/blob/main/README-CN.md)：15 个 skill、14 个 MCP server 与一个共享 Policy 库。如果对你有用，欢迎 Star 家族门户。

<a id="readme-license"></a>

[问题与反馈](https://github.com/vmware-skills/VMware-Monitor/issues) · [MIT](LICENSE)
