"""
Ra Voice Engine
===================
Text-to-speech (edge-tts by default - crisp, natural neural voices) and
streaming speech-to-text.

STT is the fast path: a single background microphone session feeds a Vosk
streaming recognizer, which emits **partials** (so the HUD shows what it hears
live) and **phrase events** the moment a complete phrase is spoken. That is
what makes hearing feel instant, and it makes barge-in easy: whatever is heard
while a reply is being generated is simply queued by the assistant.

Falls back to faster-whisper batch transcription for push-to-talk if Vosk is
not installed or its model cannot be downloaded.
"""
import ctypes
import collections
import os
import queue
import re
import threading
import time
import zipfile

import numpy as np
import sounddevice as sd

from ra import config
from ra import logging as ralog

_spk_lock = threading.Lock()
_mic_lock = threading.Lock()
_mic = None

# Cached result of mic auto-selection (module load is once-per-process).
_mic_device_cache = {"index": None, "name": None}
_stt_debug_fh = None
_STT_DEBUG_LOCK = threading.Lock()


def _stt_debug(msg: str):
    """Append one line to the RA_STT_DEBUG log (default ~/.ra/ra_stt_debug.log).
    The in-process ralog bus is memory-only and the HUD never writes ra.log to
    disk, so this file is the only way to see what the DEPLOYED EXE actually
    hears. Keeping the handle open makes ~5 lines/sec cheap."""
    global _stt_debug_fh
    d = getattr(config, "STT_DEBUG", "")
    if not d:
        return
    try:
        if _stt_debug_fh is None:
            path = d if d.lower().endswith(".log") else os.path.join(
                os.path.expanduser("~"), ".ra", "ra_stt_debug.log")
            _stt_debug_fh = open(path, "a", encoding="utf-8")
        with _STT_DEBUG_LOCK:
            _stt_debug_fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
            _stt_debug_fh.flush()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Text-to-speech
# ---------------------------------------------------------------------------
# playsound3 gives us the live Sound handle when block=False, so a barge-in /
# "Ra, stop" can kill playback MID-audio (the old code used Playsound.stop_all,
# which does not exist - stops were silently ignored and Ra kept talking).
_stop_requested = threading.Event()
_sounds_lock = threading.Lock()
_current_sound = None         # playsound3 Sound currently playing (kill target)


def clear_speech_stop():
    """Allow the NEXT utterance to start fresh (call before speaking a reply)."""
    _stop_requested.clear()


def stop_speaking():
    """Instantly stop whatever TTS is playing - mid-sentence, not just between
    sentences. Safe to call at any time and from any thread."""
    _stop_requested.set()
    with _sounds_lock:
        snd = _current_sound
    if snd is not None:
        try:
            snd.stop()
        except Exception:
            pass


# -- "read aloud" cleanup --------------------------------------------------
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_SYM_RE = re.compile(r"[*_~`#|>{}\\]")
_WS_RE = re.compile(r"[ \t]+")
_ECHO_LIST_RE = re.compile(r"(?m)^\s*[-•●◦▪◆◇]\s+")
_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+|www\.\S+")
_DASH_RE = re.compile(r"\s+[-–—]\s+")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE00-\uFE0F]"
)


def _clean_for_speech(text: str) -> str:
    """Strip markdown / symbols that neural TTS would otherwise read aloud
    literally ('asterisk asterisk ...', 'At thump', code fences) and flatten
    paragraph breaks so Ra does not stall on blank lines."""
    t = text or ""
    t = re.sub(r"```[a-z0-9]*\s*|```", " ", t)      # code fences
    t = _MD_LINK_RE.sub(r"\1", t)                    # [label](url) -> label
    t = _MD_SYM_RE.sub(" ", t)                       # * _ ~ ` # | > { } \\
    t = _ECHO_LIST_RE.sub(" ", t)                    # '- ', '• ' bullets
    t = _URL_RE.sub(" ", t)                          # bare URLs -> silence.
    t = _DASH_RE.sub(" ", t)                         # ' - ' separators
    t = _EMOJI_RE.sub(" ", t)                        # emoji / dingbats
    t = t.replace("->", " to ").replace("→", " to ") # arrows read naturally
    t = t.replace("—", ", ").replace("–", ", ")      # dashes -> comma pause
    t = t.replace("&", " and ").replace("+", " plus ").replace("=", " equals ")
    t = re.sub(r"\s*\n+\s*", " ", t)                 # collapses paragraphs
    for ch in ('"', "\u201c", "\u201d", "\u2018", "\u2019", "'''", "```"):
        t = t.replace(ch, "")
    t = _WS_RE.sub(" ", t).strip(" ,;:!?-")
    return t


# -- playback --------------------------------------------------------------
def _play_file(path: str):
    """Block until `path` finishes - but poll a stop flag so stop_speaking()
    kills it immediately instead of playing to the end."""
    from playsound3 import playsound
    if _stop_requested.is_set():
        return
    s = playsound(path, block=False)
    with _sounds_lock:
        _current_sound = s
    if _stop_requested.is_set():
        try:
            s.stop()
        except Exception:
            pass
        with _sounds_lock:
            _current_sound = None
        return
    try:
        while s.is_alive():
            if _stop_requested.is_set():
                try:
                    s.stop()
                except Exception:
                    pass
                break
            time.sleep(0.02)
    finally:
        with _sounds_lock:
            if _current_sound is s:
                _current_sound = None


def _tts_path(text: str):
    """Generate edge-tts audio for `text` into a temp mp3; returns the path
    (or None if a stop/barge-in raced in before generation finished)."""
    if _stop_requested.is_set():
        return None
    import asyncio
    import tempfile
    from edge_tts import Communicate

    async def _gen(p):
        communicate = Communicate(
            text,
            config.EDGE_TTS_VOICE,
            rate=getattr(config, "EDGE_TTS_RATE", "+0%"),
            pitch=getattr(config, "EDGE_TTS_PITCH", "+0Hz"),
        )
        await communicate.save(p)

    f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    path = f.name
    f.close()
    try:
        asyncio.run(_gen(path))
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass
        raise
    if _stop_requested.is_set():
        try:
            os.remove(path)
        except Exception:
            pass
        return None
    return path


def _speak_pyttsx3(text: str):
    import pyttsx3
    engine = pyttsx3.init()
    engine.setProperty("rate", config.TTS_RATE)
    engine.setProperty("volume", config.TTS_VOLUME)
    engine.say(text)
    engine.runAndWait()
    engine.stop()


def _speak_clean_path(path: str):
    try:
        _play_file(path)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def speak(text: str):
    """Speak `text` through the default Windows speaker/output device. Blocks
    until the audio finishes OR a stop/barge-in cuts it off."""
    text = _clean_for_speech(text)
    if not text:
        return
    with _spk_lock:
        _stop_requested.clear()
        print(f"\033[96m{config.ASSISTANT_NAME}:\033[0m {text}")
        ralog.log("voice", f"Ra: {text}")
        try:
            if config.TTS_ENGINE == "edge-tts":
                path = _tts_path(text)
                if path is not None:
                    _speak_clean_path(path)
            else:
                _speak_pyttsx3(text)
        except Exception as e:
            ralog.log("warn", f"speech output failed: {e}")


# ---------------------------------------------------------------------------
# Sentence-streamed speech (Ra starts talking before the reply finishes)
# ---------------------------------------------------------------------------
_SENTENCE_RE = re.compile(r"(?<=[.!?\u2026])\s+")
_CHUNK_TARGET = 340    # chars per generated audio segment (fewer = no gaps)


def split_sentences(text: str):
    """Split text into its individual sentences. Speech chunking itself is done
    by _chunk_for_speech (merge-into-larger-chunks), this stays as a plain
    sentence splitter for callers that need exact sentences."""
    return [p.strip() for p in _SENTENCE_RE.split(text or "") if p.strip()]


def _chunk_for_speech(clean_text: str, target: int = _CHUNK_TARGET):
    """Group CLEAN text into smoothly-spoken audio chunks of up to `target`
    chars. Sentences are merged greedily, so a whole normal reply becomes just
    a handful of edge-tts calls (a few network round-trips instead of many),
    while sentence boundaries are preserved so barge-in stays razor sharp."""
    sentences = split_sentences(clean_text)
    out = []
    buf = ""
    for s in sentences:
        if len(s) > target:
            if buf:
                out.append(buf)
                buf = ""
            acc = ""
            for piece in re.split(r"(?<=[,;]) ", s):
                if not piece:
                    continue
                if not acc or len(acc) + len(piece) + 1 <= target:
                    acc = (acc + " " + piece).strip()
                else:
                    out.append(acc)
                    acc = piece
            if acc:
                buf = acc
            continue
        if not buf or len(buf) + len(s) + 1 <= target:
            buf = (buf + " " + s).strip()
        else:
            out.append(buf)
            buf = s
    if buf:
        out.append(buf)
    return out


def _safe_remove(path):
    try:
        if path:
            os.remove(path)
    except Exception:
        pass


