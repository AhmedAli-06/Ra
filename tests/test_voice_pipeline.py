"""Session 5 voice-pipeline regressions: hands-free conversation, sentence
streaming, barge-in / echo guard. Skipped automatically when the audio deps
(numpy / sounddevice) are absent — keeps the suite installable bare."""

import queue
import threading
import time

import pytest

audio_io = pytest.importorskip("ra.audio_io")
assistant = pytest.importorskip("ra.assistant")


def _reset():
    assistant.gui = None
    assistant._busy.clear()
    assistant._talking.clear()
    assistant._tts_cancel.clear()
    assistant._halt_requested.clear()
    audio_io.clear_speech_stop()
    assistant._conversation_until = 0.0
    assistant._last_spoken = ""
    assistant._corrections.clear()
    assistant._queued_stt.clear()
    while not assistant.command_queue.empty():
        assistant.command_queue.get_nowait()


def test_split_sentences():
    assert audio_io.split_sentences("One. Two. Three") == ["One.", "Two.", "Three"]
    assert audio_io.split_sentences("") == []


def test_whisper_vad_finalize_does_not_deadlock(monkeypatch):
    """Always-on whisper-VAD hearing must survive the end of an utterance:
    _finalize_utterance is called while _process holds the clip lock, so a plain
    Lock deadlocks the mic thread at the FIRST phrase and freezes continuous
    hearing forever (regression for the RLock fix)."""
    import numpy as np

    monkeypatch.setattr(audio_io.config, "STT_PAUSE_THRESHOLD", 0.05)
    monkeypatch.setattr(audio_io.config, "STT_MIN_SPEECH_SECONDS", 0.0)
    # The flat-envelope test blocks are constant-amplitude (deliberately - the
    # regression is about the lock, not the VAD). Disable the modulation gate
    # so the clip is still finalized and queued.
    monkeypatch.setattr(audio_io.config, "STT_MODULATION_THRESHOLD", 0.0)
    # Blocks are constant-amplitude (deliberately - the regression is about
    # the lock, not the VAD). They must still clear the weak-signal floor
    # (~0.005 normalized) so the VAD enters speech; loud but flat works.

    s = audio_io._WhisperVADStream(on_phrase=lambda t: None,
                                   on_partial=lambda t: None)
    s._noise = 0.0
    s._tx_q = queue.Queue()

    def _block(level):
        return np.full(2048, int(level * 1200), dtype=np.int16)

    def _feed_with_pause(blocks):
        for b in blocks:
            s._process(b)
            time.sleep(0.03)  # let the pause gate see real elapsed time

    blocks = [_block(0.5), _block(0.5), _block(0.0001),
              _block(0.0001), _block(0.0001)]
    t = threading.Thread(target=_feed_with_pause, daemon=True, args=(blocks,))
    t.start()
    t.join(timeout=3.0)
    assert not t.is_alive(), "whisper-VAD mic thread deadlocked at phrase end"
    assert not s._clip, "utterance clip was not cleared on finalize"
    assert s._tx_q.qsize() == 1, "finalized audio was not queued for transcription"


def test_pop_sentences_streams():
    rest, sent = assistant._pop_sentences("Aaaa. Bbbb. Cccc")
    assert sent == "Aaaa. Bbbb."
    assert rest == "Cccc"
    rest, sent = assistant._pop_sentences("no terminator yet")
    assert sent == "" and rest == "no terminator yet"


def test_wake_word_opens_hands_free_conversation():
    _reset()
    assistant._on_phrase("what time is it")  # ambient, ignored
    assert assistant.command_queue.empty()
    assistant._on_phrase("Fire up what time is it")
    assert assistant.command_queue.get() == "what time is it"
    assert assistant._conversation_active()
    assistant._on_phrase("and the weather")  # hands-free follow-up, no wake word
    assert assistant.command_queue.get() == "and the weather"


def test_echo_guarded_but_real_barge_in_needs_no_wake_word():
    _reset()
    assistant._busy.set()
    assistant._talking.set()
    stopped = []
    assistant.stop_speaking = lambda: stopped.append(1)
    # Echo: what Ra is saying comes back through the mic -> ignored.
    assistant._last_spoken = "the weather will be clear and sunny today"
    assistant._on_phrase("the weather will be clear")
    assert assistant.command_queue.empty() and not stopped
    # A natural interruption that shares almost no words -> barges in, no wake word.
    assistant._on_phrase("wait actually no")
    assert stopped and assistant._tts_cancel.is_set()
    assert assistant.command_queue.get() == "wait actually no"
    # And a wake word still wins outright even if it echoes oddly.
    stopped.clear()
    assistant._on_phrase("fire up stop")
    assert stopped == [1]
    assert assistant.command_queue.get() == "stop"
    assistant._busy.clear()
    assistant._talking.clear()


