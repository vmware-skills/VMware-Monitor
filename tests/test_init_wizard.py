"""Regression tests for the `vmware-monitor init` setup wizard.

Locks the first-run contract: writes config.yaml + .env, stores the password
grep-safe (b64:, never plaintext), sets 0600, and round-trips back to the
original password through the normal config loader.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vmware_monitor import init_wizard


@pytest.fixture
def _wizard_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the wizard's config paths at a temp dir."""
    cfg_dir = tmp_path / ".vmware-monitor"
    monkeypatch.setattr(init_wizard, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(init_wizard, "CONFIG_FILE", cfg_dir / "config.yaml")
    monkeypatch.setattr(init_wizard, "ENV_FILE", cfg_dir / ".env")
    return cfg_dir


def _feed(monkeypatch: pytest.MonkeyPatch, answers: list[object], confirms: list[bool]) -> None:
    """Drive typer.prompt / typer.confirm deterministically."""
    a = iter(answers)
    c = iter(confirms)
    monkeypatch.setattr(init_wizard.typer, "prompt", lambda *args, **kwargs: next(a))
    monkeypatch.setattr(init_wizard.typer, "confirm", lambda *args, **kwargs: next(c))


def test_init_writes_grep_safe_env(_wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # prompts: name, host, type, username, port, password
    _feed(
        monkeypatch,
        answers=["lab-vc", "10.1.2.3", "vcenter", "administrator@vsphere.local", 443, "S3cr3t!pw"],
        confirms=[True],  # verify_ssl
    )
    rc = init_wizard.run_init(skip_test=True)
    assert rc == 0

    env_text = (_wizard_env / ".env").read_text(encoding="utf-8")
    # Password is present under the right key, but NOT in plaintext.
    assert "VMWARE_LAB_VC_PASSWORD=b64:" in env_text
    assert "S3cr3t!pw" not in env_text
    # 0600 perms.
    mode = (_wizard_env / ".env").stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_init_password_round_trips(_wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vmware_monitor.config import _decode_secret

    _feed(
        monkeypatch,
        answers=["prod1", "vc.example.com", "vcenter", "svc@vsphere.local", 443, "p@ss w/ space"],
        confirms=[False],  # verify_ssl off
    )
    init_wizard.run_init(skip_test=True)

    # The wizard exported the live env var for an in-process connection test.
    assert os.environ["VMWARE_PROD1_PASSWORD"] == "p@ss w/ space"

    # And the on-disk b64 token decodes back to the original via the loader.
    line = next(
        ln
        for ln in (_wizard_env / ".env").read_text(encoding="utf-8").splitlines()
        if ln.startswith("VMWARE_PROD1_PASSWORD=")
    )
    stored = line.split("=", 1)[1]
    assert _decode_secret(stored) == "p@ss w/ space"


def test_init_declines_overwrite(_wizard_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _wizard_env.mkdir(parents=True)
    (_wizard_env / "config.yaml").write_text("targets: []\n", encoding="utf-8")
    # First confirm = "overwrite?" → False ⇒ abort, no prompts consumed.
    _feed(monkeypatch, answers=[], confirms=[False])
    rc = init_wizard.run_init(skip_test=True)
    assert rc == 0
    # Untouched.
    assert (_wizard_env / "config.yaml").read_text(encoding="utf-8") == "targets: []\n"