def _speak_chunks(chunks, cancel_event=None):
    """Play provided audio chunks with a 2-deep prefetch so generation latency
    disappears between segments (only the very first call costs upfront)."""
    paths_q: queue.Queue = queue.Queue(maxsize=2)

    def _produce():
        try:
            for chunk in chunks:
                if _stop_requested.is_set():
                    return
                try:
                    path = _tts_path(chunk)
                except Exception:
                    path = None
                if path and not _stop_requested.is_set():
                    try:
                        paths_q.put(path, timeout=1.0)
                        continue
                    except queue.Full:
                        pass
                _safe_remove(path)
                if _stop_requested.is_set():
                    return
            try:
                paths_q.put(None, timeout=1.0)
            except queue.Full:
                pass
        finally:
            try:
                paths_q.put(None, timeout=1.0)
            except queue.Full:
                pass

    threading.Thread(target=_produce, daemon=True,
                     name="ra-tts-prefetch").start()
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _stop_requested.set()
                break
            try:
                path = paths_q.get(timeout=0.1)
            except queue.Empty:
                if _stop_requested.is_set():
                    break
                continue
            if path is None:
                break
            if _stop_requested.is_set() or (
                    cancel_event is not None and cancel_event.is_set()):
                _safe_remove(path)
                break
            _speak_clean_path(path)
    finally:
        while True:
            try:
                leftover = paths_q.get_nowait()
            except queue.Empty:
                break
            if leftover:
                _safe_remove(leftover)


def speak_sentences(text: str, cancel_event=None):
    """Speak a complete reply fluidly: clean it for speech (no 'asterisk'
    markdown read-aloud), merge it into a few larger chunks, and play them
    with a prefetch so there is no dead air between sentences."""
    clean = _clean_for_speech(text)
    chunks = [c for c in _chunk_for_speech(clean) if c]
    if not chunks:
        return
    if cancel_event is not None and cancel_event.is_set():
        return
    print(f"\033[96m{config.ASSISTANT_NAME}:\033[0m {clean}")
    ralog.log("voice", f"Ra: {clean}")
    _speak_chunks(chunks, cancel_event)


