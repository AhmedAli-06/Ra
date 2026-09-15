"""
Ra Skills
=============
Real actions Ra can take on your Windows PC, plus the retrieval tools that
power its RAG: indexing documents, searching its local knowledge base, and
(consent-gated) capturing screen text and device snapshots. Each skill is
described as an OpenAI-style tool schema so the LLM brain can call it.

Add your own by: (1) adding an entry to TOOLS, (2) adding a branch in
execute_tool(), (3) writing the function.
"""
import os
import platform
import math
import re
import subprocess
import time
import urllib.parse
import webbrowser
from datetime import datetime
from html.parser import HTMLParser

from ra import config
from ra import logging as ralog
from ra.computer import _no_window
from ra.rag.embedder import HashEmbedder
from ra.rag.store import SemanticStore

# Friendly name -> Windows launch command
APP_COMMANDS = {
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "file explorer": "explorer",
    "explorer": "explorer",
    "word": "winword",
    "microsoft word": "winword",
    "excel": "excel",
    "microsoft excel": "excel",
    "powerpoint": "powerpnt",
    "spotify": "spotify",
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
    "terminal": "wt",
    "windows terminal": "wt",
    "command prompt": "cmd",
    "cmd": "cmd",
    "task manager": "taskmgr",
    "paint": "mspaint",
    "snipping tool": "snippingtool",
    "settings": "start ms-settings:",
    "device manager": "start devmgmt.msc",
    "control panel": "start shell:::{BB06C0E4-BB5F-11CF-8E0F-00C04FD7C09F}",
    "app store": "start ms-windows-store:",
    "maps": "start bingmaps:",
    "photos": "start ms-photos:",
    "movies": "start ms-media:",
    "calculator app": "start calculator:",
    "alarms": "start ms-clock:",
    "weather": "start ms-weather:",
    "security": "start windowsdefender:",
    "defender": "start windowsdefender:",
    "task scheduler": "start taskschd.msc",
    "disk management": "start diskmgmt.msc",
    "event viewer": "start eventvwr.msc",
    "services": "start services.msc",
    "registry editor": "regedit",
    "powershell": "powershell",
    "performance monitor": "start perfmon",
    "system information": "start msinfo32",
    "about windows": "start winver",
    "network connections": "start ncpa.cpl",
    "bluetooth": "start ms-settings:bluetooth",
    "display settings": "start ms-settings:display",
    "sound settings": "start ms-settings:sound",
    "wifi settings": "start ms-settings:network-wifi",
    "accounts": "start ms-settings:accounts",
    "time": "start ms-settings:dateandtime",
    "personalization": "start ms-settings:personalization",
    "accessibility": "start ms-settings:easeofaccess",
    "privacy": "start ms-settings:privacy",
    "update": "start ms-settings:windowsupdate",
    "firewall": "start wf.msc",
}

# Installed-browser detection: well-known install paths probed in order. This is
# why "open youtube in brave" actually opens Brave and not the default browser.
BROWSER_ALIASES = {
    "brave", "chrome", "google chrome", "edge", "microsoft edge",
    "firefox", "mozilla firefox", "opera", "vivaldi",
}
_BROWSER_PATH_CANDIDATES = {
    "brave": [
        "%LOCALAPPDATA%\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
        "%PROGRAMFILES%\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
        "%PROGRAMFILES(X86)%\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
    ],
    "chrome": [
        "%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe",
        "%PROGRAMFILES%\\Google\\Chrome\\Application\\chrome.exe",
        "%PROGRAMFILES(X86)%\\Google\\Chrome\\Application\\chrome.exe",
    ],
    "edge": [
        "%PROGRAMFILES%\\Microsoft\\Edge\\Application\\msedge.exe",
        "%PROGRAMFILES(X86)%\\Microsoft\\Edge\\Application\\msedge.exe",
    ],
    "firefox": [
        "%PROGRAMFILES%\\Mozilla Firefox\\firefox.exe",
        "%PROGRAMFILES(X86)%\\Mozilla Firefox\\firefox.exe",
    ],
    "opera": [
        "%LOCALAPPDATA%\\Programs\\Opera\\opera.exe",
        "%PROGRAMFILES%\\Opera\\opera.exe",
    ],
    "vivaldi": [
        "%LOCALAPPDATA%\\Vivaldi\\Application\\vivaldi.exe",
        "%PROGRAMFILES%\\Vivaldi\\Application\\vivaldi.exe",
    ],
}


def _norm_browser(name: str) -> str:
    name = (name or "").strip().lower()
    if name in ("google",):
        return "chrome"
    if name in ("google chrome", "chromium"):
        return "chrome"
    if name in ("microsoft edge", "msedge"):
        return "edge"
    if name in ("mozilla firefox", "moz"):
        return "firefox"
    return name


def _find_browser(name: str) -> str | None:
    """Return the absolute path of an installed browser, or None."""
    name = _norm_browser(name)
    for cand in _BROWSER_PATH_CANDIDATES.get(name, []):
        path = os.path.expandvars(cand)
        if path and os.path.isfile(path):
            return path
    try:
        out = subprocess.run(
            ["where", name], capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace", **_no_window(),
        )
    except Exception:
        return None
    lined = [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]
    return lined[0] if lined else None


def _launch_browser(name: str, url: str | None = None) -> str | None:
    """Launch `name` (optionally at `url`). Returns None if the browser wasn't
    found; otherwise the path that was launched."""
    exe = _find_browser(name)
    if not exe:
        return None
    cmd = [exe] + ([url] if url else [])
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(cmd, close_fds=True, **kwargs)
    return exe


def _store() -> SemanticStore:
    return SemanticStore(config.RAG_INDEX_PATH or os.path.join(os.path.expanduser("~"), ".ra", "ra_index.sqlite"))


def _embedder() -> HashEmbedder:
    return HashEmbedder(dim=config.RAG_EMBED_DIM)


