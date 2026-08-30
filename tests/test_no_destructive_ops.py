"""Naming-convention guard: no ops/ function is *named* after a destructive verb.

This is a hygiene check on our own function names, not an enforcement of the
read-only claim -- a function called ``get_status`` could still call
``Destroy_Task``, and this test would not notice. The actual gate on what
vSphere methods this package calls lives in
``tests/eval/regression/test_read_only_enforcement.py``.

Kept because a destructive-sounding name in a read-only skill is a design
smell worth catching early, and because it is cheap.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

OPS_DIR = Path(__file__).resolve().parent.parent / "vmware_monitor" / "ops"

# Patterns that indicate a destructive function definition.
_DESTRUCTIVE_RE = re.compile(
    r"(delete|power_off|remove|disable|reset|force|shutdown|revert|destroy|unregister)",
    re.IGNORECASE,
)


def _collect_function_names(directory: Path) -> list[tuple[str, str]]:
    """Return [(file_name, func_name), ...] for every def in *directory*."""
    results: list[tuple[str, str]] = []
    for py_file in sorted(directory.rglob("*.py")):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                results.append((py_file.name, node.name))
    return results


@pytest.mark.unit
class TestNoDestructiveOps:
    """VMware-Monitor must remain strictly non-destructive."""

    def test_no_destructive_function_names(self) -> None:
        """No function name in ops/ should match a destructive pattern."""
        assert OPS_DIR.exists(), f"{OPS_DIR} not found"

        violations: list[str] = []
        for file_name, func_name in _collect_function_names(OPS_DIR):
            if _DESTRUCTIVE_RE.search(func_name):
                violations.append(f"{file_name}: {func_name}")

        assert not violations, (
            "Destructive function(s) found in read-only monitor skill:\n"
            + "\n".join(violations)
        )

    def test_ops_dir_exists(self) -> None:
        """Sanity check: ops/ directory must exist."""
        assert OPS_DIR.is_dir(), f"{OPS_DIR} is not a directory"
