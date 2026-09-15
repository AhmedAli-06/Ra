"""
Ra - Personal Systems Agent
===============================
The unified assistant: an always-on streaming microphone (instant phrase events,
live partials, whisper-accurate final transcription), a natural hands-free
conversation mode (address Ra once, then just keep talking), sentence-
streamed speech (Ra starts answering while the rest is still generating),
a single command queue with true barge-in (interrupts cancel the speech), real
PC control, and a running log pane.

Run:  python run_app.py
"""
import collections
import os
import queue
import re
import sys
import threading
import time

from ra import config
from ra import logging as ralog
from ra import brain
from ra.audio_io import (
    speak_stream, listen_once, start_mic, stop_mic, stop_speaking,
    clear_speech_stop,
)


class _TranscriptSink:
    """Optional event sink (Tk GUI, HUD bridge, tests). Defaults to silent.
    Implementations may provide some/all of: post_status, post_message,
    post_partial, post_log."""

    def post_status(self, text: str, key: str): ...
    def post_message(self, who: str, text: str): ...
    def post_partial(self, text: str): ...
    def post_log(self, level: str, message: str): ...


stop_event = threading.Event()
command_queue: queue.Queue = queue.Queue()
gui = None
_transcript = _TranscriptSink()
_busy = threading.Event()          # a command is being processed / spoken
_talking = threading.Event()  # TTS audio is actually playing (echo guard)
_tts_cancel = threading.Event()    # barge-in: cut the current reply short
_halt_requested = threading.Event()  # barge-in / "stop": abort the LLM+tool work
_tts_queue: queue.Queue = queue.Queue()  # speakable sentence chunks
_active_stream = None                     # current incremental TTS session
_mic_session = None
_conv_lock = threading.Lock()
_conversation_until = 0.0          # time.monotonic() deadline for hands-free mode
_last_spoken = ""                  # reply text currently spoken-streamed (echo compare)
_spoken_ring = collections.deque(maxlen=4)   # rolling window of recently-spoken chunks
_spoken_lock = threading.Lock()
_corrections: dict = {}            # vosk text -> whisper-corrected text (pending)
_queued_stt: set = set()           # STT-originated commands awaiting a correction

_SENT_END = re.compile(r"[.!?\u2026]")

# "Ra, stop" / "abort" / "shut up" / "never mind" — kill the reply instantly
# instead of sending another LLM turn. 'stop' MUST match even mid-reply.
_STOP_RE = re.compile(
    r"^\s*(?:you\s+|please\s+)?(?:stop|halt|abort|cancel|"
    r"shut\s*(?:up|it)|be\s+quiet|quiet|silence|enough|freeze|"
    r"stand\s*down|never\s*mind|forget\s*it|cut\s+(?:that|it)"
    r"(?:\s*out)?)\b.*$",
    re.I,
)


def set_status(text: str, key: str):
    try:
        _transcript.post_status(text, key)
    except Exception:
        pass
    if gui:
        gui.post_status(text, key)


def log_to_gui(level: str, message: str):
    try:
        _transcript.post_log(level, message)
    except Exception:
        pass
    if gui:
        gui.post_log(level, message)


def _post_message(who: str, text: str):
    try:
        _transcript.post_message(who, text)
    except Exception:
        pass
    if gui:
        gui.post_message(who, text)


def _post_partial(text: str):
    try:
        _transcript.post_partial(text)
    except Exception:
        pass
    if gui:
        gui.post_partial(text)


def attach_sink(sink) -> None:
    """Attach an event sink (HUD bridge, GUI, tests) that receives status,
    message, partial, and log events from the engine. Only one sink at a time;
    pass None to detach."""
    global _transcript
    _transcript = sink if sink is not None else _TranscriptSink()


_WAKE_WORDS = tuple(w.lower() for w in config.WAKE_WORDS)
_WAKE_PAT = re.compile(
    r"\b(?:hey|ok|okay|yo|please)?\.?,?\s*(" + "|".join(map(re.escape, _WAKE_WORDS)) + r")\b",
    flags=re.I,
)


