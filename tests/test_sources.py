import pytest

from ra import config
from ra.rag import sources
from ra.rag.embedder import HashEmbedder


def test_screen_requires_grant(monkeypatch):
    monkeypatch.setitem(config.GRANTED_ACCESS, "screen", False)
    with pytest.raises(PermissionError, match="not granted"):
        sources.capture_screen_text()


def test_screen_without_pillow(monkeypatch):
    monkeypatch.setitem(config.GRANTED_ACCESS, "screen", True)
    real_import = __import__

    def fake(name, *a, **k):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("No module named 'PIL'")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake)
    with pytest.raises(RuntimeError, match="Pillow"):
        sources.capture_screen_text()


def test_screen_without_tesseract(monkeypatch):
    monkeypatch.setitem(config.GRANTED_ACCESS, "screen", True)
    real_import = __import__

    def fake(name, *a, **k):
        if name == "pytesseract":
            raise ImportError("No module named 'pytesseract'")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake)
    with pytest.raises(RuntimeError, match="pytesseract"):
        sources.capture_screen_text()


def test_device_snapshot_graceful_without_deps(monkeypatch):
    monkeypatch.setitem(config.GRANTED_ACCESS, "devices", True)
    real_import = __import__

    def fake(name, *a, **k):
        if name in ("psutil", "sounddevice"):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake)
    text = sources.collect_device_snapshot()
    assert "System:" in text
    assert "psutil" in text
    assert "sounddevice" in text


def test_devices_require_grant(monkeypatch):
    monkeypatch.setitem(config.GRANTED_ACCESS, "devices", False)
    with pytest.raises(PermissionError, match="not granted"):
        sources.collect_device_snapshot()


def test_ingest_devices_indexes_snapshot(monkeypatch, store):
    monkeypatch.setitem(config.GRANTED_ACCESS, "devices", True)
    real_import = __import__

    def fake(name, *a, **k):
        if name in ("psutil", "sounddevice"):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake)
    out = sources.ingest_devices(store, HashEmbedder(dim=128))
    assert store.stats()["total_chunks"] == 1
    assert out["result"].startswith("Device snapshot")


def test_list_audio_devices_graceful(monkeypatch):
    real_import = __import__

    def fake(name, *a, **k):
        if name == "sounddevice":
            raise ImportError("No module named 'sounddevice'")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake)
    assert "sounddevice" in sources.list_audio_devices()