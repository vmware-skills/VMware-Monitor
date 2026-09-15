# Cluster Health Summary — Display Template

This is the **editable template** for the `cluster_health_summary` view (CLI:
`vmware-monitor summary`, MCP tool: `cluster_health_summary`). It is intentionally
opinionated and intentionally *not* a widget builder — the goal is the 5-second
"is anything on fire?" glance, not a replacement for Aria Operations dashboards.

Everything below is meant to be changed. Add columns, drop columns, retune
thresholds, regroup, or switch the output to an HTML page. The tool returns clean
structured data; **how it is displayed is entirely up to this template and to what
the operator asks for in plain language.**

> **Not affiliated with, endorsed by, or sponsored by VMware, Inc. or Broadcom Inc.**
> "VMware" and "vSphere" are trademarks of Broadcom.

---

## Default layout

The rollup returns `{ totals, top_issues, issues_total, clusters, snapshot,
customization_hint }` and renders in two parts: a **top-N focus list** (the
headline) followed by the **per-cluster table** (the context).

### Part 1 — Top-N issues (the focus list)

For a large fleet, scanning every cluster row is too slow, so the individual
anomalies are flattened into one ranked list — the top N things wrong right now,
worst first. This is the headline; lead with it.

| Column   | Field       | Meaning |
|----------|-------------|---------|
| #        | (position)  | Rank, 1 = most urgent |
| Severity | `severity`  | `CRITICAL` / `WARNING` |
| Object   | `object`    | The host or cluster the problem is on |
| Cluster  | `cluster`   | Which cluster it rolls up to |
| Problem  | `detail`    | Plain-language description (alarm name, "memory at 96%", "host notResponding") |
| Next step| `drilldown` | The tool to run next for this issue |

`top_n` (CLI `--top`, default 10) caps the list; `issues_total` reports the
pre-cap count so truncation is visible (e.g. "Top 5 issues (of 8)"). `--top 0`
hides the list and shows only the table. Ranking: by severity, then kind
(`host_down` → `alarm` → `capacity` → `config`), then hottest capacity first.
Tune the order in `_KIND_RANK` / `_rank_issues` in `ops/cluster_summary.py`.

### Part 2 — Per-cluster table

The rollup's `clusters` array renders one row per cluster, worst status first:

| Column       | Field                         | Meaning |
|--------------|-------------------------------|---------|
| Status       | `status`                      | Opinionated verdict: `CRITICAL` / `WARN` / `OK` |
| Cluster      | `name`                        | Cluster name (`(standalone hosts)` bucket for un-clustered hosts) |
| Hosts        | `hosts_connected`/`hosts_total` | Connected vs total ESXi hosts |
| VMs on       | `vms_on`/`vms_total`          | Powered-on vs total VMs (omitted when `include_vms=false`) |
| CPU%         | `cpu_used_pct`                | Live cluster CPU utilisation |
| Mem%         | `mem_used_pct`                | Live cluster memory utilisation |
| HA           | `ha_enabled`                  | vSphere HA on/off; `null` (shown `n/a`) on the `(standalone hosts)` and `(vCenter-level)` rows |
| DRS          | `drs_enabled`                 | DRS on/off; `null` (shown `n/a`) on the same two rows |
| Alarms C/W   | `alarms.critical`/`.warning`  | Triggered alarm counts (cluster + its hosts) |
| Attention    | `attention[]`                 | Plain-language reasons the status is not OK |

Header line shows cross-cluster totals and the single worst status. The last line
is always the **friendly customization hint** (`customization_hint`) — keep it.

### The three signals (why these columns)

An operator's first look is always the same three questions. The default columns
map to exactly these and nothing more:

- **Problems** → `Status`, `Alarms C/W`, `Attention`, disconnected hosts
- **Capacity** → `CPU%`, `Mem%`
- **Health**  → `HA`, `DRS`, hosts connected

