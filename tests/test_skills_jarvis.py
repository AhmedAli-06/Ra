"""Jarvis-grade skills: media keys, Spotify player, live web answers, system
power, process control, networking and file management. Offline-safe: any real
keyboard/video/power actions are mocked."""
import types
from types import SimpleNamespace

import pytest

from ra.skills import (
    _DISPATCH, _MEDIA_KEYS, get_now_playing, manage_files, media_control,
    network_status, open_website, play_spotify, process_control,
    system_power, toast_notify,
)


@pytest.fixture
def fake_press(monkeypatch):
    """Record every press_key call so no real keys are injected."""
    pressed = []
    monkeypatch.setattr("ra.skills._computer.press_key",
                        lambda k: pressed.append(k))
    return pressed


@pytest.fixture
def fake_smtc(monkeypatch):
    """Mock the SMTC media-session read-back so tests never call PowerShell."""
    def _fake_session(app_filter=""):
        return {"status": "none", "title": "", "artist": "", "app": ""}
    def _fake_control(app_filter=""):
        return "ok"
    monkeypatch.setattr("ra.skills._computer.get_media_session_for",
                        lambda af="": _fake_session(af))
    monkeypatch.setattr("ra.skills._computer.get_media_session",
                        lambda: _fake_session())


class _FakeClock:
    """Stands in for ra.skills.time: sleep advances the fake clock and time()
    ticks forward on each read, so polling loops finish instantly and
    deterministically instead of waiting real wall-clock seconds."""

    def __init__(self):
        self.t = 1000.0

    def time(self):
        self.t += 0.5
        return self.t

    def sleep(self, s):
        self.t += float(s or 0)


# ---------------------------------------------------------------------------
# media_control
# ---------------------------------------------------------------------------
def test_media_control_play_resume_map_to_toggle(fake_press, fake_smtc):
    for action in ("play", "pause", "resume", "toggle play"):
        media_control(action)
    assert fake_press == ["mediaplaypause"] * 4


def test_media_control_next_previous_stop(fake_press, fake_smtc):
    out = media_control("next")
    assert "Nothing is reporting" in out
    out = media_control("previous")
    assert "Nothing is reporting" in out
    media_control("stop")
    assert "Nothing is reporting" in out
    assert fake_press == ["medianext", "mediaprev", "mediastop"]


def test_media_control_volume_up_repeats(fake_press):
    out = media_control("volume up", amount=3)
    assert fake_press == ["volumeup"] * 3
    assert "up by 3" in out


def test_media_control_rejects_junk(fake_press, fake_smtc):
    out = media_control("dance the hokey pokey")
    assert fake_press == []
    assert "Try: play, pause" in out


# ---------------------------------------------------------------------------
# play_spotify
# ---------------------------------------------------------------------------
def test_play_spotify_web_fallback_when_not_installed(monkeypatch):
    opened = []
    monkeypatch.setattr("ra.skills._spotify_installed", lambda: False)
    monkeypatch.setattr("ra.skills.open_website",
                        lambda url, browser=None: opened.append(url) or "Opened the web player")
    out = play_spotify("tame impala loser")
    assert opened, "web player should have been opened"
    assert "open.spotify.com/search/tame+impala+loser" in opened[0]
    assert "Opened the web player" in out


def test_play_spotify_desktop_path(monkeypatch, fake_press, fake_smtc):
    started = []
    # Fake clock: no wall-clock polling waits during the test.
    clock = _FakeClock()
    monkeypatch.setattr("ra.skills.time", clock)
    monkeypatch.setattr("ra.skills._spotify_installed", lambda: True)
    monkeypatch.setattr("ra.skills.os.startfile", lambda uri: started.append(uri))
    # Never touch the real accessible tree / real windows from a test.
    monkeypatch.setattr("ra.uia.ui_click", lambda name, double=False, region="":
                        "No control found.")
    monkeypatch.setattr("ra.skills._computer.focus_window", lambda t: "ok")
    out = play_spotify("loser")
    assert started == ["spotify:search:loser"]
    assert "Spotify opened" in out or "confirm" in out


