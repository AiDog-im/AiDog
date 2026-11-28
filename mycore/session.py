"""mycore.session — JSONL turn persistence (P4-1b).

File format: one JSON object per line, UTF-8.
  line 1 (file creation): {"meta": {"created": <epoch>, "model": str, "format": 1}}
  following lines:        {"role": ..., "content": ..., "ts": <epoch>, ...}
                           (tool-call turns keep tool_calls / tool_call_id)

Design rules:
  - append-only; each append flushes (a crash loses at most the current line)
  - corrupt lines are skipped and counted (`Session.corrupt`) — honest
    reporting instead of a hard crash on a half-written tail line
  - messages round-trip as plain dicts: whatever the loop puts in a message
    (role/content/tool_calls/tool_call_id/name) comes back out
English comments per repo i18n policy.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from mycore import memforget   # the suppression ledger (stage 6, D30-D35)

MSG_KEYS = ("role", "content", "name", "tool_calls", "tool_call_id", "ts")

# ------------------------------------- batch 4 item 4: the page anchor ----
# The console history face serves a PAGE of the transcript (newest first,
# absolute ids) instead of the whole file. An id is an ordinal over load()'s
# message rows, so a backward window alone cannot know the TOTAL row count a
# page's ids are measured from — that count is kept as an in-process ANCHOR:
#
#     anchor[<abs path>] = (fingerprint, n)
#     fingerprint = (st_dev, st_ino, st_size, st_mtime_ns)
#
# Rules (fail-closed by construction, no new files anywhere):
#   * the anchor is honoured ONLY while the file's fingerprint matches
#     exactly. An append, a rewind/fork/resume (os.replace -> new inode), a
#     prune or a hand edit moves it, and the next page falls back to the full
#     parse that WAS the whole read path before this car (correct, slower);
#   * n is only ever recorded by a full parse of that very fingerprint, or
#     by a writer that saw a valid anchor BEFORE its own append (it then
#     knows the exact new count) — otherwise the anchor is dropped;
#   * a page scan that reaches the start of the file recounts every row and
#     must land on n; any disagreement drops the anchor and the page is
#     re-served from a fresh full parse — never from a wrong number;
#   * writers and readers share the single-writer-per-transcript convention
#     the console's `done` frame already depends on (its `base_len + i` ids
#     are computed the same way); a SECOND process appending to the same
#     file changes the fingerprint and forces a full parse.
ANCHOR_MAX = 16
_anchor: dict[str, tuple[tuple[int, int, int, int], int]] = {}


def _fingerprint(path: str | Path) -> tuple[int, int, int, int] | None:
    """(dev, ino, size, mtime_ns) of `path`, or None when it is not a file
    we can read (missing, unreadable)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _anchor_get(path: str | Path, fp: tuple | None = None) -> int | None:
    """The row count `path` had at its CURRENT fingerprint; None = unknown
    (every caller then falls back to a full parse). A stale entry is dropped
    on sight, so the map never outlives the file it describes."""
    key = str(path)
    hit = _anchor.get(key)
    if hit is None:
        return None
    if fp is None:
        fp = _fingerprint(path)
    if fp is None or hit[0] != fp:
        _anchor.pop(key, None)
        return None
    return hit[1]


def _anchor_set(path: str | Path, fp: tuple, n: int) -> None:
    if len(_anchor) >= ANCHOR_MAX:
        _anchor.clear()                     # tiny working set, drop rather than LRU
    _anchor[str(path)] = (fp, int(n))


def _anchor_drop(path: str | Path) -> None:
    _anchor.pop(str(path), None)


def _anchor_after_append(path: str | Path, pre_fp: tuple | None,
                         pre_n: int | None, added: int) -> None:
    """Re-anchor after WE appended `added` rows. The new count is only
    trustworthy when the anchor was valid before the write (then n + added
    is exact); any other history means: drop it and let the next read pay
    the full parse."""
    fp = _fingerprint(path)
    if fp is None or pre_fp is None or pre_n is None or added < 0:
        _anchor_drop(path)
        return
    _anchor_set(path, fp, pre_n + added)



class Session:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.corrupt = 0          # lines skipped at last load (honest stat)

    # ------------------------------------------------------------ write ----
    @classmethod
    def create(cls, path: str | Path, meta: dict | None = None) -> "Session":
        """Create/overwrite a session file with its meta line."""
        s = cls(path)
        s.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"meta": {"created": time.time(), "format": 1,
                        **({"model": meta["model"]} if meta and meta.get("model") else {})}}
        s.path.write_text(json.dumps(doc, ensure_ascii=False) + "\n", encoding="utf-8")
        fp = _fingerprint(s.path)          # a fresh file holds 0 message rows
        if fp is not None:
            _anchor_set(s.path, fp, 0)
        return s

    def append(self, message: dict) -> None:
        """Append one message (role/content/...) as a JSONL line; flushed.

        A crash can leave a partial tail line without a newline — heal it first
        (terminate the orphan line as its own corrupt entry) so this append
        starts on a fresh line instead of gluing onto garbage.
"""
        if not isinstance(message, dict) or not message.get("role"):
            raise ValueError("message must be a dict with a 'role'")
        line = {k: message[k] for k in MSG_KEYS if k in message}
        line.setdefault("ts", time.time())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pre_fp = _fingerprint(self.path)
        pre_n = _anchor_get(self.path, pre_fp)
        self._ensure_line_start()
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            fh.flush()
        _anchor_after_append(self.path, pre_fp, pre_n, 1)

    def _ensure_line_start(self) -> None:
        """Terminate a partial (newline-less) tail line left by a crash."""
        try:
            if self.path.stat().st_size == 0:
                return
            with open(self.path, "rb") as fh:
                fh.seek(-1, 2)
                if fh.read(1) != b"\n":
                    with open(self.path, "ab") as fa:
                        fa.write(b"\n")
        except OSError:
            return

    def append_many(self, messages: list[dict]) -> None:
        """Append a run-turn delta (assistant + tool messages) in one flush."""
        if not messages:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pre_fp = _fingerprint(self.path)
        pre_n = _anchor_get(self.path, pre_fp)
        self._ensure_line_start()
        added = 0
        with open(self.path, "a", encoding="utf-8") as fh:
            for m in messages:
                if not isinstance(m, dict) or not m.get("role"):
                    continue
                line = {k: m[k] for k in MSG_KEYS if k in m}
                line.setdefault("ts", time.time())
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
                added += 1
            fh.flush()
        _anchor_after_append(self.path, pre_fp, pre_n, added)

    # ------------------------------------------------------------- read ----
    def load(self) -> list[dict]:
        """All messages (meta line dropped). Corrupt lines skipped + counted.

        A load is also an ANCHOR write (batch 4 item 4): when the file's
        fingerprint is stable across the read, its exact row count is what a
        later bounded page read starts from."""
        self.corrupt = 0
        if not self.path.is_file():
            return []
        fp0 = _fingerprint(self.path)
        out: list[dict] = []
        for raw in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                doc = json.loads(raw)
            except ValueError:
                self.corrupt += 1
                continue
            if not isinstance(doc, dict):
                self.corrupt += 1
                continue
            if "meta" in doc and "role" not in doc:
                continue                              # header line
            if not doc.get("role"):
                self.corrupt += 1
                continue
            out.append({k: doc[k] for k in MSG_KEYS if k in doc})
        if fp0 is not None and _fingerprint(self.path) == fp0:
            _anchor_set(self.path, fp0, len(out))
        return out

    def meta(self) -> dict:
        """Header dict ({} when absent/invalid)."""
        if not self.path.is_file():
            return {}
        try:
            first = self.path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            doc = json.loads(first)
        except (OSError, ValueError, IndexError):
            return {}
        return doc.get("meta", {}) if isinstance(doc, dict) else {}

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