def _strip_wake(text: str) -> str:
    return _WAKE_PAT.sub("", text).strip(" .,:!?")


def _contains_wake(text: str) -> bool:
    return _WAKE_PAT.search(text) is not None


# ---------------------------------------------------------------------------
# Hands-free conversation mode
# ---------------------------------------------------------------------------
def _conversation_active() -> bool:
    with _conv_lock:
        return time.monotonic() < _conversation_until


def _extend_conversation():
    """Every exchange refreshes the hands-free window: keep talking naturally."""
    global _conversation_until
    with _conv_lock:
        window = max(getattr(config, "CONVERSATION_WINDOW", 120.0), 5.0)
        was_active = time.monotonic() < _conversation_until
        _conversation_until = time.monotonic() + window
    if not was_active:
        ralog.log("voice", "hands-free conversation open - no wake word needed.")


def _set_idle_status():
    if config.CONTINUOUS_LISTEN:
        if _conversation_active():
            set_status("Listening", "listening")
        else:
            set_status(f"Say \"{config.WAKE_WORDS[0].title()}\"", "idle")
    else:
        set_status("Idle", "idle")


# ---------------------------------------------------------------------------
# Sentence-streamed speech
# ---------------------------------------------------------------------------
def _pop_sentences(buffer: str):
    """Split complete sentences off the front of the streaming buffer."""
    last = 0
    for m in _SENT_END.finditer(buffer):
        last = m.end()
    if not last:
        return buffer, ""
    return buffer[last:].lstrip(), buffer[:last]


def _remember_spoken(text: str):
    """Record a spoken chunk into the rolling echo window (most recent only), so
    the echo guard compares the heard phrase against what Ra JUST said instead
    of the whole accumulated reply (which would swallow real barge-ins that
    reuse earlier words)."""
    global _last_spoken
    with _spoken_lock:
        _spoken_ring.append(text)
        _last_spoken = " ".join(_spoken_ring)


def _clear_spoken():
    global _last_spoken
    with _spoken_lock:
        _spoken_ring.clear()
        _last_spoken = ""


def _speaker():
    """Feeds streamed reply text into ONE incremental TTS session per reply,
    so audio generation flows continuously across sentence-batches (no dead
    air between sentences/commas/paragraphs). A barge-in cancel cuts the
    current audio instantly and the next command starts a fresh session."""
    global _active_stream
    while not stop_event.is_set():
        try:
            item = _tts_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            if item is None:
                if _active_stream is not None:
                    try:
                        _active_stream.close()
                    except Exception:
                        pass
                    _active_stream = None
                    _clear_spoken()
                    _talking.clear()
                continue
            if _tts_cancel.is_set():
                continue
            if _active_stream is None:
                set_status("Speaking", "speaking")
                _talking.set()
                _active_stream = speak_stream(cancel_event=_tts_cancel)
            _remember_spoken(item)
            _active_stream.push(item)
        finally:
            _tts_queue.task_done()


