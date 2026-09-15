"""The model must be able to tell which target answered, and choose one on purpose.

2026-09-15 conversation tests: the lab config listed a standalone ESXi host
first, so every tool called without ``target`` answered from that host. The
model said "vCenter has 9 VMs" (vCenter has 11) and "no vCenter target is
configured", because nothing told it which targets exist and no result said
where it came from.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from vmware_monitor.config import AppConfig, ConfigError, TargetConfig, load_config
from vmware_monitor.mcp_server import server

ESXI = TargetConfig(name="home-esxi", host="192.0.2.15", config_username="root", type="esxi")
VC = TargetConfig(name="home-vcenter", host="192.0.2.16", config_username="admin", type="vcenter")


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


TWO_TARGETS = """targets:
  - name: home-esxi
    host: 192.0.2.15
    type: esxi
  - name: home-vcenter
    host: 192.0.2.16
    type: vcenter
"""


class TestDefaultTargetKey:
    def test_without_the_key_the_first_target_is_the_default(self, tmp_path):
        assert load_config(_write(tmp_path, TWO_TARGETS)).default_target.name == "home-esxi"

    def test_the_key_chooses_the_default(self, tmp_path):
        cfg = load_config(_write(tmp_path, TWO_TARGETS + "default_target: home-vcenter\n"))
        assert cfg.default_target.name == "home-vcenter"

    def test_an_unknown_name_is_refused_not_ignored(self, tmp_path):
        """Falling back to the first target is how the wrong host answered."""
        with pytest.raises(ConfigError, match="home-vcentre"):
            load_config(_write(tmp_path, TWO_TARGETS + "default_target: home-vcentre\n"))


class TestInstructionsNameTheTargets:
    def test_targets_types_default_and_the_ask_rule_are_listed(self, monkeypatch):
        cfg = AppConfig(targets=(ESXI, VC), default_target_name="home-vcenter")
        monkeypatch.setattr(server, "load_config", lambda: cfg)
        text = server._target_instructions()
        assert "home-esxi (esxi, 192.0.2.15)" in text
        assert "home-vcenter (vcenter, 192.0.2.16, default)" in text
        assert "ask the user" in text

    def test_a_broken_config_still_yields_instructions(self, monkeypatch):
        def boom():
            raise FileNotFoundError("no config")

        monkeypatch.setattr(server, "load_config", boom)
        text = server._target_instructions()
        assert "Choosing a target" in text


class TestResultsNameTheirTarget:
    @pytest.fixture
    def config(self, monkeypatch):
        cfg = AppConfig(targets=(ESXI, VC), default_target_name="home-vcenter")

        class _Mgr:
            _config = cfg

        monkeypatch.setattr(server, "_ensure_conn_mgr", lambda: _Mgr())
        return cfg

    def test_named_target_is_reported(self, config):
        tool = server._with_target(lambda target=None: {"items": []})
        assert tool(target="home-esxi")["target"] == {"name": "home-esxi", "type": "esxi"}

    def test_omitted_target_reports_the_default(self, config):
        tool = server._with_target(lambda target=None: {"items": []})
        assert tool()["target"] == {"name": "home-vcenter", "type": "vcenter"}

    def test_tools_without_a_target_parameter_are_untouched(self, config):
        fn = lambda limit=None: {"items": []}  # noqa: E731
        assert server._with_target(fn) is fn

    def test_every_registered_target_tool_names_its_target(self):
        tools = server.mcp._tool_manager._tools
        taking_target = [
            name for name, t in tools.items() if "target" in inspect.signature(t.fn).parameters
        ]
        assert taking_target, "no registered tool takes target — the check would be vacuous"
        unwrapped = [
            name for name in taking_target if not getattr(tools[name].fn, "_names_target", False)
        ]
        assert unwrapped == []