# ------------------------------------- batch 4 item 4: bounded page read --
READ_PAGE_MAX = 500            # one HTTP page, hard bound (abuse/budget cap)
READ_PAGE_CHUNK = 1 << 16      # bytes read per backward step


def _iter_rows_backward(fh, size: int):
    """Yield the raw lines of an open binary file from the LAST line to the
    first, in READ_PAGE_CHUNK steps — a page never holds the transcript in
    memory and never reads bytes it has already passed. The file is read up
    to `size` only, so a file that grows mid-scan cannot leak into the page.
    Blank lines are yielded as b"" and skipped by the caller (they consume
    no id, exactly like Session.load)."""
    pos = size
    pending = b""                # tail read so far that starts mid-line
    while pos > 0:
        take = min(READ_PAGE_CHUNK, pos)
        pos -= take
        fh.seek(pos)
        buf = fh.read(take) + pending
        if pos > 0:
            nl = buf.find(b"\n")
            if nl < 0:
                pending = buf    # still no line start: read further back
                continue
            # buf[:nl+1] completes the line that started before `pos`; what
            # follows is line-aligned (it starts right after a newline).
            rest, first = buf[nl + 1:], buf[:nl + 1]
            pending = b""
            for raw in reversed(rest.split(b"\n")):
                yield raw
            yield first[:-1] if first.endswith(b"\n") else first
            continue
        pending = b""
        for raw in reversed(buf.split(b"\n")):
            yield raw


def _page_from_full(path: str | Path, limit: int, before: int | None,
                    keep) -> dict:
    """The honest fallback: parse the whole file (today's read path), then
    page it in memory. Also primes the anchor for the next call."""
    rows = Session(path).load()
    picked: list[tuple[int, dict]] = []
    for i, row in enumerate(rows):
        if before is not None and i >= before:
            break
        if keep is None or keep(row):
            picked.append((i, row))
    has_more = len(picked) > limit
    if has_more:
        picked = picked[-limit:]
    return {"messages": picked, "count": len(picked), "has_more": has_more}


def _page_from_anchor(path: str | Path, fp: tuple, n: int, limit: int,
                      before: int | None, keep) -> dict | None:
    """The bounded path: walk the file BACKWARD from its end, parsing only
    the rows a page needs, and derive each row's absolute id from the
    anchored total (`id = n - 1 - rows after it`).

    Returns None whenever the anchor and the file disagree (more rows than
    `n`, a count that does not land on `n` at byte 0, a fingerprint that
    moved during the read): the caller then drops the anchor and serves the
    page from a full parse — never from a number it cannot prove."""
    out: list[tuple[int, dict]] = []
    after = 0
    has_more = False
    try:
        with open(path, "rb") as fh:
            if _fingerprint(path) != fp:
                return None
            for raw in _iter_rows_backward(fh, fp[2]):
                if not raw.strip():
                    continue
                try:
                    doc = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue          # corrupt row: consumes no id (as load)
                if not isinstance(doc, dict):
                    continue
                if "meta" in doc and "role" not in doc:
                    continue                              # header line
                if not doc.get("role"):
                    continue
                if after >= n:
                    return None       # more rows than the anchor accounted for
                mid = n - 1 - after
                after += 1
                if before is not None and mid >= before:
                    continue          # outside this page (still counted above)
                row = {k: doc[k] for k in MSG_KEYS if k in doc}
                if keep is not None and not keep(row):
                    continue
                out.append((mid, row))
                if len(out) > limit:
                    has_more = True   # one row past the page proves it exists
                    break
            else:
                if after != n:
                    return None       # the whole file disagrees with n
            if _fingerprint(path) != fp:
                return None           # rewritten while we read it
    except OSError:
        return None
    if has_more:
        out = out[:limit]
    return {"messages": list(reversed(out)), "count": len(out),
            "has_more": has_more}