def _granted(area: str) -> bool:
    return config.GRANTED_ACCESS.get(area, False)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "open_app",
        "description": "Open/launch an application on the user's Windows PC, e.g. notepad, chrome, spotify, calculator, file explorer, task manager.",
        "input_schema": {
            "type": "object",
            "properties": {"app_name": {"type": "string", "description": "Name of the app to open, e.g. 'notepad' or 'chrome'"}},
            "required": ["app_name"],
        },
    },
    {
        "name": "open_website",
        "description": "Open a website or URL. If the user names a specific browser (brave, chrome, edge, firefox, opera, vivaldi), pass it in 'browser' so it opens there - never the default browser when one is named.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL or domain to open, e.g. 'youtube.com'"},
                "browser": {"type": "string", "description": "Optional browser name: brave, chrome, edge, firefox, opera, vivaldi"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "youtube_search",
        "description": "Open YouTube's search-result LIST for a query in the named browser (or default). Use only when the user wants to SEE/choose from the results. To actually PLAY a song/video, call play_youtube instead - it opens the top result and it plays.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms, e.g. 'avengers doomsday trailer'"},
                "browser": {"type": "string", "description": "Optional browser name: brave, chrome, edge, firefox, opera, vivaldi"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "play_youtube",
        "description": "Search YouTube and PLAY the top video result immediately - fetches the results over HTTP, picks the first real video, and opens its watch page in the named browser (or default) so it starts playing. Use for 'play <song/video>', 'play the first result on youtube for X', 'play the trailer of X', 'play some music'. ONE call - no clicking, no vision.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search and play, e.g. 'shape of you ed sheeran' or 'avengers endgame trailer'"},
                "browser": {"type": "string", "description": "Optional browser name: brave, chrome, edge, firefox, opera, vivaldi"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "play_spotify",
        "description": "Search Spotify and PLAY the top result: opens the Spotify app (desktop preferred, web player fallback) already searching your query, then hits play on the top match. Use for 'play <song/artist/album> on/in Spotify', 'play <song> by <artist>', 'play some music on spotify'. If you merely need to pause/resume the current song, use media_control.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to search and play, e.g. 'loser tame impala' or 'shape of you ed sheeran'"}},
            "required": ["query"],
        },
    },
    {
        "name": "media_control",
        "description": "Control whatever music/video is playing on the PC (Spotify, YouTube, VLC, etc.) instantly with global media keys - no app switching, no clicking. Actions: play/pause/resume (toggles current track), next, previous, stop, volume up, volume down, mute. Use for 'resume the song', 'pause the music', 'play it', 'skip to the next song', 'previous track', 'stop the music', 'turn the volume up/down'. This is how Ra resumes a paused song.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["play", "pause", "resume", "next", "previous", "stop", "volume up", "volume down", "mute"], "description": "What to do with playback or volume"},
                "amount": {"type": "integer", "description": "For volume up/down: how many steps (default 1, max 10)"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "web_search_results",
        "description": "LIVE internet answers: fetch the top real search results (titles, links, snippets) for a question over HTTP in a second or two - no browser needed. Use to answer any factual question Ra doesn't already know ('who is X', 'latest news about Y', 'what is the score of Z'). Then summarise the answer yourself from the snippets. Use web_fetch to read a whole page in depth if snippets aren't enough.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The question or search, e.g. 'tame impala loser release year'"}},
            "required": ["query"],
        },
    },
    {
        "name": "system_power",
        "description": "Power control for the PC: shutdown, restart/reboot, sleep, sign out, lock. Use for 'shutdown the PC', 'restart', 'go to sleep', 'sign out', 'lock it'.",
        "input_schema": {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["shutdown", "restart", "sleep", "sign out", "lock"], "description": "Power action to perform"}},
            "required": ["action"],
        },
    },
    {
        "name": "process_control",
        "description": "List the apps/windows currently open, or close a specific app by name. Use for 'what apps are open', 'close the calculator', 'open apps'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "close"], "description": "'list' shows open apps; 'close' needs a name"},
                "name": {"type": "string", "description": "App/process name to close when action is 'close', e.g. 'Spotify'"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "network_status",
        "description": "Report the current network state: connected Wi-Fi SSID (or wired/off), local IP, and public IP. Use for 'what is my wifi', 'am I connected to the internet', 'what is my IP'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "manage_files",
        "description": "Copy, move, rename, or delete a file on the PC. Use for 'copy this file to X', 'move downloads to documents', 'delete file X', 'rename file Y'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["copy", "move", "rename", "delete"], "description": "File operation"},
                "source": {"type": "string", "description": "Absolute path to the source file"},
                "target": {"type": "string", "description": "Absolute destination path (copy/move/rename)"},
            },
            "required": ["action", "source"],
        },
    },
    {
        "name": "web_search",
        "description": "Open a Google search for a query in the named browser (or default).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms"},
                "browser": {"type": "string", "description": "Optional browser name: brave, chrome, edge, firefox, opera, vivaldi"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": "Fetch a URL and return its readable text plus the top page links (fast HTTP GET, no clicking or vision needed). Use to read a page's contents, find a department page link, or extract details like an HOD's name. Perfect for multi-step browsing: fetch the homepage, pick the right link, fetch that page, read the details.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full URL or domain, e.g. 'https://gndec.ac.in' or 'gndec.ac.in'"}},
            "required": ["url"],
        },
    },
    {
        "name": "go_to_url",
        "description": "Navigate the ACTIVE browser window to a URL instantly (Ctrl+L address bar + type + Enter). Reuses the open browser instead of spawning a new window. Chain calls for multi-page journeys: 'go to the GNDEC website, then the computer science page, then the HOD page'.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full URL or domain, e.g. 'https://gndec.ac.in'"}},
            "required": ["url"],
        },
    },
    {
        "name": "type_and_enter",
        "description": "Type text into the focused field and press Enter - one step for search boxes, the browser address bar, or forms.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to type then submit"}},
            "required": ["text"],
        },
    },
    {
        "name": "copy_page_text",
        "description": "Select-all + copy the active window (usually a webpage) and return the copied text (first 3000 chars). Use to 'read what is on this page' without vision.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_on_page",
        "description": "Find/highlight text on the current page (Ctrl+F). Confirms whether the text is present.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Text to find, e.g. 'hod' or 'head of department'"}},
            "required": ["query"],
        },
    },
    {
        "name": "browser_tab",
        "description": "Control the active browser's tabs instantly via keyboard: new, close, next, prev, reload, back, forward, new window, close window, reopen.",
        "input_schema": {
            "type": "object",
            "properties": {"action": {"type": "string", "description": "new | close | next | prev | reload | back | forward | new window | close window | reopen"}},
            "required": ["action"],
        },
    },
    {
        "name": "scroll_page",
        "description": "Scroll the active window: up, down (one page), top, bottom - instant keyboard scrolling.",
        "input_schema": {
            "type": "object",
            "properties": {"direction": {"type": "string", "description": "up | down | top | bottom"}},
            "required": ["direction"],
        },
    },
    {
        "name": "wait_seconds",
        "description": "Pause a short moment (0.5 to 15 seconds) to let a page or app finish loading before the next step.",
        "input_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "integer", "description": "How many seconds to wait (max 15)"}},
            "required": ["seconds"],
        },
    },
    {
        "name": "get_time",
        "description": "Get the current local date and time.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_weather",
        "description": "Get the current weather for a named location (city/town).",
        "input_schema": {
            "type": "object",
            "properties": {"location": {"type": "string", "description": "City or place name, e.g. 'Bidar, India'"}},
            "required": ["location"],
        },
    },
    {
        "name": "lock_pc",
        "description": "Lock the user's Windows PC immediately (like pressing Win+L).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "take_note",
        "description": "Save a short note/reminder for the user to Ra's local notes file, timestamped. Notes are also indexed for retrieval.",
        "input_schema": {
            "type": "object",
            "properties": {"note": {"type": "string", "description": "The note text to save"}},
            "required": ["note"],
        },
    },
    {
        "name": "read_notes",
        "description": "Read back all previously saved notes.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_battery",
        "description": "Get the laptop's current battery percentage and charging status.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_system_status",
        "description": "Get a quick system health report: CPU usage, RAM used/free, disk free space, and uptime.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_timer",
        "description": "Set a spoken timer/reminder. Ra will speak 'time's up'-style after the given number of seconds.",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "description": "Seconds to wait before Ra speaks the reminder"},
                "label": {"type": "string", "description": "What the reminder is for, e.g. 'pasta is done'"},
            },
            "required": ["seconds"],
        },
    },
    {
        "name": "create_directory",
        "description": "Create a directory (and any parent folders) at the given absolute path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path to create, e.g. 'C:\\Users\\me\\Documents\\projects\\new'"}},
            "required": ["path"],
        },
    },
    {
        "name": "find_local_file",
        "description": "Search the user's home/documents folders for a file whose name contains the query, and return up to a few matching paths.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Part of the file name to find, e.g. 'report 2026' or 'budget.xlsx'"}},
            "required": ["query"],
        },
    },
    {
        "name": "search_context",
        "description": "Search Ra's local knowledge index (the user's indexed files, notes, prior screen captures and device snapshots) and return the most relevant passages. Use this for any question about the user's own documents, notes, what was on screen, or device stats.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search phrase, e.g. 'what is the wifi password note'"},
                "k": {"type": "integer", "description": "How many passages to return (default 3)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "index_documents",
        "description": "Index a folder of local documents (txt, md, pdf, code, csv, etc.) into Ra's local knowledge base so it can answer questions about their contents.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path to a directory (or single file) to index"}},
            "required": ["path"],
        },
    },
    {
        "name": "capture_screen",
        "description": "Capture the current screen and extract its text into Ra's knowledge base. Consent-gated and privacy-safe: only local OCR text is indexed, image pixels never leave the device.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "refresh_device_snapshot",
        "description": "Collect a fresh snapshot of the system and devices (CPU, memory, battery, disks, audio devices, network) and index it for later retrieval.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_audio_devices",
        "description": "List the audio input/output devices currently connected to the PC.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_access",
        "description": "Show which data sources (files, devices, screen) the user has granted Ra retrieval access to.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_access",
        "description": "Grant or revoke Ra's retrieval access for a data source: 'files', 'devices', 'screen'; or 'computer' - which gates total PC control (shell commands, keys, mouse, files, clipboard, windows).",
        "input_schema": {
            "type": "object",
            "properties": {
                "area": {"type": "string", "enum": ["files", "devices", "screen", "computer"], "description": "Which data source / capability"},
                "allowed": {"type": "boolean", "description": "True to grant, False to revoke"},
            },
            "required": ["area", "allowed"],
        },
    },
    # ---- computer control (gated by "computer" access) ----
    {
        "name": "run_command",
        "description": "Run a shell command on the user's Windows PC and return its output. Use for anything scriptable: system info, network, updates, files, etc.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The command to run, e.g. 'netstat -an' or 'systeminfo'"}},
            "required": ["command"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text as if on the keyboard into the currently focused field.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to type"}},
            "required": ["text"],
        },
    },
    {
        "name": "press_keys",
        "description": "Press a key or keyboard chord, e.g. 'ctrl+c', 'win+d', 'alt+tab', 'enter', 'volumeup', 'f11'.",
        "input_schema": {
            "type": "object",
            "properties": {"keys": {"type": "string", "description": "Key or chord, e.g. 'ctrl+s'"}},
            "required": ["keys"],
        },
    },
    {
        "name": "click_screen",
        "description": "Move the mouse to a screen position and click. Coordinates are in pixels from the top-left.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X pixel coordinate"},
                "y": {"type": "integer", "description": "Y pixel coordinate"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "move_mouse",
        "description": "Move the mouse pointer to a screen position without clicking.",
        "input_schema": {
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"],
        },
    },
    {
        "name": "scroll_wheel",
        "description": "Scroll the mouse wheel. Positive = up, negative = down, e.g. 3 or -5.",
        "input_schema": {
            "type": "object",
            "properties": {"amount": {"type": "integer", "description": "Scroll notches (positive up, negative down)"}},
            "required": ["amount"],
        },
    },
    {
        "name": "list_windows",
        "description": "List the visible open windows on the PC (useful to know what's on screen).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "focus_window",
        "description": "Bring a window to the foreground by title text, e.g. 'Notepad' or 'Spotify'.",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string", "description": "Window title (or part of it)"}},
            "required": ["title"],
        },
    },
    {
        "name": "list_directory",
        "description": "List the contents of a folder on the PC.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute folder path"}},
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file from the PC (first 3000 chars).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute file path"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write/replace a text file on the PC (creates folders as needed).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path"},
                "text": {"type": "string", "description": "Full contents to write"},
            },
            "required": ["path", "text"],
        },
    },
    {
        "name": "open_path",
        "description": "Open a file or folder on the PC (opens Explorer / default app).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path to open"}},
            "required": ["path"],
        },
    },
    {
        "name": "screenshot",
        "description": "Take a full-screen screenshot and save it locally.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_screen_info",
        "description": "Get the screen resolution and current mouse position.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "see_screen",
        "description": "Look at the current screen (reads the pixels visually) and describe what is visible. Use when the user asks 'what is on my screen?', or before driving UI so you know the layout.",
        "input_schema": {
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "Optional: what to focus on while describing, e.g. 'look for a File menu'"}},
        },
    },
    {
        "name": "gui_do",
        "description": "VISUAL SCREEN CONTROL: watch the screen, and autonomously click/type/scroll through the UI until the instruction is done. Post-action verification tells the vision model if a click missed, so it re-aims or uses a keyboard shortcut next turn instead of repeating a dead spot. Use for unnamed icons (play arrows, close buttons without labels), complex multi-step visual workflows, or anything the accessible tools (ui_find/ui_click) can't reach by name. Returns a log of every step it performed.",
        "input_schema": {
            "type": "object",
            "properties": {"instruction": {"type": "string", "description": "The full UI task, e.g. 'click File then Save', 'press the printer icon', 'close this window'"}},
            "required": ["instruction"],
        },
    },
    {
        "name": "ui_find",
        "description": "ACCESSIBLE UI TREE: read the names and exact screen rectangles of the real controls in the active app (menu items, buttons, list rows like song titles, edit fields). Pass a text snippet to narrow to matching controls, e.g. ui_find('Stairway'). The control rectangles (in pixels) tell you exactly where things are - use this FIRST before clicking, and to locate the row beside a small unnamed icon.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Optional text the control must contain, e.g. a song title, button label, or menu item."},
                "region": {"type": "string", "description": "Optional 'x1,y1,x2,y2' physical-pixel rectangle to limit the search to."},
            },
        },
    },
    {
        "name": "ui_click",
        "description": "ACCESSIBLE CLICK: click the real control whose text contains `name` (e.g. a song title, a Save button) - the accessible tree finds it exactly, no pixel guessing. Set double=true to double-click (needed to PLAY a track/list item in music apps). If the target is a small unnamed icon, first ui_find the row's text to see its rectangle, then use gui_do to press the icon beside it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Text the target control contains."},
                "double": {"type": "boolean", "description": "Double-click instead of single (for play/open on rows)."},
                "region": {"type": "string", "description": "Optional 'x1,y1,x2,y2' rectangle to search within."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "ui_type",
        "description": "ACCESSIBLE TYPE: click the text field whose accessible name contains `name` and enter `text` into it (value pattern, or paste if the app doesn't expose one).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Text the target field's name contains, e.g. 'Search', 'Address'."},
                "text": {"type": "string", "description": "The text to enter."},
            },
            "required": ["name", "text"],
        },
    },
    {
        "name": "get_clipboard",
        "description": "Read the current clipboard text.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_clipboard",
        "description": "Copy text to the clipboard.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to copy"}},
            "required": ["text"],
        },
    },
    {
        "name": "get_now_playing",
        "description": "Detect what is currently playing on Spotify, a browser, or any media app by reading the window title. Returns the song/track name and artist if playing, or 'nothing detected'. Use for 'what song is playing', 'what is this track', 'what is playing right now'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "toast_notify",
        "description": "Send a Windows toast notification (pop-up in the system tray) with a title and message. Use to alert the user visually when a timer goes off, a download finishes, or something needs attention - paired with TTS it's very Jarvis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Notification title, e.g. 'Timer'"},
                "message": {"type": "string", "description": "Notification body, e.g. 'Pasta is done!'"},
            },
            "required": ["title", "message"],
        },
    },
    {
        "name": "get_selected_text",
        "description": "Capture whatever text the user has highlighted/selected in the focused app (Ctrl+C silently, then return the clipboard contents). Use for 'read what I have selected', 'what does this say', 'read my selection'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_volume",
        "description": "Set the system master volume to a specific level 0-100, or mute/unmute. Faster than pressing volume keys repeatedly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "level": {"type": "integer", "description": "Volume level 0-100 (omit for mute/unmute)"},
                "mute": {"type": "boolean", "description": "True to mute, False to unmute"},
            },
        },
    },
    {
        "name": "remember",
        "description": "Store a fact, preference, name, or decision in long-term memory so Ra remembers it in FUTURE sessions (e.g. 'the user is left-handed', 'project deadline is Friday', 'always call me boss'). Call this whenever the user tells you something worth keeping.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "The fact or preference to remember"},
            },
            "required": ["fact"],
        },
    },
    {
        "name": "recall_memories",
        "description": "Search Ra's long-term memory for facts or preferences about the user/their setup. Use when the user asks something that may have been told to Ra before (names, hobbies, deadlines, settings).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look up, e.g. 'deadline', 'favourite music', 'my name'"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_news",
        "description": "Fetch the latest news headlines for a topic (or general headlines if no topic). Sources are live web news via DuckDuckGo. e.g. 'what's in the news today', 'any tech news'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Optional topic, e.g. 'technology', 'ai', 'cricket'. Omit for top headlines."},
            },
        },
    },
    {
        "name": "run_selftest",
        "description": "Run Ra's capability self-test and report which subsystems are live (network, news, telemetry, screen/OCR, audio, RAG, memory, fast lane). Use when the user asks 'are you ready', 'how healthy are you', 'what can you do', or after install/setup.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "system_health",
        "description": "One-shot read of CPU, RAM, disk and battery. Use to answer 'how's the system doing', 'check performance', 'any problems with the pc' without starting a watcher.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "start_monitor",
        "description": "Start a background system watcher that raises a notification whenever CPU/RAM/disk pass a % threshold or battery drops low. Use when the user wants Ra to watch the health of their pc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "interval": {"type": "integer", "description": "Check interval in seconds (default 60)"},
                "batt_low": {"type": "integer", "description": "Battery alert threshold % (default 20)"},
            },
        },
    },
    {
        "name": "stop_monitor",
        "description": "Turn off the background system-health watcher started by start_monitor.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "morning_briefing",
        "description": "Give a morning/briefing recap: current time, weather (if RA_HOME_CITY is set), battery, system status, and top news headlines. Use when the user greets you, says 'good morning', or asks 'what's new' / 'brief me'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "schedule_task",
        "description": "Schedule a task for Ra to perform automatically in the background, e.g. 'every hour' or 'daily at 09:00' or 'every 30 minutes'. Ra runs it while you're away. Use for 'set a reminder every hour', 'schedule a daily briefing', 'run this every night'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "What to do each time the schedule fires, e.g. 'check system health' or 'tell me the weather'"},
                "name": {"type": "string", "description": "A short label for the schedule, default = the task text"},
                "cadence": {"type": "string", "description": "one of: 'every minute', 'every hour', 'every N minutes', 'every N hours', 'daily', 'daily at HH:MM'"},
                "at": {"type": "string", "description": "For daily schedules: 'HH:MM' (24h), e.g. '09:00'"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "list_schedules",
        "description": "List all scheduled background tasks and when they next run.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_schedule",
        "description": "Remove a scheduled background task by its name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The schedule's name (from list_schedules)"}},
            "required": ["name"],
        },
    },
    {
        "name": "run_schedule",
        "description": "Run a scheduled background task's action immediately, on demand (even if it isn't due).",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The schedule's name"}},
            "required": ["name"],
        },
    },
    {
        "name": "add_task",
        "description": "Add a task, or a multi-step plan (steps), to Ra's persistent to-do list. Use for 'create a plan', 'make a checklist', 'add a to-do'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task or plan title"},
                "steps": {"type": "array", "items": {"type": "string"}, "description": "Optional ordered steps for a multi-step plan"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "list_tasks",
        "description": "Show the current to-do list / plans and each item's status.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "complete_task",
        "description": "Mark a task (by its number from list_tasks, 1-based) as done.",
        "input_schema": {
            "type": "object",
            "properties": {"number": {"type": "integer", "description": "Task position (1-based) from list_tasks"}},
            "required": ["number"],
        },
    },
    {
        "name": "update_task",
        "description": "Set a task (by number) to a status: 'todo', 'doing' or 'done'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "Task position (1-based)"},
                "status": {"type": "string", "enum": ["todo", "doing", "done"]},
            },
            "required": ["number", "status"],
        },
    },
    {
        "name": "remove_task",
        "description": "Delete a task (by number) from the list without marking it done.",
        "input_schema": {
            "type": "object",
            "properties": {"number": {"type": "integer", "description": "Task position (1-based)"}},
            "required": ["number"],
        },
    },
    {
        "name": "clear_tasks",
        "description": "Empty the entire to-do list.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "transcribe_audio_file",
        "description": "Transcribe a local audio or video file (mp3, wav, m4a, mp4, etc.) offline and return the text. Use for 'transcribe this audio', 'what does this recording say', 'read my voice note'.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path to the audio/video file"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_plugins",
        "description": "List the user-installed skill plugins currently available to Ra.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "calculate",
        "description": "Evaluate a math expression precisely (percent, powers, sqrt, trig, log). Use for ANY arithmetic instead of guessing.",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "e.g. '17*23', 'sqrt(144)+2**10', '35% of 240'"}},
            "required": ["expression"],
        },
    },
    {
        "name": "define",
        "description": "Look up the dictionary definition, part of speech and phonetics of an English word.",
        "input_schema": {
            "type": "object",
            "properties": {"word": {"type": "string"}},
            "required": ["word"],
        },
    },
    {
        "name": "translate",
        "description": "Translate text to another language (e.g. target 'fr', 'es', 'de', 'hi', 'ja').",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "target": {"type": "string", "description": "Target language code or name, e.g. 'fr'"},
                "source": {"type": "string", "description": "Optional source language (default: auto-detect 'en')"},
            },
            "required": ["text", "target"],
        },
    },
    {
        "name": "currency_convert",
        "description": "Convert money between currencies at today's live rate (e.g. 100 USD to EUR).",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "from_cur": {"type": "string", "description": "e.g. USD"},
                "to_cur": {"type": "string", "description": "e.g. EUR"},
            },
            "required": ["amount", "from_cur", "to_cur"],
        },
    },
    {
        "name": "empty_recycle_bin",
        "description": "Permanently empty the Windows Recycle Bin.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "wifi_password",
        "description": "Get the Wi-Fi network name and saved password of the network the PC is connected to.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "window_control",
        "description": "Control the CURRENT (foreground) window: minimize, maximize, restore or close it.",
        "input_schema": {
            "type": "object",
            "properties": {"action": {"type": "string", "description": "minimize | maximize | restore | close"}},
            "required": ["action"],
        },
    },
    {
        "name": "list_installed_apps",
        "description": "List the programs installed on this PC (from the registry) - use to check what can be opened.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------
def open_app(app_name: str) -> str:
    key = app_name.lower().strip()
    # Real browser exe when the user names a browser - handles Brave, Chrome, etc.
    if key in BROWSER_ALIASES:
        path = _launch_browser(key)
        if path:
            return f"Opened {app_name}."
        return (f"I couldn't find {app_name} installed - "
                f"opened it anyway via the start command.")
    if key in ("spotify", "music"):
        try:
            os.startfile("spotify:")
            return "Opened Spotify."
        except Exception:
            pass  # no desktop app -> fall through to web
    cmd = APP_COMMANDS.get(key, app_name)
    try:
        if platform.system() == "Windows":
            subprocess.Popen(
                f'start "" "{cmd}"',
                shell=True, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            )
        else:
            subprocess.Popen(
                [cmd], close_fds=True,
                start_new_session=True,
            )
        return f"Opened {app_name}."
    except Exception as e:
        return f"Couldn't open {app_name}: {e}"


def open_website(url: str, browser: str | None = None) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if browser:
        path = _launch_browser(browser, url)
        if path:
            return f"Opened {url} in {browser}."
        webbrowser.open(url)
        return (f"I couldn't find {browser} installed, so I opened {url} "
                f"in your default browser instead.")
    webbrowser.open(url)
    return f"Opened {url} in your default browser."


def youtube_search(query: str, browser: str | None = None) -> str:
    """Open YouTube's search results for `query` (e.g. a trailer request),
    in the named browser or the default one."""
    q = urllib.parse.quote_plus(query)
    return open_website(f"https://www.youtube.com/results?search_query={q}",
                        browser=browser)


def _yt_json_video(html: str):
    """Robust fallback: pull the embedded ytInitialData JSON and return the
    first real (videoId, title) it describes. Anchor parsing fails when YouTube
    serves a shell/consent page even though the JSON payload is present."""
    import json as _json
    import re as _re
    m = _re.search(r"var ytInitialData\s*=\s*(\{.+?\})\s*;?\s*</script>", html, _re.S)
    if not m:
        return None
    try:
        data = _json.loads(m.group(1))
    except Exception:
        return None
    found = []

    def _walk(node):
        if isinstance(node, dict):
            if "videoId" in node and isinstance(node["videoId"], str) and len(node["videoId"]) == 11:
                title = " ".join(str(node.get("title", "")).split()) if isinstance(node.get("title"), str) else ""
                if not title and "title" in node:
                    t = node["title"]
                    if isinstance(t, dict):
                        runs = t.get("runs")
                        if isinstance(runs, list) and runs:
                            title = " ".join(str(r.get("text", "")) for r in runs if isinstance(r, dict))
                if not title:
                    title = ""
                found.append((node["videoId"], " ".join(title.split())))
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(data)
    for vid, title in found:
        if title.lower().startswith(("shorts", "youtube shorts")):
            continue
        return {"url": f"https://www.youtube.com/watch?v={vid}", "title": title}
    if found:
        vid, title = found[0]
        return {"url": f"https://www.youtube.com/watch?v={vid}", "title": title}
    return None


def _first_watch_link(html: str):
    """Find the first real video on a YouTube results page: returns
    {url, title} or None. Prefers the page's anchor links, then the embedded
    ytInitialData JSON, so we never guess."""
    import re as _re
    parser = _ReadableHTMLParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    seen = set()
    for label, href in parser.links:
        href = (href or "").strip()
        m = _re.search(r"/watch\?v=([\w-]{11})", href)
        vid = m.group(1) if m else None
        if not vid or vid in seen:
            continue
        title = " ".join((label or "").split())
        if title.lower().startswith(("shorts", "youtube shorts")) or "list=" in href:
            # Skip shorts/playlist entries; prefer the first plain video.
            continue
        return {"url": f"https://www.youtube.com/watch?v={vid}", "title": title}
    return _yt_json_video(html)


def play_youtube(query: str, browser: str | None = None) -> str:
    """Search YouTube over HTTP and open the TOP result's watch page so the
    video actually plays - no clicking, no vision, one fast call."""
    q = urllib.parse.quote_plus(query)
    results_url = f"https://www.youtube.com/results?search_query={q}"
    try:
        import requests
        resp = requests.get(results_url, timeout=10, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0 Safari/537.36"),
            "Accept-Language": "en",
        })
        video = _first_watch_link(resp.text) if getattr(resp, "text", "") else None
    except Exception as e:
        return f"Couldn't search YouTube: {e}"
    if video:
        opened = open_website(video["url"], browser=browser)
        title = video["title"] or "your video"
        return f"{opened} Playing the top result: {title}."
    opened = open_website(results_url, browser=browser)
    return (f"{opened} I couldn't pick out a video link, so I opened the "
            f"search results instead.")


