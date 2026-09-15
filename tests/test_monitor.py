"""Monitor and selftest - env-tolerant offline checks (psutil/pytesseract may
not exist in the test venv; the modules must still behave honestly either way).
"""
import pytest

from ra import selftest, monitor


def _have_psutil():
    try:
        import psutil  # noqa: F401
        return True
    except ImportError:
        return False


def test_selftest_runs():
    out = selftest.run()
    assert "Ra capability self-test:" in out
    assert "check:" in out  # reports N/M capabilities green
    assert (out.count("fail") == 0) == ("fail" not in out)


def test_health_read_shape():
    h = monitor.health()
    # psutil may be missing -> every probe must be None, never raise.
    assert "cpu" in h and "ram" in h and "disk" in h and "warn" in h
    if _have_psutil():
        assert isinstance(h["cpu"], float)
        assert isinstance(h["ram"], float)


def test_report_contract():
    r = monitor.report()
    if _have_psutil():
        assert "System health:" in r
        assert "CPU" in r
    else:
        assert "couldn't read telemetry" in r


def test_briefing_runs():
    b = monitor.morning_briefing()
    assert "It's" in b  # contains time
    assert "battery" in b.lower() or "Battery" in b


def test_monitor_lifecycle():
    msg = monitor.monitor(interval=9999, cpu_warn=200, ram_warn=200,
                          disk_warn=200, batt_low=0)
    assert "Monitoring started" in msg
    assert "System monitoring stopped." == monitor.stop_monitor()