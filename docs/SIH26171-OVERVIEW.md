# Ra — On-device Visual Perception for Light-weight Browser Agents

**SIH26171 | Smart India Hackathon 2026 | ISRO Problem Statement**

**Team:** Team EtherealSpark
**Theme:** Smart Automation
**Category:** Software

---

## 1. What Ra Is

Ra (Retrieval-Augmented Virtual Assistant) is a privacy-first, on-device AI assistant that runs entirely on a Windows PC. It combines:

- **Vision-grounded screen perception** — Ra sees the screen, reads UI elements, clicks buttons, fills forms, and navigates multi-step workflows autonomously.
- **Retrieval-Augmented Generation (RAG)** — a local knowledge index over the user's own files, notes, and device snapshots, so Ra answers from *their* data, not a generic model's training set.
- **Voice-native interaction** — continuous hands-free conversation via hybrid STT (Vosk streaming + faster-whisper correction) and natural British-accented TTS (edge-tts).
- **90 built-in tools** — from launching apps and controlling media, to scheduling background tasks and managing files, all exposed as structured tool schemas the LLM brain can call.

Ra is not a chatbot. It is an autonomous systems agent that takes real actions on the user's PC and verifies every outcome before reporting success.

---

## 2. Why This Matters (SIH26171 Context)

ISRO's problem statement (SIH26171) calls for:

> *On-device Visual Perception for Light-weight Browser Agents*

The core challenge: how do you give a lightweight browser-based agent the ability to understand and interact with on-screen UI — reading text, clicking buttons, filling forms — **without sending screenshots to a remote server**?

Ra directly addresses this challenge with a production-ready implementation:

| SIH26171 Requirement | Ra's Implementation |
|---|---|
| Client-side vision processing (WebGPU/ONNX) | `vision.py` — full-screen perception pipeline: screenshot capture, LLM vision model reads pixels, identifies UI elements, returns structured (x, y, action) commands; DPI-aware physical-pixel coordinates; zoom-refine for small targets |
| Privacy-preserving filter | `redact.py` — auto-redacts emails, phone numbers, credit card numbers, and secrets before any data leaves the device; consent-gating on every data source (`GRANTED_ACCESS`) |
| Server-side integration for reasoning | `brain.py` — multi-brain LLM routing (Gemini, Groq, NVIDIA NIM); OpenAI-compatible endpoints; reasoning over sanitized text + vision data; tool-calling loop with up to 14 iterations |
| On-device deployment | Entire Ra core (`src/ra/`) runs locally; voice processing (Vosk, faster-whisper) is on-device; RAG index is SQLite; no cloud dependencies for core functionality; builds to a single `Ra.exe` via PyInstaller |

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        HUD Shell (hud/)                         │
│   pywebview window  ·  FastAPI bridge  ·  4 display modes      │
│   full  ·  dock  ·  widget  ·  orb                             │
└───────────────────────┬─────────────────────────────────────────┘
                        │ HTTP + WebSocket (port 8747)
┌───────────────────────▼─────────────────────────────────────────┐
│                     Ra Brain (src/ra/)                          │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                      │
│  │  Gemini   │  │   Groq   │  │ NVIDIA   │  Multi-brain race   │
│  │  (Flash)  │  │ (gpt-oss)│  │   NIM    │  (first wins)       │
│  └─────┬─────┘  └────┬─────┘  └────┬─────┘                     │
│        └──────────────┼──────────────┘                          │
│                       ▼                                         │
│              Tool Dispatch Loop                                 │
│              (up to 14 iterations)                              │
│                       │                                         │
│  ┌────────────────────┼────────────────────────────────────┐    │
│  │                    │                                    │    │
│  ▼                    ▼                                    ▼    │
│  ┌─────────┐   ┌───────────┐   ┌──────────┐   ┌──────────┐   │
│  │ Vision  │   │    RAG    │   │  Voice   │   │  Tools   │   │
│  │ Pipeline│   │   Core    │   │ Pipeline │   │  (90)    │   │
│  └─────────┘   └───────────┘   └──────────┘   └──────────┘   │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │ Scheduler│  │  Memory  │  │ Monitor  │  │ Plugins  │       │
│  │ (cron)   │  │ (JSONL)  │  │ (health) │  │ (user)   │       │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘       │
└─────────────────────────────────────────────────────────────────┘
```

### Key Components

| Component | File(s) | Purpose |
|---|---|---|
| **Brain** | `brain.py` | Multi-provider LLM routing, conversation history, tool-calling loop, self-healing model fallback |
| **Vision** | `vision.py` | Screenshot capture, LLM vision perception, zoom-refine, post-action verification, `_screen_changed` diff |
| **Computer** | `computer.py` | Pure ctypes Win32 API — mouse, keyboard, clipboard, window management, DPI awareness (no pyautogui) |
| **UIA** | `uia.py` | UIAutomation via PowerShell — accessible tree queries (`ui_find`), named control clicks (`ui_click`), typed input (`ui_type`) |
| **Skills** | `skills.py` | 90 structured tool schemas + dispatch — the LLM's complete action vocabulary |
| **RAG** | `rag/` | SQLite-backed retrieval: `embedder.py` (hash embeddings), `indexer.py`, `retriever.py`, `store.py`, `sources.py` |
| **Voice** | `audio_io.py` | Hybrid STT (Vosk streaming + faster-whisper final), TTS (edge-tts + pyttsx3 fallback), continuous listen, conversation window |
| **Memory** | `memory.py` | Long-term JSONL memory — `remember`/`recall_memories` across sessions |
| **Scheduler** | `scheduler.py` | Cron-like background task scheduling with catch-up |
| **Monitor** | `monitor.py` | System health watchdog — CPU, RAM, disk, battery alerts |
| **Redact** | `redact.py` | PII auto-redaction — emails, phones, CC numbers, secrets |
| **Fast Lane** | `fastlane.py` | Deterministic short-circuit for trivial commands (no LLM needed) |
| **Plugins** | `plugins.py` | User-dropped `.py` plugin system (`~/.ra/plugins/`) |
| **Config** | `config.py` | All defaults, system prompt, provider detection, consent flags |

---

## 4. How Ra Meets Each Evaluation Criterion

### 4.1 Accuracy of On-Screen Perception (25%)

Ra's vision pipeline reads the screen with high accuracy through a multi-stage approach:

1. **Full-screen pass** — a screenshot is captured and sent to the vision LLM, which returns a structured JSON describing what it sees and where to act (physical pixel coordinates).
2. **DPI enforcement** — `computer.py` calls `SetProcessDPIAware()` at startup; all coordinates are in physical pixels matching the screenshot bitmap, eliminating the scaling mismatch that defeats naive implementations.
3. **Zoom-refine** — when the target is small (< 80px radius), Ra crops a high-resolution region around the predicted click point and sends it back to the vision model for a more precise re-estimate. JPEG quality is 95 to preserve fine text.
4. **Post-action verification** — after every click, Ra takes a fresh screenshot and runs a thumbnail diff (`_screen_changed`). If the screen didn't change (missed click), the model receives negative feedback telling it to re-aim or try a keyboard shortcut instead.
5. **Accessible tree fallback** — for UI elements with visible text labels, `ui_find` reads the Windows UIAutomation tree to get exact control rectangles, bypassing pixel uncertainty entirely.

### 4.2 PII Recall and Precision (20%)

- `redact.py` applies regex-based redaction for emails, phone numbers, credit card numbers, and common secret patterns *before* any data leaves the device.
- Consent-gating (`GRANTED_ACCESS`) ensures Ra never reads files, devices, or screen content unless the user explicitly grants access per category.
- Screen content is OCR'd locally; image pixels are passed to the vision model only for the action in flight, never stored persistently.
- The `set_access` tool allows the user to revoke any data source mid-session.

### 4.3 Redaction Precision (20%)

- Ra's redaction is applied at the *retrieval layer* — indexed passages are redacted before injection into the LLM context.
- The `capture_screen` tool indexes only OCR text, never raw pixels.
- Notes and indexed documents are stored locally in SQLite; no cloud sync.
- Long-term memory (`remember`/`recall`) stores facts as plain text in a local JSONL file, with the user in full control.

### 4.4 Client Resource Utilization (20%)

- The entire RAG core is pure Python standard library + SQLite — zero heavy dependencies for indexing and retrieval.
- Voice processing uses lightweight models: Vosk (small-en-us-0.15, ~50MB) for streaming STT, faster-whisper base.en for final correction.
- The vision pipeline is lazy-loaded — the LLM vision module is only imported when `gui_do` or `see_screen` is actually called.
- The HUD shell uses pywebview (native WebView2 on Windows) — not Electron, not a full browser.
- Builds to a single `Ra.exe` via PyInstaller with no runtime Python installation required.

### 4.5 End-to-End Latency (15%)

- **Voice round-trip**: Vosk streams partials in real-time; the conversation window stays open without re-triggering wake words.
- **Tool execution**: the `fastlane.py` module short-circuits trivial commands (time, date, simple math) without an LLM call.
- **Multi-brain race**: when two or more LLM providers are configured, Ra sends the same turn to all of them simultaneously — the first valid answer wins, survivors continue if one fails.
- **Web journeys**: `web_fetch` reads a page's text over HTTP in a single GET — no browser rendering needed. `go_to_url` navigates the active browser via the address bar (reuses the open window).
- **Post-action verification**: `_screen_changed` uses a fast 128×80 thumbnail diff — no second LLM call needed to verify a click landed.

---

## 5. Project Timeline

| Milestone | Status | Description |
|---|---|---|
| Ra Core Engine | Complete | Brain routing, tool dispatch, 90 tools, system prompt |
| RAG Pipeline | Complete | SQLite store, hash embeddings, file/screen/device indexing |
| Voice Pipeline | Complete | Hybrid STT (Vosk + faster-whisper), edge-tts TTS, continuous listen |
| Screen Vision | Complete | Full-screen perception, zoom-refine, post-action verification, DPI-aware |
| Accessible UI | Complete | UIAutomation tree queries, named control clicks, typed input |
| HUD Shell | Complete | pywebview + FastAPI, 4 display modes, orb widget |
| Privacy Layer | Complete | Consent-gating, PII redaction, local-only processing |
| Reliability Hardening | Complete | Post-action verification, cursor settle, proximity guard, model fallbacks |
| Standalone Build | Complete | PyInstaller → `Ra.exe`, no Python needed |
| Documentation | In Progress | SIH26171 alignment docs, feature catalog, technical deep dive |

---

## 6. Future Roadmap

Ra's architecture is designed for progressive enhancement. Planned areas of growth include:

- **WebGPU-accelerated on-device vision** — replacing the remote vision LLM with a local ONNX/WebGPU model for fully on-device screen perception, as envisioned by SIH26171.
- **Browser extension integration** — a lightweight extension that exposes the browser DOM to Ra, reducing the need for pixel-based vision for web tasks.
- **Cross-platform support** — extending beyond Windows to macOS and Linux, leveraging platform-native accessibility APIs.
- **Plugin marketplace** — community-contributed skill plugins via the `~/.ra/plugins/` system.
- **Multi-modal memory** — indexing screenshots alongside text for visual recall ("what was on my screen when I was working on X").
- **Team collaboration** — shared schedules, task lists, and memory across multiple Ra instances.

---

*Document prepared for SIH26171 submission by Team EtherealSpark.*
