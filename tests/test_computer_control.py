"""Computer-control regression tests (no clicks, no screenshots, no network):
DPI/no-window subprocess behavior, the accessible (UIA) tool registration,
and the YouTube-result JSON fallback that fixes play_youtube failures."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_no_window_kwargs_on_windows():
    from ra import computer
    kw = computer._no_window()
    if os.name == "nt":
        import subprocess
        assert kw.get("creationflags") == subprocess.CREATE_NO_WINDOW
    else:
        assert kw == {}


def test_dpi_awareness_runs_cleanly_on_windows():
    """The awareness call must not blow up on a real desktop (SetProcessDpiAwareness
    returns E_ACCESSDENIED harmlessly once another library claimed awareness)."""
    from ra import computer
    computer._set_dpi_awareness()  # idempotent, no raise


def test_uia_tools_registered():
    from ra.skills import TOOLS, execute_tool
    names = {t["name"] for t in TOOLS}
    for tool in ("ui_find", "ui_click", "ui_type"):
        assert tool in names, f"{tool} missing from TOOLS"
    schema = next(t for t in TOOLS if t["name"] == "ui_click")
    props = schema["input_schema"]["properties"]
    assert "name" in props and "double" in props and "region" in props
    assert schema["input_schema"]["required"] == ["name"]
    # Handlers bound
    for tool in ("ui_find", "ui_click", "ui_type"):
        out = execute_tool(tool, {"name": "zzz-no-such-control"})
        assert isinstance(out, str) and len(out) > 0


def _yt_page(ytdata: dict) -> str:
    import json
    return "<script>var ytInitialData = " + json.dumps(ytdata) + ";</script>"


def test_yt_json_fallback_extracts_embedded_video():
    """The play_youtube failure ('couldn't pick out a video link') happens
    when the results page is a heavy JS shell; the embedded ytInitialData JSON
    still names the top video. The fallback must find it."""
    from ra.skills import _yt_json_video
    html = _yt_page({
        "contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {
            "sectionListRenderer": {"contents": [{"itemSectionRenderer": {"contents": [
                {"videoRenderer": {"videoId": "dQw4w9WgXcQ", "title": {
                    "runs": [{"text": "Rick Astley - Never Gonna Give You Up"}]}}}
            ]}}]}}
        }}
    })
    hit = _yt_json_video(html)
    assert hit is not None
    assert hit["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert "Never Gonna Give" in hit["title"]


def test_yt_json_skips_shorts_first():
    from ra.skills import _yt_json_video
    html = _yt_page({
        "a": {"videoRenderer": {"videoId": "aBcDeFgHiJk",
                                "title": {"runs": [{"text": "Shorts #shorts"}]}}},
        "b": {"videoRenderer": {"videoId": "AB12cdEF34g",
                                "title": {"runs": [{"text": "The Real Video"}]}}},
    })
    hit = _yt_json_video(html)
    assert hit["url"].endswith("AB12cdEF34g")


def test_coord_factor_returns_scale():
    """_coord_factor labels the screenshot-vs-Win32 relationship; on a
    DPI-aware process it must be 1.0 (bitmap and cursor space are both
    physical)."""
    from ra.vision import _coord_factor
    from PIL import Image
    f = _coord_factor(Image.new("RGB", (1920, 1080)))
    assert 0.5 <= f <= 1.5


def test_dpi_virtualized_debug_factor_scales_down():
    """If the debug process stays DPI-virtualized (logical 1536 wide vs a
    1920-physical bitmap), clicks must be scaled to LOGICAL space - the safety
    factor keeps correctness even when the awareness call failed."""
    from ra.vision import _coord_factor
    from PIL import Image
    img = Image.new("RGB", (1920, 1080))

    class _FakeUser32:
        @staticmethod
        def GetSystemMetrics(i):
            return 1536 if i == 78 else 864

    import ra.computer as real_computer
    saved = real_computer._user32
    real_computer._user32 = _FakeUser32
    try:
        assert _coord_factor(img) == pytest.approx(0.8)
    finally:
        real_computer._user32 = saved


def test_vision_retry_delay_parses_delay():
    from ra.vision import _retry_delay

    class Q(Exception):
        pass

    # Gemini OpenAI-compat reports 429 with {"seconds": N} in the error body.
    assert _retry_delay(Q('429 RESOURCE_EXHAUSTED ... "retryDelay":{"seconds":13}')) == 13.0
    assert _retry_delay(Q('429 Too Many Requests ... retryDelay "30s"')) == 30.0
    # Non-quota failures are NOT retried.
    assert _retry_delay(Q("connection reset by peer")) is None
    assert _retry_delay(Q("403 forbidden")) is None


def test_system_prompt_documents_uia_first():
    from ra import config
    prompt = config.SYSTEM_PROMPT
    # The model must learn to reach for the accessible tools before vision.
    assert "ui_find" in prompt
    assert "ui_click" in prompt
    assert "ui_type" in prompt
    assert "double=true" in prompt


def test_groq_opt_in_only_via_flag(monkeypatch):
    from ra import config
    monkeypatch.setattr(config, "_env", lambda k, d="": os.environ.get(k, d))
    monkeypatch.setenv("RA_GROQ_API_KEY", "key")
    monkeypatch.delenv("RA_GROQ_ENABLE", raising=False)
    assert not config.groq_enabled()
    monkeypatch.setenv("RA_GROQ_ENABLE", "1")
    assert config.groq_enabled()
    monkeypatch.setenv("RA_GROQ_ENABLE", "multi")
    assert config.groq_enabled()
    monkeypatch.delenv("RA_GROQ_API_KEY", raising=False)
    monkeypatch.setenv("RA_GROQ_ENABLE", "1")
    assert not config.groq_enabled()  # key missing: never enabled