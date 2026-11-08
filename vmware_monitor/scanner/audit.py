"""Policy and minimal audit records for scheduled reads and webhook sends."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from vmware_policy.guard import audit_call, guard
from vmware_policy.policy import PolicyDenied
from vmware_policy.sanitize import sanitize


@dataclass
class Outcome:
    status: str = "ok"
    result: dict = field(default_factory=dict)


class DaemonAudit:
    """Remember unchanged failures for this process; every success is recorded."""

    def __init__(self) -> None:
        self._failures: dict[tuple[str, str], str] = {}

    @contextmanager
    def operation(self, tool: str, target: str) -> Iterator[Outcome]:
        started = time.monotonic()
        outcome = Outcome()
        try:
            guard("monitor", tool, {"target": target}, target=target, risk_level="low")
            yield outcome
        except PolicyDenied as exc:
            outcome.status = "denied"
            outcome.result = {"rule": sanitize(exc.result.rule, 200)}
            raise
        except Exception as exc:
            # Exception text may contain URL credentials or raw API content.
            outcome.status, outcome.result = "error", {"error": type(exc).__name__}
            raise
        except BaseException as exc:
            outcome.status, outcome.result = "interrupted", {"error": type(exc).__name__}
            raise
        finally:
            key = (tool, target)
            signature = json.dumps([outcome.status, outcome.result], sort_keys=True)
            unchanged = outcome.status != "ok" and self._failures.get(key) == signature
            if outcome.status == "ok":
                self._failures.pop(key, None)
            else:
                self._failures[key] = signature
            if not unchanged:
                audit_call(
                    "monitor",
                    tool,
                    params={"target": target},
                    result=outcome.result,
                    status=outcome.status,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    risk_level="low",
                    surface="daemon",
                )
