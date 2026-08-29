"""Shared CLI plumbing for VMware Monitor (read-only).

Extracted from cli.py so the growing command set can live in focused modules
(cli.py, cli_observability.py) while sharing one error decorator, connection
helper, audit logger, and option types. Keeps every CLI module under the
800-line family limit.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Annotated, Any, Callable

import typer
from rich.console import Console

from vmware_monitor.notify.audit import AuditLogger

console = Console()
audit = AuditLogger()

TargetOption = Annotated[str | None, typer.Option("--target", "-t", help="Target name from config")]
ConfigOption = Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")]


def fail(message: str) -> None:
    """Print one red teaching line and exit 1 (no traceback)."""
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(1)


def cli_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Translate known failures into one red teaching line + exit 1.

    Without this, config/auth/network problems surface as raw tracebacks.
    Catches: FileNotFoundError, KeyError, OSError (incl. socket errors and
    ConnectionError), VMNotFoundError, and vim/vmodl API faults.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from pyVmomi import vmodl

        from vmware_monitor.ops.vm_info import VMNotFoundError

        try:
            return fn(*args, **kwargs)
        except typer.Exit:
            raise
        except VMNotFoundError as e:
            fail(f"{e}. Run 'vmware-monitor inventory vms' to see available VMs.")
        except FileNotFoundError as e:
            fail(
                f"Config file missing: {e}. Run: vmware-monitor init "
                "(or: mkdir -p ~/.vmware-monitor && cp config.example.yaml "
                "~/.vmware-monitor/config.yaml)"
            )
        except KeyError as e:
            fail(
                f"Missing config key or password env var: {e}. "
                "Check ~/.vmware-monitor/config.yaml and ~/.vmware-monitor/.env."
            )
        except vmodl.fault.NotImplemented as e:
            # A bare ESXi answers this to anything that is really a vCenter
            # service — the event query above all. Connectivity and credentials
            # are fine, so the general "run doctor" remedy sends the user to a
            # check that passes and explains nothing. Observed on a standalone
            # host, 2026-08-29.
            fail(
                f"This target does not implement that operation: "
                f"{str(getattr(e, 'msg', None) or type(e).__name__).rstrip('.')}. "
                f"This is what a "
                f"standalone ESXi answers for vCenter-only services such as the "
                f"event query — it is not a connectivity or credential problem, "
                f"and 'doctor' will pass. Point the command at a vCenter target, "
                f"or use the paths that work directly against a host: "
                f"'vmware-monitor health alarms', 'vmware-monitor scan logs', "
                f"'vmware-monitor summary'."
            )
        except vmodl.MethodFault as e:
            fail(
                f"vSphere API fault: {getattr(e, 'msg', None) or type(e).__name__}. "
                "Run 'vmware-monitor doctor' to verify connectivity and credentials."
            )
        except (ConnectionError, OSError) as e:
            fail(
                f"Connection failed: {e}. "
                "Run 'vmware-monitor doctor' to verify connectivity and credentials."
            )
        except Exception as e:
            # pyVim.connect.SmartConnect raises a BARE Exception when the port
            # answers but no VIM service does — a vCenter mid-boot, a wrong
            # port, a proxy, or an HTTPS endpoint that simply is not vCenter.
            # It is neither OSError nor ConnectionError, so it walked past every
            # clause above and printed twenty lines of pyVim internals. Seen
            # live against a vCSA that was still starting, 2026-08-29.
            #
            # Matched on the message rather than caught wholesale: a blanket
            # `except Exception` here would turn genuine bugs in this codebase
            # into a calm sentence, which is worse than the traceback it
            # replaces. Anything else re-raises.
            if "is not a VIM server" not in str(e):
                raise
            fail(
                f"{e}. TCP reached the host, but nothing there answered as a "
                f"vSphere endpoint — most often a vCenter that is still "
                f"starting up (a vCSA takes several minutes after power-on), "
                f"and otherwise a wrong port, a proxy in the way, or an HTTPS "
                f"service that is not vSphere. Wait and retry, or check the "
                f"host and port for this target in ~/.vmware-monitor/config.yaml."
            )

    return wrapper


def get_connection(target: str | None, config_path: Path | None = None):
    """Helper to get a pyVmomi connection.  Returns (si, cfg, target_name)."""
    from vmware_monitor.config import load_config
    from vmware_monitor.connection import ConnectionManager

    cfg = load_config(config_path)
    mgr = ConnectionManager(cfg)
    target_name = target or cfg.default_target.name
    return mgr.connect(target), cfg, target_name


def get_all_connections(config_path: Path | None = None):
    """Connect to every configured target, tolerating per-target failures.

    Returns ``(sessions, unreachable)`` — see ``ConnectionManager.connect_all`` —
    for the cross-vCenter attention view. One dead vCenter never fails the whole
    command; it is reported under ``unreachable`` instead.
    """
    from vmware_monitor.config import load_config
    from vmware_monitor.connection import ConnectionManager

    cfg = load_config(config_path)
    return ConnectionManager(cfg).connect_all()
