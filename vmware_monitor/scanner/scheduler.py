"""APScheduler-based daemon for periodic scanning."""

from __future__ import annotations

import logging
import os
import signal
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from vmware_monitor.config import AppConfig, load_config
from vmware_monitor.connection import ConnectionManager
from vmware_monitor.notify.logger import ScanLogger
from vmware_monitor.notify.webhook import WebhookNotifier
from vmware_monitor.scanner.alarm_scanner import scan_alarms
from vmware_monitor.scanner.log_scanner import scan_host_logs_since, scan_logs

logger = logging.getLogger("vmware-monitor.scheduler")

PID_FILE = Path.home() / ".vmware-monitor" / "daemon.pid"

_UNREADABLE_SOURCE = "host_log:unavailable"
_SKIPPED_SOURCE = "host_log:skipped"


class HostLogCursors:
    """Where the daemon last read each host log: (target, host, log) -> line.

    Process memory only: a restarted daemon reads the tail of every log once
    more, then continues from there. Without this every cycle re-read the last
    500 lines and reported the same lines again, every 15 minutes.
    """

    def __init__(self) -> None:
        self._by_target: dict[str, Mapping[tuple[str, str], int]] = {}

    def since(self, target: str) -> Mapping[tuple[str, str], int]:
        return dict(self._by_target.get(target, {}))

    def advance(self, target: str, positions: Mapping[tuple[str, str], int]) -> None:
        self._by_target = {**self._by_target, target: dict(positions)}


def _pages(issue: dict) -> bool:
    """Whether an issue goes to the webhook.

    Critical always does. A warning does unless it is a host-log line: a
    busy host writes routine lines that match "fail"/"timeout" by the
    hundred (live: 135 of 228 matches in one cycle were one hostd line), so
    host-log warnings go to the scan log only. Alarm and event warnings page
    exactly as before.
    """
    severity = issue.get("severity")
    if severity == "critical":
        return True
    if severity == "warning":
        return not str(issue.get("source", "")).startswith("host_log")
    return False


def _host_log_issues(si: object, target_name: str, cursors: HostLogCursors) -> list[dict]:
    """New host-log lines as issues, plus a row for every log not (fully) read.

    An unreadable log is recorded as ``info``: enough that the cycle cannot
    report "all clear", not enough to page the webhook every interval for a
    log that may never exist on a host. Skipped lines are ``info`` too.
    """
    result = scan_host_logs_since(si, cursors.since(target_name))
    cursors.advance(target_name, result.positions)
    now = datetime.now(tz=timezone.utc).isoformat()
    gaps = [
        _info(_UNREADABLE_SOURCE, g["host"], now,
              f"{g['host']}: {g['log']} log could not be read — {g['reason']}")
        for g in result.logs_unavailable
    ]
    skips = [
        _info(_SKIPPED_SOURCE, s["host"], now, _skip_message(s))
        for s in result.lines_skipped
    ]
    return [*result.items, *gaps, *skips]


def _skip_message(row: dict) -> str:
    if row["skipped"] is None:
        return (f"{row['host']}: {row['log']} log rotated since the last read — lines "
                "written between that read and the rotation were not scanned")
    return (f"{row['host']}: {row['log']} log grew by more than one read — "
            f"{row['skipped']} line(s) were not scanned; the newest were")


def _info(source: str, entity: str, now: str, message: str) -> dict:
    return {"severity": "info", "source": source, "message": message,
            "time": now, "entity": entity}


def _scan_target(
    si: object, target_name: str, config: AppConfig, cursors: HostLogCursors
) -> tuple[list[dict], list[str]]:
    """Run the three passes on one target: (issues, names of failed passes)."""
    passes = (
        ("Alarm scan", lambda: scan_alarms(si)),
        ("Log scan", lambda: scan_logs(si, config.scanner)),
        ("Host log scan", lambda: _host_log_issues(si, target_name, cursors)),
    )
    issues: list[dict] = []
    failed: list[str] = []
    for label, run in passes:
        try:
            issues.extend(run())
        except Exception as e:
            # With the traceback: a failed pass may be a bug in this code, and
            # "Scan complete" below will say the cycle was incomplete.
            logger.error("%s failed for %s: %s", label, target_name, e, exc_info=True)
            failed.append(f"{target_name}: {label.lower()}")
    return issues, failed


