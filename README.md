<!-- mcp-name: io.github.vmware-skills/vmware-monitor -->

# VMware-Monitor

<a id="readme-overview"></a>

Find what needs attention across VMware infrastructure, with evidence.

English · [简体中文](README-CN.md)

Community-maintained · MIT · Not an official VMware/Broadcom product.

[![18-second illustrative investigation demo](https://raw.githubusercontent.com/vmware-skills/marketplace/main/assets/demos/vmware-skills-en.gif)](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-en.mp4)

Synthetic-data animation, not a live recording. [Watch the AI-narrated family introduction (~2 min)](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-en.mp4)

<a id="readme-capabilities-and-workflows"></a>

## Why use it?

- Summarize cluster and cross-vCenter priorities.
- Correlate VM, host, datastore and event observations.
- Keep unreadable objects and missing evidence visible.

<a id="readme-quick-start"></a>

## Get started

Install the published CLI and inspect its commands:

```sh
uv tool install vmware-monitor==2.6.2
vmware-monitor --help
```

Requires uv and Python >=3.10. Configure targets and credentials locally before remote queries. [Setup guide](skills/vmware-monitor/references/setup-guide.md).

For Claude Code and other host installation paths, see the [family portal](https://github.com/vmware-skills/marketplace).

<a id="readme-safety-and-audit"></a>

## Know the boundaries

No VMware infrastructure mutation tools. Local configuration, audit logs and reports still write files; optional daemon and webhook behavior is documented separately.

<a id="readme-mcp-configuration"></a> <a id="readme-troubleshooting"></a> <a id="readme-reference"></a>

## Go deeper

- [Capabilities](skills/vmware-monitor/references/capabilities.md)
- [CLI reference](skills/vmware-monitor/references/cli-reference.md)
- [Agent guardrails](skills/vmware-monitor/references/agent-guardrails.md)
- [Detailed usage and architecture](skills/vmware-monitor/references/detailed-guide.md)
- [MCP launch configuration](.mcp.json)
- [Canonical skill instructions](skills/vmware-monitor/SKILL.md)

<a id="readme-companion-skills"></a>

Part of [VMware Skills](https://github.com/vmware-skills/marketplace): 15 skills, 14 MCP servers and one shared Policy library. If it helps your work, star the family portal.

<a id="readme-license"></a>

[Issues and feedback](https://github.com/vmware-skills/VMware-Monitor/issues) · [MIT](LICENSE)
