"""The first useful result points to the public setup-feedback form.

Downloads cannot show whether anyone reached a useful result, so the CLI names
the form at that moment. It prints a link only: nothing is sent, and a failed
check or an all-unreachable attention run does not ask for a success report.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from vmware_monitor import _feedback, cli_observability
from vmware_monitor import doctor as doc
from vmware_monitor.ops import attention

URL = "https://github.com/vmware-skills/marketplace/issues/new?template=setup-feedback.yml"


def _recording(monkeypatch, module) -> Console:
    rec = Console(width=200, record=True, force_terminal=False, color_system=None)
    monkeypatch.setattr(module, "console", rec)
    return rec


def _checks(monkeypatch, ok: bool) -> None:
    monkeypatch.setattr(doc, "_CHECKS", [("Config file", lambda: (ok, "stub"))])


@pytest.mark.unit
def test_the_form_url_is_the_central_setup_feedback_template():
    assert _feedback.SETUP_FEEDBACK_URL == URL


@pytest.mark.unit
def test_a_passing_doctor_names_the_next_step_and_the_form(monkeypatch):
    _checks(monkeypatch, ok=True)
    rec = _recording(monkeypatch, doc)
    assert doc.run_doctor() == 0
    text = rec.export_text()
    assert "vmware-monitor attention" in text
    assert URL in text
    assert "nothing is sent" in text


@pytest.mark.unit
def test_a_failing_doctor_does_not_ask_for_a_result(monkeypatch):
    _checks(monkeypatch, ok=False)
    rec = _recording(monkeypatch, doc)
    assert doc.run_doctor() == 1
    assert URL not in rec.export_text()


def _attention(monkeypatch, data: dict) -> Console:
    monkeypatch.setattr(cli_observability, "get_all_connections", lambda config: ([], []))
    monkeypatch.setattr(attention, "get_cross_vcenter_attention", lambda *a, **k: data)
    monkeypatch.setattr(cli_observability, "render_attention_console", lambda d: None)
    return _recording(monkeypatch, cli_observability)


@pytest.mark.unit
def test_attention_with_a_reached_target_points_to_the_form(monkeypatch):
    rec = _attention(monkeypatch, {"targets": [{"name": "vc1"}], "unreachable": []})
    cli_observability.attention_cmd(cluster=None, top=10, html=False, html_path=None, config=None)
    assert URL in rec.export_text()


@pytest.mark.unit
def test_attention_with_every_target_unreachable_does_not(monkeypatch):
    rec = _attention(monkeypatch, {"targets": [], "unreachable": [{"name": "vc1"}]})
    cli_observability.attention_cmd(cluster=None, top=10, html=False, html_path=None, config=None)
    assert URL not in rec.export_text()


@pytest.mark.unit
def test_attention_html_snapshot_output_stays_unchanged(monkeypatch):
    rec = _attention(monkeypatch, {"targets": [{"name": "vc1"}], "unreachable": []})
    monkeypatch.setattr(cli_observability, "write_attention_html_snapshot", lambda d, p: None)
    cli_observability.attention_cmd(cluster=None, top=10, html=True, html_path=None, config=None)
    assert URL not in rec.export_text()
