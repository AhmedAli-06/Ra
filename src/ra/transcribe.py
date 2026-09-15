"""
Ra File Transcription
=======================
Offline transcription of a local audio/video file via faster-whisper (already
bundled for Ra's live STT final pass). Used for dictation-style flows
("transcribe this audio"), interview recordings, voice notes - without ever
touching the live microphone pipeline.
"""
import os


def transcribe_file(path: str, max_chars: int = 4000) -> str:
    """Transcribe a local audio/video file. Best-effort and honest: returns a
    readable transcript (truncated to `max_chars`) or a clear reason why not."""
    path = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    if not os.path.isfile(path):
        return f"File not found: {path}"
    if not os.access(path, os.R_OK):
        return f"I can't read that file (permission denied): {path}"
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return ("faster-whisper isn't available in this build - the live STT "
                "final pass needs it too. Install: pip install faster-whisper")
    import time
    from ra import config
    try:
        model = WhisperModel(
            config.WHISPER_MODEL_SIZE,
            device="cpu",
            compute_type="int8",
            download_root=config.DATA_DIR,
        )
    except Exception as e:
        return f"Couldn't load the whisper model: {e}"
    try:
        # Long files stream as segments; cap the total transcript length.
        segments, info = model.transcribe(
            path, language=None, beam_size=1, vad_filter=True)
        parts = []
        for seg in segments:
            parts.append(str(seg.text).strip())
            if sum(len(p) for p in parts) >= max_chars:
                break
            # Keep the daemon responsive to barge-ins while long files run.
            time.sleep(0.0)
        text = " ".join(p for p in parts if p).strip()
    except Exception as e:
        return f"Transcription failed: {e}"
    if not text:
        return "The file had no recognisable speech."
    if len(text) > max_chars:
        text = text[:max_chars] + " ..."
    return text