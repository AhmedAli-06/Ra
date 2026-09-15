"""Drive the real HUD app (run.py's API) programmatically and screenshot modes."""
import os
import sys
import threading
import time

os.environ.setdefault("RA_HUD_PORT", "8747")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _grab_rect(x, y, w, h):
    from System import Drawing
    bmp = Drawing.Bitmap(int(w), int(h))
    g = Drawing.Graphics.FromImage(bmp)
    g.CopyFromScreen(int(x), int(y), 0, 0, bmp.Size)
    g.Dispose()
    c1 = bmp.GetPixel(2, 2)
    c2 = bmp.GetPixel(int(w / 2), int(h / 2))
    bmp.Dispose()
    return ((c1.R, c1.G, c1.B), (c2.R, c2.G, c2.B))


def _grab_grid(x, y, w, h):
    from System import Drawing
    bmp = Drawing.Bitmap(int(w), int(h))
    g = Drawing.Graphics.FromImage(bmp)
    g.CopyFromScreen(int(x), int(y), 0, 0, bmp.Size)
    g.Dispose()
    pts = [(0.5, 0.5), (0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)]
    out = []
    for fx, fy in pts:
        p = bmp.GetPixel(int(w * fx), int(h * fy))
        out.append((p.R, p.G, p.B))
    bmp.Dispose()
    return out


def _drive(api, win):
    from webview.platforms.winforms import BrowserView

    def form(win):
        return BrowserView.instances[win.uid]

    def info(tag, win):
        import ctypes
        from ctypes import windll, wintypes
        class RECT(wintypes.RECT): pass
        r = RECT()
        windll.user32.GetWindowRect(wintypes.HWND(form(win).Handle.ToInt32()), ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        g = _grab_grid(r.left, r.top, w, h)
        print("%-12s rect=(%d,%d,%dx%d) grid=%s" % (tag, r.left, r.top, w, h, g))

    time.sleep(3.5)
    info("full       ", win)
    api.set_mode("orb")
    time.sleep(3)
    info("orb        ", win)
    api.expand()
    time.sleep(3)
    info("expanded   ", win)

    import threading as _t
    _t.Timer(2.0, lambda: os._exit(0)).start()


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from run import _HudApi, _MAIN_SIZE
    import webview

    # start the server exactly like run.py does
    from run import _start_server, _wait_ready, _url
    threading.Thread(target=_start_server, daemon=True).start()
    _wait_ready()

    api = _HudApi(None)
    win = webview.create_window(
        "Ra — Neural Command Interface", _url("/"),
        width=_MAIN_SIZE["full"][0], height=_MAIN_SIZE["full"][1],
        min_size=(150, 150), frameless=True, easy_drag=False,
        background_color="#08090d", text_select=True, js_api=api,
    )
    api._main = win
    webview.start(func=lambda: _drive(api, win), gui="edgechromium")


if __name__ == "__main__":
    main()