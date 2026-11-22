"""mycore.secrets_store — E2 static encryption of secrets.env (opt-in).

Owner rules (2026-11-07/08; docs/design/security_car_design.md §4 — the
non-negotiables):

1. the passphrase never enters argv, a log line, a response, a screenshot
   or the model context — the vendored CKX CLI only ever sees it through
   `--password-env` (child environment);
2. plaintext never lands on disk: decrypt and encrypt both run through
   stdin/stdout (`--in -` / `--out -`), so the runtime file is ciphertext
   at rest and the plaintext lives only in this process's memory;
3. fail-closed: locked and no passphrase -> readers see an EMPTY store
   (honest absence, same shape as "no slots"), writers refuse, and the
   console / doctor surface `locked` explicitly — never a plaintext
   fallback;
4. default OFF: a plaintext store keeps the exact historical read/write
   bytes (this module then only detects and passes through), so E2 is a
   pure opt-in — nothing in install/setup changes until `enable` runs.

Format: the CKX container (first four bytes ``SAFE``); anything else is
the plaintext env format. Unlock source A (the adjudicated default): a
0600 passphrase file OUTSIDE the repo/state tree
(``~/.config/aidog/secrets.passphrase``; env override
``AIDOG_SECRETS_PASSPHRASE_FILE`` for ops and tests). The passphrase is
read into this process, handed to the CKX child via a child env var, and
cached beside the decrypted text under a lock — one unlock per process,
every later read is a dict lookup.

The subprocess plumbing is shared with mycore.ckxbackup (same vendored
CLI, same injectable runner — tests never touch a real binary or the real
store).
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from . import ckxbackup

# container magic: "SAFE" + version byte (observed on the vendored v0.6 CLI)
MAGIC = b"SAFE"
# passphrase file (source A) — env override for ops/tests only
PW_FILE_ENV = "AIDOG_SECRETS_PASSPHRASE_FILE"
DEFAULT_PW_FILE = Path.home() / ".config" / "aidog" / "secrets.passphrase"
# the ONLY env name the child ever sees for the passphrase value
CHILD_PW_ENV = "AIDOG_SECRETS_PW"
# a failed unlock is remembered until forget() (no subprocess per read)
_UNLOCK_TIMEOUT = 60

_LOCK = threading.RLock()
_CACHE: dict = {"path": None, "text": None, "pw": None, "failed": None}


class SecretsError(Exception):
    """Store-level failure with a chat-safe reason only (never a value)."""


def passphrase_path() -> Path:
    """Source A: the out-of-tree passphrase file (env override wins)."""
    override = os.environ.get(PW_FILE_ENV, "").strip()
    return Path(override) if override else DEFAULT_PW_FILE


def detect(path: str | Path) -> str:
    """'absent' | 'plain' | 'encrypted' — the first bytes decide."""
    p = Path(path)
    try:
        with p.open("rb") as fh:
            head = fh.read(4)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "absent"          # unreadable = unusable = honest absence
    if head[:4] == MAGIC:
        return "encrypted"
    return "plain"


def is_locked(path: str | Path) -> bool:
    """True only when the store IS encrypted and this process cannot read
    it — the explicit state the console/doctor must surface (rule3)."""
    p = Path(path)
    if detect(p) != "encrypted":
        return False
    with _LOCK:
        if _CACHE["path"] == p and _CACHE["text"] is not None:
            return False
    return not _attempt_unlock(p)


def state(path: str | Path) -> str:
    """'absent' | 'plain' | 'locked' | 'unlocked' — for status surfaces."""
    p = Path(path)
    st = detect(p)
    if st != "encrypted":
        return st
    with _LOCK:
        if _CACHE["path"] == p and _CACHE["text"] is not None:
            return "unlocked"
    return "locked" if not _attempt_unlock(p) else "unlocked"


def secrets_text(path: str | Path) -> str:
    """The store contents for every reader.

    plain   -> read the file (byte-identical historical behavior, no cache)
    locked  -> "" (fail-closed honest absence; never a plaintext fallback)
    unlocked-> the cached decrypted text (rule2: memory only).
    """
    p = Path(path)
    st = detect(p)
    if st == "absent":
        return ""
    if st == "plain":
        return p.read_text(encoding="utf-8")
    with _LOCK:
        if _CACHE["path"] == p and _CACHE["text"] is not None:
            return _CACHE["text"]
    if not _attempt_unlock(p):
        return ""                      # locked: honest absence (rule3)
    with _LOCK:
        return _CACHE["text"] or ""


def encrypt_bytes(data: bytes) -> bytes | None:
    """Ciphertext for an atomic write. None = no usable passphrase (the
    CALLER must refuse — rule3: never write plaintext over a locked store).

    A fresh writer (enable / provision / console process that never
    unlocked) gets the passphrase straight from source A; an unlocked
    process uses its cached one. Write paths are rare, so a source-A read
    per write costs nothing.
    """
    pw = _passphrase()
    if not pw:
        return None
    return _ckx(["encrypt", "--in", "-", "--out", "-"], data, pw)


def set_text(path: str | Path, text: str) -> None:
    """Post-write cache refresh (single-writer discipline): this process
    just wrote `text` to `path`, so later reads must not serve the
    pre-write snapshot. Memory only — nothing lands on disk here."""
    with _LOCK:
        _CACHE.update(path=Path(path), text=text, failed=None)


def forget() -> None:
    """Drop the cached passphrase/plaintext (tests, disable, rekey)."""
    with _LOCK:
        _CACHE.update(path=None, text=None, pw=None, failed=None)


def _passphrase() -> str | None:
    """Source-A passphrase for a WRITE path: the cached one when this
    process already unlocked, else a fresh read of the out-of-tree file
    (0600 enforced — a world-readable file is refused). Never logged,
    never argv; the caller hands it to the child as an env value."""
    with _LOCK:
        if _CACHE["pw"]:
            return _CACHE["pw"]
    pw_file = passphrase_path()
    try:
        mode = pw_file.stat().st_mode & 0o777
        pw = pw_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not pw or (mode & 0o077):
        return None
    return pw


def _attempt_unlock(p: Path) -> bool:
    """Source-A unlock: read the out-of-tree passphrase file, decrypt via
    the CKX child (env-only passphrase), cache text+pw. Failures are
    remembered so a locked store never spawns a subprocess per read."""
    with _LOCK:
        if _CACHE["path"] == p and _CACHE["text"] is not None:
            return True
        if _CACHE["failed"] == p:
            return False
    pw = _passphrase()
    if not pw:
        with _LOCK:
            _CACHE["failed"] = p
        return False
    plain = _ckx(["decrypt", "--in", str(p), "--out", "-"], None, pw)
    if plain is None:
        with _LOCK:
            _CACHE["failed"] = p
        return False
    with _LOCK:
        _CACHE.update(path=p, text=plain.decode("utf-8", "replace"),
                      pw=pw, failed=None)
    return True


def _ckx(args: list[str], data: bytes | None, pw: str) -> bytes | None:
    """One vendored-CLI call. The passphrase travels ONLY in the child
    environment (rule1); stdout is the result, stderr never leaks."""
    node = ckxbackup._node_exe()
    if not node:
        return None
    env = os.environ.copy()
    env[CHILD_PW_ENV] = pw
    argv = [node, str(ckxbackup.CKX_CLI), *args,
            "--password-env", CHILD_PW_ENV]
    try:
        proc = ckxbackup._run(argv, env=env, data=data,
                              timeout=_UNLOCK_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or b""
