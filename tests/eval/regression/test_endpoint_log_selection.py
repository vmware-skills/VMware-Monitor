"""Endpoint identity and selected diagnostic keys retain the evidence scope."""
from types import SimpleNamespace

import pytest
from pyVmomi import vim
from typer.testing import CliRunner

from tests.eval.regression._diag_fakes import LogStub
from tests.eval.regression.test_host_log_scan_reads_real_logs import _si
from vmware_monitor.ops import cluster_summary
from vmware_monitor.scanner import log_scanner


@pytest.mark.parametrize("api_type", ["VirtualCenter", "HostAgent"])
def test_summary_identifies_the_endpoint_not_an_inventory_host(monkeypatch, api_type):
    monkeypatch.setattr(cluster_summary, "_collect", lambda *a, **kw: [])
    about = vim.AboutInfo(apiType=api_type, version="8.0.3", build="24022510")
    si = SimpleNamespace(content=SimpleNamespace(
        about=about, rootFolder=SimpleNamespace(triggeredAlarmState=[])))
    out = cluster_summary.get_cluster_health_summary(si)
    assert out["endpoint"] == {"api_type": api_type, "version": "8.0.3", "build": "24022510"}


@pytest.mark.parametrize("about", [None, SimpleNamespace(version=42, build=True)])
def test_missing_or_malformed_endpoint_fields_are_unknown(monkeypatch, about):
    monkeypatch.setattr(cluster_summary, "_collect", lambda *a, **kw: [])
    si = SimpleNamespace(content=SimpleNamespace(
        about=about, rootFolder=SimpleNamespace(triggeredAlarmState=[])))
    assert cluster_summary.get_cluster_health_summary(si)["endpoint"] == {
        "api_type": None, "version": None, "build": None}


def test_endpoint_external_text_is_sanitized(monkeypatch):
    monkeypatch.setattr(cluster_summary, "_collect", lambda *a, **kw: [])
    si = SimpleNamespace(content=SimpleNamespace(
        about=SimpleNamespace(apiType="unknown", version="8\x1b[2J", build="a" * 900),
        rootFolder=SimpleNamespace(triggeredAlarmState=[])))
    out = cluster_summary.get_cluster_health_summary(si)["endpoint"]
    assert out["api_type"] == "unknown"
    assert "\x1b" not in out["version"]
    assert len(out["build"]) <= 510


def test_selected_keys_are_the_only_logs_read_and_gaps_remain_visible(monkeypatch):
    stub = LogStub({"vobd": ["error: selected log"]}, fail={"fdm": vim.fault.CannotAccessFile()})
    si = _si(monkeypatch, stub)
    out = log_scanner.scan_host_logs(si, log_keys=("vobd", "fdm"))
    assert {c["key"] for c in stub.calls} == {"vobd", "fdm"}
    assert out["log_keys"] == ["vobd", "fdm"]
    assert out["items"] and out["logs_unavailable"][0]["log"] == "fdm"


@pytest.mark.parametrize("keys", [[], ["hostd", "hostd"], ["../auth"], ["/var/log/auth.log"],
                                  ["x\nsecret"], ["x" * 65], [str(i) for i in range(17)],
                                  "hostd", [True], [""], ["é"], ["hostd", None]])
def test_invalid_keys_stop_before_any_remote_read(monkeypatch, keys):
    def forbidden(*a, **kw):
        pytest.fail("invalid log keys reached a connection")
    monkeypatch.setattr(log_scanner, "_scan_pass", forbidden)
    with pytest.raises(ValueError, match="log_keys"):
        log_scanner.scan_host_logs(object(), log_keys=keys)


def test_mcp_selection_reaches_diagnostics(monkeypatch):
    from vmware_monitor.mcp_server import server
    stub = LogStub({"vobd": ["error: selected"]})
    monkeypatch.setattr(server, "_get_connection", lambda target=None: _si(monkeypatch, stub))
    out = server.scan_host_logs(log_keys=["vobd"])
    assert out["log_keys"] == ["vobd"] and out["items"]
    assert {c["key"] for c in stub.calls} == {"vobd"}


def test_mcp_invalid_selection_does_not_connect_or_echo_input(monkeypatch):
    from vmware_monitor.mcp_server import server
    monkeypatch.setattr(server, "_get_connection", lambda *a, **kw: pytest.fail("connected"))
    out = server.scan_host_logs(log_keys=["../secret-token"])
    assert "log_keys" in out["error"] and "secret-token" not in out["error"]


def test_cli_repeatable_selection_reaches_diagnostics(monkeypatch):
    from vmware_monitor import cli
    stub = LogStub({"hostd": ["error: first"], "vobd": ["error: second"]})
    monkeypatch.setattr(cli, "_get_connection", lambda *a: (_si(monkeypatch, stub), None, "lab"))
    out = CliRunner().invoke(cli.app, ["scan", "logs", "--log-key", "hostd", "--log-key", "vobd"])
    assert out.exit_code == 0, out.output
    assert {c["key"] for c in stub.calls} == {"hostd", "vobd"}


def test_cli_invalid_selection_does_not_connect(monkeypatch):
    from vmware_monitor import cli
    monkeypatch.setattr(cli, "_get_connection", lambda *a: pytest.fail("connected"))
    out = CliRunner().invoke(cli.app, ["scan", "logs", "--log-key", "../secret-token"])
    assert out.exit_code != 0 and "log_keys" in out.output
    assert "secret-token" not in out.output


def test_unreadable_endpoint_does_not_discard_health_or_expose_exception(monkeypatch):
    monkeypatch.setattr(cluster_summary, "_collect", lambda *a, **kw: [])

    class Unreadable:
        @property
        def about(self):
            raise RuntimeError("secret-token")

        rootFolder = SimpleNamespace(triggeredAlarmState=[])  # noqa: N815 - SDK field

    out = cluster_summary.get_cluster_health_summary(SimpleNamespace(content=Unreadable()))
    assert out["totals"]["clusters"] == 0
    assert out["endpoint"]["build"] is None
    assert "RuntimeError" in out["endpoint"]["unavailable"]
    assert "secret-token" not in str(out)
