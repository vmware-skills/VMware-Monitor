"""A 404 from a 9.1-only endpoint must not be explained as a bad cluster MoID.

``GET /api/vcenter/deployment/size`` is annotated "NEW in 9.1" in spec §D and
takes no id at all — yet an 8.x vCenter's 404 was answered with "confirm it with
'vmware-monitor inventory clusters' — the REST API wants the cluster MoID". The
operator is sent to check an argument the call does not have.
"""

from __future__ import annotations

import httpx

from vmware_monitor.ops.patching import REQUIRES_DEPLOYMENT_SIZE
from vmware_monitor.rest import RestNotFoundError, _translate_status

PATH = "/api/vcenter/deployment/size"


def _404() -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        "not found",
        request=httpx.Request("GET", f"https://vc.example.test{PATH}"),
        response=httpx.Response(404),
    )


def test_the_moid_advice_is_gone_from_a_version_gated_404() -> None:
    err = _translate_status(_404(), PATH, REQUIRES_DEPLOYMENT_SIZE)
    assert isinstance(err, RestNotFoundError)
    msg = str(err)
    assert "9.1+" in msg
    assert "cluster MoID" not in msg, "still sending the operator after an id this call has no field for"
    assert PATH in msg


def test_it_names_the_floor_without_claiming_the_build_is_old() -> None:
    """VsphereRest holds a session, not a version — so it must not guess one.

    Asserting "your vCenter is 8.0" from a 404 alone would be a confident claim
    about something nobody read (形态 #1 inverted: an unknown rendered as a fact).
    """
    msg = str(_translate_status(_404(), PATH, REQUIRES_DEPLOYMENT_SIZE))
    assert "could not be read" in msg
    assert "reports" not in msg


def test_an_ungated_404_keeps_the_moid_advice() -> None:
    """The two compliance paths ARE cluster-templated, and are not 9.1-only.

    Their 404 usually is a bad MoID, so the original remedy must survive
    untouched — the floor is declared per call site, not per file.
    """
    msg = str(_translate_status(_404(), "/api/esx/settings/clusters/domain-c9/software/compliance"))
    assert "cluster MoID" in msg
    assert "9.1+" not in msg


def test_get_deployment_size_passes_the_floor_through_the_real_client(monkeypatch) -> None:
    """Drives the real VsphereRest, patching only the HTTP transport.

    The first version of this test replaced ``VsphereRest`` wholesale with a
    stand-in whose ``get_json`` accepted ``requires`` — so it proved only that
    the ops layer passes the argument, never that the client threads it to the
    place that raises. It did not: ``requires`` was a parameter of ``get_json``
    while the raise lives in ``_get_once``, and the call there referenced a name
    that was not in scope. Every test passed; the first real 8.0.3 vCenter
    answered ``NameError: name 'requires' is not defined``.

    形态 #3 twice over — the second time inside a test written *for* a
    version-floor bug. So this one keeps the whole client and fakes only what is
    genuinely outside the process.
    """
    import httpx
    import pytest

    from vmware_monitor.ops import patching
    from vmware_monitor.rest import VsphereRest

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

    target = type("T", (), {"host": "vc.example.test", "port": 443, "verify_ssl": False})()
    with pytest.raises(RestNotFoundError) as exc:
        patching.get_deployment_size(target)

    msg = str(exc.value)
    assert "9.1+" in msg, "the floor never reached the code that raises"
    assert "cluster MoID" not in msg


def test_an_ungated_call_through_the_real_client_keeps_the_moid_advice(monkeypatch) -> None:
    """The same path, without a floor, must be untouched.

    Guards the other direction: a version story on every 404 would bury the real
    cause for the two cluster-templated compliance reads, whose 404 usually *is*
    a bad MoID.
    """
    import httpx
    import pytest

    from vmware_monitor.rest import VsphereRest

    class _Resp:
        status_code = 404

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "not found",
                request=httpx.Request("GET", "https://vc.example.test/api/x"),
                response=httpx.Response(404),
            )

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, path, headers=None):
            return _Resp()

    monkeypatch.setattr(VsphereRest, "_login", lambda self: "t")
    monkeypatch.setattr(VsphereRest, "_client", lambda self: _Client())

    target = type("T", (), {"host": "vc.example.test", "port": 443, "verify_ssl": False})()
    rest = VsphereRest(target)
    with pytest.raises(RestNotFoundError) as exc:
        rest.get_json("/api/esx/settings/clusters/domain-c9/software/compliance")
    assert "cluster MoID" in str(exc.value)


