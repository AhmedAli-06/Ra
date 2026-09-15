"""
Ra Computer Control
=======================
Direct Win32 automation via ctypes - no pyautogui, no extra wheels. Lets the
assistant press keys, type text, move/click the mouse, drive windows, run
shell commands, manipulate files and the clipboard, and screenshot.

Every action is gated behind `computer_access()` so it can be killed from the
GUI or locked down with the RA_COMPUTER_ACCESS=0 env var.
"""
import ctypes
import os
import subprocess
import time
from ctypes import wintypes

from ra import config

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


def _set_dpi_awareness():
    """Make this process Per-Monitor DPI aware so Win32 metrics, SetCursorPos
    and PIL screenshots ALL agree on PHYSICAL pixels.

    Without this, a DPI-unaware process on a scaled display (125%/150%) gets
    virtualized LOGICAL metrics (GetSystemMetrics -> 1536x864 on a 1920x1080
    screen at 125%) while ImageGrab still captures PHYSICAL pixels (1920x1080).
    Every mouse click then lands scaled-down-right of where the vision model
    aimed - the classic "clicked the wrong button / wrong row" bug. Doing this
    at import time also covers dev `python run_app.py` runs, where the Store
    python is unaware by default."""
    if os.name != "nt":
        return
    try:
        aware = ctypes.c_int(0)
        if (ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(aware)) == 0
                and aware.value == 0):
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            if not _user32.IsProcessDPIAware():
                _user32.SetProcessDPIAware()
        except Exception:
            pass


_set_dpi_awareness()


def _no_window() -> dict:
    """subprocess kwargs that stop a console child from flashing a terminal
    window. Ra.exe is windowed (console=False), so without this flag every
    cmd/powershell/where/netsh child pops a black window on screen. CREATE_"""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


# ---------------------------------------------------------------------------
# Consent gate
# ---------------------------------------------------------------------------
_COMPUTER_ACCESS = None


def computer_access() -> bool:
    """Runtime toggle for PC control. On by default (user asked for total
    control); disable from the GUI switch or RA_COMPUTER_ACCESS=0."""
    global _COMPUTER_ACCESS
    if _COMPUTER_ACCESS is None:
        _COMPUTER_ACCESS = (
            config.GRANTED_ACCESS.get("computer", True)
            and os.environ.get("RA_COMPUTER_ACCESS", "1") != "0"
        )
    return _COMPUTER_ACCESS


def set_computer_access(allowed: bool):
    global _COMPUTER_ACCESS
    _COMPUTER_ACCESS = bool(allowed)
    config.GRANTED_ACCESS["computer"] = bool(allowed)


# ---------------------------------------------------------------------------
# Mouse
# ---------------------------------------------------------------------------
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800


def _ensure_enabled():
    if not computer_access():
        raise PermissionError(
            "Computer control is disabled. Ask me to enable computer access "
            "(or flip the HUD switch)."
        )


def get_screen_size() -> str:
    w = _user32.GetSystemMetrics(0)
    h = _user32.GetSystemMetrics(1)
    return f"{w}x{h}"


def _virtual_origin() -> tuple[int, int]:
    """Top-left of the virtual desktop (SM_XVIRTUALSCREEN/SM_YVIRTUALSCREEN)
    in absolute physical screen coordinates. Screenshots of the full virtual
    desktop start at this origin, so absolute coords = img_coord + origin."""
    return (_user32.GetSystemMetrics(76), _user32.GetSystemMetrics(77))


def get_virtual_origin() -> str:
    x, y = _virtual_origin()
    return f"{x},{y}"


def get_mouse_position() -> str:
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return f"({pt.x}, {pt.y})"


def move_mouse(x: int, y: int) -> str:
    _ensure_enabled()
    _user32.SetCursorPos(int(x), int(y))
    return f"Moved mouse to ({x}, {y})."


def _click(button: str = "left"):
    down = up = _MOUSEEVENTF_LEFTDOWN
    if button == "right":
        down, up = _MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP
    elif button == "middle":
        down, up = _MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP
    _user32.mouse_event(down, 0, 0, 0, 0)
    _user32.mouse_event(up, 0, 0, 0, 0)


def click(x: int, y: int, button: str = "left") -> str:
    _ensure_enabled()
    _user32.SetCursorPos(int(x), int(y))
    time.sleep(0.04)          # settle so the target app sees the move
    _click(button)
    return f"Clicked {button} at ({x}, {y})."