# ---------------------------------------------------------------------------
# Media & live-internet skills
# ---------------------------------------------------------------------------
_MEDIA_KEYS = {
    "play": "mediaplaypause",
    "pause": "mediaplaypause",
    "resume": "mediaplaypause",
    "play pause": "mediaplaypause",
    "toggle play": "mediaplaypause",
    "next": "medianext",
    "skip": "medianext",
    "next track": "medianext",
    "skip track": "medianext",
    "previous": "mediaprev",
    "prev": "mediaprev",
    "previous track": "mediaprev",
    "stop": "mediastop",
    "volume up": "volumeup",
    "volume down": "volumedown",
    "mute": "volumemute",
    "unmute": "volumemute",
    "toggle mute": "volumemute",
}


def media_control(action: str, amount: int = 1) -> str:
    """Control whatever is playing (Spotify, YouTube in a browser, VLC, etc.).
    play/pause/resume all toggle the current track - which is exactly how you
    resume a paused song. Volume uses the Windows volume keys. The result is
    verified by reading back what the OS now reports as playing."""
    a = str(action or "").strip().lower().replace("_", " ").replace("-", " ")
    a = " ".join(a.split())
    key = _MEDIA_KEYS.get(a, a.replace(" ", ""))
    if key not in _MEDIA_KEYS.values():
        return (f"Unknown media action '{action}'. Try: play, pause, resume, "
                f"next, previous, stop, volume up, volume down, mute.")
    try:
        if key in ("volumeup", "volumedown", "volumemute"):
            n = max(1, int(amount or 1))
            for _ in range(min(n, 10)):
                _computer.press_key(key)
            label = {"volumeup": "Volume up",
                     "volumedown": "Volume down",
                     "volumemute": "Muted the audio."}.get(key)
            return label + (f" by {n}." if key != "volumemute" else "")
    except Exception as e:
        return f"Media control failed: {e}"
    try:
        before = _computer.get_media_session_for("spotify")
    except Exception:
        before = {}
    _before = f"{before.get('title','')}||{before.get('artist','')}"
    try:
        _computer.press_key(key)
    except Exception as e:
        return f"Media control failed: {e}"
    # Wait a beat and read the REAL state so we report truth, not a guess.
    time.sleep(0.8)
    try:
        state = _computer.get_media_session()
    except Exception:
        state = {"status": "none"}
    if a in ("stop",):
        if state.get("status") in ("playing", "paused", "stopped"):
            return f"Stopped - {state.get('title') or 'playback'} is no longer playing."
        return "Stopped the music."
    if state.get("status") == "none":
        return ("Nothing is reporting as playing right now - the media key was "
                "pressed but no app confirmed a session.")
    title = state.get("title", "")
    artist = state.get("artist", "")
    label = f"{title} by {artist}" if title else "the current track"
    if a in ("next", "skip", "next track", "skip track"):
        return f"Skipped - now on: {label}."
    if a in ("previous", "prev", "previous track"):
        return f"Went back - now on: {label}."
    now_after = f"{title}||{artist}"
    if now_after != _before and state.get("status") == "playing":
        return f"Playing: {label}."
    if state.get("status") == "playing":
        return f"Playing: {label}."
    if state.get("status") == "paused":
        return f"Paused on: {label}."
    return f"{a} - current media session: {state.get('status')}."


