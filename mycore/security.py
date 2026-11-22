"""mycore.security — the startup security self-check (car E, owner ask #6).

Six READ-ONLY probes over the local machine and the credential file, so the
console can tell the owner whether his laptop's exposure matches the
historical incident pattern (design: docs/design/security_car_design.md;
root-cause research: docs/research/car_e_theft_root_cause.md — internal).

Contract (every caller depends on it):
  * fail-soft: `run_checks()` NEVER raises — a probe that cannot run degrades
    its own row to `unknown` with an honest one-line reason;
  * zero value echo: results carry slot NAMES, counts, lengths, modes and
    filesystem names only — never a credential value (the token-strength
    row reports length + character classes, the same surface the Credentials
    card already shows);
  * read-only: probes only observe. Nothing is patched, restarted, blocked
    or "fixed" — the console reports, the owner decides. The firewall chain
    uses list/status subcommands only (never -I/-A/-D/-P);
  * honest states: ok / warn / fail / unknown, where `unknown` is a
    first-class verdict (permission bits are genuinely not enforceable on a
    9p mount) — never dressed up as ok;
  * verdict = worst state over the six rows (fail > warn > unknown > ok).

Checks, in report order:
  listening  console port loopback? + other non-loopback TCP listeners
  firewall   ufw -> firewall-cmd -> nft -> iptables, first verdict wins
  token      WEB_TOKEN strength heuristic (length + classes + template words)
  sentinel   any secrets.env value still equal to a template/example value
  age        secrets.env mtime age (a review nudge — local files never expire)
  perms      secrets.env mode, qualified by what the filesystem enforces

Seams (tests only): `set_runner_for_tests` (subprocess), `PROC_NET_DIR`
(fallback listening probe), and the explicit path/time parameters of
`run_checks`.
"""
from __future__ import annotations

import os
import re
import socket
import stat
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
from lib import profile_paths as pp  # noqa: E402  (profile path seam)

CHECK_IDS = ("listening", "firewall", "token", "sentinel", "age", "perms")
STATE_RANK = {"ok": 0, "unknown": 1, "warn": 2, "fail": 3}  # worst wins

# Whole-value template words (case-insensitive, exact match only — a substring
# match would false-positive on real keys). Kept deliberately tight: these are
# values a human left from a sample file instead of choosing their own.
TEMPLATE_WORDS = frozenset({
    "changeme", "change-me", "change_me", "replaceme", "replace-me",
    "replace_me", "your-token-here", "your_token_here", "your-token",
    "your-api-key", "your_api_key", "password", "secret", "123456",
    "12345678", "123456789", "admin", "root", "letmein", "welcome",
    "test1234", "example", "placeholder", "sample", "dummy",
})

# Filesystems that do not store POSIX permission bits — a group/other-readable
# mode there is a reporting artifact, not a leak, and must say so honestly.
NOT_ENFORCEABLE_FS = frozenset({
    "9p", "drvfs", "cifs", "smb3", "smbfs", "vfat", "msdos", "exfat",
    "ntfs", "ntfs3", "ncpfs", "afs", "coda",
})

AGE_WARN_DAYS = 180          # unchanged longer than this -> review nudge
FUTURE_MTIME_GRACE = 300     # seconds of clock jitter tolerated as "now"
LISTEN_PORT_CAP = 8          # ports listed in the listening row's args
PROC_NET_DIR = Path("/proc/net")   # fallback probe root (test seam)

_ENV_RE = re.compile(r"(?m)^([A-Za-z_][A-Za-z0-9_]*)=(\S[^\r\n]*)$")

# ------------------------------------------------------------- test seams ---
_RUNNER = None               # callable(argv, timeout=...) -> _Res | None


