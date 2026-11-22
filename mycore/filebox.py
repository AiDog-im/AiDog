"""mycore.filebox — outbound file staging for the file transfer car (0.5.90).

`send_file` (mycore.tools) resolves the source inside the allowed roots and
stages a COPY into <state>/outbox/<stamp>_<name>; the copy is what the
service layer later hands to a route adapter (TG sendDocument, feishu
upload+file message) — a copy, so a model that stages a file and then
deletes the original in the same turn can never corrupt the delivery.

Queue semantics mirror mycore.imagegen's photo outbox: a module-level list
guarded by a lock, drained atomically by whoever owns the current turn.
Only the service registers the send_file tool (build_registry
deliver_files=True), so a queue entry can only ever be staged inside the
service process — no other surface can leave a file for someone else's
turn to deliver.

Staged files are never deleted (the same stance as data/images): the tool
result names the staged path and that path stays true. Size is capped at
FILE_MAX_BYTES (mirrors tg.MAX_FILE_BYTES — the delivery cap) BEFORE the
copy, so an undeliverable file is refused honestly instead of failing at
send time. Failures raise FileError with a short English message (repo
i18n policy: code/messages are English); no credential ever reaches a
message, log or repr.
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

FILE_MAX_BYTES = 5_000_000        # mirrors tg.MAX_FILE_BYTES (delivery cap)
_STAMP_FMT = "%Y%m%d-%H%M%S"

_outbox: list[tuple[Path, str]] = []
_outbox_lock = threading.Lock()


class FileError(RuntimeError):
    """Honest staging failure; never contains a credential."""


def safe_name(raw, fallback: str = "file.bin") -> str:
    """A disk-safe file name: basename only, printable characters only
    (path separators, NUL, quotes and control characters stripped), 100
    chars max. `fallback` when nothing survives — never empty, never a
    traversal."""
    name = str(raw or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch != "\x00")
    name = name.strip().lstrip(".")[:100].strip()
    return name or fallback


def drain_outbox() -> list[tuple[Path, str]]:
    """Atomically take everything staged since the last drain."""
    with _outbox_lock:
        items = list(_outbox)
        _outbox.clear()
    return items


def save_inbox(state_dir: str | Path, raw_name, data: bytes) -> Path:
    """Persist a RECEIVED file under <state>/inbox/ with a sanitized,
    collision-free name (file car): basename-only safe_name, then
    report(1).pdf / report(2).pdf … so a re-sent file never overwrites.
    Raises OSError (mkdir/write) — callers turn it into one honest line."""
    inbox = Path(state_dir) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    name = safe_name(raw_name)
    path = inbox / name
    n = 1
    while path.exists():
        stem, dot, ext = name.partition(".")
        base = stem if dot else name
        path = inbox / (f"{base}({n}){dot + ext}" if dot else f"{base}({n})")
        n += 1
    path.write_bytes(data)
    return path


def stage(src: Path, state_dir: str | Path | None = None) -> tuple[Path, str]:
    """Copy `src` into <state>/outbox/ under a collision-free stamped name,
    queue (path, name) for the next drain, and return the pair.

    `state_dir` None resolves the profile/repo state root at CALL time
    (same seam as ckxbackup — AIDOG_MC_STATE aware). Raises FileError on a
    missing source, an unreadable source, an oversized file or an
    unwritable outbox; the message carries a type name, never content."""
    from lib import profile_paths as pp  # local: keeps the module graph acyclic
    from pathlib import Path as _P
    try:
        src = _P(src)
    except TypeError as e:
        raise FileError(f"bad path: {type(e).__name__}") from None
    if not src.is_file():
        raise FileError("not a readable file")
    try:
        size = src.stat().st_size
    except OSError as e:
        raise FileError(f"stat failed: {type(e).__name__}") from None
    if size > FILE_MAX_BYTES:
        raise FileError(f"file too large ({FILE_MAX_BYTES} byte limit)")
    if size == 0:
        raise FileError("file is empty")
    name = safe_name(src.name)
    root = Path(state_dir) if state_dir is not None else pp.state_dir()
    outdir = root / "outbox"
    stamp = time.strftime(_STAMP_FMT)
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        dst = outdir / f"{stamp}_{name}"
        n = 1
        while dst.exists():            # same-second re-sends never collide
            dst = outdir / f"{stamp}_{n}_{name}"
            n += 1
        shutil.copyfile(src, dst)
    except OSError as e:
        raise FileError(f"staging failed: {type(e).__name__}") from None
    with _outbox_lock:
        _outbox.append((dst, name))
    return dst, name


def queued_names() -> list[str]:
    """Names currently staged (diagnostics/tests only — never drains)."""
    with _outbox_lock:
        return [n for _, n in _outbox]
