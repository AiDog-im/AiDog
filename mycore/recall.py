"""mycore.recall — cross-session, cross-slot FTS5 recall (batch 4 item 1).

`recall(query)` is ONE read face over everything the active profile holds in
raw form:

  * every session transcript under `<state>/sessions` (root + ``archive/``),
  * the three memory slots (`MEMORY_curated.md`, `USER_curated.md`,
    `EVOLUTION_learned.md`),

ranked by FTS5 BM25 relevance BLENDED with recency, bounded by `limit`, and
NEVER returning a suppressed source.

Design rules (the hard ones):

  * SUPPRESSION IS THE FIRST CALL, not a later filter. `memforget.load_set`
    raising `LedgerUnavailable` makes the whole call answer with zero hits
    and `suppression: "unavailable"` — an unknown ledger is never read as
    "nothing is forgotten". That is the fail-closed half of the suppression
    contract, and it has its own test.
  * The index is a DERIVED cache: `<state>/recall_index.sqlite3`, resynced
    from the authoritative files whenever (mtime, size) changes, dropped and
    rebuilt when corrupt. Suppression is decided at QUERY time against the
    LIVE ledger, so a rebuild can never resurrect a forgotten source.
  * Zero network, zero new dependencies: stdlib `sqlite3` + FTS5. Without an
    FTS5 build (or with an index that stays unusable after one rebuild, or a
    query nothing can be indexed) the call degrades to an honest substring
    pass — `method: "substring", fallback: true, reason: ...` — and says so
    instead of failing.
  * Read-only over every source: transcripts and memory slots are opened for
    reading only; the only bytes written are the index under the profile
    state root.
  * Profile-scoped (AIDOG_PROFILE seam): the state root and the memory slots
    both come from the active profile. A sessions directory that does not
    belong to the active profile contributes transcript rows only — another
    profile's memory is never opened.

English comments per repo i18n policy.
"""
from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path

from mycore import memforget
from mycore.session import (SESSION_CHANNEL, _fts_expr,  # noqa: E402
                            _fts_norm, _msg_text, _recall_probes,
                            _recall_snippet, _scan_docs, _session_files,
                            _mtime, message_digest, message_identity,
                            state_root_of)

# ----------------------------------------------------------------- limits --
INDEX_NAME = "recall_index.sqlite3"
LIMIT_DEFAULT = 20
LIMIT_MAX = 100
MEMORY_CAP = 1 << 20              # bytes read per memory slot file
MAX_MATCH_ROWS = 500              # bounded over-fetch: one broad query can
#                                   never turn a read face into a dump
RECENCY_HALF_LIFE_DAYS = 14.0     # a hit loses half its recency boost / 14d
RECENCY_PENALTY = 3.0             # max BM25 units an ancient hit may lose
_FTS5_AVAILABLE = True            # monkeypatch seam: the degradation test flips it

_FTS_COLS = ("text", "display", "path", "kind", "ordinal", "ts", "role",
             "heading", "msgdig", "body")

_UNAVAILABLE_REASON = "suppression_unavailable"


def _unavailable(query: str, limit: int) -> dict:
    """The fail-closed answer: zero hits, and it SAYS why."""
    return {"query": query, "limit": limit, "method": "none",
            "fallback": True, "reason": _UNAVAILABLE_REASON, "hits": [],
            "count": 0, "truncated": False, "suppression": "unavailable",
            "note": memforget.LEDGER_UNAVAILABLE}


# ------------------------------------------------------------ index plumbing --
def _index_path(directory: str | Path) -> Path:
    """`<parent of sessions>/recall_index.sqlite3` — the profile STATE root,
    never inside the sessions directory (its scan surface is *.jsonl only)."""
    return Path(directory).parent / INDEX_NAME


def _drop_index(index_path: Path) -> None:
    """Best-effort removal of a broken index (plus journal siblings) so the
    next attempt rebuilds it from the live files. Fail-soft."""
    for cand in (index_path, Path(str(index_path) + "-journal"),
                 Path(str(index_path) + "-wal"), Path(str(index_path) + "-shm")):
        try:
            cand.unlink()
        except OSError:
            pass


