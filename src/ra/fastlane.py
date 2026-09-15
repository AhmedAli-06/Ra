"""
Ra Fast Lane
=============
Deterministic answer path for well-formed trivial queries (time, date,
battery, system status, timers/reminders). Skips the LLM round-trip entirely
for these, giving instant offline answers - the "fast lane" every serious
open-source voice assistant borrows for perceived latency.

Only fires when the ENTIRE utterance matches a strict pattern. Anything
freeform, ambiguous, or in the deny set falls through to the full brain.
"""
import re

from ra import skills

_DENY = (
    "explain", "explain ", "works", "how do", "how does", "how to",
    "why ", "definition", "recycle", "solar", "water", "charge my phone",
    "charge the phone", "phone battery", "car battery", "boosts",
    "really ", "long ", "solid ", "what is a ", "what is an ", "what's a ",
)

_TIME_RE = re.compile(
    r"^(?:whats|what'?s|what is|what)\s+(?:is\s+|the\s+|current\s+|today'?s\s+)?"
    r"(?:the\s+|current\s+|today'?s\s+)?(?:time|date|day)"
    r"(?:\s+right\s+now|\s+now)?\??$"
)
_TIME2_RE = re.compile(r"^what\s+time\s+is\s+it\??$")
_DATE_RE = re.compile(
    r"^(?:whats|what'?s|what is|what)\s+(?:is\s+|the\s+)?date\??$"
)
_DOW_RE = re.compile(
    r"^(?:what\s+day\s+is\s+it|what\s+day\s+is\s+today|what'?s?\s+the\s+day)\??$"
)
_BATT_RE = re.compile(
    r"^(?:whats|what'?s|what is|how is|how'?s|how much)\s+"
    r"(?:is\s+|the\s+|my\s+)?(?:battery\s+)?(?:battery|charge|charge level|"
    r"battery level|battery percentage)(?:\s+(?:right now|level|at))?\??$"
)
_BATT2_RE = re.compile(
    r"^(?:check|get|read)\s+(?:the\s+|my\s+)?(?:battery|charge)\??$"
)
_SYS_RE = re.compile(
    r"^(?:whats|what'?s|what is|how is|how'?s)\s+(?:the\s+)?"
    r"(?:system|cpu|ram|memory|pc|computer)\s+"
    r"(?:status|usage|load|doing|health|condition)\??$"
)
_TIMER_RE = re.compile(
    r"^(?:set|start)\s+(?:a\s+|an\s+|the\s+)?timer\s+(?:for\s+)?"
    r"(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours)?"
    r"(?:\s+(?:for|to|about|on|with label|:|,)?\s*(.*))?$"
)
_REMIND_RE = re.compile(
    r"^remind me\s+(?:in|after)\s+(\d+)\s*"
    r"(s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours)\s+(?:to\s+|about\s+|that\s+)?(.*)$",
    re.IGNORECASE,
)

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
}


def _deny(user_text: str) -> bool:
    low = user_text.lower()
    return any(d in low for d in _DENY)


def _s(_m) -> str:
    return " ".join((_m.group(3) or "").strip().split())


def try_answer(user_text: str):
    """Return an instant answer string, or None to fall through to the LLM."""
    text = (user_text or "").strip()
    if not text:
        return None
    low = text.lower()

    # Timers/reminders are unambiguous commands - run them before the deny
    # guard (their labels legitimately mention food, water, chores, ...).
    tm = _TIMER_RE.fullmatch(low)
    if tm:
        sec = int(tm.group(1)) * _UNITS.get((tm.group(2) or "").lower(), 1)
        label = _s(tm)
        return skills.set_timer(sec, label)

    rm = _REMIND_RE.fullmatch(text)
    if rm:
        sec = int(rm.group(1)) * _UNITS.get(rm.group(2).lower(), 1)
        label = (rm.group(3) or "").strip()
        return skills.set_timer(sec, f"remind me to {label}")

    if _deny(text):
        return None

    if _DATE_RE.fullmatch(low) or _TIME_RE.fullmatch(low) or _TIME2_RE.fullmatch(low) or _DOW_RE.fullmatch(low):
        return skills.get_time()

    if _BATT_RE.fullmatch(low) or _BATT2_RE.fullmatch(low):
        return skills.get_battery()

    if _SYS_RE.fullmatch(low):
        return skills.get_system_status()

    return None


def broadcast_light():
    """The same fast lane but as a greeting probe - avoids pulling the full
    LLM for a generic 'anything new?' probe; returns None (LLM handles)."""
    return None