"""A fault with a knowable cause must not be answered with a generic remedy.

Found on a real standalone ESXi, 2026-08-29. `vmware-monitor health events`
against a host with no vCenter raises `vmodl.fault.NotImplemented` — the event
query is a vCenter service, and a bare ESXi does not implement it. The CLI
translated that to:

    vSphere API fault: The requested operation is not implemented by the server.
    Run 'vmware-monitor doctor' to verify connectivity and credentials.

Doctor passes. Connectivity is fine, the credentials are fine, and the user is
sent to a check that confirms both and explains nothing. A remedy that cannot
apply is worse than no remedy: it costs a round trip and teaches the wrong
lesson about where the problem is.

The fault is specific and so is the answer — point at vCenter, and at the
read paths that do work on a standalone host.
"""

from __future__ import annotations

import typer
from pyVmomi import vmodl

from vmware_monitor.cli_base import cli_errors


def _raise(exc):
    @cli_errors
    def cmd():
        raise exc

    return cmd


def _message(capsys, exc) -> str:
    """The message as one line.

    Rich wraps to the terminal width, so a phrase can arrive split across a
    newline — "will \npass" is not "will pass" to `in`. Asserting against
    rendered output without normalising tests the wrapping, not the text.
    """
    try:
        _raise(exc)()
    except (typer.Exit, SystemExit):
        pass
    captured = capsys.readouterr()
    return " ".join((captured.out + captured.err).split())


def test_not_implemented_names_the_real_cause(capsys):
    msg = _message(capsys, vmodl.fault.NotImplemented(msg="not implemented by the server"))
    assert "vcenter" in msg.lower(), (
        f"the remedy does not mention vCenter, which is the actual "
        f"prerequisite: {msg!r}"
    )


def test_not_implemented_does_not_send_the_user_to_doctor(capsys):
    """Doctor passes on exactly the host that produces this fault.

    Mentioning doctor is fine and in fact useful — saying "doctor will pass"
    saves the trip. What must not survive is *instructing* the user to run it,
    which is the version that costs a round trip and points at the wrong layer.
    """
    msg = _message(capsys, vmodl.fault.NotImplemented(msg="not implemented by the server"))
    assert "run 'vmware-monitor doctor'" not in msg.lower(), (
        f"still routed to a check that passes here: {msg!r}"
    )
    assert "will pass" in msg.lower(), (
        f"does not tell the user doctor is not worth running: {msg!r}"
    )


def test_other_faults_keep_the_general_remedy(capsys):
    """Only the fault with a knowable cause gets a specific answer. A permission
    or connectivity fault genuinely is a doctor question."""
    msg = _message(capsys, vmodl.fault.SystemError(msg="boom"))
    assert "doctor" in msg.lower()
