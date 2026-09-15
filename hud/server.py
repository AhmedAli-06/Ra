"""Ra HUD — FastAPI server bridging the web UI to Ra's brain, tools, TTS, and STT."""
import asyncio
import json
import os
import sys
import threading
import time
import typing
from pathlib import Path

# Ensure ra package is importable (dev mode only; frozen exe bundles it)
_RA_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _RA_SRC not in sys.path and os.path.isdir(_RA_SRC):
    sys.path.insert(0, _RA_SRC)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from ra import config
from ra import brain
from ra.audio_io import (
    speak_stream, listen_once, stop_mic,
    stop_speaking, clear_speech_stop,
)

app = FastAPI(title="Ra HUD")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _capture_loop():
    global _server_loop
    _server_loop = asyncio.get_event_loop()
    # Always-on voice (Ra's default): actually start the mic loop so the
    # orb "hears" without needing focus for a PTT key.
    if getattr(config, "CONTINUOUS_LISTEN", False):
        _set_continuous(True)

# ── State ────────────────────────────────────────────────────────────────
_state = {"status": "idle", "reply": ""}
_state_lock = threading.Lock()
_log_path = Path.home() / ".ra" / "ra.log"

# ONE conversation at a time: brain._history is a shared module-level list,
# so two overlapping asks (rapid double-send, voice racing text) corrupt the
# history and produce garbled replies. Every talk path honours this lock.
_talk_lock = threading.Lock()

# Continuous-listening (always-on voice) mode
_ws_clients: typing.Set[WebSocket] = set()
_ws_lock = threading.Lock()
_server_loop: typing.Optional[asyncio.AbstractEventLoop] = None
_continuous_stop = threading.Event()
_continuous_busy = threading.Event()
_continuous_thread: typing.Optional[threading.Thread] = None

# Emergency stop / barge-in: shared by every talk path (voice, text, PTT).
# Setting them aborts the in-flight brain.ask and cuts the current audio.
_halt_requested = threading.Event()
_tts_cancel = threading.Event()

def _interrupt_now():
    """Abort whatever is in flight right now - the /api/stop button or a
    voice "Ra, stop" mid-reply. Safe from any thread at any time."""
    _halt_requested.set()
    _tts_cancel.set()
    stop_speaking()
    _broadcast_sync({"type": "status", "status": "idle"})
    _broadcast_sync({"type": "reply", "reply": "Stopped.", "voice": True})

# ── HTML ─────────────────────────────────────────────────────────────────
def _html_path() -> Path:
    here = Path(__file__).resolve().parent
    candidate = here / "app.html"
    if candidate.exists():
        return candidate
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "app.html"
        if bundled.exists():
            return bundled
    return candidate


@app.get("/", response_class=HTMLResponse)
async def index():
    return _html_path().read_text(encoding="utf-8")


def _orb_path() -> Path:
    here = Path(__file__).resolve().parent
    candidate = here / "orb.html"
    if candidate.exists():
        return candidate
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "orb.html"
        if bundled.exists():
            return bundled
    return candidate


@app.get("/orb", response_class=HTMLResponse)
async def orb_page():
    return _orb_path().read_text(encoding="utf-8")


# ── Broadcast to all connected HUDs ─────────────────────────────────────
async def _broadcast_async(payload: dict, except_ws: typing.Optional[WebSocket] = None):
    dead = []
    with _ws_lock:
        clients = list(_ws_clients)
    for ws in clients:
        if ws is except_ws:
            continue
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    if dead:
        with _ws_lock:
            for ws in dead:
                _ws_clients.discard(ws)


def _broadcast_sync(payload: dict, except_ws: typing.Optional[WebSocket] = None):
    loop = _server_loop
    if loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _broadcast_async(payload, except_ws), loop
        )
    except Exception:
        pass


def _broadcast_subtitle(user: str, ra: str, except_ws: typing.Optional[WebSocket] = None):
    if not getattr(config, "HUD_SUBTITLES", True):
        return
    _broadcast_sync({
        "type": "subtitle",
        "user": user,
        "ra": ra,
        "ts": time.time(),
    }, except_ws=except_ws)


