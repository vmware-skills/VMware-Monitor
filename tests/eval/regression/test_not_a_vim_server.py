"""A booting vCenter must not produce a Python traceback.

Found live, 2026-08-29, while a vCSA was starting up. Port 443 was accepting
connections but the VIM service was not answering yet, and pyVim's SmartConnect
raises a **bare `builtins.Exception`** for that — not OSError, not
ConnectionError. `cli_errors` catches FileNotFoundError, KeyError, OSError and
vmodl faults, so this one walked straight past it and twenty lines of pyVim
internals landed in the user's terminal.

The handler's own docstring says "Without this, config/auth/network problems
surface as raw tracebacks". This is a network problem surfacing as a raw
traceback: the promise was wider than the implementation.

The state is not exotic. A vCenter mid-reboot, a wrong port, a proxy in the
way, or an HTTPS service that simply is not vCenter all land here.

Catching bare Exception wholesale would be the wrong fix — it would swallow
genuine programming errors and turn a bug into a polite sentence. Only the
known signature is translated; everything else still propagates.
"""

from __future__ import annotations

import pytest
import typer

from vmware_monitor.cli_base import cli_errors


def _run(exc):
    @cli_errors
    def cmd():
        raise exc

    return cmd


def _message(capsys, exc) -> str:
    try:
        _run(exc)()
    except (typer.Exit, SystemExit):
        pass
    cap = capsys.readouterr()
    return " ".join((cap.out + cap.err).split())


#: Verbatim from pyVim.connect.SmartConnect against a vCSA that was still
#: booting. Reproduced by connecting to a host whose 443 is open but whose VIM
#: service has not started.
_PYVIM = Exception("192.168.60.16:443 is down or is not a VIM server")


def test_a_booting_vcenter_gets_a_sentence_not_a_traceback(capsys):
    msg = _message(capsys, _PYVIM)
    assert msg, "the exception escaped the handler entirely"
    assert "192.168.60.16:443" in msg


def test_the_message_names_the_likely_cause(capsys):
    """TCP is open, so 'check the network' is unhelpful. The distinguishing
    fact is that something answered and it was not vCenter."""
    msg = _message(capsys, _PYVIM).lower()
    assert "starting up" in msg or "still booting" in msg


def test_a_real_programming_error_is_not_swallowed(capsys):
    """The fix must not become a blanket except. A NameError here is a bug in
    this codebase, and turning it into a friendly sentence would hide it."""
    with pytest.raises(NameError):
        _run(NameError("no such name"))()
