"""
Ra - your personal Retrieval-Augmented Virtual (Voice) Assistant.

Ra is the single unified assistant: a voice/text HUD, a screen- and
file-aware retrieval index, device awareness, and an LLM brain (Groq) that
calls skills to actually do things on your PC.

Run it with:  python run_app.py
CLI tools:    python ra_cli.py rag ...
"""
import os
import sys

__version__ = "1.0.0"


def ensure_utf8_console():
    """Windows consoles often use a legacy encoding (cp1252) that cannot print
    indexed document content. Force UTF-8 with lossy fallback so retrieved text
    (em dashes, arrows, non-Latin scripts) never crashes the CLI/assistant.

    In a bundled windowed app (PyInstaller) `sys.stdout`/`sys.stderr` are None,
    so plain print() calls would crash GUI threads; reroute them to
    ~/.ra/ra.log instead so the debug lines are still captured."""
    import io
    data_dir = os.path.join(os.path.expanduser("~"), ".ra")
    try:
        from ra.config import DATA_DIR as _data_dir
        data_dir = _data_dir
    except Exception:
        pass
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            try:
                f = open(os.path.join(data_dir, "ra.log"), "a", encoding="utf-8", errors="replace")
            except Exception:
                f = io.StringIO()
            setattr(sys, name, f)
        else:
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass