# Ra Research Notes — Open-Source Assistant Feature Survey

Consolidated from 10 parallel web searches across the open-source JARVIS /
agent / voice-assistant / RAG landscape. Every entry names the project, the
feature, and the corresponding Ra implementation (existing or new).

## 1. JARVIS-style voice assistants

| Project | Distinctive feature | Ra implementation |
|---|---|---|
| InterGenJLU/jarvis | One-file plugin system: a Python file with `register()` auto-discovered from a `skills/` dir | NEW `ra/plugins.py` — `~/.ra/plugins/*.py`, each exposing `register()` |
| InterGenJLU/jarvis | MemGPT-pattern memory: extract facts, inject into prompts | Existing `ra/memory.py` |
| rishaadj/JARVIS | Planner -> Executor -> Evaluator task loop | NEW `ra/tasklist.py` (persistent task plan) |
| sharmakrishna1010/Jarvis-AI | ChromaDB long-term memory + sliding window | `memory.py` (JSONL) equivalent |
| momorzq-oss/JARVIS | Fast rule-based command lane (deterministic short-circuit) | Existing `ra/fastlane.py` |
| momorzq-oss/JARVIS | Cron-like scheduled agents | NEW `ra/scheduler.py` |

## 2. Computer use / autonomy

| Project | Distinctive feature | Ra implementation |
|---|---|---|
| Open Interpreter | `computer.display.view()`, `computer.mouse.click`, browser search, editor highlight | Existing `see_screen` / `gui_do` / `run_command` |
| ONEPUNCHMAN411/Jarvis | UI-automation accessibility tree (not pixel clicks) | Partial — window/process APIs; pixel `gui_do` |
| rishaadj/JARVIS | Autonomous goal agents + visual observer | Planned |

## 3. TTS / voice cloning

| Project | Distinctive feature | Ra implementation |
|---|---|---|
| CriTTS | edge-tts + Piper + Coqui XTTS v2, 100+ voices | edge-tts voice (existing); Piper/XTTS optional add    |
| fish-speech | S2 Pro 4B, Dual-AR, sub-word prosody tags | Out of scope (GPU-heavy) |
| Home Assistant Wyoming | Piper + openWakeWord + whisper satellite | OpenWakeWord optional; whisper STT already final-stage |

## 4. RAG / documents

| Project | Distinctive feature | Ra implementation |
|---|---|---|
| RAG-From-Scratch | Semantic chunking, query rewrite, cross-encoder rerank | Fixed-size chunks now + doc readers |
| FAST (high-quality-hub) | Flask RAG with PDF/DOCX/TXT/MD upload | NEW docx/xlsx/odt/epub/rtf readers in `indexer.py` |
| isair/jarvis | Knowledge-graph memory + tool router | Keyword memory (existing) |
| riddle/search | PageIndex (PDF page-aware retrieval) | Optional |

## 5. Scheduling / proactive

| Project | Distinctive feature | Ra implementation |
|---|---|---|
| cronai | YAML human-readable cron (`every hour`, `task:`, `notify:`) | NEW `ra/scheduler.py` (JSON schedules) |
| KAIROS | Proactive heartbeat daemon + memory consolidation | scheduler.py + monitor morning_briefing |
| TinMan | Heartbeat presets (sane/paranoid/chaos), check-list care | monitor gates + NEW spoken alerts |
| boo / cx | Missed-schedule catch-up, desktop notifications | scheduler `catch_up` policy + toasts |

## 6. Vision / OCR / accessibility

| Project | Distinctive feature | Ra implementation |
|---|---|---|
| Vis Aware | AI-Agent mode: screen -> LLM -> action; 7 OCR engines | Existing `vision.py` `gui_do` + `see_screen` |
| Navigator | dHash visual feedback, reflection/self-critique, undo action | Planned |
| Clawd Cursor | Accessibility-first + OCR + vision tiered perception | Window/process + OCR (existing) |

## 7. Privacy / hygiene

| Project | Distinctive feature | Ra implementation |
|---|---|---|
| isair/jarvis | Sensitive info auto-redaction | NEW `ra/redact.py` wired into memory + tool logs |

## 8. Adopted into this session (10.5)

- `indexer.py` — DOCX/XLSX/ODT/EPUB/RTF/CSV text extraction (stdlib zipfile + XML).
- `plugins.py` — auto-discovered one-file user plugins (InterGenJLU pattern).
- `scheduler.py` — persistent scheduled tasks, background daemon (cronai/KAIROS/boo).
- `tasklist.py` — persistent to-do plan with statuses (rishaadj planner).
- `monitor.py` — proactive spoken check-ins on top of toast gates (TinMan/KAIROS).
- `redact.py` — secrets/email/phone/CC masking before memory + logs (isair/jarvis).
- `transcribe.py` — batch transcription of local audio/video via faster-whisper (dictation-style flows without touching the live mic pipeline).