"""
Ra Scheduled Tasks
====================
A lightweight cron for Ra, in the spirit of cronai / KAIROS / boo / cx.

Schedules live in `~/.ra/schedules.json`:
    [
      {
        "id": "...", "name": "weather hour", "enabled": true,
        "cadence": "every hour",           # or "hourly"
        "at": "09:00",                     # for daily / weekdays cadence
        "weekdays": "mon-fri",             # optional
        "every_minutes": 30,               # for "every N minutes"
        "task": "tell me the weather",     # free text Ra will execute
        "last_run": 0, "missed": 0
      }
    ]

A daemon thread checks every ~20s; when a schedule is due it executes the task
through brain.ask (so it can use all of Ra's tools), records last_run, and
catches up on missed runs (boo-style) so a sleeping PC still gets its reports.
"""
import json
import os
import threading
import time
import uuid

from ra import config
from ra import logging as ralog

_SCHED_FILE = os.path.join(config.DATA_DIR, "schedules.json")
_CHECK_INTERVAL = 20          # seconds between due-checks
_MAX_RETRO = 2                # catch up at most this many missed runs
_MAX_SCHEDULES = 50

_lock = threading.Lock()
_thread = None
_stop_event = threading.Event()
_runs = {}                    # id -> {"count", "last_error"}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _read() -> list:
    if not os.path.exists(_SCHED_FILE):
        return []
    try:
        with _lock, open(_SCHED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write(items: list):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with _lock, open(_SCHED_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# Due logic
# ---------------------------------------------------------------------------

def _to_minutes(spec: dict) -> int | None:
    """Return the cadence in minutes, or None when the spec is a wall-clock rule."""
    cad = (spec.get("cadence") or "").strip().lower()
    if cad in ("every minute", "minute", "1 minute", "every 1 minute", "minutely"):
        return 1
    if cad in ("every hour", "hourly", "hour", "every 1 hour"):
        return 60
    if cad in ("every day", "daily", "day", "once a day"):
        return 1440
    if cad in ("every 3 hours", "every three hours"):
        return 180
    if cad in ("every 6 hours",):
        return 360
    if cad in ("every 12 hours",):
        return 720
    em = spec.get("every_minutes")
    if em:
        try:
            return max(1, int(em))
        except (TypeError, ValueError):
            return None
    if cad.startswith("every") and cad.endswith("minutes"):
        try:
            n = int(cad.split()[1])
            return max(1, n)
        except (ValueError, IndexError):
            return None
    if cad.startswith("every") and cad.endswith("hours"):
        try:
            n = int(cad.split()[1])
            return n * 60
        except (ValueError, IndexError):
            return None
    return None


def _due_now(spec: dict, now: float) -> bool:
    """Pure-testable decision: is `spec` due at unix time `now`?"""
    last = float(spec.get("last_run") or 0)
    minutes = _to_minutes(spec)
    if minutes is not None:
        return last == 0 or (now - last) >= minutes * 60
    # Wall-clock cadences: daily / weekdays at HH:MM
    lt = time.localtime(now)
    at = (spec.get("at") or "09:00").strip()
    try:
        hh, mm = (int(x) for x in at.split(":")[:2])
    except (ValueError, AttributeError):
        hh, mm = 9, 0
    today_at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    if now < today_at:
        return False
    weekday_allowed = True
    wd = (spec.get("weekdays") or "").strip().lower()
    if wd in ("mon-fri", "weekdays"):
        weekday_allowed = lt.tm_wday < 5
    elif wd in ("weekend",):
        weekday_allowed = lt.tm_wday >= 5
    elif len(wd) == 3 and wd in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        _names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        weekday_allowed = wd == _names[lt.tm_wday]
    if not weekday_allowed:
        return False
    if last >= today_at:
        return False
    return (now - today_at) <= 24 * 3600


def _next_due(spec: dict, now: float) -> float | None:
    """Next due unix time (used for the status line)."""
    minutes = _to_minutes(spec)
    if minutes is not None:
        last = float(spec.get("last_run") or 0)
        return max(now, last + minutes * 60)
    lt = time.localtime(now)
    at = (spec.get("at") or "09:00").strip()
    try:
        hh, mm = (int(x) for x in at.split(":")[:2])
    except (ValueError, AttributeError):
        hh, mm = 9, 0
    candidate = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    if candidate <= now:
        candidate += 86400
    return candidate


# ---------------------------------------------------------------------------
# Public API / skills backend
# ---------------------------------------------------------------------------

def add_schedule(name: str, task: str, cadence: str = "every hour",
                 at: str = "", every_minutes: int | None = None,
                 weekdays: str = "") -> str:
    """Add a scheduled task. Returns a human confirmation (or an error)."""
    name = str(name or "").strip()[:80]
    task = str(task or "").strip()
    cadence = (cadence or "every hour").strip().lower()
    if not task:
        return "Schedule needs a task to perform."
    if cadence in ("daily", "every day", "everyday") and not at:
        at = "09:00"
    items = _read()
    if len(items) >= _MAX_SCHEDULES:
        return "Too many schedules - remove one first."
    for s in items:
        if (s.get("name") or "").lower() == name.lower():
            return f"A schedule named '{name}' already exists."
    entry = {
        "id": uuid.uuid4().hex[:10],
        "name": name or task[:40],
        "cadence": cadence,
        "at": str(at or "").strip(),
        "weekdays": str(weekdays or "").strip(),
        "every_minutes": int(every_minutes) if every_minutes else None,
        "task": task[:300],
        "enabled": True,
        "last_run": 0,
        "missed": 0,
    }
    if _to_minutes(entry) is None and not entry["at"]:
        return (f"Unknown cadence '{cadence}'. Try: every minute, every hour, "
                f"every N minutes, every N hours, daily, or daily at HH:MM.")
    items.append(entry)
    _write(items)
    ensure_running()
    at_part = f" at {entry['at']}" if entry["at"] else ""
    return f"Scheduled '{entry['name']}' {cadence}{at_part} to: {task[:80]}."


def remove_schedule(name: str) -> str:
    items = _read()
    kept = [s for s in items if (s.get("name") or "").lower() != str(name).lower()]
    if len(kept) == len(items):
        return f"No schedule named '{name}'."
    _write(kept)
    return f"Removed schedule '{name}'."


def list_schedules() -> str:
    items = _read()
    if not items:
        return "No scheduled tasks."
    lines = []
    for i, s in enumerate(items, 1):
        if not s.get("enabled", True):
            state = " (paused)"
        else:
            state = ""
        at_part = f" at {s['at']}" if s.get("at") else ""
        wd_part = f" {s['weekdays']}" if s.get("weekdays") else ""
        lines.append(f"{i}. {s.get('name')} - {s.get('cadence')}{at_part}{wd_part}{state}")
        lines.append(f"   run: {s.get('task', '')[:120]}")
        n = _runs.get(s.get("id"), {}).get("count", 0)
        if n:
            lines.append(f"   ran {n} time(s) this session")
    return "\n".join(lines)


def pause_schedule(name: str, enabled: bool = False) -> str:
    items = _read()
    for s in items:
        if (s.get("name") or "").lower() == str(name).lower():
            s["enabled"] = bool(enabled)
            _write(items)
            state = "enabled" if enabled else "paused"
            return f"Schedule '{name}' {state}."
    return f"No schedule named '{name}'."


def run_now(name: str) -> str:
    """Manually fire a schedule immediately (speak + log), even if not due."""
    items = _read()
    for s in items:
        if (s.get("name") or "").lower() == str(name).lower():
            _execute(s, force=True)
            return f"Ran '{name}' now."
    return f"No schedule named '{name}'."


# ---------------------------------------------------------------------------
# Executor + daemon
# ---------------------------------------------------------------------------

def _execute(spec: dict, force: bool = False) -> str:
    """Run one schedule's task through the brain and record the outcome."""
    task = spec.get("task", "")
    if not spec.get("enabled", True) and not force:
        return "paused"
    result = ""
    try:
        from ra import brain
        result = brain.ask(task, retrieve=True) or ""
    except Exception as e:
        result = f"[error] {e}"
        ralog.log("err", f"scheduled '{spec.get('name')}' failed: {e}")
    run = _runs.setdefault(spec.get("id"), {"count": 0, "last_error": None})
    run["count"] += 1
    if str(result).startswith("[error]"):
        run["last_error"] = result
    if result and str(result) != "paused":
        try:
            from ra.audio_io import speak_sentences
            speak_sentences(str(result)[:400], cancel_event=threading.Event())
        except Exception:
            pass
    spec["last_run"] = time.time()
    missed = int(spec.get("missed", 0))
    spec["missed"] = max(0, missed - 1) if missed > 0 and result and not str(result).startswith("[error]") else missed
    return result


def _run_due() -> int:
    """Run every due, enabled schedule once. Returns the number run."""
    items = _read()
    ran = 0
    now = time.time()
    for s in items:
        if not s.get("enabled", True):
            continue
        last = float(s.get("last_run") or 0)
        if s.get("missed") and (now - last) >= (_to_minutes(s) or 1440) * 60:
            s["missed"] = int(s.get("missed", 0)) - 1
        try:
            if _due_now(s, now):
                _execute(s)
                ran += 1
                _write(items)
        except Exception as e:
            ralog.log("err", f"scheduler {s.get('name')}: {e}")
    return ran


def ensure_running() -> bool:
    """Start the daemon thread if it isn't already running. Safe to call
    repeatedly (from assistant startup and from add_schedule)."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return True
        _stop_event.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="ra-scheduler")
        _thread.start()
    ralog.log("ok", "scheduler daemon started")
    return True


def _loop():
    while not _stop_event.is_set():
        try:
            _run_due()
        except Exception as e:
            ralog.log("err", f"scheduler loop: {e}")
        _stop_event.wait(_CHECK_INTERVAL)


def stop() -> str:
    _stop_event.set()
    return "Scheduled-task daemon stopped."