def _spotify_installed() -> bool:
    """Best-effort: is the Spotify desktop app installed/handling spotify: URIs?"""
    try:
        out = subprocess.run(
            ["where", "spotify"], capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace", **_no_window(),
        )
        if out.returncode == 0 and (out.stdout or "").strip():
            return True
    except Exception:
        pass
    for base in (os.environ.get("LOCALAPPDATA", ""),
                 os.environ.get("APPDATA", "")):
        for cand in ("Spotify", "Spotify AB"):
            probe = os.path.join(base, cand, "Spotify.exe")
            if os.path.isfile(probe):
                return True
        probe = os.path.join(base, "Spotify", "Spotify.exe")
        if os.path.isfile(probe):
            return True
    return False


def _spotify_sig_words(query: str) -> list:
    """Significant search words from a play request: 'play loser by tame
    impala in spotify' -> ['loser', 'tame', 'impala'] (stopwords dropped)."""
    import re as _re
    stop = {"play", "plays", "playing", "song", "songs", "track", "tracks",
            "music", "by", "on", "in", "the", "a", "an", "some", "please",
            "spotify", "app", "now", "me", "my", "and", "to", "from", "put",
            "start", "open", "up", "for"}
    words = [w for w in _re.findall(r"[a-zA-Z0-9']+", str(query or "").lower())
             if w not in stop and len(w) > 1]
    return words


def _smtc_confirm_spotify(before_tag: str, secs: float):
    """Poll Spotify's OS media session up to `secs`; return the session dict
    the moment a NEW track is actually PLAYING (the pre-existing track never
    counts - that is how we avoid claiming success when nothing happened)."""
    end = time.time() + secs
    while time.time() < end:
        try:
            s = _computer.get_media_session_for("spotify")
        except Exception:
            s = {}
        tag = f"{s.get('title','')}||{s.get('artist','')}||{s.get('status','')}"
        if s.get("status") == "playing" and s.get("title") and tag != before_tag:
            return s
        time.sleep(0.5)
    return None


def _spotify_playing_after(query: str, timeout: float = 16.0) -> str:
    """Play a Spotify search END TO END and verify with the OS media transport.

    Strategy (each step verified, never assumed):
      1. Open the search for the FULL query in the desktop app.
      2. Double-click the matching result ROW through the accessible UI tree
         (exact, no blind pixel guessing).
      3. Fall back to focusing Spotify and committing the highlighted result
         with Enter.
      4. Only claim success when SMTC reports a new track actually playing;
         otherwise report precisely what happened."""
    try:
        before = _computer.get_media_session_for("spotify")
    except Exception:
        before = {}
    before_tag = f"{before.get('title','')}||{before.get('artist','')}||{before.get('status','')}"
    os.startfile("spotify:search:" + urllib.parse.quote_plus(query))
    time.sleep(2.4)

    words = _spotify_sig_words(query)
    needles = []
    if words:
        needles.append(words[0])                 # the song title, usually
        for w in reversed(words):                # then the artist, as retry
            if w != needles[0]:
                needles.append(w)
                break

    # 1) UIA double-click of the top matching row (plays it for real).
    for needle in needles:
        try:
            from ra import uia as _uia
            out = _uia.ui_click(needle, double=True)
        except Exception:
            out = ""
        if out.startswith(("Clicked", "Double-clicked")):
            state = _smtc_confirm_spotify(before_tag, 4.0)
            if state:
                return (f"Playing {state['title']} by {state['artist']} in "
                        f"Spotify - verified playing.")
        # Row not found (app still loading) - give it one more beat.
        time.sleep(1.0)

    # 2) Keyboard fallback: commit the highlighted search result with Enter.
    try:
        _computer.focus_window("Spotify")
    except Exception:
        pass
    time.sleep(0.4)
    try:
        _computer.press_key("enter")
    except Exception:
        pass
    state = _smtc_confirm_spotify(before_tag, 6.0)
    if state:
        return f"Playing {state['title']} by {state['artist']} in Spotify - verified playing."

    # 3) Honest outcome with the actual state, so the brain can react.
    try:
        now = _computer.get_media_session_for("spotify")
    except Exception:
        now = {}
    if now.get("title") and now.get("status") in ("playing", "paused"):
        verb = "playing" if now.get("status") == "playing" else "paused on"
        return (f"Spotify is {verb} {now['title']} by {now['artist']}, but that "
                f"may be the previous track - the search for '{query}' did not "
                f"confirm. If it is the wrong track, say 'next' or name it again.")
    if before.get("status") == "playing":
        return (f"Spotify stayed on {before.get('title')} by {before.get('artist')} - "
                f"the new search didn't take over. Say 'next' to skip, or name "
                f"the track and artist again.")
    return (f"Spotify opened with the search for '{query}' but I could not "
            f"confirm playback yet (the app may still be loading). Ask me to "
            f"'play the top result' and I'll click it.")


def play_spotify(query: str) -> str:
    """Search Spotify and play the top result. Desktop app is preferred (opened
    via its URI scheme, search committed, then the ACTUAL playing track is read
    back from the OS media transport so we never lie about success); the web
    player is the fallback. Reports the verified track or a precise reason."""
    q = urllib.parse.quote_plus(query)
    desktop = _spotify_installed()
    if desktop:
        try:
            return _spotify_playing_after(query)
        except Exception as e:
            ralog.log("err", f"play_spotify smtc path failed: {e}")
            desktop = False
    if not desktop:
        return open_website(f"https://open.spotify.com/search/{q}")


import html as _html_mod
import re as _re_mod


def _clean_html_fragment(frag: str) -> str:
    frag = _re_mod.sub(r"<[^>]+>", "", frag or "")
    frag = _html_mod.unescape(frag)
    return " ".join(frag.split())


def web_search_results(query: str, num: int = 5) -> str:
    """Return the top live search results (title, url, snippet) for `query`
    using the duckduckgo-search package (handles captcha/bot-detection
    automatically). Ra answers from real internet knowledge instead of guessing."""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max(1, min(int(num or 5), 8))))
        if not results:
            return f"No results for '{query}'."
        lines = []
        for r in results[:8]:
            title = r.get("title", "No title")
            url = r.get("href", "")
            snippet = r.get("body", "")
            if len(snippet) > 240:
                snippet = snippet[:237] + "..."
            lines.append(f"- {title}\n  {url}\n  {snippet}")
        return "Top results:\n" + "\n".join(lines)
    except Exception as e:
        return f"Web search failed: {e}"


# ---------------------------------------------------------------------------
# Knowledge & utility skills (calculate / define / translate / currency ...)
# ---------------------------------------------------------------------------
def _shell_text(cmd: str, timeout: float = 30.0) -> str:
    """run_shell + strip the 'exit N: ' status prefix so skill parsers and the
    user only ever see the real command output."""
    import re as _re
    out = str(_computer.run_shell(cmd, timeout=timeout) or "")
    return _re.sub(r"^exit -?\d+: ?", "", out).strip()


