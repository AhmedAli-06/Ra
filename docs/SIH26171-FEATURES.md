# Ra — Feature & Skills Catalog

**SIH26171 | Team EtherealSpark**

Ra exposes **90 structured tool schemas** to the LLM brain. Each tool is a JSON schema the model can call via function-calling. The brain dispatches to the matching Python function, executes it on the user's PC, and returns the result.

Tools are grouped into categories below.

---

## 1. Application & Browser Control (12 tools)

| Tool | Description |
|---|---|
| `open_app` | Launch any Windows application by name (notepad, chrome, spotify, calc, 50+ known apps) |
| `open_website` | Open a URL in a named browser (Brave, Chrome, Edge, Firefox, Opera, Vivaldi) or the default |
| `youtube_search` | Open YouTube search results for a query in a named browser |
| `play_youtube` | Search YouTube over HTTP and play the top video result immediately — one call, no clicking |
| `play_spotify` | Search Spotify and play the top result via desktop app (URI scheme + UIA double-click + SMTC verification) or web player fallback |
| `web_search` | Open a Google search in a named browser |
| `web_search_results` | Fetch top search results (title, URL, snippet) over HTTP — no browser needed |
| `web_fetch` | Read a page's full text + links over HTTP in one fast GET |
| `go_to_url` | Navigate the active browser to a URL via address bar (reuses open window) |
| `browser_tab` | Tab control: new, close, next, prev, reload, back, forward, new window, close window, reopen |
| `type_and_enter` | Type text into the focused field and press Enter |
| `copy_page_text` | Select-all + copy the active window and return its text |

---

## 2. Screen Vision & Accessible UI (6 tools)

| Tool | Description |
|---|---|
| `see_screen` | Look at the screen via the vision LLM — describes what is visible, reads UI elements |
| `gui_do` | Autonomous visual screen control — watch, click, type, scroll through multi-step UI tasks with post-action verification |
| `ui_find` | Read the Windows UIAutomation tree — get names and exact pixel rectangles of real controls (buttons, menu items, list rows, edit fields) |
| `ui_click` | Click a named control exactly via the accessible tree — no pixel guessing; supports double-click for play/open |
| `ui_type` | Click a named text field and type into it via the accessible tree |
| `screenshot` | Take a full-screen screenshot and save it locally |

---

## 3. Media & Playback (5 tools)

| Tool | Description |
|---|---|
| `media_control` | Global media keys — play/pause/resume, next, previous, stop, volume up/down, mute. Verified via OS media session (SMTC) |
| `get_now_playing` | Read the actual playing track from OS media transport — title, artist, app, status |
| `get_selected_text` | Silently copy the user's current selection and return it |
| `set_volume` | Set system master volume (0–100) or mute/unmute via Windows Core Audio API |
| `scroll_page` | Scroll the active window — up, down, top, bottom via keyboard |

---

## 4. File & System Management (14 tools)

| Tool | Description |
|---|---|
| `run_command` | Run a shell command on the PC and return output |
| `manage_files` | Copy, move, rename, or delete files |
| `create_directory` | Create a directory (and parents) at an absolute path |
| `find_local_file` | Search home/documents folders for files by name |
| `list_directory` | List folder contents |
| `read_file` | Read a text file (first 3000 chars) |
| `write_file` | Write/replace a text file (creates folders) |
| `open_path` | Open a file or folder in Explorer / default app |
| `process_control` | List open apps or close a specific app |
| `list_windows` | List visible open windows |
| `focus_window` | Bring a window to the foreground by title |
| `window_control` | Minimize, maximize, restore, or close the current window |
| `list_installed_apps` | List programs from the Windows registry |
| `empty_recycle_bin` | Empty the Windows Recycle Bin |

---

## 5. Keyboard & Input (4 tools)

| Tool | Description |
|---|---|
| `type_text` | Type text into the focused field |
| `press_keys` | Press a key or chord (ctrl+c, win+d, alt+tab, f11, etc.) |
| `click_screen` | Move the mouse and click at pixel coordinates |
| `move_mouse` | Move the mouse without clicking |

---

## 6. Clipboard & Screen Info (4 tools)

| Tool | Description |
|---|---|
| `get_clipboard` | Read clipboard text |
| `set_clipboard` | Copy text to the clipboard |
| `get_screen_info` | Get screen resolution and mouse position |
| `scroll_wheel` | Scroll the mouse wheel (positive = up, negative = down) |

---

## 7. RAG — Retrieval-Augmented Generation (4 tools)

| Tool | Description |
|---|---|
| `search_context` | Search the local knowledge index (indexed files, notes, screen captures, device snapshots) and return relevant passages |
| `index_documents` | Index a folder of documents (txt, md, pdf, code, csv, docx, xlsx, odt, epub, rtf) into the local knowledge base |
| `capture_screen` | Capture screen text via local OCR and index it — pixels never leave the device |
| `refresh_device_snapshot` | Snapshot system + audio devices and index it |