# ── Continuous (always-on) voice loop ────────────────────────────────────
def _voice_worker(text: str):
    """Run one voice turn on its own thread so the mic stays live: while Ra
    is answering, a second "Ra, stop" can still be heard and cut in."""
    reply = ""
    try:
        reply = _do_talk(text, speak=True)
    finally:
        from ra.assistant import _extend_conversation
        # Every exchange refreshes the hands-free window: keep talking with
        # no wake word while the user is engaged.
        try:
            _extend_conversation()
        except Exception:
            pass
        if reply:
            _broadcast_sync({"type": "reply", "reply": reply, "voice": True})
        _broadcast_sync({"type": "status", "status": "idle"})
        _continuous_busy.clear()


def _process_voice(text: str):
    """Handle an always-on-heard phrase: wake-gated, spoken, broadcast."""
    _continuous_busy.set()
    _broadcast_sync({"type": "status", "status": "thinking"})
    threading.Thread(target=_voice_worker, args=(text,), daemon=True,
                     name="ra-hud-voice-turn").start()


def _handle_phrase(text: str):
    lower = (text or "").strip().lower()
    if not lower:
        return
    from ra.assistant import (_contains_wake, _strip_wake, _is_echo,
                              _conversation_active, _STOP_RE)
    if _continuous_busy.is_set():
        # Mid-reply: only an emergency "Ra, stop"-style interrupt is allowed -
        # everything else is ignored so ambient noise never cuts Ra off.
        if _contains_wake(lower) and _STOP_RE.match(_strip_wake(text) or lower):
            _interrupt_now()
        return
    if _contains_wake(lower):
        _process_voice(_strip_wake(text) or lower)
    elif getattr(config, "VOICE_CONVERSATION", False) or _conversation_active():
        if not _is_echo(text):
            _process_voice(text)


def _continuous_worker():
    from ra.audio_io import start_mic
    from ra.assistant import _on_correct
    try:
        session = start_mic(
            on_phrase=_handle_phrase,
            on_partial=None,
            busy_check=_continuous_busy.is_set,
            on_correct=_on_correct,
        )
        if getattr(session, "error", None):
            _broadcast_sync({"type": "error", "error": f"mic unavailable ({session.error})"})
    except Exception as e:
        _broadcast_sync({"type": "error", "error": f"mic start failed: {e}"})
        return
    _broadcast_sync({"type": "status", "status": "idle"})
    while not _continuous_stop.is_set():
        _continuous_stop.wait(1.0)
    try:
        stop_mic()
    except Exception:
        pass


def _set_continuous(enabled: bool):
    global _continuous_thread
    if enabled:
        _continuous_stop.clear()
        if _continuous_thread is not None and _continuous_thread.is_alive():
            return {"ok": True, "running": True}
        _continuous_thread = threading.Thread(
            target=_continuous_worker, daemon=True, name="ra-hud-voice"
        )
        _continuous_thread.start()
        return {"ok": True, "running": True}
    _continuous_stop.set()
    return {"ok": True, "running": False}


