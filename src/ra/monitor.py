"""
Ra System Monitor
==================
Mark-LI-style proactive hardware telemetry: watch CPU / RAM / disk / battery
and speak or toast when a health gate is crossed. Also builds the morning
briefing (time + weather + battery + top news) the same way Mark-LI's morning
recap does - one call Ra can fire when told 'good morning' or 'what's new'.

Default poll interval 60s, unhealthy-worthy. All running state is cheap.
"""
import threading
import os


def health() -> dict:
    """One-shot read of the four health vectors. Returns statuses + gate booleans."""
    out = {"cpu": None, "ram": None, "disk": None, "battery": None,
           "warn": []}
    try:
        import psutil
        out["cpu"] = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        out["ram"] = mem.percent
        disk = psutil.disk_usage(os.path.abspath(os.sep))
        out["disk"] = disk.percent
        b = psutil.sensors_battery()
        if b is not None:
            out["battery"] = {"percent": b.percent, "plugged": b.power_plugged}
    except Exception:
        return out
    return out


def _describe(h: dict) -> str:
    parts = []
    if h["cpu"] is not None:
        parts.append(f"CPU is at {h['cpu']:.0f}%")
    if h["ram"] is not None:
        parts.append(f"RAM is at {h['ram']:.0f}%")
    if h["disk"] is not None:
        parts.append(f"system drive is {h['disk']:.0f}% full")
    b = h["battery"]
    if isinstance(b, dict):
        state = "charging" if b["plugged"] else "on battery"
        parts.append(f"battery is at {b['percent']}%, {state}")
    return ", ".join(parts) if parts else "no telemetry available"


def report() -> str:
    """Human-readable health status for the report tool / LLM."""
    h = health()
    if not any(v is not None for v in (h["cpu"], h["ram"], h["disk"], h["battery"])):
        return "System monitor couldn't read telemetry (psutil unavailable)."
    return "System health: " + _describe(h) + "."


def monitor(interval: int = 60, cpu_warn: int = 85, ram_warn: int = 85,
            disk_warn: int = 90, batt_low: int = 20, speak: bool = True) -> str:
    """Start (or re-start) a background watcher that toasts (and speaks, per
    the TinMan/KAIROS proactive-check-in pattern) whenever a gate crosses.
    Returns confirmation of the thresholds."""
    global _t, _stop, _alerted
    t = threading.Thread(
        target=_loop, kwargs=dict(interval=interval, cpu_warn=cpu_warn,
                                  ram_warn=ram_warn, disk_warn=disk_warn,
                                  batt_low=batt_low, speak=speak), daemon=True)
    _stop = threading.Event()
    _alerted = set()
    _t = t
    t.start()
    voice = " and I'll speak up" if speak else ""
    return (f"Monitoring started. I'll flag CPU/{cpu_warn}%, RAM/{ram_warn}%, "
            f"disk/{disk_warn}% or battery under {batt_low}% every {interval}s"
            f"{voice}.")


_t, _stop, _alerted = None, None, set()
_MONITOR_LOCK = threading.Lock()


def _toast(title: str, msg: str):
    try:
        from ra import skills
        return skills.toast_notify(title, msg)
    except Exception:
        return ""


def _should_alert(kind: str) -> bool:
    """True when the gate just crossed and hasn't been re-armed."""
    if kind in _alerted:
        return False
    _alerted.add(kind)
    # Re-arm after a health break so the alert can fire again later.
    thread = threading.Timer(180.0, lambda: _alerted.discard(kind))
    thread.daemon = True
    thread.start()
    return True


def _loop(interval, cpu_warn, ram_warn, disk_warn, batt_low, speak=True):
    while not _stop.is_set():
        try:
            h = health()
            msgs = []
            if h["cpu"] is not None and h["cpu"] >= cpu_warn and _should_alert("cpu"):
                msgs.append(f"CPU is high at {h['cpu']:.0f}%.")
            if h["ram"] is not None and h["ram"] >= ram_warn and _should_alert("ram"):
                msgs.append(f"RAM is high at {h['ram']:.0f}%.")
            if h["disk"] is not None and h["disk"] >= disk_warn and _should_alert("disk"):
                msgs.append(f"System drive is {h['disk']:.0f}% full - consider cleanup.")
            b = h["battery"]
            if (isinstance(b, dict) and not b["plugged"] and b["percent"] <= batt_low
                    and _should_alert("batt")):
                msgs.append(f"Battery is down to {b['percent']}% - plug in soon.")
            for m in msgs:
                _toast("Ra system monitor", m)
                if speak:
                    _speak_alert(m)
        except Exception:
            pass
        _stop.wait(interval)


def _speak_alert(message: str):
    """TinMan/KAIROS proactive check-in: the monitor speaks the gate crossing,
    not just toasts it. Failure-tolerant - no audio stack, no crash."""
    try:
        from ra.audio_io import speak_sentences
        import threading as _t
        speak_sentences(message, cancel_event=_t.Event())
    except Exception:
        pass


def stop_monitor() -> str:
    if _stop is not None:
        _stop.set()
    return "System monitoring stopped."


def morning_briefing() -> str:
    """A concise across-days morning recap: time, weather, battery, headlines."""
    from ra import skills
    lines = [skills.get_time()]
    home = os.environ.get("RA_HOME_CITY", "").strip()
    if home:
        try:
            wx = skills.get_weather(home)
            if wx and "Couldn't" not in wx:
                lines.append(wx)
        except Exception:
            pass
    lines.append(skills.get_battery())
    h = health()
    if any(v is not None for v in (h["cpu"], h["ram"])):
        lines.append(skills.get_system_status())
    try:
        news = skills.get_news("")
        if news and "No news" not in news and "Couldn't" not in news:
            top = "\n".join(news.splitlines()[:2])
            lines.append(f"Top news: {top}")
    except Exception:
        pass
    return " ".join(lines)