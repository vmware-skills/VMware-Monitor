"""Reading an old log today must not date the original event as today."""

from vmware_monitor.scanner.log_scanner import _findings


def test_host_log_finding_uses_log_time_and_separate_observation_time():
    stamp = "2026-01-01T12:00:00.123Z"
    row = _findings((f"{stamp} Er(163) hostd error while reading",), "host", "hostd")[0]
    assert row["time"] == stamp and row["log_time"] == stamp
    assert row["observed_at"] != stamp


def test_missing_log_timestamp_stays_unknown():
    row = _findings(("Er(163) hostd error without a timestamp",), "host", "hostd")[0]
    assert row["time"] is None and row["log_time"] is None
    assert row["observed_at"]