If a proposed new column does not answer one of those three, it probably belongs
in a drill-down tool (`get_alarms`, `datastore_capacity`, `host_performance`,
`vm_info`), not here.

---

## Opinionated thresholds

Defined in `vmware_monitor/ops/cluster_summary.py` as named constants — edit there
to retune:

| Constant             | Default | Drives |
|----------------------|:-------:|--------|
| `CPU_MEM_WARN_PCT`   | 85      | CPU or memory at/above → contributes `warn` |
| `CPU_MEM_CRIT_PCT`   | 95      | CPU or memory at/above → `critical` |
| `DATASTORE_OVERCOMMIT_WARN_PCT` (in `ops/capacity.py`) | 100 | Datastore provisioned above this % of capacity → a `warning` capacity issue in `top_issues` (`scope: datastore`); the same line `capacity datastores` colours red |

Status is also forced to `critical` by **any disconnected host** or **any critical
alarm**, and to at least `warn` by warning alarms or **HA disabled on a multi-host
cluster**. The HA rule applies only to real clusters — HA is a cluster setting, so
the `(standalone hosts)` row never gets it. Change `_rollup_status()` to add or relax rules.

---

## Adding a column — worked example (datastore free space)

Datastore headroom is the most common request. It is deliberately left out of the
default view so this template can show the full add-a-column path end to end:

1. **Data** — in `get_cluster_health_summary`, add a fourth batched pass over
   `vim.Datastore` collecting `["name", "summary.freeSpace", "summary.capacity",
   "host"]`. Map each datastore to clusters via its mounted hosts
   (`host[].key` → `host_to_cluster`), and record the minimum free % per cluster
   on the accumulator (e.g. `rec["ds_min_free_pct"]`).
2. **Status** — in `_rollup_status`, flag `warn` when `ds_min_free_pct` drops
   below a new `DS_FREE_WARN_PCT` constant, appending a reason like
   `"datastore <name> at 8% free"`.
3. **Render** — add a `Datastore free%` column in `cluster_summary_cmd` (CLI) and
   note the field in the table above. The MCP tool needs no change; the new field
   flows through automatically.

Removing a column is the reverse: drop the render column and, if you no longer
want it to affect status, remove its rule from `_rollup_status`.

---

## Output modes

The **same structured data** renders three ways — pick per request, no code change:

- **In chat (default):** a Markdown table of the `clusters` rows, ending with the
  customization hint line. Best for a quick answer.
- **CLI terminal:** `vmware-monitor summary` — colourised Rich table.
- **Offline HTML snapshot:** `vmware-monitor summary --html` (or `--html-path
  <file>`) renders `ops/health_html.py` → a **self-contained** file (all CSS
  inlined, no external CSS/JS/fonts, theme-aware) written to
  `~/vmware-health/cluster-health-<vc>-<YYYYMMDD-HHMMSS>.html`. It is offline by
  design — internal host/cluster names never leave the machine (a cloud-hosted
  artifact would upload them). The timestamped filename makes a folder of
  snapshots a browsable point-in-time history. It is a **snapshot, not a live
  page** — re-run to refresh. Every vSphere-sourced value is HTML-escaped at
  render time. To restyle it, edit the `_CSS` block and the row/card builders in
  `ops/health_html.py`; it reads the same `top_issues` + `clusters` fields, so no
  raw inventory is ever embedded.

---

## The friendly closing line

Every rendered summary ends with one inviting line so the operator always knows
the view bends to them. The tool returns it as `customization_hint`; echo it
verbatim, or adapt the phrasing while keeping the spirit:

> Want a different view? Just say so — e.g. "add datastore free space", "drop the
> DRS column", "only show clusters that need attention", "add per-VM CPU ready",
> or "render this as an HTML page". Columns, thresholds, and grouping are all
> adjustable.

This line is the whole point of the template being editable: the operator does not
need to know the field names — they describe what they want in words and the
assistant reshapes the table.
