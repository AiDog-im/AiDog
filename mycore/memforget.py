"""mycore.memforget — durable suppression of one memory source (stage 1).

Design authority: docs/design/memory_suppression_design.md — D1 (a tombstone
ledger, never an in-place deletion), D2 (opaque deterministic source ids),
D3 (the ledger lives in the per-profile state root), D4 (the POSIX mutation
lock), D5 (admission / read / restore), D6 (the set travels with a backup),
D7 (derived stores are filtered, never edited), D8 (curated per-entry
markers) and D10 (honest limits).

WHAT THIS IS NOT: it deletes nothing. The authoritative memory files stay
append-only and reviewable; a suppression removes a source from ACTIVE memory
(prompt injection, the console memory view, the owner profile, recall and
restored backups). The original session transcript, the copies that already
left the machine, the public repository history and the backups taken BEFORE
the suppression are untouched — every report says so (D10).

Contract in one paragraph: a source is identified by an opaque, deterministic
id (profile scope + admission channel + content digest — never a path, an
excerpt or a credential); a forget records that id in a per-profile JSONL
ledger; every read path filters the ledger out of what it shows; a restore
filters through the same set, and the set itself rides along in a backup so a
restore on another machine cannot silently un-forget a source.

The ledger row's `scope` says WHICH surfaces a tombstone silences; the
`channel` is the axis that is ENFORCED — a tombstone never crosses stores,
because the channel is part of the source id.

Stdlib only. English comments (repo i18n policy).
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl                      # native Windows has no fcntl — the
except ImportError:                   # mutation lock then fails CLOSED
    fcntl = None

REPO = Path(__file__).resolve().parent.parent
from lib import profile_paths as pp  # noqa: E402  (per-profile state seam)

# ----------------------------------------------------------------- layout --
DIR_NAME = "memforget"
LEDGER_NAME = "tombstones.jsonl"
LOCK_NAME = ".lock"

# Admission channels — WHERE a source came in. The channel is part of the
# source id, so two stores that happen to hold the same text stay two
# different sources (overlapping content attribution stays separable).
CHANNEL_LEARNED = "learned"                 # EVOLUTION_learned.md entries
CHANNEL_CURATED_MEMORY = "curated:memory"   # MEMORY_curated.md entries
CHANNEL_CURATED_USER = "curated:user"       # USER_curated.md entries
CHANNEL_CURATED_OTHER = "curated:other"     # any other curated file

# Suppression scope — WHICH surfaces the tombstone silences.
SCOPE_MEMORY = "memory"                # learned memory + the derived stores
SCOPE_CURATED = "curated-entry"        # one curated entry, everywhere it shows

CHANNELS = (CHANNEL_LEARNED, CHANNEL_CURATED_MEMORY, CHANNEL_CURATED_USER,
            CHANNEL_CURATED_OTHER)

MAX_ROWS = 20000            # hard cap on the ledger (a tombstone is rare)
LOCK_WAIT = 0.25            # seconds before dropping a contended write
LOCK_RETRY = 0.005
MAX_TEXT = 200000           # chars of source text that may be hashed

# ------------------------------------------------------------- the markers --
# D8: a curated heading may carry an explicit source marker on the line right
# under it. The marker pins the id, so an EDITED entry stays suppressed (the
# content-derived id would change with the text). Whole-line markers only.
MARKER_RE = re.compile(
    r"^\s*<!--\s*aidog-src\s*:\s*([A-Za-z0-9_.:-]{1,80})\s*-->\s*$")

# ----------------------------------------------------------------- limits --
LIMITS_NOTE = (
    "The source is removed from ACTIVE memory only: prompt injection, the "
    "console memory view, the owner profile, recall and restored backups. "
    "The original session transcript, copies that already left this machine, "
    "the published repository history and backups taken before this moment "
    "are NOT deleted.")


class MemforgetError(RuntimeError):
    """A suppression could not be recorded or applied. The message is a
    reason WORD only — never the source text, a path or a credential."""


# ------------------------------------------------------------------ ids ----
def profile_scope(repo: Path | None = None) -> str:
    """The profile scope part of every source id. The default profile is
    "default"; a named profile uses its name. Never a path."""
    try:
        name = pp.profile_name()
    except pp.ProfileError:
        name = ""
    return name or "default"


def canonical(text: str) -> str:
    """The identity form of a source text.

    Whitespace, blank lines and list markers are folded away, so the same
    source keeps the same id however it was written. The fold is a FIXED
    POINT under the learned-memory writer (which prefixes the first line
    with "- " and indents the rest by two spaces), which is what lets an
    admission-time id match the id the reader derives later."""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        while True:                       # fold repeated list markers
            stripped = re.sub(r"^[-*+]\s+", "", line, count=1)
            if stripped == line:
                break
            line = stripped
        out.append(line)
    return "\n".join(out)


def digest_of(text: str) -> str:
    """The content digest of a source (no profile, no channel)."""
    data = canonical(text)[:MAX_TEXT].encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()[:32]


def source_id(channel: str, text: str, *, repo: Path | None = None) -> str:
    """The opaque, deterministic id of one source (D2).

    profile scope + admission channel + content digest. The id carries no
    path, no excerpt and no credential — it cannot be read back into the
    source, only matched against it."""
    scope = profile_scope(repo)
    raw = f"{scope}\x1f{channel}\x1f{digest_of(text)}".encode("utf-8")
    return "src_" + hashlib.sha256(raw).hexdigest()[:32]


def channel_for_path(path: Path | str) -> str:
    """The admission channel of a memory FILE (by its file name)."""
    name = Path(path).name
    if name == "EVOLUTION_learned.md":
        return CHANNEL_LEARNED
    if name == "MEMORY_curated.md":
        return CHANNEL_CURATED_MEMORY
    if name == "USER_curated.md":
        return CHANNEL_CURATED_USER
    return CHANNEL_CURATED_OTHER


# ---------------------------------------------------------------- ledger ----
def state_dir(repo: Path | None = None, state: Path | str | None = None) -> Path:
    """The per-profile state root of the ledger (D3).

    `state` is an explicit override (a test seam and the "restore
    elsewhere" path); without it the ACTIVE profile's state root is used
    (AIDOG_MC_STATE still wins — the existing contract)."""
    if state is not None:
        return Path(state)
    return pp.state_dir(Path(repo) if repo is not None else REPO)


def ledger_dir(repo: Path | None = None, state: Path | str | None = None) -> Path:
    return state_dir(repo, state) / DIR_NAME


def ledger_path(repo: Path | None = None, state: Path | str | None = None) -> Path:
    """<state>/memforget/tombstones.jsonl — one JSON object per line."""
    return ledger_dir(repo, state) / LEDGER_NAME


def _open_nonblocking(path: str, flags: int) -> int:
    # Opening a non-regular path must not bypass the bounded lock wait.
    return os.open(path, flags | os.O_NONBLOCK | os.O_NOFOLLOW, 0o666)


@contextmanager
def _mutation_lock(directory: Path):
    """Serialize ledger writers across processes (D4).

    The pattern is the one the pipeline and fleet ledgers already use: a
    regular-file check, a BOUNDED wait, and a fail-closed result when the
    lock cannot be taken (including on a host without POSIX flock). The lock
    file stays in place so every process locks the same inode."""
    if fcntl is None:
        raise MemforgetError("the tombstone ledger requires POSIX flock")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle = open(directory / LOCK_NAME, "a+b", opener=_open_nonblocking)
    except OSError as exc:
        raise MemforgetError(
            f"the tombstone ledger is not writable: {type(exc).__name__}")
    try:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise MemforgetError("the tombstone lock is not a regular file")
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                remaining = deadline - time.monotonic()
                if (remaining <= 0 or exc.errno not in
                        (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)):
                    raise MemforgetError(
                        "the tombstone ledger is locked by another writer")
                time.sleep(min(LOCK_RETRY, remaining))
        yield
    finally:
        try:
            handle.close()
        except OSError:
            pass


@dataclass(frozen=True)
class TombstoneSet:
    """An immutable snapshot of the suppression set.

    `ids` holds the opaque source ids; `pairs` holds (channel, digest)
    tuples. The pairs exist because a tombstone set TRAVELS with a backup
    (D6): the id of a source embeds the profile scope, so a set that lands
    in another profile is matched by its content digest instead — the same
    content, the same suppression."""

    ids: frozenset = frozenset()
    pairs: frozenset = frozenset()
    rows: tuple = ()

    @classmethod
    def from_rows(cls, rows) -> "TombstoneSet":
        ids: set = set()
        pairs: set = set()
        clean: list = []
        for row in rows or ():
            row = _clean_row(row)
            if row is None:
                continue
            clean.append(row)
            ids.add(row["id"])
            pairs.add((row["channel"], row["digest"]))
        return cls(ids=frozenset(ids), pairs=frozenset(pairs), rows=tuple(clean))

    def merged(self, other: "TombstoneSet") -> "TombstoneSet":
        if not other:
            return self
        return TombstoneSet(ids=self.ids | other.ids,
                            pairs=self.pairs | other.pairs)

    def __bool__(self) -> bool:
        return bool(self.ids or self.pairs)

    def contains(self, channel: str, text: str) -> bool:
        """Is this source (as text) suppressed in this set?"""
        if not self:
            return False
        return ((channel, digest_of(text)) in self.pairs
                or source_id(channel, text) in self.ids)

    def contains_section(self, channel: str, body: str) -> bool:
        """Is this stored section (a heading plus its body) suppressed?

        Both the explicit marker id (D8) and the content-derived id count,
        so an entry stays suppressed whether it was forgotten before or
        after its marker was added."""
        if not self:
            return False
        plain = strip_markers(body)
        if (channel, digest_of(plain)) in self.pairs:
            return True
        if source_id(channel, plain) in self.ids:
            return True
        return any(mid in self.ids for mid in marker_ids(body))


_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{1,64}$")


def _clean_row(row) -> dict | None:
    """One ledger row, validated. A malformed row is DROPPED, never
    repaired — the ledger stays well-formed (no half-written record)."""
    if not isinstance(row, dict):
        return None
    rid = row.get("id")
    channel = row.get("channel")
    digest = row.get("digest")
    scope = row.get("scope")
    ts = row.get("ts")
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        return None
    if not isinstance(channel, str) or channel not in CHANNELS:
        return None
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        return None
    if not isinstance(scope, str) or not scope or len(scope) > 40:
        scope = SCOPE_MEMORY
    if not isinstance(ts, str) or len(ts) > 40:
        ts = ""
    return {"id": rid, "channel": channel, "scope": scope,
            "digest": digest, "ts": ts}


def _now_iso(now: float | None = None) -> str:
    stamp = float(now if now is not None else time.time())
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(
        timespec="seconds")


_cache: dict = {}
_cache_lock = threading.Lock()


def load_rows(repo: Path | None = None,
              state: Path | str | None = None) -> list[dict]:
    """Every well-formed ledger row. A missing ledger is an EMPTY set — the
    normal state before anything is forgotten."""
    path = ledger_path(repo, state)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue                     # a damaged line is skipped, not fatal
        clean = _clean_row(row)
        if clean is not None:
            rows.append(clean)
    return rows


def load_set(repo: Path | None = None,
             state: Path | str | None = None) -> TombstoneSet:
    """The active suppression set, cached on (path, mtime, size) so a turn
    does not re-parse the ledger for every read path."""
    path = ledger_path(repo, state)
    try:
        info = path.stat()
        key = (str(path), info.st_mtime_ns, info.st_size)
    except OSError:
        return TombstoneSet()
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit
    snapshot = TombstoneSet.from_rows(load_rows(repo, state))
    with _cache_lock:
        _cache[key] = snapshot
        if len(_cache) > 8:
            _cache.clear()
    return snapshot


def invalidate_cache() -> None:
    """Drop the parsed-ledger cache (a test seam and the restore path)."""
    with _cache_lock:
        _cache.clear()


# --------------------------------------------------------------- writing ----
def forget(channel: str, text: str, *, repo: Path | None = None,
           state: Path | str | None = None, scope: str | None = None,
           now: float | None = None) -> dict:
    """Record a tombstone for one source (D1, D5).

    Nothing is deleted: the source stays in its file and the ledger records
    that it must not be shown, injected, profiled or restored again.
    Repeating the same forget is a NO-OP (the ledger keeps one row per
    source), so the operation is idempotent.

    Returns {"ok", "id", "channel", "scope", "already", "limits"}."""
    if channel not in CHANNELS:
        raise MemforgetError("unknown admission channel")
    if scope is None:
        scope = (SCOPE_MEMORY if channel == CHANNEL_LEARNED
                 else SCOPE_CURATED)
    row = {"id": source_id(channel, text, repo=repo),
           "channel": channel, "scope": scope,
           "digest": digest_of(text), "ts": _now_iso(now)}
    directory = ledger_dir(repo, state)
    path = ledger_path(repo, state)
    try:
        with _mutation_lock(directory):
            existing = {r["id"] for r in load_rows(repo, state)}
            if row["id"] in existing:
                invalidate_cache()
                return {"ok": True, "id": row["id"], "channel": channel,
                        "scope": scope, "already": True, "limits": LIMITS_NOTE}
            if len(existing) >= MAX_ROWS:
                raise MemforgetError("the tombstone ledger is full")
            line = json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n"
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
    except OSError as exc:
        raise MemforgetError(
            f"the tombstone ledger could not be written: {type(exc).__name__}")
    invalidate_cache()
    return {"ok": True, "id": row["id"], "channel": channel, "scope": scope,
            "already": False, "limits": LIMITS_NOTE}


def import_rows(rows, *, repo: Path | None = None,
                state: Path | str | None = None) -> dict:
    """Merge a traveling tombstone set into the local ledger (D6).

    Used by the restore path: a backup carries the set, so restoring it on
    another machine keeps the suppression. Rows that are already known are
    skipped — the merge is idempotent."""
    incoming = [r for r in (_clean_row(row) for row in (rows or ()))
                if r is not None]
    if not incoming:
        return {"imported": 0, "skipped": 0}
    directory = ledger_dir(repo, state)
    path = ledger_path(repo, state)
    imported = 0
    skipped = 0
    try:
        with _mutation_lock(directory):
            known = {r["id"] for r in load_rows(repo, state)}
            with open(path, "a", encoding="utf-8") as handle:
                for row in incoming:
                    if row["id"] in known:
                        skipped += 1
                        continue
                    known.add(row["id"])
                    imported += 1
                    handle.write(json.dumps(row, ensure_ascii=True,
                                            sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    except OSError as exc:
        raise MemforgetError(
            f"the tombstone ledger could not be written: {type(exc).__name__}")
    invalidate_cache()
    return {"imported": imported, "skipped": skipped}


# ---------------------------------------------------------------- reading ---
def is_suppressed(channel: str, text: str, *, repo: Path | None = None,
                  state: Path | str | None = None,
                  tombstones: TombstoneSet | None = None) -> bool:
    """Is this source suppressed right now? The admission gate (D5)."""
    snapshot = tombstones if tombstones is not None else load_set(repo, state)
    return snapshot.contains(channel, text)


# ------------------------------------------------------------- the filter ---
def marker_ids(body: str) -> list[str]:
    """The explicit source markers of a stored section (D8)."""
    out: list[str] = []
    for line in (body or "").splitlines():
        m = MARKER_RE.match(line)
        if m:
            out.append(m.group(1))
    return out


def strip_markers(body: str) -> str:
    """The section body without its marker lines — what the reader sees and
    what the content digest is computed over."""
    if not body or "aidog-src" not in body:
        return body or ""
    kept = [line for line in body.splitlines(keepends=True)
            if not MARKER_RE.match(line)]
    return "".join(kept)


_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$")


def split_sections(text: str, min_level: int = 1,
                   max_level: int = 6) -> tuple[str, list[dict]]:
    """Split a memory file into (preamble, sections).

    A section is {"heading", "level", "heading_line", "body", "raw"}; `raw`
    is the section's bytes exactly as they appear in the file, so a filter
    can drop one section and keep every other byte. A heading outside the
    level range stays part of the body (the learned-memory writer never
    emits a body line that starts with '#', so the level-2 split of the
    learned slot is exact)."""
    preamble: list[str] = []
    chunks: list[dict] = []
    current: dict | None = None
    for line in (text or "").splitlines(keepends=True):
        m = _HEADING_RE.match(line.rstrip("\n"))
        if m:
            level = len(m.group(1))
            if min_level <= level <= max_level:
                if current is not None:
                    chunks.append(current)
                current = {"heading": m.group(2).strip(), "level": level,
                           "heading_line": line, "body_lines": []}
                continue
        if current is None:
            preamble.append(line)
        else:
            current["body_lines"].append(line)
    if current is not None:
        chunks.append(current)
    for chunk in chunks:
        chunk["body"] = "".join(chunk["body_lines"])
        chunk["raw"] = chunk["heading_line"] + chunk["body"]
    return "".join(preamble), chunks


def _levels_for(channel: str) -> tuple[int, int]:
    # The learned slot is written as "## <date> (learn)" entries; a curated
    # file is human-authored and may nest headings.
    return (2, 2) if channel == CHANNEL_LEARNED else (1, 6)


def filter_text(text: str, channel: str, *, repo: Path | None = None,
                state: Path | str | None = None,
                tombstones: TombstoneSet | None = None) -> str:
    """Drop every suppressed section from a memory file's text.

    With an empty suppression set the text is returned UNCHANGED (byte for
    byte) — the normal state before anything is forgotten."""
    if not text:
        return text or ""
    snapshot = tombstones if tombstones is not None else load_set(repo, state)
    if not snapshot:
        return text
    min_level, max_level = _levels_for(channel)
    preamble, chunks = split_sections(text, min_level, max_level)
    kept = [preamble]
    for chunk in chunks:
        if snapshot.contains_section(channel, chunk["body"]):
            continue
        kept.append(chunk["raw"])
    return "".join(kept)


def filter_slot(path: Path | str, text: str, *,
                repo: Path | None = None,
                state: Path | str | None = None,
                tombstones: TombstoneSet | None = None) -> str:
    """Drop every suppressed section from one memory SLOT's text."""
    channel = channel_for_path(path)
    return filter_text(text, channel, repo=repo, state=state,
                        tombstones=tombstones)


def forget_entry(path: Path | str, title: str, *,
                 repo: Path | None = None,
                 state: Path | str | None = None,
                 now: float | None = None) -> dict:
    """Forget one stored entry of a memory file, addressed by its heading.

    The heading is matched exactly first, then case-insensitively. The
    source text that is tombstoned is the entry body WITHOUT its marker
    line — the same text the reader derives the id from."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise MemforgetError("the memory slot could not be read")
    channel = channel_for_path(target)
    min_level, max_level = _levels_for(channel)
    _, chunks = split_sections(text, min_level, max_level)
    wanted = str(title or "").strip()
    found = [c for c in chunks if c["heading"] == wanted]
    if not found:
        low = wanted.casefold()
        found = [c for c in chunks if c["heading"].casefold() == low]
    if not found:
        raise MemforgetError("no entry with that heading")
    chunk = found[0]
    body = strip_markers(chunk["body"])
    return forget(channel, body, repo=repo, state=state, now=now)


def ledger_report(repo: Path | None = None,
                  state: Path | str | None = None) -> dict:
    """An honest, content-free report of the ledger: counts and ids only —
    never the suppressed text, a path or a credential."""
    rows = load_rows(repo, state)
    return {"count": len(rows),
            "ids": [r["id"] for r in rows],
            "channels": sorted({r["channel"] for r in rows}),
            "path": str(ledger_path(repo, state)),
            "limits": LIMITS_NOTE}


# ------------------------------------------------------------------ cli -----
def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m mycore.memforget",
        description="Record and inspect memory suppressions (tombstones).")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list", help="list the recorded tombstone ids")
    p_forget = sub.add_parser("forget", help="record a tombstone")
    p_forget.add_argument("--channel", default=CHANNEL_LEARNED,
                           choices=list(CHANNELS))
    p_forget.add_argument("--text", default="",
                           help="the source text to suppress")
    p_forget.add_argument("--slot", default="",
                           choices=("", "memory", "user", "learned"),
                           help="a memory slot of the active profile")
    p_forget.add_argument("--heading", default="",
                           help="the entry heading inside --slot")
    args = parser.parse_args(argv or ["list"])
    try:
        if args.cmd in (None, "list"):
            report = ledger_report()
            print(f"tombstones={report['count']} "
                  f"channels={','.join(report['channels']) or '-'}")
            for rid in report["ids"]:
                print(rid)
            return 0
        if args.slot:
            slot = {"memory": pp.memory_slots()[0],
                    "user": pp.memory_slots()[1],
                    "learned": pp.learned_path()}[args.slot]
            if not args.heading:
                raise MemforgetError("--slot needs --heading")
            result = forget_entry(slot, args.heading)
        else:
            if not args.text.strip():
                raise MemforgetError("--text is required")
            result = forget(args.channel, args.text)
    except MemforgetError as exc:
        sys.stderr.write(f"memforget refused: {exc}\n")
        return 1
    print(f"tombstone={result['id']} channel={result['channel']} "
          f"already={result['already']}")
    print(result["limits"])
    return 0


def main(argv: list[str] | None = None) -> int:
    return _cli(list(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
