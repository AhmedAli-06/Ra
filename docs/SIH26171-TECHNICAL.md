# Ra — Technical Deep Dive

**SIH26171 | Team EtherealSpark**

This document covers the engineering details behind Ra's key subsystems: the multi-brain LLM router, the vision perception pipeline, DPI handling, the accessible UI layer, the hybrid voice pipeline, and the path toward fully on-device WebGPU vision.

---

## 1. Multi-Brain LLM Router (`brain.py`)

Ra can race up to three LLM providers simultaneously:

```
User turn → ┌─── Gemini (gemini-3.6-flash) ──────┐
             ├─── Groq (openai/gpt-oss-120b) ─────┤ → First valid answer wins
             └─── NVIDIA NIM (nemotron-3-super) ──┘
```

### How it works

1. The user's message + system prompt + tool schemas (~9.2k tokens total) are sent to all configured providers simultaneously.
2. Each provider streams its response. The first to return a valid tool call or text answer is accepted.
3. If a provider fails (429 rate limit, 404 model not found, timeout), the others continue. Survivors carry on.
4. The winning provider's response is used. If all fail, Ra reports the error honestly.

### Self-healing model fallback

Hosted models are decommissioned without notice. Ra maintains fallback lists per provider:

```python
NIM_MODEL_FALLBACKS = (
    "nvidia/nemotron-3-super-120b-a12b",
    "openai/gpt-oss-20b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
)
GEMINI_MODEL_FALLBACKS = (
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
    "gemini-3.6-flash",
)
```

When a model returns 404/401, Ra retries the same turn on the next candidate and remembers the working model for the rest of the session — no manual intervention needed.

### Tool-calling loop

The brain runs a standard tool-calling loop with up to `MAX_TOOL_ITERATIONS = 14` rounds. Each round:

1. LLM receives the conversation + tool schemas
2. LLM returns a tool call (name + JSON input)
3. `skills.execute_tool()` dispatches to the matching function
4. Result is appended to the conversation
5. Loop continues until the LLM returns a text answer (no more tool calls)

The system prompt (`config.SYSTEM_PROMPT`) instructs the LLM to "finish every job end to end" and "verify before claiming success" — the model is expected to call multiple tools in sequence until the task is complete.

---

## 2. Vision Perception Pipeline (`vision.py`)

Ra's screen perception is the core of SIH26171 alignment. Here is the full pipeline:

### 2.1 Full-Screen Pass

```
Screenshot (local) → Resize to 1280px wide → Send to vision LLM → Structured JSON response
```

The vision LLM receives the screenshot and returns:
```json
{
  "description": "What is on screen",
  "actions": [
    {"type": "click", "x": 542, "y": 318, "label": "File menu"},
    {"type": "type", "text": "search query"}
  ]
}
```

Coordinates are in **physical pixels** — this is critical and enforced by DPI awareness (see Section 3).

### 2.2 Zoom-Refine

When the target is small (< 80px radius from center):

```
Full-screen pass → Predicted click point (x, y)
    → Crop 320×320 region around (x, y) at full native resolution
    → JPEG quality 95 (preserves fine text)
    → Send cropped image to vision LLM
    → Refined (x, y) in physical pixels
```

This two-pass approach lets Ra read small text, icon labels, and fine UI elements that would be blurry in a downscaled full-screen image.

### 2.3 Click Execution

After the vision model returns coordinates:

```python
# computer.py — pure ctypes Win32
SetCursorPos(physical_x, physical_y)  # move mouse
time.sleep(0.04)                       # let cursor settle (prevents drift)
mouse_event(MOUSEEVENTF_LEFTDOWN)      # press
mouse_event(MOUSEEVENTF_LEFTUP)        # release
```

The 40ms settle delay after `SetCursorPos` prevents the cursor from drifting when the OS hasn't finished the move before the click fires.

### 2.4 Post-Action Verification

After every click:

```
Click → Wait 0.8s → Take fresh screenshot → _screen_changed(old, new)
```

`_screen_changed()` uses a fast thumbnail diff:
- Both screenshots are resized to 128×80
- Pixel-by-pixel absolute difference is computed
- If > 1% of pixels changed (tolerance=30 per channel), the screen is considered "changed"
- Cursor and caret regions are ignored (they always change)

If the screen **didn't change** (missed click):
- The vision model receives negative feedback: "The screen did not change after the click. The target may be in a different position."
- The model can re-aim, try a keyboard shortcut, or use `ui_find` on the next iteration

If the screen **did change**:
- The same-spot counter resets
- The model proceeds with the next step of the task

### 2.5 Proximity Guard

When consecutive clicks land on the same spot (within 40px), Ra fires a proximity guard:
- "You have clicked near the same spot multiple times without the screen changing."
- "Try: 1) a keyboard shortcut 2) ui_find to read the accessible tree 3) a different region"

