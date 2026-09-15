"""Browser-aware skills: default-browser fallback, named-browser launch via
real exe, and the search-URL builders. No real browser is ever launched."""
import pytest

from ra.skills import (
    _DISPATCH, _launch_browser, _norm_browser, open_website, web_search,
    youtube_search,
)


@pytest.fixture
def fake_launch(monkeypatch):
    """Replace _launch_browser with a recorder that returns a fake exe path.

    first_result can be set to None to simulate 'browser not installed'."""
    calls = []
    state = {"result": "C:\\fake\\browser.exe"}

    def _record(browser, url=None):
        calls.append((browser, url))
        return state["result"]

    monkeypatch.setattr("ra.skills._launch_browser", _record)
    return calls, state


def _capture_open(monkeypatch):
    opened = []
    monkeypatch.setattr("ra.skills.webbrowser.open", lambda u: opened.append(u))
    return opened


def test_open_website_default_browser(monkeypatch):
    opened = _capture_open(monkeypatch)
    result = open_website("youtube.com")
    assert opened == ["https://youtube.com"]
    assert "default browser" in result


def test_open_website_adds_scheme(monkeypatch):
    opened = _capture_open(monkeypatch)
    open_website("example.org/path")
    assert opened == ["https://example.org/path"]


def test_open_website_named_browser_launches_exe(fake_launch):
    calls, _state = fake_launch
    result = open_website("youtube.com", browser="brave")
    assert calls == [("brave", "https://youtube.com")]
    assert "in brave" in result


def test_open_website_named_browser_missing_falls_back_to_default(monkeypatch, fake_launch):
    calls, state = fake_launch
    opened = _capture_open(monkeypatch)
    state["result"] = None  # browser not installed
    result = open_website("youtube.com", browser="opera")
    assert calls == [("opera", "https://youtube.com")]
    assert opened == ["https://youtube.com"]
    assert "default browser" in result


def test_youtube_search_builds_search_url(fake_launch):
    calls, _state = fake_launch
    result = youtube_search("avengers doomsday trailer", browser="brave")
    assert calls == [("brave",
                      "https://www.youtube.com/results?search_query=avengers+doomsday+trailer")]
    assert "brave" in result


def test_web_search_builds_google_url(fake_launch):
    calls, _state = fake_launch
    result = web_search("best curry recipe", browser="chrome")
    assert calls == [("chrome", "https://www.google.com/search?q=best+curry+recipe")]
    assert "chrome" in result


def test_norm_browser_aliases():
    assert _norm_browser("Brave") == "brave"
    assert _norm_browser("Google Chrome") == "chrome"
    assert _norm_browser("Mozilla Firefox") == "firefox"
    assert _norm_browser("chrome") == "chrome"


def test_browser_tools_exposed():
    names = {t["name"] for t in __import__("ra.skills", fromlist=["TOOLS"]).TOOLS}
    assert {"youtube_search", "web_search", "gui_do", "see_screen"} <= names


def test_dispatch_youtube_search_calls_through(fake_launch):
    calls, _state = fake_launch
    out = _DISPATCH["youtube_search"]({"query": "hello world", "browser": "brave"})
    assert calls == [("brave", "https://www.youtube.com/results?search_query=hello+world")]


def test_launch_browser_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr("ra.skills._find_browser", lambda name: None)
    assert _launch_browser("not-a-browser") is None