def _schema(conn) -> None:
    """Create the bookkeeping table and the FTS5 table if missing.

    `msgdig` is the content digest of a transcript message's NORMALISED text
    (the digest a session source id derives from); `body` is a memory
    section's body WITH its D8 marker lines (the exact argument
    `TombstoneSet.contains_section` takes). Both let a hit be silenced
    without the query re-reading the source file."""
    conn.execute("CREATE TABLE IF NOT EXISTS recall_src ("
                 "path TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                 "mtime INTEGER NOT NULL, size INTEGER NOT NULL)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS recall_fts USING "
                 "fts5(text, display UNINDEXED, path UNINDEXED, kind UNINDEXED, "
                 "ordinal UNINDEXED, ts UNINDEXED, role UNINDEXED, "
                 "heading UNINDEXED, msgdig UNINDEXED, body UNINDEXED, "
                 "tokenize='unicode61')")
    conn.commit()


def _schema_ok(conn) -> bool:
    """Does the FTS table carry exactly the columns this build reads?

    A derived store built by an older build does not; it is dropped and
    re-synced rather than queried with a shape this code cannot read."""
    try:
        cols = tuple(row[1] for row in conn.execute("PRAGMA table_info(recall_fts)"))
    except sqlite3.Error:
        return True                     # unknown -> let the query fail loudly
    if not cols:
        return True                     # table absent -> CREATE builds it
    return cols == _FTS_COLS


def _read_text(path: Path, cap: int) -> tuple[str, bool]:
    """(first `cap` bytes as text, partial). Bounded on purpose: a memory slot
    is read in one window, never loaded whole when it grows unexpectedly."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(cap)
            partial = bool(fh.read(1))
    except OSError:
        return "", False
    return text, partial


# --------------------------------------------------------------- row builders --
def _session_rows(path: Path, spec: dict) -> tuple[list[tuple], bool]:
    """One transcript file -> per-message index rows (bounded by SCAN_CAP)."""
    docs, partial = _scan_docs(path)
    rows: list[tuple] = []
    for i, doc in enumerate(docs):
        if "role" not in doc:
            continue
        norm = message_identity(doc)
        if not norm.strip():
            continue
        ts = doc.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            ts = spec["mtime"] // 1000
        rows.append((norm, _msg_text(doc), spec["ap"], "session", i, int(ts),
                     str(doc.get("role") or ""), "", message_digest(doc), ""))
    return rows, partial


def _memory_rows(path: Path, spec: dict) -> tuple[list[tuple], bool]:
    """One memory slot file -> per-ENTRY index rows.

    The split is `memforget.split_sections` at the channel's own heading
    levels — the SAME split `filter_slot` uses — so an indexed entry owns
    exactly the text a forget would tombstone. A file with no heading at all
    is ONE entry (the whole file), mirroring `filter_text`."""
    text, partial = _read_text(path, MEMORY_CAP)
    if not text.strip():
        return [], partial
    channel = memforget.channel_for_path(path)
    _preamble, chunks = memforget.split_sections(
        text, *memforget.levels_for(channel))
    ts = spec["mtime"] // 1000
    rows: list[tuple] = []
    if chunks:
        for i, ch in enumerate(chunks):
            body = ch["body"]
            heading = str(ch.get("heading") or "")
            rows.append((_fts_norm(heading + "\n" + body),
                         heading + "\n" + body, spec["ap"], "memory", i, ts,
                         "memory", heading, "", body))
    else:
        rows.append((_fts_norm(text), text, spec["ap"], "memory", 0, ts,
                     "memory", "", "", text))
    return rows, partial


def _sync(conn, sources: dict) -> bool:
    """Incremental sync keyed by (mtime ms, size). Returns True when at least
    one source was only scanned up to its cap window (honest `partial`).

    Rows of sources that VANISHED are dropped BEFORE anything is searchable,
    so a pruned or rotated transcript can never be matched again."""
    known = {p: (m, s) for p, m, s in conn.execute(
        "SELECT path, mtime, size FROM recall_src")}
    for path in list(known):
        if path not in sources:
            conn.execute("DELETE FROM recall_fts WHERE path=?", (path,))
            conn.execute("DELETE FROM recall_src WHERE path=?", (path,))
    partial = False
    for path, spec in sources.items():
        if known.get(path) == (spec["mtime"], spec["size"]):
            continue
        conn.execute("DELETE FROM recall_fts WHERE path=?", (path,))
        conn.execute("DELETE FROM recall_src WHERE path=?", (path,))
        builder = _session_rows if spec["kind"] == "session" else _memory_rows
        rows, was_partial = builder(Path(path), spec)
        partial = partial or was_partial
        if rows:
            conn.executemany(
                "INSERT INTO recall_fts(text, display, path, kind, ordinal, "
                "ts, role, heading, msgdig, body) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows)
        conn.execute("INSERT INTO recall_src(path, kind, mtime, size) "
                     "VALUES (?,?,?,?)",
                     (path, spec["kind"], spec["mtime"], spec["size"]))
    conn.commit()
    return partial


def _attempt(index_path: Path, sources: dict, expr: str,
             budget: int) -> tuple[list[tuple], bool]:
    """One open -> schema -> sync -> match pass. Returns (rows, partial),
    rows best-first by raw BM25. Raises for the caller's rebuild ladder."""
    conn = sqlite3.connect(str(index_path), timeout=5.0)
    try:
        _schema(conn)
        if not _schema_ok(conn):
            conn.execute("DROP TABLE IF EXISTS recall_fts")
            conn.execute("DROP TABLE IF EXISTS recall_src")
            conn.commit()
            _schema(conn)
        partial = _sync(conn, sources)
        rows = conn.execute(
            "SELECT path, kind, ordinal, ts, role, heading, msgdig, body, "
            "display, bm25(recall_fts) AS score FROM recall_fts "
            "WHERE recall_fts MATCH ? ORDER BY score LIMIT ?",
            (expr, budget)).fetchall()
        return rows, partial
    finally:
        conn.close()