def double_click(x: int, y: int) -> str:
    _ensure_enabled()
    _user32.SetCursorPos(int(x), int(y))
    time.sleep(0.04)          # settle so the target app sees the move
    _click("left")
    _click("left")
    return f"Double-clicked at ({x}, {y})."


def scroll(amount: int) -> str:
    _ensure_enabled()
    _user32.mouse_event(_MOUSEEVENTF_WHEEL, 0, 0, int(amount) * 120, 0)
    return f"Scrolled {amount} notches."


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_MAP = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D, "menu": 0x5D,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "capslock": 0x14,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD,
    "mediatoggleplay": 0xB3, "mediaplaypause": 0xB3, "medianext": 0xB0,
    "mediaprev": 0xB1, "mediastop": 0xB2,
    "printscreen": 0x2C, "screenshot": 0x2C, "pause": 0x13, "scrolllock": 0x91,
    "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63,
    "numpad4": 0x64, "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67,
    "numpad8": 0x68, "numpad9": 0x69,
    "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
    "slash": 0xBF, "backslash": 0xDC, "semicolon": 0xBA, "quote": 0xDE,
    "openbracket": 0xDB, "closebracket": 0xDD, "tick": 0xC0,
}


def _vk_for(key: str) -> int:
    key = key.strip().lower()
    if key in VK_MAP:
        return VK_MAP[key]
    if len(key) == 1:
        ch = ord(key.upper())
        if 0x30 <= ch <= 0x39 or 0x41 <= ch <= 0x5A:
            return ch
    if len(key) == 2 and key[0] == "f" and key[1:].isdigit():
        return 0x70 + int(key[1:])
    raise ValueError(f"Unknown key: {key}")


def _send_vk(vk: int, up: bool = False):
    _user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("ki", _KEYBDINPUT)]


def _send_unicode(char: str):
    events = (
        _INPUT(1, _KEYBDINPUT(0, ord(char), KEYEVENTF_UNICODE, 0, 0)),
        _INPUT(1, _KEYBDINPUT(0, ord(char), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)),
    )
    sent = _user32.SendInput(2, ctypes.byref((_INPUT * 2)(*events)), ctypes.sizeof(_INPUT))
    if sent != 2:
        raise RuntimeError("SendInput failed to type a character.")


def type_text(text: str) -> str:
    _ensure_enabled()
    for ch in text:
        if ch == "\n":
            _send_vk(VK_MAP["enter"])
            continue
        if ch == "\t":
            _send_vk(VK_MAP["tab"])
            continue
        _send_unicode(ch)
    return f"Typed {len(text)} characters."


def press_key(key: str) -> str:
    """Press a key or chord, e.g. 'ctrl+c', 'win+d', 'alt+tab', 'enter',
    'volumeup', 'f11'."""
    _ensure_enabled()
    parts = [p.strip() for p in str(key).replace("+", " + ").split() if p.strip()]
    parts = [p for p in parts if p != "+"]
    keys = [_vk_for(p) for p in parts]
    if not keys:
        raise ValueError(f"Nothing to press: {key}")
    for vk in keys:
        _send_vk(vk)
    for vk in reversed(keys):
        _send_vk(vk, up=True)
    return f"Pressed {key}."


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
def _all_window_titles():
    result = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, lparam):
        if _user32.IsWindowVisible(hwnd):
            length = _user32.GetWindowTextLengthW(hwnd) + 1
            buf = ctypes.create_unicode_buffer(length)
            _user32.GetWindowTextW(hwnd, buf, length)
            if buf.value.strip():
                result.append(buf.value)
        return True

    _user32.EnumWindows(_cb, 0)
    return result


def list_windows() -> str:
    titles = _all_window_titles()
    if not titles:
        return "No visible windows found."
    return " | ".join(titles[:25]) + (" ..." if len(titles) > 25 else "")


def focus_window(title: str) -> str:
    _ensure_enabled()
    target = title.lower()
    best = None
    best_score = 0

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, lparam):
        nonlocal best, best_score
        if _user32.IsWindowVisible(hwnd):
            length = _user32.GetWindowTextLengthW(hwnd) + 1
            buf = ctypes.create_unicode_buffer(length)
            _user32.GetWindowTextW(hwnd, buf, length)
            name = buf.value
            if target in name.lower():
                score = len(name) if name.lower() == target else len(target) / max(len(name), 1)
                if score > best_score:
                    best, best_score = hwnd, score
        return True

    _user32.EnumWindows(_cb, 0)
    if not best:
        return f"I couldn't find a window called '{title}'."
    _user32.ShowWindow(best, 9)  # SW_RESTORE
    _user32.SetForegroundWindow(best)
    return f"Focused window '{title}'."


