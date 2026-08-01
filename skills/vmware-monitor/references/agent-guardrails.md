# Operating the VMware skills with a local / small model

Claude-class models drive these skills without special instruction. Smaller and
locally-hosted models — Llama 3.3 70B, Qwen, Mistral, and similar, served
through Goose, Ollama, or OpenShift AI — need explicit operating rules to call
tools reliably.

This page exists because an operator wrote those rules by hand first. The
guardrails below are adapted, with thanks, from the working configuration
[@juanpf-ha](https://github.com/juanpf-ha) developed while running
vmware-monitor and vmware-aria against a production vSphere estate with Llama
3.3 70B FP8 on an on-prem H100
([VMware-AIops#31](https://github.com/vmware-skills/VMware-AIops/issues/31)).

> **Disclaimer**: This is a community-maintained open-source project and is
> **not affiliated with, endorsed by, or sponsored by VMware, Inc. or Broadcom
> Inc.** "VMware" and "vSphere" are trademarks of Broadcom.

---

## First: the rules you no longer need to write

Several guardrails from the original configuration are now enforced by the
skills themselves. Prompt instructions are advisory — a model can ignore them.
These are structural, so it cannot.

| Guardrail you would otherwise prompt for | Now enforced by |
|---|---|
| "Work read-only and never modify anything" | **Zero write tools.** This skill has no create, modify, delete, or power operation in its registry at all — `list_tools()` only ever offers reads, so the model cannot call what does not exist. |
| "First resolve the affected resource_id through vmware-aria before querying vmware-monitor" | **`investigate_alert`** does the whole alert → resource → confirmed-name sequence in one call. |
| "Do not confuse the alert ID with the affected resource ID" | `investigate_alert` returns a `correlation` block with both UUIDs explicitly labelled. |
| "Only correlate Aria and vCenter data after the resource name and type have been confirmed" | `investigate_alert` returns `correlation.confirmed`, and withholds its `next_step` handoff until name and kind are known. |
| "Use explicit limits for queries that may return large amounts of data" | **The list envelope.** Every list tool returns `{items, returned, limit, total, truncated, hint}`, so the model reads truncation instead of guessing at it. Most tools are unlimited unless you pass `limit` (`vm_performance` defaults to 25); the envelope states which case you got. |
| "If a requested field was not returned by any tool, show it as not available" | Tools return explicit `null` for unresolved fields rather than omitting the key. |

---

## The system prompt

Everything below still benefits from being stated explicitly. Copy this into
your agent's instruction block.

```text
## Tool use

- Always call an MCP tool before answering any question about the current
  VMware environment. Never answer from memory or assumption.
- Never describe a tool call, and never output a JSON example, instead of
  executing the tool. If you intend to call a tool, call it.
- If a tool fails, report the actual error text. Do not complete the answer
  with assumptions about what the result would have been.
- Use explicit limits on queries that may return large amounts of data. Do not
  request unlimited results unless the user asks for them.

## Skill routing

- vmware-monitor: vCenter inventory, ESXi hosts, clusters, datastores, VMs,
  snapshots, alarms, events, performance.
- vmware-aria: Aria Operations health, resources, metrics, alerts, capacity.
- vmware-aiops: VM lifecycle (power, clone, snapshot, migrate, delete).
- vmware-nsx / vmware-nsx-security: networking and firewall.
- vmware-storage: datastores, iSCSI, vSAN.

## Data fidelity

- Never invent infrastructure objects, metrics, alarms, events, or
  relationships. If a tool did not return it, it does not exist for this answer.
- Preserve the exact criticality, status, impact, and control-state values the
  tools return. Do not translate, normalise, or prettify enum values.
- If a requested field was not returned, show it as "not available". Do not
  infer it from other fields.
- Preserve the original order and the full set of fields when the user asks
  for specific ones.
- When a response is long, report every item it contains. If a result is
  truncated, the tool says so explicitly — report the truncation rather than
  describing the visible subset as the whole.

## Analysis discipline

- Separate observed data from interpretation. State which is which.
- Do not claim a security, performance, storage, or capacity problem unless
  the tool output contains explicit supporting evidence.
- Avoid generic recommendations that are not directly supported by the results.

## Correlating Aria alerts with vCenter

- Use investigate_alert to go from an alert to its affected resource. It
  resolves the resource and confirms its name and kind in one call.
- Do not pass an alert UUID where a resource UUID is expected. investigate_alert
  labels both in its correlation block.
- Only query vCenter for a resource once correlation.confirmed is true. The
  next_step block names the exact tool and argument to use.
```

---

## Known failure modes on small models

Observed with Llama 3.3 70B FP8 (Goose, on-prem H100), and useful as a
checklist when evaluating any local model against these skills:

| Symptom | Mitigation |
|---|---|
| Describes a tool call, or emits a JSON example, instead of executing it | The "never describe a tool call" rule above. Also check your harness is not echoing tool schemas into context — models imitate the nearest format they see. |
| Long tool responses: omits items, or reports "no data returned" when data was present | Ask for explicit limits so responses stay small. Check the envelope's `truncated` / `returned` / `total` fields rather than trusting the model's summary — every list tool states them, so a "no data" claim is checkable against `returned`. |
| Adds generic recommendations unsupported by results | The "analysis discipline" rules. |
| Drops requested fields or reorders results | State the required fields and ordering in the request itself, not only in the system prompt. |
| Multi-tool workflows take 30–50s end to end | Prefer the aggregate tools — `investigate_alert`, `cluster_health_summary`, `vm_investigation_bundle` — which collapse a 3-4 call sequence into one round trip. |

## Reporting results

Local-model compatibility is an explicit design constraint for this family, and
the evidence base is small. If you evaluate a model against these skills —
Qwen, Mistral, Granite, or anything else — a report of what worked and what did
not is genuinely useful:
[github.com/vmware-skills/VMware-Monitor/issues](https://github.com/vmware-skills/VMware-Monitor/issues).
