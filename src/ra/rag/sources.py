"""
Consent-gated data sources for Ra.

- screen:  capture the visible desktop, OCR it to *text only* - image pixels
           never leave the device, nothing is stored except indexed text.
- devices: system + audio device snapshot (CPU, memory, battery, disks, audio).
- files:   handled by the indexer (index_directory / index_file).

Heavy dependencies (Pillow, pytesseract, psutil, sounddevice) are imported
lazily, so the core RAG stack runs anywhere (constants like config are pure
stdlib).
"""
import os
import platform
import shutil

from ra import config


def _require_grant(area: str):
    if not config.GRANTED_ACCESS.get(area, False):
        raise PermissionError(
            f"Retrieval access to '{area}' is not granted by the user. "
            "Ask Ra to 'grant access to {0}' or run `python ra_cli.py rag grant {0}`."
        )


def capture_screen_text() -> str:
    """Capture the screen and return its OCR'd text (image pixels are discarded)."""
    _require_grant("screen")
    try:
        from PIL import ImageGrab
    except ImportError:
        raise RuntimeError("Pillow is not installed. Run: pip install pillow")
    try:
        import pytesseract
    except ImportError:
        raise RuntimeError(
            "pytesseract is not installed. Run: pip install pytesseract, and install "
            "the Tesseract OCR binary from https://github.com/tesseract-ocr/tesseract"
        )
    if shutil.which("tesseract") is None:
        raise RuntimeError(
            "The Tesseract OCR binary was not found on PATH. On Windows: winget install "
            "tesseract-ocr, or install from https://github.com/tesseract-ocr/tesseract"
        )
    image = ImageGrab.grab()
    text = pytesseract.image_to_string(image).strip()
    return text or "(no readable text found on screen)"


def collect_device_snapshot() -> str:
    """Return a text snapshot of system + device info for indexing."""
    _require_grant("devices")
    lines = [f"System: {platform.system()} {platform.release()} ({platform.machine()})"]
    try:
        import psutil
        lines.append(f"CPU usage: {psutil.cpu_percent(interval=0.1)}%")
        mem = psutil.virtual_memory()
        lines.append(f"Memory: {mem.used / 1e9:.1f} GB / {mem.total / 1e9:.1f} GB ({mem.percent}%)")
        disk = psutil.disk_usage(os.path.abspath(os.sep))
        lines.append(f"Disk C: {disk.free / 1e9:.1f} GB free of {disk.total / 1e9:.1f} GB ({disk.percent}%)")
        batt = psutil.sensors_battery()
        if batt is not None:
            state = "charging" if batt.power_plugged else "on battery"
            lines.append(f"Battery: {batt.percent}%, {state}")
    except ImportError:
        lines.append("[psutil not installed - run: pip install psutil (system details skipped)]")
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        names = [f"{i}: {d['name']}" for i, d in enumerate(devices)]
        lines.append(f"Audio devices: {', '.join(names)}")
    except ImportError:
        lines.append("[sounddevice not installed - run: pip install sounddevice (audio devices skipped)]")
    return "\n".join(lines)


def list_audio_devices() -> str:
    try:
        import sounddevice as sd
    except ImportError:
        return "sounddevice is not installed. Run: pip install sounddevice"
    devices = sd.query_devices()
    lines = [f"{i}: {d['name']} ({d['max_input_channels']} in / {d['max_output_channels']} out)" for i, d in enumerate(devices)]
    return "\n".join(lines) or "No audio devices found."


def ingest_screen(store, embedder) -> dict:
    text = capture_screen_text()
    store.add_chunks(
        [{
            "source_type": "screen",
            "source": "screen-capture",
            "path": "screen",
            "chunk_index": 0,
            "text": text,
        }],
        embedder,
    )
    return {"text": text, "result": "Screen text captured and indexed."}


def ingest_devices(store, embedder) -> dict:
    text = collect_device_snapshot()
    store.add_chunks(
        [{
            "source_type": "device",
            "source": "device-snapshot",
            "path": "device",
            "chunk_index": 0,
            "text": text,
        }],
        embedder,
    )
    return {"text": text, "result": "Device snapshot captured and indexed."}