def test_play_spotify_desktop_uia_row_click_plays(monkeypatch, fake_press, fake_smtc):
    """The accessible-tree double-click of the result row must be attempted
    (not just a blind Enter) and success must come from SMTC verification."""
    started = []
    clicked = []
    clock = _FakeClock()
    monkeypatch.setattr("ra.skills.time", clock)
    monkeypatch.setattr("ra.skills._spotify_installed", lambda: True)
    monkeypatch.setattr("ra.skills.os.startfile", lambda uri: started.append(uri))
    monkeypatch.setattr("ra.uia.ui_click", lambda name, double=False, region="":
                        clicked.append((name, double)) or f"Double-clicked ListItem '{name}'")
    # The OS reports 'nothing' first (the 'before' snapshot), then a NEW track.
    calls = {"n": 0}
    def _session(af=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "none", "title": "", "artist": "", "app": ""}
        return {"status": "playing", "title": "Loser",
                "artist": "Tame Impala", "app": "spotify"}
    monkeypatch.setattr("ra.skills._computer.get_media_session_for",
                        lambda af="": _session(af))
    out = play_spotify("loser by tame impala")
    assert clicked and clicked[0][0] == "loser" and clicked[0][1] is True
    assert "Playing Loser" in out and "verified" in out


def test_play_spotify_desktop_startfile_error_falls_web(monkeypatch, fake_smtc):
    opened = []
    monkeypatch.setattr("ra.skills._spotify_installed", lambda: True)
    monkeypatch.setattr("ra.skills.os.startfile", lambda uri: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr("ra.skills.open_website",
                        lambda url, browser=None: opened.append(url) or "Opened web player")
    out = play_spotify("loser")
    assert opened, "should have fallen back to the web player"
    assert "open.spotify.com/search/" in opened[0]


# ---------------------------------------------------------------------------
# web_search_results (live internet answers)
# ---------------------------------------------------------------------------
def test_web_search_results_uses_ddgs(monkeypatch):
    shipped = []

    class FakeDDGS:
        def __init__(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, q, max_results=5):
            shipped.append((q, max_results))
            return [{"title": "Loser - Tame Impala", "href": "https://genius.com/Tame-impala-loser-lyrics",
                     "body": "Second single from the album Deadbeat."}]

    monkeypatch.setitem(__import__("sys").modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))
    out = _DISPATCH["web_search_results"]({"query": "tame impala loser", "num": 3})
    assert shipped == [("tame impala loser", 3)]
    assert "Loser - Tame Impala" in out
    assert "genius.com" in out


def test_web_search_results_no_results(monkeypatch):
    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, q, max_results=5):
            return []

    monkeypatch.setitem(__import__("sys").modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))
    out = _DISPATCH["web_search_results"]({"query": "zzzzz", "num": 3})
    assert "No results" in out


# ---------------------------------------------------------------------------
# system_power
# ---------------------------------------------------------------------------
def test_system_power_sleep_restart(monkeypatch):
    cmds = []
    monkeypatch.setattr("ra.skills._computer.run_shell",
                        lambda c: cmds.append(c))
    out = system_power("sleep")
    assert "sleep" in out.lower()
    assert any("SetSuspendState" in c for c in cmds)
    out = system_power("restart")
    assert cmds[-1] == "shutdown /r /t 5"


def test_system_power_lock_uses_lock_pc(monkeypatch):
    called = []
    monkeypatch.setattr("ra.skills.lock_pc", lambda: called.append(True))
    system_power("lock")
    assert called


# ---------------------------------------------------------------------------
# process_control + network_status
# ---------------------------------------------------------------------------
def test_process_control_list_parses_windows(monkeypatch):
    class FakePopenResult:
        returncode = 0
        stdout = "Spotify\r\nCode\r\n"
        stderr = ""

    monkeypatch.setattr("ra.skills.subprocess.run",
                        lambda *a, **k: FakePopenResult())
    out = _DISPATCH["process_control"]({"action": "list"})
    assert "Spotify" in out and "Code" in out


