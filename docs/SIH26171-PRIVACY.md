# Ra — Privacy & Security Architecture

**SIH26171 | Team EtherealSpark**

Ra is designed from the ground up with the principle that **the user's data stays on the user's machine**. Every subsystem — vision, voice, RAG, memory, tools — enforces this through multiple overlapping mechanisms.

---

## 1. Core Privacy Principles

1. **Local-first**: All indexing, embedding, retrieval, voice processing, and screen perception happen on-device. No data is sent to any server except the single LLM API call the user explicitly configures.
2. **Consent-gating**: Every data source (files, devices, screen, computer control) requires explicit user grant before Ra will read from or act on it. The user can revoke any source mid-session.
3. **Minimal exposure**: Screenshots are passed to the vision model only for the action in flight, then discarded. Screen content is OCR'd to text locally; raw pixels are never stored persistently.
4. **Transparent control**: The user can inspect and modify what Ra knows at any time via `list_access`, `set_access`, `recall_memories`, and `read_notes`.

---

## 2. Consent-Gating System

Ra maintains a runtime consent map in `config.py`:

```python
GRANTED_ACCESS = {
    "files": True,        # local files / notes
    "devices": True,      # system + audio device info
    "screen": True,       # on-screen text via local OCR
    "computer": True,     # full PC control (shell, keys, mouse, files, clipboard, windows)
}
```

Every tool that touches a data source checks its consent flag before executing. The user controls these via:

- **Voice/UI**: `set_access(area="screen", allowed=False)` — revokes screen access instantly
- **CLI**: `python ra_cli.py rag revoke screen`
- **Runtime default**: `RA_COMPUTER_ACCESS=0` in the environment disables all PC control at boot

When a consent check fails, the tool returns a clear message explaining what access is needed and how to grant it — Ra never silently bypasses the gate.

---

## 3. PII Auto-Redaction (`redact.py`)

Before any user data is indexed or injected into the LLM context, Ra applies regex-based redaction:

| Pattern | Redacted To |
|---|---|
| Email addresses | `[REDACTED_EMAIL]` |
| Phone numbers (various formats) | `[REDACTED_PHONE]` |
| Credit card numbers (16-digit) | `[REDACTED_CARD]` |
| Common secret patterns (API keys, tokens) | `[REDACTED_SECRET]` |

