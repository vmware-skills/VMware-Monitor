"""APScheduler-based daemon for periodic scanning."""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from vmware_monitor.config import AppConfig, load_config
from vmware_monitor.connection import ConnectionManager
from vmware_monitor.notify.logger import ScanLogger
from vmware_monitor.notify.webhook import WebhookNotifier
from vmware_monitor.scanner.alarm_scanner import scan_alarms
from vmware_monitor.scanner.log_scanner import scan_host_logs, scan_logs

logger = logging.getLogger("vmware-monitor.scheduler")

PID_FILE = Path.home() / ".vmware-monitor" / "daemon.pid"


def _run_scan(config: AppConfig, conn_mgr: ConnectionManager) -> None:
    """Execute a single scan cycle across all targets."""
    scan_logger = ScanLogger(config.notify.log_file)
    webhook = WebhookNotifier(
        url=config.notify.webhook_url,
        timeout=config.notify.webhook_timeout,
    )

    all_issues: list[dict] = []

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
            continue

        # Scan alarms
        try:
            all_issues.extend(scan_alarms(si))
        except Exception as e:
            logger.error("Alarm scan failed for %s: %s", target_name, e)

        # Scan events/logs
        try:
            all_issues.extend(scan_logs(si, config.scanner))
        except Exception as e:
            logger.error("Log scan failed for %s: %s", target_name, e)

        # Scan host-level logs
        try:
            all_issues.extend(scan_host_logs(si)["items"])
        except Exception as e:
            logger.error("Host log scan failed for %s: %s", target_name, e)

    # Log all issues — one bad write must not discard the rest of the cycle
    for issue in all_issues:
        try:
            scan_logger.log_issue(issue)
        except Exception as e:
            logger.error("Failed to log issue %r: %s", issue.get("message", "?"), e)

    # Send webhook if there are critical/warning issues
    important = [i for i in all_issues if i["severity"] in ("critical", "warning")]
    if important and config.notify.webhook_url:
        webhook.send(important)

    if all_issues:
        logger.info("Scan complete: %d issue(s) found", len(all_issues))
    else:
        logger.info("Scan complete: all clear")


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

    scheduler = BlockingScheduler()
    scheduler.add_job(
        _run_scan,
        trigger=IntervalTrigger(minutes=config.scanner.interval_minutes),
        args=[config, conn_mgr],
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
        _run_scan(config, conn_mgr)
        scheduler.start()
    finally:
        PID_FILE.unlink(missing_ok=True)
        conn_mgr.disconnect_all()