# ---------------------------------------------------------------------------
# Command queue worker
# ---------------------------------------------------------------------------
def _processor():
    """One command at a time, in order. Streams the reply to the HUD live and
    hands completed sentences to the speaker thread as they arrive, so Ra
    starts talking while the rest of the answer is still being generated."""
    global _active_stream
    while not stop_event.is_set():
        try:
            text = command_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        if not text:
            continue
        # Whisper-correction grace: STT phrases are dispatched on the Vosk text
        # instantly, but whisper usually produces a better transcript within a
        # second or two. Wait just long enough to catch it before acting, so
        # commands are both snappy AND accurate.
        key = text.strip().lower()
        if key in _queued_stt:
            _queued_stt.discard(key)
            if key in _corrections:
                text = _corrections.pop(key)
            else:
                corrected = _wait_correction(key)
                if corrected:
                    text = corrected
        _busy.set()
        # A fresh command supersedes any stale streamed speech (barge-in).
        if _active_stream is not None:
            try:
                _active_stream.close()
            except Exception:
                pass
            _active_stream = None
            _clear_spoken()
            _talking.clear()
        set_status("Thinking", "thinking")
        _post_partial("")
        holder = {"buffer": ""}

        def _on_token(t: str):
            holder["buffer"] += t
            rest, sentences = _pop_sentences(holder["buffer"])
            if sentences:
                holder["buffer"] = rest
                _tts_queue.put(sentences)

        # Note: spoken "fire up stop" already has the wake word stripped; a typed
        # "ra stop" is stripped here too so both paths intercept instantly.
        is_stop = bool(_STOP_RE.match(_strip_wake(text)))
        if is_stop:
            # "Ra, stop": abort instantly - no LLM turn, no tools. Kill the
            # current audio, drop any queued leftover speech, then confirm
            # crisply without burning an LLM call.
            _tts_cancel.set()
            _halt_requested.set()
            stop_speaking()
            _drain_tts()
            _tts_cancel.clear()
            clear_speech_stop()
            reply = "Stopped."
            _tts_queue.put("Stopped.")
        else:
            _tts_cancel.clear()
            _halt_requested.clear()
            clear_speech_stop()
            try:
                reply = brain.ask(text, stream=True, on_token=_on_token,
                                  on_tool=_on_tool, halt_event=_halt_requested)
            except Exception as e:
                ralog.log("err", f"brain error: {type(e).__name__}: {e}")
                reply = "I hit a snag while thinking. Check the logs and try again."
            if _halt_requested.is_set():
                # A barge-in / 'stop' killed this reply - don't speak leftovers.
                _tts_cancel.set()

        if holder["buffer"].strip() and not _tts_cancel.is_set():
            _tts_queue.put(holder["buffer"])
        _post_partial("")
        _post_message(config.ASSISTANT_NAME, reply)
        _tts_queue.put(None)   # sentinel: no more sentences for this reply
        _tts_queue.join()      # wait until everything has been spoken

        # Always extend the conversation after a reply - keeps the hands-free
        # window alive as long as the user is engaged.
        _extend_conversation()

        # Barge-in: if anything arrived while we were busy, keep the busy flag
        # set and let the next loop iteration pick it up immediately.
        if not command_queue.empty():
            continue
        _busy.clear()
        _set_idle_status()


def _on_tool(name: str, args: dict, result: str):
    set_status(f"Calling {name}", "tooling")


def _wait_correction(key: str, base_timeout=None):
    """Block up to the correction grace period for a whisper transcript of an
    STT phrase. Returns the corrected text or None (proceed with Vosk text)."""
    from ra import config as _cfg
    from ra import audio_io
    if getattr(_cfg, "STT_FINAL_ENGINE", "whisper") != "whisper":
        return None
    if not getattr(audio_io, "whisper_available", False):
        return None  # model not loaded yet - don't stall on the first phrase
    grace = base_timeout if base_timeout is not None else float(
        getattr(_cfg, "STT_CORRECTION_GRACE", 3.0))
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if key in _corrections:
            return _corrections.pop(key)
        time.sleep(0.05)
    return None


# ---------------------------------------------------------------------------
# Voice policy
# ---------------------------------------------------------------------------
def _is_echo(text: str) -> bool:
    """True when the heard phrase is mostly Ra's own words (mic heard the
    speakers), so we don't interrupt our own speech. Real user speech shares
    few tokens with the sentence currently being spoken, so it barges in."""
    spoken = _last_spoken or ""
    spoken_tokens = set(re.findall(r"[a-z']+", spoken.lower()))
    heard_tokens = set(re.findall(r"[a-z']+", (text or "").lower()))
    if not heard_tokens or not spoken_tokens:
        return False
    overlap = len(heard_tokens & spoken_tokens) / len(heard_tokens)
    return overlap >= 0.6


