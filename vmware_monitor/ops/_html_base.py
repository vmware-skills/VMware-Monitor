"""Shared offline-HTML palette and helpers for the snapshot renderers.

Both the cluster-health snapshot (``health_html``) and the object-investigation
bundles (``investigate_html``) render self-contained, offline ``file://`` pages —
no external CSS/JS/fonts, so nothing leaves the machine. This module owns the one
theme-aware palette and the severity/status mappings so the two renderers stay
visually identical instead of drifting apart. Component-specific CSS (cards,
issue rows, ``<details>`` sections) lives with each renderer and is appended after
this base.

Escaping note: this module only supplies styling and classification; every value
that originates from vSphere must still be ``html.escape``-d at render time by the
caller — ``sanitize`` strips control characters but does not neutralise markup.
"""

from __future__ import annotations

# Darker slate palette, theme-aware (light + dark), semantic severity colours kept
# separate from the accent hue.
BASE_CSS = """
  :root {
    --ground:#d7dde7; --surface:#eef2f7; --surface-2:#e4e9f1; --ink:#131a26;
    --muted:#4d5a6e; --faint:#74839a; --border:#cbd3e0; --accent:#1f5698;
    --crit:#bf283f; --crit-bg:#f4dde1; --warn:#9c6207; --warn-bg:#f4e7cf;
    --ok:#237a4b; --ok-bg:#dcefe3; --meter-track:#d3dae6;
    --shadow:0 1px 2px rgba(23,35,54,.06),0 2px 8px rgba(23,35,54,.05);
    --mono:ui-monospace,"SF Mono","SFMono-Regular",Menlo,Consolas,monospace;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  }
  @media (prefers-color-scheme:dark){:root{
    --ground:#070b12; --surface:#0f1723; --surface-2:#141d2b; --ink:#e7edf6;
    --muted:#8c9bb1; --faint:#63728a; --border:#202a38; --accent:#6aa6df;
    --crit:#f2647c; --crit-bg:#2e1620; --warn:#e0a44a; --warn-bg:#2c2210;
    --ok:#5cca8c; --ok-bg:#0e2c1f; --meter-track:#212b3a;
    --shadow:0 1px 2px rgba(0,0,0,.35),0 2px 10px rgba(0,0,0,.3);
  }}
  :root[data-theme="light"]{
    --ground:#d7dde7; --surface:#eef2f7; --surface-2:#e4e9f1; --ink:#131a26;
    --muted:#4d5a6e; --faint:#74839a; --border:#cbd3e0; --accent:#1f5698;
    --crit:#bf283f; --crit-bg:#f4dde1; --warn:#9c6207; --warn-bg:#f4e7cf;
    --ok:#237a4b; --ok-bg:#dcefe3; --meter-track:#d3dae6;
  }
  :root[data-theme="dark"]{
    --ground:#070b12; --surface:#0f1723; --surface-2:#141d2b; --ink:#e7edf6;
    --muted:#8c9bb1; --faint:#63728a; --border:#202a38; --accent:#6aa6df;
    --crit:#f2647c; --crit-bg:#2e1620; --warn:#e0a44a; --warn-bg:#2c2210;
    --ok:#5cca8c; --ok-bg:#0e2c1f; --meter-track:#212b3a;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
    line-height:1.5;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1080px;margin:0 auto;padding:32px 24px 56px}
  header{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 16px}
  .eyebrow{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);font-weight:600}
  h1{font-size:26px;margin:2px 0 0;font-weight:680;letter-spacing:-.01em;text-wrap:balance;width:100%}
  h1 .vc{color:var(--accent)}
  .stamp{font-family:var(--mono);font-size:12.5px;color:var(--muted)}
  .overall{margin-left:auto;align-self:center;display:inline-flex;align-items:center;gap:8px;
    font-weight:700;font-size:13px;letter-spacing:.04em;text-transform:uppercase;
    padding:7px 14px;border-radius:999px;border:1px solid transparent}
  .overall.crit{color:var(--crit);background:var(--crit-bg);border-color:color-mix(in srgb,var(--crit) 30%,transparent)}
  .overall.warn{color:var(--warn);background:var(--warn-bg);border-color:color-mix(in srgb,var(--warn) 30%,transparent)}
  .overall.ok{color:var(--ok);background:var(--ok-bg);border-color:color-mix(in srgb,var(--ok) 30%,transparent)}
  .overall .dot{width:8px;height:8px;border-radius:50%;background:currentColor}
  .totals{display:grid;grid-template-columns:repeat(var(--tc,4),1fr);gap:1px;background:var(--border);
    border:1px solid var(--border);border-radius:12px;overflow:hidden;margin:20px 0 32px;box-shadow:var(--shadow)}
  .totals .cell{background:var(--surface);padding:14px 16px}
  .totals .k{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint)}
  .totals .v{font-family:var(--mono);font-size:21px;font-weight:600;margin-top:3px;font-variant-numeric:tabular-nums}
  .totals .v small{font-size:13px;color:var(--muted);font-weight:400}
  .totals .v .c{color:var(--crit)} .totals .v .w{color:var(--warn)}
  .section-label{font-size:12px;letter-spacing:.1em;text-transform:uppercase;font-weight:700;
    color:var(--muted);margin:0 0 12px;display:flex;align-items:baseline;gap:8px}
  .section-label .count{font-family:var(--mono);color:var(--faint);font-weight:500;letter-spacing:0}
  .issues{display:flex;flex-direction:column;gap:8px;margin-bottom:36px}
  .issue{display:grid;grid-template-columns:26px 92px 1fr auto;gap:4px 14px;align-items:center;
    background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--sev);
    border-radius:10px;padding:12px 16px 12px 15px;box-shadow:var(--shadow)}
  .issue.crit{--sev:var(--crit)} .issue.warn{--sev:var(--warn)}
  .issue .rank{font-family:var(--mono);font-size:13px;color:var(--faint);text-align:right;font-variant-numeric:tabular-nums}
  .chip{font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
    padding:3px 9px;border-radius:6px;text-align:center;white-space:nowrap}
  .chip.crit{color:var(--crit);background:var(--crit-bg)} .chip.warn{color:var(--warn);background:var(--warn-bg)}
  .chip.info{color:var(--muted);background:var(--surface-2)}
  .issue .what{min-width:0} .issue .prob{font-weight:600;font-size:14.5px}
  .issue .obj{font-family:var(--mono);font-size:12px;color:var(--muted);margin-top:2px}
  .issue .obj b{color:var(--ink);font-weight:600}
  .issue .next{font-family:var(--mono);font-size:11.5px;color:var(--faint);text-align:right;max-width:260px}
  .issue .next .arrow{color:var(--accent)}
  @media (max-width:720px){.issue{grid-template-columns:26px 1fr}
    .issue .chip{grid-column:2;justify-self:start} .issue .next{grid-column:2;text-align:left;max-width:none}}
  .clean-all{background:var(--surface);border:1px solid var(--border);border-radius:10px;
    padding:16px;color:var(--ok);font-weight:600;box-shadow:var(--shadow);margin-bottom:36px}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow);overflow:hidden}
  .card>.top{display:flex;align-items:center;gap:10px;padding:14px 16px;
    border-bottom:1px solid var(--border);border-top:3px solid var(--sev)}
  .card.crit{--sev:var(--crit)} .card.warn{--sev:var(--warn)} .card.ok{--sev:var(--ok)}
  .card .cname{font-weight:680;font-size:16px;letter-spacing:-.01em}
  .pill{margin-left:auto;font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
    padding:4px 10px;border-radius:999px}
  .pill.crit{color:var(--crit);background:var(--crit-bg)} .pill.warn{color:var(--warn);background:var(--warn-bg)}
  .pill.ok{color:var(--ok);background:var(--ok-bg)}
  .card .body{padding:14px 16px;display:flex;flex-direction:column;gap:11px}
  .metric{display:grid;grid-template-columns:58px 1fr auto;gap:10px;align-items:center}
  .metric .ml{font-size:12px;color:var(--muted)}
  .metric .mv{font-family:var(--mono);font-size:13px;font-variant-numeric:tabular-nums;text-align:right}
  .meter{height:6px;border-radius:3px;background:var(--meter-track);overflow:hidden}
  .meter>span{display:block;height:100%;border-radius:3px;background:var(--mc)}
  .mok{--mc:var(--ok)} .mwarn{--mc:var(--warn)} .mcrit{--mc:var(--crit)} .macc{--mc:var(--accent)}
  .kv{display:flex;flex-wrap:wrap;gap:6px}
  .tag{font-size:11px;font-family:var(--mono);padding:3px 8px;border-radius:6px;
    background:var(--surface-2);border:1px solid var(--border);color:var(--muted)}
  .tag b{color:var(--ink);font-weight:600}
  .tag.on b{color:var(--ok)} .tag.off b{color:var(--crit)} .tag.dim{opacity:.7}
  .attn{display:flex;flex-direction:column;gap:4px;margin-top:2px}
  .attn .a{font-size:12.5px;color:var(--ink);display:flex;gap:7px}
  .attn .a::before{content:"•";color:var(--sev)}
  .attn .clean{font-size:12.5px;color:var(--ok)}
  footer{margin-top:40px;padding-top:18px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:10px}
  .hint{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;
    padding:13px 16px;font-size:13.5px;color:var(--ink)}
  .hint b{color:var(--accent)}
  .meta{font-family:var(--mono);font-size:11.5px;color:var(--faint);display:flex;flex-wrap:wrap;gap:4px 18px}
"""

# Severity (event/alarm) and status (cluster rollup) label + class maps, shared so
# both renderers colour identical bands identically.
SEV_LABEL = {"critical": "Critical", "warning": "Warning", "info": "Info"}
SEV_CLASS = {"critical": "crit", "warning": "warn", "info": "info"}
STATUS_CLASS = {"critical": "crit", "warn": "warn", "ok": "ok"}
STATUS_LABEL = {"critical": "Critical", "warn": "Warn", "ok": "OK"}


def meter_class(pct: float) -> str:
    """Threshold colour for a utilisation meter (matches cluster_summary bands)."""
    if pct >= 95:
        return "mcrit"
    if pct >= 85:
        return "mwarn"
    return "mok"


def clamp(pct: float) -> float:
    """Clamp a percentage to the drawable 0–100 range."""
    return max(0.0, min(100.0, pct))


def unreadable_note(row: dict) -> str:
    """" (N unreadable)" after a connected/total host count, or nothing.

    A host this account cannot read is not a disconnected host; without the
    note "0/1" reads as an outage (lab 2026-10-18, Read-only on one VM).
    """
    n = row.get("hosts_unreadable", 0)
    return f" ({n} unreadable)" if n else ""
