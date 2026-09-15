"""Scheduled tasks: cronai/KAIROS-style cadence + due logic + store."""
import threading
import time

import pytest

from ra import scheduler


@pytest.fixture(autouse=True)
def _stop_daemon():
    """Stop the background daemon after every test so a leftover thread can't
    race the next test's temp file writes."""
    yield
    scheduler.stop()
    scheduler._thread = None
    scheduler._stop_event = threading.Event()


def test_cadence_parsing():
    assert scheduler._to_minutes({"cadence": "every hour"}) == 60
    assert scheduler._to_minutes({"cadence": "hourly"}) == 60
    assert scheduler._to_minutes({"cadence": "every minute"}) == 1
    assert scheduler._to_minutes({"cadence": "daily"}) == 1440
    assert scheduler._to_minutes({"cadence": "every 30 minutes"}) == 30
    assert scheduler._to_minutes({"cadence": "every 2 hours"}) == 120
    assert scheduler._to_minutes({"cadence": "every minute", "every_minutes": 15}) == 1
    assert scheduler._to_minutes({"cadence": "daily at 09:00"}) is None  # wall-clock path


def test_add_and_list(tmp_path, monkeypatch):
    monkeypatch.setattr("ra.config.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(scheduler, "_SCHED_FILE", str(tmp_path / "sched.json"))
    msg = scheduler.add_schedule("health", "check system health", "every hour")
    assert "Scheduled" in msg
    out = scheduler.list_schedules()
    assert "health" in out
    assert "check system health" in out


def test_add_duplicate_name_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_SCHED_FILE", str(tmp_path / "sched.json"))
    scheduler.add_schedule("dup", "task one")
    msg = scheduler.add_schedule("dup", "task two")
    assert "already exists" in msg.lower()


def test_remove(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_SCHED_FILE", str(tmp_path / "sched.json"))
    scheduler.add_schedule("gone", "bye")
    assert "Removed" in scheduler.remove_schedule("gone")
    assert "No schedule" in scheduler.remove_schedule("gone")


def test_invalid_cadence_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_SCHED_FILE", str(tmp_path / "sched.json"))
    msg = scheduler.add_schedule("x", "do stuff", "twice a lunar month")
    assert "cadence" in msg.lower()


def test_due_now_every_hour():
    spec = {"cadence": "every hour", "last_run": 0}
    assert scheduler._due_now(spec, time.time()) is True      # never run -> due
    spec["last_run"] = time.time() - 61 * 60
    assert scheduler._due_now(spec, time.time()) is True      # overdue
    spec["last_run"] = time.time() - 10
    assert scheduler._due_now(spec, time.time()) is False     # ran recently


def test_due_now_daily_wallclock():
    lt = time.localtime()
    at = f"{lt.tm_hour:02d}:{max(0, lt.tm_min - 1):02d}"   # 1 min ago
    spec = {"cadence": "daily", "at": at, "weekdays": "", "last_run": 0}
    assert scheduler._due_now(spec, time.time()) is True
    spec["last_run"] = time.time() - 30
    assert scheduler._due_now(spec, time.time()) is False   # ran today already


def test_due_now_weekdays_skips_weekend():
    lt = time.localtime()
    at = f"{lt.tm_hour:02d}:{max(0, lt.tm_min - 1):02d}"
    spec = {"cadence": "daily", "at": at, "weekdays": "mon-fri", "last_run": 0}
    if lt.tm_wday < 5:
        assert scheduler._due_now(spec, time.time()) is True
    else:
        assert scheduler._due_now(spec, time.time()) is False


def test_persist_after_add(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_SCHED_FILE", str(tmp_path / "sched.json"))
    scheduler.add_schedule("keep", "persist me", "every 5 minutes")
    items = scheduler._read()
    assert len(items) == 1
    assert items[0]["name"] == "keep"


def test_pause_and_run_now(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_SCHED_FILE", str(tmp_path / "sched.json"))
    scheduler.add_schedule("pause_me", "nothing", "every hour")
    out = scheduler.pause_schedule("pause_me", enabled=False)
    assert "paused" in out
    assert "No schedule" in scheduler.pause_schedule("nope")


def test_list_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_SCHED_FILE", str(tmp_path / "sched.json"))
    assert scheduler.list_schedules() == "No scheduled tasks."