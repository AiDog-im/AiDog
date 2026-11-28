"""mycore.steer — queued steering for a run that is already working.

prior art's "queueing steering", ported onto our own contracts: while one
turn is in flight the owner's follow-up instructions are accepted into a
BOUNDED, DURABLE queue and merged into the LIVE run at its next iteration
boundary — the run does not stop, the instruction does not wait for the
turn to end, and nothing is silently dropped.

Three rules, in priority order:

1. Suppression first (D31). A queued instruction is a transcript message
   like any other: it is checked against the session-channel suppression
   set at ADMISSION and again at MERGE, so a source the owner forgot can
   never come back through the queue. An unreadable ledger fails CLOSED —
   the item is refused (admission) or withheld (merge), never treated as
   "nothing is forgotten".
2. Bounded. MAX_ITEMS per session key and MAX_CHARS per item; over a bound
   is an honest refusal (SteerError with a reason word), never a silent
   drop and never truncate-then-accept.
3. Durable. One JSON document under the ACTIVE profile's state root,
   written atomically (tmp + os.replace), so a restart keeps the queue.
   Every item carries an id and a state: `pending` (waiting) and
   `inflight` (merged into a run, not yet persisted by that run). `recover`
   closes the crash window — it reads the session back and either commits
   an inflight item (its text is in the transcript: it landed) or releases
   it (it did not: it rides the next turn). Nothing is lost, nothing runs
   twice.

Isolation: one queue file per profile (the state root), one ITEM key per
session — a run only ever takes its own key's items.

The module never echoes message text: a refusal carries a reason WORD and
an English detail line, exactly like the other gates.
"""
from __future__ import annotations

import errno
import json
import os
import secrets
import threading
import time
from pathlib import Path

from lib import profile_paths as pp  # noqa: E402  (profile state seam)

try:                                 # native Windows has no fcntl — see
    import fcntl                     # memforget/evolution: degrade to the
except ImportError:                  # in-process lock, never crash on it
    fcntl = None

QUEUE_NAME = "steer_queue.json"
LOCK_NAME = "steer_queue.lock"
VERSION = 1

MAX_ITEMS = 5                        # per session key (the console's bar)
MAX_CHARS = 4000                     # per item, refusing never truncating
LOCK_WAIT = 0.25                     # seconds before a contended write gives up
LOCK_RETRY = 0.005

# Reason word -> the English detail line a refusal answers with. Server-side
# strings stay English by repo policy; the console renders them verbatim
# (the standing contract for non-gate error details).
DETAILS = {
    "empty": "message is empty",
    "too_long": f"message too long (max {MAX_CHARS} characters)",
    "full": f"queue is full ({MAX_ITEMS} items) — wait for the current turn",
    "suppressed": "this message is suppressed and cannot be queued",
    "ledger_unavailable": ("the suppression ledger is unavailable; "
                           "the message was not queued"),
    "unreadable": "the steering queue could not be read",
    "not_writable": "the steering queue could not be written",
}


class SteerError(RuntimeError):
    """An honest refusal. The message is a reason line — never the source
    text, a path or a credential."""

    def __init__(self, code: str) -> None:
        detail = DETAILS.get(code, code)
        super().__init__(detail)
        self.code = code
        self.detail = detail


def queue_path(state: Path | str | None = None) -> Path:
    """<state>/steer_queue.json — the ACTIVE profile's state root unless an
    explicit `state` is given (the test seam and the restore-elsewhere path)."""
    root = Path(state) if state is not None else pp.state_dir()
    return root / QUEUE_NAME


def _sessions_dir(state: Path | str | None = None) -> Path:
    """The transcripts directory this queue is judged against: the product
    layout puts it directly under the state root (same rule as
    mycore.session.state_root_of)."""
    root = Path(state) if state is not None else pp.state_dir()
    return root / "sessions"