# ------------------------------------------------------------------- ranking --
def _rank(row: tuple, now: float) -> float:
    """Blended score (LOWER is better): raw BM25 plus an age penalty that
    decays with a 14-day half-life — relevance first, recency as the
    tie-shifter, never a recency-only reorder."""
    bm25 = float(row[9] or 0.0)
    ts = float(row[3] or 0.0)
    age_days = max(0.0, (now - ts) / 86400.0)
    boost = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    return bm25 + RECENCY_PENALTY * (1.0 - boost)


def _memory_suppressed(snapshot, path: str, body: str) -> bool:
    """Is this memory ENTRY silenced right now?

    The exact predicate `filter_slot` applies (`contains_section`): the
    content digest, the opaque source id and the D8 marker all count, so an
    entry stays suppressed however it was forgotten."""
    if not snapshot:
        return False
    return snapshot.contains_section(memforget.channel_for_path(path), body)


def _snippet(display: str, probes: list[str]) -> str:
    """A bounded excerpt centred on the earliest probe hit."""
    return _recall_snippet(str(display or ""), probes)


def _build(row: tuple, *, probes: list[str], now: float, repo, info: dict) -> dict:
    (path, kind, _ordinal, ts, role, heading, msgdig, body,
     display, bm25) = row
    hit: dict = {
        "kind": kind,
        "when": int(ts or 0),
        "score": round(_rank(row, now), 4),
        "bm25": round(float(bm25 or 0.0), 4),
        "text": _snippet(display, probes),
    }
    if kind == "session":
        meta = info.get(path)
        hit["session"] = meta[0] if meta else Path(path).stem
        hit["role"] = str(role or "")
        hit["source_id"] = memforget.source_id_from_digest(
            SESSION_CHANNEL, str(msgdig or ""), repo=repo)
    else:
        channel = memforget.channel_for_path(path)
        plain = memforget.strip_markers(str(body or ""))
        hit["slot"] = Path(path).name
        hit["heading"] = str(heading or "")
        hit["role"] = "memory"
        hit["source_id"] = memforget.source_id(channel, plain, repo=repo)
    return hit


