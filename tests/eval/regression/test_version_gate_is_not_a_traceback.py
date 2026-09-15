"""Regression — a vSphere version gate is an answer, not a traceback.

Found 2026-09-15 against a lab vCenter 8.0.3. ``vmware-monitor deployment-size``
printed forty lines of Rich traceback ending in::

    RestNotFoundError: Appliance deployment-size read requires vCenter 9.1 or
    newer; this appliance reports 8.0.3.00000. ...

The sentence was right and already authored; nothing on the way to the terminal
knew what to do with it. ``get_deployment_size`` caught only ``RestNotReadyError``,
and ``cli_errors`` had no clause for the REST layer's authored errors, so the same
traceback followed any 404 from ``patch compliance`` / ``patch last-apply`` (a
mistyped cluster MoID) and the pre-8.0U3 memory-tiering teaching error.

Two contracts are pinned here:

* A version gate is ``available: False`` with the reason, the same shape the
  command already used for a mid-patch 503. The audit row is ``ok``: the call
  reached the appliance and got a definitive answer ("this build does not have
  it"). Recording ``error`` would feed the circuit breaker for a question that
  was answered correctly, and would be indistinguishable from a real failure.
* A 404 that is *not* a version gate (the floor is met) still raises: an
  unexplained 404 on a 9.1 appliance is not "unavailable", it is unknown.
"""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from vmware_monitor.cli import app
from vmware_monitor.ops import patching
from vmware_monitor.rest import RestNotFoundError, VsphereRest

PATH = patching.DEPLOYMENT_SIZE_PATH
runner = CliRunner()


class _Target:
    name = "lab-vc"
    host = "vc.example.test"
    port = 443
    verify_ssl = False
    username = "reader"
    password = "not-used"


def _fake_404_transport(monkeypatch, version: str | None) -> None:
    """Keep the real VsphereRest; fake only what is outside the process."""

    class _Resp:
        status_code = 404

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "not found",
                request=httpx.Request("GET", f"https://vc.example.test{PATH}"),
                response=httpx.Response(404),
            )

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, path, headers=None):
            return _Resp()

    monkeypatch.setattr(VsphereRest, "_login", lambda self: "session-token")
    monkeypatch.setattr(VsphereRest, "_client", lambda self: _Client())
    monkeypatch.setattr(VsphereRest, "product_version", lambda self: version)


class _Recorder:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def log(self, **kwargs) -> None:
        self.rows.append(kwargs)


@pytest.fixture
def audit(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr("vmware_policy.guard.get_engine", lambda: recorder)
    return recorder


@pytest.fixture
def target(monkeypatch):
    monkeypatch.setattr(
        "vmware_monitor.cli_vsphere91._target_config", lambda t, c: (_Target(), _Target.name)
    )
    return _Target()


# ─── ops layer ────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_deployment_size_on_an_older_vcenter_is_unavailable_not_raised(monkeypatch):
    _fake_404_transport(monkeypatch, "8.0.3.00000")
    out = patching.get_deployment_size(_Target())
    assert out["available"] is False
    assert "9.1" in out["reason"] and "8.0.3" in out["reason"]
    assert "cluster MoID" not in out["reason"]


@pytest.mark.unit
def test_an_unreadable_version_still_raises_and_claims_no_version(monkeypatch):
    """Review 2026-09-15 (M1): with the version unreadable the first version
    returned ``available: False`` with the note "Not a failure: this endpoint does
    not exist on this appliance version" — beside a reason saying the version
    could not be read. Only a version actually read below the floor is a gate."""
    from vmware_monitor.rest import RestVersionGateError

    _fake_404_transport(monkeypatch, None)
    with pytest.raises(RestNotFoundError) as exc:
        patching.get_deployment_size(_Target())
    assert not isinstance(exc.value, RestVersionGateError)
    assert "could not be read" in str(exc.value)
    assert "reports" not in str(exc.value)


@pytest.mark.unit
def test_an_esxi_target_is_told_the_command_needs_a_vcenter():
    """Lab, 2026-09-15 (L3): `deployment-size --target home-esxi` printed
    "vCenter REST call to /api/session failed (400)." and nothing else. An ESXi
    host has no vSphere Automation REST API; that is the next step to name."""
    from vmware_monitor.rest import _SESSION_PATH, _translate_status

    err = httpx.HTTPStatusError(
        "bad request",
        request=httpx.Request("POST", f"https://esx.example.test{_SESSION_PATH}"),
        response=httpx.Response(400),
    )
    msg = str(_translate_status(err, _SESSION_PATH))
    assert "(400)" in msg
    assert "vCenter target" in msg and "ESXi" in msg


@pytest.mark.unit
def test_a_404_on_an_appliance_that_meets_the_floor_still_raises(monkeypatch):
    _fake_404_transport(monkeypatch, "9.1.0.0")
    with pytest.raises(RestNotFoundError):
        patching.get_deployment_size(_Target())


# ─── CLI surface ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cli_deployment_size_prints_one_line_and_audits_ok(monkeypatch, target, audit):
    _fake_404_transport(monkeypatch, "8.0.3.00000")
    result = runner.invoke(app, ["deployment-size", "--target", "lab-vc"])
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert result.exception is None, repr(result.exception)
    assert "9.1" in result.output and "8.0.3" in result.output
    rows = [r for r in audit.rows if r.get("tool") == "vcenter_deployment_size"]
    assert rows and rows[-1]["status"] == "ok", audit.rows


@pytest.mark.unit
@pytest.mark.parametrize("sub", ["compliance", "last-apply"])
def test_cli_patch_404_is_one_teaching_line(monkeypatch, target, sub):
    def _raise(*_a, **_k):
        raise RestNotFoundError(
            "vCenter says: Entity 'domain-cBOGUS' does not exist or is not of type "
            "'cluster'. (404 at /api/esx/settings/clusters/domain-cBOGUS/software/compliance)."
        )

    fn = "get_patch_compliance" if sub == "compliance" else "get_last_apply_result"
    monkeypatch.setattr(patching, fn, _raise)
    result = runner.invoke(app, ["patch", sub, "domain-cBOGUS", "--target", "lab-vc"])
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "domain-cBOGUS" in result.output


@pytest.mark.unit
def test_cli_memory_tiering_on_a_pre_803_target_is_one_teaching_line(monkeypatch):
    from pyVmomi import vmodl

    from vmware_monitor.ops import memory_tiering

    def _collect(si, types, paths):
        raise vmodl.query.InvalidProperty(name="hardware.memoryTieringType")

    monkeypatch.setattr(memory_tiering, "_collect", _collect)
    monkeypatch.setattr(
        "vmware_monitor.cli_vsphere91.get_connection", lambda t, c: (object(), None, "lab")
    )
    result = runner.invoke(app, ["memory", "tiering", "--target", "lab"])
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "8.0U3" in result.output