def _barge_in(text: str):
    """Interrupt the current reply: kill the audio, abort the in-flight LLM +
    tool work, drop leftover sentences, and queue the new command."""
    _post_message("You", f"(barge-in) {text}")
    _tts_cancel.set()
    _halt_requested.set()
    stop_speaking()
    _drain_tts()
    stripped = _strip_wake(text)
    command_queue.put(stripped if stripped else text)
    _queued_stt.add(stripped if stripped else text)


def _drain_tts():
    """Drop all queued (stale) spoken-sentence chunks after an abort, so a
    cancelled reply never finishes talking once 'stop' has been obeyed."""
    while True:
        try:
            _tts_queue.get_nowait()
        except queue.Empty:
            return
        _tts_queue.task_done()


def _on_correct(original: str, corrected: str):
    """Whisper found a better transcript for an already-dispatched Vosk phrase:
    remember it so the processor can upgrade the command before acting."""
    _corrections[original.strip().lower()] = corrected
    ralog.log("voice", f"corrected: '{original}' -> '{corrected}'")


def _on_phrase(text: str):
    lower = text.lower().strip()
    if not lower:
        return
    ralog.log("voice", f"heard: {text}")
    if _busy.is_set():
        if _talking.is_set():
            # Natural barge-in: a wake word always wins; otherwise accept any
            # speech that isn't an echo of what Ra is currently saying.
            if _contains_wake(lower) or not _is_echo(text):
                _barge_in(text)
            return
        _barge_in(text)
        return
    if _contains_wake(lower):
        remainder = _strip_wake(text)
        command_queue.put(remainder if remainder else lower)
        _queued_stt.add(remainder if remainder else lower)
        _extend_conversation()
    elif _conversation_active():
        # Hands-free follow-up: no wake word needed.
        command_queue.put(text)
        _queued_stt.add(text)
        _extend_conversation()
    # else: ambient chatter while idle - politely ignored.


def _on_partial(text: str):
    _post_partial(f"… {text}")


def _mic_worker():
    """Owns the microphone lifecycle (so the ALWAYS-ON toggle actually starts
    and stops it) and refreshes the HUD status."""
    global _mic_session
    while not stop_event.is_set():
        if config.CONTINUOUS_LISTEN and _mic_session is None:
            try:
                _mic_session = start_mic(
                    on_phrase=_on_phrase, on_partial=_on_partial,
                    busy_check=_talking.is_set,
                    on_correct=_on_correct)
                if getattr(_mic_session, "error", None):
                    ralog.log("warn", f"always-on listening unavailable "
                                      f"({_mic_session.error}); you can still "
                                      f"tap the mic and type.")
                else:
                    ralog.log("ok", f"microphone online - say \"{config.WAKE_WORDS[0].title()}, ...\"")
            except Exception as e:
                ralog.log("err", f"microphone start failed: {e}")
        elif not config.CONTINUOUS_LISTEN and _mic_session is not None:
            stop_mic()
            _mic_session = None
            ralog.log("voice", "microphone stopped (always-on is OFF).")
        if not _busy.is_set():
            _set_idle_status()
        stop_event.wait(1.0)


def _mic_push_to_talk():
    def _run():
        set_status("Listening", "listening")
        heard = listen_once(timeout=8, phrase_time_limit=12)
        if heard:
            command_queue.put(heard)
            _extend_conversation()
        else:
            _set_idle_status()
    threading.Thread(target=_run, daemon=True).start()


def _typed(text: str):
    text = text.strip()
    if text:
        command_queue.put(text)


def on_wake_toggle(active: bool):
    config.CONTINUOUS_LISTEN = active
    ralog.log("ok", f"Always-on listening is now {'ON' if active else 'OFF'}.")


