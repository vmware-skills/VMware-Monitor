"""MCP Apps views: interactive pages a host renders next to a tool's result.

A host that supports the MCP Apps extension (``io.modelcontextprotocol/ui``)
sees ``_meta.ui.resourceUri`` on a tool, reads that ``ui://`` resource, loads
it in a sandboxed iframe and hands it the tool result over postMessage. A host
without the extension ignores both, so the tool's text output stays exactly
what it was — nothing here changes what a tool returns.

The page is one self-contained HTML string: no CDN (the iframe's default CSP
blocks network loads, and customer networks often cannot reach one), and the
palette is the offline HTML report's, so the two views never drift apart.

Calls the page makes go back through the host as ordinary ``tools/call``
requests, so they pass the same role check, policy and audit as any call.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from vmware_monitor.ops._html_base import BASE_CSS, SEV_CLASS, SEV_LABEL, STATUS_CLASS, STATUS_LABEL

MIME_TYPE = "text/html;profile=mcp-app"
CLUSTER_HEALTH_URI = "ui://vmware-monitor/cluster-health.html"

# Which read-only tool a "drill down" button calls for an issue of each scope,
# and the argument that carries the issue's ``object`` name. Tests check every
# entry against the live tool registry.
DRILLDOWN: dict[str, dict[str, str]] = {
    "host": {"tool": "investigate_host", "arg": "host_name"},
    "datastore": {"tool": "investigate_datastore", "arg": "datastore_name"},
}

_CSS_SLOT = "/*__BASE_CSS__*/"
_DRILLDOWN_SLOT = "__DRILLDOWN_JSON__"
_LABELS_SLOT = "__LABELS_JSON__"


def _template(name: str) -> str:
    return resources.files("vmware_monitor.mcp_server").joinpath("ui", name).read_text(encoding="utf-8")


def cluster_health_html() -> str:
    """The cluster-health view, with the shared palette and drill-down map filled in."""
    page = _template("cluster_health.html")
    if any(slot not in page for slot in (_CSS_SLOT, _DRILLDOWN_SLOT, _LABELS_SLOT)):
        raise RuntimeError("cluster_health.html lost a template slot")
    labels = {
        "sev_label": SEV_LABEL,
        "sev_class": SEV_CLASS,
        "status_class": STATUS_CLASS,
        "status_label": STATUS_LABEL,
    }
    return (
        page.replace(_CSS_SLOT, BASE_CSS)
        .replace(_DRILLDOWN_SLOT, json.dumps(DRILLDOWN))
        .replace(_LABELS_SLOT, json.dumps(labels))
    )


def ui_meta(uri: str) -> dict[str, Any]:
    """The tool ``meta`` that attaches the view at ``uri``."""
    return {"ui": {"resourceUri": uri}}


def register(mcp: Any) -> None:
    """Register every view as a ``ui://`` resource on ``mcp``."""

    @mcp.resource(
        CLUSTER_HEALTH_URI,
        name="cluster_health_view",
        title="Cluster health",
        description="Interactive view for get_cluster_health_summary.",
        mime_type=MIME_TYPE,
    )
    def cluster_health_view() -> str:
        return cluster_health_html()