def read_page(path: str | Path, limit: int, before: int | None = None,
              keep=None) -> dict:
    """The NEWEST `limit` message rows of `path` that pass `keep`, in
    ASCENDING absolute-id order — the bounded read behind the console's
    paginated history.

    `before` (exclusive) pages backwards: only rows with id < before are
    returned, so the client walks the transcript oldest-ward without ever
    re-reading what it already has. `keep(row)` decides membership (roles,
    suppression) and runs on THIS read only — a row it rejects still consumes
    its id, which is why ids stay the file's own indices (rewind/fork
    handles never move).

    Returns {"messages": [(id, row)], "count", "has_more"}: `has_more` is
    exact — True only when a row the page rejected really exists further
    back, False when the scan reached byte 0.

    Raises ValueError for an unusable limit/before (the HTTP layer answers
    400). The ledger is NOT consulted here: suppression is the CALLER's
    `keep`, and a caller that cannot read the ledger must not call this.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) \
            or not 1 <= limit <= READ_PAGE_MAX:
        raise ValueError(f"limit must be an integer in 1..{READ_PAGE_MAX}")
    if before is not None and (not isinstance(before, int)
                               or isinstance(before, bool) or before < 0):
        raise ValueError("before must be a non-negative integer")
    if before == 0:
        return {"messages": [], "count": 0, "has_more": False}
    path = Path(path)
    fp = _fingerprint(path)
    n = _anchor_get(path, fp) if fp is not None else None
    if n is not None:
        page = _page_from_anchor(path, fp, n, limit, before, keep)
        if page is not None:
            return page
        _anchor_drop(path)
    return _page_from_full(path, limit, before, keep)


# ------------------------------------------- Wave 6 delta2: X12/X13 faces ---
# The class above is deliberately minimal (append-only write + load). The
# module faces below add the three maintenance/read surfaces the upstream
# parity audit flagged as missing: age pruning (X12, opt-in from settings),
# list + real FTS5 full-text search (X13, honest substring fallback) and
# single-session export (X13). Design rules hold
# throughout:
#   - fail-soft or explicitly typed errors (ValueError / FileNotFoundError),
#     never an OSError escaping to an HTTP 500;
#   - an id can never escape `directory` (grammar + resolve parent check);
#   - summary/search scan at most SCAN_CAP chars per file and say so
#     (`partial: true`) instead of pretending the file was fully read.

SESSION_SUFFIX = ".jsonl"
LIST_LIMIT_DEFAULT = 200
LIST_LIMIT_MAX = 500
SCAN_CAP = 1 << 20                  # chars read per file for summary/search
SUMMARY_MAX = 120                   # summary excerpt length
# id grammar: one or two path segments (the second is the archive/ level),
# each starting alnum — "..", absolute paths and separators cannot match.
_SID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
    r"(/[A-Za-z0-9][A-Za-z0-9._-]{0,95})?")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")

# ------------------------------------- FTS5 full-text session search -------
# Owner decision (2026-10-27): `?q=` runs REAL full-text search over a
# SQLite FTS5 index; the old substring scan survives only as an honest,
# labelled fallback. Design rules:
#   - INDEX LOCATION: `<state root>/session_search.sqlite3` — the directory
#     CONTAINING sessions/, NEVER inside it (X12 prune touches only .jsonl
#     and _session_files must not see the index), and outside every probe
#     byte-identity surface (secrets.env / web/settings.json / profiles/** /
#     data/images / the checkpoints index).
#   - LAZY SYNC at query time, keyed by (mtime ms, size): new/changed files
#     are (re)indexed; rows of vanished files are dropped BEFORE matching,
#     so pruned or resume-rotated content can never be matched again.
#   - one row per message; text normalized by _fts_norm (casefold + a space
#     around every character of a no-space script that unicode61 cannot
#     split into tokens); sessions rank by their best FTS5 BM25 score,
#     ties newest first.
#   - HONEST DEGRADATION: no FTS5 build, an index still unusable after one
#     rebuild, or a query with nothing indexable (punctuation-only) runs the
#     original substring scan and SAYS SO (`method:"substring",
#     fallback:true, reason:...`); success reports `method:"fts5",
#     engine:"fts5", fallback:false`.
INDEX_NAME = "session_search.sqlite3"
_FTS5_AVAILABLE = True          # monkeypatch seam: degradation test flips it
# No-space scripts: han ideographs (incl. ext A/B+), kana, hangul — runs of
# these carry no spaces, so unicode61 would tokenize a whole run as ONE
# token and a mid-run query could never match without the spacing below.
_NSS_CLASS = ("\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
              "\u3040-\u30ff"
              "\uac00-\ud7af\u1100-\u11ff"
              "\U00020000-\U0003134f")
_NSS_RE = re.compile("[" + _NSS_CLASS + "]+")
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _session_files(directory: str | Path) -> list[tuple[Path, str]]:
    """All session files as (path, id) — top level plus archive/, newest
    callers sort themselves. A missing directory is an empty list."""
    root = Path(directory)
    out: list[tuple[Path, str]] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return out
    for p in entries:
        if p.is_file() and p.suffix == SESSION_SUFFIX:
            out.append((p, p.stem))
    arch = root / "archive"
    try:
        entries = list(arch.iterdir())
    except OSError:
        entries = []
    for p in entries:
        if p.is_file() and p.suffix == SESSION_SUFFIX:
            out.append((p, f"archive/{p.stem}"))
    return out


def _msg_text(msg: dict) -> str:
    """One message as plain text: a str content verbatim; a multimodal block
    list as its text parts plus an honest '[image]' marker per image part
    (the marker mirrors what the console persists — no image byte here)."""
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block.get("type") == "image_url":
                parts.append("[image]")
        return " ".join(parts)
    return ""


def _scan_docs(path: Path) -> tuple[list[dict], bool]:
    """Parse a session file up to SCAN_CAP chars: (docs, partial). Lines that
    do not parse (a cut tail inside the cap window) are skipped — this is a
    scan for summary/search, not Session.load's integrity report."""
    docs: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(SCAN_CAP)
            partial = bool(fh.read(1))
    except OSError:
        return [], False
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if isinstance(doc, dict) and ("role" in doc or "meta" in doc):
            docs.append(doc)
    return docs, partial


def _summary_of(docs: list[dict],
                suppression: "memforget.ChannelSuppression | None" = None) -> str:
    """First user (else first assistant) text, whitespace-collapsed, control
    chars stripped, capped at SUMMARY_MAX. "" = genuinely nothing readable.

    A silenced message is skipped: the session list never shows the text of
    a forgotten message (D31)."""
    chosen = ""
    for want in ("user", "assistant"):
        for doc in docs:
            if doc.get("role") != want:
                continue
            text = " ".join(_msg_text(doc).split())
            if not text:
                continue
            if suppression is not None and not suppression.empty \
                    and suppression.suppresses_digest(message_digest(doc)):
                continue              # a forgotten message is not a summary
            chosen = text
            break
        if chosen:
            break
    chosen = _CTRL_RE.sub(" ", chosen)
    if len(chosen) > SUMMARY_MAX:
        chosen = chosen[:SUMMARY_MAX].rstrip() + "…"
    return chosen


def _fts_norm(text: str) -> str:
    """Index/query normalization: casefold, then isolate every character of a
    no-space script (han/kana/hangul) with spaces so ONE character becomes
    ONE FTS5 token — unicode61 would otherwise tokenize a whole run as a
    single token and a mid-run query could never match."""
    return _NSS_RE.sub(lambda m: " " + " ".join(m.group(0)) + " ",
                       text.casefold())


# ------------------------------------- stage 6: the transcript suppression --
# D30-D35. A transcript MESSAGE is a source of its own: the store that
# reaches the model most directly (through the recall window after a fold)
# is the last one a tombstone can silence.
#
# The identity of a message is the NORMALISED message text — the same
# normalisation the search index uses — so the source id agrees on BOTH
# sides of the index (D30). The transcript FILE is never rewritten: the
# message stays on disk, append-only and reviewable, and every READ path
# drops it (D31).
SESSION_CHANNEL = memforget.CHANNEL_SESSION

# A search hit carries the source id of its message (D32). The list is
# bounded so one broad query cannot turn the read face into a dump; when
# the bound bites the face says so instead of pretending it is complete.
MAX_HIT_SOURCES = 200

# Files the source RESOLUTION may open. The resolution is an owner action
# (one click), not a hot path; the bound keeps it honest about the window.
RESOLVE_FILES_MAX = 500


def message_identity(doc: dict) -> str:
    """The NORMALISED text that identifies one transcript message (D30).

    A multimodal message contributes its text parts and one honest
    '[image]' marker per image part — exactly what the index stores, so the
    id agrees on both sides of the index."""
    return _fts_norm(_msg_text(doc))


_digest_cache: dict = {}
_DIGEST_CACHE_MAX = 8192


