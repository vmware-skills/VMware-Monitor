"""``access-info``: which account the CLI uses on a target, and what it may do (HLD §17 D-17)."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from vmware_policy import audited

from vmware_monitor.cli_base import (
    ConfigOption,
    TargetOption,
    audit,
    cli_errors,
    console,
    fail,
    get_connection,
)

ToolOption = Annotated[
    str | None,
    typer.Option(
        "--tool", help="One MCP tool name, e.g. list_active_sessions (default: every tool)"
    ),
]


@cli_errors
@audited("get_access_info")
def access_info_cmd(
    tool: ToolOption = None,
    target: TargetOption = None,
    config: ConfigOption = None,
) -> None:
    """Show the account in use and whether it holds each tool's privileges (root folder)."""
    from vmware_policy.access import vsphere_access_info

    from vmware_monitor.access_requirements import REQUIREMENTS

    if tool is not None and tool not in REQUIREMENTS:
        try:  # the helper words the unknown-name error (with close matches)
            vsphere_access_info(None, target="", requirements=REQUIREMENTS, tool=tool)
        except ValueError as e:
            fail(str(e))
    si, _, tgt = get_connection(target, config)
    result = vsphere_access_info(si, target=tgt, requirements=REQUIREMENTS, tool=tool)
    audit.log_query(target=tgt, resource="access_info", query_type="get_access_info")
    console.print_json(json.dumps(result, default=str))


def register(app: typer.Typer) -> None:
    """Attach ``access-info`` to the root CLI."""
    app.command("access-info")(access_info_cmd)