# ---------------------------------------------------------------------------
# Shell + files + clipboard
# ---------------------------------------------------------------------------
def run_shell(command: str, timeout: float = 30.0) -> str:
    _ensure_enabled()
    if not command.strip():
        raise ValueError("Empty shell command.")
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
            **_no_window(),
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout:.0f}s: {command}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    tail = out if out else err
    if not tail:
        return f"Done (exit {proc.returncode})."
    if len(tail) > 1200:
        tail = tail[:1197] + "..."
    return f"exit {proc.returncode}: {tail}"


def list_directory(path: str) -> str:
    _ensure_enabled()
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return f"Not a directory: {path}"
    names = os.listdir(path)
    if not names:
        return f"{path} is empty."
    entries = []
    for n in sorted(names)[:40]:
        full = os.path.join(path, n)
        kind = "dir" if os.path.isdir(full) else "file"
        entries.append(f"{kind} {n}")
    more = f" ... and {len(names) - 40} more" if len(names) > 40 else ""
    return "\n".join(entries) + more


def read_file(path: str, max_chars: int = 3000) -> str:
    _ensure_enabled()
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        return f"File not found: {path}"
    if not os.access(path, os.R_OK):
        return f"No read permission: {path}"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read(max_chars)
    note = "" if len(content) < max_chars else "\n[truncated]"
    return content + note


def write_file(path: str, text: str) -> str:
    _ensure_enabled()
    path = os.path.abspath(os.path.expanduser(path))
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return f"Wrote {len(text)} characters to {path}."


def open_path(path: str) -> str:
    _ensure_enabled()
    path = os.path.abspath(os.path.expanduser(path))
    os.startfile(path)  # noqa: S606 - intentional local automation
    return f"Opened {path}."


# ---------------------------------------------------------------------------
# Media session (SMTC) - the truth about what is actually playing
# ---------------------------------------------------------------------------
# Reads the Windows Global System Media Transport Controls: which media app has
# an active session, its current track title/artist, and playback status. This
# is how Ra VERIFIES a play/pause/resume actually happened instead of trusting
# blind key-presses. Implemented via an inlined PowerShell WinRT interop script
# (System.Runtime.WindowsRuntime AsTask helper) executed with -File to dodge
# all quoting/escaping problems.
_SMTC_PS = r"""
param([string]$Mode = 'read', [string]$AppFilter = '')
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.System,ContentType=WindowsRuntime] | Out-Null
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
try {
    $mgrTask = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()
    $mgr = Await $mgrTask ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
    $sessions = @($mgr.GetSessions())
    if ($Mode -eq 'list') {
        $lines = @()
        foreach ($s in $sessions) {
            $p = $s.GetPlaybackInfo()
            $lines += ("{0}|{1}|app={2}|source={3}" -f $p.PlaybackStatus, ($s.SourceAppUserModelId -replace '[|]','/-'), $s.SourceAppUserModelId, '')
        }
        Write-Output (("SESSIONS=" + ($lines -join '; ')))
        exit
    }
    $session = $null
    if ($AppFilter -ne '') {
        foreach ($s in $sessions) {
            if ($s.SourceAppUserModelId -match $AppFilter) { $session = $s; break }
        }
    } else {
        $session = $mgr.GetCurrentSession()
        if ($null -eq $session) {
            # Some apps (Spotify after unlock, paused players) report sessions
            # but no "current" one - fall back to the first live session.
            foreach ($s in $sessions) { $session = $s; break }
        }
    }
    if ($null -eq $session) { Write-Output 'NONE'; exit }
    if ($Mode -eq 'control') {
        switch ($AppFilter) {
            'play'   { $a = $session.TryPlayAsync();   $ok = Await $a ([bool]) }
            'pause'  { $a = $session.TryPauseAsync();  $ok = Await $a ([bool]) }
            'stop'   { $a = $session.TryStopAsync();   $ok = Await $a ([bool]) }
            'next'   { $a = $session.TrySkipNextAsync();     $ok = Await $a ([bool]) }
            'prev'   { $a = $session.TrySkipPreviousAsync(); $ok = Await $a ([bool]) }
            'toggle' { $a = $session.TryPlayAsync();  $ok = Await $a ([bool]) }
            default  { Write-Output ("UNKNOWN_ACTION=" + $AppFilter); exit }
        }
        if ($ok) { Write-Output ('ACTION_OK') } else { Write-Output 'ACTION_FAIL' }
        exit
    }
    $status = $session.GetPlaybackInfo().PlaybackStatus
    $mediaTask = $session.TryGetMediaPropertiesAsync()
    $media = Await $mediaTask ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
    $title = ($media.Title -replace '[|]','/-').Replace([char]13+[char]10,' ')
    $artist = ($media.Artist -replace '[|]','/-').Replace([char]13+[char]10,' ')
    $appId = $session.SourceAppUserModelId
    Write-Output ("STATUS={0}|TITLE={1}|ARTIST={2}|APP={3}" -f $status,$title,$artist,$appId)
} catch {
    Write-Output ("ERR=" + $_.Exception.Message)
    exit 1
}
"""