# ------------------------------------------------------- honest degradation --
def _substring_hits(files, slots, qf, suppression,
                    snapshot, budget: int) -> tuple[list[tuple], bool, bool]:
    """The no-FTS5 ladder: a bounded substring pass over the same sources
    with the SAME suppression filter. -> (rows, partial, truncated).

    Rows carry score 0.0 — there is no BM25 here, so the shared ranker
    orders them by recency alone, which is what the fallback can honestly
    claim."""
    rows: list[tuple] = []
    partial = False
    truncated = False
    ranked = sorted(files, key=lambda pair: _mtime(pair[0]), reverse=True)
    for path, _sid in ranked:
        if len(rows) >= budget:
            truncated = True
            break
        docs, part = _scan_docs(path)
        partial = partial or part
        ap = os.path.abspath(str(path))
        for i, doc in enumerate(docs):
            if "role" not in doc:
                continue
            display = _msg_text(doc)
            if qf not in display.casefold():
                continue
            digest = message_digest(doc)
            if suppression.suppresses_digest(digest):
                continue              # a forgotten message is not a hit
            if len(rows) >= budget:
                truncated = True
                break
            ts = doc.get("ts")
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                ts = _mtime(path)
            rows.append((ap, "session", i, int(ts),
                         str(doc.get("role") or ""), "", digest, "",
                         display, 0.0))
    for slot in slots:
        if len(rows) >= budget:
            truncated = True
            break
        text, part = _read_text(slot, MEMORY_CAP)
        partial = partial or part
        if not text.strip():
            continue
        channel = memforget.channel_for_path(slot)
        _preamble, chunks = memforget.split_sections(
            text, *memforget.levels_for(channel))
        entries = chunks or [{"heading": "", "body": text,
                              "heading_line": ""}]
        ap = os.path.abspath(str(slot))
        ts = _mtime(slot)
        for i, ch in enumerate(entries):
            body = ch["body"]
            display = str(ch.get("heading") or "") + "\n" + body
            if qf not in display.casefold():
                continue
            if _memory_suppressed(snapshot, ap, body):
                continue              # a forgotten entry is not a hit
            if len(rows) >= budget:
                truncated = True
                break
            rows.append((ap, "memory", i, ts, "memory",
                         str(ch.get("heading") or ""), "", body, display, 0.0))
    return rows, partial, truncated


# ------------------------------------------------------------------ the face --
def _active_slots(repo, state_root: Path) -> list[Path]:
    """The ACTIVE profile's three memory slots — or NONE.

    Two guards. (1) The seam refuses to read another profile's memory: the
    slots are only used when they belong to the same profile state root the
    transcripts were resolved from, so a caller pointing at a foreign
    directory gets transcript rows only instead of mixing two profiles'
    memory. (2) `repo` is normalised to the repository root before it reaches
    `profile_paths`, whose `Path(repo)` defaults break on an explicit None
    (the DEFAULT profile never sets AIDOG_PROFILES_DIR)."""
    root = Path(repo) if repo is not None else memforget.REPO
    try:
        active = Path(memforget.state_dir(repo))
    except (memforget.MemforgetError, ValueError, OSError):
        return []                            # an unresolvable profile reads nothing
    if active != Path(state_root):
        return []
    from lib import profile_paths as pp
    try:
        return list(pp.memory_slots(root)) + [pp.learned_path(root)]
    except (ValueError, OSError, TypeError):
        return []