class _Res:
    """Minimal CompletedProcess stand-in (also what test fakes return)."""

    def __init__(self, returncode: int, stdout: str = "",
                 stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def set_runner_for_tests(fn) -> None:
    """Install/replace the subprocess seam (None restores the real runner)."""
    global _RUNNER
    _RUNNER = fn


def _run(argv: list[str], timeout: float = 3.0) -> _Res | None:
    """Run one read-only probe. None = binary missing / hung / seam refused
    (every caller maps None to an honest `unknown`, never to a guess)."""
    if _RUNNER is not None:
        try:
            return _RUNNER(argv, timeout=timeout)
        except Exception:  # noqa: BLE001 — a refusing seam reads as unavailable
            return None
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return _Res(p.returncode, p.stdout or "", p.stderr or "")
    except Exception:  # noqa: BLE001 — FileNotFoundError / TimeoutExpired / ...
        return None


# ------------------------------------------------------- probe: listening ----
def _hex_ip(h: str, v6: bool) -> str:
    """/proc/net packed hex -> presentation address (kernel stores each 32-bit
    word host-endian; both families go through inet_ntop for a canonical form)."""
    try:
        raw = bytes.fromhex(h)
        if v6:
            if len(raw) != 16:
                return ""
            raw = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
            return socket.inet_ntop(socket.AF_INET6, raw)
        if len(raw) != 4:
            return ""
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    except Exception:  # noqa: BLE001 — malformed hex is simply skipped
        return ""


def _parse_proc_net(path: Path, v6: bool) -> list[tuple[str, int]]:
    """One /proc/net/tcp{,6} file -> [(host, port)] for LISTEN (st=0A)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[tuple[str, int]] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "0A":
            continue
        host_hex, _, port_hex = parts[1].rpartition(":")
        host = _hex_ip(host_hex, v6)
        try:
            port = int(port_hex, 16)
        except ValueError:
            continue
        if host and port:
            out.append((host, port))
    return out


def _parse_ss(text: str) -> list[tuple[str, int]]:
    """`ss -lnt` output -> [(host, port)] for LISTEN rows."""
    out: list[tuple[str, int]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "LISTEN":
            continue
        local = parts[3]
        host, _, port_s = local.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_s)
        except ValueError:
            continue
        if host and port:
            out.append((host, port))
    return out


def _is_loopback(host: str) -> bool:
    return host == "::1" or host.startswith("127.")


def _is_wildcard(host: str) -> bool:
    return host in ("0.0.0.0", "::", "*")


def _listening_entries() -> list[tuple[str, int]] | None:
    """TCP listeners: `ss -lnt` first, /proc/net fallback, None = unavailable."""
    res = _run(["ss", "-lnt"], timeout=3.0)
    if res is not None and res.returncode == 0 and res.stdout.strip():
        return _parse_ss(res.stdout)
    # fallback: no ss binary, non-zero exit, or empty output
    entries: list[tuple[str, int]] = []
    readable = False
    for name, v6 in (("tcp", False), ("tcp6", True)):
        p = PROC_NET_DIR / name
        if p.is_file():
            readable = True
            entries.extend(_parse_proc_net(p, v6))
    if readable:
        return entries
    return None


def _check_listening(console_port: int | None) -> dict:
    entries = _listening_entries()
    if entries is None:
        return {"id": "listening", "state": "unknown",
                "i18n": "sck.d.listening.unknown",
                "detail": "listening-table probe unavailable",
                "args": {}}
    if console_port is None:
        return {"id": "listening", "state": "unknown",
                "i18n": "sck.d.listening.noport",
                "detail": "console port not provided", "args": {}}
    own = [h for h, p in entries if p == console_port]
    if not own:
        return {"id": "listening", "state": "unknown",
                "i18n": "sck.d.listening.absent",
                "detail": f"console port {console_port} not found in the "
                          f"listening table",
                "args": {"port": console_port}}
    exposed_own = [h for h in own if not _is_loopback(h)]
    others = sorted({p for h, p in entries
                     if p != console_port and not _is_loopback(h)})
    ports = ", ".join(str(p) for p in others[:LISTEN_PORT_CAP])
    if len(others) > LISTEN_PORT_CAP:
        ports += f" +{len(others) - LISTEN_PORT_CAP}"
    args = {"port": console_port, "others": len(others), "ports": ports}
    if exposed_own:
        return {"id": "listening", "state": "fail",
                "i18n": "sck.d.listening.fail",
                "detail": f"console port {console_port} is exposed beyond "
                          f"loopback ({', '.join(sorted(set(exposed_own)))})",
                "args": args}
    if others:
        return {"id": "listening", "state": "warn",
                "i18n": "sck.d.listening.warn",
                "detail": f"console on loopback ({console_port}) · "
                          f"{len(others)} other service(s) expose: {ports}",
                "args": args}
    return {"id": "listening", "state": "ok",
            "i18n": "sck.d.listening.ok",
            "detail": f"console on loopback ({console_port}) · no other "
                      f"service exposes a TCP port",
            "args": args}


# -------------------------------------------------------- probe: firewall ----
def _ufw_verdict(res: _Res) -> str | None:
    if res.returncode != 0:
        return None                      # needs root / not really answering
    if "Status: active" in res.stdout:
        return "active"
    if "Status: inactive" in res.stdout:
        return "inactive"
    return None


def _firewalld_verdict(res: _Res) -> str | None:
    if res.returncode == 0 and "running" in res.stdout:
        return "active"
    if res.returncode != 0 and "not running" in (res.stdout + res.stderr):
        return "inactive"
    return None


def _nft_verdict(res: _Res) -> str | None:
    if res.returncode != 0:
        return None                      # permission denied -> unreadable
    return "active" if re.search(r"\bhook\b", res.stdout) else "inactive"


def _iptables_verdict(res: _Res) -> str | None:
    if res.returncode != 0:
        return None                      # permission denied -> unreadable
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith(("-A ",
                                                                     "-P "))]
    if any(ln.startswith("-A ") for ln in lines):
        return "active"                  # real filtering rules exist
    if any(re.match(r"-P\s+(INPUT|FORWARD)\s+(DROP|REJECT)", ln)
           for ln in lines):
        return "active"                  # default-deny policy is filtering
    return "inactive"                    # ACCEPT policies, no rules


_FIREWALL_BACKENDS = (
    ("ufw", ["ufw", "status"], _ufw_verdict),
    ("firewalld", ["firewall-cmd", "--state"], _firewalld_verdict),
    ("nft", ["nft", "list", "ruleset"], _nft_verdict),
    ("iptables", ["iptables", "-S"], _iptables_verdict),
)


def _check_firewall() -> dict:
    """Chain of read-only backends: first `active` wins; otherwise the first
    definite `inactive` names the row; nothing readable -> unknown."""
    first_inactive = ""
    for name, argv, verdict in _FIREWALL_BACKENDS:
        res = _run(argv, timeout=2.0)
        if res is None:
            continue                     # binary missing / seam refused
        v = verdict(res)
        if v == "active":
            return {"id": "firewall", "state": "ok",
                    "i18n": "sck.d.firewall.ok",
                    "detail": f"{name}: filtering active",
                    "args": {"backend": name}}
        if v == "inactive" and not first_inactive:
            first_inactive = name
    if first_inactive:
        return {"id": "firewall", "state": "warn",
                "i18n": "sck.d.firewall.warn",
                "detail": f"{first_inactive}: no filtering detected",
                "args": {"backend": first_inactive}}
    return {"id": "firewall", "state": "unknown",
            "i18n": "sck.d.firewall.unknown",
            "detail": "no readable firewall backend (missing binary or "
                      "permission)",
            "args": {}}


# ---------------------------------------------------------- probe: token -----
def token_strength(value: str) -> dict:
    """WEB_TOKEN strength heuristic -> one report row (NEVER echoes `value`;
    only length and character-class counts leave this frame)."""
    v = (value or "").strip()
    if not v:
        return {"id": "token", "state": "unknown",
                "i18n": "sck.d.token.unknown", "detail": "not configured",
                "args": {}}
    classes = sum(bool(re.search(pat, v)) for pat in
                  (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    args = {"length": len(v), "classes": classes}
    if v.lower() in TEMPLATE_WORDS:
        return {"id": "token", "state": "fail",
                "i18n": "sck.d.token.template",
                "detail": "still a template value — replace it",
                "args": {}}
    if len(v) < 16:
        return {"id": "token", "state": "fail",
                "i18n": "sck.d.token.fail",
                "detail": f"too weak ({len(v)} characters) — use a long "
                          f"random string",
                "args": args}
    if len(v) >= 32 or (len(v) >= 24 and classes >= 3):
        return {"id": "token", "state": "ok",
                "i18n": "sck.d.token.ok",
                "detail": f"strong ({len(v)} characters, {classes} classes)",
                "args": args}
    return {"id": "token", "state": "warn",
            "i18n": "sck.d.token.warn",
            "detail": f"weak ({len(v)} characters, {classes} classes) — "
                      f"lengthen it",
            "args": args}


def _parse_env_file(path: Path) -> dict[str, str]:
    """Own-line KEY=value pairs with NON-EMPTY values (comments and `KEY=`
    lines never match — the same rule the runtime slot readers use)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise
    return {m.group(1): m.group(2).strip()
            for m in _ENV_RE.finditer(text)}


# ------------------------------------------------------- probe: sentinel -----
def _check_sentinel(secrets_path: Path, example_path: Path) -> dict:
    try:
        values = _parse_env_file(secrets_path)
    except OSError:
        return {"id": "sentinel", "state": "unknown",
                "i18n": "sck.d.sentinel.unknown",
                "detail": "secrets.env unreadable", "args": {}}
    example: dict[str, str] = {}
    try:
        if example_path.is_file():
            example = _parse_env_file(example_path)
    except OSError:
        example = {}                     # no template to compare = words only
    hits = [k for k, v in values.items()
            if v.lower() in TEMPLATE_WORDS
            or (example.get(k) and v == example[k])]
    if hits:
        slots = ", ".join(hits[:LISTEN_PORT_CAP])
        if len(hits) > LISTEN_PORT_CAP:
            slots += f" +{len(hits) - LISTEN_PORT_CAP}"
        return {"id": "sentinel", "state": "fail",
                "i18n": "sck.d.sentinel.fail",
                "detail": f"template value in: {slots}",
                "args": {"slots": slots, "count": len(hits)}}
    return {"id": "sentinel", "state": "ok",
            "i18n": "sck.d.sentinel.ok",
            "detail": "no template values found", "args": {}}


# ----------------------------------------------------------- probe: age ------
def _check_age(secrets_path: Path, now: float) -> dict:
    try:
        mtime = secrets_path.stat().st_mtime
    except OSError:
        return {"id": "age", "state": "unknown",
                "i18n": "sck.d.age.missing",
                "detail": "credential file missing", "args": {}}
    if mtime > now + FUTURE_MTIME_GRACE:
        return {"id": "age", "state": "unknown",
                "i18n": "sck.d.age.future",
                "detail": "timestamp in the future (clock?)", "args": {}}
    days = max(0, int((now - mtime) // 86400))
    if days > AGE_WARN_DAYS:
        return {"id": "age", "state": "warn",
                "i18n": "sck.d.age.warn",
                "detail": f"unchanged for {days} days — worth a review",
                "args": {"days": days}}
    return {"id": "age", "state": "ok",
            "i18n": "sck.d.age.ok",
            "detail": f"changed {days} day(s) ago",
            "args": {"days": days}}


# ---------------------------------------------------------- probe: perms -----
def _fs_class(fs: str | None) -> str:
    """Classify a filesystem name for permission enforceability (pure — the
    unit legs pin every branch without touching a real mount)."""
    if not fs:
        return "unknown"
    if fs.startswith("fuse"):
        return "unverifiable"            # fuse MAY store modes; no guarantee
    if fs in NOT_ENFORCEABLE_FS:
        return "not_enforceable"         # modes are synthesized, never stored
    return "unix"


def _fs_type_for(path: Path) -> str | None:
    """Longest-mountpoint match from /proc/mounts -> fstype (None = unreadable).
    Mount points arrive with octal escapes (\\040 etc.); decode before match."""
    try:
        text = Path("/proc/mounts").read_text(encoding="utf-8",
                                              errors="replace")
    except OSError:
        return None
    target = os.path.abspath(str(path))
    best_mp, best_fs = "", None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mp = parts[1].replace("\\040", " ").replace("\\011", "\t") \
                     .replace("\\012", "\n").replace("\\134", "\\")
        prefix = mp.rstrip("/") + "/" if mp != "/" else "/"
        if target == mp.rstrip("/") or target.startswith(prefix):
            if len(mp) > len(best_mp):
                best_mp, best_fs = mp, parts[2]
    return best_fs


def _check_perms(secrets_path: Path) -> dict:
    try:
        mode = stat.S_IMODE(secrets_path.stat().st_mode)
    except OSError:
        return {"id": "perms", "state": "unknown",
                "i18n": "sck.d.perms.missing",
                "detail": "credential file missing", "args": {}}
    mode_s = f"{mode:04o}"
    if not (mode & 0o077):
        return {"id": "perms", "state": "ok",
                "i18n": "sck.d.perms.ok",
                "detail": f"mode {mode_s} — owner-only",
                "args": {"mode": mode_s}}
    fs = _fs_type_for(secrets_path)
    cls = _fs_class(fs)
    if cls == "unix":
        return {"id": "perms", "state": "fail",
                "i18n": "sck.d.perms.fail",
                "detail": f"mode {mode_s} on {fs} — group/other can read it",
                "args": {"mode": mode_s, "fs": fs or "?"}}
    if cls == "not_enforceable":
        return {"id": "perms", "state": "unknown",
                "i18n": "sck.d.perms.na",
                "detail": f"not enforceable on {fs} (filesystem stores no "
                          f"Unix modes)",
                "args": {"fs": fs or "?"}}
    if cls == "unverifiable":
        return {"id": "perms", "state": "unknown",
                "i18n": "sck.d.perms.unver",
                "detail": f"unverifiable on {fs}",
                "args": {"fs": fs or "?"}}
    return {"id": "perms", "state": "unknown",
            "i18n": "sck.d.perms.unknown",
            "detail": "permission probe unavailable", "args": {}}


# ------------------------------------------------------------- assembler -----
def _row(cid: str, fn, *args) -> dict:
    """Run one check; any exception degrades THAT row to unknown (the row
    never disappears and the report never raises). `cid` is the stable row id
    — the exception path cannot trust a function name for it."""
    try:
        row = fn(*args)
        if not isinstance(row, dict) or "id" not in row:
            raise TypeError("bad check row")
        return row
    except Exception as e:  # noqa: BLE001 — fail-soft contract
        return {"id": cid, "state": "unknown",
                "i18n": "sck.d.unknown", "detail": type(e).__name__,
                "args": {}}


def run_checks(secrets_path: Path | str | None = None,
               example_path: Path | str | None = None,
               console_port: int | None = None,
               now: float | None = None) -> dict:
    """Run all six checks -> the report (see module docstring for contract).

    `secrets_path`/`example_path` default to the ACTIVE profile's files;
    `now` is a test seam for the age row. Never raises."""
    # path/time resolution sits OUTSIDE _row — wrap it so the fail-soft
    # contract holds for a profile resolver that raises, too: every row then
    # degrades honestly (missing file -> unknown), the report still ships.
    try:
        s_path = Path(secrets_path) if secrets_path is not None \
            else pp.secrets_path(REPO)
        e_path = Path(example_path) if example_path is not None \
            else REPO / "secrets.env.example"
        ts = float(time.time() if now is None else now)
    except Exception:  # noqa: BLE001 — fail-soft contract
        s_path = Path("/nonexistent/secrets.env")
        e_path = Path("/nonexistent/secrets.env.example")
        ts = time.time()
    checks = [
        _row("listening", _check_listening, console_port),
        _row("firewall", _check_firewall),
        _row("token", token_strength, _slot_value(s_path, "WEB_TOKEN")),
        _row("sentinel", _check_sentinel, s_path, e_path),
        _row("age", _check_age, s_path, ts),
        _row("perms", _check_perms, s_path),
    ]
    summary = {k: 0 for k in ("ok", "warn", "fail", "unknown")}
    for c in checks:
        c["state"] = c.get("state", "unknown") if \
            c.get("state") in STATE_RANK else "unknown"
        summary[c["state"]] += 1
    verdict = max((c["state"] for c in checks),
                  key=lambda s: STATE_RANK[s])
    return {"verdict": verdict, "ts": ts, "summary": summary,
            "checks": checks}


def _slot_value(path: Path, key: str) -> str:
    """Own-line non-empty slot read (glue-pit safe). The value travels only
    into the strength heuristic and never out of this frame."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(rf"(?m)^{re.escape(key)}=(\S[^\r\n]*)$", text)
    return m.group(1).strip() if m else ""


def startup_line(report: dict) -> str:
    """The one sanitized stderr line the console writes at boot: states and
    ids only — never a value, never a length, never a path."""
    states = " ".join(f"{c.get('id', '?')}={c.get('state', '?')}"
                      for c in report.get("checks", []))
    return (f"security self-check: verdict="
            f"{report.get('verdict', 'unknown')} [{states}]")
