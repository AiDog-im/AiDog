"""mycore.memorylog — the per-entry revision timeline of the memory slots.

The gap (batch 4-3): a memory slot entry that was edited, overwritten or
restored left NO trace of what it had held before — the owner could not
see that an entry changed, when it changed, or into what. This module is
the side-band LOG of those revisions (the Knowledge-layer Timeline of the
competitive baseline): one record per recorded version of one entry.

Design rules — the same philosophy as mycore.checkpoint, plus the
suppression invariant that outranks every one of them:

  * SIDE-BAND, APPEND-ONLY. The timeline never writes a memory or session
    file (D10's spirit): writers append to <state>/memorylog/timeline.jsonl
    and READERS never create it. The memory files stay byte-exact.
  * PRIVACY BY CONSTRUCTION. A record is {ts, slot, title, op, id, digest}:
    WHEN, WHICH entry (its heading), WHICH operation, and the OPAQUE ids
    of the version — never the entry text. The heading is the same string
    the memory view already shows for a VISIBLE entry; nothing else is
    stored. Full text lives in the memory files and in the pre-write
    checkpoints (mycore.checkpoint), which a reader consults instead — a
    timeline revision therefore cannot itself restore text, and does not
    pretend to.
  * BOUNDED, HONESTLY DROPPED. MAX_PER_ENTRY records per entry and
    MAX_TOTAL overall; overflow drops the OLDEST records and counts them
    in a persisted meta line, so every read can say how much history was
    dropped. The cap is enforced by an atomic rewrite only when it is
    actually exceeded; otherwise the log is a plain append.
  * SUPREMACY — the hard rule for this new READ surface:
      1. the suppression ledger must LOAD. memforget.LedgerUnavailable
         propagates out of every read face (fail-closed): a timeline the
         product cannot verify is WITHHELD, never answered as "empty".
      2. a group is visible only while its entry is present in the
         SUPPRESSION-FILTERED current slot file — a forgotten entry's
         heading disappears with it, and its whole history goes with it.
      3. no record whose id or content digest is tombstoned may appear,
         even inside a group whose title still exists.
  * FAIL-SOFT WRITES. A timeline failure returns None/0 and NEVER fails
    or changes the memory write that triggered it (checkpoint's rule).
  * PROFILE-SCOPED. State lives under pp.state_dir (AIDOG_MC_STATE wins),
    so profiles never read each other's timelines; the recorded ids embed
    the profile scope, so a foreign record never matches a local tombstone.

Coverage is honest: the recorded write paths are the lint-gated /learn
append (mycore.evolution.learn_memory) and the console backup restore
(web.server.backup_apply_memory). Deploy-time sync/restore scripts and
direct file edits made outside the product (run_bash) are NOT recorded.

Stdlib only; English-only source (repo i18n policy).
"""
from __future__ import annotations

import errno
import json
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl                      # native Windows has no fcntl — the
except ImportError:                   # timeline then serializes in-process
    fcntl = None

REPO = Path(__file__).resolve().parent.parent
from lib import profile_paths as pp  # noqa: E402  (profile state/body seams)
from mycore import memforget  # noqa: E402  (the suppression ledger it must obey)

# ----------------------------------------------------------------- layout --
DIR_NAME = "memorylog"       # under the profile state dir
FILE_NAME = "timeline.jsonl" # one JSON object per line (+ one meta line)
LOCK_NAME = ".lock"

SLOT_LABELS = ("memory", "user", "learned")
OPS = ("learn", "restore")   # the recorded write surfaces (see module doc)

# ----------------------------------------------------------------- limits --
MAX_TOTAL = 2000             # records kept on disk (write-side retention)
MAX_PER_ENTRY = 20           # records kept per (slot, title)
MAX_TITLE = 400              # chars of heading kept in a record
LOCK_WAIT = 0.25             # seconds before giving up on a contended log
LOCK_RETRY = 0.005

READ_PER_ENTRY = 10          # records a read face returns per entry
READ_MAX_TOTAL = 100         # records a read face returns in total

