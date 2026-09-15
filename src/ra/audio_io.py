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

    def __init__(self):
        self._q = queue.Queue(maxsize=256)
        self._stream = None
        self._closed = threading.Event()

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

        self._stream = sd.InputStream(
            samplerate=_SAMPLE_RATE, channels=_CHANNELS, dtype="int16",
            blocksize=int(_SAMPLE_RATE * 0.2), callback=_cb,
        )
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


def _open_capture_source():
    """Open the best available mic capture; returns (source, backend_name).
    Raises RuntimeError if no backend can open a stream."""
    order = _BACKEND_ORDER.get(getattr(config, "STT_AUDIO_BACKEND", "auto"),
                               _BACKEND_ORDER["auto"])
    failures = []
    for name in order:
        try:
            src = _WinMMCapture() if name == "winmm" else _SDCapture()
            src.start()
            ralog.log("voice", f"Mic capture backend: {name}")
            return src, name
        except Exception as e:
            failures.append(f"{name}: {e}")
            ralog.log("warn", f"Mic capture backend '{name}' unavailable: {e}")
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
    def _track_noise(self, level: float):
        """Slow-adapt to the room's ambient level (only silence pulls it down)."""
        floor = self._noise
        if level > floor * 3:
            return  # speech - don't let loud audio raise the floor
        self._noise = floor * 0.95 + level * 0.05

    def _speech_level(self, level: float) -> bool:
        return level > max(self._noise * 2.0, 0.006)

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
        """Whisper re-transcribes ONLY the finished phrase's audio (parallel to
        the already-dispatched Vosk text). If the result is meaningfully
        different, tell the assistant so it can upgrade the pending phrase."""
        if not vosk_text or config.STT_FINAL_ENGINE != "whisper" \
                or self.busy_check():
            return
        if audio is None or audio.shape[0] < _SAMPLE_RATE * 0.3:
            return
        try:
            model = _get_whisper_model()
            segments, _info = model.transcribe(
                audio, language="en", beam_size=1, vad_filter=True)
            wtext = " ".join(seg.text for seg in segments).strip()
            if wtext and wtext.lower().strip() != vosk_text.lower().strip():
                ralog.log("voice", f"vosk: '{vosk_text}' -> whisper: '{wtext}'")
                self.on_correct(vosk_text, wtext)
        except Exception as e:
            ralog.log("warn", f"whisper finalize failed ({e}); keeping vosk text")

    # -- streaming ----------------------------------------------------------
    def _finalize_worker(self):
        """Whisper re-transcription runs OFF the audio callback (a CPU-heavy
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
            if text and sustained < self._min_speech:
                # A noise blip (typing, slam, cough) ended the utterance - only
                # dispatched on sustained speech, so it must not act on Ra.
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
        level = float(np.abs(block).mean())
        self._track_noise(level)
        sec = block.shape[0] / _SAMPLE_RATE
        if self._speech_level(level):
            self._clip_block(block)
            self._speech_seconds += sec
        else:
            # Decay faster than it accrues so scattered blips can't sum to
            # "speech" - only genuinely sustained speech passes the gate.
            self._speech_seconds = max(0.0, self._speech_seconds - sec * 2)
        self._feed(block.tobytes())  # never drop audio - Vosk handles silence


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
        self._clip_lock = threading.Lock()
        self._noise = float(getattr(config, "STT_ENERGY_THRESHOLD", 300)) / 32768.0
        self._in_speech = False
        self._silence_since = None
        self._speech_started = 0.0
        self._speech_seconds = 0.0

    # -- capture ----------------------------------------------------------
    def _track_noise(self, level: float):
        floor = self._noise
        if level > floor * 3:
            return
        self._noise = floor * 0.95 + level * 0.05

    def _is_speech(self, level: float) -> bool:
        return level > max(self._noise * 1.7, 0.004)

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
        level = float(np.abs(block).mean())
        self._track_noise(level)
        talking = self._is_speech(level)
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


def start_mic(on_phrase, on_partial=None, busy_check=None, on_correct=None) -> MicSession:
    """Start ALWAYS-ON streaming listening. Returned object exposes `.error` if
    the mic is unavailable. Prefers the vosk streaming path (instant partials +
    whisper finalize); if vosk is missing, misbehaving, or STT_ENGINE=whisper,
    it automatically falls back to the whisper+VAD streamer - in every case the
    mic stays on continuously: no push-to-talk required."""
    global _mic
    with _mic_lock:
        if _mic and _mic.error is None:
            return _mic
        try:
            if config.STT_ENGINE == "whisper":
                _mic = _WhisperVADStream(on_phrase, on_partial,
                                         busy_check=busy_check,
                                         on_correct=on_correct).start()
            else:
                _mic = MicSession(on_phrase, on_partial, busy_check=busy_check,
                                  on_correct=on_correct).start()
        except Exception as e:
            ralog.log("warn", f"vosk streaming unavailable ({e}) - "
                              f"falling back to whisper-VAD always-on.")
            try:
                _mic = _WhisperVADStream(on_phrase, on_partial,
                                         busy_check=busy_check,
                                         on_correct=on_correct).start()
            except Exception as e2:
                ralog.log("err", f"voice unavailable ({e2}) - "
                                 f"push-to-talk uses Whisper")
                _mic = MicSession(on_phrase, on_partial)
                _mic.error = e2 if not getattr(_mic, "error", None) else _mic.error
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


def listen_once(timeout: int = 8, phrase_time_limit: int = 10) -> str:
    """Push-to-talk: listen once and return the recognized phrase."""
    ralog.log("voice", "listening...")
    audio = _record_once(timeout=timeout, pause_after_speech=1.4)
    if audio is None:
        ralog.log("voice", "nothing heard.")
        return ""
    # Push-to-talk favors accuracy: whisper first (same model the streaming
    # path uses to finalize), falling back to vosk if whisper is unavailable.
    try:
        model = _get_whisper_model()
        segments, _info = model.transcribe(audio, language="en", beam_size=1,
                                           vad_filter=True)
        text = " ".join(seg.text for seg in segments).strip()
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