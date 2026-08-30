"""Regression — second-pass batching of managed-object boundary reads (issue #31 tail).

``get_ntp_status``, ``get_host_services`` and ``get_certificate_status`` each
collect a per-host reference (``configManager.serviceSystem`` /
``configManager.certificateManager``) in one batched PropertyCollector call, but
the property they actually need — ``serviceInfo`` / ``certificateInfo`` — lives on
that *referenced* managed object, which a ``HostSystem`` container view cannot
cross. The naive code read ``ref.serviceInfo`` lazily, i.e. one extra SOAP
round-trip per host — the same issue #31 slowdown, one hop removed.

The fix adds a second batched pass (``_collect_objects``) that fetches the
boundary property for every reference in a single ``RetrievePropertiesEx``.

The reference objects below are real pyVmomi proxies backed by a stub that raises
on ANY invoked method, so if the code ever regresses to a lazy ``ref.serviceInfo``
read the test fails loudly instead of silently going O(N-hosts) round-trips.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

from pyVmomi import vim

from vmware_monitor.ops import health, infra_health

# --------------------------------------------------------------------------
# Fake PropertyCollector harness (routes by requested managed-object type, so
# the same fake serves both the HostSystem sweep and the boundary second pass)
# --------------------------------------------------------------------------


class _RaisingStub:
    """SOAP stub: any invoked method (== any lazy property read) is a bug."""

    def InvokeMethod(self, mo, info, args):  # noqa: N802 - pyVmomi contract
        raise AssertionError(
            "lazy SOAP property access on a boundary managed object — host checks"
            " must batch serviceInfo/certificateInfo via _collect_objects"
        )


class _NoLazyMO:
    """Fake host managed object: any attribute read is a lazy round-trip = a bug."""

    def __init__(self, label: str) -> None:
        object.__setattr__(self, "_label", label)

    def __getattr__(self, name: str):  # pragma: no cover - only hit on regression
        raise AssertionError(f"lazy property access '{name}' on host {self._label!r}")

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other


class _Prop:
    def __init__(self, name, val) -> None:
        self.name = name
        self.val = val


class _ObjContent:
    def __init__(self, obj, props: dict) -> None:
        self.obj = obj
        self.propSet = [_Prop(k, v) for k, v in props.items()]


class _Batch:
    def __init__(self, objects, token=None) -> None:
        self.objects = objects
        self.token = token


class _FakePropertyCollector:
    def __init__(self, fixtures: dict) -> None:
        self._fixtures = fixtures

    def RetrievePropertiesEx(self, specs, options):  # noqa: N802
        obj_type = specs[0].propSet[0].type
        rows = self._fixtures.get(obj_type, [])
        return _Batch([_ObjContent(o, p) for o, p in rows], token=None)

    def ContinueRetrievePropertiesEx(self, token):  # noqa: N802 - pragma: no cover
        return None


class _FakeStub:
    def InvokeMethod(self, mo, info, args):  # noqa: N802 - pyVmomi contract
        return None


class _FakeViewManager:
    def CreateContainerView(self, root, obj_type, recursive):  # noqa: N802
        return vim.view.ContainerView("cv-fake", _FakeStub())


class _FakeContent:
    def __init__(self, pc) -> None:
        self.viewManager = _FakeViewManager()
        self.propertyCollector = pc
        self.rootFolder = _NoLazyMO("rootFolder")


class _FakeSI:
    def __init__(self, fixtures: dict) -> None:
        self._content = _FakeContent(_FakePropertyCollector(fixtures))

    def RetrieveContent(self):  # noqa: N802
        return self._content


def _si(fixtures):
    return _FakeSI(fixtures)


# --------------------------------------------------------------------------
# Boundary payload fakes (plain objects — not proxies; read after batching)
# --------------------------------------------------------------------------


class _Svc:
    def __init__(self, key, label, running, policy) -> None:
        self.key = key
        self.label = label
        self.running = running
        self.policy = policy


class _ServiceInfo:
    def __init__(self, services) -> None:
        self.service = services


def _svc_ref(moid: str) -> vim.HostServiceSystem:
    return vim.HostServiceSystem(moid, _RaisingStub())


def _cert_ref(moid: str) -> vim.HostCertificateManager:
    return vim.HostCertificateManager(moid, _RaisingStub())


# --------------------------------------------------------------------------
# get_host_services — serviceInfo batched, no lazy ref.serviceInfo
# --------------------------------------------------------------------------


def test_get_host_services_batches_serviceinfo():
    ss1, ss2 = _svc_ref("hss-1"), _svc_ref("hss-2")
    fixtures = {
        vim.HostSystem: [
            (_NoLazyMO("esx-01"), {"name": "esx-01", "configManager.serviceSystem": ss1}),
            (_NoLazyMO("esx-02"), {"name": "esx-02", "configManager.serviceSystem": ss2}),
        ],
        vim.HostServiceSystem: [
            (ss1, {"serviceInfo": _ServiceInfo([_Svc("ntpd", "NTP Daemon", True, "on")])}),
            (ss2, {"serviceInfo": _ServiceInfo([_Svc("TSM-SSH", "SSH", False, "off")])}),
        ],
    }
    rows = health.get_host_services(_si(fixtures))["items"]
    assert {(r["host"], r["service"]) for r in rows} == {
        ("esx-01", "ntpd"),
        ("esx-02", "TSM-SSH"),
    }


def test_get_host_services_host_filter_batches_only_match():
    ss1, ss2 = _svc_ref("hss-1"), _svc_ref("hss-2")
    fixtures = {
        vim.HostSystem: [
            (_NoLazyMO("esx-01"), {"name": "esx-01", "configManager.serviceSystem": ss1}),
            (_NoLazyMO("esx-02"), {"name": "esx-02", "configManager.serviceSystem": ss2}),
        ],
        vim.HostServiceSystem: [
            (ss2, {"serviceInfo": _ServiceInfo([_Svc("ntpd", "NTP", True, "on")])}),
        ],
    }
    rows = health.get_host_services(_si(fixtures), host_name="esx-02")["items"]
    assert [r["host"] for r in rows] == ["esx-02"]
    assert rows[0]["running"] is True


# --------------------------------------------------------------------------
# get_ntp_status — serviceInfo batched, dateTimeInfo read from batched prop
# --------------------------------------------------------------------------


def test_get_ntp_status_batches_serviceinfo():
    ss1 = _svc_ref("hss-1")
    dt = types.SimpleNamespace(ntpConfig=types.SimpleNamespace(server=["pool.ntp.org"]))
    fixtures = {
        vim.HostSystem: [
            (
                _NoLazyMO("esx-01"),
                {
                    "name": "esx-01",
                    # Load-bearing since 2026-08-30: a host whose state is not
                    # "connected" is reported as unknown rather than judged.
                    "runtime.connectionState": "connected",
                    "config.dateTimeInfo": dt,
                    "configManager.serviceSystem": ss1,
                },
            ),
        ],
        vim.HostServiceSystem: [
            (ss1, {"serviceInfo": _ServiceInfo([_Svc("ntpd", "NTP", True, "on")])}),
        ],
    }
    rows = infra_health.get_ntp_status(_si(fixtures))["items"]
    assert len(rows) == 1
    assert rows[0]["host"] == "esx-01"
    assert rows[0]["ntp_servers"] == ["pool.ntp.org"]
    assert rows[0]["ntpd_running"] is True
    assert rows[0]["healthy"] is True


def test_get_ntp_status_no_service_system_is_unknown_not_unhealthy():
    """Renamed and inverted on 2026-08-30, deliberately.

    This host is connected, but neither its dateTimeInfo nor its serviceSystem
    could be read — so nothing about its NTP configuration was observed.
    ``healthy: False`` said it is misconfigured, which is a finding, and the
    finding was manufactured out of two defaults. It is the same defect the
    unreachable-host fix addresses, one level down: not unreached, but unread.

    A host that answers with dateTimeInfo and no NTP server still reports
    ``healthy: False`` — see the neighbouring test — so the actionable signal
    this tool exists for is untouched.
    """
    fixtures = {
        vim.HostSystem: [
            (
                _NoLazyMO("esx-99"),
                {
                    "name": "esx-99",
                    "runtime.connectionState": "connected",
                    "config.dateTimeInfo": None,
                    "configManager.serviceSystem": None,
                },
            ),
        ],
    }
    rows = infra_health.get_ntp_status(_si(fixtures))["items"]
    assert rows[0]["reachable"] is True
    assert rows[0]["ntpd_running"] is None
    assert rows[0]["ntp_servers"] is None
    assert rows[0]["healthy"] is None


def test_get_ntp_status_configured_with_no_servers_is_still_unhealthy():
    """The control for the test above: a real misconfiguration must still be
    reported as one, without needing the service state to confirm it."""
    fixtures = {
        vim.HostSystem: [
            (
                _NoLazyMO("esx-98"),
                {
                    "name": "esx-98",
                    "runtime.connectionState": "connected",
                    "config.dateTimeInfo": types.SimpleNamespace(
                        ntpConfig=types.SimpleNamespace(server=[])
                    ),
                    "configManager.serviceSystem": None,
                },
            ),
        ],
    }
    rows = infra_health.get_ntp_status(_si(fixtures))["items"]
    assert rows[0]["ntp_servers"] == []
    assert rows[0]["healthy"] is False


# --------------------------------------------------------------------------
# get_certificate_status — certificateInfo batched, no lazy ref.certificateInfo
# --------------------------------------------------------------------------


def test_get_certificate_status_batches_certificateinfo():
    now = datetime.now(tz=timezone.utc)
    cm1, cm2 = _cert_ref("cm-1"), _cert_ref("cm-2")
    fixtures = {
        vim.HostSystem: [
            (_NoLazyMO("esx-01"), {"name": "esx-01", "configManager.certificateManager": cm1}),
            (_NoLazyMO("esx-02"), {"name": "esx-02", "configManager.certificateManager": cm2}),
        ],
        vim.HostCertificateManager: [
            (cm1, {"certificateInfo": types.SimpleNamespace(notAfter=now + timedelta(days=200))}),
            (cm2, {"certificateInfo": types.SimpleNamespace(notAfter=now + timedelta(days=5))}),
        ],
    }
    rows = infra_health.get_certificate_status(_si(fixtures))["items"]
    # soonest-to-expire first, and the 5-day cert is flagged expiring
    assert rows[0]["host"] == "esx-02"
    assert rows[0]["expiring"] is True
    assert rows[1]["host"] == "esx-01"
    assert rows[1]["expiring"] is False
