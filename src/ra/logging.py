"""
Ra logging hub
==================
Thread-safe message bus: any module calls `log(level, message)` and every
registered handler (the GUI logs pane) receives it. Levels: info, ok, warn,
err, tool, voice.
"""
import threading

_handlers = []
_lock = threading.Lock()

LEVELS = ("info", "ok", "warn", "err", "tool", "voice")


def log(level: str, message: str):
    if level not in LEVELS:
        level = "info"
    for handler in list(_handlers):
        try:
            handler(level, str(message))
        except Exception:
            pass


def register(handler):
    with _lock:
        if handler not in _handlers:
            _handlers.append(handler)


def clear():
    with _lock:
        _handlers.clear()