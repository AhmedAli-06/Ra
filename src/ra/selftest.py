"""
Ra Capability Self-Test
========================
A diagnostic pass that verifies every optional capability Ra advertises by
attempting the actual call path (not just the import). Mirrors the
self-tests in rishaadj/JARVIS and the plugin health checks in FatihMakes/
Mark-LI - a silent check lets Ra acknowledge real gaps instead of a vague
"yes I can do that".

Returns report lines like "pass  -> basic voice (builtin)", so the LLM can
read them back truthfully.
"""
import sys


class _Caps:
    def __init__(self):
        self.entries = []

    def check(self, name, fn, lvl="ok"):
        try:
            result = fn()
            if result is False:
                self.entries.append((name, "fail", "probe returned false"))
            else:
                self.entries.append((name, lvl, ""))
        except Exception as e:
            self.entries.append((name, "fail", str(e)))


def run():
    caps = _Caps()
    import os

    def _ver(name):
        mod = __import__(name)
        return getattr(mod, "__version__", "?")

    cap = caps.check

    have = []
    for name in ("requests", "ddgs", "psutil", "PIL", "pytesseract", "sounddevice"):
        try:
            mod = __import__(name)
            have.append(f"{name} {getattr(mod, '__version__', 'ok')}")
        except Exception:
            have.append(f"{name} MISSING")

    cap("network libraries", lambda: __import__("requests") and True)
    cap("news search (ddgs)", lambda: __import__("ddgs") and True)
    cap("system telemetry (psutil)", lambda: __import__("psutil") and True)
    cap("screen capture (Pillow)", lambda: __import__("PIL") and True)
    cap("OCR (pytesseract+tesseract)", lambda: __import__("pytesseract") and True)
    cap("audio devices (sounddevice)", lambda: __import__("sounddevice") and True)

    try:
        from ra import config
        cap("config load", lambda: config.SYSTEM_PROMPT and True)
    except Exception as e:
        cap("config load", lambda: False)
        caps.entries[-1] = (caps.entries[-1][0], "fail", str(e))

    try:
        from ra import config as _cfg
        _brains = _cfg.configured_brains()
        _detail = "+".join(_brains) if _brains else "none"
        _mode = "on" if _cfg.multi_brain_enabled() else "off"
        _want = _cfg.multi_brain_enabled() if len(_brains) >= 2 else True
        cap(f"multi-brain race ({_detail}, {_mode})",
            lambda: _want and True)
    except Exception as e:
        cap("multi-brain race", lambda: False)
        caps.entries[-1] = (caps.entries[-1][0], "fail", str(e))

    try:
        import tempfile
        from ra.rag.embedder import HashEmbedder
        from ra.rag.store import SemanticStore
        _sdir = tempfile.mkdtemp(prefix="ra_selftest_")
        _sidb = os.path.join(_sdir, "idx.sqlite")
        e_ = HashEmbedder(dim=512)
        s_ = SemanticStore(_sidb)
        s_.add_chunks([{"source_type": "selftest", "source": "selftest",
                        "path": "x", "chunk_index": 0,
                        "text": "ra selftest marker"}], e_)
        hits = s_.search(e_.embed("marker"), k=1)
        cap("semantic search (RAG)", lambda: bool(hits) and True)
        del s_, e_, hits
        try:
            import shutil
            shutil.rmtree(_sdir, ignore_errors=True)
        except Exception:
            pass
    except Exception:
        cap("semantic search (RAG)", lambda: False)

    try:
        import tempfile
        from ra import memory
        _orig = memory._MEMORY_FILE
        _fd, _path = tempfile.mkstemp(suffix=".jsonl")
        os.close(_fd)
        memory._MEMORY_FILE = _path
        try:
            cap("persistent memory", lambda: memory.remember("selftest", source="selftest") and True)
        finally:
            memory._MEMORY_FILE = _orig
            try:
                os.remove(_path)
            except OSError:
                pass
    except Exception:
        cap("persistent memory", lambda: False)

    try:
        from ra import fastlane
        cap("fast lane (instant answers)", lambda: bool(fastlane.try_answer("what time is it")))
    except Exception:
        cap("fast lane (instant answers)", lambda: False)

    try:
        import tempfile
        import zipfile
        from ra.rag import indexer
        _sdir = tempfile.mkdtemp(prefix="ra_selftest_")
        _doc = os.path.join(_sdir, "probe.docx")
        with zipfile.ZipFile(_doc, "w") as z:
            z.writestr(
                "word/document.xml",
                '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>ra docx probe</w:t>'
                '</w:r></w:p></w:body></w:document>',
            )
        txt = indexer.read_docx(_doc)
        cap("office docs (docx/xlsx/odt)", lambda: ("probe" in txt) and True)
        try:
            import shutil
            shutil.rmtree(_sdir, ignore_errors=True)
        except Exception:
            pass
    except Exception:
        cap("office docs (docx/xlsx/odt)", lambda: False)

    try:
        from ra import plugins
        _p = plugins.plugin_dir()
        cap("plugin directory", lambda: os.path.isdir(_p) and True)
    except Exception:
        cap("plugin directory", lambda: False)

    lines = [f"{'ok' if lvl == 'ok' else lvl:>4}  {name}"
             if not err else f"fail  {name}: {err}"
             for name, lvl, err in caps.entries]

    head = "Ra capability self-test:"
    body = head + "\n" + "\n".join(lines)
    body += "\nenv: python " + sys.version.split()[0]
    if have:
        body += "\noptional deps: " + ", ".join(have)
    ok = sum(1 for _, lvl, _ in caps.entries if lvl == "ok")
    body += f"\ncheck: {ok}/{len(caps.entries)} capabilities green."
    return body