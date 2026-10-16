"""What vmware-policy needs to apply roles to this skill (HLD §18 D-21/D-22).

Policy cannot see which tools escape a role or how a missing target resolves; this
module declares them. Imported by the MCP server (roles apply only to a network
MCP server). With a vmware-policy that has no roles module (< 1.24) nothing is
declared.
"""

from __future__ import annotations

import functools

from vmware_policy import skill_name

from vmware_monitor.config import load_config

#: Tools that give their holder more than a role. Host-path tools are added by
#: policy while a file root is set.
ESCAPES = frozenset()


@functools.cache
def _startup_config():
    """The config as loaded once, like the connection manager, which never reloads it.

    A role must be checked against the target ``connect()`` will use. Following config
    edits here while connections keep the startup copy let a role limited to one target
    run a default-target call on another after the file was edited (review H1). Both
    pick up an edited config together, on restart.
    """
    return load_config()


def _target_name(target: str | None) -> str:
    """The target the connection layer would use for ``target`` (or the default)."""
    config = _startup_config()
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