LIMITS_NOTE = (
    "The timeline records WHEN an entry changed and the opaque ids of its "
    "versions — never the entry text. A forgotten entry's history is "
    "withheld together with the entry itself; the text lives only in the "
    "memory files and their checkpoints.")

_ID_RE = re.compile(r"^src_[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{32}$")

_THREAD_LOCK = threading.Lock()   # in-process writer serialization


class TimelineError(memforget.MemforgetError):
    """A timeline record could not be built or stored. Reason WORD only —
    never the entry text, a path or a credential."""


# ------------------------------------------------------------- path seams --
def state_dir(repo: Path | None = None,
              state: Path | str | None = None) -> Path:
    """The per-profile state root (the same seam as the tombstone ledger):
    an explicit `state` wins, else pp.state_dir resolves the active
    profile (AIDOG_MC_STATE still wins — the existing contract)."""
    if state is not None:
        return Path(state)
    return pp.state_dir(Path(repo) if repo is not None else REPO)


def timeline_path(repo: Path | None = None,
                  state: Path | str | None = None) -> Path:
    """<state>/memorylog/timeline.jsonl — never inside a body/memory dir."""
    return state_dir(repo, state) / DIR_NAME / FILE_NAME


def slot_file(slot: str, repo: Path | None = None) -> Path:
    """The ACTIVE profile's own file for one timeline slot label."""
    root = Path(repo) if repo is not None else REPO
    mem, usr = pp.memory_slots(root)
    if slot == "memory":
        return mem
    if slot == "user":
        return usr
    if slot == "learned":
        return pp.learned_path(root)
    raise TimelineError("unknown memory slot")


def channel_of(slot: str) -> str:
    """The admission channel a slot's records are ids-of (same map the
    forget control uses — the timeline can never disagree with it)."""
    if slot == "memory":
        return memforget.CHANNEL_CURATED_MEMORY
    if slot == "user":
        return memforget.CHANNEL_CURATED_USER
    if slot == "learned":
        return memforget.CHANNEL_LEARNED
    raise TimelineError("unknown memory slot")


# ------------------------------------------------------------- the writer --
def _build_record(slot: str, title: str, op: str, body: str, *,
                  repo: Path | None = None,
                  now: float | None = None) -> dict:
    """One record. `body` is used ONLY to derive the opaque id and the
    content digest — the text itself is never written anywhere."""
    channel = channel_of(slot)
    return {"ts": float(now if now is not None else time.time()),
            "slot": slot, "title": str(title)[:MAX_TITLE], "op": op,
            "id": memforget.source_id(channel, body, repo=repo),
            "digest": memforget.digest_of(body)}


def _clean_record(row) -> dict | None:
    """One validated record. A malformed row is DROPPED, never repaired —
    the log stays well-formed (the same rule as the tombstone ledger)."""
    ts = row.get("ts")
    slot = row.get("slot")
    title = row.get("title")
    op = row.get("op")
    rid = row.get("id")
    digest = row.get("digest")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    if slot not in SLOT_LABELS or op not in OPS:
        return None
    if not isinstance(title, str) or len(title) > MAX_TITLE:
        return None
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        return None
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        return None
    return {"ts": float(ts), "slot": slot, "title": title, "op": op,
            "id": rid, "digest": digest}


