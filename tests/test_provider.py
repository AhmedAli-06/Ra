"""Provider selection + client wiring tests (pure, offline — no network, no SDK)."""
import sys

import pytest

from ra import brain
from ra import config


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(brain, "_client", None)
    yield
    monkeypatch.setattr(brain, "_client", None)


def test_provider_explicit_override(monkeypatch):
    monkeypatch.delenv("RA_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("RA_GROQ_API_KEY", raising=False)
    monkeypatch.setenv("RA_LLM_PROVIDER", "gemini")
    assert config.get_provider() == "gemini"


def test_provider_infers_gemini_when_key_present(monkeypatch):
    monkeypatch.delenv("RA_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("RA_GEMINI_API_KEY", "AQ-test")
    monkeypatch.delenv("RA_GROQ_API_KEY", raising=False)
    assert config.get_provider() == "gemini"


def test_provider_unknown_raises(monkeypatch):
    monkeypatch.setenv("RA_LLM_PROVIDER", "claude")
    with pytest.raises(RuntimeError, match="Unknown RA_LLM_PROVIDER"):
        config.get_provider()


def test_llm_model_follows_provider(monkeypatch):
    monkeypatch.setenv("RA_LLM_PROVIDER", "gemini")
    assert config.get_llm_model() == config.GEMINI_MODEL
    monkeypatch.setenv("RA_LLM_MODEL", "custom/foo")
    assert config.get_llm_model() == "custom/foo"


def test_gemini_client_uses_openai_sdk(monkeypatch):
    monkeypatch.setenv("RA_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("RA_GEMINI_API_KEY", "AQ-test-key")

    captured = {}

    class FakeOpenAI:
        def __init__(self, **kw):
            captured.update(kw)

    import types as _types
    fake_mod = _types.ModuleType("openai")
    fake_mod.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_mod)
    monkeypatch.setitem(sys.modules, "groq", None)

    client = brain.get_client()
    assert "api_key" in captured and captured["api_key"] == "AQ-test-key"
    assert captured["base_url"] == config.GEMINI_BASE_URL
    assert client is not None


def test_gemini_missing_sdk_friendly_error(monkeypatch):
    monkeypatch.setenv("RA_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("RA_GEMINI_API_KEY", "AQ-test-key")
    import types as _types
    fake_mod = _types.ModuleType("openai")   # present but has NO OpenAI class
    monkeypatch.setitem(sys.modules, "openai", fake_mod)
    with pytest.raises(RuntimeError, match="'openai' package is not installed"):
        brain.get_client()


def test_groq_client_uses_groq_sdk(monkeypatch):
    monkeypatch.setenv("RA_LLM_PROVIDER", "groq")
    monkeypatch.setenv("RA_GROQ_API_KEY", "gsk-test")

    captured = {}

    class FakeGroq:
        def __init__(self, **kw):
            captured.update(kw)

    import types as _types
    fake_mod = _types.ModuleType("groq")
    fake_mod.Groq = FakeGroq
    monkeypatch.setitem(sys.modules, "groq", fake_mod)

    brain.get_client()
    assert captured["api_key"] == "gsk-test"