def normalised_digest(norm: str) -> str:
    """The content digest of an ALREADY normalised text.

    The digest is a pure function of the text, so it is computed ONCE per
    distinct message and never needs invalidating — the ledger can change
    under it, the digest cannot (D34)."""
    key = norm or ""
    hit = _digest_cache.get(key)
    if hit is None:
        hit = memforget.digest_of(key)
        if len(_digest_cache) >= _DIGEST_CACHE_MAX:
            _digest_cache.clear()
        _digest_cache[key] = hit
    return hit


def message_digest(doc: dict) -> str:
    """The content digest of one transcript message (its normalised text)."""
    return normalised_digest(message_identity(doc))


def text_digest(text: str) -> str:
    """The content digest of a raw message TEXT (normalised first).

    The same identity form as `message_digest`, for the callers that hold
    the text instead of the parsed message — the profiling corpus reads
    the transcript's own JSON, so it has the text."""
    return normalised_digest(_fts_norm(text or ""))


def message_source_id(doc: dict, *, repo=None) -> str:
    """The opaque, deterministic id of one transcript message (D30).

    The usual id: profile scope + admission channel `session` + the content
    digest of the normalised message text. It carries no path, no excerpt
    and no credential — it can only be matched, never read back."""
    return memforget.source_id_from_digest(SESSION_CHANNEL, message_digest(doc),
                                           repo=repo)


def state_root_of(directory) -> Path:
    """The state root that owns a sessions directory (its parent).

    The product's layout puts the sessions directory INSIDE the per-profile
    state root (the search index lives beside it), so the parent's ledger
    is the ledger that governs these transcripts."""
    return Path(directory).parent


def message_suppression(directory=None, *, repo=None,
                        state=None) -> memforget.ChannelSuppression:
    """The session-channel suppressor of the ACTIVE ledger (D34).

    The comparison is skipped entirely while the ledger holds no
    session-channel row — the normal state, where nothing is forgotten — so
    a read path pays nothing when there is nothing to suppress. The
    ledger is the one of the state root that owns the transcripts (the
    sessions directory's parent) or, without a directory, the one of the
    active profile."""
    try:
        if state is None and directory is not None:
            state = state_root_of(directory)
        snapshot = memforget.load_set(repo, state)
    except memforget.LedgerUnavailable:
        return memforget.ChannelSuppression(SESSION_CHANNEL, available=False)
    return memforget.ChannelSuppression(SESSION_CHANNEL, snapshot)


def visible_session_messages(messages, suppression=None, *,
                             directory=None) -> list[dict]:
    """The messages of a loaded session that are still VISIBLE (D31).

    ONE implementation for EVERY read path: the chat service, the one-shot
    runner and the console all call THIS function. The three copies that
    used to live in those modules are gone — one rule, one place, so the
    three surfaces cannot drift apart.

    `suppression` may be passed in (a caller that already resolved the
    ledger for this profile pays for the ledger read once) or left out, in
    which case the suppressor of `directory` — or of the ACTIVE profile —
    is resolved here.

    A forgotten message is dropped from every read: the model's own
    context, the turn count, the /rewind head, the console's history. The
    transcript FILE is never rewritten — the line stays on disk,
    append-only and reviewable (D10/D31).

    The check is skipped entirely while the ledger holds no session-channel
    row (D34): with nothing forgotten the list comes back unchanged, so
    the behaviour is byte-identical to the pre-suppression build."""
    if suppression is None:
        suppression = message_suppression(directory)
    if suppression.empty:
        return messages
    return [m for m in messages
            if not suppression.suppresses_digest(message_digest(m))]


def message_is_visible(message: dict, suppression) -> bool:
    """Is this ONE message still visible (D31)?

    The per-message half of `visible_session_messages`, for a face that
    walks the rows itself and must keep every OTHER row's identity (the
    console's history keeps each message's absolute id, so a row that is
    filtered out never shifts the ids of the rows around it)."""
    if suppression.empty:
        return True
    return not suppression.suppresses_digest(message_digest(message))


def _fts_expr(qf: str) -> str | None:
    """FTS5 MATCH expression for a casefolded query; None when the query has
    nothing FTS can index (punctuation/underscore only) — the caller then
    runs the substring scan, which still matches those characters.

    Terms are ANDed. A word-ish term matches as a PREFIX phrase (a typed
    fragment still finds the word). Each word-run segment made only of
    no-space-script characters matches as an exact adjacent-token phrase —
    substring-style matching for scripts unicode61 cannot split."""
    terms: list[str] = []
    for seg in _WORD_RE.findall(qf):          # raw word runs of the query
        phrase: list[str] = []
        for tok in _WORD_RE.findall(_fts_norm(seg)):
            if len(tok) == 1 and _NSS_RE.fullmatch(tok):
                phrase.append(tok)            # one no-space-script char
                continue
            if phrase:
                terms.append('"' + " ".join(phrase) + '"')
                phrase = []
            if tok.strip("_"):
                terms.append('"%s"*' % tok)
        if phrase:
            terms.append('"' + " ".join(phrase) + '"')
    return " ".join(terms) or None


# --------------------------- car 5-D7 item 4: fold-time recall ------------
# "Recall before re-send" (design §4.2 item 5): when loop.run_turn folds old
# turns away, ONLY the snippets that match the live task are re-injected as a
# `[recalled ...]` system marker — the folded turns are not re-sent whole.
# It reuses the session search's own FTS5 engine, query grammar and BM25
# ranking (`_fts_norm` / `_fts_expr` vocabulary), but over an IN-MEMORY window
# of the just-folded messages: no new on-disk index, no new process, no new
# dependency. Honest degradation is the same ladder as `?q=`: no FTS5 build
# here -> a casefolded substring pass reported as method "substring"; a task
# line with nothing indexable -> method "none" and zero hits (never invented).
RECALL_LIMIT = 3                # snippets re-injected per fold
RECALL_SNIPPET_CHARS = 240      # per snippet
RECALL_SNIPPET_LEAD = 80        # context kept before the first hit
RECALL_MAX_PROBES = 24          # task-line probes sent to the MATCH expression
_FTS5_RECALL_AVAILABLE = True   # monkeypatch seam: the degradation test flips it

_RECALL_STOP = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
    "has", "his", "her", "was", "one", "our", "out", "get", "how", "now",
    "new", "old", "see", "way", "who", "did", "its", "let", "say", "she",
    "too", "use", "that", "with", "have", "this", "will", "your", "from",
    "they", "know", "want", "been", "much", "some", "time", "very",
    "when", "come", "here", "just", "like", "make", "many", "over",
    "such", "take", "than", "them", "well", "were", "what", "into"})