This redaction is applied at the **retrieval layer** — indexed passages are redacted before injection into the LLM context window. The original data remains in the local SQLite index (the user's own machine) but never reaches the remote LLM in unredacted form.

---

## 4. Screen Content — Privacy by Design

### 4.1 Local OCR Only

When the user grants screen access, Ra captures the screen and runs OCR locally to extract text. Only the extracted text is indexed into the local knowledge base. Raw screenshot pixels are:

- Passed to the vision LLM *only* for the current action in flight
- Never stored to disk
- Never indexed
- Never sent to any server other than the configured LLM provider for the active task

### 4.2 Vision Model Interaction

When `gui_do` or `see_screen` is called:

1. A screenshot is captured locally
2. The screenshot is sent to the configured vision-capable LLM (Gemini, Groq, or NIM) as a single image in the tool-calling context
3. The LLM returns structured coordinates/actions
4. The image exists in memory only for the duration of that API call
5. After the action completes, the next screenshot replaces it

No screenshots are accumulated, logged, or transmitted beyond the single active API call.

### 4.3 Post-Action Verification

The `_screen_changed()` function in `vision.py` uses a fast 128×80 thumbnail diff to verify clicks landed — no second LLM call, no additional image transmission.

---

## 5. Voice Pipeline — Local Processing

### 5.1 Speech-to-Text

- **Vosk** (streaming): A small on-device model (`vosk-model-small-en-us-0.15`, ~50MB) runs entirely locally. Audio never leaves the device for streaming recognition.
- **faster-whisper** (correction): The `base.en` model (~150MB) runs locally for final transcription accuracy. Audio is processed on-device.
- **Audio capture**: Microphone audio is captured via `sounddevice` or the Windows WaveIn API — no cloud speech service is involved.

### 5.2 Text-to-Speech

- **edge-tts**: Synthesizes speech using Microsoft's Edge TTS service (requires network for synthesis, but no audio is uploaded — only the text to synthesize).
- **pyttsx3**: Fully offline fallback using Windows SAPI — zero network dependency.
- Voice is `en-GB-RyanNeural` by default (crisp British male).

### 5.3 Conversation Window

Once addressed ("Ra, ..."), the conversation stays open for a configurable window (default 6 hours) without requiring the wake word again. The mic streams continuously but Ra only responds to direct speech — background noise is filtered by `STT_MIN_SPEECH_SECONDS`.

---

## 6. RAG Index — Local Storage

The entire RAG pipeline runs on-device:

| Component | Implementation | Storage |
|---|---|---|
| Embeddings | Hash-based deterministic embeddings (no neural model needed) | In-memory |
| Index | SQLite database | `~/.ra/ra_index.sqlite` |
| Documents | Text extraction from txt, md, pdf, code, csv, docx, xlsx, odt, epub, rtf | Local filesystem |
| Screen captures | OCR text only | SQLite |
| Device snapshots | System info text | SQLite |

No cloud storage, no sync, no external database. The index is a single SQLite file the user can inspect, back up, or delete at any time.

---

## 7. Long-Term Memory

Ra's `remember`/`recall_memories` system stores facts as plain text in a local JSONL file (`~/.ra/notes.txt`). The user can:

- Read all stored notes: `read_notes`
- Search memory: `recall_memories(query)`
- The user is always told what Ra remembers and can ask it to forget

Memory is never synced to any cloud service.

---

## 8. Secrets Management

| Secret | Storage | Never |
|---|---|---|
| LLM API key | Environment variable or `~/.ra/.env` (gitignored) | In source code, in the built `.exe`, in any log |
| Wi-Fi password | Read on-demand via `netsh`, returned to user only | Stored by Ra, logged, or indexed |

The `.env` file is gitignored by default. The build script (`build_exe.bat`) copies the key to `~/.ra/.env` for the standalone executable — the key is never embedded in the binary.

---

## 9. Data Flow Summary

```
User speaks → Vosk (local) → text → Brain (LLM API) → tool call
                                                         │
                                          ┌──────────────┼──────────────┐
                                          ▼              ▼              ▼
                                     Local files    Screen OCR     System
                                     (consent)      (consent)      (consent)
                                          │              │              │
                                          ▼              ▼              ▼
                                     Redact PII → Index (SQLite) → Inject context
                                                                       │
                                                                       ▼
                                                              LLM answers
                                                                       │
                                                                       ▼
                                                              TTS → User hears
```

At no point does raw user data leave the device except:
1. The text of the current question (sent to the configured LLM)
2. A single screenshot image (sent to the vision LLM for the active action only)
3. Text to synthesize (sent to edge-tts for voice output)

---

## 10. Comparison with Browser Extension Approach

The SIH26171 problem statement envisions a browser extension for client-side vision. Ra's approach extends this concept to the *entire desktop*:

| Aspect | Browser Extension | Ra |
|---|---|---|
| Scope | Browser only | Full desktop — browser, native apps, system UI |
| Vision | DOM inspection | Screenshot + LLM vision + UIAutomation tree |
| Privacy | Extension sandbox | Consent-gating + PII redaction + local-only processing |
| Deployment | Browser install | Standalone `Ra.exe` — no browser required |
| Voice | None | Full STT/TTS pipeline |

Ra's architecture is designed so that a future browser extension could be added as an additional perception layer — reading the DOM for web tasks while the vision pipeline handles native UI.

---

*Document prepared for SIH26171 submission by Team EtherealSpark.*
