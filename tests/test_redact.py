"""Redaction (isair/jarvis sensitive-info masking) + transcription error paths."""
from ra import redact


def test_api_key_masked():
    out = redact.redact("my key is sk-abcdefghijklmnopqrstuvwxyz1234567890")
    assert "[REDACTED]" in out
    assert "sk-abcdefghijklmnopqrstuvwxyz1234567890" not in out


def test_gemini_key_masked():
    t = "key=AIzaSyFakeValueForPrivacy0123456789abcdefgh"
    out = redact.redact(t)
    assert "[REDACTED]" in out


def test_email_masked():
    out = redact.redact("email me at john.doe@example.com please")
    assert "john.doe@example.com" not in out
    assert "[REDACTED]" in out


def test_phone_masked():
    out = redact.redact("call +1-555-123-4567 today")
    assert "[REDACTED]" in out
    assert "555-123-4567" not in out


def test_normal_prose_untouched():
    t = "The quick brown fox jumps over the lazy dog in the park."
    assert redact.redact(t) == t


def test_contains_secret():
    assert redact.contains_secret("key sk-abcdefghijklmnopqrstuvwxyz1234567890")
    assert not redact.contains_secret("just some words")


def test_memory_masks_secrets(tmp_path, monkeypatch):
    from ra import memory
    monkeypatch.setattr(memory, "_MEMORY_FILE", str(tmp_path / "memory.jsonl"))
    memory.remember("backup password is sk-abcdefghijklmnopqrstuvwxyz1234567890",
                    source="test")
    entries = memory._read_all()
    assert entries
    stored = entries[0]["fact"]
    assert "sk-abcdefghijklmnopqrstuvwxyz1234567890" not in stored
    assert "[REDACTED]" in stored


def test_transcribe_missing_file():
    from ra import transcribe
    assert "not found" in transcribe.transcribe_file("C:/definitely/missing.mp3")