def _smtc(mode: str = "read", app_filter: str = "", action: str = "") -> dict | str:
    """Run the SMTC interop script. Returns a parsed status dict for 'read',
    a sessions summary for 'list', or ok/fail for 'control'."""
    import tempfile
    script = _SMTC_PS
    if mode == "control":
        # The script reuses param Mode but control action rides in AppFilter
        script = script.replace("param([string]$Mode = 'read', [string]$AppFilter = '')",
                                f"param([string]$Mode = 'control', [string]$AppFilter = '{action}')")
        mode_ps = "control"
    else:
        mode_ps = mode
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script.replace("param([string]$Mode = 'read', [string]$AppFilter = '')",
                                  f"param([string]$Mode = '{mode_ps}', [string]$AppFilter = '{app_filter}')"))
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
            **_no_window(),
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    text = (out.stdout or "").strip() if out.returncode == 0 else ""
    if mode == "read":
        if not text or text.startswith("ERR="):
            return {"status": "none", "title": "", "artist": "", "app": ""}
        if text == "NONE":
            return {"status": "none", "title": "", "artist": "", "app": ""}
        d = {}
        for part in text.split("|"):
            if "=" in part:
                k, _, v = part.partition("=")
                d[k.strip()] = v.strip()
        return {
            "status": d.get("STATUS", "").lower(),
            "title": d.get("TITLE", ""),
            "artist": d.get("ARTIST", ""),
            "app": d.get("APP", ""),
        }
    if mode == "list":
        return text
    return "ok" if text == "ACTION_OK" else "fail"


def get_media_session() -> dict:
    """Active media session: {status: playing/paused/stopped/none, title,
    artist, app}. This is the source of truth for 'what is actually playing'."""
    return _smtc("read")


def get_media_session_for(app_filter: str) -> dict:
    """Media session filtered by app-id match (e.g. 'spotify', 'edge')."""
    return _smtc("read", app_filter=app_filter)


def media_smtc(action: str) -> str:
    """Control the currently-active media session (play/pause/stop/next/prev/
    toggle). VERIFIES the action landed and reads back the resulting state."""
    ok = _smtc("control", action=action.lower())
    state = _smtc("read")
    if ok != "ok":
        return f"The media transport didn't confirm the {action} command."
    if state["status"] == "none":
        return f"Sent {action}; nothing is reporting a media session right now."
    playing = "Playing" if state["status"] == "playing" else state["status"].capitalize()
    label = f"{state['title']} - {state['artist']}" if state["title"] else "(no track title)"
    return f"{action.capitalize()} - now {playing}: {label}."


def get_clipboard() -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
            **_no_window(),
        )
    except Exception as e:
        return f"Couldn't read clipboard: {e}"
    return (out.stdout or "").strip()


def set_clipboard(text: str) -> str:
    _ensure_enabled()
    safe = text.replace("'", "''")
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{safe}'"],
        timeout=15, **_no_window(),
    )
    return f"Copied to clipboard ({len(text)} chars)."


def screenshot(path: str | None = None) -> str:
    _ensure_enabled()
    target = path or config.SCREENSHOTS_DIR
    os.makedirs(target, exist_ok=True)
    if not path:
        from datetime import datetime
        path = os.path.join(target, f"shot-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png")
    try:
        from PIL import ImageGrab
        ImageGrab.grab().save(path)
    except ImportError:
        return "Screenshot needs PIL (pip install pillow)."
    except Exception as e:
        return f"Screenshot failed: {e}"
    return f"Saved screenshot to {path}."