def test_barge_in_while_thinking_accepts_any_phrase():
    _reset()
    assistant._busy.set()  # thinking, NOT speaking -> real user speech
    assistant._on_phrase("actually wait")
    assert assistant.command_queue.get() == "actually wait"
    assistant._busy.clear()


def test_stop_command_is_intercepted_without_an_llm_turn(monkeypatch):
    _reset()
    asked = []
    monkeypatch.setattr(assistant.brain, "ask",
                        lambda text, **kw: asked.append(text) or "reply")
    monkeypatch.setattr(audio_io, "_tts_path", lambda text: None)
    monkeypatch.setattr(audio_io, "_play_file", lambda path: None)
    assistant.command_queue.put("stop")
    worker = threading.Thread(target=assistant._processor, daemon=True)
    speaker = threading.Thread(target=assistant._speaker, daemon=True)
    worker.start()
    speaker.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        if not assistant._busy.is_set():
            break
        time.sleep(0.02)
    assert asked == []                 # no LLM call for a stop
    assert assistant._halt_requested.is_set()
    assistant.stop_event.set()
    time.sleep(0.3)
    assistant.stop_event.clear()


def test_whisper_correction_replaces_pending_stt_command(monkeypatch):
    _reset()
    executed = []
    monkeypatch.setattr(assistant.brain, "ask",
                        lambda text, **kw: executed.append(text) or "done.")
    monkeypatch.setattr(audio_io, "speak", lambda text: None)
    # Whisper finished before the processor handled the phrase: upgrade it.
    assistant._on_phrase("fire up open notpa")             # dispatched instantly
    assert assistant.command_queue.get() == "open notpa"
    assistant._queued_stt.add("open notpa")
    assistant._corrections["open notpa"] = "open notepad"  # whisper arrives
    assistant.command_queue.put("open notpa")
    worker = threading.Thread(target=assistant._processor, daemon=True)
    speaker = threading.Thread(target=assistant._speaker, daemon=True)
    worker.start()
    speaker.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        if executed:
            break
        time.sleep(0.02)
    assert executed == ["open notepad"]
    assistant.stop_event.set()
    time.sleep(0.3)
    assistant.stop_event.clear()


def test_processor_speaks_sentences_in_order_and_opens_conversation(monkeypatch):
    _reset()
    spoken = []
    monkeypatch.setattr(audio_io, "_tts_path",
                        lambda text: (spoken.append(text), "fake.mp3")[1])
    monkeypatch.setattr(audio_io, "_play_file", lambda path: None)

    def fake_ask(text, stream=False, on_token=None, on_tool=None, **kwargs):
        for tok in ["First part. ", "Second part! ", "a tail"]:
            if on_token:
                on_token(tok)
        return "First part. Second part! a tail"

    monkeypatch.setattr(assistant.brain, "ask", fake_ask)
    assistant.command_queue.put("hello")
    worker = threading.Thread(target=assistant._processor, daemon=True)
    speaker = threading.Thread(target=assistant._speaker, daemon=True)
    worker.start()
    speaker.start()
    dead = time.time() + 5
    while time.time() < dead:
        if not assistant._busy.is_set() and assistant._tts_queue.empty():
            break
        time.sleep(0.02)
    # Merged chunks: the whole reply streams as one gap-free audio session.
    seen = " ".join(spoken)
    first, second, third = seen.find("First part."), seen.find("Second part!"), seen.find("a tail")
    assert -1 not in (first, second, third)
    assert first < second < third, "sentences must be streamed in order"
    assert assistant._conversation_active()
    assistant.stop_event.set()
    time.sleep(0.3)  # let daemon threads observe the stop flag


# ---------------------------------------------------------------------------
# sherpa-onnx streaming path
# ---------------------------------------------------------------------------
class _FakeStream:
    def __init__(self):
        self.audio = []

    def accept_waveform(self, sr, samples):
        self.audio.append(samples)


class _FakeRecognizer:
    """Stands in for sherpa_onnx.OnlineRecognizer: records accepted audio,
    returns configured partials/finals and an endpoint flag, and counts resets."""

    def __init__(self, partial="", final="hi there", endpoint=False):
        self.partial = partial
        self.final = final
        self.endpoint = endpoint
        self.stream = _FakeStream()
        self.reset_calls = 0
        self.decode_calls = 0

    def create_stream(self):
        return self.stream

    def is_ready(self, stream):
        return self.decode_calls == 0

    def decode_stream(self, stream):
        self.decode_calls += 1

    def get_result(self, stream):
        return self.final

    def is_endpoint(self, stream):
        return self.endpoint

    def reset(self, stream):
        self.reset_calls += 1