def recall(query, *, directory: str | Path | None = None,
           state: str | Path | None = None, repo=None,
           slots=None, limit: int = LIMIT_DEFAULT,
           now: float | None = None) -> dict:
    """Cross-session + memory-slot recall for `query`.

    -> `{"query", "method", "fallback", "reason"?, "hits"[], "count",
         "truncated", "limit", "suppression", "note"?, "partial"?}` where
      * `method` is `fts5` (BM25 + recency) or `substring` (labelled
        degradation, with `fallback: true` and an honest `reason`);
      * each hit carries `kind` (session|memory), `when`, `score` (blended,
        lower = better), `bm25`, a bounded `text` snippet and the opaque
        `source_id` the forget controls address;
      * `suppression` is `ok`, or `unavailable` with ZERO hits — the
        fail-closed answer when the tombstone ledger cannot be read.

    Never raises: every failure degrades to zero hits rather than leaking a
    source that might be suppressed. Read-only over every source file."""
    q = str(query or "").strip()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = LIMIT_DEFAULT
    limit = max(1, min(limit, LIMIT_MAX))
    now = float(now) if now is not None else time.time()
    base = {"query": q, "limit": limit}

    # --- 1. suppression FIRST (fail-closed, never "empty means nothing") ---
    if directory is None:
        root = Path(state) if state is not None else memforget.state_dir(repo)
        directory = root / "sessions"
    directory = Path(directory)
    state_root = Path(state) if state is not None else state_root_of(directory)
    try:
        snapshot = memforget.load_set(repo, state_root)
    except memforget.LedgerUnavailable:
        return _unavailable(q, limit)
    suppression = memforget.ChannelSuppression(SESSION_CHANNEL, snapshot)

    # --- 2. sources: transcripts of this root + the ACTIVE profile's slots --
    files = _session_files(directory)
    info: dict[str, tuple] = {}
    sources: dict[str, dict] = {}
    for path, sid in files:
        try:
            st = path.stat()
        except OSError:                     # vanished after the listing
            continue
        ap = os.path.abspath(str(path))
        sources[ap] = {"kind": "session", "mtime": int(st.st_mtime * 1000),
                       "size": st.st_size, "ap": ap}
        info[ap] = (sid, int(st.st_mtime), st.st_size)
    active_slots = _active_slots(repo, state_root) if slots is None \
        else [Path(s) for s in (slots or ())]
    for slot in active_slots:
        try:
            st = slot.stat()
        except OSError:
            continue                        # an absent slot is simply absent
        if not stat.S_ISREG(st.st_mode):
            continue
        ap = os.path.abspath(str(slot))
        sources[ap] = {"kind": "memory", "mtime": int(st.st_mtime * 1000),
                       "size": st.st_size, "ap": ap}

    qf = q.casefold()
    probes = _recall_probes(qf)
    expr = _fts_expr(qf)
    budget = min(MAX_MATCH_ROWS, max(limit * 10, 100))

    # --- 3. the index ladder: plain -> one rebuild -> honest degradation ---
    matched: list[tuple] | None = None
    reason = ""
    partial = False
    global _FTS5_AVAILABLE
    if not _FTS5_AVAILABLE:
        reason = "fts5_unavailable"
    elif expr is None:
        reason = "unindexable_query"
    else:
        index_path = _index_path(directory)
        rebuilt = False
        while True:                         # at most: plain -> after rebuild
            try:
                matched, partial = _attempt(index_path, sources, expr, budget)
                break
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "fts5" in msg:            # no such module -> honest
                    _FTS5_AVAILABLE = False
                    reason = "fts5_unavailable"
                    break
                if "locked" in msg or "busy" in msg:
                    # Another reader/writer holds the index. Withholding
                    # for THIS query is honest; destroying a healthy index
                    # on lock contention is not — never drop on busy.
                    reason = "index_busy"
                    break
                if rebuilt:
                    reason = "index_unusable"
                    break
                rebuilt = True
                _drop_index(index_path)         # corrupt -> rebuild once
            except (sqlite3.Error, OSError, ValueError, TypeError):
                if rebuilt:
                    reason = "index_unusable"
                    break
                rebuilt = True
                _drop_index(index_path)

    # --- 4. rank, suppress, bound ------------------------------------------
    if matched is None:
        rows, fb_partial, truncated = _substring_hits(
            files, active_slots, qf, suppression, snapshot, budget)
        partial = partial or fb_partial
        method, fallback = "substring", True
    else:
        rows = [r for r in matched
                if (r[1] != "session"
                    and not _memory_suppressed(snapshot, r[0], r[7]))
                or (r[1] == "session"
                    and not suppression.suppresses_digest(str(r[6] or "")))]
        truncated = len(matched) >= budget
        method, fallback = "fts5", False

    rows.sort(key=lambda r: (_rank(r, now), -int(r[3] or 0), str(r[0]),
                             int(r[2] or 0)))
    count = len(rows)
    hits = [_build(r, probes=probes, now=now, repo=repo, info=info)
            for r in rows[:limit]]
    out = {**base, "method": method, "fallback": fallback, "hits": hits,
           "count": count, "truncated": truncated or count > len(hits),
           "suppression": "ok"}
    if reason:
        out["reason"] = reason
    if partial:
        out["partial"] = True
    return out
