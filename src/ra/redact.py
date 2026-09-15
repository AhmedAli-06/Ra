"""
Ra Redaction
=============
Sensitive-info auto-redaction, borrowed from isair/jarvis. Before anything the
user says (or a tool's arguments) is persisted to memory or written to the
log, we mask secrets so plaintext keys/numbers never land on disk.

This is cheap, deterministic, and stdlib-only:
- API keys / bearer tokens
- E-mail addresses, phone numbers, credit-card numbers
- Long hex/base64 secrets and obvious long numeric IDs
"""
import re

_PATTERNS = [
    # API keys, bearer tokens, sk- / gsk- / AIza prefixed keys
    re.compile(r"\b(?:sk|sk-[A-Za-z0-9_\-]{16,})\b"),
    re.compile(r"\b(?:gsk|gsk_[A-Za-z0-9]{16,})\b"),
    re.compile(r"\b(?:AIza[0-9A-Za-z_\-]{20,})\b"),
    re.compile(r"\b(?:Bearer\s+)([0-9A-Za-z\-._~+/]+=*)", re.I),
    # E-mail addresses
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    # Phone numbers (international / spaced / dashed)
    re.compile(r"\b(?:\+?\d{1,3}[\s\-.]?)?(?:\(?\d{2,4}\)?[\s.\-]?){2,4}\d{2,4}\b"),
    # Credit-card shapes (13-19 digits, option on groups of 4)
    re.compile(r"\b(?:\d[ \-]?){12,18}\d\b"),
    # Long hex secrets (8+ hex chars), long base64-ish runs, long digit runs
    re.compile(r"\b[0-9a-fA-F]{16,}\b"),
    re.compile(r"\b[0-9A-Za-z+/]{24,}={0,2}\b"),
    re.compile(r"\b\d{8,}\b"),
]

CRC_NETWORK = {
    "1.1.1.1", "8.8.8.8", "8.8.4.4", "0.0.0.0", "127.0.0.1", "255.255.255.255",
    "192.168.0.1", "192.168.1.1", "10.0.0.1", "172.16.0.1",
}

_MASK = "[REDACTED]"


def redact(text) -> str:
    """Mask sensitive values in `text`. Normal prose is untouched."""
    if not text:
        return str(text or "")
    t = str(text)
    for pat in _PATTERNS:
        t = pat.sub(_MASK, t)
    return t


def contains_secret(text) -> bool:
    """True when `text` would be modified by redaction (a secret is present)."""
    return redact(text) != str(text)


def log_safe(text) -> str:
    """Short form for log lines: redact, then truncate keeps context cheap."""
    return str(redact(text))[:200]