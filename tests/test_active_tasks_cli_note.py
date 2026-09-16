"""`activity tasks` must mention unreadable tasks even when some tasks could be read.

The 1.15.0 CLI printed ``unreadable_note`` only when no row came back. With 3
readable tasks and 11 unreadable ones it printed a normal table of 3, which reads
as "these are all the tasks" (independent review, 2026-09-15). The MCP result
was right; the terminal said the opposite of it.
"""

from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

NOTE = "11 task(s) could not be read"


def _row(name: str) -> dict:
    return {
        "name": name,
        "entity": "vm-1",
        "state": "running",
        "progress_pct": 40,
        "user": "root",
        "active": True,
        "error": None,
    }


def _run(monkeypatch, result: dict) -> str:
    import vmware_monitor.cli_observability as cli_obs
    import vmware_monitor.ops.activity as activity
    from vmware_monitor.cli import app

    monkeypatch.setattr(
        cli_obs, "get_connection", lambda target, config: (SimpleNamespace(), None, "lab")
    )
    monkeypatch.setattr(activity, "get_active_tasks", lambda si, **kw: result)
    out = CliRunner().invoke(app, ["activity", "tasks"], terminal_width=200)
    assert out.exit_code == 0, out.output
    return out.output


def test_the_note_is_printed_beside_readable_tasks(monkeypatch):
    output = _run(
        monkeypatch,
        {"items": [_row("desc.t1")], "total": 1, "unreadable_tasks": 11, "unreadable_note": NOTE},
    )
    assert "desc.t1" in output
    assert NOTE in output


def test_the_note_is_printed_when_nothing_was_readable(monkeypatch):
    output = _run(
        monkeypatch, {"items": [], "total": 0, "unreadable_tasks": 11, "unreadable_note": NOTE}
    )
    assert NOTE in output
    assert "No tasks." not in output


def test_no_note_when_every_task_reads(monkeypatch):
    output = _run(monkeypatch, {"items": [_row("desc.t1")], "total": 1})
    assert "could not be read" not in output
