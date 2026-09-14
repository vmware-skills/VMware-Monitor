"""Regression — license_status says which license each asset is assigned.

Found 2026-09-14 on a lab vCenter 8.0.3. vCenter showed "Expired vCenter Server
license" (since 2026-06-01) while `infra licenses` listed only valid licenses
expiring 2027-06-28. Whether the alarm was stale or an asset really still ran on
an expired key could not be answered: the tool listed the license inventory and
said outright that it did not report which asset consumes which license.

``LicenseAssignmentManager.QueryAssignedLicenses`` (System.View) answers it. On
the lab: the vCenter (192.168.60.16) is on "vCenter Server 8 Standard" and both
hosts on "vSphere 8 Enterprise Plus for VCF", all expiring 2027-06-28 — so the
alarm is historical. ``assignments`` now carries one row per asset with its
license name, edition, expiration and ``expired``. The license key itself is
never returned. When assignments cannot be read, ``assignments`` is ``None`` with
``assignments_note`` — not an empty list.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pyVmomi import vim

from vmware_monitor.ops import infra_health

_UUID = "21e8a2f1-5484-4205-96b6-612afce2bde5"
_NOW = datetime.now(timezone.utc)


def _license(name, edition, expires):
    props = [SimpleNamespace(key="expirationDate", value=expires)] if expires else []
    return SimpleNamespace(
        name=name,
        editionKey=edition,
        licenseKey="AAAAA-SECRET-KEY-00000",
        total=288,
        used=None,
        properties=props,
    )


def _assignment(entity_id, scope, display, lic):
    return SimpleNamespace(
        entityId=entity_id, scope=scope, entityDisplayName=display, assignedLicense=lic
    )


def _si(assignments=None, fault=None, has_manager=True):
    vc = _license("vCenter Server 8 Standard", "vc.standard.instance", _NOW + timedelta(days=300))
    esx = _license(
        "vSphere 8 Enterprise Plus for VCF", "esx.vcf.cpuCoreMin", _NOW + timedelta(days=300)
    )

    def query(entityId=None):  # noqa: N803 — pyVmomi keyword
        if fault is not None:
            raise fault
        return (
            assignments
            if assignments is not None
            else [
                _assignment("host-13", _UUID, "192.168.60.15", esx),
                _assignment(_UUID, None, "192.168.60.16", vc),
            ]
        )

    lam = SimpleNamespace(QueryAssignedLicenses=query)
    mgr = SimpleNamespace(licenses=[vc, esx])
    if has_manager:
        mgr.licenseAssignmentManager = lam
    return SimpleNamespace(RetrieveContent=lambda: SimpleNamespace(licenseManager=mgr))


@pytest.mark.unit
def test_each_asset_names_its_license_and_whether_it_expired():
    out = infra_health.get_license_status(_si())
    rows = {r["asset"]: r for r in out["assignments"]}
    vc, host = rows["192.168.60.16"], rows["192.168.60.15"]
    assert (vc["kind"], vc["license_name"], vc["expired"]) == (
        "vcenter",
        "vCenter Server 8 Standard",
        False,
    )
    assert (host["kind"], host["asset_id"], host["expired"]) == ("host", "host-13", False)
    assert "assignments_note" not in out


@pytest.mark.unit
def test_an_expired_assignment_is_flagged():
    old = _license("vCenter Server 8 Standard", "vc.standard.instance", _NOW - timedelta(days=5))
    perpetual = _license("Perpetual", "esx.perpetual", None)
    out = infra_health.get_license_status(
        _si(
            assignments=[
                _assignment(_UUID, None, "vc-01", old),
                _assignment("host-9", _UUID, "esx-09", perpetual),
            ]
        )
    )
    rows = {r["asset"]: r for r in out["assignments"]}
    assert rows["vc-01"]["expired"] is True
    assert rows["esx-09"]["expired"] is False and rows["esx-09"]["expiration"] == "never"
    assert "1 asset" in out["assignments_expired_note"]


@pytest.mark.unit
def test_the_license_key_is_never_returned():
    assert "SECRET-KEY" not in json.dumps(infra_health.get_license_status(_si()), default=str)


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs,words",
    [
        ({"fault": vim.fault.NoPermission()}, "System.View"),
        ({"has_manager": False}, "does not expose"),
    ],
)
def test_unreadable_assignments_are_none_not_empty(kwargs, words):
    out = infra_health.get_license_status(_si(**kwargs))
    assert out["assignments"] is None
    assert words in out["assignments_note"]
    assert len(out["items"]) == 2, "the inventory must still be returned"


@pytest.mark.unit
def test_cli_and_mcp_show_assignments(monkeypatch):
    import inspect

    from rich.console import Console
    from typer.testing import CliRunner

    from vmware_monitor import cli, cli_observability
    from vmware_monitor.mcp_server import server

    assert "assignments" in inspect.getdoc(server.license_status)
    monkeypatch.setattr(
        cli_observability, "get_connection", lambda target, config: (_si(), None, "vc")
    )
    monkeypatch.setattr(cli_observability, "console", Console(width=220, color_system=None))
    result = CliRunner().invoke(cli.app, ["infra", "licenses"])
    assert result.exit_code == 0, result.output
    assert "192.168.60.16" in result.output and "vCenter Server 8 Standard" in result.output
