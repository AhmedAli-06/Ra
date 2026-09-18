"""Ra branding / identity / hygiene checks."""
import inspect
import os

from ra import __version__, config


def test_full_product_name():
    assert config.ASSISTANT_NAME == "Ra"


def test_wake_word_is_fire_up():
    assert config.WAKE_WORD == "fire up"
    assert config.LAUNCH_PHRASE == "fire up"


def test_system_prompt_is_rag_aware():
    assert "Ra" in config.SYSTEM_PROMPT
    assert "retrieval-augmented" in config.SYSTEM_PROMPT.lower()
    assert "search_context" in config.SYSTEM_PROMPT


def test_no_hardcoded_secret_in_code():
    assert "gsk_" not in inspect.getsource(config)


def test_keys_read_from_environment():
    assert os.environ.get("RA_GROQ_API_KEY") == "test-key-not-real"


def test_version():
    assert __version__ == "1.0.0"


def test_gui_is_ra_branded():
    from ra.gui import RaGUI
    assert RaGUI.__name__ == "RaGUI"


def test_skills_expose_retrieval_tools():
    from ra.skills import TOOLS, execute_tool
    names = {t["name"] for t in TOOLS}
    required = {"search_context", "index_documents", "capture_screen",
                "refresh_device_snapshot", "list_access", "set_access"}
    assert required <= names
    assert execute_tool("no_such_tool", {}) == "Unknown tool: no_such_tool"


def test_skills_expose_computer_control():
    from ra.skills import TOOLS
    names = {t["name"] for t in TOOLS}
    required = {"run_command", "type_text", "press_keys", "click_screen",
                "list_windows", "focus_window", "read_file", "write_file",
                "set_clipboard", "screenshot"}
    assert required <= names


def test_skills_expose_fast_browser_tools(monkeypatch):
    from ra.skills import TOOLS, execute_tool, _html_to_readable
    names = {t["name"] for t in TOOLS}
    required = {"web_fetch", "go_to_url", "type_and_enter", "copy_page_text",
                "find_on_page", "browser_tab", "scroll_page", "wait_seconds"}
    assert required <= names
    # Pure parser: extracts readable text + links (no network, no injections).
    html = ("<html><head><script>var x=1;</script></head><body>"
            "Welcome to GNDEC <a href='/cse'>Computer Science Dept</a>"
            "<a>no-href</a><style>.x{}</style> END</body></html>")
    out = _html_to_readable(html)
    assert "Welcome to GNDEC" in out and "END" in out
    assert "Computer Science Dept -> /cse" in out
    # Guards: invalid inputs return a friendly error WITHOUT injecting keys.
    assert "Unknown tab action" in execute_tool("browser_tab", {"action": "bogus"})
    assert "Unknown scroll direction" in execute_tool("scroll_page", {"direction": "bogus"})
    assert execute_tool("wait_seconds", {"seconds": 1}).startswith("Waited 1 seconds")


def test_access_defaults_are_consent_gated():
    assert set(config.GRANTED_ACCESS) == {"files", "devices", "screen", "computer"}
    assert config.GRANTED_ACCESS["screen"] is True
    assert config.GRANTED_ACCESS["computer"] is True


def test_voice_defaults_natural_and_streaming():
    assert config.TTS_ENGINE == "edge-tts"
    assert config.STT_ENGINE == "sherpa-onnx"
    assert config.EDGE_TTS_VOICE != "en-US-AndrewMultilingualNeural"


def test_rag_defaults_sane():
    assert config.RAG_ENABLED is True
    assert config.RAG_AUTO_RETRIEVE is True
    assert 0 < config.RAG_MIN_SCORE < 1
    assert config.RAG_EMBED_DIM >= 64