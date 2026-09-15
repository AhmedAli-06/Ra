"""
Ra Memory
===========
Log-based persistent memory: Ra remembers facts, preferences, and session
summaries across restarts, then auto-injects the most relevant memories into
every conversation (the pattern every serious open-source assistant uses -
Vellum, OpenJarvis, Thoth, Mark-LI, vierisid/jarvis).

Storage is a simple append-only JSONL file in the RA data dir - no database,
no extra wheels. Retrieval is a keyword-overlap + recency hybrid (stdlib only).
"""
import json
import os
import re
import time

from ra import config

_MEMORY_FILE = os.path.join(config.DATA_DIR, "memory.jsonl")
_MAX_RECALL = 8          # how many memories to inject per conversation
_PROMPT_TEMPLATE = (
    "Memories you should draw on (from earlier sessions; use them only if "
    "relevant, and do not repeat them as news):\n{lines}\n---"
)


def _read_all() -> list[dict]:
    if not os.path.exists(_MEMORY_FILE):
        return []
    entries = []
    try:
        with open(_MEMORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries


def _write(entry: dict):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(_MEMORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def remember(fact: str, source: str = "") -> str:
    """Persist one fact/preference/decision for future sessions. Secrets are
    redshift masked before they ever reach disk."""
    fact = str(fact or "").strip()
    if not fact:
        return "Nothing to remember."
    from ra import redact
    safe = redact.redact(fact[:600])
    _write({
        "fact": safe,
        "ts": int(time.time()),
        "source": source,
    })
    if safe != fact[:600]:
        return "Remembered (some details were masked for privacy)."
    return "Remembered. I'll keep that in mind going forward."


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    stops = {
        "the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on",
        "and", "or", "for", "with", "about", "at", "by", "from", "your", "my",
        "i", "you", "it", "that", "this", "do", "does", "did", "be", "been",
        "me", "we", "our", "us", "not", "no", "yes", "can", "could", "would",
        "should", "will", "shall", "please", "have", "has", "had", "what",
        "when", "where", "which", "who", "whom", "how", "need", "want",
    }
    return set(words) - stops


def recall(query: str, limit: int | None = None) -> list[dict]:
    """Best-effort keyword+recency retrieval over logged memories. Returns
    [{'fact','ts'}] sorted by (overlap, recency)."""
    q = _tokens(query or "")
    entries = _read_all()
    scored = []
    for e in entries:
        fact = e.get("fact", "")
        if not fact:
            continue
        ft = _tokens(fact)
        overlap = len(q & ft) if q else 0
        scored.append((overlap, e))
    scored.sort(key=lambda x: (x[0], x[1].get("ts", 0)), reverse=True)
    kept = [e for s, e in scored if s >= 1]
    return kept[: (limit or _MAX_RECALL)]


def recall_text(query: str, limit: int | None = None) -> str:
    """Readable recall for the LLM-facing tool: '• fact (time ago)' lines."""
    got = recall(query, limit)
    if not got:
        return "No relevant memories yet."
    lines = []
    for e in got:
        ago = _fuzzy_ago(e.get("ts", 0))
        lines.append(f"- {e.get('fact', '')} ({ago})")
    return "\n".join(lines)


def _fuzzy_ago(ts: float) -> str:
    d = int(time.time() - ts)
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60} min ago"
    if d < 86400:
        return f"{d // 3600} h ago"
    if d < 7 * 86400:
        return f"{d // 86400} d ago"
    return f"{d // (7 * 86400)} w ago"


def inject_context(query: str) -> str | None:
    """Return prompt blob of memories relevant to `query`, or None."""
    lines = recall_text(query)
    if lines.startswith("No relevant"):
        return None
    return _PROMPT_TEMPLATE.format(lines=lines)


def summarize_session(turns: list[dict], limit: int = 400) -> str | None:
    """Compress a list of {role, content} conversation turns into one memory
    line stored for the future. Returns None when there's nothing worth
    storing. Simple lexical approach - no LLM round-trip needed."""
    if not turns:
        return None
    user_lines = [t.get("content", "").strip() for t in turns if t.get("role") == "user"]
    user_lines = [u for u in user_lines if u]
    if not user_lines:
        return None
    top = " | ".join(user_lines[:6])[:int(limit)]
    remember(f"Session covered: {top}", source="session-summary")
    return top