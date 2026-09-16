"""Ra HUD — native desktop shell.

Runs the FastAPI server on a loopback port and opens the HUD in a native
pywebview (WebView2) window. Call: python run.py  (or the frozen Ra.exe)

The whole app lives in ONE borderless window; every mode is a resize + URL
swap of that same window:

  - 'full'   → the full HUD (1280×820)
  - 'dock'   → compact side panel (420×820, always-on-top)
  - 'widget' → compact card (440×590, always-on-top)
  - 'orb'    → the same window navigates to /orb and shrinks to a tight
               bottom-right chip (~220×220) showing only the 3D orb.

A single window avoids WebView2's broken per-pixel transparency on this
machine (verified by probe: DefaultBackgroundColor=Transparent renders an
opaque white/dark base, so a second "transparent" orb window was showing up
as a solid box — the orb the user saw "disappearing").

Bridge (exposed to JS as `window.pywebview.api`):
  - set_mode(mode)       — switch mode (resize/position/navigate as above)
  - move_window(dx, dy)  — drag the window (HUD header + orb page)
  - expand()             — orb page clicked: back to the full HUD
  - minimize() / close() — window controls for the borderless title bar
"""
import os
import threading
import time
import urllib.request
from pathlib import Path

_APP_PORT = int(os.environ.get("RA_HUD_PORT", "8747"))
_MAIN_SIZE = {"full": (1280, 820), "dock": (420, 820), "widget": (440, 590)}
_ORB_SIZE = (220, 220)
_ORB_MARGIN = 24


def _screen_logical() -> tuple:
    """Primary-screen size in pywebview's LOGICAL pixels.

    pywebview's move()/resize()/create-window sizes are logical (DPI-aware),
    but webview.screens returns PHYSICAL dimensions on this setup, so using
    those directly puts windows off-screen at >100% scaling."""
    from ctypes import windll
    dpi = windll.user32.GetDpiForSystem()
    scale = dpi / 96.0
    phys_w = windll.user32.GetSystemMetrics(0)
    phys_h = windll.user32.GetSystemMetrics(1)
    return int(phys_w / scale), int(phys_h / scale)


def _orb_screen_pos() -> tuple:
    """Bottom-right of the primary screen (logical px) with a margin."""
    orb_w, orb_h = _ORB_SIZE
    w, h = _screen_logical()
    x = int(w - orb_w - _ORB_MARGIN)
    y = int(h - orb_h - _ORB_MARGIN)
    return max(x, 0), max(y, 0)


def _center_pos(w: int, h: int) -> tuple:
    sw, sh = _screen_logical()
    return int((sw - w) / 2), int((sh - h) / 2)


def _set_corner_rounding(win, rounded: bool):
    """DWM window-corner preference: rounded (orb chip) vs default."""
    try:
        from webview.platforms.winforms import BrowserView
        import ctypes
        from ctypes import windll
        form = BrowserView.instances[win.uid]
        hwnd = form.Handle.ToInt32()
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        val = ctypes.c_int(2 if rounded else 0)
        windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(val), 4)
    except Exception:
        pass


def _url(path: str = "") -> str:
    return f"http://127.0.0.1:{_APP_PORT}{path}"


class _HudApi:
    """Python bridge reachable from JS via window.pywebview.api."""

    def __init__(self, main_window):
        self._main = main_window

    # -- mode switching -------------------------------------------------
    def set_mode(self, mode: str):
        if self._main is None:
            return
        if mode == "orb":
            self._enter_orb()
            return
        if mode in _MAIN_SIZE:
            w, h = _MAIN_SIZE[mode]
            try:
                self._main.resize(w, h)
            except Exception:
                pass
            x, y = _center_pos(w, h)
            try:
                self._main.move(x, y)
            except Exception:
                pass
        try:
            _set_corner_rounding(self._main, False)
        except Exception:
            pass
        try:
            self._main.set_on_top(True)
        except Exception:
            pass

    def _enter_orb(self):
        if self._main is None:
            return
        pos = _orb_screen_pos()
        if pos:
            try:
                self._main.move(*pos)
            except Exception:
                pass
        try:
            self._main.resize(*_ORB_SIZE)
        except Exception:
            pass
        try:
            _set_corner_rounding(self._main, True)
        except Exception:
            pass
        try:
            self._main.set_on_top(True)
        except Exception:
            pass

    def expand(self):
        """Orb clicked → return to the full main HUD."""
        try:
            self._main.set_on_top(True)
        except Exception:
            pass
        self._main.resize(*_MAIN_SIZE["full"])
        x, y = _center_pos(*_MAIN_SIZE["full"])
        try:
            self._main.move(x, y)
        except Exception:
            pass
        try:
            _set_corner_rounding(self._main, False)
        except Exception:
            pass
        try:
            self._main.run_js("setWindowMode('full')")
        except Exception:
            pass

    # -- dragging -------------------------------------------------------
    def move_window(self, dx, dy):
        """Drag the (single) borderless window by a screen-space delta."""
        if self._main is None:
            return
        try:
            self._main.move(
                int(self._main.x) + int(dx or 0),
                int(self._main.y) + int(dy or 0),
            )
        except Exception:
            pass

    # -- window controls ------------------------------------------------
    def minimize(self):
        try:
            self._main.minimize()
        except Exception:
            pass

    def close(self):
        try:
            self._main.destroy()
        except Exception:
            pass


def _start_server():
    import uvicorn
    from server import app
    uvicorn.run(app, host="127.0.0.1", port=_APP_PORT, log_level="warning")


def _wait_ready(timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(_url("/api/health"), timeout=1)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def main():
    import traceback
    try:
        _main()
    except BaseException:
        log = Path(os.environ.get("TEMP", ".")) / "ra-crash.log"
        try:
            log.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise


def _main():
    global webview
    import webview

    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()
    if not _wait_ready():
        print("HUD server failed to start on port", _APP_PORT)

    api = _HudApi(None)
    main_window = webview.create_window(
        "Ra — Neural Command Interface",
        _url("/"),
        width=_MAIN_SIZE["full"][0],
        height=_MAIN_SIZE["full"][1],
        min_size=(150, 150),
        frameless=True,
        on_top=True,
        easy_drag=False,
        background_color="#08090d",
        text_select=True,
        js_api=api,
    )
    api._main = main_window

    try:
        webview.start()
    finally:
        try:
            from ra.audio_io import stop_mic, stop_speaking
            stop_speaking()
            stop_mic()
        except Exception:
            pass


if __name__ == "__main__":
    main()