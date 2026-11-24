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
  * PROVENANT. Every entry carries its admission channel and the opaque,
    deterministic source id of the text it owns (mycore.memforget.source_id)
    — the two facts the owner-facing forget control needs, and nothing
    more: no path, no credential, no new body text.

Stdlib only; English comments (repo i18n policy).
"""
from __future__ import annotations

from pathlib import Path

from mycore import memforget

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


def _norm(text: str) -> str:
    """The reader's form of a body: no trailing spaces, no outer blank
    lines. The same shape the view has always shown."""
    return "\n".join(line.rstrip() for line in (text or "").splitlines()).strip()


def _entries(text: str, channel: str) -> list[dict]:
    """Split a memory file into browsable entries.

    The split is the SAME one the suppression filter uses
    (mycore.memforget.split_sections with the channel's heading levels), so
    the source id computed here is the id a forget would record — the view
    and the filter can never disagree about which text an entry owns.

    A heading starts a new entry; the lines under it are its body. A file
    with no heading is ONE entry (the whole file) — the shape stays honest
    rather than inventing structure. Text that precedes the first heading
    stays with the first entry, so nothing the file holds is dropped from
    the view."""
    preamble, chunks = memforget.split_sections(
        text, *memforget.levels_for(channel))
    out: list[dict] = []
    for index, chunk in enumerate(chunks):
        # The marker line (D8) is never shown to the reader, and the id is
        # derived from the body WITHOUT it — the text a forget tombstones.
        body = memforget.strip_markers(chunk["body"])
        out.append({"title": chunk["heading"],
                    "display": _norm((preamble + body) if index == 0 else body),
                    "body": body,
                    "source_id": memforget.source_id(channel, body)})
    if not chunks and (text or "").strip():
        # a file with no heading at all is ONE entry: the whole file
        out.append({"title": "", "display": _norm(text), "body": text,
                    "source_id": memforget.source_id(channel, text)})
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
    try:
        snapshot = memforget.load_set(root)
    except memforget.LedgerUnavailable:
        return {"slots": [], "entries_total": 0, "query": q, "read_only": True,
                "local_only": True, "editable": False,
                "suppression": "unavailable", "note": memforget.LEDGER_UNAVAILABLE}
    total = 0
    for label, path in (("memory", mem), ("user", usr)):
        text, error = _read(path)
        if error:
            slots.append({"slot": label, "available": False,
                           "note": f"unreadable ({error})", "entries": []})
            continue
        # Memory suppression (D5): a tombstoned entry is not shown here.
        # With nothing suppressed the text is unchanged, byte for byte.
        text = memforget.filter_slot(path, text, tombstones=snapshot)
        channel = memforget.channel_for_path(path)
        entries = _entries(text, channel)
        total += len(entries)
        if q:
            entries = [e for e in entries
                       if q in e["title"].lower() or q in e["display"].lower()]
        slots.append({
            "slot": label,
            "available": True,
            "file": path.name,
            "chars": len(text),
            "entries": [{
                "title": e["title"][:120],
                "excerpt": e["display"][:MAX_EXCERPT],
                "truncated": len(e["display"]) > MAX_EXCERPT,
                "chars": len(e["display"]),
                # The provenance of the entry: the admission channel (the
                # store it lives in) and the opaque, deterministic id of the
                # text it owns. No path, no credential, no new body text.
                "channel": channel,
                "source_id": e["source_id"],
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