---

## 8. Voice & Memory (4 tools)

| Tool | Description |
|---|---|
| `remember` | Store a fact/preference in long-term memory for future sessions |
| `recall_memories` | Search long-term memory for previously stored facts |
| `transcribe_audio_file` | Transcribe a local audio/video file offline |
| `take_note` | Save a timestamped note (auto-indexed) |
| `read_notes` | Read back all saved notes |

---

## 9. Scheduling & Planning (10 tools)

| Tool | Description |
|---|---|
| `schedule_task` | Schedule a background task — "every hour", "daily at 09:00", "every 30 minutes" |
| `list_schedules` | List all scheduled tasks and next run times |
| `cancel_schedule` | Remove a scheduled task |
| `run_schedule` | Fire a scheduled task immediately |
| `add_task` | Add a task or multi-step plan to the persistent to-do list |
| `list_tasks` | Show the current to-do list with statuses |
| `complete_task` | Mark a task as done |
| `update_task` | Set a task to todo/doing/done |
| `remove_task` | Delete a task from the list |
| `clear_tasks` | Empty the entire to-do list |

---

## 10. System Health & Monitoring (5 tools)

| Tool | Description |
|---|---|
| `run_selftest` | Run capability self-test — network, news, telemetry, screen/OCR, audio, RAG, memory, fast lane |
| `system_health` | One-shot read of CPU, RAM, disk, battery |
| `start_monitor` | Start background system watcher with threshold alerts |
| `stop_monitor` | Stop the background watcher |
| `morning_briefing` | Morning recap — time, weather, battery, system, headlines |

---

## 11. Network & Connectivity (3 tools)

| Tool | Description |
|---|---|
| `network_status` | Report Wi-Fi SSID, local IP, public IP |
| `wifi_password` | Get the current Wi-Fi network name and saved password |
| `find_on_page` | Find/highlight text on the current page (Ctrl+F) |

---

## 12. Knowledge & Utilities (6 tools)

| Tool | Description |
|---|---|
| `calculate` | Evaluate math expressions (percent, powers, sqrt, trig, log) — safe AST evaluation, no `eval()` |
| `define` | Dictionary definition via dictionaryapi.dev |
| `translate` | Translate text via MyMemory (free, no key) |
| `currency_convert` | Live FX conversion via open.er-api.com |
| `get_time` | Current local date and time |
| `get_weather` | Current weather for a location via Open-Meteo |

---

## 13. System Power & Notifications (4 tools)

| Tool | Description |
|---|---|
| `system_power` | Shutdown, restart, sleep, sign out, lock |
| `lock_pc` | Lock the PC immediately (Win+L) |
| `toast_notify` | Windows toast notification — title + message |
| `set_timer` | Spoken timer — Ra speaks when time's up |

---

## 14. Consent & Access Control (2 tools)

| Tool | Description |
|---|---|
| `list_access` | Show which data sources are granted/revoked |
| `set_access` | Grant or revoke access: files, devices, screen, computer |

---

## 15. Plugin System (1 tool)

| Tool | Description |
|---|---|
| `list_plugins` | List user-installed skill plugins from `~/.ra/plugins/` |

---

## Tool Count Summary

| Category | Count |
|---|---|
| Application & Browser | 12 |
| Screen Vision & Accessible UI | 6 |
| Media & Playback | 5 |
| File & System Management | 14 |
| Keyboard & Input | 4 |
| Clipboard & Screen Info | 4 |
| RAG | 4 |
| Voice & Memory | 5 |
| Scheduling & Planning | 10 |
| System Health & Monitoring | 5 |
| Network & Connectivity | 3 |
| Knowledge & Utilities | 6 |
| System Power & Notifications | 4 |
| Consent & Access Control | 2 |
| Plugin System | 1 |
| **Total** | **85 core tools** |

> Additional tools are dynamically added at runtime via the plugin system (`~/.ra/plugins/*.py`), bringing the effective tool count to 90+.

---

## LLM Brain Models

| Provider | Default Model | Base URL |
|---|---|---|
| Gemini | `gemini-3.6-flash` | `generativelanguage.googleapis.com` |
| Groq | `openai/gpt-oss-120b` | `api.groq.com` |
| NVIDIA NIM | `nvidia/nemotron-3-super-120b-a12b` | `integrate.api.nvidia.com` |

Self-healing model fallback: when a model returns 404/401, Ra automatically retries the next candidate and remembers the working model for the rest of the session.

---

*Document prepared for SIH26171 submission by Team EtherealSpark.*