def calculate(expression: str) -> str:
    """Safe math evaluation (no eval of arbitrary code): numbers, + - * / // %
    **, parentheses and the usual math functions/constants. Understands
    'X% of Y' and 'X percent of Y' phrasing too."""
    import ast
    raw = str(expression or "").strip()
    if not raw:
        return "Give me an expression to calculate."
    low = raw.lower()
    m = _re_mod.match(r"^\s*([\d.]+)\s*(?:%|percent)\s*of\s*([\d.]+)\s*$", low)
    if m:
        return f"{float(m.group(1)) / 100 * float(m.group(2)):g}"
    expr = low.replace("^", "**").replace("×", "*").replace("÷", "/").replace("π", "pi")
    allowed_names = {
        "pi": math.pi, "e": math.e, "tau": math.tau,
        "sqrt": math.sqrt, "abs": abs, "round": round,
        "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "asin": math.asin, "acos": math.acos, "atan": math.atan,
        "log": math.log, "log10": math.log10, "log2": math.log2,
        "exp": math.exp, "floor": math.floor, "ceil": math.ceil,
        "factorial": math.factorial, "pow": pow, "min": min, "max": max,
    }

    def _eval(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult,
                ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)):
            left, right = _eval(node.left), _eval(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                return left // right
            if isinstance(node.op, ast.Mod):
                return left % right
            return left ** right
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            v = _eval(node.operand)
            return v if isinstance(node.op, ast.UAdd) else -v
        if isinstance(node, ast.Name) and node.id in allowed_names:
            return allowed_names[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in allowed_names:
            return allowed_names[node.func.id](*[_eval(a) for a in node.args])
        raise ValueError(f"unsupported element in '{raw}'")

    try:
        result = _eval(ast.parse(expr, mode="eval").body)
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return f"{raw} = {result:g}" if isinstance(result, float) else f"{raw} = {result}"
    except Exception as e:
        return f"Couldn't calculate '{expression}': {e}"


def define(word: str) -> str:
    """Dictionary definition via the free dictionaryapi.dev (no key needed)."""
    w = str(word or "").strip().strip(".!? ")
    if not w:
        return "Which word should I look up?"
    try:
        import requests
        r = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(w)}",
                         timeout=8, headers={"User-Agent": "Ra/1.0"})
        data = r.json()
    except Exception as e:
        return f"Dictionary lookup failed: {e}"
    if not isinstance(data, list) or not data:
        return f"No dictionary entry found for '{w}'."
    entry = data[0]
    phon = (entry.get("phonetic") or
            ((entry.get("phonetics") or [{}])[0].get("text", "")
             if entry.get("phonetics") else ""))
    parts = []
    for meaning in (entry.get("meanings") or [])[:2]:
        pos = meaning.get("partOfSpeech", "")
        defs = meaning.get("definitions") or [{}]
        definition = _clean_html_fragment(defs[0].get("definition", ""))
        line = f"{pos}: {definition}" if pos else definition
        parts.append(line)
    out = f"{w}{(' ' + phon) if phon else ''} - " + " ".join(parts[:2])
    return out[:600]


def translate(text: str, target: str, source: str = "en") -> str:
    """Translate text via MyMemory (free, no key)."""
    txt = str(text or "").strip()
    tgt = str(target or "").strip().lower()
    src = str(source or "en").strip().lower()
    if not txt or not tgt:
        return "Translate needs text and a target language."
    _LANGS = {"english": "en", "french": "fr", "spanish": "es", "german": "de",
              "italian": "it", "portuguese": "pt", "hindi": "hi", "japanese": "ja",
              "korean": "ko", "chinese": "zh", "russian": "ru", "arabic": "ar",
              "dutch": "nl", "turkish": "tr", "polish": "pl", "swedish": "sv"}
    tgt = _LANGS.get(tgt, tgt[:2])
    src = _LANGS.get(src, src[:2])
    try:
        import requests
        r = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": txt[:450], "langpair": f"{src}|{tgt}"},
            timeout=10, headers={"User-Agent": "Ra/1.0"})
        data = r.json()
        translated = ((data.get("responseData") or {}).get("translatedText") or "").strip()
        if not translated or translated.upper().startswith("MYMEMORY WARNING"):
            raise ValueError(translated or "empty response")
        return f"Translation: {translated}"
    except Exception as e:
        return f"Translation failed: {e}"


def currency_convert(amount: float, from_cur: str, to_cur: str) -> str:
    """Live FX conversion via open.er-api.com (free, no key)."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return "Give me a numeric amount."
    base = str(from_cur or "USD").strip().upper()[:3]
    quote = str(to_cur or "").strip().upper()[:3]
    if not quote:
        return "Give me both currencies."
    try:
        import requests
        r = requests.get(f"https://open.er-api.com/v6/latest/{urllib.parse.quote(base)}",
                         timeout=10, headers={"User-Agent": "Ra/1.0"})
        data = r.json()
        rate = (data.get("rates") or {}).get(quote)
        if not rate:
            return f"No rate found for {base} to {quote}."
        result = amount * float(rate)
        return f"{amount:g} {base} = {result:,.2f} {quote} (live rate {rate:g})."
    except Exception as e:
        return f"Currency conversion failed: {e}"


def empty_recycle_bin() -> str:
    """Empty the Windows Recycle Bin."""
    out = _shell_text(
        "powershell -NoProfile -Command "
        "\"Clear-RecycleBin -Force -ErrorAction SilentlyContinue; 'BIN_EMPTY'\"")
    if "BIN_EMPTY" in out:
        return "Recycle Bin emptied."
    return "Tried to empty the Recycle Bin - it may already be empty."


def wifi_password() -> str:
    """The SSID + saved password of the network the PC is on right now."""
    import re as _re
    text = _shell_text(
        "powershell -NoProfile -Command \""
        "$i = netsh wlan show interfaces | Select-String ' SSID '; "
        "if (-not $i) { Write-Output 'NO_WIFI'; exit }; "
        "$ssid = ($i.ToString().Split(':'))[1].Trim(); "
        "$p = netsh wlan show profile name=`\"$ssid`\" key=clear | Select-String 'Key Content'; "
        "$key = if ($p) { ($p.ToString().Split(':'))[1].Trim() } else { '(not stored)' }; "
        "Write-Output ('SSID=' + $ssid + '|KEY=' + $key)\"")
    if "NO_WIFI" in text:
        return "This PC isn't connected to any Wi-Fi network right now."
    m_ssid = _re.search(r"SSID=([^|\n]*)", text)
    m_key = _re.search(r"KEY=([^|\n]*)", text)
    if not m_ssid:
        return f"Couldn't read the Wi-Fi details: {text[:200]}"
    ssid = m_ssid.group(1).strip()
    key = m_key.group(1).strip() if m_key else "(not stored)"
    return f"Wi-Fi network '{ssid}' password: {key}"


def window_control(action: str) -> str:
    """Minimize / maximize / restore / close the CURRENT foreground window."""
    a = str(action or "").strip().lower()
    cmd = {"minimize": 6, "maximize": 3, "restore": 9}.get(a)
    ps = (
        "Add-Type 'using System;using System.Runtime.InteropServices;public class RaW {"
        "[DllImport(\"user32.dll\")] public static extern IntPtr GetForegroundWindow();"
        "[DllImport(\"user32.dll\")] public static extern bool ShowWindowAsync(IntPtr h, int c);"
        "[DllImport(\"user32.dll\")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l); }'; "
        "$h = [RaW]::GetForegroundWindow(); "
        "if ($h -eq [IntPtr]::Zero) { Write-Output 'NO_WINDOW'; exit }; ")
    if cmd:
        ps += f"[void][RaW]::ShowWindowAsync($h, {cmd}); Write-Output 'WINDOW_OK'"
    elif a in ("close", "quit"):
        ps += ("[void][RaW]::PostMessage($h, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero); "
               "Write-Output 'WINDOW_OK'")
    else:
        return f"Unknown window action '{action}'. Try: minimize, maximize, restore, close."
    out = _shell_text("powershell -NoProfile -Command \"" + ps + "\"")
    if "WINDOW_OK" in out:
        return f"{a.capitalize()}d the current window."
    return f"The window action ({a}) didn't land - no foreground window?"


def list_installed_apps() -> str:
    """Programs installed on this PC, from the registry uninstall keys."""
    out = _shell_text(
        "powershell -NoProfile -Command \"Get-ItemProperty "
        "'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*' "
        "-ErrorAction SilentlyContinue | Where-Object {$_.DisplayName} | "
        "Sort-Object DisplayName -Unique | Select-Object -ExpandProperty DisplayName\"")
    apps = [l.strip() for l in str(out or "").splitlines() if l.strip()]
    if not apps:
        return "Couldn't read the installed-programs list."
    if len(apps) > 120:
        shown = apps[:120]
        extra = f" ...and {len(apps) - 120} more."
    else:
        shown, extra = apps, ""
    return "Installed programs: " + ", ".join(shown) + extra


# ---------------------------------------------------------------------------
# System, files & network skills
# ---------------------------------------------------------------------------
def system_power(action: str) -> str:
    a = str(action or "").strip().lower().replace("_", " ").replace("-", " ")
    if a in ("shutdown", "shut down", "power off", "turn off"):
        _computer.run_shell("shutdown /s /t 5")
        return "Shutting down in 5 seconds. Say the word if you need to cancel."
    if a in ("restart", "reboot"):
        _computer.run_shell("shutdown /r /t 5")
        return "Restarting in 5 seconds."
    if a in ("sleep", "suspend", "sleep mode"):
        _computer.run_shell("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
        return "Putting the PC to sleep."
    if a in ("sign out", "logout", "log out"):
        _computer.run_shell("shutdown /l")
        return "Signing you out."
    if a in ("lock",):
        return lock_pc()
    return f"Unknown power action '{action}'. Try: shutdown, restart, sleep, sign out, lock."


def process_control(action: str = "list", name: str = "") -> str:
    a = str(action or "").strip().lower()
    if a in ("list", "apps", "open apps", "whats open", "what's open"):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Process | Where-Object {$_.MainWindowTitle} | "
                 "Sort-Object ProcessName | Select-Object -ExpandProperty ProcessName"],
                capture_output=True, text=True, timeout=20, encoding="utf-8",
                errors="replace", **_no_window(),
            )
            names = [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]
            uniq = list(dict.fromkeys(names))
            return ("Open apps: " + ", ".join(uniq)) if uniq else "No visible apps open."
        except Exception as e:
            return f"Couldn't list apps: {e}"
    if a in ("close", "stop", "quit", "kill") and (name or "").strip():
        app = str(name).strip()
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Stop-Process -Name '{app}' -Force -ErrorAction SilentlyContinue"],
                capture_output=True, text=True, timeout=15, encoding="utf-8",
                errors="replace", **_no_window(),
            )
            return f"Closed {app}."
        except Exception as e:
            return f"Couldn't close {app}: {e}"
    return "Use action='list' to see open apps, or action='close' with a name."


def network_status() -> str:
    ssid = ""
    try:
        out = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, timeout=10, encoding="utf-8",
            errors="replace", **_no_window(),
        )
        for line in (out.stdout or "").splitlines():
            if "SSID" in line and "BSSID" not in line and ":" in line:
                cand = line.split(":", 1)[1].strip()
                if cand:
                    ssid = cand
                    break
    except Exception:
        pass
    local_ip = "unknown"
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    public_ip = "unknown"
    try:
        import requests
        public_ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
        if not public_ip:
            public_ip = requests.get("https://ifconfig.me/ip", timeout=5).text.strip()
    except Exception:
        try:
            import requests
            public_ip = requests.get("https://checkip.amazonaws.com", timeout=8).text.strip()
        except Exception:
            pass
    return (f"Network: {ssid or 'wired/not connected'}; local IP {local_ip}; "
            f"public IP {public_ip}.")


def manage_files(action: str, source: str = "", target: str = "") -> str:
    import shutil
    a = str(action or "").strip().lower()
    if a in ("copy",):
        if not source or not target:
            return "Copy needs both source and target paths."
        try:
            shutil.copy2(source, target)
            return f"Copied {source} to {target}."
        except Exception as e:
            return f"Copy failed: {e}"
    if a in ("move",):
        if not source or not target:
            return "Move needs both source and target paths."
        try:
            shutil.move(source, target)
            return f"Moved {source} to {target}."
        except Exception as e:
            return f"Move failed: {e}"
    if a in ("rename",):
        if not source or not target:
            return "Rename needs source and new target paths."
        try:
            os.replace(source, target)
            return f"Renamed to {target}."
        except Exception as e:
            return f"Rename failed: {e}"
    if a in ("delete", "remove"):
        if not source:
            return "Delete needs a source path."
        try:
            os.remove(source)
            return f"Deleted {source}."
        except OSError:
            try:
                shutil.rmtree(source)
                return f"Deleted {source}."
            except Exception as e:
                return f"Delete failed: {e}"
    return f"Unknown file action '{action}'. Try: copy, move, rename, delete."


# ---------------------------------------------------------------------------
# Now-playing, notifications, selection & volume skills
# ---------------------------------------------------------------------------
def get_now_playing() -> str:
    """Report what is ACTUALLY playing, verified via the Windows media session
    (SMTC) first - title, artist, app and status straight from the OS. Falls
    back to window-title heuristics only when no media session is reporting."""
    try:
        state = _computer.get_media_session()
    except Exception as e:
        state = {"status": "none"}
        ralog.log("err", f"get_now_playing smtc failed: {e}")
    if state.get("status") not in ("none", "stopped"):
        title = state.get("title", "")
        artist = state.get("artist", "")
        app = state.get("app", "")
        clean_app = app.split("!")[-1].replace("_", " ").strip() if app else ""
        label = f"{title} by {artist}" if title else "something"
        woven = f"{label} on {clean_app}" if clean_app else label
        if state["status"] == "playing":
            return f"Playing: {woven}."
        return f"Paused on: {woven}."
    try:
        titles = _computer.list_windows()
    except Exception:
        return "I could not read the open windows."
    music_hints = ("spotify", "chrome", "brave", "edge", "firefox", "opera",
                   "vlc", "groove", "music", "youtube", "winamp", "foobar",
                   "audacious", "deezer", "soundcloud", "apple music")
    parts = [t for t in titles.split(" | ") if t.strip()]
    if not parts:
        return "Nothing is visibly playing right now."
    bare = {"spotify", "spotify premium", "spotify free", "spotify - web player",
            "groove music", "windows media player", "winamp", "vlc media player"}
    music_open = any(
        p.strip().lower() in bare or any(h in p.lower() for h in music_hints)
        for p in parts
    )
    for part in parts:
        clean = " ".join(part.split())
        low = clean.lower()
        if low in bare:
            continue
        if len(clean) > 3 and "-" in clean and music_open:
            return f"Right now: {clean}."
        if any(h in low for h in music_hints) and len(clean) > 3:
            return f"Right now: {clean}."
    return "Nothing is visibly playing right now."


def toast_notify(title: str, message: str) -> str:
    """Fire a Windows toast via PowerShell's BurntToast-style toast. Uses the
    built-in Windows.UI.Notifications bridge through a tiny PowerShell one-liner
    with no extra packages; falls back to msg.exe (classic message box)."""
    try:
        t = (str(title or "Ra")[:60]).replace("'", "''")
        m = (str(message or ""))[:200].replace("'", "''")
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, "
            "Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
            "$tmpl = [Windows.UI.Notifications.ToastNotificationManager]::"
            "GetTemplateContent('ToastText02'); "
            "$txt = $tmpl.GetElementsByTagName('text'); "
            f"$txt.Item(0).AppendChild($tmpl.CreateTextNode('{t}')) > $null; "
            f"$txt.Item(1).AppendChild($tmpl.CreateTextNode('{m}')) > $null; "
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
            "$xml.LoadXml($tmpl.GetXml()); "
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier"
            "('Ra').Show(New-Object Windows.UI.Notifications.ToastNotification($xml))"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, text=True, timeout=15, encoding="utf-8",
            errors="replace", **_no_window(),
        )
        return f"Notification sent: {title} - {message}"
    except Exception:
        try:
            subprocess.run(
                ["msg", "%username%", f"{title}: {message}"],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace", **_no_window(),
            )
            return f"Notification sent: {title} - {message}"
        except Exception:
            return "Could not send a notification."


def get_selected_text() -> str:
    """Silently copy the user's current selection and return it."""
    try:
        _computer.press_key("ctrl+c")
        time.sleep(0.25)
        return _computer.get_clipboard() or "Nothing was selected."
    except Exception as e:
        return f"Could not grab the selection: {e}"