This prevents the vision model from getting stuck in a dead loop.

### 2.6 Vision Loop

The full `gui_do` loop runs up to `VISION_MAX_STEPS = 16` iterations:

```
for step in range(16):
    screenshot → vision LLM → action → execute → verify
    if task_complete: break
    if screen_changed: continue (next step)
    if screen_unchanged: negative feedback → re-aim
```

---

## 3. DPI Awareness (`computer.py`)

Windows reports different values for "logical" vs "physical" pixels depending on the display scaling (125%, 150%, 200%, etc.). A naive screenshot is in physical pixels, but `GetCursorPos` returns logical pixels — a mismatch that causes clicks to land in the wrong place.

Ra solves this at startup:

```python
def _set_dpi_awareness():
    """Call SetProcessDPIAware() once at startup."""
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
```

After this call, **all coordinates are in physical pixels** — consistent across `SetCursorPos`, `GetCursorPos`, screenshot dimensions, and the vision model's coordinate output.

This is verified in tests:
```python
def test_physical_pixel_consistency():
    # Screenshot width == physical screen width (not logical)
    assert screenshot_width == physical_width
```

---

## 4. Accessible UI Layer (`uia.py`)

For UI elements with visible text labels, Ra bypasses pixel-based vision entirely and uses the Windows UIAutomation tree:

### 4.1 `ui_find(name, region)`

PowerShell script wraps the `UIAutomation` COM object:
1. Gets the `AutomationElement` for the foreground window
2. Walks the tree looking for controls whose `Name` contains the search string
3. Returns each match's `Name`, `ControlType`, and `BoundingRectangle` (physical pixels)

This gives Ra the **exact screen position** of any named control — no guessing, no vision model needed.

### 4.2 `ui_click(name, double)`

1. Calls `ui_find(name)` to locate the control
2. Computes the center of the `BoundingRectangle`
3. Uses `SetCursorPos` + `mouse_event` to click that exact center
4. For double-click (needed to play tracks in Spotify), fires two click cycles

### 4.3 `ui_type(name, text)`

1. Calls `ui_find(name)` to locate the text field
2. Clicks the field to focus it
3. Sets the `ValuePattern.Value` property directly (no keystroke simulation)
4. Falls back to clipboard paste if the app doesn't expose `ValuePattern`

### 4.4 When to use which

| Scenario | Tool | Why |
|---|---|---|
| Button with text label | `ui_click("Save")` | Exact, no pixels, works behind DPI |
| Song title in Spotify | `ui_click("Loser", double=True)` | Double-click plays the track |
| Search field | `ui_type("Search", "query")` | Value pattern, no keystroke simulation |
| Small unnamed icon | `ui_find("row text")` → `gui_do` | Find the row's rectangle first, then click the icon beside it |
| Complex multi-step UI | `gui_do("press File then Save")` | Vision handles the whole workflow |

---

## 5. Hybrid Voice Pipeline (`audio_io.py`)

### 5.1 STT Architecture

```
Microphone → sounddevice/waveint capture → Ring buffer (30s rolling)
    │
    ├──→ Vosk (streaming) → Partial text (instant)
    │         │
    │         └── Phrase end detected → Buffer audio chunk
    │                                       │
    │                                       ▼
    │                              faster-whisper (base.en)
    │                                       │
    │                                       ▼
    └────────────────────────────────── Final text (accurate)
```

**Why hybrid?** Vosk's small model is fast but inaccurate for complex speech. The tiny model gets the timing right (when a phrase ends), and faster-whisper re-transcribes the buffered audio for accuracy. The result is both fast *and* accurate.

### 5.2 Key parameters

| Parameter | Value | Purpose |
|---|---|---|
| `VOSK_MODEL_ID` | `vosk-model-small-en-us-0.15` | Streaming recognition (~50MB) |
| `WHISPER_MODEL_SIZE` | `base.en` | Final correction (~150MB) |
| `STT_MIN_SPEECH_SECONDS` | 0.4 | Filters background noise blips |
| `STT_CORRECTION_GRACE` | 0.8s | Max wait for whisper before acting on Vosk text |
| `STT_UTTERANCE_BUFFER_SECONDS` | 30 | Rolling buffer for re-transcription |

### 5.3 TTS

- **Primary**: `edge-tts` — Microsoft's Edge TTS service, voice `en-GB-RyanNeural`, rate `-8%` for smoother cadence
- **Fallback**: `pyttsx3` — Windows SAPI, fully offline
- Speech is streamed and interruptible — the user can say "fire up" again and Ra stops speaking

### 5.4 Conversation Semantics