def _read(path: Path) -> tuple[list[dict], int]:
    """(records in append order, persisted dropped counter).

    Absent file = ([], 0) and NOTHING is created — a read face must never
    materialise state. A corrupt line is skipped: the log answers with
    what it can prove. An unreadable file reads as empty, which withholds
    history (the safe direction) instead of guessing."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0
    records: list[dict] = []
    dropped = 0
    first = True
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            first = False
            continue
        if not isinstance(obj, dict):
            first = False
            continue
        if first and obj.get("kind") == "meta":
            count = obj.get("dropped")
            if isinstance(count, int) and not isinstance(count, bool) \
                    and count >= 0:
                dropped = count
            first = False
            continue
        first = False
        rec = _clean_record(obj)
        if rec is not None:
            records.append(rec)
    return records, dropped


def _append_line(path: Path, rec: dict) -> bool:
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=True, sort_keys=True)
                         + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except OSError:
        return False


def _prune(records: list[dict]) -> tuple[list[dict], int]:
    """(kept records, count dropped) enforcing MAX_PER_ENTRY per entry and
    MAX_TOTAL overall — always the OLDEST records that go. Input is append
    order, so "keep the last N" is "keep the newest N"."""
    counts: dict = {}
    kept: list[dict] = []
    for rec in reversed(records):
        key = (rec["slot"], rec["title"])
        seen = counts.get(key, 0)
        if seen < MAX_PER_ENTRY:
            counts[key] = seen + 1
            kept.append(rec)
    kept.reverse()
    cut = len(records) - len(kept)
    if len(kept) > MAX_TOTAL:
        cut += len(kept) - MAX_TOTAL
        kept = kept[-MAX_TOTAL:]
    return kept, cut


def _rewrite(path: Path, kept: list[dict], dropped: int) -> bool:
    """Atomically replace the log with the pruned set + the meta line that
    carries the dropped counter. os.replace keeps readers from ever seeing
    a half-written file."""
    try:
        lines = [json.dumps({"kind": "meta", "dropped": int(dropped)},
                            ensure_ascii=True, sort_keys=True)]
        lines.extend(json.dumps(rec, ensure_ascii=True, sort_keys=True)
                     for rec in kept)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        return False


@contextmanager
def _lock(directory: Path):
    """Serialize timeline writers across threads and processes.

    Yields False when no lock could be taken — the caller then SKIPS the
    record (a missing revision is honest history; an interleaved corrupt
    line is not). The in-process lock always serializes; on a host with
    POSIX flock a bounded wait guards the cross-process case, and a host
    without fcntl still gets the in-process guarantee plus single-line
    O_APPEND writes (readers skip a torn line)."""
    with _THREAD_LOCK:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            yield False
            return
        if fcntl is None:
            yield True
            return
        try:
            handle = open(directory / LOCK_NAME, "a+b")
        except OSError:
            yield False
            return
        try:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                yield False
                return
            deadline = time.monotonic() + LOCK_WAIT
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or exc.errno not in (
                            errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                        yield False
                        return
                    time.sleep(min(LOCK_RETRY, remaining))
            yield True
        finally:
            try:
                handle.close()
            except OSError:
                pass


def _append(rec: dict, *, repo: Path | None = None,
            state: Path | str | None = None) -> dict | None:
    """Store one record. Returns it on success, None when the log could
    not be written or locked (fail-soft — never an exception into the
    memory write that asked for the record)."""
    directory = state_dir(repo, state) / DIR_NAME
    with _lock(directory) as ok:
        if not ok:
            return None
        path = directory / FILE_NAME
        records, dropped = _read(path)
        records.append(rec)
        kept, cut = _prune(records)
        if cut:
            dropped += cut
            if _rewrite(path, kept, dropped):
                return rec
            # The cap could not be enforced (io) — keep the record anyway
            # rather than lose history silently; the next successful
            # prune re-applies the cap and the counter.
            return rec if _append_line(path, rec) else None
        return rec if _append_line(path, rec) else None


def record_entry(slot: str, title: str, op: str, body: str, *,
                 repo: Path | None = None, state: Path | str | None = None,
                 now: float | None = None) -> dict | None:
    """Append ONE revision record for one stored entry.

    `body` is the entry's stored text (marker-stripped, the form the
    forget control derives ids from) — used ONLY to derive the opaque id
    and the content digest; it is never written. Returns the record, or
    None on any failure: a bad slot/op or an unwritable log must never
    fail or change the memory write that triggered it."""
    try:
        if slot not in SLOT_LABELS or op not in OPS:
            return None
        if not isinstance(title, str) or not isinstance(body, str):
            return None
        rec = _build_record(slot, title, op, body, repo=repo, now=now)
        return _append(rec, repo=repo, state=state)
    except Exception:  # noqa: BLE001 — a log must never break its writer
        return None


def record_slot_write(slot: str, before: str, after: str, op: str, *,
                      repo: Path | None = None,
                      state: Path | str | None = None,
                      now: float | None = None) -> int:
    """Record the entries whose stored form CHANGED across a whole-file
    write (the console backup restore). Returns how many records landed.

    Only titles present in `after` are candidates: a section the write
    REMOVED may have been removed BY the suppression filter, so its title
    must never enter the log (supremacy beats completeness). Unchanged
    entries record nothing — a restore that changed nothing leaves no
    revision. Fail-soft: any failure returns 0, never raises."""
    try:
        if slot not in SLOT_LABELS or op not in OPS:
            return 0
        if not isinstance(before, str) or not isinstance(after, str):
            return 0
        channel = channel_of(slot)
        min_level, max_level = memforget.levels_for(channel)

        def entries(text: str) -> dict:
            """title -> digest of the stored body (markers stripped), the
            same derivation the forget control and the view use."""
            _, chunks = memforget.split_sections(text, min_level, max_level)
            if not chunks:
                return {"": memforget.digest_of(memforget.strip_markers(text))} \
                    if text.strip() else {}
            out: dict = {}
            for chunk in chunks:
                out.setdefault(chunk["heading"],
                               memforget.digest_of(
                                   memforget.strip_markers(chunk["body"])))
            return out

        old = entries(before)
        new = entries(after)
        written = 0
        for title, digest in new.items():
            if old.get(title) == digest:
                continue
            # Re-derive the body text for the id: the digest alone is not
            # the id's input (the id carries profile scope + channel), so
            # the record keeps exactly what a forget of THIS version would
            # record. The body comes from `after`, never from `before`.
            body = _body_of(after, title, channel)
            if body is None:
                continue
            if record_entry(slot, title, op, body, repo=repo, state=state,
                            now=now) is not None:
                written += 1
        return written
    except Exception:  # noqa: BLE001 — a log must never break its writer
        return 0


def _body_of(text: str, title: str, channel: str) -> str | None:
    """The stored body of ONE entry (marker-stripped), or None."""
    min_level, max_level = memforget.levels_for(channel)
    _, chunks = memforget.split_sections(text, min_level, max_level)
    if not chunks:
        return memforget.strip_markers(text) if text.strip() and title == "" \
            else None
    for chunk in chunks:
        if chunk["heading"] == title:
            return memforget.strip_markers(chunk["body"])
    return None


# ------------------------------------------------------------ read faces ----
def load_records(repo: Path | None = None,
                 state: Path | str | None = None) -> tuple[list[dict], int]:
    """(records in append order, persisted dropped count). READ-ONLY —
    an absent timeline is ([], 0) and nothing is created."""
    return _read(timeline_path(repo, state))


def _suppressed(snapshot: memforget.TombstoneSet, rec: dict,
                channel: str) -> bool:
    """Is THIS recorded version silenced right now? Both match forms the
    ledger keeps: the opaque id (this profile) and the content digest (a
    set that travelled from another profile)."""
    if not snapshot:
        return False
    if rec["id"] in snapshot.ids:
        return True
    return (channel, rec["digest"]) in snapshot.pairs


def _visible_titles(repo: Path | None, state: Path | str | None,
                    snapshot: memforget.TombstoneSet) -> dict[str, set]:
    """{slot: {headings}} of the entries the SUPPRESSION-FILTERED current
    slot files still hold — rule 2 of supremacy. An absent or unreadable
    slot yields no titles, so its history is withheld (the safe
    direction), never guessed."""
    visible: dict[str, set] = {slot: set() for slot in SLOT_LABELS}
    for slot in SLOT_LABELS:
        path = slot_file(slot, repo)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        channel = channel_of(slot)
        try:
            filtered = memforget.filter_slot(path, text, repo=repo,
                                             state=state, tombstones=snapshot)
            _, chunks = memforget.split_sections(
                filtered, *memforget.levels_for(channel))
        except (memforget.MemforgetError, ValueError):
            continue
        if chunks:
            visible[slot].update(chunk["heading"] for chunk in chunks)
        elif filtered.strip():
            visible[slot].add("")
    return visible


def history_for_view(snapshot: memforget.TombstoneSet | None,
                     visible: dict, records: list[dict], *,
                     per_entry: int = READ_PER_ENTRY,
                     max_total: int = READ_MAX_TOTAL
                     ) -> tuple[dict, bool]:
    """The console view's half — pure, no I/O.

    `visible` is {slot: {titles}} — the entries the view ALREADY shows
    from its own filtered read (rule 2 satisfied by construction); rule 1
    then still applies to every record via that set, and rule 3 is the
    per-record suppression check. Returns ({(slot, title): {"revisions",
    "records"}}, truncated): `revisions` counts every visible record of
    the entry (the honest total), `records` is the bounded newest-first
    slice a render shows.

    Fail-closed: an unknown snapshot (None) raises LedgerUnavailable —
    an unverifiable ledger never yields history."""
    if snapshot is None:
        raise memforget.LedgerUnavailable(memforget.LEDGER_UNAVAILABLE)
    stream: list[dict] = []
    for rec in records:
        if rec["title"] not in visible.get(rec["slot"], ()):
            continue                       # rule 2: not visible now
        if _suppressed(snapshot, rec, channel_of(rec["slot"])):
            continue                       # rule 3: this version is silenced
        stream.append(rec)
    counts: dict = {}
    for rec in stream:
        key = (rec["slot"], rec["title"])
        counts[key] = counts.get(key, 0) + 1
    truncated = False
    if len(stream) > max_total:
        stream = stream[len(stream) - max_total:]
        truncated = True
    buckets: dict = {}
    order: list = []
    for rec in stream:
        key = (rec["slot"], rec["title"])
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(rec)
    out: dict = {}
    for key in order:
        group = buckets[key]
        if len(group) > per_entry:
            group = group[len(group) - per_entry:]
            truncated = True
        out[key] = {"revisions": counts[key],
                    "records": [_public(rec) for rec in reversed(group)]}
    return out, truncated


def _public(rec: dict) -> dict:
    """What a read face returns for one record: the facts, no more. The
    group already carries slot and title, so they are not repeated."""
    return {"ts": rec["ts"], "op": rec["op"], "id": rec["id"],
            "digest": rec["digest"]}


def timeline(slot: str | None = None, title: str | None = None, *,
             repo: Path | None = None, state: Path | str | None = None
             ) -> dict:
    """The full read face, grouped per entry and suppression-filtered.

    Raises memforget.LedgerUnavailable when the suppression ledger is not
    known to be valid (fail-closed — never an empty "nothing here"), and
    TimelineError for an unknown slot. Bounded: READ_PER_ENTRY records per
    entry, READ_MAX_TOTAL overall, both reported through `truncated`."""
    if slot is not None and slot not in SLOT_LABELS:
        raise TimelineError("unknown memory slot")
    snapshot = memforget.load_set(repo, state)   # fail-closed here
    records, dropped = load_records(repo, state)
    visible = _visible_titles(repo, state, snapshot)
    grouped, truncated = history_for_view(snapshot, visible, records)
    entries = []
    for (g_slot, g_title), payload in grouped.items():
        if slot is not None and g_slot != slot:
            continue
        if title is not None and g_title != title:
            continue
        entries.append({"slot": g_slot, "title": g_title,
                        "revisions": payload["revisions"],
                        "records": payload["records"]})
    return {"slot": slot or "", "title": title if title is not None else "",
            "entries": entries,
            "records_total": sum(len(e["records"]) for e in entries),
            "dropped": dropped, "truncated": truncated,
            "limits": LIMITS_NOTE}
