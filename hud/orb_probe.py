"""Orb window probe: verify the transparent orb child window displays (and
stays transparent across show/hide/re-show), by capturing its screen region.

Run from the Ra\\hud dir.
"""
import os
import threading
import time
import urllib.request

import webview

# Microsoft's documented fix for transparent WebView2 (WinForms): route the
# compositor through the CPU path which honours DefaultBackgroundColor.
os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--disable-gpu"

_APP_PORT = 8791


def _start_server():
    import uvicorn
    import server
    uvicorn.run(server.app, host="127.0.0.1", port=_APP_PORT, log_level="warning")


def _wait_ready():
    for _ in range(200):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{_APP_PORT}/api/health", timeout=1)
            return
        except Exception:
            time.sleep(0.05)


def _grab_rect(x, y, w, h):
    """Return (corner_rgb, center_rgb) for an absolute screen rect."""
    from System import Drawing
    bmp = Drawing.Bitmap(w, h)
    g = Drawing.Graphics.FromImage(bmp)
    g.CopyFromScreen(x, y, 0, 0, bmp.Size)
    g.Dispose()
    c1 = bmp.GetPixel(2, 2)
    c2 = bmp.GetPixel(int(w / 2), int(h / 2))
    bmp.Dispose()
    return ((c1.R, c1.G, c1.B), (c2.R, c2.G, c2.B))


def _grab(hwnd):
    """Return (corner_rgb, center_rgb) of the native window hwnd."""
    import ctypes
    from ctypes import windll, wintypes

    class RECT(wintypes.RECT):
        pass

    r = RECT()
    windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
    return _grab_rect(r.left, r.top, r.right - r.left, r.bottom - r.top)


def _drive(orb, main_win, api):
    from webview.platforms.winforms import BrowserView
    from ctypes import windll

    def orb_form(win):
        return BrowserView.instances[win.uid]

    time.sleep(3)
    try:
        base = _grab_rect(760, 80, 212, 212)
        print(f"desktop-baseline: corner={base[0]} center={base[1]}")

        # Force the classic layered form state too (harmless with the CPU
        # compositor path; the TransparencyKey COULD punch out the orb visuals
        # so we stage it but rely on genuine alpha first).
        f = orb_form(orb)
        f.AllowTransparency = True
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x80000
        hwnd = f.Handle.ToInt32()
        ex = windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        print("form patched: layered=True (CPU compositing via env var)")
        orb.show()
        time.sleep(2)

        def snap(tag):
            r, c = _grab(orb_form(orb).Handle.ToInt32())
            print(f"{tag}: pos=({orb.x},{orb.y}) corner={r} center={c}")
            return r, c

        r1, c1 = snap("shown-1")
        orb.hide()
        time.sleep(1)
        orb.show()
        time.sleep(2)
        r2, c2 = snap("shown-2  ")
        orb.move(orb.x + 60, orb.y + 60)
        time.sleep(1)
        r3, c3 = snap("moved     ")

        def close_to(a, b):
            return all(abs(x - y) <= 25 for x, y in zip(a, b))

        print("----------")
        print("corner matches desktop baseline (transparent) ?",
              close_to(r1[0], base[0]))
        print("center differs from baseline (orb drawn) ?",
              not close_to(c1[1], base[1]))
        print("re-show still transparent ?", close_to(r2[0], base[0]))
    finally:
        try:
            webview.destroy_window(orb)
        except Exception:
            pass
        try:
            webview.destroy_window(main_win)
        except Exception:
            pass
        time.sleep(1)
        threading.Timer(1.5, lambda: os._exit(0)).start()


def main():
    thread = threading.Thread(target=_start_server, daemon=True)
    thread.start()
    _wait_ready()

    class Api:
        def __init__(self):
            self.visible = False

    api = Api()
    orb = webview.create_window(
        "Probe Orb", f"http://127.0.0.1:{_APP_PORT}/orb",
        width=212, height=212, frameless=True, transparent=True, on_top=True,
        hidden=True, resizable=False, focus=False, shadow=False, js_api=api,
        x=760, y=80,
    )
    main_win = webview.create_window("Probe Main", f"http://127.0.0.1:{_APP_PORT}",
                                     width=640, height=480, frameless=True,
                                     x=40, y=40)

    webview.start(func=lambda: _drive(orb, main_win, api),
                  gui="edgechromium")


if __name__ == "__main__":
    main()