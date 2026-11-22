"""mycore.redact — output-side secret masking (C3 L1).

The gap this closes (audit #16 / B-3): the slot discipline stops a secret
from being INJECTED, but it cannot stop a secret that `run_bash` or a file
read picked up from flowing back OUT in a reply or a channel message.

What this module does:
  * runs a narrow, high-confidence secret-shape regex set over OUTBOUND
    text (the reply the owner reads, a channel message, a mission report);
  * replaces the secret VALUE with a shape-preserving mask (so the owner can
    still see that a credential was there) and counts the hits;
  * never prints, logs or returns the matched value.

What it explicitly does NOT do (the 2026-10-31 ruling — we never pretend):
  * it does not constrain `run_bash` — the shell stays a declared trust
    boundary; C3 L2 refuses a narrow list of high-risk shapes separately;
  * it does not touch the model's INPUT (masking what the model reads would
    break legitimate work);
  * it does not guess: a shape that does not match is left alone.

Stdlib only; English comments (repo i18n policy).
"""
from __future__ import annotations

import re

# (name, regex, mask). Each pattern matches a VALUE, not a label, so the
# mask replaces the credential itself and keeps the surrounding text. A mask
# may contain the placeholder "\1" (the shape prefix) — the substitution
# only uses it when the pattern actually has that group.
PATTERNS: tuple[tuple[str, "re.Pattern[str]", str], ...] = (
    ("openai-like key",
     re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"), r"\1-***"),
    ("github token",
     re.compile(r"\b(gh[pousr])_[A-Za-z0-9]{16,}\b"), r"\1_***"),
    ("slack token",
     re.compile(r"\b(xox[baprs])-[A-Za-z0-9-]{10,}\b"), r"\1-***"),
    ("aws access key id",
     re.compile(r"\b(AKIA)[0-9A-Z]{16}\b"), r"\1***"),
    ("azure storage key",
     re.compile(r"\b(AccountKey=)[A-Za-z0-9+/=]{40,}"), r"\1***"),
    ("connection string password",
     re.compile(r"(?i)\b(password|pwd)\s*=\s*([^\s;'\"]{6,})"),
     r"\1=***"),
    ("assigned secret literal",
     re.compile(
         r"(?i)\b(api[_-]?key|apikey|secret|token|passwd|password)"
         r"\s*[=:]\s*[\"']?([A-Za-z0-9_\-./+=]{12,})[\"']?"),
     r"\1=***"),
    ("private key block",
     re.compile(
         r"-----BEGIN (?!.*PUBLIC KEY)[A-Z ]*PRIVATE KEY-----"
         r".*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "***"),
    ("telegram bot token",
     re.compile(r"\b\d{6,10}:[A-Za-z0-9_-]{30,}\b"), "***"),
    ("jwt",
     re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
     "***"),
)

MAX_HITS_REPORTED = 5


def mask_secrets(text: str) -> tuple[str, int]:
    """Mask secret-shaped values in one outbound text.

    Returns (masked_text, hit_count). Never raises; a non-string input comes
    back unchanged with zero hits."""
    if not isinstance(text, str) or not text:
        return (text if isinstance(text, str) else ""), 0
    out = text
    hits = 0
    for name, pattern, mask in PATTERNS:
        def _sub(m: "re.Match[str]") -> str:
            nonlocal hits
            hits += 1
            if "\\1" in mask and m.re.groups >= 1:
                return mask.replace("\\1", m.group(1) or "")
            return mask
        out = pattern.sub(_sub, out)
    return out, hits


def mask_secrets_with_note(text: str, note_key: str = "") -> tuple[str, int]:
    """Mask and, when hits occurred, append a short honest note.

    The note never contains the matched value; it names the count only. The
    caller supplies the localized note (the engine has no UI strings)."""
    masked, hits = mask_secrets(text)
    if hits and note_key:
        masked = f"{masked}\n\n[{note_key}: {hits}]"
    return masked, hits


def kinds_found(text: str) -> list[str]:
    """The names of the shapes present in a text (for an honest count log).

    Never returns any matched value — only the shape names."""
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    for name, pattern, _mask in PATTERNS:
        try:
            if pattern.search(text):
                found.append(name)
        except re.error:  # pragma: no cover — the table is ours
            continue
    return found