def _recall_probes(query: str) -> list[str]:
    """Indexable probes of the LIVE task line: latin/number word runs (>=3
    chars, stopwords dropped, prefix-matchable) plus no-space-script runs cut
    into 2-grams — unicode61 cannot tokenize a han/kana/hangul run into words,
    so an adjacent-token PHRASE is the only form that recalls a Chinese
    fragment (the same reasoning as `_fts_expr`). De-duplicated, ordered,
    bounded by RECALL_MAX_PROBES (long runs are thinned on a stride instead of
    truncated, so a long task line keeps its whole span)."""
    latin: list[str] = []
    cjk: list[str] = []
    seen: set[str] = set()
    for run in _WORD_RE.findall(str(query).casefold()):
        for sub in re.findall("[" + _NSS_CLASS + "]+|[^" + _NSS_CLASS + "]+", run):
            if _NSS_RE.search(sub):
                grams = [sub[i:i + 2] for i in range(len(sub) - 1)] if len(sub) > 1 else [sub]
                for g in grams:
                    if g not in seen:
                        seen.add(g)
                        cjk.append(g)
            elif len(sub) >= 3 and sub not in _RECALL_STOP and sub not in seen:
                seen.add(sub)
                latin.append(sub)
    budget = max(0, RECALL_MAX_PROBES - len(latin))
    if len(cjk) > budget:
        step = max(1, -(-len(cjk) // max(1, budget)))   # ceil division
        cjk = cjk[::step][:budget]
    return latin + cjk


def _recall_expr(probes: list[str]) -> str | None:
    """FTS5 MATCH expression over `probes`, ORed (a task line matches on ANY
    of its terms; ANDing would return nothing realistic). Latin terms are
    PREFIX phrases (`"tok"*`), no-space-script probes are adjacent-token
    PHRASES over the normalized text. None when there is nothing to match."""
    parts: list[str] = []
    for p in probes:
        if _NSS_RE.search(p):
            norm = " ".join(_fts_norm(p).split())
            if norm:
                parts.append('"%s"' % norm)
        else:
            parts.append('"%s"*' % p)
    return " OR ".join(dict.fromkeys(parts)) or None


def _recall_match(texts: list[str], expr: str, limit: int
                  ) -> dict[int, float] | None:
    """BM25 over an in-memory FTS5 table: {candidate index: score} (lower =
    better), or None when FTS5 cannot run in this python — the caller then
    uses the substring pass. Never raises; the table lives only for this call."""
    global _FTS5_RECALL_AVAILABLE
    if not _FTS5_RECALL_AVAILABLE:
        return None
    conn = None
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE recall_fts USING "
                     "fts5(text, tokenize='unicode61')")
        conn.executemany("INSERT INTO recall_fts(text) VALUES (?)",
                         [(_fts_norm(t),) for t in texts])
        out: dict[int, float] = {}
        for rowid, score in conn.execute(
                "SELECT rowid, bm25(recall_fts) FROM recall_fts "
                "WHERE recall_fts MATCH ? ORDER BY bm25(recall_fts) LIMIT ?",
                (expr, max(1, int(limit)))):
            out[int(rowid) - 1] = float(score)
        return out
    except sqlite3.OperationalError as e:
        if "fts5" in str(e).lower():        # no such module -> permanent here
            _FTS5_RECALL_AVAILABLE = False
        return None
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _recall_snippet(text: str, probes: list[str]) -> str:
    """One bounded excerpt of `text`, centred on the EARLIEST probe hit when it
    sits past the opening (a 12k-char tool result recalls its relevant line,
    not its first 240 chars). Whitespace-collapsed, control chars stripped."""
    flat = " ".join(_CTRL_RE.sub(" ", text).split())
    if not flat:
        return ""
    cf = flat.casefold()
    best = -1
    for p in probes:
        pos = cf.find(p)
        if pos >= 0 and (best < 0 or pos < best):
            best = pos
    start = best - RECALL_SNIPPET_LEAD if best > RECALL_SNIPPET_LEAD else 0
    cut = flat[start:start + RECALL_SNIPPET_CHARS]
    more = len(flat) - start > len(cut)
    return (("…" if start else "") + cut + ("…" if more else ""))


def recall_snippets(candidates: list[dict], query: str, *,
                    limit: int = RECALL_LIMIT) -> dict:
    """Car 5-D7 item 4 — FTS5 recall over the messages a fold just dropped.

    -> `{method, hits:[{role, text, score}], probes}` where method is
      * `fts5`      — BM25 ranking from the in-memory FTS5 pass,
      * `substring` — honest fallback (no FTS5 in this python),
      * `none`      — empty task line, no candidates or no indexable probe.
    Never raises: every failure degrades to `none`/`substring` with zero hits
    rather than breaking the turn. Snippets are bounded (RECALL_* constants)
    and role-tagged so the model can see WHERE a fact came from.

    A FORGOTTEN message is never recalled (D31): the fold-time recall is a
    READ path, so a silenced message leaves the window before it can be
    re-injected into the model's context. The check is skipped entirely
    while no session-channel tombstone exists (D34)."""
    suppression = message_suppression()
    try:
        probes = _recall_probes(query)
        entries: list[tuple[dict, str]] = []
        for m in candidates:
            if not isinstance(m, dict):
                continue
            if not suppression.empty and \
                    suppression.suppresses_digest(message_digest(m)):
                continue          # a forgotten message is never recalled
            txt = _msg_text(m)
            if txt.strip():
                entries.append((m, txt))
    except Exception:  # noqa: BLE001 — recall is an optimisation, never a fault
        return {"method": "none", "hits": [], "probes": 0}
    if not probes or not entries:
        return {"method": "none", "hits": [], "probes": len(probes)}
    texts = [t for _m, t in entries]
    expr = _recall_expr(probes)
    matched: dict[int, float] = {}
    method = "substring"
    if expr is not None:
        got = _recall_match(texts, expr, max(1, limit * 4))
        if got is not None:
            matched = got
            method = "fts5"
    scored: list[tuple[int, float, int]] = []
    for i, t in enumerate(texts):
        cf = t.casefold()
        sub = sum(1 for p in probes if p in cf)
        bm = matched.get(i)
        if sub == 0 and bm is None:
            continue
        # rank: most probe evidence first, BM25 breaks ties (lower wins),
        # then source order — deterministic for identical input
        scored.append((-(sub + (2 if bm is not None else 0)),
                       bm if bm is not None else 0.0, i))
    scored.sort()
    hits = []
    for _neg, bm, i in scored[:max(0, int(limit))]:
        snip = _recall_snippet(texts[i], probes)
        if snip:
            hits.append({"role": entries[i][0].get("role", "?"),
                         "text": snip, "score": round(bm, 3)})
    if not hits:
        return {"method": "none", "hits": [], "probes": len(probes)}
    return {"method": method, "hits": hits, "probes": len(probes)}


def _fts_index_path(directory: str | Path) -> Path:
    """`<parent of sessions>/session_search.sqlite3` - never inside the
    sessions directory itself."""
    return Path(directory).parent / INDEX_NAME


def _fts_drop_index(directory: str | Path) -> None:
    """Best-effort removal of a broken index (plus journal/wal siblings) so
    the next attempt rebuilds it from the live files. Fail-soft."""
    base = _fts_index_path(directory)
    for cand in (base, Path(str(base) + "-journal"),
                 Path(str(base) + "-wal"), Path(str(base) + "-shm")):
        try:
            cand.unlink()
        except OSError:
            pass


def _fts_schema(conn) -> None:
    """Create the two tables if missing (a corrupt file never gets this far —
    sqlite raises before).

    `msgdig` is the content digest of the row's normalised text — the
    digest a source id is derived from (D30). It lets a search hit name
    its source (and be silenced) without the index keeping anything else
    about the message."""
    conn.execute("CREATE TABLE IF NOT EXISTS session_index ("
                 "path TEXT PRIMARY KEY, root TEXT NOT NULL, "
                 "mtime INTEGER NOT NULL, size INTEGER NOT NULL)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS session_fts USING "
                 "fts5(text, root UNINDEXED, path UNINDEXED, "
                 "msgdig UNINDEXED, tokenize='unicode61')")
    conn.commit()


def _fts_ensure_msgdig(conn) -> bool:
    """True when the index carries the per-message digest column.

    An index built by an older build does not. The search index is a
    DERIVED store — a cache of the transcripts, never a source of truth —
    so the old tables are dropped and the next sync re-indexes every file.
    A hit that cannot name its source would be a hit the owner could not
    address (D32)."""
    try:
        cols = {row[1] for row in
                conn.execute("PRAGMA table_info(session_fts)")}
    except sqlite3.Error:
        return True                     # unknown -> let the query fail loudly
    if "msgdig" in cols:
        return True
    try:
        conn.execute("DROP TABLE IF EXISTS session_fts")
        conn.execute("DROP TABLE IF EXISTS session_index")
        conn.commit()
    except sqlite3.Error:
        return True
    return False


def _fts_sync(conn, root: str, stat: dict[str, tuple[int, int]]) -> None:
    """Incremental sync for ONE sessions root, keyed by (mtime ms, size):
    rows for files that vanished are dropped BEFORE anything is searchable
    (prune / resume rotation can never leak), changed or new files are
    re-scanned over the same SCAN_CAP window the substring scan reads."""
    known = {p: (m, s) for p, m, s in conn.execute(
        "SELECT path, mtime, size FROM session_index WHERE root=?", (root,))}
    for path in known:
        if path not in stat:
            conn.execute("DELETE FROM session_fts WHERE path=?", (path,))
            conn.execute("DELETE FROM session_index WHERE path=?", (path,))
    for path, (mtime, size) in stat.items():
        if known.get(path) == (mtime, size):
            continue
        conn.execute("DELETE FROM session_fts WHERE path=?", (path,))
        conn.execute("DELETE FROM session_index WHERE path=?", (path,))
        docs, _partial = _scan_docs(Path(path))
        rows = []
        for doc in docs:
            if "role" not in doc:
                continue
            norm = message_identity(doc)
            if norm.strip():
                rows.append((norm, root, path, normalised_digest(norm)))
        conn.executemany(
            "INSERT INTO session_fts(text, root, path, msgdig) "
            "VALUES (?,?,?,?)", rows)
        conn.execute(
            "INSERT INTO session_index(path, root, mtime, size) "
            "VALUES (?,?,?,?)", (path, root, mtime, size))
    conn.commit()


def _fts_attempt(directory: str | Path, root: str,
                 stat: dict[str, tuple[int, int]], expr: str
                 ) -> list[tuple[str, int, float, list[str]]]:
    """One open → schema → sync → match pass over the index. Returns
    [(absolute path, hits, bm25 score, [message digests])] best-first
    (BM25 ascending; a session's rows collapse to its FIRST = best row,
    extra rows add hits). The digests are the per-message identities a hit
    is addressed by (D30/D32).
    Raises sqlite3.Error/OSError for the caller's rebuild/degrade ladder."""
    conn = sqlite3.connect(str(_fts_index_path(directory)), timeout=5.0)
    try:
        _fts_schema(conn)
        if not _fts_ensure_msgdig(conn):
            _fts_schema(conn)           # recreate with the digest column
        _fts_sync(conn, root, stat)
        order: list[str] = []
        hits: dict[str, int] = {}
        scores: dict[str, float] = {}
        digests: dict[str, list[str]] = {}
        for path, score, msgdig in conn.execute(
                "SELECT path, bm25(session_fts) AS score, msgdig "
                "FROM session_fts WHERE session_fts MATCH ? AND root=? "
                "ORDER BY score", (expr, root)):
            if path in hits:
                hits[path] += 1
                digests[path].append(msgdig)
            else:
                hits[path] = 1
                scores[path] = score
                digests[path] = [msgdig]
                order.append(path)
        return [(p, hits[p], scores[p], digests[p]) for p in order]
    finally:
        conn.close()


def _fts_lookup(directory: str | Path, files: list[tuple[Path, str]],
                qf: str) -> tuple[list[tuple[Path, str, int, int, int,
                                            float, list[str]]] | None, str]:
    """FTS5 search for the casefolded `qf` over `files` [(path, id)]:
    -> ([ (path, id, when, size, hits, score, digests) ... ] best-first, "")
    on success, or (None, reason) when list_sessions must degrade to the
    substring scan. reason ∈ fts5_unavailable | unindexable_query |
    index_unusable. Fail-soft by design: ANY failure degrades, never raises."""
    global _FTS5_AVAILABLE
    if not _FTS5_AVAILABLE:
        return None, "fts5_unavailable"
    expr = _fts_expr(qf)
    if expr is None:
        return None, "unindexable_query"
    root = os.path.abspath(str(directory))
    stat: dict[str, tuple[int, int]] = {}
    info: dict[str, tuple[str, int, int]] = {}
    for path, sid in files:
        try:
            st = path.stat()
        except OSError:                     # vanished after the listing
            continue
        ap = os.path.abspath(str(path))
        stat[ap] = (int(st.st_mtime * 1000), st.st_size)
        info[ap] = (sid, int(st.st_mtime), st.st_size)
    rebuilt = False
    while True:                             # at most: plain → after rebuild
        try:
            matched = _fts_attempt(directory, root, stat, expr)
            break
        except sqlite3.OperationalError as e:
            if "fts5" in str(e).lower():   # no such module: fts5 → honest
                _FTS5_AVAILABLE = False    # permanent degradation this run
                return None, "fts5_unavailable"
            if rebuilt:
                return None, "index_unusable"
            rebuilt = True
            _fts_drop_index(directory)      # corrupt/locked → rebuild once
        except (sqlite3.Error, OSError):
            if rebuilt:
                return None, "index_unusable"
            rebuilt = True
            _fts_drop_index(directory)
    rows: list[tuple[Path, str, int, int, int, float, list[str]]] = []
    for ap, hits, score, digests in matched:
        if ap not in info or not os.path.isfile(ap):
            continue        # deleted between sync and now — never surface
        sid, when, size = info[ap]
        rows.append((Path(ap), sid, when, size, hits, score, digests))
    rows.sort(key=lambda r: (r[5], -r[2]))  # BM25 best first, ties newest
    return rows, ""


def _hit_sources(digests: list[str], repo=None) -> list[dict]:
    """The opaque source id of every visible hit (D30/D32).

    One id per hit, nothing else: no path, no excerpt, no credential. The
    list is bounded — a broad query can match thousands of messages — and
    the face says so when the bound bites."""
    out = [{"source_id": memforget.source_id_from_digest(SESSION_CHANNEL, d,
                                                        repo=repo)}
           for d in digests[:MAX_HIT_SOURCES]]
    return out


def list_sessions(directory: str | Path, *, q: str | None = None,
                  limit: int = LIST_LIMIT_DEFAULT) -> dict:
    """X13 read face: session rows.

    Without `q`: newest-first rows {id, when, size, summary[, partial]},
    `count` = all sessions and NO `search` key (contract unchanged).

    With `q` (casefolded by the caller's contract): REAL FTS5 full-text
    search — rows in BM25 order, `hits` = matching messages, and an honest
    search block `{q, method:"fts5", engine:"fts5", fallback:false,
    case_insensitive:true}`; any degradation runs the original substring
    scan and says so instead (`method:"substring", fallback:true,
    reason:...`). `count` = matched sessions (rows returned may be fewer;
    `truncated` says so). Fail-soft: an unreadable file becomes a missing
    row, never a raised error.

    A FORGOTTEN message is skipped everywhere in this face (D31): it is not
    a hit, it is not counted, it never becomes a session summary, and a
    session whose every hit is silenced does not appear at all. Every
    visible hit carries the opaque source id of its message
    (`hit_sources`), so the owner can address it (D32)."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = LIST_LIMIT_DEFAULT
    limit = max(1, min(limit, LIST_LIMIT_MAX))
    qf = str(q).casefold() if q else None
    files = _session_files(directory)
    files.sort(key=lambda pair: _mtime(pair[0]), reverse=True)
    suppression = message_suppression(directory)
    if not suppression.available:
        return {"sessions": [], "count": 0, "suppression": "unavailable",
                "note": memforget.LEDGER_UNAVAILABLE}

    if qf is None:
        rows: list[dict] = []
        for path, sid in files:
            if len(rows) >= limit:
                break
            docs, partial = _scan_docs(path)
            row: dict = {"id": sid, "when": _mtime(path), "size": _size(path),
                         "summary": _summary_of(docs, suppression)}
            if partial:
                row["partial"] = True
            rows.append(row)
        return {"sessions": rows, "count": len(files),
                "truncated": len(files) > len(rows), "limit": limit}

    matched, reason = _fts_lookup(directory, files, qf)
    rows = []
    if matched is not None:
        search: dict = {"q": str(q), "method": "fts5", "engine": "fts5",
                        "fallback": False, "case_insensitive": True}
        count = 0
        for path, sid, when, size, _hits, _score, digests in matched:
            visible = [d for d in digests
                       if not suppression.suppresses_digest(d)]
            if not visible:
                continue          # every hit of this session is silenced
            count += 1
            if len(rows) >= limit:
                continue
            docs, partial = _scan_docs(path)
            row = {"id": sid, "when": when, "size": size,
                   "summary": _summary_of(docs, suppression)}
            if partial:
                row["partial"] = True
            row["hits"] = len(visible)
            row["hit_sources"] = _hit_sources(visible)
            if len(visible) > MAX_HIT_SOURCES:
                row["hit_sources_truncated"] = True
            rows.append(row)
    else:
        # honest degradation: the original substring scan, labelled as such
        search = {"q": str(q), "method": "substring", "fallback": True,
                  "case_insensitive": True, "reason": reason}
        count = 0
        for path, sid in files:
            docs, partial = _scan_docs(path)
            visible = []
            for doc in docs:
                if qf not in _msg_text(doc).casefold():
                    continue
                digest = message_digest(doc)
                if suppression.suppresses_digest(digest):
                    continue      # a forgotten message is not a hit
                visible.append(digest)
            if not visible:
                continue
            count += 1
            if len(rows) >= limit:
                continue
            row = {"id": sid, "when": _mtime(path), "size": _size(path),
                   "summary": _summary_of(docs, suppression)}
            if partial:
                row["partial"] = True
            row["hits"] = len(visible)
            row["hit_sources"] = _hit_sources(visible)
            if len(visible) > MAX_HIT_SOURCES:
                row["hit_sources_truncated"] = True
            rows.append(row)
    return {"sessions": rows, "count": count,
            "truncated": count > len(rows), "limit": limit,
            "search": search}


def _mtime(path: Path) -> int:
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _resolve_session_path(directory: str | Path, sid: str) -> Path:
    """Grammar-checked id -> existing session file. Shared by
    export_session and the console resume face (X20) so one rule governs
    both: the id grammar plus a resolved-parent check make a traversal
    impossible even if the grammar is ever loosened. Raises FileNotFoundError
    naming the id (honest 404 in the HTTP layer)."""
    root = Path(directory)
    target = root / f"{sid}{SESSION_SUFFIX}"
    try:
        resolved = target.resolve()
        base = root.resolve()
        ok_parent = resolved.parent == base or resolved.parent == base / "archive"
    except OSError:
        raise FileNotFoundError(f"no session {sid}") from None
    if not ok_parent or not resolved.is_file():
        raise FileNotFoundError(f"no session {sid}")
    return resolved


def session_file(directory: str | Path, sid: str) -> Path:
    """X20 resume resolver: one session id (root or archive/ level) -> its
    file, with the SAME grammar and parent double-check export uses — one
    accept/reject rule, so a resume can never leave the sessions directory.
    Raises ValueError (bad grammar, -> 400) or FileNotFoundError (unknown id,
    -> 404)."""
    sid = str(sid or "").strip()
    if not _SID_RE.fullmatch(sid):
        raise ValueError("session id must be [A-Za-z0-9._-] segments with "
                         "at most one archive/ level")
    return _resolve_session_path(directory, sid)


# ------------------------------------------- car C1 (A-2): rewind/fork ----
def read_head(path: str | Path, keep: int) -> tuple[dict, list[dict]]:
    """(meta, first `keep` messages) of a session file — the read half of
    the car C1 rewind/fork primitives. Raises ValueError when `keep` is out
    of range (caller maps that to an honest 400/404); corrupt lines are
    skipped exactly like load() (they are garbage, never re-emitted)."""
    s = Session(path)
    msgs = s.load()
    if not isinstance(keep, int) or isinstance(keep, bool) \
            or not 0 <= keep <= len(msgs):
        raise ValueError(f"keep must be 0..{len(msgs)}")
    return s.meta(), msgs[:keep]


def write_session_file(path: str | Path, meta: dict,
                       messages: list[dict]) -> None:
    """Atomic full rewrite of a session file (tmp + os.replace, same dir):
    the meta line (when non-empty) then one JSON line per message in the
    MSG_KEYS shape append() writes — a round-trip through load(). The tmp
    name carries entropy so a pre-planted symlink at a predictable name can
    never be followed (wrap-up audit W2-02 rule). Raises OSError to the
    caller (an honest failure — the caller leaves the live file alone)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if meta:
        lines.append(json.dumps({"meta": meta}, ensure_ascii=False))
    for m in messages:
        lines.append(json.dumps(
            {k: m[k] for k in MSG_KEYS if k in m}, ensure_ascii=False))
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{os.urandom(4).hex()}")
    try:
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""),
                       encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    # The rewrite is exact by construction: meta + one line per message, so
    # the new fingerprint carries `len(messages)` rows (batch 4 item 4).
    fp = _fingerprint(path)
    if fp is not None:
        _anchor_set(path, fp, len(messages))


