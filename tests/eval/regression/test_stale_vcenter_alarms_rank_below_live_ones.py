"""Regression — vCenter-level alarms: checked where the skill holds evidence, labelled,
and ranked below live problems when they are stale.

Found 2026-09-15 on a lab vCenter 8.0.3. ``attention`` ranked "Expired vCenter
Server license" CRITICAL beside live problems. ``infra licenses`` showed the
vCenter (asset 21e8a2f1-…, 192.168.60.16) on "vCenter Server 8 Standard" valid to
2027-06-28, but ``health alarms`` said ``Condition now: unknown`` because only
state-based alarms were re-evaluated. Its definition, read through the skill::

    systemName alarm.LicenseExpiredVc
    OR(EventAlarmExpression eventTypeId=com.vmware.license.VcLicenseExpiredEvent
       status=red, no comparisons)

The skill holds the evidence that decides that condition: which license the
vCenter itself is assigned and when it expires. With a readable, unexpired
expiry for *this* vCenter (matched by ``about.instanceUuid``) the verdict is
``cleared``; an expired one is ``holds``; anything short of that stays
``unknown``. Unknown is not a verdict.

The same view showed "Memory Exhaustion on 192" on "Datacenters". The name is
vCenter's own (its AlarmStatusChangedEvent quotes it); the object was the root
folder. The definition identifies it without reading any name::

    OR(EventAlarmExpression eventTypeId=vim.event.ResourceExhaustionStatusChangedEvent
       comparisons resourceName=mem_usage, newStatus=<colour>,
                   _sourcehost_=192.168.60.16) × green/yellow/red

and "Root user password expired." is ``com.vmware.vc.system.RootPasswordExpiredEvent``.
Those are labelled as the vCenter appliance (with its address when the definition
carries one). ``entity_name`` is unchanged — vmware-aiops resolves it to reset or
acknowledge the alarm.

Ranking: a cleared alarm, or one whose condition cannot be re-checked and that
someone acknowledged a week or more ago, is listed after live problems of any
severity — never dropped.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pyVmomi import vim
from rich.console import Console
from typer.testing import CliRunner

from vmware_monitor.ops import alarm_condition as ac
from vmware_monitor.ops import attention, cluster_summary, health

VC_UUID = "21e8a2f1-5484-4205-96b6-612afce2bde5"
NOW = datetime.now(timezone.utc)
ROOT = vim.Folder("group-d1")
HOST = vim.HostSystem("host-13")


def _event(event_type_id, status="red", comparisons=()):
    return vim.alarm.EventAlarmExpression(
        eventType=vim.event.EventEx,
        eventTypeId=event_type_id,
        status=status,
        comparisons=[
            vim.alarm.EventAlarmExpression.Comparison(attributeName=a, operator="equals", value=v)
            for a, v in comparisons
        ],
    )


LICENSE_EXPR = vim.alarm.OrAlarmExpression(
    expression=[_event("com.vmware.license.VcLicenseExpiredEvent")]
)
MEMORY_EXPR = vim.alarm.OrAlarmExpression(
    expression=[
        _event(
            "vim.event.ResourceExhaustionStatusChangedEvent",
            status=colour,
            comparisons=[
                ("resourceName", "mem_usage"),
                ("newStatus", colour),
                ("_sourcehost_", "192.168.60.16"),
            ],
        )
        for colour in ("green", "yellow", "red")
    ]
)
ROOT_PW_EXPR = vim.alarm.OrAlarmExpression(
    expression=[
        _event(
            "com.vmware.vc.system.RootPasswordExpiredEvent",
            comparisons=[("componentName", "Root Password"), ("userName", "root")],
        )
    ]
)
TPM_EXPR = vim.alarm.OrAlarmExpression(
    expression=[_event("com.vmware.vc.host.TPMAttestationFailedEvent")]
)


def _assignment(uuid, name, expires, edition="vc.standard.instance", kind_scope=None):
    props = [SimpleNamespace(key="expirationDate", value=expires)] if expires else []
    lic = SimpleNamespace(name=name, editionKey=edition, properties=props)
    return SimpleNamespace(
        entityId=uuid, scope=kind_scope, entityDisplayName="192.168.60.16", assignedLicense=lic
    )


def _evidence(rows, uuid=VC_UUID, why=None):
    from vmware_monitor.ops.infra_health import _expiry

    out = []
    for a in rows or []:
        props = {p.key: p.value for p in a.assignedLicense.properties}
        expiration, expired = _expiry(props)
        out.append(
            {
                "asset": "192.168.60.16",
                "asset_id": a.entityId,
                "kind": "vcenter",
                "license_name": a.assignedLicense.name,
                "edition_key": a.assignedLicense.editionKey,
                "expiration": expiration,
                "expired": expired,
            }
        )
    return ac.LicenseEvidence(
        vcenter_uuid=uuid, assignments=out if rows is not None else None, why=why
    )


# ─── pure verdicts ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_valid_vcenter_license_clears_the_expired_license_alarm():
    ev = _evidence([_assignment(VC_UUID, "vCenter Server 8 Standard", NOW + timedelta(days=286))])
    verdict = ac.evaluate(LICENSE_EXPR, "red", ROOT, {}, licenses=ev)
    assert verdict.state == ac.CLEARED
    assert "vCenter Server 8 Standard" in verdict.note


@pytest.mark.unit
def test_an_expired_vcenter_license_holds():
    ev = _evidence([_assignment(VC_UUID, "vCenter Server 8 Standard", NOW - timedelta(days=1))])
    assert ac.evaluate(LICENSE_EXPR, "red", ROOT, {}, licenses=ev).state == ac.HOLDS


@pytest.mark.unit
@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param(None, id="no evidence passed"),
        pytest.param(_evidence(None, why="needs System.View"), id="assignments unreadable"),
        pytest.param(
            _evidence([_assignment("another-vcenter", "vCenter Server 8 Standard",
                                   NOW + timedelta(days=286))]),
            id="only another vCenter's assignment",
        ),
        pytest.param(
            _evidence([_assignment(VC_UUID, "vCenter Server 8 Standard",
                                   NOW + timedelta(days=286))], uuid=None),
            id="this vCenter's uuid unreadable",
        ),
        pytest.param(
            _evidence([_assignment(VC_UUID, "Product Evaluation", None, edition="eval")]),
            id="evaluation mode with no expiry property",
        ),
    ],
)
def test_without_positive_evidence_the_license_alarm_stays_unknown(evidence):
    assert ac.evaluate(LICENSE_EXPR, "red", ROOT, {}, licenses=evidence).state == ac.UNKNOWN


@pytest.mark.unit
def test_appliance_alarms_are_recognised_from_their_definition_not_their_name():
    assert ac.appliance_label(MEMORY_EXPR) == "vCenter appliance 192.168.60.16"
    assert ac.appliance_label(ROOT_PW_EXPR) == "vCenter appliance"
    assert ac.appliance_label(TPM_EXPR) is None
    assert ac.appliance_label(LICENSE_EXPR) is None
    assert ac.appliance_label(None) is None


# ─── get_active_alarms ────────────────────────────────────────────────────────


def _state(alarm, entity, status="red", acked_days=None):
    return SimpleNamespace(
        alarm=alarm,
        entity=entity,
        overallStatus=status,
        time=str(NOW - timedelta(days=120)),
        acknowledged=acked_days is not None,
        acknowledgedByUser="VSPHERE.LOCAL\\Administrator" if acked_days is not None else None,
        acknowledgedTime=NOW - timedelta(days=acked_days) if acked_days is not None else None,
    )


_ALARMS = {
    vim.alarm.Alarm("alarm-lic"): ("Expired vCenter Server license", LICENSE_EXPR),
    vim.alarm.Alarm("alarm-mem"): ("Memory Exhaustion on 192", MEMORY_EXPR),
    vim.alarm.Alarm("alarm-pw"): ("Root user password expired.", ROOT_PW_EXPR),
    vim.alarm.Alarm("alarm-tpm"): ("Host TPM attestation alarm", TPM_EXPR),
}
_REF = {name: ref for ref, (name, _e) in _ALARMS.items()}


def _content(root_states, license_calls):
    def query(entityId=None):  # noqa: N803 — pyVmomi keyword
        license_calls.append(1)
        return [_assignment(VC_UUID, "vCenter Server 8 Standard", NOW + timedelta(days=286))]

    lam = SimpleNamespace(QueryAssignedLicenses=query)
    return SimpleNamespace(
        rootFolder=SimpleNamespace(name="Datacenters", triggeredAlarmState=root_states),
        about=SimpleNamespace(apiType="VirtualCenter", instanceUuid=VC_UUID),
        licenseManager=SimpleNamespace(licenseAssignmentManager=lam, licenses=[]),
    )


def _fake_alarm_objects(si, objs, obj_type, paths):
    if obj_type is vim.alarm.Alarm:
        return [
            (ref, {"info.name": _ALARMS[ref][0], "info.expression": _ALARMS[ref][1]})
            for ref in set(objs)
        ]
    return []


def _lab_root_states():
    return [
        _state(_REF["Root user password expired."], ROOT, acked_days=103),
        _state(_REF["Expired vCenter Server license"], ROOT, acked_days=103),
        _state(_REF["Memory Exhaustion on 192"], ROOT, status="yellow"),
    ]


@pytest.mark.unit
def test_get_alarms_clears_the_license_alarm_and_labels_the_appliance(monkeypatch):
    calls: list = []
    content = _content(_lab_root_states(), calls)
    monkeypatch.setattr(health, "_collect", lambda si, t, p: [])
    monkeypatch.setattr(health, "_collect_objects", _fake_alarm_objects)
    out = health.get_active_alarms(SimpleNamespace(RetrieveContent=lambda: content))
    rows = {r["alarm_name"]: r for r in out["items"]}
    lic = rows["Expired vCenter Server license"]
    assert lic["condition_now"] == "cleared", lic
    assert "2027" in lic["condition_note"] or "valid" in lic["condition_note"]
    mem = rows["Memory Exhaustion on 192"]
    assert mem["object_label"] == "vCenter appliance 192.168.60.16"
    assert mem["entity_name"] != "vCenter appliance 192.168.60.16", "aiops resolves entity_name"
    assert rows["Root user password expired."]["condition_now"] == "unknown"
    assert calls == [1], "license assignments must be read exactly once"


@pytest.mark.unit
def test_no_license_read_without_a_license_alarm(monkeypatch):
    calls: list = []
    content = _content([_state(_REF["Memory Exhaustion on 192"], ROOT, status="yellow")], calls)
    monkeypatch.setattr(health, "_collect", lambda si, t, p: [])
    monkeypatch.setattr(health, "_collect_objects", _fake_alarm_objects)
    health.get_active_alarms(SimpleNamespace(RetrieveContent=lambda: content))
    assert calls == []


@pytest.mark.unit
def test_cli_health_alarms_names_the_appliance(monkeypatch):
    from vmware_monitor import cli

    calls: list = []
    content = _content(_lab_root_states(), calls)
    monkeypatch.setattr(health, "_collect", lambda si, t, p: [])
    monkeypatch.setattr(health, "_collect_objects", _fake_alarm_objects)
    si = SimpleNamespace(RetrieveContent=lambda: content)
    monkeypatch.setattr(cli, "_get_connection", lambda target, config: (si, None, "home-vcenter"))
    monkeypatch.setattr(cli, "console", Console(width=220, color_system=None))
    result = CliRunner().invoke(cli.app, ["health", "alarms"])
    assert result.exit_code == 0, result.output
    assert "vCenter appliance 192.168.60.16" in result.output


# ─── summary + attention ranking ──────────────────────────────────────────────


class _SummarySI:
    def __init__(self, content):
        self.content = content

    def RetrieveContent(self):  # noqa: N802 — pyVmomi contract
        return self.content


def _summary(monkeypatch):
    calls: list = []
    content = _content(_lab_root_states(), calls)
    tpm_live = _state(_REF["Host TPM attestation alarm"], HOST)

    def fake_collect(si, obj_type, paths):
        if obj_type[0] is vim.HostSystem:
            return [
                (
                    HOST,
                    {
                        "name": "192.168.60.56",
                        "runtime.connectionState": "connected",
                        "summary.quickStats.overallCpuUsage": 100,
                        "summary.quickStats.overallMemoryUsage": 1024,
                        "summary.hardware.cpuMhz": 2800,
                        "summary.hardware.numCpuCores": 4,
                        "summary.hardware.memorySize": 32 * 1024**3,
                        "triggeredAlarmState": [tpm_live],
                    },
                )
            ]
        return []

    monkeypatch.setattr(cluster_summary, "_collect", fake_collect)
    monkeypatch.setattr(cluster_summary, "_collect_objects", _fake_alarm_objects)
    return cluster_summary.get_cluster_health_summary(_SummarySI(content), top_n=10)


@pytest.mark.unit
def test_the_summary_ranks_only_cleared_alarms_after_live_ones(monkeypatch):
    """Review 2026-09-15 (H1): the first version also demoted an ``unknown`` alarm
    acknowledged a week ago. Unknown is not a verdict — an acknowledgement is not
    evidence the condition went away — so it keeps its severity rank."""
    issues = [i for i in _summary(monkeypatch)["top_issues"] if i["kind"] == "alarm"]
    order = [i["detail"].split(" — ")[0] for i in issues]
    # Critical (unknown, acknowledged 103 days ago) still outranks the warning.
    assert order.index("Root user password expired.") < order.index("Memory Exhaustion on 192")
    assert order[-1] == "Expired vCenter Server license", order  # cleared goes last
    by_name = {i["detail"].split(" — ")[0]: i for i in issues}
    assert by_name["Expired vCenter Server license"]["condition_now"] == "cleared"
    assert by_name["Root user password expired."]["acknowledged_days"] == 103
    assert "acknowledged 103 days ago" in by_name["Root user password expired."]["detail"]
    assert by_name["Memory Exhaustion on 192"]["object"] == "vCenter appliance 192.168.60.16"


@pytest.mark.unit
def test_attention_keeps_that_order_when_it_re_ranks_across_targets(monkeypatch):
    summary = _summary(monkeypatch)
    si = object()
    monkeypatch.setattr(
        attention, "get_cluster_health_summary", lambda s, cluster_filter=None, top_n=10: summary
    )
    monkeypatch.setattr(
        attention, "target_identity", lambda s: {"endpoint": "vcenter", "hosts": None,
                                                 "datastore_urls": None}
    )
    data = attention.get_cross_vcenter_attention([("home-vcenter", si)], top_n=10)
    alarms = [i["detail"].split(" — ")[0] for i in data["top_issues"] if i["kind"] == "alarm"]
    assert alarms.index("Root user password expired.") < alarms.index("Memory Exhaustion on 192")
    assert alarms[-1] == "Expired vCenter Server license"


def _issue(sev, kind, obj, detail, **extra):
    return {"severity": sev, "kind": kind, "object": obj, "scope": "datastore",
            "cluster": "c", "detail": detail, "drilldown": "hint", **extra}


@pytest.mark.unit
def test_an_old_acknowledged_unknown_critical_survives_the_top_n_cap():
    """The live shape of H1: ten warnings and one CRITICAL whose condition cannot
    be re-checked (acknowledged 196 days ago). With top_n=10 the critical was
    ranked eleventh and dropped out of ``top_issues``."""
    warnings = [_issue("warning", "capacity", f"ds{n}", f"thin-provisioned to {150 + n}%")
                for n in range(10)]
    tpm = _issue("critical", "alarm", "192.168.60.15", "Host TPM attestation alarm",
                 condition_now="unknown", acknowledged_days=196)
    cleared = _issue("critical", "alarm", "Datacenters", "Expired vCenter Server license",
                     condition_now="cleared", acknowledged_days=103)
    top, total = cluster_summary._rank_issues([*warnings, cleared, tpm], 10)
    assert total == 12
    assert top[0]["detail"] == "Host TPM attestation alarm", [i["detail"] for i in top]
    assert all(i["detail"] != "Expired vCenter Server license" for i in top), (
        "a cleared alarm (positive evidence) is the only kind that may fall below live issues"
    )


@pytest.mark.unit
def test_tool_text_never_calls_an_unknown_alarm_not_a_live_problem():
    from vmware_monitor.mcp_server import server

    doc = " ".join((server.cluster_health_summary.__doc__ or "").split())
    assert "not a live problem" not in doc
    assert "not re-checked" in doc
