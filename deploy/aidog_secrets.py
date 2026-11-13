#!/usr/bin/env python3
"""aidog secrets — E2 static encryption of secrets.env (opt-in control).

  aidog secrets status           # read-only: at-rest state (never values)
  aidog secrets enable           # create the out-of-tree 0600 passphrase
                                 # (source A) + encrypt secrets.env in place
  aidog secrets disable          # decrypt back to plaintext, remove the
                                 # passphrase file (--keep-passphrase keeps it)

Shell helpers used by deploy scripts (not for humans):
  aidog secrets status --state   # one word: absent|plain|locked|unlocked
                                 # (always exit 0 — the WORD carries truth)
  aidog secrets empty-slots A B  # print the names whose slot is empty or
                                 # unreadable (install.sh presence check)
  aidog secrets shell-env        # `export K='V'` lines for `eval`
                                 # (deploy_site.sh; never the passphrase)

Rules (docs/design/security_car_design.md §4 — non-negotiables):
  * the passphrase never enters argv, a log, a response or a screenshot —
    the vendored CKX child sees it only through `--password-env`;
  * plaintext never lands on disk — decrypt/encrypt run through stdin/stdout;
  * fail-closed — a locked store is refused with a reason word, never
    decrypted by anything other than source A, never left half-converted;
  * default OFF — installer/setup stay byte-identical until `enable` runs.

Exit codes: 0 ok / 1 error / 2 locked (fail-closed pending).
English output per repo i18n policy; values are never printed.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets as pysecrets
import shlex
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))  # `from mycore import secrets_store`

from lib import profile_paths  # noqa: E402  (profile seam, same as console)
from mycore import secrets_store  # noqa: E402

# Active profile's store (default profile = <repo>/secrets.env), resolved at
# import — the same seam web/server.py uses, so CLI and console never disagree.
SECRETS = profile_paths.secrets_path(REPO)


def say(msg: str) -> None:
    print(msg)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Sibling-temp atomic write (same hardening as the console writer:
    random name + O_CREAT|O_EXCL + O_NOFOLLOW, final mode from the start)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _own_line(text: str, key: str) -> bool:
    """Own-line, whitespace-free value (the exact install.sh grep rule:
    `^KEY=[^[:space:]]+$`) so shell presence checks and this reader agree."""
    return bool(re.search(rf"(?m)^{re.escape(key)}=[^ \t\r\n]+$", text))


def cmd_status(args: argparse.Namespace) -> int:
    state = secrets_store.state(SECRETS)
    if getattr(args, "state", False):
        # machine word for deploy scripts — the word carries the truth, so
        # this path ALWAYS exits 0 (callers branch on the word itself).
        print(state)
        return 0
    pwf = secrets_store.passphrase_path()
    say(f"secrets.env: {state}")
    if pwf.is_file():
        mode = stat.S_IMODE(pwf.stat().st_mode)
        say(f"passphrase file: {pwf} (mode {mode:04o}, out-of-tree)")
        if mode & 0o077:
            say("  WARNING: group/other accessible — `chmod 600` it "
                "(unlock refuses it as-is)")
    else:
        say(f"passphrase file: absent ({pwf})")
    if state == "plain":
        if pwf.is_file():
            say("store is PLAINTEXT while a passphrase file exists — run "
                "`aidog secrets enable` to encrypt (or delete the file)")
        else:
            say("E2 off (opt-in) — `aidog secrets enable` to encrypt "
                "at rest")
    elif state == "unlocked":
        say("encrypted at rest, unlocked in this process (values never "
            "shown)")
    elif state == "locked":
        say("LOCKED: readers fail-closed (empty store), writers refuse — "
            "restore the passphrase file above, then everything reads again")
    elif state == "absent":
        say("no store yet (run `aidog setup` to provision)")
    return 2 if state == "locked" else 0


def cmd_enable(_args: argparse.Namespace) -> int:
    st = secrets_store.detect(SECRETS)
    if st == "encrypted":
        say(f"already enabled — secrets.env is encrypted "
            f"(state: {secrets_store.state(SECRETS)})")
        return 0
    if st == "absent":
        say("FAIL: secrets.env absent — run `aidog setup` first")
        return 1
    pwf = secrets_store.passphrase_path()
    created = False
    if pwf.is_file():
        mode = stat.S_IMODE(pwf.stat().st_mode)
        if mode & 0o077:
            say(f"FAIL: passphrase file mode {mode:04o} is group/other "
                f"accessible — `chmod 600 {pwf}` first (refusing)")
            return 1
        say(f"reusing passphrase file: {pwf} (mode {mode:04o}, out-of-tree)")
    else:
        pwf.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(pwf.parent, 0o700)
        except OSError:
            pass                       # exotic FS: mode best-effort below
        atomic_write(pwf, (pysecrets.token_urlsafe(48) + "\n").encode(),
                     0o600)
        created = True
        say(f"passphrase file created: {pwf} (mode 0600, out-of-tree) — "
            f"back it up out-of-band; losing it locks the store")
    raw = SECRETS.read_bytes()
    blob = secrets_store.encrypt_bytes(raw)
    if blob is None:
        if created:
            pwf.unlink(missing_ok=True)   # rollback: E2 stays OFF
        say("FAIL: could not encrypt (vendored CKX CLI / node unavailable) "
            "— store left plaintext")
        return 1
    atomic_write(SECRETS, blob, 0o600)
    # verify-before-done: drop the in-process snapshot and really decrypt —
    # a roundtrip mismatch must roll back, never ship a broken store.
    secrets_store.forget()
    if secrets_store.secrets_text(SECRETS).encode("utf-8") != raw:
        atomic_write(SECRETS, raw, 0o600)
        secrets_store.forget()
        if created:
            pwf.unlink(missing_ok=True)
        say("FAIL: encrypt roundtrip mismatch — rolled back to plaintext "
            "(E2 off)")
        return 1
    say("secrets.env encrypted at rest (E2 on) — plaintext never written")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    st = secrets_store.detect(SECRETS)
    if st == "absent":
        say("secrets.env absent — nothing to disable")
        return 0
    if st != "encrypted":
        say("already plaintext — E2 is off")
        return 0
    if secrets_store.state(SECRETS) == "locked":
        say(f"FAIL: store LOCKED (no usable passphrase at "
            f"{secrets_store.passphrase_path()}) — refusing to touch the "
            f"file (fail-closed). Restore the passphrase file, then retry.")
        return 2
    orig = SECRETS.read_bytes()
    text = secrets_store.secrets_text(SECRETS)
    atomic_write(SECRETS, text.encode("utf-8"), 0o600)
    secrets_store.forget()
    if SECRETS.read_bytes() != text.encode("utf-8"):   # read-back verify
        atomic_write(SECRETS, orig, 0o600)
        say("FAIL: decrypt read-back mismatch — ciphertext restored")
        return 1
    say("secrets.env decrypted to plaintext (E2 off)")
    pwf = secrets_store.passphrase_path()
    if pwf.is_file():
        if getattr(args, "keep_passphrase", False):
            say(f"passphrase file kept: {pwf} — delete it to fully disable "
                f"(while it exists, provisioning re-encrypts)")
        else:
            pwf.unlink()
            say(f"passphrase file removed: {pwf}")
    return 0


def cmd_empty_slots(args: argparse.Namespace) -> int:
    """install.sh presence check: names whose slot has no whitespace-free
    own-line value. A LOCKED store prints every asked name (fail-closed:
    presence is unknowable, so nothing counts as filled)."""
    text = secrets_store.secrets_text(SECRETS)
    empty = [n for n in args.names if not _own_line(text, n)]
    if empty:
        print(" ".join(empty))
    return 0


def cmd_shell_env(args: argparse.Namespace) -> int:
    """deploy script credential load: shlex-quoted `export` lines on stdout
    (captured by the caller's `$(...)` — shell memory only, never a
    terminal, never a file). Optional NAME args narrow the output to those
    slots (run_service.sh needs exactly two — the same minimal env surface
    its sed path always exported). Locked -> reason on stderr, exit 2, so
    the caller dies instead of running tokenless."""
    state = secrets_store.state(SECRETS)
    if state == "locked":
        print(f"secrets.env locked (E2, passphrase unavailable at "
              f"{secrets_store.passphrase_path()})", file=sys.stderr)
        return 2
    names = list(getattr(args, "names", None) or [])
    text = secrets_store.secrets_text(SECRETS)
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not value:
            continue
        if names and key not in names:
            continue
        print(f"export {key}={shlex.quote(value)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="aidog secrets",
        description="E2 static encryption of secrets.env (opt-in; default "
                    "off). Values are never printed.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="read-only at-rest state report")
    st.add_argument("--state", action="store_true",
                    help="print one word (absent|plain|locked|unlocked); "
                         "always exit 0")
    en = sub.add_parser("enable",
                        help="create the out-of-tree 0600 passphrase + "
                             "encrypt secrets.env in place")
    en.set_defaults(func=cmd_enable)
    di = sub.add_parser("disable",
                        help="decrypt secrets.env + remove the passphrase "
                             "file")
    di.add_argument("--keep-passphrase", action="store_true",
                    help="decrypt but keep the passphrase file")
    di.set_defaults(func=cmd_disable)
    se = sub.add_parser("shell-env",
                        help="emit `export K='V'` lines for shell eval "
                             "(deploy_site.sh / run_service.sh)")
    se.add_argument("names", nargs="*",
                    help="only these slot names (default: every slot)")
    se.set_defaults(func=cmd_shell_env)
    em = sub.add_parser("empty-slots",
                        help="print asked names whose slot is empty/"
                             "unreadable (install.sh)")
    em.add_argument("names", nargs="+")
    em.set_defaults(func=cmd_empty_slots)
    st.set_defaults(func=cmd_status)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