def on_computer_toggle(active: bool):
    from ra.computer import set_computer_access
    set_computer_access(active)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main():
    from ra import ensure_utf8_console
    ensure_utf8_console()

    if os.environ.get("RA_SELFTEST") == "1" or "--selftest" in sys.argv:
        import numpy  # noqa: F401
        from ra.config import get_provider, get_llm_model
        try:
            from ra import audio_io
            _src, backend = audio_io._open_capture_source()
            import numpy as _np
            peak = 0
            _t0 = time.time()
            while time.time() - _t0 < 1.2:  # confirm real audio powers on
                _b = _src.next_block(0.2)
                if _b is not None and _b.shape[0]:
                    _p = int(_np.abs(_b).max())
                    if _p > peak:
                        peak = _p
            _src.close()
            audio = f"{backend} (peak {peak})"
        except Exception as e:
            audio = f"UNAVAILABLE ({e})"
        try:
            import edge_tts  # noqa: F401
            tts = config.EDGE_TTS_VOICE
        except Exception:
            tts = "(edge-tts missing)"
        stt_loaded = []
        try:
            audio_io._get_vosk()
            stt_loaded.append("vosk")
        except Exception as e:
            stt_loaded.append(f"vosk-FAIL({type(e).__name__})")
        if config.STT_FINAL_ENGINE == "whisper":
            try:
                audio_io._get_whisper_model()
                stt_loaded.append("whisper")
            except Exception as e:
                stt_loaded.append(f"whisper-FAIL({type(e).__name__})")
        brains = config.configured_brains()
        brains_txt = "+".join(brains) if brains else "none"
        mode_txt = "race" if config.multi_brain_enabled() else "single"
        print(
            f"Ra selftest OK | provider={get_provider()} model={get_llm_model()} "
            f"| brains={brains_txt} ({mode_txt}) "
            f"| index={'ready' if os.path.exists(config.RAG_INDEX_PATH) else 'empty'} "
            f"| tts={tts} stt={'+'.join(stt_loaded) or 'NONE'} "
            f"| micInput={audio}"
        )
        return

    global gui
    from ra.gui import RaGUI

    ralog.register(log_to_gui)

    gui = RaGUI(
        on_submit=_typed,
        on_mic=_mic_push_to_talk,
        on_wake_toggle=on_wake_toggle,
        on_computer_toggle=on_computer_toggle,
        assistant_name=config.ASSISTANT_NAME,
    )
    try:
        from ra.config import get_provider, get_llm_model
        gui.set_model(f"{get_provider()}/{get_llm_model()}  •  {config.EDGE_TTS_VOICE}")
    except Exception:
        pass

    gui.post_message(
        config.ASSISTANT_NAME,
        'I am online and I am always listening. '
        + (
            'Just talk to me - no wake word needed, and I will keep the '
            'conversation flowing all session.'
            if getattr(config, "VOICE_CONVERSATION", False)
            else f'Say "{config.WAKE_WORDS[0].title()}" to talk, then just keep chatting - you will '
                 'not need to repeat my name. Say "stop" to cut me off.'
        ),
    )
    set_status("Starting", "idle")

    if getattr(config, "VOICE_CONVERSATION", False):
        _extend_conversation()   # live voice chat from boot, no wake word
        ralog.log("ok", "live voice chat on - just talk, no wake word needed")
    elif config.CONTINUOUS_LISTEN:
        wake_echo = ", ".join(config.WAKE_WORDS)
        ralog.log("ok", f"always listening - say '{wake_echo}' once, then keep talking")

    threading.Thread(target=_processor, daemon=True, name="ra-processor").start()
    threading.Thread(target=_speaker, daemon=True, name="ra-speaker").start()
    threading.Thread(target=_mic_worker, daemon=True, name="ra-voice").start()
    try:
        from ra import scheduler
        scheduler.ensure_running()   # scheduled background tasks from boot
        n = len(scheduler._read()) if hasattr(scheduler, "_read") else 0
        if n:
            ralog.log("ok", f"scheduler daemon running with {n} schedule(s)")
    except Exception as e:
        ralog.log("warn", f"scheduler start skipped: {e}")
    ralog.log("ok", f"Initialized. Wake word: '{', '.join(config.WAKE_WORDS)}' | computer control: {config.GRANTED_ACCESS.get('computer', False)}")

    try:
        gui.run()
    finally:
        stop_event.set()
        _tts_cancel.set()
        stop_speaking()
        stop_mic()


if __name__ == "__main__":
    main()