class SteerQueue:
    """The bounded, durable steering queue of ONE profile.

    `state` overrides the state root (tests); `directory` overrides the
    sessions directory the suppression set is resolved from (a caller whose
    transcripts live elsewhere). Both default to the active profile.
    """

    def __init__(self, state: Path | str | None = None,
                 directory: Path | str | None = None) -> None:
        self.path = queue_path(state)
        self.directory = Path(directory) if directory is not None \
            else _sessions_dir(state)
        self._lock = threading.Lock()          # in-process writers

    # ------------------------------------------------------------ storage --
    def _read(self) -> dict:
        """The queue document. A missing file is an empty queue; an
        unreadable or damaged file is a REFUSAL (fail-closed), never an
        empty set — a queue we cannot read must not look like a queue with
        nothing in it."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": VERSION, "items": []}
        except OSError:
            raise SteerError("unreadable") from None
        try:
            doc = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise SteerError("unreadable") from None
        items = doc.get("items") if isinstance(doc, dict) else None
        if not isinstance(items, list):
            raise SteerError("unreadable")
        clean = []
        for item in items:
            if (isinstance(item, dict) and isinstance(item.get("id"), str)
                    and isinstance(item.get("key"), str)
                    and isinstance(item.get("text"), str)
                    and item.get("state") in ("pending", "inflight")):
                clean.append(item)
        return {"version": VERSION, "items": clean}

    def _write(self, doc: dict) -> None:
        """Atomic replace: a crash leaves either the old queue or the new
        one, never a half-written file."""
        tmp: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:                # best effort, never raise here
                    pass
            raise SteerError("not_writable") from None

    def _mutate(self, fn, skip=None) -> None:
        """Run `fn(doc)` under the in-process lock AND a bounded cross-
        process flock (the file is shared with the service process once a
        second surface drives turns). Where POSIX locking is unavailable
        (native Windows) the in-process lock still serializes this process
        and the atomic replace keeps the file valid — a contended writer
        fails closed instead of clobbering.

        `skip(doc)` is the FAST PATH: a probe that would decide nothing
        returns before any lock file exists, so an empty queue costs one
        read and creates no file at all."""
        with self._lock:
            doc = self._read()
            if skip is not None and skip(doc):
                return
            if fcntl is None:
                fn(doc)
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self.path.with_name(LOCK_NAME), "a+b")
            except OSError:
                raise SteerError("not_writable") from None
            try:
                deadline = time.monotonic() + LOCK_WAIT
                while True:
                    try:
                        fcntl.flock(handle.fileno(),
                                    fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as exc:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise SteerError("not_writable") from None
                        if exc.errno not in (errno.EACCES, errno.EAGAIN,
                                             errno.EWOULDBLOCK):
                            raise SteerError("not_writable") from None
                        time.sleep(min(LOCK_RETRY, remaining))
                # authoritative read: the probe above was only a fast path
                fn(self._read())
            finally:
                try:
                    handle.close()
                except OSError:
                    pass

    # -------------------------------------------------------- suppression --
    def _suppression(self):
        """The session-channel suppressor of this queue's transcripts. A
        caller that needs the reason word gets `available` from the set —
        `message_suppression` turns an unreadable ledger into
        `available=False` instead of raising, which is exactly the
        fail-closed signal this module wants."""
        from . import session as _session        # local: keeps the graph acyclic
        return _session.message_suppression(directory=self.directory)

    def _admission_visible(self, text: str) -> None:
        """Refuse (fail-closed) unless the message may still be spoken."""
        sup = self._suppression()
        if not sup.available:
            raise SteerError("ledger_unavailable")
        from . import session as _session
        if sup.suppresses_digest(_session.text_digest(text)):
            raise SteerError("suppressed")

    # ------------------------------------------------------------ public --
    def enqueue(self, key: str, text: str, *, uid: object = None) -> dict:
        """Admit one instruction for `key`. Returns {"id", "n", "max"}.

        `uid` is an idempotency key (the caller's message id): a redelivered
        admission is answered with the EXISTING item instead of a second
        copy, so a crash between "queued" and "acked" never doubles an item.
        """
        text = (text or "").strip()
        if not text:
            raise SteerError("empty")
        if len(text) > MAX_CHARS:
            raise SteerError("too_long")
        result: dict = {}

        def apply(doc: dict) -> None:
            items = doc["items"]
            if uid is not None:
                for item in items:
                    if item.get("uid") == uid and item.get("key") == key:
                        result.update(id=item["id"],
                                      n=self._count(items, key),
                                      max=MAX_ITEMS, dup=True)
                        return
            if self._count(items, key) >= MAX_ITEMS:
                raise SteerError("full")
            self._admission_visible(text)        # the invariant, before the write
            item = {"id": secrets.token_hex(4), "key": key, "text": text,
                    "state": "pending", "ts": time.time()}
            if uid is not None:
                item["uid"] = uid
            items.append(item)
            result.update(id=item["id"], n=self._count(items, key),
                          max=MAX_ITEMS)
            self._write(doc)

        self._mutate(apply)
        return result

    @staticmethod
    def _count(items: list[dict], key: str) -> int:
        return sum(1 for i in items if i.get("key") == key)

    def pending(self, key: str) -> list[dict]:
        """The items of `key` still waiting (never inflight)."""
        with self._lock:
            doc = self._read()
        return [{"id": i["id"], "text": i["text"]} for i in doc["items"]
                if i.get("key") == key and i.get("state") == "pending"]

    def remaining(self, key: str) -> int:
        return len(self.pending(key))

    def take(self, key: str) -> list[dict]:
        """Decide every pending item of `key` and return the decisions as
        [{"id", "text", "dropped"}] — this is the merge point: the caller
        injects the NOT-dropped texts into the run and `commit`s them once
        the run's rows are on disk.

        `dropped: True` means the item was forgotten WHILE it waited: it is
        already out of the queue and must never reach the model (the caller
        still learns its id so its row can be marked honestly — never its
        text re-surfaced anywhere else).

        Fail-closed on the suppression path: an unreadable ledger REFUSES
        the merge with `ledger_unavailable` (every item still pending,
        nothing injected) — a queue we cannot verify injects nothing
        instead of injecting everything. An empty queue returns [] without
        opening the ledger at all.
        """
        decided: list[dict] = []

        def apply(doc: dict) -> None:
            items = doc["items"]
            mine = [i for i in items
                    if i.get("key") == key and i.get("state") == "pending"]
            if not mine:
                return
            # Resolved only once there is something to decide: an empty
            # queue never even opens the ledger (the D34 "costs nothing
            # while nothing is forgotten" rule, applied to this surface too).
            sup = self._suppression()
            if not sup.available:
                # Fail-closed AND honest: the caller is told why nothing is
                # being merged, and every item stays exactly where it was.
                raise SteerError("ledger_unavailable")
            from . import session as _session
            keep: list[dict] = []
            for item in items:
                if item in mine:
                    if sup.suppresses_digest(
                            _session.text_digest(item["text"])):
                        # forgotten while queued: out of the queue for good,
                        # reported by id only — never handed to the model.
                        decided.append({"id": item["id"],
                                        "text": item["text"],
                                        "dropped": True})
                        continue
                    item["state"] = "inflight"
                    decided.append({"id": item["id"], "text": item["text"],
                                    "dropped": False})
                keep.append(item)
            doc["items"] = keep
            self._write(doc)

        self._mutate(apply, skip=lambda doc: not any(
            i.get("key") == key and i.get("state") == "pending"
            for i in doc["items"]))
        return decided

    def commit(self, items: list[dict]) -> None:
        """The run persisted its rows: drop those items for good."""
        if not items:
            return
        ids = {i.get("id") for i in items if isinstance(i, dict)}
        if not ids:
            return

        def apply(doc: dict) -> None:
            kept = [i for i in doc["items"] if i.get("id") not in ids]
            if len(kept) != len(doc["items"]):
                doc["items"] = kept
                self._write(doc)

        self._mutate(apply, skip=lambda doc: not any(
            i.get("id") in ids for i in doc["items"]))

    def release(self, items: list[dict]) -> None:
        """The run did NOT persist its rows: put the items back to pending
        so they ride the next turn instead of vanishing with a lost turn."""
        if not items:
            return
        ids = {i.get("id") for i in items if isinstance(i, dict)}
        if not ids:
            return

        def apply(doc: dict) -> None:
            changed = False
            for item in doc["items"]:
                if item.get("id") in ids and item.get("state") == "inflight":
                    item["state"] = "pending"
                    changed = True
            if changed:
                self._write(doc)

        self._mutate(apply, skip=lambda doc: not any(
            i.get("id") in ids and i.get("state") == "inflight"
            for i in doc["items"]))

    def recover(self, key: str, persisted_texts) -> int:
        """Close the crash window of an inflight item.

        The run took the item (state=inflight) and the process died before
        (or after) the session append. Compare the item's text against the
        messages the session actually holds: found -> it landed, commit it;
        missing -> it did not, release it so it rides the next turn. Returns
        the number of items decided. Cheap by construction: a no-op while
        nothing is inflight for `key`."""
        texts = [t for t in (persisted_texts or []) if isinstance(t, str)]
        decided = 0

        def apply(doc: dict) -> None:
            nonlocal decided
            mine = [i for i in doc["items"]
                    if i.get("key") == key and i.get("state") == "inflight"]
            if not mine:
                return
            keep = []
            for item in doc["items"]:
                if item in mine:
                    if any(item["text"] in t for t in texts):
                        decided += 1              # in the transcript: done
                        continue
                    item["state"] = "pending"    # never reached the model
                    decided += 1
                keep.append(item)
            doc["items"] = keep
            self._write(doc)

        self._mutate(apply, skip=lambda doc: not any(
            i.get("key") == key and i.get("state") == "inflight"
            for i in doc["items"]))
        return decided