def test_network_status_parses_ssid(monkeypatch):
    class FakePopenResult:
        returncode = 0
        stdout = "    SSID                    : patched-net\n    BSSID                   : xx\n"
        stderr = ""

    monkeypatch.setattr("ra.skills.subprocess.run",
                        lambda *a, **k: FakePopenResult())
    fake_sock = SimpleNamespace()
    fake_sock.connect = lambda *a: None
    fake_sock.getsockname = lambda: ("10.0.0.9", 0)
    fake_sock.close = lambda: None
    monkeypatch.setattr("socket.socket", lambda *a, **k: fake_sock)
    monkeypatch.setattr("requests.get",
                        lambda url, timeout=5: SimpleNamespace(text="8.8.8.8"))
    out = network_status()
    assert "patched-net" in out
    assert "10.0.0.9" in out
    assert "8.8.8.8" in out


# ---------------------------------------------------------------------------
# manage_files
# ---------------------------------------------------------------------------
def test_manage_files_copy_delete(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hi")
    dst = tmp_path / "b.txt"
    assert "Copied" in manage_files("copy", str(src), str(dst))
    assert dst.exists() and dst.read_text() == "hi"
    assert "Deleted" in manage_files("delete", str(dst))
    assert not dst.exists()


def test_manage_files_move_rename(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("x")
    moved = tmp_path / "m.txt"
    assert "Moved" in manage_files("move", str(src), str(moved))
    assert moved.exists() and not src.exists()
    renamed = tmp_path / "r.txt"
    assert "Renamed" in manage_files("rename", str(moved), str(renamed))
    assert renamed.exists()


# ---------------------------------------------------------------------------
# get_now_playing + toast_notify
# ---------------------------------------------------------------------------
def test_get_now_playing_spotify_title(monkeypatch, fake_smtc):
    monkeypatch.setattr("ra.skills._computer.list_windows",
                        lambda: "Spotify | Dope - Tame Impala")
    out = get_now_playing()
    assert "Dope - Tame Impala" in out


def test_get_now_playing_smtc_verified(monkeypatch):
    def _fake_session():
        return {"status": "playing", "title": "Let It Happen", "artist": "Tame Impala",
                "app": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"}
    monkeypatch.setattr("ra.skills._computer.get_media_session",
                        lambda: _fake_session())
    out = get_now_playing()
    assert "Let It Happen" in out and "Tame Impala" in out and "Playing" in out


def test_get_now_playing_nothing(monkeypatch, fake_smtc):
    monkeypatch.setattr("ra.skills._computer.list_windows",
                        lambda: "Script Editor")
    out = get_now_playing()
    assert "Nothing" in out


def test_toast_notify_runs_powershell(monkeypatch):
    calls = []
    monkeypatch.setattr("ra.skills.subprocess.run",
                        lambda *a, **k: calls.append(a[0]) or SimpleNamespace(returncode=0))
    out = toast_notify("Ra", "Done")
    assert "Notification sent" in out
    assert any("ToastNotificationManager" in " ".join(c) for c in calls)


# ---------------------------------------------------------------------------
# new tool surfaced in the LLM-facing catalog
# ---------------------------------------------------------------------------
def test_new_skills_exposed():
    from ra.skills import TOOLS
    names = {t["name"] for t in TOOLS}
    assert {"play_spotify", "media_control", "web_search_results", "system_power",
            "process_control", "network_status", "manage_files", "set_volume",
            "get_now_playing", "toast_notify", "get_selected_text"} <= names


def test_media_keys_map_sane():
    assert _MEDIA_KEYS["resume"] == "mediaplaypause"
    assert _MEDIA_KEYS["skip"] == "medianext"
    assert _MEDIA_KEYS["mute"] == "volumemute"