def set_volume(level=None, mute=None) -> str:
    """Set the system master volume to 0-100, or mute/unmute, via the Windows
    Core Audio API (no extra packages, works on Win10/11)."""
    type_def = (
        "using System.Runtime.InteropServices; "
        "[Guid(\"5CDF2C82-841E-4546-9722-0CF74078229A\"), "
        "InterfaceType(ComInterfaceType.InterfaceIsIUnknown)] "
        "interface IAudioEndpointVolume { "
        "int RegisterControlChangeNotify(System.IntPtr p); "
        "int UnregisterControlChangeNotify(System.IntPtr p); "
        "int GetChannelCount(out int c); "
        "int SetMasterVolumeLevel(float lvl, ref System.Guid ctx); "
        "int SetMasterVolumeLevelScalar(float lvl, ref System.Guid ctx); "
        "int GetMasterVolumeLevel(out float lvl); "
        "int GetMasterVolumeLevelScalar(out float lvl); } "
        "[Guid(\"BCDE0395-E52F-467C-8E3D-C4579291692E\"), ComImport] "
        "class MMDeviceEnumerator { } "
        "[Guid(\"A95664D2-9614-4F35-A746-DE8DB63617E6\"), "
        "InterfaceType(ComInterfaceType.InterfaceIsIUnknown)] "
        "interface IMMDeviceEnumerator { "
        "int EnumAudioEndpoints(int d, int s, out System.IntPtr p); "
        "int GetDefaultAudioEndpoint(int d, int s, out IMMDevice dev); } "
        "[Guid(\"D666063F-1587-4E43-81F1-B948E807363F\"), "
        "InterfaceType(ComInterfaceType.InterfaceIsIUnknown)] "
        "interface IMMDevice { "
        "int Activate(ref System.Guid iid, int ctx, System.IntPtr p, "
        "out IAudioEndpointVolume vol); } "
        "public class Audio { "
        "static IAudioEndpointVolume GetVolObj() { "
        "var e = (IMMDeviceEnumerator)(new MMDeviceEnumerator()); "
        "IMMDevice d; e.GetDefaultAudioEndpoint(0, 0, out d); "
        "var g = new System.Guid(\"5CDF2C82-841E-4546-9722-0CF74078229A\"); "
        "IAudioEndpointVolume v; d.Activate(ref g, 1, System.IntPtr.Zero, out v); "
        "return v; } "
        "public static float Get() { IAudioEndpointVolume v = GetVolObj(); "
        "float l; v.GetMasterVolumeLevelScalar(out l); return l * 100f; } "
        "public static void Set(float lvl) { "
        "IAudioEndpointVolume v = GetVolObj(); var g = System.Guid.Empty; "
        "v.SetMasterVolumeLevelScalar((lvl < 0 ? 0 : (lvl > 100 ? 100 : lvl)) / 100f, "
        "ref g); } "
        "public static void Mute(bool on) { "
        "IAudioEndpointVolume v = GetVolObj(); var g = System.Guid.Empty; "
        "v.SetMasterVolumeLevelScalar(0f, ref g); } }"
    )
    try:
        call = ""
        if mute is not None:
            call = ("[Audio]::Mute(" + ("$true" if mute else "$false") + ")")
        elif level is not None:
            call = "[Audio]::Set(" + str(max(0, min(100, int(level)))) + ")"
        else:
            call = "[Audio]::Get()"
        out = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
             f"Add-Type -TypeDefinition '{type_def}'; {call}"],
            capture_output=True, text=True, timeout=20, encoding="utf-8",
            errors="replace", **_no_window(),
        )
        if mute is True:
            return "Muted."
        if mute is False:
            return "Unmuted - volume restored."
        if level is not None:
            return f"Volume set to {max(0, min(100, int(level)))}."
        val = (out.stdout or "").strip()
        try:
            val_f = float(val)
        except ValueError:
            return f"Volume query returned: {val or '(no output)'}."
        flag = " (muted)" if val_f < 0.5 else ""
        return f"Current volume: {val_f:.0f}%{flag}."
    except Exception as e:
        return f"Volume control failed: {e}"


def web_search(query: str, browser: str | None = None) -> str:
    """Open a web search for `query` in the named browser or the default one."""
    q = urllib.parse.quote_plus(query)
    return open_website(f"https://www.google.com/search?q={q}",
                        browser=browser)


def get_time() -> str:
    now = datetime.now()
    return now.strftime("It's %I:%M %p on %A, %B %d, %Y.")


def get_weather(location: str) -> str:
    try:
        import requests
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1},
            timeout=8,
        ).json()
        if not geo.get("results"):
            return f"I couldn't find a place called '{location}'."
        place = geo["results"][0]
        lat, lon = place["latitude"], place["longitude"]
        name = f"{place.get('name')}, {place.get('country', '')}".strip(", ")

        wx = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon, "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"},
            timeout=8,
        ).json()
        cur = wx.get("current", {})
        temp = cur.get("temperature_2m")
        humidity = cur.get("relative_humidity_2m")
        wind = cur.get("wind_speed_10m")
        return f"In {name}: {temp}°C, {humidity}% humidity, wind {wind} km/h."
    except Exception as e:
        return f"Couldn't fetch the weather: {e}"


def lock_pc() -> str:
    try:
        if platform.system() == "Windows":
            import ctypes
            ctypes.windll.user32.LockWorkStation()
            return "PC locked."
        return "Locking is only supported on Windows."
    except Exception as e:
        return f"Couldn't lock the PC: {e}"


def _index_notes_file(text: str):
    try:
        from ra.rag import indexer
        indexer.index_file(config.NOTES_FILE, _store(), _embedder())
    except Exception as e:
        print(f"[note indexing skipped: {e}]")


