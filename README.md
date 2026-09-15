# Ra

**Ra** (Retrieval-Augmented Virtual Assistant) is your personal voice-and-vision assistant running on your own PC.

A neural command interface: an LLM brain (Gemini/Groq via your API key) that answers from a **Retrieval-Augmented Generation** index over **your local files** — and takes real actions on your desktop (open apps, control media, click the UI by vision, run system tasks).

## Repo layout

```
Ra/
  src/ra/        the brain: LLM routing, RAG core, voice (STT/TTS), tools +
                 screen vision (pure-stdlib retrieval, SQLite index)
  hud/           the HUD shell: FastAPI bridge + pywebview window, orb,
                 ra.spec (builds Ra.exe)
  tests/         unit tests (pytest, offline)
  ra_cli.py      retrieval CLI: python ra_cli.py rag ... / ask "..."
  run_app.py     desktop assistant entry: python run_app.py
  docs/          design research
  .env.example   secrets template (the API key lives here, never in code)
```

## Quickstart (Windows)

**Easiest:** double-click **`run.bat`** in this folder. A menu lets you start the
voice+HUD assistant (auto-installs missing deps the first time), ask a question,
run a quick index-and-ask demo, or run the test suite. No install step needed
before that.

Or from a terminal:

```bash
# 1. environment: copy the template and add your LLM API key (one provider)
copy .env.example .env
#   Gemini (default): https://aistudio.google.com/apikey -> RA_GEMINI_API_KEY
#   Groq:             https://console.groq.com        -> RA_GROQ_API_KEY
# (the provider is auto-detected from which key is set, or force it with RA_LLM_PROVIDER)

# 2. install runtime deps (RAG core + tests need none; these power the LLM/voice/screen)
pip install -r requirements.txt

# 3. run
python run_app.py                 # voice + text HUD
python ra_cli.py rag index        # index your default docs into the local index
python ra_cli.py rag search "what did we decide about the API key"   # verify retrieval
python ra_cli.py ask "what's my budget note about?"  # live LLM answer with RAG
```

You can also add this folder to your `PATH` and use the `ra` shortcut anywhere:

```powershell
ra ask "beat sheet by 'gem'"      # live LLM answer with RAG
ra rag index --dir "C:\Docs"      # index a folder
ra rag search "screen redaction"  # verify retrieval
```

The retrieval core (embedding, indexing, store, retrieval, CLI, tests) is
**pure Python standard library + SQLite** — it runs and is fully tested without
installing any of the heavier deps above. `python -m pytest` from this folder
runs the Ra tests offline.

## Build a standalone .exe (no Python needed to run)

Double-click **`run.bat` → option 6** (or run `build_exe.bat`). It installs
PyInstaller, copies your API key to `~\.ra\.env` (the exe reads it from
there at runtime — the key is **not** embedded in the exe), and produces:

```
hud\dist\Ra.exe   double-click to launch the HUD (voice + orb assistant)
~\.ra\ra.log      debug output when running without a console
```

To change/rotate the key after building, just edit `~\.ra\.env` and restart
the app — no rebuild needed.

## Data sources & consent

Ra only retrieves from sources you have **granted** access to
(`config.GRANTED_ACCESS`), adjustable in-session with the `set_access` skill or
`python ra_cli.py rag grant|revoke files|devices|screen`:

- **files** — index any folder with `ra_cli.py rag index --dir <path>` (or ask
  the assistant to "index a folder"). Supports txt/md/pdf/code/csv/logs...
- **devices** — system + audio snapshot (`refresh_device_snapshot` /
  `ra_cli.py rag ingest-devices`).
- **screen** — visible desktop; either OCR'd to text only
  (`capture_screen` / `ra_cli.py rag ingest-screen`) or, with computer access
  granted, controlled visually via `gui_do` (screenshot → model decides the next
  click → verified post-action) and named-control clicks via `ui_find`/`ui_click`.

## Privacy & secrets

- Your LLM key (Gemini or Groq) is read from the environment / `.env` (gitignored) — never hardcoded.
- Screen content is OCR'd locally; pixels are passed to the vision model only for the action in flight, never stored.

## What's verified

- ✅ **Ra core: full pytest suite passes** — embedding determinism, chunking,
  SQLite store + persistence, retrieval filtering, brain RAG-context injection
  (with an injected fake LLM), LLM provider routing (gemini/groq), 429 retry/backoff,
  consent gating, CLI, branding/secret hygiene, screen-vision action parsing.
- ✅ **HUD shell**: FastAPI bridge + pywebview window (full/dock/widget/orb modes) built as `Ra.exe`.

## SIH26171 Documentation

Ra is built for ISRO's Smart India Hackathon 26171 — "On-device Visual Perception for Light-weight Browser Agents." Full documentation:

- [**Project Overview & Architecture**](docs/SIH26171-OVERVIEW.md) — system design, SIH26171 alignment, evaluation criteria mapping
- [**Feature & Skills Catalog**](docs/SIH26171-FEATURES.md) — all 85+ tools across 15 categories
- [**Privacy & Security Architecture**](docs/SIH26171-PRIVACY.md) — consent-gating, PII redaction, local-only processing
- [**Technical Deep Dive**](docs/SIH26171-TECHNICAL.md) — brain routing, vision pipeline, DPI, hybrid STT, WebGPU roadmap

## License

MIT (see [`LICENSE`](LICENSE)).