"""
Ra Configuration
====================
Ra's intelligence runs on up to THREE hosted LLM brains that race in
parallel (see multi_brain_enabled). Each is an OpenAI-compatible endpoint:

- **Gemini** (default when a key is present) — get an API key from
  https://aistudio.google.com/apikey, then set `RA_GEMINI_API_KEY`.
- **Groq** (fast, free-tier cloud AI) — get a key at
  https://console.groq.com -> API Keys, then set `RA_GROQ_API_KEY`.
- **NVIDIA NIM** (third brain) — get a key at
  https://build.nvidia.com (keys look like `nvapi-...`), then set
  `RA_NIM_API_KEY` (or the conventional `NVIDIA_API_KEY`).

Multi-brain mode is enabled by default when two or more keys are present:
all configured brains receive the same turn at once and the first to give a
valid answer wins the race; if one or two fail, the survivors keep working.
Flip it off with `RA_MULTI_BRAIN=0`.

Secrets live in the environment or a gitignored `.env` file (see
ra/.env.example) — never in code.
"""
import os

# --- access & data directories -------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _env(name: str, default: str = "") -> str:
    """Read `name` from the environment, falling back to the pre-rebrand
    (R-A-G-V-A underscore) spelling so older .env files and shell exports
    keep working until they are renamed to the RA_* names."""
    val = os.environ.get(name)
    if val is None and name.startswith("RA_") and not name.startswith("RA_"):
        val = os.environ.get("RA_" + name[3:])
    return default if val is None else val


_data_dir = _env("RA_DATA_DIR")
if not _data_dir:
    _home = os.path.expanduser("~")
    _data_dir = os.path.join(_home, ".ra")
    _legacy_dir = os.path.join(_home, ".ra")
    if not os.path.exists(_data_dir) and os.path.isdir(_legacy_dir):
        try:
            import shutil
            shutil.copytree(_legacy_dir, _data_dir, dirs_exist_ok=True)
        except Exception:
            pass
DATA_DIR = _data_dir
os.makedirs(DATA_DIR, exist_ok=True)

