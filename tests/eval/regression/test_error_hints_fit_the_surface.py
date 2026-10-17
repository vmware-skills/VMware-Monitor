"""Error hints must fit the error, and the surface that shows them.

Real hardware, 2026-10-17, three shapes:

* Every MCP error carried ``hint: "Run 'vmware-monitor doctor' to verify
  connectivity and credentials."`` -- also for a VM name that does not resolve or
  an out-of-range argument, where connectivity is fine and the message itself
  already says what to change. An agent that follows the hint runs doctor, sees
  green, and is no closer.
* Messages an agent reads told it to run ``'vmware-monitor inventory clusters'``
  or ``pass --target`` -- a CLI command and a CLI flag. The MCP names are
  ``list_all_clusters`` and the ``target`` parameter.
* Those REST messages sent the reader to the cluster listing for a cluster
  MoID, and the listing had no MoID in it on either surface.
"""

from __future__ import annotations

import httpx
import pytest
from pyVmomi import vim

from vmware_monitor.mcp_server import server
from vmware_monitor.ops import inventory
from vmware_monitor.ops.vm_info import VMNotFoundError
from vmware_monitor.rest import _SESSION_PATH, _translate_status


def _unknown_target():
    from vmware_monitor.config import UnknownTargetError

    return UnknownTargetError("Target 'x' not found.")


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc",
    [ValueError("days must be 1..365"), VMNotFoundError("VM 'x' not found."), _unknown_target()],
)
def test_an_authored_non_connectivity_error_does_not_send_the_agent_to_doctor(exc) -> None:
    assert server._hint_for(exc) != server._DOCTOR_HINT
    assert "doctor" not in server._hint_for(exc)


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc", [ConnectionError("dropped"), TimeoutError("slow"), RuntimeError("?")]
)
def test_connectivity_and_unplanned_errors_keep_the_doctor_hint(exc) -> None:
    assert server._hint_for(exc) == server._DOCTOR_HINT


def _err(code: int, path: str) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        "x",
        request=httpx.Request("GET", f"https://vc.example.test{path}"),
        response=httpx.Response(code),
    )


@pytest.mark.unit
def test_bad_cluster_moid_hint_names_the_mcp_tool() -> None:
    path = "/api/esx/settings/clusters/domain-c9/software"
    msg = str(_translate_status(_err(404, path), path))
    assert "list_all_clusters" in msg
    assert "moid" in msg


@pytest.mark.unit
def test_esxi_rest_refusal_names_the_target_parameter() -> None:
    msg = str(_translate_status(_err(400, _SESSION_PATH), _SESSION_PATH))
    assert "target=" in msg or "'target'" in msg


@pytest.mark.unit
def test_cluster_rows_carry_the_moid_the_hints_promise(monkeypatch) -> None:
    ref = vim.ClusterComputeResource("domain-c42", None)
    monkeypatch.setattr(inventory, "_collect", lambda si, types, props: [(ref, {"name": "c1"})])

    row = inventory.list_clusters(object())["items"][0]

    assert row["moid"] == "domain-c42"



@pytest.mark.unit
@pytest.mark.parametrize("exc", [
    __import__("json").JSONDecodeError("Expecting value", "<html>", 0),
    KeyError("summary.capacity"),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
])
def test_parse_errors_and_bare_key_errors_keep_the_doctor_hint(exc) -> None:
    """Independent review 2026-10-17: a reverse proxy answering HTML raises
    JSONDecodeError (a ValueError), and a programming slip raises a bare KeyError;
    neither message says what to change, so they must not be told it does."""
    assert server._hint_for(exc) == server._DOCTOR_HINT


@pytest.mark.unit
def test_an_unknown_target_is_still_a_key_error() -> None:
    """Callers catch KeyError from get_target; the subclass must not break them."""
    assert issubclass(type(_unknown_target()), KeyError)


@pytest.mark.unit
def test_html_from_the_session_endpoint_names_a_reverse_proxy(monkeypatch) -> None:
    """A proxy answering HTML 200 used to escape as a bare JSONDecodeError."""
    from types import SimpleNamespace

    from vmware_monitor import rest

    request = httpx.Request("POST", "https://vc.example.test/api/session")
    html = httpx.Response(200, text="<html>login</html>", request=request)
    transport = httpx.MockTransport(lambda request: html)
    target = SimpleNamespace(username="u", password="p", verify_ssl=True,
                             host="vc.example.test", port=443)
    monkeypatch.setattr(rest, "_base_url", lambda t: "https://vc.example.test")
    client = rest.VsphereRest(target)
    monkeypatch.setattr(
        client, "_client",
        lambda: httpx.Client(base_url="https://vc.example.test", transport=transport),
    )

    with pytest.raises(rest.RestAuthError, match="reverse proxy") as info:
        client._login()
    assert server._hint_for(info.value) == server._DOCTOR_HINT