def test_a_readable_version_makes_the_message_definitive(monkeypatch) -> None:
    """When the appliance says 8.0.3, say 8.0.3 — do not hedge.

    The first cut of this always passed ``None`` and printed "the running
    version could not be read", which was honest but weaker than the truth:
    ``GET /api/appliance/system/version`` answers on a live 8.0.3 (observed
    2026-08-31, body ``{"version": "8.0.3.00000", ...}``).
    """
    from vmware_monitor.rest import VsphereRest, _VERSION_PATH

    monkeypatch.setattr(VsphereRest, "_login", lambda self: "t")

    def _get_once(self, path, token, requires=None):
        if path == _VERSION_PATH:
            return {"version": "8.0.3.00000", "build": "24022515"}
        raise _translate_status(_404(), path, requires, self.product_version())

    monkeypatch.setattr(VsphereRest, "_get_once", _get_once)
    target = type("T", (), {"host": "vc.example.test", "port": 443, "verify_ssl": False})()
    rest = VsphereRest(target)
    import pytest

    with pytest.raises(RestNotFoundError) as exc:
        rest.get_json(PATH, requires=REQUIRES_DEPLOYMENT_SIZE)
    msg = str(exc.value)
    assert "8.0.3.00000" in msg and "9.1" in msg
    assert "could not be read" not in msg


def test_an_unreadable_version_probe_does_not_replace_the_original_error(monkeypatch) -> None:
    """The version probe runs inside an error path, so it must never raise.

    If reading the version blew up, the operator would get that exception
    instead of the 404 they actually hit.
    """
    from vmware_monitor.rest import VsphereRest, _VERSION_PATH

    monkeypatch.setattr(VsphereRest, "_login", lambda self: "t")

    def _get_once(self, path, token, requires=None):
        if path == _VERSION_PATH:
            raise RuntimeError("version endpoint exploded")
        raise _translate_status(_404(), path, requires, self.product_version())

    monkeypatch.setattr(VsphereRest, "_get_once", _get_once)
    target = type("T", (), {"host": "vc.example.test", "port": 443, "verify_ssl": False})()
    rest = VsphereRest(target)
    import pytest

    with pytest.raises(RestNotFoundError) as exc:
        rest.get_json(PATH, requires=REQUIRES_DEPLOYMENT_SIZE)
    assert "could not be read" in str(exc.value)


def test_the_version_is_probed_once_not_per_404(monkeypatch) -> None:
    """An appliance that cannot answer must not be asked again on every error."""
    from vmware_monitor.rest import VsphereRest, _VERSION_PATH

    calls = []
    monkeypatch.setattr(VsphereRest, "_login", lambda self: "t")

    def _get_once(self, path, token, requires=None):
        if path == _VERSION_PATH:
            calls.append(path)
            raise RuntimeError("no")
        raise _translate_status(_404(), path, requires, self.product_version())

    monkeypatch.setattr(VsphereRest, "_get_once", _get_once)
    target = type("T", (), {"host": "vc.example.test", "port": 443, "verify_ssl": False})()
    rest = VsphereRest(target)
    import pytest

    for _ in range(3):
        with pytest.raises(RestNotFoundError):
            rest.get_json(PATH, requires=REQUIRES_DEPLOYMENT_SIZE)
    assert len(calls) == 1, f"version probed {len(calls)} times, expected 1"


def test_vcenters_own_explanation_beats_a_guess() -> None:
    """vCenter writes the reason into the body; discarding it to guess is worse.

    ``last-apply-result`` 404s on a cluster that has simply never been
    remediated, and vCenter says exactly that. The tool answered "if this used a
    cluster id, confirm it" — sending the operator to re-check an id that was
    correct, with the real answer sitting unread in the response.
    """
    import httpx

    from vmware_monitor.rest import _translate_status

    body = {
        "error_type": "NOT_FOUND",
        "messages": [{"default_message": "The last remediation results for entity mgmt-cl01 are unavailable."}],
    }
    err = _translate_status(
        httpx.HTTPStatusError(
            "x",
            request=httpx.Request("GET", "https://vc/x"),
            response=httpx.Response(404, json=body),
        ),
        "/api/esx/settings/clusters/domain-c9/software/reports/last-apply-result",
    )
    msg = str(err)
    assert "are unavailable" in msg
    # ...and the MoID pointer survives, because the two are additive: vCenter
    # names the id back at you without saying what shape it should have been.
    assert "domain-c123" in msg


def test_a_body_with_nothing_to_say_keeps_the_original_remedy() -> None:
    import httpx

    from vmware_monitor.rest import _translate_status

    for body in ({}, {"messages": []}, {"messages": [{"x": 1}]}, {"messages": "nope"}):
        err = _translate_status(
            httpx.HTTPStatusError(
                "x", request=httpx.Request("GET", "https://vc/x"),
                response=httpx.Response(404, json=body),
            ),
            "/api/esx/settings/clusters/domain-c9/software/compliance",
        )
        assert "cluster MoID" in str(err), f"{body!r} lost the fallback"


def test_a_non_json_body_does_not_crash_the_translator() -> None:
    """The translator runs while another error is being built; it must not raise."""
    import httpx

    from vmware_monitor.rest import _translate_status

    err = _translate_status(
        httpx.HTTPStatusError(
            "x", request=httpx.Request("GET", "https://vc/x"),
            response=httpx.Response(404, text="<html>gateway page</html>"),
        ),
        "/api/x",
    )
    assert "vCenter has no resource" in str(err)
