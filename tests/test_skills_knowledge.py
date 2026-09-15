"""Knowledge & utility skills: calculator, dictionary, translation, FX,
recycle bin, Wi-Fi, window control, installed apps. Network skills are
live-called only if the API is reachable; assertions tolerate offline runs."""
import pytest

from ra.skills import TOOLS, _DISPATCH, _spotify_sig_words, calculate, execute_tool


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------
def test_knowledge_tools_registered():
    names = {t["name"] for t in TOOLS}
    for tool in ("calculate", "define", "translate", "currency_convert",
                 "empty_recycle_bin", "wifi_password", "window_control",
                 "list_installed_apps"):
        assert tool in names, f"{tool} missing from TOOLS"
        assert tool in _DISPATCH, f"{tool} missing from _DISPATCH"


def test_tool_count_grew():
    # 82 pre-existing tools + 8 new knowledge/utility tools.
    assert len(TOOLS) >= 90


# ---------------------------------------------------------------------------
# calculate (pure, offline)
# ---------------------------------------------------------------------------
def test_calculate_basic_arithmetic():
    assert calculate("17*23") == "17*23 = 391"
    assert calculate("2**10") == "2**10 = 1024"
    assert calculate("sqrt(144)+1") == "sqrt(144)+1 = 13"


def test_calculate_percent_phrasing():
    assert calculate("35% of 240") == "84"
    assert calculate("20 percent of 50") == "10"


def test_calculate_rejects_code_injection():
    out = calculate("__import__('os').system('echo hi')")
    assert "Couldn't calculate" in out


def test_dispatch_calculate():
    assert execute_tool("calculate", {"expression": "6*7"}) == "6*7 = 42"


# ---------------------------------------------------------------------------
# _shell_text strips run_shell's 'exit N: ' status prefix (parsers depend on it)
# ---------------------------------------------------------------------------
def test_shell_text_strips_exit_prefix(monkeypatch):
    import ra.skills as sk
    monkeypatch.setattr(sk._computer, "run_shell",
                        lambda cmd, timeout=30.0: "exit 0: SSID=MyNet|KEY=hunter2")
    out = sk._shell_text("whatever")
    assert out == "SSID=MyNet|KEY=hunter2"


def test_wifi_password_parses_with_prefix(monkeypatch):
    import ra.skills as sk
    monkeypatch.setattr(sk._computer, "run_shell",
                        lambda cmd, timeout=30.0: "exit 0: SSID=HomeNet|KEY=pass123")
    out = sk.wifi_password()
    assert "HomeNet" in out and "pass123" in out


# ---------------------------------------------------------------------------
# Spotify search-word extraction (drives the end-to-end play flow)
# ---------------------------------------------------------------------------
def test_sig_words_drop_stopwords():
    assert _spotify_sig_words("play loser by tame impala in spotify") == \
        ["loser", "tame", "impala"]
    assert _spotify_sig_words("play the song lose yourself") == ["lose", "yourself"]
    assert _spotify_sig_words("") == []


# ---------------------------------------------------------------------------
# define / translate / currency (live APIs; graceful offline)
# ---------------------------------------------------------------------------
def test_define_word_live_or_graceful():
    out = execute_tool("define", {"word": "serendipity"})
    assert isinstance(out, str) and out
    assert ("serendipity" in out.lower()) or ("failed" in out.lower())


def test_translate_live_or_graceful():
    out = execute_tool("translate", {"text": "good morning", "target": "fr"})
    assert isinstance(out, str) and out
    assert ("bonjour" in out.lower()) or ("Translation failed" in out or "Translation" in out)


def test_currency_convert_live_or_graceful():
    out = execute_tool("currency_convert",
                       {"amount": 100, "from_cur": "USD", "to_cur": "EUR"})
    assert isinstance(out, str) and out
    assert ("USD" in out and "EUR" in out) or "failed" in out.lower()