def _log_summary(all_issues: list[dict], paged: list[dict], failed: list[str]) -> None:
    """One line that never claims "all clear" about a cycle that did not run."""
    unreadable = sum(1 for i in all_issues if i.get("source") == _UNREADABLE_SOURCE)
    skipped = sum(1 for i in all_issues if i.get("source") == _SKIPPED_SOURCE)
    findings = len(all_issues) - unreadable - skipped
    if not all_issues and not failed:
        logger.info("Scan complete: all clear")
        return
    summary = (f"{findings} finding(s) ({len(paged)} sent to the webhook), "
               f"{unreadable} unreadable host log(s), {skipped} host log(s) with "
               f"unscanned lines, {len(failed)} failed pass(es)")
    if failed:
        logger.warning("Scan INCOMPLETE: %s: %s", summary, "; ".join(failed))
    else:
        logger.info("Scan complete: %s", summary)


def _run_scan(
    config: AppConfig,
    conn_mgr: ConnectionManager,
    cursors: HostLogCursors | None = None,
) -> None:
    """Execute a single scan cycle across all targets.

    ``cursors`` is the daemon's memory of how far each host log was read; the
    scheduler passes the same one to every cycle. Without it (a one-off cycle)
    every log's last lines are read.
    """
    cursors = cursors if cursors is not None else HostLogCursors()
    scan_logger = ScanLogger(config.notify.log_file)
    webhook = WebhookNotifier(
        url=config.notify.webhook_url,
        timeout=config.notify.webhook_timeout,
    )

    all_issues: list[dict] = []
    failed: list[str] = []

    for target_name in conn_mgr.list_targets():
        try:
            si = conn_mgr.connect(target_name)
        except Exception as e:
            issue = {
                "severity": "critical",
                "source": "connection",
                "message": f"Failed to connect to {target_name}: {e}",
                "time": "",
                "entity": target_name,
            }
            all_issues.append(issue)
            failed.append(f"{target_name}: connect")
            continue
        issues, target_failed = _scan_target(si, target_name, config, cursors)
        all_issues.extend(issues)
        failed.extend(target_failed)

    # Log all issues — one bad write must not discard the rest of the cycle
    for issue in all_issues:
        try:
            scan_logger.log_issue(issue)
        except Exception as e:
            logger.error("Failed to log issue %r: %s", issue.get("message", "?"), e)

    paged = [i for i in all_issues if _pages(i)]
    if paged and config.notify.webhook_url:
        webhook.send(paged)

    _log_summary(all_issues, paged, failed)


def start_scheduler(config_path: Path | None = None) -> None:
    """Start the blocking scheduler daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    config = load_config(config_path)
    conn_mgr = ConnectionManager(config)

    if not config.scanner.enabled:
        logger.warning("Scanner is disabled in config. Exiting.")
        return

    # One cursor store for the life of the process, shared by the first scan
    # and every scheduled one — that is what makes each host-log line appear
    # in one cycle rather than in every cycle it stays within the last 500.
    cursors = HostLogCursors()
    scheduler = BlockingScheduler()
    scheduler.add_job(
        _run_scan,
        trigger=IntervalTrigger(minutes=config.scanner.interval_minutes),
        args=[config, conn_mgr, cursors],
        id="vmware_scan",
        name="VMware Monitor Scanner",
        max_instances=1,
        next_run_time=None,  # Scheduler interval starts after manual first run below
    )

    def _shutdown(signum, frame):
        logger.info("Shutting down scanner...")
        scheduler.shutdown(wait=False)
        PID_FILE.unlink(missing_ok=True)
        conn_mgr.disconnect_all()
        sys.exit(0)

    # Register signal handlers BEFORE the PID file is written and the first
    # scan runs — otherwise a SIGTERM during the initial scan leaves a stale
    # PID file behind.
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Write PID file (owner-only dir; PID itself is not sensitive)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(PID_FILE.parent, 0o700)
    except OSError:
        pass
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    try:
        # Run first scan immediately, then scheduler takes over. Inside the
        # try so a crash during the first scan still removes the PID file.
        logger.info(
            "Scanner starting. Interval: %dm. Targets: %s",
            config.scanner.interval_minutes,
            ", ".join(conn_mgr.list_targets()),
        )
        _run_scan(config, conn_mgr, cursors)
        scheduler.start()
    finally:
        PID_FILE.unlink(missing_ok=True)
        conn_mgr.disconnect_all()