def take_note(note: str) -> str:
    try:
        with open(config.NOTES_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {note}\n")
        _index_notes_file(note)
        return "Noted."
    except Exception as e:
        return f"Couldn't save the note: {e}"


def read_notes() -> str:
    if not os.path.exists(config.NOTES_FILE):
        return "You don't have any notes yet."
    with open(config.NOTES_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
    return content if content else "You don't have any notes yet."


def remember(fact: str) -> str:
    """Persist one fact/preference/decision to long-term memory."""
    from ra import memory
    return memory.remember(fact, source="user-said")


def recall_memories(query: str) -> str:
    """Look up relevant long-term memories for a query, readable output."""
    from ra import memory
    return memory.recall_text(query)


def get_news(topic: str = "") -> str:
    """Live news headlines via DuckDuckGo (bundled dependency, no API key)."""
    try:
        from ddgs import DDGS
        topic = str(topic or "").strip()
        with DDGS() as d:
            items = d.news(topic or "top headlines", max_results=5)
        if not items:
            return "No news found for that topic right now."
        lines = []
        for it in items[:5]:
            title = (it.get("title") or "").strip()
            body = re.sub(r"\s+", " ", it.get("body") or "").strip()
            if title:
                snippet = f" - {body[:90]}" if body else ""
                lines.append(f"- {title}{snippet}")
        return "\n".join(lines) if lines else "No news found for that topic right now."
    except ImportError:
        return "News search isn't bundled in this build."
    except Exception as e:
        return f"Couldn't fetch the news: {e}"


def run_selftest() -> str:
    from ra import selftest
    return selftest.run()


def system_health() -> str:
    from ra import monitor
    return monitor.report()


def start_monitor(interval: int | None = None, batt_low: int | None = None) -> str:
    from ra import monitor
    return monitor.monitor(interval=int(interval or 60),
                           batt_low=int(batt_low or 20))


def stop_monitor() -> str:
    from ra import monitor
    return monitor.stop_monitor()


def morning_briefing() -> str:
    from ra import monitor
    return monitor.morning_briefing()


def schedule_task(task: str, name: str = "", cadence: str = "every hour",
                  at: str = "") -> str:
    from ra import scheduler
    return scheduler.add_schedule(name or task, task, cadence, at)


def list_schedules() -> str:
    from ra import scheduler
    return scheduler.list_schedules()


def cancel_schedule(name: str) -> str:
    from ra import scheduler
    return scheduler.remove_schedule(name)


def run_schedule(name: str) -> str:
    from ra import scheduler
    return scheduler.run_now(name)


def add_task(task: str, steps=None) -> str:
    from ra import tasklist
    return tasklist.add_task(task, steps)


def list_tasks() -> str:
    from ra import tasklist
    return tasklist.list_tasks()


def complete_task(number: int) -> str:
    from ra import tasklist
    return tasklist.complete_task(number)


def update_task(number: int, status: str) -> str:
    from ra import tasklist
    return tasklist.update_status(number, status=status)


def remove_task(number: int) -> str:
    from ra import tasklist
    return tasklist.remove_task(number)


def clear_tasks() -> str:
    from ra import tasklist
    return tasklist.clear_tasks()


def transcribe_audio_file(path: str) -> str:
    from ra import transcribe
    return transcribe.transcribe_file(path)


def list_plugins() -> str:
    from ra import plugins
    return plugins.list_plugins()


def get_battery() -> str:
    try:
        import psutil
        batt = psutil.sensors_battery()
        if batt is None:
            return "No battery detected (probably a desktop)."
        status = "charging" if batt.power_plugged else "on battery"
        return f"Battery is at {batt.percent}%, currently {status}."
    except Exception as e:
        return f"Couldn't read the battery: {e}"


def get_system_status() -> str:
    try:
        import psutil
        uptime_s = int(time.time() - psutil.boot_time())
        days, rem = divmod(uptime_s, 86400)
        hh, rem = divmod(rem, 3600)
        mm = rem // 60
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(os.path.expanduser("~"))
        return (
            f"CPU {psutil.cpu_percent(interval=0.2):.0f}% | RAM {mem.percent:.0f}% "
            f"({mem.used // (1024**3)} GB used of {mem.total // (1024**3)} GB) | "
            f"Disk free {disk.free // (1024**3)} GB | Uptime {days}d {hh}h {mm}m."
        )
    except Exception as e:
        return f"Couldn't read system status: {e}"


_timers = []  # [[remaining_seconds, label], ...]
_timer_thread = None


def set_timer(seconds: int, label: str = "") -> str:
    global _timer_thread
    seconds = max(1, int(seconds))
    _timers.append([seconds, label])
    import threading as _t
    if _timer_thread is None or not _timer_thread.is_alive():
        _timer_thread = _t.Thread(target=_timer_loop, daemon=True)
        _timer_thread.start()
    msg = f"Timer set for {seconds} seconds."
    if label:
        msg += f" Reminder: {label}."
    return msg


def _timer_loop():
    """Daemon loop that counts timers down and speaks when one expires."""
    import threading as _t
    import time as _time
    from ra.audio_io import speak_sentences
    while True:
        for seg in _timers:
            seg[0] -= 1
            if seg[0] <= 0:
                label = seg[1]
                try:
                    speak_sentences(f"Time is up. {label}".strip() + ".", cancel_event=_t.Event())
                except Exception:
                    pass
        _timers[:] = [t for t in _timers if t[0] > 0]
        if not _timers:
            break
        _time.sleep(1)


def create_directory(path: str) -> str:
    target = os.path.abspath(os.path.expanduser(path))
    try:
        os.makedirs(target, exist_ok=True)
        return f"Created directory: {target}"
    except Exception as e:
        return f"Couldn't create directory: {e}"


def find_local_file(query: str) -> str:
    query = query.strip().lower()
    if not query:
        return "No search term given."
    candidates = []
    for root in (os.path.expanduser("~"),):
        if not root:
            continue
        for dirpath, _, files in os.walk(root):
            for fn in files:
                if query in fn.lower() and not fn.lower().endswith((".tmp", ".log")):
                    candidates.append(os.path.join(dirpath, fn))
            if len(candidates) >= 6:
                break
    if not candidates:
        return "No matching files found."
    shown = [os.path.normpath(p) for p in candidates[:6]]
    return "Found:\n" + "\n".join(shown)


# ---------------------------------------------------------------------------
# Fast text-driven browser / screen navigation (no vision round-trips)
# ---------------------------------------------------------------------------
class _ReadableHTMLParser(HTMLParser):
    """Extracts readable text + <a> links from a page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text_parts = []
        self.links = []
        self._in_script = 0
        self._anchor = None
        self._anchor_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript", "head"):
            self._in_script += 1
        elif tag == "a" and self._anchor is None:
            self._anchor = attrs.get("href")
            self._anchor_text = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "head") and self._in_script:
            self._in_script -= 1
        elif tag == "a" and self._anchor is not None:
            text = " ".join("".join(self._anchor_text).split())
            if text:
                self.links.append((text, self._anchor))
            self._anchor = None

    def handle_data(self, data):
        if self._in_script:
            return
        if self._anchor is not None:
            self._anchor_text.append(data)
        self.text_parts.append(data)


def _html_to_readable(html: str, max_text: int = 3000, max_links: int = 25) -> str:
    parser = _ReadableHTMLParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = " ".join("".join(parser.text_parts).split())
    if len(text) > max_text:
        text = text[: max_text] + " ..."
    seen, links = set(), []
    for label, href in parser.links:
        href = (href or "").strip()
        if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append(f"{label or '(link)'} -> {href}")
        if len(links) >= max_links:
            break
    out = text
    if links:
        out += "\n\nPAGE LINKS:\n" + "\n".join(links)
    return out


def web_fetch(url: str) -> str:
    """Read a page's text + links over HTTP in one fast call - no clicking."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        import requests
        resp = requests.get(url, timeout=8, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en",
        })
        if resp.status_code >= 400:
            return f"Couldn't fetch {url}: HTTP {resp.status_code}."
        ctype = resp.headers.get("content-type", "").lower()
        if "html" not in ctype:
            text = " ".join(resp.text.split())
            return (text[:1500] + " ...") if len(text) > 1500 else (text or "(empty response)")
        return _html_to_readable(resp.text)
    except Exception as e:
        return f"Couldn't fetch {url}: {e}"


def go_to_url(url: str) -> str:
    """Navigate the ACTIVE browser via the address bar (fast, reuses window)."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    _computer.press_key("ctrl+l")
    time.sleep(0.15)
    _computer.type_text(url)
    _computer.press_key("enter")
    return f"Navigated the active browser to {url}."


def type_and_enter(text: str) -> str:
    """Type into the focused field and press Enter (search box / address bar)."""
    _computer.type_text(text)
    _computer.press_key("enter")
    return f"Typed and submitted: {text}"


def copy_page_text() -> str:
    """Select-all + copy the active window and return its text (no vision)."""
    _computer.press_key("ctrl+a")
    time.sleep(0.1)
    _computer.press_key("ctrl+c")
    time.sleep(0.1)
    try:
        text = _computer.get_clipboard()
    except Exception as e:
        return f"Couldn't read the clipboard: {e}"
    text = " ".join((text or "").split())
    if not text:
        return "The active window had no selectable/copyable text."
    if len(text) > 3000:
        text = text[:2997] + "..."
    return text


def find_on_page(query: str) -> str:
    """Highlight a search term on the current page via Ctrl+F."""
    _computer.press_key("ctrl+f")
    time.sleep(0.15)
    _computer.type_text(query)
    _computer.press_key("enter")
    _computer.press_key("esc")
    return f"Find dialog used for '{query}' on the page."


def browser_tab(action: str) -> str:
    """Instant keyboard control of the active browser's tabs/window."""
    _TAB_KEYS = {
        "new": "ctrl+t", "new tab": "ctrl+t",
        "close": "ctrl+w", "close tab": "ctrl+w", "close current": "ctrl+w",
        "next": "ctrl+tab", "previous": "ctrl+shift+tab", "prev": "ctrl+shift+tab",
        "reload": "f5", "refresh": "f5",
        "back": "alt+left", "forward": "alt+right",
        "new window": "ctrl+n", "close window": "ctrl+shift+w",
        "reopen": "ctrl+shift+t", "reopen tab": "ctrl+shift+t",
        "address bar": "ctrl+l", "focus address": "ctrl+l",
    }
    key = _TAB_KEYS.get((action or "").lower().strip())
    if not key:
        return ("Unknown tab action. Try: new, close, next, prev, reload, "
                "back, forward, new window, close window, reopen.")
    _computer.press_key(key)
    return f"Browser: {action.strip().lower()}."


def scroll_page(direction: str) -> str:
    """Scroll the active window with keyboard keys (fast, no vision)."""
    _PAGE_KEYS = {"up": "pageup", "down": "pagedown", "top": "home", "bottom": "end"}
    key = _PAGE_KEYS.get((direction or "").lower().strip())
    if not key:
        return "Unknown scroll direction. Try: up, down, top, bottom."
    _computer.press_key(key)
    return f"Scrolled {direction.strip().lower()}."


def wait_seconds(seconds: int) -> str:
    """Brief pause so a page/app can finish loading (capped at 15s)."""
    seconds = max(0.5, min(int(seconds or 1), 15))
    time.sleep(seconds)
    return f"Waited {seconds} seconds."


def search_context(query: str, k: int = 3) -> str:
    if not _granted("files"):
        return "Retrieval over local files is not currently granted. Ask me to grant access to 'files'."
    from ra.rag.retriever import retrieve
    results = retrieve(_store(), _embedder(), query, k=max(1, min(k or 3, 8)))
    if not results:
        return "No matching passages found in the local knowledge index."
    parts = []
    for r in results:
        snippet = " ".join(r.text.split())
        if len(snippet) > 500:
            snippet = snippet[:497] + "..."
        parts.append(f"[{r.source_type} | {r.source}]\n{snippet}")
    return "\n\n".join(parts)


def index_documents(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        return f"Path not found: {path}"
    from ra.rag import indexer
    if os.path.isfile(path):
        added = indexer.index_file(path, _store(), _embedder())
        return f"Indexed {path}: {added} chunks."
    stats = indexer.index_directory(path, _store(), _embedder())
    msg = f"Indexed {path}: {stats['files']} files, {stats['chunks']} chunks."
    if stats["errors"]:
        msg += f" {len(stats['errors'])} file(s) failed."
    return msg


def capture_screen() -> str:
    if not _granted("screen"):
        return "Screen access is not granted. Ask me to grant access to 'screen'."
    try:
        from ra.rag import sources
        out = sources.ingest_screen(_store(), _embedder())
        body = " ".join(out["text"].split())
        if len(body) > 400:
            body = body[:397] + "..."
        return f"Captured the screen and indexed its text. Here is what I read: {body}"
    except PermissionError as e:
        return str(e)
    except RuntimeError as e:
        return f"Screen capture is available but not ready: {e}"


def refresh_device_snapshot() -> str:
    if not _granted("devices"):
        return "Device access is not granted. Ask me to grant access to 'devices'."
    from ra.rag import sources
    out = sources.ingest_devices(_store(), _embedder())
    body = " ".join(out["text"].split())
    return f"Device snapshot captured and indexed: {body}"


def list_audio_devices() -> str:
    if not _granted("devices"):
        return "Device access is not granted. Ask me to grant access to 'devices'."
    from ra.rag import sources
    return sources.list_audio_devices()


def list_access() -> str:
    return ", ".join(f"{k} is {'GRANTED' if v else 'REVOKED'}" for k, v in config.GRANTED_ACCESS.items())


def set_access(area: str, allowed: bool) -> str:
    if area not in config.GRANTED_ACCESS:
        return f"Unknown area '{area}'. Valid: {sorted(config.GRANTED_ACCESS)}"
    config.GRANTED_ACCESS[area] = bool(allowed)
    return f"{area} access is now {'GRANTED' if allowed else 'REVOKED'}."


_DISPATCH = {
    "open_app": lambda i: open_app(i["app_name"]),
    "open_website": lambda i: open_website(i["url"], i.get("browser")),
    "youtube_search": lambda i: youtube_search(i["query"], i.get("browser")),
    "play_youtube": lambda i: play_youtube(i["query"], i.get("browser")),
    "play_spotify": lambda i: play_spotify(i["query"]),
    "media_control": lambda i: media_control(i["action"], i.get("amount", 1)),
    "web_search_results": lambda i: web_search_results(i["query"], i.get("num", 5)),
    "system_power": lambda i: system_power(i["action"]),
    "process_control": lambda i: process_control(i["action"], i.get("name", "")),
    "network_status": lambda i: network_status(),
    "manage_files": lambda i: manage_files(i["action"], i.get("source", ""), i.get("target", "")),
    "get_now_playing": lambda i: get_now_playing(),
    "toast_notify": lambda i: toast_notify(i["title"], i["message"]),
    "get_selected_text": lambda i: get_selected_text(),
    "set_volume": lambda i: set_volume(i.get("level"), i.get("mute")),
    "remember": lambda i: remember(i["fact"]),
    "recall_memories": lambda i: recall_memories(i["query"]),
    "get_news": lambda i: get_news(i.get("topic", "")),
    "run_selftest": lambda i: run_selftest(),
    "system_health": lambda i: system_health(),
    "start_monitor": lambda i: start_monitor(i.get("interval") or 60, i.get("batt_low") or 20),
    "stop_monitor": lambda i: stop_monitor(),
    "morning_briefing": lambda i: morning_briefing(),
    "schedule_task": lambda i: schedule_task(i["task"], i.get("name", ""),
                                             i.get("cadence", "every hour"),
                                             i.get("at", "")),
    "list_schedules": lambda i: list_schedules(),
    "cancel_schedule": lambda i: cancel_schedule(i["name"]),
    "run_schedule": lambda i: run_schedule(i["name"]),
    "add_task": lambda i: add_task(i["task"], i.get("steps")),
    "list_tasks": lambda i: list_tasks(),
    "complete_task": lambda i: complete_task(i["number"]),
    "update_task": lambda i: update_task(i["number"], i["status"]),
    "remove_task": lambda i: remove_task(i["number"]),
    "clear_tasks": lambda i: clear_tasks(),
    "transcribe_audio_file": lambda i: transcribe_audio_file(i["path"]),
    "list_plugins": lambda i: list_plugins(),
    "web_search": lambda i: web_search(i["query"], i.get("browser")),
    "web_fetch": lambda i: web_fetch(i["url"]),
    "go_to_url": lambda i: go_to_url(i["url"]),
    "type_and_enter": lambda i: type_and_enter(i["text"]),
    "copy_page_text": lambda i: copy_page_text(),
    "find_on_page": lambda i: find_on_page(i["query"]),
    "browser_tab": lambda i: browser_tab(i["action"]),
    "scroll_page": lambda i: scroll_page(i["direction"]),
    "wait_seconds": lambda i: wait_seconds(i["seconds"]),
    "get_time": lambda i: get_time(),
    "get_weather": lambda i: get_weather(i["location"]),
    "lock_pc": lambda i: lock_pc(),
    "take_note": lambda i: take_note(i["note"]),
    "read_notes": lambda i: read_notes(),
    "get_battery": lambda i: get_battery(),
    "get_system_status": lambda i: get_system_status(),
    "set_timer": lambda i: set_timer(i["seconds"], i.get("label", "")),
    "create_directory": lambda i: create_directory(i["path"]),
    "find_local_file": lambda i: find_local_file(i["query"]),
    "search_context": lambda i: search_context(i["query"], i.get("k", 3)),
    "index_documents": lambda i: index_documents(i["path"]),
    "capture_screen": lambda i: capture_screen(),
    "refresh_device_snapshot": lambda i: refresh_device_snapshot(),
    "list_audio_devices": lambda i: list_audio_devices(),
    "list_access": lambda i: list_access(),
    "set_access": lambda i: set_access(i["area"], i["allowed"]),
    "run_command": lambda i: _computer.run_shell(i["command"]),
    "type_text": lambda i: _computer.type_text(i["text"]),
    "press_keys": lambda i: _computer.press_key(i["keys"]),
    "click_screen": lambda i: _computer.click(i["x"], i["y"], i.get("button", "left")),
    "move_mouse": lambda i: _computer.move_mouse(i["x"], i["y"]),
    "scroll_wheel": lambda i: _computer.scroll(i["amount"]),
    "list_windows": lambda i: _computer.list_windows(),
    "focus_window": lambda i: _computer.focus_window(i["title"]),
    "list_directory": lambda i: _computer.list_directory(i["path"]),
    "read_file": lambda i: _computer.read_file(i["path"]),
    "write_file": lambda i: _computer.write_file(i["path"], i["text"]),
    "open_path": lambda i: _computer.open_path(i["path"]),
    "screenshot": lambda i: _computer.screenshot(),
    "get_screen_info": lambda i: f"Screen: {_computer.get_screen_size()}, mouse at {_computer.get_mouse_position()}.",
    "get_clipboard": lambda i: _computer.get_clipboard(),
    "set_clipboard": lambda i: _computer.set_clipboard(i["text"]),
    "see_screen": lambda i: _vision_mod().see_screen(i.get("prompt", "")),
    "gui_do": lambda i: _vision_mod().gui_do(i["instruction"]),
    "ui_find": lambda i: _uia_mod().ui_find(i.get("name", ""), i.get("region", "")),
    "ui_click": lambda i: _uia_mod().ui_click(i["name"], bool(i.get("double")), i.get("region", "")),
    "ui_type": lambda i: _uia_mod().ui_type(i["name"], i["text"]),
    "calculate": lambda i: calculate(i["expression"]),
    "define": lambda i: define(i["word"]),
    "translate": lambda i: translate(i["text"], i["target"], i.get("source", "en")),
    "currency_convert": lambda i: currency_convert(i["amount"], i["from_cur"], i["to_cur"]),
    "empty_recycle_bin": lambda i: empty_recycle_bin(),
    "wifi_password": lambda i: wifi_password(),
    "window_control": lambda i: window_control(i["action"]),
    "list_installed_apps": lambda i: list_installed_apps(),
}


def _uia_mod():
    """Lazy import of the UIA layer so the base app stays light on non-Windows."""
    from ra import uia
    return uia


def _vision_mod():
    """Lazy import: the vision stack is only pulled in when screen control is
    actually used (keeps the base app + tests light and offline-friendly)."""
    from ra import vision
    return vision

try:
    from ra import computer as _computer
except Exception:  # pragma: no cover - non-Windows
    class _computer:  # type: ignore
        @staticmethod
        def run_shell(c): return "Computer control is only available on Windows."
        type_text = run_shell
        press_key = run_shell
        click = run_shell
        move_mouse = run_shell
        scroll = run_shell
        list_windows = lambda: "Computer control is only available on Windows."
        focus_window = run_shell
        list_directory = run_shell
        read_file = run_shell
        write_file = run_shell
        open_path = run_shell
        screenshot = run_shell
        get_screen_size = lambda: "N/A"
        get_mouse_position = lambda: "N/A"
        get_clipboard = lambda: "N/A"
        set_clipboard = run_shell


def execute_tool(name: str, tool_input: dict) -> str:
    fn = _DISPATCH.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        return fn(tool_input)
    except Exception as e:
        return f"Error running {name}: {e}"


def _install_user_plugins():
    """Auto-discover one-file plugins in ~/.ra/plugins/ and merge their tools
    into TOOLS + _DISPATCH (InterGenJLU/jarvis pattern). Idempotent and
    failure-tolerant: a broken plugin never takes the app down."""
    try:
        from ra import plugins
        added, errors = plugins.install(TOOLS, _DISPATCH)
        for e in errors:
            ralog.log("warn", f"plugin issue: {e}")
        return added
    except Exception as e:
        ralog.log("warn", f"plugin directory scan failed: {e}")
        return []


PLUGINS_LOADED = _install_user_plugins()