# ── Talk (blocking, runs in thread) ──────────────────────────────────────
def _do_talk(text: str, speak: bool = True) -> str:
    """Run brain.ask streaming and feed the reply into ONE incremental TTS
    session as complete sentences arrive, so Ra starts talking while the rest
    of the answer is still being generated (no dead air, natural cadence).
    Every transition, token and status is broadcast so ALL windows (HUD +
    floating orb) stay in sync. Serialized: overlapping asks would corrupt the
    shared conversation history."""
    from ra.assistant import (_STOP_RE, _strip_wake, _pop_sentences)
    if not _talk_lock.acquire(blocking=False):
        return "I'm still finishing the previous request - give me a moment."
    try:
        _halt_requested.clear()
        _tts_cancel.clear()
        clear_speech_stop()
        with _state_lock:
            _state["status"] = "thinking"
        _broadcast_sync({"type": "status", "status": "thinking"})

        is_stop = bool(_STOP_RE.match(_strip_wake(text)))
        if is_stop:
            # "Ra, stop": abort instantly - no LLM turn, no tools. Kill the
            # current audio, then confirm crisply without an LLM call.
            _halt_requested.set()
            _tts_cancel.set()
            stop_speaking()
            _tts_cancel.clear()
            clear_speech_stop()
            reply = "Stopped."
            if speak:
                try:
                    s = speak_stream(cancel_event=_tts_cancel)
                    s.push("Stopped.")
                    s.close()
                except Exception:
                    pass
        else:
            holder = {"buffer": ""}
            tts = {"stream": None}

            def _on_token(t: str):
                holder["buffer"] += t
                # Live: frontend + orb see the reply as it is being written.
                _broadcast_sync({"type": "token", "token": t,
                                 "buffer": holder["buffer"]})
                if _tts_cancel.is_set():
                    return
                rest, sentences = _pop_sentences(holder["buffer"])
                if sentences:
                    holder["buffer"] = rest
                    if speak:
                        if tts["stream"] is None:
                            with _state_lock:
                                _state["status"] = "speaking"
                            _broadcast_sync({"type": "status", "status": "speaking"})
                            tts["stream"] = speak_stream(cancel_event=_tts_cancel)
                        tts["stream"].push(sentences)

            def _on_tool(name, args, result):
                pass  # could forward tool events

            try:
                reply = brain.ask(text, stream=True, on_token=_on_token,
                                  on_tool=_on_tool, halt_event=_halt_requested)
            except Exception as e:
                reply = f"Error: {type(e).__name__}: {e}"

            if _halt_requested.is_set():
                # An interrupt (stop button / voice barge-in) killed this
                # reply - _interrupt_now already broadcast "Stopped.".
                _tts_cancel.set()
                reply = ""
            else:
                rest = holder["buffer"].strip()
                if speak and rest and not _tts_cancel.is_set():
                    if tts["stream"] is None:
                        with _state_lock:
                            _state["status"] = "speaking"
                        _broadcast_sync({"type": "status", "status": "speaking"})
                        tts["stream"] = speak_stream(cancel_event=_tts_cancel)
                    tts["stream"].push(rest)
                if speak and tts["stream"] is not None:
                    try:
                        tts["stream"].close()
                    except Exception:
                        pass

        if reply:
            _broadcast_subtitle(text, reply)
        with _state_lock:
            _state["status"] = "idle"
            _state["reply"] = reply
        _broadcast_sync({"type": "status", "status": "idle"})
        return reply
    finally:
        _talk_lock.release()


@app.post("/api/talk")
async def api_talk(payload: dict):
    text = (payload.get("text") or "").strip()
    speak = payload.get("speak", True)
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)
    loop = asyncio.get_event_loop()
    reply = await loop.run_in_executor(None, _do_talk, text, speak)
    return {"reply": reply, "status": "idle"}