def _speech_block(v=0.3):
    """One loud, modulated 0.2s speech-like int16 block (energy well above the
    weak-signal floor and obviously 'talking' on the band gate)."""
    import numpy as np
    n = int(audio_io._SAMPLE_RATE * 0.2)
    t = np.arange(n) / audio_io._SAMPLE_RATE
    tone = (v * 32767.0 * (0.6 * np.sin(2 * np.pi * 220.0 * t)
            + 0.3 * np.sin(2 * np.pi * 440.0 * t))).astype(np.int16)
    return tone


def _silence_block():
    """One 0.2s all-zero int16 block — digital dead air (never 'talking')."""
    import numpy as np
    return np.zeros(int(audio_io._SAMPLE_RATE * 0.2), dtype=np.int16)


def test_sherpa_stream_dispatches_partial_and_phrase(monkeypatch):
    """_SherpaStream must stream live partials from the recognizer, dispatch
    the final when RA's OWN gate (trailing silence) closes the utterance, and
    reset for the next one - even when sherpa is_endpoint() NEVER fires (the
    exact regression that killed continuous hearing)."""
    monkeypatch.setattr(audio_io.config, "STT_MIN_SPEECH_SECONDS", 0.0)
    monkeypatch.setattr(audio_io.config, "STT_FINAL_ENGINE", "sherpa")
    monkeypatch.setattr(audio_io.config, "STT_PAUSE_THRESHOLD", 0.0)
    s = audio_io._SherpaStream(on_phrase=lambda t: None,
                               on_partial=lambda t: None)
    rec = _FakeRecognizer(partial="hi the", final="hi there", endpoint=False)
    s._recognizer = rec
    s._stream = rec.create_stream()
    s._dispatch_q = queue.Queue()
    # One loud speech block opens the gate...
    s._process_block(_speech_block())
    assert rec.decode_calls > 0, "sherpa decode must run after accept_waveform"
    assert s._dispatch_q.empty(), "mid-speech must not dispatch yet"
    # ...then trailing silence beyond STT_PAUSE_THRESHOLD closes it, even with
    # sherpa's is_endpoint() stuck at False (the continuous-hearing bug).
    s._silence_since = time.time() - 5.0
    s._process_block(_silence_block())
    assert s._dispatch_q.qsize() == 1, "gate must dispatch the final phrase"
    assert s._dispatch_q.get() == "hi there"
    assert rec.reset_calls == 1, "stream must reset after the gate closes"


def test_sherpa_stream_ignores_short_noise_phrase(monkeypatch):
    """Sustained-speech gate must still drop a noise blip even though the
    trailing-silence boundary fired - the blip never reaches on_phrase."""
    monkeypatch.setattr(audio_io.config, "STT_MIN_SPEECH_SECONDS", 0.4)
    monkeypatch.setattr(audio_io.config, "STT_FINAL_ENGINE", "sherpa")
    monkeypatch.setattr(audio_io.config, "STT_PAUSE_THRESHOLD", 0.0)
    s = audio_io._SherpaStream(on_phrase=lambda t: None,
                               on_partial=lambda t: None)
    rec = _FakeRecognizer(final="tip tap", endpoint=True)
    s._recognizer = rec
    s._stream = rec.create_stream()
    s._dispatch_q = queue.Queue()
    s._process_block(_speech_block())
    s._silence_since = time.time() - 5.0
    s._process_block(_silence_block())
    assert s._dispatch_q.empty(), "noise blip must not be dispatched"


def test_sherpa_stream_enqueues_offline_refinement(monkeypatch):
    """With STT_FINAL_ENGINE=parakeet a finalized phrase must also be queued
    for the offline parakeet refinement pass on the captured clip."""
    monkeypatch.setattr(audio_io.config, "STT_MIN_SPEECH_SECONDS", 0.0)
    monkeypatch.setattr(audio_io.config, "STT_FINAL_ENGINE", "parakeet")
    monkeypatch.setattr(audio_io.config, "STT_PAUSE_THRESHOLD", 0.0)
    s = audio_io._SherpaStream(on_phrase=lambda t: None,
                               on_partial=lambda t: None)
    rec = _FakeRecognizer(final="hi there", endpoint=True)
    s._recognizer = rec
    s._stream = rec.create_stream()
    s._dispatch_q = queue.Queue()
    s._fin_q = queue.Queue()
    s._process_block(_speech_block())
    s._silence_since = time.time() - 5.0
    s._process_block(_silence_block())
    assert s._dispatch_q.qsize() == 1
    assert s._fin_q.qsize() == 1, "parakeet final pass must be queued"