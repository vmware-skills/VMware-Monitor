"""What vmware-policy needs to apply roles to this skill (HLD §18 D-21/D-22).

Policy cannot see which tools escape a role or how a missing target resolves; this
module declares them. Imported by the MCP server (roles apply only to a network
MCP server). With a vmware-policy that has no roles module (< 1.24) nothing is
declared.
"""

from __future__ import annotations

import sys

from vmware_policy import skill_name

from vmware_monitor.config import load_config

#: Tools that give their holder more than a role. Host-path tools are added by
#: policy while a file root is set.
ESCAPES = frozenset()


def _connection_config():
    """The config the connection layer uses for this call (review H1, then H-1).

    The server's connection manager loads the config once, on its first connection,
    and keeps it. Once it exists, the role is checked against that same object, so
    an edit to config.yaml cannot move the check off the target the call connects
    to. Before it exists, the file as it is now is what the manager is about to load.
    Looked up in ``sys.modules`` so the CLI never imports the MCP server.
    """
    server = sys.modules.get("vmware_monitor.mcp_server.server")
    manager = getattr(server, "_conn_mgr", None) if server is not None else None
    return manager._config if manager is not None else load_config()


def _target_name(target: str | None) -> str:
    """The target the connection layer would use for ``target`` (or the default)."""
    config = _connection_config()
    return (config.get_target(target) if target else config.default_target).name


try:
    from vmware_policy.roles import declare_role_metadata
except ImportError:  # vmware-policy without roles: nothing to declare
    declare_role_metadata = None

if declare_role_metadata is not None:
    declare_role_metadata(
        skill_name(__name__),
        escapes=ESCAPES,
        resolver=_target_name,
    )