def export_session(directory: str | Path, sid: str, fmt: str = "json") -> tuple[str, str]:
    """X13 export: one session as (text, content_type).

    Raises ValueError for an id/format the contract refuses and
    FileNotFoundError for an unknown id — the HTTP layer maps them to
    400/404. The id grammar plus a resolved-parent check make a traversal
    impossible even if the grammar is ever loosened. Export carries message
    content the console already reads (chat history contract) — never a
    credential slot value; corrupt lines are skipped and counted honestly."""
    sid = str(sid or "").strip()
    if not _SID_RE.fullmatch(sid):
        raise ValueError("session id must be [A-Za-z0-9._-] segments with "
                         "at most one archive/ level")
    fmt = str(fmt or "").strip().lower()
    if fmt not in ("json", "md"):
        raise ValueError("format must be one of: json | md")
    resolved = _resolve_session_path(directory, sid)
    sess = Session(resolved)
    meta = sess.meta()
    msgs = sess.load()
    # A FORGOTTEN message leaves the export (D31) — the file on disk stays
    # byte-identical, only the READ drops it.
    suppression = message_suppression(directory)
    if not suppression.available:
        raise memforget.LedgerUnavailable(memforget.LEDGER_UNAVAILABLE)
    if not suppression.empty:
        msgs = [m for m in msgs
                if not suppression.suppresses_digest(message_digest(m))]
    if fmt == "json":
        doc = {"id": sid, "meta": meta, "count": len(msgs),
               "corrupt": sess.corrupt, "messages": msgs}
        return json.dumps(doc, ensure_ascii=False, indent=2) + "\n", \
            "application/json; charset=utf-8"
    lines = [f"# session {sid}", ""]
    if meta:
        bits = []
        if isinstance(meta.get("created"), (int, float)):
            bits.append("created " + time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(meta["created"])))
        if meta.get("model"):
            bits.append(f"model {meta['model']}")
        if bits:
            lines.append(" · ".join(bits))
            lines.append("")
    if sess.corrupt:
        lines.append(f"> {sess.corrupt} corrupt line(s) skipped at export")
        lines.append("")
    for m in msgs:
        role = str(m.get("role") or "?")
        if role == "tool":
            name = m.get("name") or m.get("tool_call_id") or ""
            label = f"tool {name}" if name else "tool"
        else:
            label = role
        lines.append(f"**{label}:** {_msg_text(m)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", "text/markdown; charset=utf-8"


def find_message_by_source_id(directory: str | Path, source_id: str, *,
                              repo=None,
                              limit_files: int = RESOLVE_FILES_MAX) -> dict | None:
    """Locate the transcript message an opaque source id names (D32).

    The SERVER resolves the source: the client sends the opaque id and
    never invents one. The scan uses the SAME bounded window the search
    face reads (SCAN_CAP chars per file) and a bounded number of files; a
    message that is gone, or that sits outside that window, is refused
    honestly — never guessed at.

    Returns {"text": <the NORMALISED text>, "role": <the role of the first
    match>, "count": <how many messages carry this id>, "user_items": <how
    many of them are the owner's own messages>} or None when the id names
    nothing inside the window. The text is returned to the CALLER (the
    server) so it can record the tombstone; it never reaches the client."""
    target = str(source_id or "").strip()
    if not target:
        return None
    root = Path(directory)
    files = _session_files(root)
    files.sort(key=lambda pair: _mtime(pair[0]), reverse=True)
    scope = memforget.profile_scope(repo)
    found_text = ""
    role = ""
    count = 0
    user_items = 0
    for path, _sid in files[:max(1, int(limit_files))]:
        docs, _partial = _scan_docs(path)
        for doc in docs:
            if "role" not in doc:
                continue
            norm = message_identity(doc)
            if not norm.strip():
                continue
            sid = memforget.source_id_from_digest(
                SESSION_CHANNEL, normalised_digest(norm), scope=scope)
            if sid != target:
                continue
            if not found_text:
                found_text = norm
                role = str(doc.get("role") or "")
            count += 1
            if str(doc.get("role") or "") == "user":
                user_items += 1
    if not found_text:
        return None
    return {"text": found_text, "role": role, "count": count,
            "user_items": user_items}


def prune_old(directory: str | Path, max_age_days: int, *,
              keep: tuple[str, ...] | list[str] = (),
              now: float | None = None) -> dict:
    """X12: delete *.jsonl session files older than `max_age_days`.

    Retention boundary is INCLUSIVE: a file whose age is exactly the
    threshold (mtime == now - days) is KEPT — only strictly older files go.
    `keep` holds ids (stems) that are never touched — the console protects
    its live "web" session with it. archive/ files are pruned by the same
    rule (their stems include the archive stamp, so a stem match still
    applies). Non-.jsonl files (.model overrides, stray notes) are never
    touched. Fail-soft: per-file OSError counts as an error, never raises.
    Returns {removed, kept, freed, errors} — names are never needed by the
    caller and stay out of the result on purpose."""
    if isinstance(max_age_days, bool) or not isinstance(max_age_days, int) \
            or max_age_days <= 0:
        raise ValueError("max_age_days must be a positive integer")
    cutoff = (time.time() if now is None else float(now)) - max_age_days * 86400
    keep_set = {str(k) for k in keep}
    removed = kept = errors = 0
    freed = 0
    for path, sid in _session_files(directory):
        if sid in keep_set or Path(sid).stem in keep_set:
            kept += 1
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            errors += 1
            continue
        if mtime >= cutoff:               # at-or-younger than the threshold
            kept += 1
            continue
        try:
            freed += path.stat().st_size
            path.unlink()
            removed += 1
        except OSError:
            errors += 1
    return {"removed": removed, "kept": kept, "freed": freed,
            "errors": errors}