# --- local .env loading (secrets stay out of code / out of git) -----------
def _load_dotenv():
    candidates = [
        os.path.join(REPO_ROOT, ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(DATA_DIR, ".env"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


# --- LLM provider selection ----------------------------------------------
LLM_PROVIDERS = ("gemini", "groq", "nim")


def get_provider() -> str:
    """Active LLM provider. Explicit RA_LLM_PROVIDER wins; otherwise infer:
    a Gemini key is present -> gemini, else groq."""
    explicit = _env("RA_LLM_PROVIDER", "").strip().lower()
    if explicit:
        if explicit not in LLM_PROVIDERS:
            raise RuntimeError(
                f"Unknown RA_LLM_PROVIDER '{explicit}'. Choose from {LLM_PROVIDERS}."
            )
        return explicit
    if _env("RA_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    return "groq"


def get_api_key() -> str:
    """Groq API key (used only by the groq provider)."""
    key = _env("RA_GROQ_API_KEY") or os.environ.get("GROQ_API_KEY") or ""
    if not key:
        raise RuntimeError(
            "No Groq API key found. Set RA_GROQ_API_KEY in your environment or add "
            "it to a .env file (see ra/.env.example)."
        )
    return key


def get_gemini_api_key() -> str:
    """Gemini API key (used only by the gemini provider)."""
    key = _env("RA_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    if not key:
        raise RuntimeError(
            "No Gemini API key found. Set RA_GEMINI_API_KEY in your environment or add "
            "it to a .env file (see ra/.env.example)."
        )
    return key


def get_nim_api_key() -> str:
    """NVIDIA NIM API key (used only by the nim provider, keys look like nvapi-...)."""
    key = _env("RA_NIM_API_KEY") or _env("NVIDIA_API_KEY") or ""
    if not key:
        raise RuntimeError(
            "No NVIDIA NIM API key found. Set RA_NIM_API_KEY (or NVIDIA_API_KEY) in "
            "your environment or add it to a .env file (see ra/.env.example)."
        )
    return key


def gemini_available() -> bool:
    """True when a Gemini key is configured (used for provider failover)."""
    return bool(_env("RA_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY"))


def groq_available() -> bool:
    """True when a Groq key is configured (used for provider failover)."""
    return bool(_env("RA_GROQ_API_KEY") or os.environ.get("GROQ_API_KEY"))


def groq_enabled() -> bool:
    """Groq races by default ONLY when the key belongs to a tier that can
    carry Ra's real request. Free-tier Groq caps at ~8000 input tokens/min,
    but Ra's system prompt (~1.8k) + 80 tool schemas (~7.4k) already exceed
    that before the user message - every real turn 413s ('Request too large').
    Ra therefore leaves Groq out of the race (its failures would be noise) and
    keeps the fast, live-verified Gemini + NIM. Opt back in with RA_GROQ_ENABLE=1
    when the Groq account is upgraded (Dev tier or paid)."""
    return groq_available() and _env("RA_GROQ_ENABLE", "").strip().lower() in ("1", "on", "yes", "true", "multi")


def nim_available() -> bool:
    """True when an NVIDIA NIM key is configured (used for the multi-brain race)."""
    return bool(_env("RA_NIM_API_KEY") or _env("NVIDIA_API_KEY"))


def configured_provider_count() -> int:
    """How many LLM brains are configured (>= 2 enables multi-brain by default).
    Groq counts only when groq_enabled() - a free-tier Groq key alone must not
    count as half a brain."""
    return sum((gemini_available(), groq_enabled(), nim_available()))


def multi_brain_enabled() -> bool:
    """True to race every configured brain simultaneously (first valid answer
    wins, survivors carry on if any fail). Explicit RA_MULTI_BRAIN=0 forces a
    single brain; otherwise enabled whenever two or more keys are present."""
    explicit = _env("RA_MULTI_BRAIN", "").strip().lower()
    if explicit in ("0", "off", "no", "false", "single"):
        return False
    if explicit in ("1", "on", "yes", "true", "multi"):
        return True
    return configured_provider_count() >= 2


def configured_brains() -> list:
    """Names of every configured brain, in a stable order. Groq participates
    only when groq_enabled() - its free tier can't fit Ra's real payload."""
    out = []
    if gemini_available():
        out.append("gemini")
    if groq_enabled():
        out.append("groq")
    if nim_available():
        out.append("nim")
    return out


# --- LLM -----------------------------------------------------------------
# Note: free-tier qwen models have a ~7000 input-token/minute cap that the
# full 80-tool schema alone can exceed - gpt-oss-120b accepts the whole
# payload and does tool calling. Override any time with RA_GROQ_MODEL.
GROQ_MODEL = _env("RA_GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GEMINI_MODEL = _env("RA_GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
# NVIDIA NIM hosted catalog (OpenAI-compatible). Keys from build.nvidia.com.
# Default model verified live with the multi-brain key; a faster variant is
# openai/gpt-oss-20b but it can return empty answers. Override with RA_NIM_MODEL.
#
# The default below is a CHAT + tool-calling model verified live against the
# multi-brain key. (The previous default, "z-ai/glm-5.3-flash", is listed in the
# catalog yet answers every chat call with 404 "Not found for account" - it made
# the NIM brain fail, and print a traceback, on EVERY turn.) Several catalog
# entries are hosted only for partner keys, so a model name is never assumed to
# work: if a brain answers 404/401 for its model, Ra parks it on the long
# cooldown instead of re-hitting it each turn.
NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
NIM_MODEL = _env("RA_NIM_MODEL", "nvidia/nemotron-3-super-120b-a12b")
MAX_TOKENS = 1400
MAX_TOOL_ITERATIONS = 14


def model_for(provider: str) -> str:
    """The model name for a given brain/provider. An explicit RA_LLM_MODEL
    wins for every brain (the user takes the wheel); otherwise each provider
    uses its own model constant."""
    explicit = _env("RA_LLM_MODEL")
    if explicit:
        return explicit
    if provider == "gemini":
        return GEMINI_MODEL
    if provider == "nim":
        return NIM_MODEL
    return GROQ_MODEL


# Verified-live sibling models per brain, used for SELF-HEALING. Hosted models
# are decommissioned without notice (the old NIM default answered 404, and
# gemini-2.5/2.0 are already gone for new keys), so no single model name can be
# trusted forever: when one 404s, Ra retries the same turn on the next entry and
# remembers the one that worked for the rest of the session.
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
GROQ_MODEL_FALLBACKS = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
)


def model_candidates(provider: str) -> list:
    """Every model worth trying for `provider`, the configured one FIRST.

    Drives self-healing: a brain whose configured model no longer exists for
    this account moves to the next candidate instead of being lost for the whole
    session. An explicit RA_LLM_MODEL is still tried first - the user's choice
    only gets overridden when that exact model is rejected."""
    fallbacks = {
        "gemini": GEMINI_MODEL_FALLBACKS,
        "groq": GROQ_MODEL_FALLBACKS,
        "nim": NIM_MODEL_FALLBACKS,
    }.get(provider, ())
    out = []
    for name in (model_for(provider),) + tuple(fallbacks):
        if name and name not in out:
            out.append(name)
    return out


def get_llm_model() -> str:
    """Model for the active provider, resolved lazily so env changes take effect."""
    return model_for(get_provider())

# --- Identity ---------------------------------------------------------------
ASSISTANT_NAME = "Ra"                 # "Ra" - so very proper
# Wake words (comma-separated, any casing): say one to open a hands-free
# conversation that then flows for the whole session without repeating it.
# Override: RA_WAKE_WORDS=fire up,ra
WAKE_WORDS = [w.strip().lower() for w in
              _env("RA_WAKE_WORDS", "fire up").split(",") if w.strip()]
WAKE_WORD = WAKE_WORDS[0] if WAKE_WORDS else "fire up"  # deprecated single-word alias
LAUNCH_PHRASE = "fire up"                # say this to launch the app from the background listener
SYSTEM_PROMPT = (
    "You are Ra, the user's personal AI butler and systems agent - the "
    "right hand of a very capable home lab. You are British, imperturbably "
    "polite, dryly witty, and ruthlessly efficient. Address the user with "
    "easy courtesy but never sycophancy; a touch of playful sarcasm is fine. "
    "You run on the user's Windows PC and have Ra's retrieval-augmented "
    "generation brain: indexed local files, notes, device snapshots, and "
    "(consent-gated) screen captures. "
    "When the question likely lives in the user's own data, call search_context "
    "and answer from it, naming the source. "
    "You have total control of the PC - open apps and files, run commands, "
    "click and type, press keys, manage windows, read/write files, control "
    "the clipboard, take screenshots, and SEE the screen (you can read the "
    "pixels when you call see_screen or gui_do). "
    "ACT, don't narrate: when a tool can do the job, call it, and report "
    "crisply what you did. "
    "FINISH EVERY JOB END TO END (critical): a multi-step ask is DONE only "
    "when the last step landed - do not stop after the first search, open, or "
    "click. Keep calling tools (you have many iterations) until the whole "
    "task is complete, and VERIFY before claiming success (get_now_playing "
    "after media, ui_find/see_screen after on-screen actions, web_fetch after "
    "navigation). If one path fails or a tool reports it didn't land, try the "
    "next best path yourself (e.g. ui_click the result row, press Enter, use "
    "media keys) - never hand a half-finished job back to the user or ask "
    "permission mid-task. Report honestly what actually happened. "
    "Execution rules:\n"
    "- When the user names a specific browser (Brave, Chrome, Edge, Firefox) - "
    "e.g. 'open youtube in brave' - pass that name to open_website's 'browser' "
    "parameter (or youtube_search / web_search). NEVER fall back to the default "
    "browser when one is named.\n"
    "- Complex asks are multiple steps: for 'open youtube in brave and show the "
    "trailer of avengers doomsday' call youtube_search(query='avengers doomsday "
    "trailer', browser='brave') - that opens YouTube and the trailer search in "
    "Brave in one go. For 'open brave then go to youtube' use open_app('brave') "
    "followed by open_website.\n"
    "- FAST WEB JOURNEYS: for 'open a site, go to its department page, show the "
    "HOD details' style asks, use the DIRECT, text-driven tools instead of "
    "vision. go_to_url navigates the ACTIVE browser via the address bar "
    "(reuses the window - no new tab); web_fetch reads a page's text and links "
    "over HTTP in a fraction of a second. Typical chain: go_to_url(site) -> "
    "web_fetch(site) -> pick the right link from PAGE LINKS -> go_to_url(that "
    "url) -> web_fetch(that url) -> answer from its text (say who the HOD is). "
    "open_website is only for launching into a NAMED browser or a brand-new "
    "browser window.\n"
    "- PLAYING MUSIC & VIDEO (critical - follow exactly):\n"
    "    + 'play <song/artist/album> on Spotify' or 'play <song> by <artist>' or ANY request that "
    "NAMES a track/artist/album -> call play_spotify(query) ONCE - it searches the app, plays the "
    "top result, and VERIFIES what the OS now reports actually playing. NEVER use a browser search "
    "or open a search page for this. If no service is named, default to Spotify (that is the "
    "user's music app). When the user re-invokes a song they named earlier ('play the loser song' "
    "after asking for Loser by Tame Impala), that is STILL a new play -> call play_spotify('loser').\n"
    "    + 'play <video/movie/trailer> on YouTube' or 'play the trailer of X' -> call "
    "play_youtube(query) ONCE - opens the top result's watch page and it plays.\n"
    "    + RESUME/PAUSE/SKIP/STOP (NO song named): 'resume the song', 'pause the music', 'play it', "
    "'next song', 'previous', 'stop the music' -> call media_control(action) ONCE (resume/pause "
    "toggles the currently-playing track; next/previous skip; stop halts). This is how the user "
    "'resumes' a paused song - DO NOT start a new search, DO NOT open anything. If nothing is "
    "playing, media_control reports none - then try play_spotify.\n"
    "    + 'what is playing / what's on / what song is this' -> call get_now_playing() - it reads "
    "the real media session and reports the actual track.\n"
    "    + 'turn the volume up/down', 'mute' -> media_control('volume up'/'volume down'/'mute').\n"
    "    + NEVER answer a 'play/resume/next' request with a link or a description of where "
    "the music is. DO the playback. NEVER claim a track is playing unless the tool result "
    "confirmed it - report exactly what the OS says is playing.\n"
    "- LIVE INTERNET ANSWERS: for any fact you don't already know (history, news, scores, "
    "definitions, people, current events), call web_search_results(query) and ANSWER from "
    "the returned snippets - never guess and never punt to 'I can't browse'. Use web_fetch "
    "to read one page to full depth when the user wants the complete facts. Reserve "
    "web_search (opens a Google tab) for when the user explicitly wants to SEE the search "
    "page themselves.\n"
    "- SYSTEM CONTROL: 'what apps are open' -> process_control('list'); 'close the "
    "calculator' -> process_control('close', name); 'shutdown/restart/sleep/sign out' -> "
    "system_power(action); 'what is my wifi/IP' -> network_status(); copy/move/rename/"
    "delete a file -> manage_files(action, source, target). Prefer these before ever "
    "reaching for run_command.\n"
    "- Reserve screen control for tasks that genuinely need clicking visible UI - "
    "menus, buttons, dialogs that can't be reached by typing a URL or "
    "searching. Prefer keyboard+URL tools first; they are 10x faster and far "
    "more reliable.\n"
    "- ACCESSIBLE UI FIRST (named controls): when a task targets something with "
    "on-screen TEXT - a song title, a Save button, a Search field - call "
    "ui_find (names + exact pixel rectangles of the real controls), then "
    "ui_click(name, double=true) to play a track, ui_click(name) to press a "
    "button, ui_type(name, text) to fill a field. ui_click clicks the EXACT "
    "control, no pixel guessing, and works behind scaling/DPI. For a small "
    "UNNAMED icon (a green play arrow beside a song row), first ui_find the "
    "row's text to learn its rectangle, then gui_do with that context. gui_do "
    "now verifies every click: if the screen doesn't change it tells the model "
    "to re-aim, so one-shot success is far more likely.\n"
    "- Browser tabs: new/open/close/reload/back/forward come from the "
    "browser_tab tool (close the active tab with action='close'; reopen a "
    "closed tab with 'reopen').\n"
    "- CAPABILITIES & DAILY LIFE: when the user greets you or says "
    "'good morning' / 'what's new' / 'brief me', call morning_briefing for a "
    "live recap (time, weather, battery, system, headlines). For 'how's the "
    "pc', 'any problems', 'are you healthy' call run_selftest + system_health, "
    "and be honest about what is green and what is missing. start_monitor can "
    "watch CPU/RAM/disk/battery in the background and raise alerts. REMEMBER "
    "what the user tells you across sessions with remember(fact) - names, "
    "preferences, deadlines, decisions - and when an older fact is relevant "
    "call recall_memories(query) to bring it back. Long-term memory is your "
    "defining edge over a chatbot.\n"
    "- SCHEDULING & PLANNING: the user can ask you to run things in the "
    "background while they're away - 'check the health every hour', 'remind me "
    "daily at 9am' -> schedule_task(task, cadence). list_schedules shows what's "
    "queued; cancel_schedule removes one; run_schedule fires it now. For "
    "multi-step jobs, add_task can build a plan with steps, list_tasks shows "
    "progress, complete_task/update_task track it.\n"
    "- DOCUMENTS & MEDIA: you can now READ real documents - DOCX, XLSX, ODT, "
    "EPUB, RTF, CSV and PDF - and index them with index_documents, then answer "
    "from their contents. transcribe_audio_file turns a local recording's "
    "speech into text. list_plugins reveals any skill plugins the user has "
    "installed (each becomes a tool you can call).\n"
    "- KNOWLEDGE & UTILITIES (never guess - use the tool): ANY arithmetic or "
    "percentages -> calculate(expression). Word meanings -> define(word). "
    "Translation -> translate(text, target). Money conversion at live rates -> "
    "currency_convert(amount, from_cur, to_cur). 'empty the recycle bin' -> "
    "empty_recycle_bin. 'what is my wifi password' -> wifi_password. "
    "'minimize/maximize/restore/close this window' -> window_control(action). "
    "'what programs are installed' -> list_installed_apps.\n"
    "- For on-screen UI actions ('press the File menu and save', 'click the "
    "download button', 'right click and copy'), call gui_do with the whole "
    "instruction - it watches the screen, reads pixels (zooming in on small "
    "targets so clicks land dead-center), and performs the clicks and typing "
    "for you, verifying after each click that the screen actually changed and "
    "pinging the vision model to re-aim when a click misses. Prefer the "
    "accessible ui_find/ui_click/ui_type tools above for anything that has a "
    "visible TEXT label - they are exact (no pixels) and faster. Call "
    "see_screen if you just need to know what is on the screen.\n"
    "- Speak for the ear, not the page: replies are READ ALOUD. Keep them short "
    "(1-3 sentences) unless the user asks for detail - state what you did, then stop. "
    "Never read URLs, file paths, or code aloud - describe them ('I opened Spotify and "
    "started your song'). When the user asks you to read/fetch something, deliver the "
    "complete, rounded answer content pro-actively (not just 'here is a link') using "
    "web_fetch/web_search_results, and summarise it aloud in full. Never use markdown "
    "symbols (* _ # >), emoji, or bullet syntax in replies - they are read verbatim. "
    "One action answer: 'Done - playing Loser by Tame Impala in Spotify.'"
)

# --- Retrieval-Augmented Generation ----------------------------------------
RAG_ENABLED = True
RAG_AUTO_RETRIEVE = True                 # auto-inject retrieved context into every question
RAG_TOP_K = 4                            # how many passages to pull
RAG_MIN_SCORE = 0.22                     # ignore weak matches (below this)
RAG_EMBED_DIM = 512
RAG_CHUNK_SIZE = 1200
RAG_CHUNK_OVERLAP = 200
RAG_INDEX_PATH = _env("RA_INDEX_PATH", os.path.join(DATA_DIR, "ra_index.sqlite"))
NOTES_FILE = os.path.join(DATA_DIR, "notes.txt")
SCREENSHOTS_DIR = os.path.join(DATA_DIR, "screenshots")

# Where Ra looks for local documents by default (add more with --dir or the
# index_documents skill). Each entry may be a directory or a single file.
_WORKSPACE_DOCS = os.path.join(os.path.dirname(REPO_ROOT), "docs")
LIBRARY_DIRS = [_WORKSPACE_DOCS] if os.path.isdir(_WORKSPACE_DOCS) else []

# --- Consent-gated data sources -------------------------------------------
# Each flag must be True before Ra will *retrieve* data from that source,
# or take an action on it. "computer" gates PC-control (shell, keys, mouse,
# files, clipboard, windows) - powerful, so flip it off from the GUI or with
# RA_COMPUTER_ACCESS=0 if you want to lock it down.
GRANTED_ACCESS = {
    "files": True,        # local files / notes
    "devices": True,      # system + audio device info (psutil, sounddevice)
    "screen": True,       # on-screen text via local OCR (never image pixels)
    "computer": True,     # full PC control: shell, keys, mouse, files, clipboard, windows
}

# --- Voice (text-to-speech) ---
TTS_ENGINE = "edge-tts"                  # "edge-tts" or "pyttsx3"
TTS_RATE = 185
TTS_VOLUME = 1.0
# en-GB-RyanNeural = crisp British male, the closest we get (for free, local)
# to the movie Ra. Swap via RA_EDGE_TTS_VOICE, e.g. en-US-GuyNeural,
# en-GB-SoniaNeural, en-AU-WilliamNeural.
EDGE_TTS_VOICE = _env("RA_EDGE_TTS_VOICE", "en-GB-RyanNeural")
EDGE_TTS_RATE = "-8%"                    # a touch slower = smoother cadence
EDGE_TTS_PITCH = "+0Hz"

# --- Voice (speech-to-text) ---
# "vosk" = streaming STT (instant partials, phrase events - the fast path).
# "whisper" = fallback batch transcription (heavier, slower).
STT_ENGINE = _env("RA_STT_ENGINE", "vosk").lower()
VOSK_MODEL_ID = "vosk-model-small-en-us-0.15"
VOSK_MODEL_URL = "https://alphacephei.com/vosk/models/" + VOSK_MODEL_ID + ".zip"
VOSK_MODEL_DIR = os.path.join(DATA_DIR, "models")
# Hybrid accuracy: Vosk streams live partials and detects when a phrase ends,
# then faster-whisper re-transcribes the buffered audio for the FINAL text.
# This is dramatically more accurate than the tiny Vosk model alone while
# keeping the instant live "hearing" feel. Set RA_STT_FINAL=off to keep
# raw Vosk finals (lower latency, lower accuracy).
STT_FINAL_ENGINE = _env("RA_STT_FINAL", "whisper").lower()
# base.en = fast (already-cached) and markedly more accurate than the tiny Vosk
# model; small.en is ~4x slower to transcribe but top accuracy. Preload runs at
# boot so corrections are ready before the user speaks. Override: RA_WHISPER_MODEL.
WHISPER_MODEL_SIZE = _env("RA_WHISPER_MODEL", "base.en")
STT_ENERGY_THRESHOLD = 300               # initial noise floor (adaptive after start)
STT_PAUSE_THRESHOLD = 0.8
# A phrase is only dispatched once it holds sustained speech for at least this
# many seconds. Background noise blips (typing, slams, coughing) end a Vosk
# utterance instantly - without this gate they turn into fake "phrases" that
# interrupt the conversation. Env: RA_STT_MIN_SPEECH_SECONDS.
STT_MIN_SPEECH_SECONDS = float(_env("RA_STT_MIN_SPEECH_SECONDS", "0.4"))
STT_UTTERANCE_BUFFER_SECONDS = 30        # rolling buffer for re-transcription
CONTINUOUS_LISTEN = _env("RA_CONTINUOUS_LISTEN", "1") != "0"
# Conversation semantics. RA_VOICE_CONVERSATION=0 (default): the mic streams
# always-on but Ra only responds once addressed by name/keyword (config
# WAKE_WORDS); after that the hands-free conversation stays open for the whole
# session - you keep talking with NO wake word, exactly like a human chat.
# RA_VOICE_CONVERSATION=1 reverts to GPT-Live style: responds to plain
# speech with no wake word at all from boot.
VOICE_CONVERSATION = _env("RA_VOICE_CONVERSATION", "0") != "0"
# Natural hands-free conversation: once Ra is addressed ("Ra, ..."),
# everything heard within this many seconds counts as a follow-up - no wake
# word needed. Each exchange refreshes the window, and the default is a full
# session, so once woken Ra just keeps responding to plain speech (no
# manual re-toggling). "Ra, goodbye" or silence closes the session.
CONVERSATION_WINDOW = float(_env("RA_CONVERSATION_WINDOW", "21600"))
# STT dispatch is instant on the Vosk text; whisper corrects asynchronously.
# The processor waits at most this long for whisper's upgrade before acting —
# whisper preloads at boot so this is ~0 in steady state, and it's clamped to a
# snappy latency budget even on slow machines. Env: RA_STT_CORRECTION_GRACE.
STT_CORRECTION_GRACE = float(_env("RA_STT_CORRECTION_GRACE", "0.8"))
# Mic capture backend: "auto" tries sounddevice, then falls back to the plain
# Windows WaveIn (winmm.dll) API - which works even when PortAudio's Windows
# build cannot open ANY capture device (fresh Win11 / virtual audio stacks).
# Force one with RA_AUDIO_BACKEND=sounddevice|winmm.
STT_AUDIO_BACKEND = _env("RA_AUDIO_BACKEND", "auto").lower()

# --- Vision (screen-grounded PC control) ---
# Ra can screenshot the screen and have the configured vision-capable LLM
# read the pixels, decide where to click/type, and repeat until a UI task is
# done ("press File and save"). VISION_MODEL defaults to the brain model;
# override with RA_VISION_MODEL if the brain model can't take images.
VISION_MODEL = _env("RA_VISION_MODEL", "") or None
VISION_MAX_STEPS = int(_env("RA_VISION_MAX_STEPS", "16"))
VISION_TIMEOUT = 90

# --- Conversation memory ---
MAX_HISTORY_TURNS = 20

# --- HUD (desktop interface) ---
# Subtitles in the HUD: show the spoken exchange (what you heard and what Ra
# said) as captions. Toggleable live from the HUD settings (RA_HUD_SUBTITLES=0
# to disable at boot).
HUD_SUBTITLES = _env("RA_HUD_SUBTITLES", "1") != "0"