@app.post("/api/stop")
async def api_stop():
    """Emergency stop from the HUD: abort generation + speech instantly."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _interrupt_now)
    return {"ok": True, "status": "stopped"}


# ── Talk with WebSocket streaming ────────────────────────────────────────
@app.websocket("/ws/talk")
async def ws_talk(ws: WebSocket):
    await ws.accept()
    with _ws_lock:
        _ws_clients.add(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "error": "invalid json"})
                continue

            text = (msg.get("text") or "").strip()
            speak = msg.get("speak", True)
            if not text:
                await ws.send_json({"type": "error", "error": "empty text"})
                continue

            # _do_talk serializes internally and broadcasts status/tokens/
            # reply to every HUD + orb; this socket just awaits the turn.
            loop = asyncio.get_event_loop()
            reply = await loop.run_in_executor(None, _do_talk, text, speak)
            if reply:
                await ws.send_json({"type": "reply", "reply": reply})
            await ws.send_json({"type": "done"})

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        with _ws_lock:
            _ws_clients.discard(ws)


# ── Continuous / push-to-talk mic mode ───────────────────────────────────
@app.post("/api/micmode")
async def api_micmode(payload: dict):
    enabled = bool(payload.get("enabled", False))
    result = _set_continuous(enabled)
    return result


# ── Push-to-talk mic ─────────────────────────────────────────────────────
@app.post("/api/mic")
async def api_mic(payload: dict = None):
    timeout = 8
    phrase_time_limit = 12
    if payload:
        timeout = payload.get("timeout", timeout)
        phrase_time_limit = payload.get("phrase_time_limit", phrase_time_limit)

    loop = asyncio.get_event_loop()
    with _state_lock:
        _state["status"] = "listening"
    _broadcast_sync({"type": "status", "status": "listening"})

    heard = await loop.run_in_executor(
        None, listen_once, timeout, phrase_time_limit
    )

    with _state_lock:
        _state["status"] = "idle"
    _broadcast_sync({"type": "status", "status": "idle"})

    if not heard:
        return {"heard": "", "status": "idle"}

    # Auto-submit to brain
    reply = await loop.run_in_executor(None, _do_talk, heard, True)
    return {"heard": heard, "reply": reply, "status": "idle"}


# ── Status ───────────────────────────────────────────────────────────────
@app.get("/api/status")
async def api_status():
    with _state_lock:
        return {"status": _state["status"]}


# ── Logs ─────────────────────────────────────────────────────────────────
@app.get("/api/logs")
async def api_logs(payload: dict = None):
    lines = 100
    if payload:
        lines = payload.get("lines", lines)
    if not _log_path.exists():
        return {"logs": []}
    try:
        text = _log_path.read_text(encoding="utf-8", errors="replace")
        all_lines = text.strip().splitlines()
        return {"logs": all_lines[-lines:]}
    except Exception as e:
        return {"logs": [f"Error reading log: {e}"]}


# ── Settings ─────────────────────────────────────────────────────────────
@app.post("/api/settings")
async def api_settings(payload: dict):
    results = []

    if "continuous_listen" in payload:
        val = bool(payload["continuous_listen"])
        config.CONTINUOUS_LISTEN = val
        _set_continuous(val)
        results.append(f"continuous_listen={'ON' if val else 'OFF'}")

    if "voice_conversation" in payload:
        val = bool(payload["voice_conversation"])
        config.VOICE_CONVERSATION = val
        results.append(f"voice_conversation={'ON' if val else 'OFF'}")

    if "computer_access" in payload:
        val = bool(payload["computer_access"])
        config.GRANTED_ACCESS["computer"] = val
        results.append(f"computer_access={'ON' if val else 'OFF'}")

    if "wake_words" in payload:
        words = payload["wake_words"]
        if isinstance(words, str):
            words = [w.strip() for w in words.split(",") if w.strip()]
        if words:
            config.WAKE_WORDS = [w.lower() for w in words]
            results.append(f"wake_words={config.WAKE_WORDS}")

    if "tts_voice" in payload:
        config.EDGE_TTS_VOICE = payload["tts_voice"]
        results.append(f"tts_voice={config.EDGE_TTS_VOICE}")

    if "hud_subtitles" in payload:
        val = bool(payload["hud_subtitles"])
        config.HUD_SUBTITLES = val
        results.append(f"hud_subtitles={'ON' if val else 'OFF'}")

    return {"ok": True, "applied": results}


@app.get("/api/config")
async def api_config():
    return {
        "assistant_name": config.ASSISTANT_NAME,
        "wake_words": config.WAKE_WORDS,
        "continuous_listen": config.CONTINUOUS_LISTEN,
        "voice_conversation": getattr(config, "VOICE_CONVERSATION", False),
        "computer_access": config.GRANTED_ACCESS.get("computer", False),
        "tts_voice": config.EDGE_TTS_VOICE,
        "hud_subtitles": getattr(config, "HUD_SUBTITLES", True),
        "provider": config.get_provider(),
        "model": config.get_llm_model(),
        "brains": config.configured_brains(),
        "multi_brain": config.multi_brain_enabled(),
    }


# ── Health ───────────────────────────────────────────────────────────────
@app.get("/api/health")
async def api_health():
    return {"ok": True}