Once the user says "Ra, ..." (or any configured wake word), the conversation window opens:
- Default window: 6 hours (21600 seconds)
- Every exchange refreshes the window
- "Ra, goodbye" or extended silence closes the session
- No wake word needed within the window — plain speech is understood

---

## 6. RAG Core (`rag/`)

### 6.1 Architecture

```
Documents → index_directory() → chunk (1200 chars, 200 overlap)
    → HashEmbedder (512-dim deterministic) → SQLite store
    → retrieve(query, k=4) → Top-K passages → Inject into LLM context
```

### 6.2 Key design decisions

| Decision | Rationale |
|---|---|
| Hash embeddings (no neural model) | Zero dependencies, deterministic, instant — no GPU needed, no model download |
| SQLite store | Single file, portable, zero configuration, ACID |
| Fixed-size chunks (1200 chars) | Simple, predictable, works across all document types |
| `RAG_MIN_SCORE = 0.22` | Ignores weak matches to avoid injecting irrelevant noise |
| `RAG_TOP_K = 4` | Enough context for most questions without bloating the prompt |

### 6.3 Supported document formats

The `indexer.py` module handles: `.txt`, `.md`, `.py`, `.js`, `.ts`, `.json`, `.csv`, `.log`, `.pdf`, `.docx`, `.xlsx`, `.odt`, `.epub`, `.rtf`, and more.

---

## 7. Fast Lane (`fastlane.py`)

For trivial commands that don't need an LLM:

```python
# Pattern: "what time is it" → get_time() directly
# Pattern: "what's 2+2" → calculate("2+2") directly
# Pattern: "good morning" → morning_briefing() directly
```

The fast lane saves LLM API calls, reduces latency to near-zero, and works even when no API key is configured.

---

## 8. Plugin System (`plugins.py`)

Following the InterGenJLU/jarvis pattern:

```
~/.ra/plugins/
    my_tool.py      # Must expose register() → (tool_schema, handler_fn)
    another.py
```

At startup, Ra scans `~/.ra/plugins/`, calls each `register()`, and merges the returned tool schemas + handlers into the runtime tool set. Broken plugins are logged and skipped — they never take down the app.

---

## 9. Standalone Build

```
python -m PyInstaller --noconfirm --clean hud/ra.spec
    → hud/dist/Ra.exe     (single executable, ~30MB)
    → reads API key from ~/.ra/.env
    → no Python installation needed on target machine
```

The build script (`build_exe.bat`) handles everything: installs PyInstaller, copies the API key, runs the build, and reports the output path.

---

## 10. Path to WebGPU On-Device Vision

The current architecture sends screenshots to a remote vision LLM. The natural evolution toward fully on-device processing (as envisioned by SIH26171) is:

### 10.1 Current: Remote Vision LLM

```
Screenshot → API call to Gemini/Groq/NIM → Coordinates back
```

### 10.2 Future: Local ONNX/WebGPU Model

```
Screenshot → Local ONNX model (WebGPU inference) → Coordinates back
```

### 10.3 Integration points

Ra's vision pipeline is already structured for this swap:

1. `vision.py` captures the screenshot locally
2. The vision model is called via a single function (`_call_vision_model`)
3. The response is parsed into structured coordinates

Replacing the remote call with a local ONNX model requires only changing `_call_vision_model` — all downstream logic (zoom-refine, DPI handling, post-action verification) remains identical.

### 10.4 Candidate models

- **UI-TARS** (ByteDance) — 2B params, designed for GUI grounding
- **CogAgent** (Tsinghua) — 18B, high-accuracy GUI understanding
- **MiniCPM-V** — multimodal, suitable for screen perception

### 10.5 WebGPU readiness

The HUD shell already uses WebView2 (Chromium-based), which supports WebGPU. A future version could run the ONNX model directly in the browser context:

```
Browser → WebGPU context → ONNX Runtime Web → Local vision model
```

This would enable a browser extension that performs on-device screen perception without any native code — exactly the architecture SIH26171 envisions.

---

## 11. Test Suite

Ra's test suite (220 tests, all passing) covers:

| Area | Coverage |
|---|---|
| RAG embedding | Determinism, dimension, consistency |
| RAG chunking | Size, overlap, edge cases |
| RAG store | SQLite persistence, upsert, delete |
| RAG retrieval | Filtering, scoring, top-K |
| Brain routing | Provider detection, failover, multi-brain |
| Tool schemas | Validation, required fields |
| Consent gating | Grant/revoke enforcement |
| Branding | `ASSISTANT_NAME == "Ra"`, wake word, version |
| Screen vision | Action parsing, DPI, post-action verification |
| Fast lane | Pattern matching, short-circuit |
| CLI | Index, search, grant, revoke |

---

*Document prepared for SIH26171 submission by Team EtherealSpark.*