# ---------------------------------------------------------------------------
# Incremental streaming speech
# ---------------------------------------------------------------------------
class _StreamSpeech:
    """Incremental TTS: text keeps arriving while the earlier audio plays, and
    everything is merged into a handful of cleaned audio chunks with a 2-deep
    prefetch - so a streamed reply has the fastest possible start and zero
    stops between sentences, commas, spaces or paragraph changes."""

    def __init__(self, cancel_event=None):
        self._cancel = cancel_event
        self._cv = threading.Condition()
        self._acc = ""
        self._closed = False
        self._chunks = []
        self._pending = collections.deque()
        self._done = threading.Event()
        threading.Thread(target=self._generate, daemon=True,
                         name="ra-tts-gen").start()
        threading.Thread(target=self._consume, daemon=True,
                         name="ra-tts-play").start()

    # -- public API -------------------------------------------------------
    def push(self, text: str):
        with self._cv:
            self._acc += text or ""
            self._cv.notify_all()

    def close(self):
        """No more text will arrive; block until everything has been spoken
        (or the reply is aborted and cleaned up)."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        self._done.wait(timeout=30)
        with self._cv:
            leftover = list(self._pending)
            self._pending.clear()
            self._cv.notify_all()
        for p in leftover:
            _safe_remove(p)

    def said(self) -> str:
        with self._cv:
            return _clean_for_speech(self._acc)

    # -- internals --------------------------------------------------------
    def _aborted(self) -> bool:
        return _stop_requested.is_set() or (
            self._cancel is not None and self._cancel.is_set())

    def _generate(self):
        emitted = 0
        try:
            while True:
                if self._aborted():
                    return
                with self._cv:
                    self._chunks = _chunk_for_speech(
                        _clean_for_speech(self._acc))
                chunks = self._chunks
                while emitted < len(chunks):
                    if self._aborted():
                        return
                    try:
                        path = _tts_path(chunks[emitted])
                    except Exception:
                        path = None
                    emitted += 1
                    if path is None:
                        continue
                    while True:
                        if self._aborted():
                            _safe_remove(path)
                            return
                        with self._cv:
                            if len(self._pending) < 2:
                                self._pending.append(path)
                                self._cv.notify_all()
                                break
                        self._cv.wait(0.2)
                if self._aborted():
                    return
                with self._cv:
                    if self._closed and emitted >= len(self._chunks):
                        return
                    self._cv.wait(0.5)
        finally:
            with self._cv:
                if not self._done.is_set():
                    self._pending.append(None)
                    self._cv.notify_all()

    def _consume(self):
        try:
            while True:
                if self._aborted():
                    break
                with self._cv:
                    while not self._pending and not self._done.is_set():
                        if self._aborted():
                            break
                        self._cv.wait(0.15)
                    if self._done.is_set() and not self._pending:
                        break
                    if not self._pending:
                        continue
                    path = self._pending.popleft()
                    self._cv.notify_all()
                if path is None:
                    break
                _speak_clean_path(path)
        finally:
            self._done.set()
            with self._cv:
                self._cv.notify_all()


def speak_stream(cancel_event=None):
    """Open an incremental TTS session. Feed it with `.push(text)` as the
    reply streams in and finish with `.close()`. The generator merges all
    incoming text into a handful of cleaned audio chunks with a 2-deep
    prefetch, so there is no dead air across sentence / comma / paragraph
    boundaries. `.said()` returns the cleaned transcript for the log."""
    return _StreamSpeech(cancel_event)


# ---------------------------------------------------------------------------
# Speech-to-text
# ---------------------------------------------------------------------------
_SAMPLE_RATE = 16000
_CHANNELS = 1
_vosk_model = None
_vosk_init_lock = threading.Lock()
_whisper_model = None
whisper_available = False          # True once the whisper model has loaded
_sherpa_recognizer = None
_sherpa_offline_recognizer = None
_sherpa_init_lock = threading.Lock()


def _download_vosk_model() -> str:
    """Download (or reuse) the Vosk STT model into ~/.ra/models."""
    import requests

    model_zip = os.path.join(config.VOSK_MODEL_DIR, config.VOSK_MODEL_ID + ".zip")
    model_path = os.path.join(config.VOSK_MODEL_DIR, config.VOSK_MODEL_ID)
    os.makedirs(config.VOSK_MODEL_DIR, exist_ok=True)
    if os.path.isdir(model_path):
        return model_path
    if os.path.exists(model_zip):
        try:
            with zipfile.ZipFile(model_zip) as check:
                if check.testzip() is None:
                    return model_zip
        except zipfile.BadZipFile:
            pass
        ralog.log("warn", "Incomplete Vosk model zip found - re-downloading.")
        os.remove(model_zip)
    part = model_zip + ".part"
    ralog.log("voice", f"Downloading Vosk model ({config.VOSK_MODEL_ID})...")
    with requests.get(config.VOSK_MODEL_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        got = 0
        with open(part, "wb") as f:
            for chunk in r.iter_content(chunk_size=262144):
                f.write(chunk)
                got += len(chunk)
                if total:
                    percent = got / total
                    ralog.log("voice", f"download {percent:.0%}")
    os.replace(part, model_zip)
    with zipfile.ZipFile(model_zip) as z:
        z.extractall(config.VOSK_MODEL_DIR)
    ralog.log("ok", "Vosk model ready.")
    return model_path


def _get_vosk():
    global _vosk_model
    if _vosk_model is None:
        with _vosk_init_lock:
            if _vosk_model is None:
                try:
                    import vosk  # noqa: F401
                except ImportError:
                    raise RuntimeError(
                        "Vosk STT is not installed. Run `pip install vosk` or set "
                        "RA_STT_ENGINE=whisper.",
                    )
                _vosk_model = vosk.Model(_download_vosk_model())
    return _vosk_model


def _download_sherpa_model() -> str:
    """Download (or reuse) the sherpa-onnx streaming fast-conformer bundle into
    ~/.ra/models. The asset is a .tar.bz2 bundle that extracts to a folder
    named after SHERPA_ONNX_MODEL_ID."""
    import tarfile

    tarball = os.path.join(config.SHERPA_ONNX_MODEL_DIR,
                           config.SHERPA_ONNX_MODEL_ID + ".tar.bz2")
    model_path = os.path.join(config.SHERPA_ONNX_MODEL_DIR,
                              config.SHERPA_ONNX_MODEL_ID)
    os.makedirs(config.SHERPA_ONNX_MODEL_DIR, exist_ok=True)
    if os.path.isdir(model_path):
        return model_path
    if os.path.exists(tarball):
        os.remove(tarball)
        ralog.log("warn", "Incomplete sherpa streaming model tarball removed - "
                          "re-downloading.")
    part = tarball + ".part"
    ralog.log("voice", f"Downloading sherpa-onnx streaming model "
                       f"({config.SHERPA_ONNX_MODEL_ID})...")
    import requests
    with requests.get(config.SHERPA_ONNX_MODEL_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        got = 0
        with open(part, "wb") as f:
            for chunk in r.iter_content(chunk_size=262144):
                f.write(chunk)
                got += len(chunk)
                if total:
                    percent = got / total
                    ralog.log("voice", f"sherpa download {percent:.0%}")
    os.replace(part, tarball)
    _extract_sherpa_tar(tarball)
    ralog.log("ok", "Sherpa-onnx streaming model ready.")
    return model_path


def _download_sherpa_offline_model() -> str:
    """Download (or reuse) the sherpa-onnx offline parakeet bundle into
    ~/.ra/models. Used for the refined final pass of the hybrid STT."""
    tarball = os.path.join(config.SHERPA_OFFLINE_MODEL_DIR,
                           config.SHERPA_OFFLINE_MODEL_ID + ".tar.bz2")
    model_path = os.path.join(config.SHERPA_OFFLINE_MODEL_DIR,
                              config.SHERPA_OFFLINE_MODEL_ID)
    os.makedirs(config.SHERPA_OFFLINE_MODEL_DIR, exist_ok=True)
    if os.path.isdir(model_path):
        return model_path
    if os.path.exists(tarball):
        os.remove(tarball)
        ralog.log("warn", "Incomplete sherpa offline model tarball removed - "
                          "re-downloading.")
    part = tarball + ".part"
    ralog.log("voice", f"Downloading sherpa-onnx offline model "
                       f"({config.SHERPA_OFFLINE_MODEL_ID})...")
    import requests
    with requests.get(config.SHERPA_OFFLINE_MODEL_URL, stream=True,
                      timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        got = 0
        with open(part, "wb") as f:
            for chunk in r.iter_content(chunk_size=262144):
                f.write(chunk)
                got += len(chunk)
                if total:
                    percent = got / total
                    ralog.log("voice", f"sherpa offline download {percent:.0%}")
    os.replace(part, tarball)
    _extract_sherpa_tar(tarball)
    ralog.log("ok", "Sherpa-onnx offline model ready.")
    return model_path


def _extract_sherpa_tar(tarball: str):
    """Extract a sherpa .tar.bz2 bundle into config.SHERPA_ONNX_MODEL_DIR."""
    import tarfile
    with tarfile.open(tarball, "r:bz2") as tf:
        tf.extractall(config.SHERPA_ONNX_MODEL_DIR)


def _get_sherpa() -> "sherpa_onnx.OnlineRecognizer":
    """Lazy-load the sherpa-onnx streaming recognizer
    (fast-conformer-transducer-en-480ms-int8). Raises RuntimeError with a clear
    message if the package is missing or the model cannot be obtained."""
    global _sherpa_recognizer
    if _sherpa_recognizer is None:
        with _sherpa_init_lock:
            if _sherpa_recognizer is None:
                try:
                    import sherpa_onnx  # noqa: F401
                except ImportError:
                    raise RuntimeError(
                        "sherpa-onnx STT is not installed. Run "
                        "`pip install sherpa-onnx` or set RA_STT_ENGINE=vosk.",
                    )
                model_path = _download_sherpa_model()
                import os as _os
                from sherpa_onnx import OnlineRecognizer
                tokens = _os.path.join(model_path, "tokens.txt")
                encoder = _os.path.join(model_path,
                                        "encoder.int8.onnx")
                decoder = _os.path.join(model_path, "decoder.int8.onnx")
                joiner = _os.path.join(model_path, "joiner.int8.onnx")
                _sherpa_recognizer = OnlineRecognizer.from_transducer(
                    tokens=tokens,
                    encoder=encoder,
                    decoder=decoder,
                    joiner=joiner,
                    num_threads=2,
                    sample_rate=_SAMPLE_RATE,
                    feature_dim=80,
                    enable_endpoint_detection=True,
                    rule1_min_trailing_silence=2.4,
                    rule2_min_trailing_silence=1.2,
                    rule3_min_utterance_length=20.0,
                )
    return _sherpa_recognizer


def _get_sherpa_offline() -> "sherpa_onnx.OfflineRecognizer":
    """Lazy-load the sherpa-onnx offline recognizer (parakeet-tdt-0.6b-v2-int8)
    used to refine finalized phrases in the hybrid STT pipeline. Raises
    RuntimeError with a clear message if the package is missing or the model
    cannot be obtained."""
    global _sherpa_offline_recognizer
    if _sherpa_offline_recognizer is None:
        with _sherpa_init_lock:
            if _sherpa_offline_recognizer is None:
                try:
                    import sherpa_onnx  # noqa: F401
                except ImportError:
                    raise RuntimeError(
                        "sherpa-onnx STT is not installed. Run "
                        "`pip install sherpa-onnx` or set RA_STT_ENGINE=vosk.",
                    )
                model_path = _download_sherpa_offline_model()
                import os as _os
                from sherpa_onnx import OfflineRecognizer
                tokens = _os.path.join(model_path, "tokens.txt")
                encoder = _os.path.join(model_path,
                                        "encoder.int8.onnx")
                decoder = _os.path.join(model_path, "decoder.int8.onnx")
                joiner = _os.path.join(model_path, "joiner.int8.onnx")
                _sherpa_offline_recognizer = OfflineRecognizer.from_transducer(
                    tokens=tokens,
                    encoder=encoder,
                    decoder=decoder,
                    joiner=joiner,
                    num_threads=2,
                    sample_rate=_SAMPLE_RATE,
                    feature_dim=128,
                    model_type="nemo_transducer",
                )
    return _sherpa_offline_recognizer


def _get_whisper_model():
    global _whisper_model, whisper_available
    if _whisper_model is None:
        model_size = getattr(config, "WHISPER_MODEL_SIZE", "base")
        ralog.log("voice", f"Loading Whisper '{model_size}' model (first run downloads it)...")
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(model_size, device="cpu", compute_type="int8")
        whisper_available = True
    return _whisper_model


# ---------------------------------------------------------------------------
# Capture backends
# ---------------------------------------------------------------------------
# sounddevice (PortAudio) is the preferred, portable path, but its bundled
# Windows binary can fail to open ANY capture device - even perfectly healthy
# ones - on some machines (fresh Win11 installs, virtual/redirected audio
# stacks). The plain WaveIn (winmm.dll) API that Windows itself uses keeps
# working there, so we fall back to it automatically. Either backend delivers
# the same thing: 0.2s mono int16 blocks at 16 kHz via next_block().
_BACKEND_ORDER = {
    "auto": ("sounddevice", "winmm"),
    "sounddevice": ("sounddevice",),
    "winmm": ("winmm",),
}


class _SDCapture:
    """sounddevice/PortAudio capture: the audio callback pushes 0.2s int16
    blocks to a queue (never doing STT work inside the callback)."""

    def __init__(self, device=None):
        self._q = queue.Queue(maxsize=256)
        self._stream = None
        self._closed = threading.Event()
        self._device = device  # explicit PortAudio device index, or None=default

    def start(self):
        def _cb(indata, frames, t, status):
            if self._closed.is_set():
                return
            if indata is None or not indata.shape[0]:
                return
            try:
                self._q.put(indata.copy(), timeout=0.1)
            except queue.Full:
                pass

        kwargs = dict(samplerate=_SAMPLE_RATE, channels=_CHANNELS,
                      dtype="int16", blocksize=int(_SAMPLE_RATE * 0.2),
                      callback=_cb)
        if self._device is not None:
            kwargs["device"] = self._device
        self._stream = sd.InputStream(**kwargs)
        self._stream.start()

    def next_block(self, timeout=0.2):
        if self._closed.is_set():
            return None
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self._closed.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


# -- Windows WaveIn (winmm.dll) capture --------------------------------------
class _WinMMFormat(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_ulong),
        ("nAvgBytesPerSec", ctypes.c_ulong),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class _WinMMHeader(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", ctypes.c_ulong),
        ("dwBytesRecorded", ctypes.c_ulong),
        ("dwUser", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("dwLoops", ctypes.c_ulong),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_ulong),
    ]


class _WinMMCapture:
    """WaveIn (winmm.dll) streaming capture at 16k/mono - the same API the OS
    uses, which stays healthy even when PortAudio's capture path does not.
    A pump thread polls N cyclic buffers and hands each finished 0.2s block to
    next_block(). All winmm calls live on the pump thread (no cross-thread
    races); close() just signals it."""

    WAVE_MAPPER = 0xFFFFFFFF
    WHDR_DONE = 0x00000001
    _N_BLOCKS = 8

    def __init__(self):
        self._q = queue.Queue(maxsize=256)
        self._closed = threading.Event()
        self._pump = None
        self._ready = threading.Event()
        self._open_error = None
        self.error = None

    def start(self):
        if getattr(ctypes, "windll", None) is None:
            raise RuntimeError("winmm capture requires Windows")
        self._pump = threading.Thread(target=self._run, daemon=True,
                                      name="ra-winmm")
        self._pump.start()
        # Wait for the device to actually open (poll loop needs it pumping).
        if not self._ready.wait(timeout=5):
            raise RuntimeError("winmm capture did not open in time")
        if self._open_error:
            raise RuntimeError(self._open_error)

    def next_block(self, timeout=0.2):
        if self._closed.is_set():
            return None
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self._closed.set()
        if self._pump is not None:
            self._pump.join(timeout=1.0)
            self._pump = None

    def _run(self):
        try:
            self._device_loop()
        except Exception as e:
            self.error = e
            self._open_error = str(e)
            ralog.log("err", f"winmm capture failed: {e}")
        finally:
            self._ready.set()
            self._release()

    def _device_loop(self):
        winmm = ctypes.windll.winmm
        winmm.waveInOpen.restype = ctypes.c_int
        winmm.waveInOpen.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint,
            ctypes.POINTER(_WinMMFormat), ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_uint]
        winmm.waveInPrepareHeader.restype = ctypes.c_int
        winmm.waveInPrepareHeader.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_WinMMHeader), ctypes.c_uint]
        winmm.waveInAddBuffer.restype = ctypes.c_int
        winmm.waveInAddBuffer.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_WinMMHeader), ctypes.c_uint]
        winmm.waveInStart.restype = ctypes.c_int
        winmm.waveInStart.argtypes = [ctypes.c_void_p]
        winmm.waveInStop.restype = ctypes.c_int
        winmm.waveInStop.argtypes = [ctypes.c_void_p]
        winmm.waveInUnprepareHeader.restype = ctypes.c_int
        winmm.waveInUnprepareHeader.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_WinMMHeader), ctypes.c_uint]
        winmm.waveInClose.restype = ctypes.c_int
        winmm.waveInClose.argtypes = [ctypes.c_void_p]

        fmt = _WinMMFormat()
        fmt.wFormatTag = 1
        fmt.nChannels = _CHANNELS
        fmt.nSamplesPerSec = _SAMPLE_RATE
        fmt.wBitsPerSample = 16
        fmt.nBlockAlign = 2
        fmt.nAvgBytesPerSec = _SAMPLE_RATE * 2

        self._hwi = ctypes.c_void_p(0)
        rc = winmm.waveInOpen(ctypes.byref(self._hwi), self.WAVE_MAPPER,
                              ctypes.byref(fmt), None, None, 0)
        if rc != 0:
            raise RuntimeError(f"waveInOpen failed (rc={rc}) - "
                               f"is the microphone in use?")

        block_samples = int(_SAMPLE_RATE * 0.2)
        self._bufs = [bytearray(block_samples * 2) for _ in range(self._N_BLOCKS)]
        self._pins = []
        self._headers = []
        self._hdr_size = ctypes.sizeof(_WinMMHeader)
        for b in self._bufs:
            pin = (ctypes.c_ubyte * len(b)).from_buffer(b)
            hdr = _WinMMHeader()
            hdr.lpData = ctypes.cast(pin, ctypes.c_void_p).value
            hdr.dwBufferLength = len(b)
            rc = winmm.waveInPrepareHeader(self._hwi, ctypes.byref(hdr),
                                           self._hdr_size)
            if rc != 0:
                raise RuntimeError(f"waveInPrepareHeader failed (rc={rc})")
            rc = winmm.waveInAddBuffer(self._hwi, ctypes.byref(hdr),
                                       self._hdr_size)
            if rc != 0:
                raise RuntimeError(f"waveInAddBuffer failed (rc={rc})")
            self._pins.append(pin)
            self._headers.append(hdr)

        rc = winmm.waveInStart(self._hwi)
        if rc != 0:
            raise RuntimeError(f"waveInStart failed (rc={rc})")
        self._ready.set()

        while not self._closed.is_set():
            for i, hdr in enumerate(self._headers):
                if hdr.dwFlags & self.WHDR_DONE:
                    recorded = hdr.dwBytesRecorded
                    block = np.frombuffer(self._bufs[i], dtype=np.int16,
                                          count=recorded // 2).copy()
                    hdr.dwFlags = 0
                    winmm.waveInAddBuffer(self._hwi, ctypes.byref(hdr),
                                          self._hdr_size)
                    if block.shape[0]:
                        try:
                            self._q.put(block, timeout=0.1)
                        except queue.Full:
                            pass
            self._closed.wait(0.004)

    def _release(self):
        winmm = getattr(ctypes, "windll", None)
        hwi = getattr(self, "_hwi", None)
        if winmm is None or not hwi:
            return
        try:
            winmm.waveInStop(hwi)
        except Exception:
            pass
        for hdr in getattr(self, "_headers", []):
            try:
                winmm.waveInUnprepareHeader(hwi, ctypes.byref(hdr),
                                            self._hdr_size)
            except Exception:
                pass
        try:
            winmm.waveInClose(hwi)
        except Exception:
            pass


def _probe_mic_level(device, seconds=1.0):
    """Capture `seconds` from one sounddevice input; returns (peak, mean) in
    0..1 normalized units, or None if the device cannot open."""
    try:
        with sd.InputStream(samplerate=_SAMPLE_RATE, channels=_CHANNELS,
                            dtype="int16", device=device,
                            blocksize=int(_SAMPLE_RATE * 0.2)) as st:
            chunks = []
            t0 = time.time()
            while time.time() - t0 < seconds:
                d, _ovf = st.read(st.blocksize)
                chunks.append(d[:, 0].astype(np.float64))
        if not chunks:
            return None
        a = np.concatenate(chunks)
        return (float(np.max(np.abs(a))) / 32768.0,
                float(np.mean(np.abs(a))) / 32768.0)
    except Exception:
        return None


def _pick_mic_device():
    """Find the sounddevice input that actually carries audio. Windows' default
    recording device can be a dead/disconnected endpoint while the real mic is
    another index away (exactly what deafened Ra: sounddevice opened the silent
    default while your voice sat on a sibling device). We probe EVERY input,
    prefer the loudest LIVE one, and remember it for the process lifetime.

    Honors RA_STT_MIC_DEVICE override (exact index, or name substring)."""
    override = getattr(config, "STT_MIC_DEVICE", "").strip()
    if override:
        candidates = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] < 1:
                continue
            if override.isdigit() and i == int(override):
                candidates.append((i, d["name"]))
            elif not override.isdigit() and override.lower() in d["name"].lower():
                candidates.append((i, d["name"]))
        if candidates:
            i, name = candidates[0]
            _mic_device_cache["index"] = i
            _mic_device_cache["name"] = name
            ralog.log("voice", f"mic override -> device [{i}] '{name}'")
            _stt_debug(f"mic override -> device [{i}] '{name}'")
            return i
        ralog.log("warn", f"RA_STT_MIC_DEVICE='{override}' matched no openable "
                          f"input; falling back to auto-pick")
        _stt_debug(f"override '{override}' matched nothing; auto-pick")
    if _mic_device_cache["index"] is not None:
        return _mic_device_cache["index"]
    # PortAudio enumerates alias/redirect endpoints that never carry speech
    # (Sound Mapper, Primary Sound Capture Driver, Stereo Mix, aux jacks).
    # Ranked as "real mic or not": only genuine microphones are candidates.
    _ALIAS_FRAGMENTS = ("sound mapper", "primary sound capture", "stereo mix",
                        "mono mix", " aux", "aux jack", "pc speaker",
                        "handsfree", "hands-free", "hands free")
    best = None  # (peak, mean, index, name, real)
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] < 1:
            continue
        name = (d["name"] or "")
        real = not any(f in name.lower() for f in _ALIAS_FRAGMENTS)
        r = _probe_mic_level(i, seconds=0.9)
        if r is None:
            _stt_debug(f"sd[{i}] '{name[:35]}' open failed (skip)")
            continue
        peak, mean = r
        _stt_debug(f"sd[{i}] '{name[:35]}' real={real} peak={peak:.4f} "
                   f"mean={mean:.4f}")
        # Score (keep lowest):
        #   1. a REAL mic beats any alias (a Mapper can show phantom noise
        #      while smuggling zero voice);
        #   2. a device that produced ANY nonzero signal beats an exact-zero
        #      endpoint - on multi-host setups the same array exists as e.g.
        #      an MME entry that carries speech and a WDM entry that returns
        #      literal 0.0000 forever (dead path, never pick it);
        #   3. among truly-live devices the loudest wins, so during speech
        #      the working array host wins even mid-utterance.
        key = (0 if real else 1,
               0 if peak > 0 else 1,
               0 if mean > 0 else 1,
               peak)
        if best is None or key < best[0]:
            best = (key, peak, mean, i, name, real)
    if best is None:
        ralog.log("err", "capture: NO openable sounddevice input found.")
        return None
    _key, peak, mean, i, name, real = best
    _mic_device_cache["index"] = i
    _mic_device_cache["name"] = name
    ralog.log("voice", f"mic auto-picked device [{i}] '{name}' "
                       f"(real={real} peak={peak:.4f} mean={mean:.4f})")
    _stt_debug(f"mic auto-picked device [{i}] '{name}' real={real} "
               f"peak={peak:.4f} mean={mean:.4f}")
    return i


def _open_capture_source():
    """Open the best available mic capture; returns (source, backend_name).
    Chooses the LIVE device explicitly (never trusts the OS default), then
    verifies the backend actually delivers blocks. Raises RuntimeError if no
    backend can open a stream."""
    order = _BACKEND_ORDER.get(getattr(config, "STT_AUDIO_BACKEND", "auto"),
                               _BACKEND_ORDER["auto"])
    failures = []
    for name in order:
        try:
            dev = _pick_mic_device() if name == "sounddevice" else None
            src = _WinMMCapture() if name == "winmm" else _SDCapture(device=dev)
            src.start()
            # Aliveness: a backend can "open" yet endlessly deliver nothing
            # (winmm on this rig gave 0 blocks all 8s). Verify the first block
            # actually arrives before accepting it.
            got = src.next_block(timeout=1.5)
            if got is None:
                src.close()
                msg = (f"'{name}' opened but delivered no audio"
                       + (f" on device [{dev}]" if dev is not None else ""))
                failures.append(msg)
                ralog.log("err", f"capture: {msg}")
                _stt_debug(f"capture: {msg}")
                continue
            if dev is not None:
                ralog.log("voice", f"Mic capture backend: {name} "
                                   f"(device [{dev}] '{_mic_device_cache['name']}')")
            else:
                ralog.log("voice", f"Mic capture backend: {name}")
            return src, name
        except Exception as e:
            failures.append(f"{name}: {e}")
            ralog.log("warn", f"Mic capture backend '{name}' unavailable: {e}")
            _stt_debug(f"backend '{name}' unavailable: {e}")
    hint = _container_mic_hint()
    raise RuntimeError(
        f"no working mic capture backend (tried {', '.join(order)}) - "
        + "".join(failures)
        + (f" {hint}" if hint else "")
    )


def _container_mic_hint():
    """Microsoft Store (MSIX/AppContainer) Pythons are blocked from microphone
    capture at the OS level - no host API can open the device (they all fail
    with the same 'undefined external error'). The packaged Ra.exe is a
    normal desktop process and is NOT affected."""
    try:
        import sys as _sys
        if "windowsapps" in (_sys.executable or "").lower():
            return ("Hint: this Python is the Microsoft Store (AppContainer) "
                    "build, which Windows blocks from recording the microphone. "
                    "Run run.bat (option 1) or launch dist\\Ra.exe - "
                    "hearing works there.")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Room-noise calibration helpers
# ---------------------------------------------------------------------------
# A 25ms window is fine enough to see syllable-level energy swings while still
# being a stable estimate of the local amplitude. The coefficient of variation
# (std/mean) of the windowed RMS envelope separates HUMAN SPEECH from steady
# machine hum (fan / AC / cooler): speech modulates strongly (syllables,
# onsets, pauses - CV typically > 0.3), while a constant hum is nearly flat
# (CV < 0.1 near zero). This single number drives both the per-block "is this
# speech?" gate and the whole-utterance "was that just noise?" backstop.
_STT_WIN = int(_SAMPLE_RATE * 0.025)


def _rms_envelope(audio):
    """RMS level of each 25ms window across `audio` (int16 or float32)."""
    if audio is None or audio.size == 0:
        return np.zeros(1, dtype=np.float64)
    try:
        win = int(min(_STT_WIN, audio.shape[0]))
    except Exception:
        win = 1
    if win < 1:
        win = 1
    n = audio.shape[0] // win
    if n < 2:
        flat = audio.astype(np.float64)
        return np.array([float(np.sqrt(np.mean(flat * flat)))])
    flat = audio[: n * win].astype(np.float64)
    frames = flat.reshape(n, win)
    return np.sqrt(np.mean(frames * frames, axis=1))


def _energy_cv(audio) -> float:
    """Coefficient of variation of the 25ms RMS envelope. 0 = perfectly flat
    (steady fan/AC hum); real speech is strongly modulated (>0.3 typically)."""
    env = _rms_envelope(audio)
    mean = float(np.mean(env))
    if mean <= 1e-5:
        return 0.0
    return float(np.std(env)) / mean


def _block_modulation(block) -> float:
    """Modulation (CV of the windowed RMS envelope) of one 0.2s int16 block."""
    return _energy_cv(block)


def _track_noise_floor(noise: float, level: float) -> float:
    """Adapt the ambient noise floor to the room, WITHOUT ever letting speech
    (or a shout) lift it. Loud audio never moves the floor - that is what made
    Ra deaf after the first scream - while a steady fan hum at the floor is
    absorbed within a second or two (fast enough to become room tone, but too
    slow for a 2-3s phrase to inflate the threshold mid-sentence)."""
    if level >= config.STT_LOUD_LEVEL or level > noise * config.STT_NOISE_UP:
        # Loud input is speech or a one-off slam - definitely NOT room noise.
        return noise
    if level > noise:
        return min(level, noise + noise * config.STT_FLOOR_RISE)
    return max(level, noise - noise * config.STT_FLOOR_FALL)


def _classify_speech(level: float, block, noise: float) -> bool:
    """Band-based speech test for one 0.2s block. Three bands top-down:

    1. LOUD (absolute, e.g. a screaming / close-talking user) -> always speech,
       envelope irrelevant. This is the anti-deafness guarantee.
    2. CLEARLY ABOVE AMBIENT (moderate level, well above the tracked fan hum)
       -> speech on level alone (real words stream over the hum).
    3. NEAR THE FLOOR (weak signal) -> only speech when the envelope is
       modulated; a steady fan hum right at the floor stays flat = noise.
    """
    if level >= config.STT_LOUD_LEVEL:
        return True
    if level > max(noise * config.STT_NOISE_RATIO,
                   config.STT_MIN_SIGNAL_LEVEL):
        return True
    return (level >= config.STT_MIN_SIGNAL_LEVEL
            and _block_modulation(block) >= config.STT_MODULATION_THRESHOLD)


def _is_human_audio(audio) -> bool:
    """Whole-utterance backstop: is a finished clip really a person speaking?
    A LOUD clip (scream / shout) is accepted unconditionally - only quiet clips
    need envelope-modulation proof, so flat fan hum sitting at the floor is
    still dropped but a held shout can never be silenced."""
    level = float(np.abs(audio).mean())
    if level >= config.STT_LOUD_LEVEL:
        return True
    return _energy_cv(audio) >= config.STT_MODULATION_THRESHOLD


def _contains_wake(text: str) -> bool:
    if not text:
        return False
    try:
        from ra.assistant import _contains_wake as assistant_contains_wake
        return assistant_contains_wake(text)
    except Exception:
        lower = text.lower().strip()
        wake_words = ("ra", "hey ra", "fire up", "rah", "raw", "ray", "rad", "rock")
        return any(w in lower for w in wake_words)


def _is_valid_speech(text: str, audio, sustained: float, min_speech: float) -> bool:
    if not text:
        return False
    # A recognized phrase containing a wake word is ALWAYS valid speech (e.g. "ra")
    if _contains_wake(text):
        return True
    # Non-wake phrases (noise blips, ambient sounds) must meet sustained speech duration
    if min_speech > 0 and sustained < min_speech:
        return False
    if audio is not None and not _is_human_audio(audio):
        return False
    return True


class MicSession:
    """Continuous streaming STT session. Fires on_partial(text) live and
    on_phrase(text) each time a complete phrase is recognized.

    Hearing quality:
    - EVERY audio block is fed to Vosk (nothing is dropped - soft speech stays
      intact); an adaptive noise floor is tracked for energy decisions only.
    - The audio for the CURRENT phrase only is captured (blocks since the last
      Vosk phrase boundary), so faster-whisper re-transcribes just that
      utterance - not a stale 30s rolling buffer - for a far more accurate AND
      faster final text (hybrid mode).
    - The Vosk text is dispatched to the assistant IMMEDIATELY (responses start
      instantly, "smooth"); whisper runs in parallel and, when it finds a
      meaningfully different text, reports it via on_correct(original, corrected)
      so the assistant can upgrade the pending phrase before acting on it.
    - While Ra is speaking (busy_check() is truthy), the expensive whisper
      re-transcription is skipped - Vosk text is passed through and the
      assistant's echo guard decides what to do with it.
    """

    def __init__(self, on_phrase, on_partial=None, busy_check=None, on_correct=None):
        self.on_phrase = on_phrase
        self.on_partial = on_partial or (lambda text: None)
        self.busy_check = busy_check or (lambda: False)
        self.on_correct = on_correct or (lambda original, corrected: None)
        self._last_partial = ""
        self._thread = None
        self._stop = threading.Event()
        self._src = None
        self._backend = None
        self.error = None
        self._clip = []               # int16 blocks since the last phrase boundary
        self._clip_samples = 0
        self._buf_lock = threading.Lock()
        self._noise = float(getattr(config, "STT_ENERGY_THRESHOLD", 300)) / 32768.0
        self._speech_seconds = 0.0
        self._min_speech = float(getattr(config, "STT_MIN_SPEECH_SECONDS", 0.4))

    def start(self):
        import vosk  # eager: fail fast with a clear message
        vosk.SetLogLevel(-1)
        model = _get_vosk()
        from vosk import KaldiRecognizer
        self._rec = KaldiRecognizer(model, _SAMPLE_RATE)
        self._src, self._backend = _open_capture_source()
        self._dispatch_q: queue.Queue = queue.Queue()
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_worker, daemon=True, name="ra-dispatch")
        self._dispatch_thread.start()
        self._fin_q: queue.Queue = queue.Queue()
        self._fin_thread = threading.Thread(target=self._finalize_worker,
                                            daemon=True, name="ra-finalize")
        self._fin_thread.start()
        # Warm whisper in the background so the first spoken phrase doesn't pay
        # model-download/load latency and corrections land immediately.
        self._preload_thread = threading.Thread(
            target=self._preload_whisper, daemon=True, name="ra-preload")
        self._preload_thread.start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ra-mic")
        self._thread.start()
        return self

    def _preload_whisper(self):
        try:
            _get_whisper_model()
            ralog.log("ok", "Whisper model ready.")
        except Exception as e:
            ralog.log("warn", f"Whisper preload failed ({e}); vosk-only until it loads")

    def _dispatch_worker(self):
        """Calls on_phrase on a DEDICATED thread - never inside the sounddevice
        audio callback (anything blocking there glitches the mic stream)."""
        while not self._stop.is_set():
            try:
                text = self._dispatch_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if text:
                self.on_phrase(text)

    def stop(self):
        self._stop.set()
        if self._src is not None:
            self._src.close()

    def _run(self):
        ralog.log("voice", f"microphone streaming started "
                           f"({self._backend}, adaptive, hybrid finalize).")
        try:
            while not self._stop.is_set():
                block = self._src.next_block(timeout=0.2)
                if block is None:
                    continue
                self._process_block(block)
        except Exception as e:
            self.error = e
            ralog.log("err", f"microphone failed: {e}")

    # -- noise floor --------------------------------------------------------
    def _track_noise(self, level: float, block):
        """Adapt the floor to the room's ambient. Loud audio (speech, shouts,
        slams) NEVER moves the floor - a loud user must not become the new
        hearing threshold - and a steady fan hum is absorbed as room tone."""
        self._noise = _track_noise_floor(self._noise, level)

    # -- utterance clip (audio for the CURRENT phrase only) ----------------
    def _clip_block(self, block):
        with self._buf_lock:
            self._clip.append(block.copy())
            self._clip_samples += block.shape[0]
            # cap: drop the oldest blocks of a single very long utterance
            max_samples = int(_SAMPLE_RATE * getattr(
                config, "STT_UTTERANCE_BUFFER_SECONDS", 30))
            while self._clip_samples > max_samples and self._clip:
                dropped = self._clip.pop(0)
                self._clip_samples -= dropped.shape[0]

    def _take_clip(self):
        """Return the captured audio for the phrase that just ended (float32),
        and reset the capture for the next one. Locked: the audio callback runs
        on the sounddevice thread while this may run on the finalize thread."""
        with self._buf_lock:
            if not self._clip:
                return None
            audio = np.concatenate(self._clip, axis=0).astype(np.float32) \
                .flatten() / 32768.0
            self._clip = []
            self._clip_samples = 0
            return audio if audio.shape[0] >= _SAMPLE_RATE * 0.2 else None

    def _finalize(self, vosk_text: str, audio):
        """Offline re-transcribes ONLY the finished phrase's audio (parallel to
        the already-dispatched Vosk text). If the result is meaningfully
        different, tell the assistant so it can upgrade the pending phrase."""
        if not vosk_text or self.busy_check():
            return
        if audio is None or audio.shape[0] < _SAMPLE_RATE * 0.3:
            return
        fin = getattr(config, "STT_FINAL_ENGINE", "parakeet").lower()
        try:
            if fin == "groq":
                wtext = _groq_transcribe(audio)
            elif fin == "parakeet":
                wtext = _transcribe_sherpa_offline(audio)
            else:
                model = _get_whisper_model()
                segments, _info = model.transcribe(
                    audio, language="en", beam_size=1, vad_filter=True)
                wtext = " ".join(seg.text for seg in segments).strip()
            if wtext and wtext.lower().strip() != vosk_text.lower().strip():
                ralog.log("voice", f"vosk: '{vosk_text}' -> {fin}: '{wtext}'")
                self.on_correct(vosk_text, wtext)
        except Exception as e:
            ralog.log("warn", f"{fin} finalize failed ({e}); keeping vosk text")

    # -- streaming ----------------------------------------------------------
    def _finalize_worker(self):
        """Offline re-transcription runs OFF the audio callback (a CPU-heavy
        decode inside the sounddevice callback would glitch the stream)."""
        while not self._stop.is_set():
            try:
                text, audio = self._fin_q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._finalize(text, audio)

    def _feed(self, block: bytes):
        if self._rec.AcceptWaveform(block):
            import json
            result = self._rec.Result()
            text = (json.loads(result).get("text", "") or "").strip()
            self._last_partial = ""
            sustained = self._speech_seconds
            self._speech_seconds = 0.0
            audio = self._take_clip()
            if text and not _is_valid_speech(text, audio, sustained, self._min_speech):
                ralog.log("voice", f"ignoring noise phrase '{text}' "
                                   f"({sustained:.2f}s speech).")
                return
            if text:
                # Smooth: dispatch on a dedicated thread (instant, callback-free).
                self._dispatch_q.put(text)
                # Clear: whisper re-transcribes the phrase in the background.
                if audio is not None:
                    self._fin_q.put((text, audio))
        else:
            import json
            partial = self._rec.PartialResult()
            text = (json.loads(partial).get("partial", "") or "").strip()
            if text and text != self._last_partial:
                self._last_partial = text
                self.on_partial(text)

    def _process_block(self, block):
        """A 0.2s mono int16 block arrived from the capture backend."""
        if self._stop.is_set():
            return
        level = float(np.abs(block).mean()) / 32768.0
        self._track_noise(level, block)
        sec = block.shape[0] / _SAMPLE_RATE
        # Band classifier: a scream is ALWAYS speech, audio clearly above the
        # ambient hum is speech, and only weak near-floor audio needs envelope
        # modulation proof (fan / AC hum is flat real speech is not).
        talking = _classify_speech(level, block, self._noise)
        _stt_debug("vol=%s lvl=%.4f cv=%.3f floor=%.4f -> %s" % (
            "S" if talking else "-", level,
            _energy_cv(block) if level >= config.STT_MIN_SIGNAL_LEVEL else 0.0,
            self._noise, "SPEECH" if talking else "noise"))
        if talking:
            self._clip_block(block)
            self._speech_seconds += sec
        else:
            # Decay faster than it accrues so scattered blips can't sum to
            # "speech" - only genuinely sustained speech passes the gate.
            self._speech_seconds = max(0.0, self._speech_seconds - sec * 2)
        self._feed(block.tobytes())  # never drop audio - Vosk handles silence


# ---------------------------------------------------------------------------
# sherpa-onnx streaming (default STT - fast-conformer en-480ms int8)
# ---------------------------------------------------------------------------
class _SherpaStream:
    """Continuous streaming STT session powered by sherpa-onnx. Live partials
    come from the streaming fast-conformer transducer; sherpa's own endpoint
    rules detect phrase boundaries (sustained-speech + _is_human_audio
    backstops kept). Mirrors MicSession's architecture: EVERY gated block is
    fed to the streaming recognizer. The final text is refined in the
    background by the offline parakeet recognizer (STT_FINAL_ENGINE=parakeet)
    or, optionally, by Groq Whisper / faster-whisper (STT_FINAL_ENGINE=groq/
    whisper)."""

    def __init__(self, on_phrase, on_partial=None, busy_check=None, on_correct=None):
        self.on_phrase = on_phrase
        self.on_partial = on_partial or (lambda text: None)
        self.busy_check = busy_check or (lambda: False)
        self.on_correct = on_correct or (lambda original, corrected: None)
        self.error = None
        self._thread = None
        self._stop = threading.Event()
        self._src = None
        self._backend = None
        self._last_partial = ""
        self._clip = []
        self._clip_lock = threading.RLock()
        self._clip_samples = 0
        self._noise = float(getattr(config, "STT_ENERGY_THRESHOLD", 300)) / 32768.0
        self._speech_seconds = 0.0
        self._in_speech = False
        self._silence_since = None
        self._min_speech = float(getattr(config, "STT_MIN_SPEECH_SECONDS", 0.4))

    def start(self):
        self._recognizer = _get_sherpa()
        self._stream = self._recognizer.create_stream()
        self._src, self._backend = _open_capture_source()
        self._dispatch_q: queue.Queue = queue.Queue()
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_worker, daemon=True, name="ra-dispatch")
        self._dispatch_thread.start()
        self._fin_q: queue.Queue = queue.Queue()
        self._fin_thread = threading.Thread(
            target=self._finalize_worker, daemon=True, name="ra-finalize")
        self._fin_thread.start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ra-mic")
        self._thread.start()
        ralog.log("voice", f"microphone streaming started "
                           f"({self._backend}, sherpa-onnx, adaptive gate).")
        return self

    def stop(self):
        self._stop.set()
        if self._src is not None:
            self._src.close()

    def _dispatch_worker(self):
        while not self._stop.is_set():
            try:
                text = self._dispatch_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if text:
                self.on_phrase(text)

    def _run(self):
        try:
            while not self._stop.is_set():
                block = self._src.next_block(timeout=0.2)
                if block is None:
                    continue
                self._process_block(block)
        except Exception as e:
            self.error = e
            ralog.log("err", f"sherpa microphone failed: {e}")

    def _clip_block(self, block):
        with self._clip_lock:
            self._clip.append(block.copy())
            self._clip_samples += block.shape[0]
            max_samples = int(_SAMPLE_RATE * getattr(
                config, "STT_UTTERANCE_BUFFER_SECONDS", 30))
            while self._clip_samples > max_samples and self._clip:
                dropped = self._clip.pop(0)
                self._clip_samples -= dropped.shape[0]

    def _take_clip(self):
        with self._clip_lock:
            if not self._clip:
                return None
            audio = np.concatenate(self._clip, axis=0).astype(np.float32) \
                .flatten() / 32768.0
            self._clip = []
            self._clip_samples = 0
            return audio if audio.shape[0] >= _SAMPLE_RATE * 0.2 else None

    def _process_block(self, block):
        """A 0.2s mono int16 block arrived from the capture backend.

        Phrase boundaries are driven by Ra's OWN adaptive gate (adaptive noise
        floor + band classifier + trailing-silence), mirroring the proven
        `_WhisperVADStream` - NOT by sherpa's `is_endpoint()` rules (sherpa's
        1.2-2.4s trailing-silence endpoint never fires on a noisy room floor,
        which is exactly why continuous mode went silent while PTT is perfect).
        Sherpa is still fed EVERY block so live partials flow; `_end_phrase`
        resets its stream when Ra decides a phrase is done."""
        if self._stop.is_set():
            return
        level = float(np.abs(block).mean()) / 32768.0
        sec = block.shape[0] / _SAMPLE_RATE
        self._noise = _track_noise_floor(self._noise, level)
        talking = _classify_speech(level, block, self._noise)
        _stt_debug("vol=%s lvl=%.4f cv=%.3f floor=%.4f -> %s" % (
            "S" if talking else "-", level,
            _energy_cv(block) if level >= config.STT_MIN_SIGNAL_LEVEL else 0.0,
            self._noise, "SPEECH" if talking else "noise"))
        finalize = False
        with self._clip_lock:
            if talking and not self._in_speech:
                self._in_speech = True
                self._speech_seconds = 0.0
                self._silence_since = None
                self._clip = [block.copy()]
            elif self._in_speech:
                self._clip_block(block)
                if talking:
                    self._speech_seconds += sec
                    self._silence_since = None
                else:
                    self._speech_seconds = max(
                        0.0, self._speech_seconds - sec * 2)
                    if self._silence_since is None:
                        self._silence_since = time.time()
                    else:
                        pause = float(getattr(
                            config, "STT_PAUSE_THRESHOLD", 0.8))
                        if time.time() - self._silence_since >= pause:
                            finalize = True
                            self._in_speech = False
                            self._silence_since = None
        if finalize:
            self._end_phrase()
        # Feed the float32 samples ([-1, 1]) to sherpa - never drop audio.
        samples = (block.astype(np.float32) / 32768.0).flatten()
        self._stream.accept_waveform(_SAMPLE_RATE, samples)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        partial = (self._recognizer.get_result(self._stream) or "").strip()
        if partial and partial != self._last_partial:
            self._last_partial = partial
            self.on_partial(partial)

    def _end_phrase(self):
        text = (self._recognizer.get_result(self._stream) or "").strip()
        self._recognizer.reset(self._stream)
        self._last_partial = ""
        sustained = self._speech_seconds
        self._speech_seconds = 0.0
        audio = self._take_clip()
        if text and not _is_valid_speech(text, audio, sustained, self._min_speech):
            ralog.log("voice", f"ignoring noise phrase '{text}' "
                               f"({sustained:.2f}s speech).")
            return
        if text:
            self._dispatch_q.put(text)
            fin = getattr(config, "STT_FINAL_ENGINE", "parakeet").lower()
            if audio is not None and fin in ("parakeet", "groq", "whisper") \
                    and not self.busy_check():
                self._fin_q.put((text, audio))

    def _finalize_worker(self):
        fin = getattr(config, "STT_FINAL_ENGINE", "parakeet").lower()
        while not self._stop.is_set():
            try:
                text, audio = self._fin_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if fin == "groq":
                    wtext = _groq_transcribe(audio)
                elif fin == "parakeet":
                    wtext = _transcribe_sherpa_offline(audio)
                else:
                    wtext = _whisper_transcribe(audio)
                if wtext and wtext.lower().strip() != text.lower().strip():
                    ralog.log("voice", f"sherpa: '{text}' -> final: '{wtext}'")
                    self.on_correct(text, wtext)
            except Exception as e:
                ralog.log("warn", f"finalize failed ({e}); keeping sherpa text")


def _whisper_transcribe(audio) -> str:
    """faster-whisper batch transcription of a float32 clip."""
    model = _get_whisper_model()
    segments, _info = model.transcribe(
        audio, language="en", beam_size=1, vad_filter=True)
    return " ".join(seg.text for seg in segments).strip()


def _groq_transcribe(audio) -> str:
    """Optional cloud FINAL pass via Groq Whisper (whisper-large-v3-turbo).
    Requires RA_GROQ_API_KEY. Converts the clip to an in-memory WAV and POSTs
    it to the Groq audio endpoint."""
    import io
    import wave
    import json

    api_key = os.environ.get("RA_GROQ_API_KEY") or os.environ.get("GROQ_API_KEY")
    if not api_key:
        ralog.log("warn", "STT_FINAL_ENGINE=groq but no RA_GROQ_API_KEY set.")
        return ""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(_CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes((audio * 32768).astype(np.int16).tobytes())
    buf.seek(0)
    import requests
    r = requests.post(
        getattr(config, "GROQ_WHISPER_URL", "https://api.groq.com/openai/v1/"
                "audio/transcriptions"),
        headers={"Authorization": "Bearer " + api_key},
        files={"file": ("clip.wav", buf.getvalue(),
                        "audio/wav")},
        data={"model": getattr(config, "GROQ_WHISPER_MODEL",
                               "whisper-large-v3-turbo")},
        timeout=60)
    r.raise_for_status()
    return (json.loads(r.text) or {}).get("text", "").strip()


# ---------------------------------------------------------------------------
# Always-on Whisper+VAD streaming (zero vosk dependency)
# ---------------------------------------------------------------------------
class _WhisperVADStream:
    """A keep-it-simple always-on listener that needs NO vosk: the mic streams
    continuously, an adaptive-endpoint VAD clips each utterance (0.2s blocks,
    trailing silence ~`STT_PAUSE_THRESHOLD` -> end-of-phrase), and faster-whisper
    transcribes just that clip. `on_phrase(text)` fires per finished utterance so
    the assistant hears instantly and the conversation flows exactly like the
    vosk path - without a single push-to-talk press.

    Used automatically whenever vosk is unavailable, misbehaving, or when
    RA_STT_ENGINE=whisper - so "always listening" is guaranteed on any box
    that can record audio at all.
    """

    def __init__(self, on_phrase, on_partial=None, busy_check=None, on_correct=None):
        self.on_phrase = on_phrase
        self.on_partial = on_partial or (lambda text: None)
        self.busy_check = busy_check or (lambda: False)
        self.on_correct = on_correct or (lambda original, corrected: None)
        self.error = None
        self._stop = threading.Event()
        self._src = None
        self._clip = []
        self._clip_lock = threading.RLock()
        self._noise = float(getattr(config, "STT_ENERGY_THRESHOLD", 300)) / 32768.0
        self._in_speech = False
        self._silence_since = None
        self._speech_started = 0.0
        self._speech_seconds = 0.0

# -- capture ----------------------------------------------------------
    def _track_noise(self, level: float, block):
        self._noise = _track_noise_floor(self._noise, level)

    # -- lifecycle --------------------------------------------------------
    def start(self):
        # Keep the whisper model warm so the first utterance transcribes fast.
        threading.Thread(target=self._preload, daemon=True,
                         name="ra-preload-w").start()
        self._src, self._backend = _open_capture_source()
        self._tx_q: queue.Queue = queue.Queue()
        threading.Thread(target=self._transcribe_worker, daemon=True,
                         name="ra-transcribe").start()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ra-whisper-mic")
        self._thread.start()
        ralog.log("voice", f"microphone streaming started ({self._backend}, "
                           f"whisper-VAD always-on).")
        return self

    def _preload(self):
        try:
            _get_whisper_model()
            ralog.log("ok", "Whisper model ready (whisper-VAD path).")
        except Exception as e:
            ralog.log("warn", f"Whisper preload failed ({e})")

    def stop(self):
        self._stop.set()
        if self._src is not None:
            self._src.close()

    # -- main loop ---------------------------------------------------------
    def _run(self):
        try:
            while not self._stop.is_set():
                block = self._src.next_block(timeout=0.25)
                if block is None:
                    continue
                self._process(block)
        except Exception as e:
            self.error = e
            ralog.log("err", f"whisper-VAD microphone failed: {e}")

    def _process(self, block):
        level = float(np.abs(block).mean()) / 32768.0
        self._track_noise(level, block)
        # Band classifier: a scream is ALWAYS speech, audio clearly above the
        # ambient hum is speech, and only weak near-floor audio needs envelope
        # modulation proof - a steady flat fan hum is never "talking".
        talking = _classify_speech(level, block, self._noise)
        _stt_debug("vol=%s lvl=%.4f cv=%.3f floor=%.4f -> %s" % (
            "S" if talking else "-", level,
            _energy_cv(block) if level >= config.STT_MIN_SIGNAL_LEVEL else 0.0,
            self._noise, "SPEECH" if talking else "noise"))
        sec = block.shape[0] / _SAMPLE_RATE
        with self._clip_lock:
            if talking and not self._in_speech:
                self._in_speech = True
                self._speech_started = time.time()
                self._speech_seconds = 0.0
                self._silence_since = None
                self._clip = [block.copy()]
                self.on_partial("…")
            elif self._in_speech:
                self._clip.append(block.copy())
                if talking:
                    self._speech_seconds += sec
                    self._silence_since = None
                else:
                    self._speech_seconds = max(
                        0.0, self._speech_seconds - sec * 2)
                    if self._silence_since is None:
                        self._silence_since = time.time()
                    else:
                        pause = float(getattr(config, "STT_PAUSE_THRESHOLD", 0.8))
                        if time.time() - self._silence_since >= pause:
                            self._finalize_utterance()

    def _finalize_utterance(self):
        with self._clip_lock:
            if not self._clip:
                return
            audio = np.concatenate(self._clip, axis=0).astype(np.float32) \
                .flatten() / 32768.0
            sustained = self._speech_seconds
            self._clip = []
            self._in_speech = False
            self._silence_since = None
            self._speech_seconds = 0.0
        self.on_partial("")
        if audio.shape[0] < _SAMPLE_RATE * 0.3:
            return
        min_speech = float(getattr(config, "STT_MIN_SPEECH_SECONDS", 0.4))
        if sustained < min_speech:
            # Random blip at or near the threshold (two clicks, a puff) - not a
            # real utterance; transcribing it just yields hallucinated words.
            ralog.log("voice", f"ignoring noise blip ({sustained:.2f}s speech).")
            return
        if not _is_human_audio(audio):
            # Whole-utterance backstop: steady hum that kept individual blocks
            # near the VAD threshold is still too flat to be human speech
            # (unless the clip is loud - a shout is human, period).
            ralog.log("voice", "ignoring flat-utterance (modulation too low).")
            return
        self._tx_q.put(audio)

    def _transcribe_worker(self):
        while not self._stop.is_set():
            try:
                audio = self._tx_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                model = _get_whisper_model()
                segments, _info = model.transcribe(
                    audio, language="en", beam_size=1, vad_filter=True)
                text = " ".join(seg.text for seg in segments).strip()
                if text:
                    self.on_partial(text)
                    self.on_phrase(text)
            except Exception as e:
                ralog.log("warn", f"whisper-VAD transcribe failed ({e})")


def start_mic(on_phrase, on_partial=None, busy_check=None, on_correct=None):
    """Start ALWAYS-ON streaming listening. Returned object exposes `.error` if
    the mic is unavailable. Prefers the vosk streaming path (the proven
    always-on engine); sherpa-onnx streaming and the whisper+VAD streamer are
    selectable via STT_CONTINUOUS_ENGINE. In every case the mic stays on
    continuously: no push-to-talk required. PTT still uses STT_ENGINE
    (default sherpa-onnx), so the two modes are independently configurable."""
    global _mic
    with _mic_lock:
        if _mic and _mic.error is None:
            return _mic
        # ALWAYS-ON listening uses its own engine choice (default vosk - the
        # proven always-on streaming path), independent of PTT's STT_ENGINE.
        engine = getattr(config, "STT_CONTINUOUS_ENGINE", "vosk").lower()
        try:
            if engine == "whisper":
                _mic = _WhisperVADStream(on_phrase, on_partial,
                                         busy_check=busy_check,
                                         on_correct=on_correct).start()
            elif engine == "sherpa-onnx":
                _mic = _SherpaStream(on_phrase, on_partial, busy_check=busy_check,
                                     on_correct=on_correct).start()
            else:
                _mic = MicSession(on_phrase, on_partial, busy_check=busy_check,
                                  on_correct=on_correct).start()
        except Exception as e:
            ralog.log("warn", f"{engine} streaming unavailable ({e}) - "
                              f"falling back to vosk streaming.")
            try:
                _mic = MicSession(on_phrase, on_partial, busy_check=busy_check,
                                  on_correct=on_correct).start()
            except Exception as e2:
                ralog.log("warn", f"vosk streaming unavailable ({e2}) - "
                                  f"falling back to whisper-VAD always-on.")
                try:
                    _mic = _WhisperVADStream(on_phrase, on_partial,
                                             busy_check=busy_check,
                                             on_correct=on_correct).start()
                except Exception as e3:
                    ralog.log("err", f"voice unavailable ({e3}) - "
                                     f"push-to-talk uses Whisper")
                    _mic = MicSession(on_phrase, on_partial)
                    _mic.error = e2 if not getattr(
                        _mic, "error", None) else _mic.error
        return _mic


def stop_mic():
    global _mic
    with _mic_lock:
        if _mic:
            _mic.stop()
        _mic = None


def _record_once(timeout=8, pause_after_speech=1.4, min_speech_seconds=0.35):
    """Record from the mic until silence or timeout; returns int16 float array."""
    threshold = getattr(config, "STT_ENERGY_THRESHOLD", 300)
    frames = []
    state = {"speech": False, "silence_since": None}
    src, _backend = _open_capture_source()
    try:
        start = time.time()
        while True:
            block = src.next_block(timeout=0.03)
            if block is None or not block.shape[0]:
                continue
            frames.append(block)
            vol = float(np.abs(block).mean())
            if vol > threshold:
                state["speech"] = True
                state["silence_since"] = None
            elif state["speech"] and state["silence_since"] is None:
                state["silence_since"] = time.time()
            if time.time() - start > timeout:
                break
            if state["speech"] and state["silence_since"] is not None \
                    and time.time() - state["silence_since"] > pause_after_speech:
                break
            if state["speech"] and sum(f.shape[0] for f in frames) / _SAMPLE_RATE > 45:
                break
    finally:
        src.close()
    if not frames or not state["speech"]:
        return None
    audio = np.concatenate(frames, axis=0).astype(np.float32).flatten() / 32768.0
    if sum(f.shape[0] for f in frames) / _SAMPLE_RATE < min_speech_seconds:
        return None
    return audio


def _transcribe_vosk(audio) -> str:
    from vosk import KaldiRecognizer
    rec = KaldiRecognizer(_get_vosk(), _SAMPLE_RATE)
    rec.AcceptWaveform(audio.tobytes())
    import json
    text = (json.loads(rec.FinalResult()).get("text", "") or "").strip()
    return text


def _transcribe_sherpa(audio) -> str:
    """Batch transcription of one captured clip via sherpa-onnx (used by
    push-to-talk when STT_ENGINE=sherpa-onnx). Feeds the whole utterance to a
    fresh stream, adds tail padding, and returns the decoded text. When
    STT_FINAL_ENGINE=parakeet the streaming result is refined by the offline
    parakeet recognizer for maximum accuracy."""
    recognizer = _get_sherpa()
    stream = recognizer.create_stream()
    stream.accept_waveform(_SAMPLE_RATE, audio.astype(np.float32))
    tail = np.zeros(int(_SAMPLE_RATE * 0.8), dtype=np.float32)
    stream.accept_waveform(_SAMPLE_RATE, tail)
    stream.input_finished()
    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)
    text = (recognizer.get_result(stream) or "").strip()
    recognizer.reset(stream)
    fin = getattr(config, "STT_FINAL_ENGINE", "parakeet").lower()
    if fin == "parakeet" and text:
        refined = _transcribe_sherpa_offline(audio)
        if refined and refined.lower().strip() != text.lower().strip():
            ralog.log("voice", f"sherpa: '{text}' -> parakeet final: '{refined}'")
            return refined
    return text


def _transcribe_sherpa_offline(audio) -> str:
    """Offline (non-streaming) transcription of one clip via the offline
    parakeet recognizer - the higher-accuracy FINAL pass of the hybrid STT.
    Returns "" if the clip is too short to decode."""
    if audio is None or audio.shape[0] < _SAMPLE_RATE * 0.2:
        return ""
    recognizer = _get_sherpa_offline()
    samples = audio.astype(np.float32)
    tail = np.zeros(int(_SAMPLE_RATE * 0.8), dtype=np.float32)
    samples_aug = np.concatenate([samples, tail])
    stream = recognizer.create_stream()
    stream.accept_waveform(_SAMPLE_RATE, samples_aug)
    recognizer.decode_stream(stream)
    text = (stream.result.text or "").strip()
    return text


def listen_once(timeout: int = 8, phrase_time_limit: int = 10) -> str:
    """Push-to-talk: listen once and return the recognized phrase."""
    ralog.log("voice", "listening...")
    audio = _record_once(timeout=timeout, pause_after_speech=1.4)
    if audio is None:
        ralog.log("voice", "nothing heard.")
        return ""
    # Push-to-talk favors accuracy: the configured STT engine first, then a
    # final cloud pass only when STT_FINAL_ENGINE=groq/whisper. Falls back
    # down the chain (sherpa -> whisper -> vosk) if an engine is unavailable.
    engine = getattr(config, "STT_ENGINE", "sherpa-onnx").lower()
    if engine == "whisper":
        try:
            text = _whisper_transcribe(audio)
            if text:
                ralog.log("voice", f"heard: {text}")
                return text
        except Exception as e:
            ralog.log("warn", f"whisper transcription failed ({e}); using sherpa")
    else:
        try:
            text = _transcribe_sherpa(audio) if engine == "sherpa-onnx" \
                else _transcribe_vosk(audio)
            if text:
                ralog.log("voice", f"heard: {text}")
                return text
        except Exception as e:
            ralog.log("warn", f"{engine} transcription failed ({e}); using whisper")
    try:
        text = _whisper_transcribe(audio)
        if text:
            ralog.log("voice", f"heard: {text}")
            return text
    except Exception as e:
        ralog.log("warn", f"whisper transcription failed ({e}); using vosk")
    try:
        text = _transcribe_vosk(audio)
        if text:
            ralog.log("voice", f"heard: {text}")
        return text
    except Exception as e:
        ralog.log("err", f"speech recognition failed: {e}")
        return ""

