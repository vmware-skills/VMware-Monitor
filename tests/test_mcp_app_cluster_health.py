"""The cluster-health MCP App: a view attached to get_cluster_health_summary.

What must hold, whatever the view looks like:

* the tool names the view, and the view is registered as a ``ui://`` resource
  with the MCP Apps MIME type — a dangling ``resourceUri`` renders nothing;
* a host without MCP Apps support sees exactly what it saw before: the tool's
  output is not changed (no output schema, no structured content added);
* the page loads nothing from the network and never parses vSphere strings as
  HTML — VM, host and alarm names are written by whoever can name objects in
  vCenter;
* every tool the view can call exists, is read-only, and takes the argument the
  view passes.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from vmware_monitor.mcp_server import apps
from vmware_monitor.mcp_server.server import mcp

pytestmark = pytest.mark.unit

TOOL = "get_cluster_health_summary"


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def _html() -> str:
    contents = asyncio.run(mcp.read_resource(apps.CLUSTER_HEALTH_URI))
    assert contents, "the view resource returned nothing"
    return contents[0].content


def _script(html: str) -> str:
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert scripts, "the view has no inline script"
    return "\n".join(scripts)


def test_tool_points_at_the_view() -> None:
    meta = _tools()[TOOL].meta or {}
    assert meta.get("ui", {}).get("resourceUri") == apps.CLUSTER_HEALTH_URI


def test_view_is_registered_with_the_mcp_apps_mime_type() -> None:
    resources = {str(r.uri): r for r in asyncio.run(mcp.list_resources())}
    assert apps.CLUSTER_HEALTH_URI in resources
    assert resources[apps.CLUSTER_HEALTH_URI].mimeType == "text/html;profile=mcp-app"


def test_every_ui_uri_a_tool_names_is_registered() -> None:
    """Both directions: no tool points at a view that does not exist."""
    registered = {str(r.uri) for r in asyncio.run(mcp.list_resources())}
    named = {
        (t.meta or {}).get("ui", {}).get("resourceUri")
        for t in _tools().values()
        if (t.meta or {}).get("ui")
    }
    assert named, "no tool names a view — the check would pass on nothing"
    assert named <= registered


def test_output_for_hosts_without_ui_is_unchanged() -> None:
    tool = mcp._tool_manager.get_tool(TOOL)
    assert tool.output_schema is None


def test_page_loads_nothing_from_the_network() -> None:
    html = _html()
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", html, flags=re.I)
    assert "@import" not in html
    assert not re.search(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource)\b", _script(html))


@pytest.mark.parametrize(
    "sink",
    ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"],
)
def test_script_never_parses_strings_as_html_or_code(sink: str) -> None:
    assert sink not in _script(_html())


def test_view_shares_the_offline_report_palette() -> None:
    from vmware_monitor.ops._html_base import BASE_CSS

    assert BASE_CSS.strip() in _html()


def test_drilldown_tools_exist_are_read_only_and_take_the_argument() -> None:
    tools = _tools()
    assert apps.DRILLDOWN, "no drill-down configured — the check would pass on nothing"
    for scope, spec in apps.DRILLDOWN.items():
        tool = tools.get(spec["tool"])
        assert tool is not None, f"{scope}: {spec['tool']} is not registered"
        assert tool.annotations and tool.annotations.readOnlyHint is True, spec["tool"]
        assert spec["arg"] in tool.inputSchema["required"], (scope, spec)


def test_drilldown_config_reaches_the_page_as_data() -> None:
    html = _html()
    match = re.search(r'<script type="application/json" id="drilldown">(.*?)</script>', html, re.S)
    assert match, "drill-down config is not embedded"
    assert json.loads(match.group(1)) == apps.DRILLDOWN
