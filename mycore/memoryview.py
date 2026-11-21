"""mycore.memoryview — a read-only view of the curated memory files.

Need-B B-11 (the UX advice, adopted): the owner should be able to SEE what
the product remembers — the curated memory was a gauge and nothing else.
This module turns the two curated Markdown slots into browsable ENTRIES so
the console can list, search and show the original text.

Design rules (the same ones the rest of the product follows):

  * READ-ONLY. Nothing here writes, moves or prunes a memory file. Editing
    and archiving are a separate decision with its own gate.
  * LOCAL-ONLY. The files are read from the active body root; nothing is
    sent anywhere and no credential is involved.
  * HONEST SHAPE. An absent file is an empty list with a note, never a
    guess; a file that cannot be read is reported as unavailable.
  * BOUNDED. Entries, characters and the excerpt length are all capped, so
    one enormous memory file cannot become a giant response.

Stdlib only; English comments (repo i18n policy).
"""
from __future__ import annotations

from pathlib import Path

MAX_ENTRIES = 200
MAX_CHARS = 20000
MAX_EXCERPT = 600
MAX_QUERY = 120

SLOTS = ("MEMORY_curated.md", "USER_curated.md")


def _read(path: Path) -> tuple[str, str]:
    """(text, error). `error` is "" on success."""
    try:
        return path.read_text(encoding="utf-8"), ""
    except FileNotFoundError:
        return "", ""
    except OSError as e:
        return "", type(e).__name__


def _entries(text: str) -> list[dict]:
    """Split a Markdown memory file into browsable entries.

    A heading starts a new entry; the lines under it are its body. A file
    with no heading is ONE entry (the whole file) — the shape stays honest
    rather than inventing structure."""
    out: list[dict] = []
    title = ""
    body: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("#"):
            if title or body:
                out.append({"title": title.strip(), "body": "\n".join(body).strip()})
            title = line.lstrip("#").strip()
            body = []
        else:
            body.append(line)
    if title or body:
        out.append({"title": title.strip(), "body": "\n".join(body).strip()})
    return [e for e in out if e["title"] or e["body"]]


def collect(repo: Path | None = None, query: str = "",
            body: Path | None = None) -> dict:
    """The read model: every curated entry, optionally filtered by a local
    substring match. `query` is matched case-insensitively against the
    title and the body. `body` overrides the body root (a test seam); the
    production path always uses the active profile."""
    from lib import profile_paths as pp
    root = Path(repo) if repo is not None else Path(__file__).resolve(
        ).parent.parent
    if body is not None:
        mem = Path(body) / "MEMORY_curated.md"
        usr = Path(body) / "USER_curated.md"
    else:
        mem, usr = pp.memory_slots(root)
    q = str(query or "").strip()[:MAX_QUERY].lower()
    slots: list[dict] = []
    total = 0
    for label, path in (("memory", mem), ("user", usr)):
        text, error = _read(path)
        if error:
            slots.append({"slot": label, "available": False,
                           "note": f"unreadable ({error})", "entries": []})
            continue
        entries = _entries(text)
        total += len(entries)
        if q:
            entries = [e for e in entries
                       if q in e["title"].lower() or q in e["body"].lower()]
        slots.append({
            "slot": label,
            "available": True,
            "file": path.name,
            "chars": len(text),
            "entries": [{
                "title": e["title"][:120],
                "excerpt": e["body"][:MAX_EXCERPT],
                "truncated": len(e["body"]) > MAX_EXCERPT,
                "chars": len(e["body"]),
            } for e in entries[:MAX_ENTRIES]],
            "entries_total": len(entries),
            "truncated": len(entries) > MAX_ENTRIES,
        })
    return {
        "slots": slots,
        "entries_total": total,
        "query": q,
        "read_only": True,
        "local_only": True,
        "editable": False,
        "note": "the curated memory files are read-only here; editing and "
                "archiving are a separate, human-approved action",
    }
