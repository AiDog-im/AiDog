#!/usr/bin/env python3
"""The owner-facing FORGET control for a DERIVED profile item — end to end.

What this proves, on a real machine, through the REAL console endpoints
(never through a direct function call):

  * the owner-profile read face carries the PROVENANCE of every keyword —
    the admission channel ("profile") and the opaque, deterministic source
    id (mycore.memforget.source_id(CHANNEL_PROFILE, <token>)) — and
    nothing more: no excerpt, no path, no credential;
  * GET /api/profiling/forget/preview is READ-ONLY: it answers what a
    forget WOULD do (the channel, the opaque id, how many profile items it
    would silence, the surfaces it silences, the honest D10 limits) and it
    writes NOTHING; an unknown source id answers an HONEST error, never a
    silent empty;
  * POST /api/profiling/forget is Bearer-gated (401 without the token, 401
    with a wrong one) and records a TOMBSTONE only: NOTHING is deleted, the
    transcript row stays in the session file, and the item leaves the
    derived profile at once — no restart, no tick;
  * the item's score contribution and its tier/count/coverage tallies are
    recomputed without it, an unrelated item keeps its own score, and a
    RE-ANALYSIS cannot reintroduce the forgotten item;
  * repeating the forget is idempotent (already=true, one ledger row);
  * a suppression stays inside its profile: another profile's profile still
    shows the item;
  * the ledger row, the preview and every answer carry no excerpt, no file
    path and no credential;
  * the REPOSITORY's tracked files and the session transcripts are untouched
    by the whole flow.

Isolation rules honoured here:
  * the console runs from a THROWAWAY SANDBOX (a copy of lib/ + mycore/ +
    web/ + overlay/locales + VERSION) — the repository tree, the production
    profile and the production state dir are never written;
  * every profile is a throwaway one under a temp profiles root, with its
    own state root (AIDOG_MC_STATE), its own sessions and its own
    secrets.env — no production secret is read or written;
  * the network guard (sitecustomize.py in the sandbox) allows loopback
    only and records every other attempt — the run fails if anything
    reaches out;
  * no product file is modified by this test; the only new file is this
    one.

Run:  python3 tests/profiling_forget_api.py     (exit 0 = all green)
English output per repo i18n policy.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
RESERVED_PORTS = {19167, 3460}          # the production console + shared proxy

# Fixture credential (throwaway, profile-local, never printed).
TOKEN = "E2E-PROFILE-FORGET-TOKEN-BETA"

PROFILE_A = "profalpha"
PROFILE_B = "profbeta"

# The owner's own words (the ONLY input the profile is derived from). The
# FORGOTTEN token is "chain"; the SURVIVOR is "wallet" — an unrelated item
# that must keep its own score.
LEGACY_A = "public chain wallet contract"
LEGACY_B = "public chain wallet contract deadline"
LEGACY_C = "release version bump console"
FORGOTTEN_TOKEN = "chain"
SURVIVOR_TOKEN = "wallet"

PASSED = 0
FAILED = 0
NOTES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """One named check. The detail is printed only when the check FAILS."""
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED += 1
        print(f"FAIL  {name}   [{detail}]")
    return ok


def note(text: str) -> None:
    """An observation that is not a pass/fail check (reported to the owner)."""
    NOTES.append(text)
    print(f"NOTE  {text}")


# ------------------------------------------------------------- utilities --
def free_port() -> int:
    """An ephemeral loopback port that is not a reserved production port."""
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((HOST, 0))
            port = sock.getsockname()[1]
        if port not in RESERVED_PORTS:
            return port
    raise RuntimeError("no free loopback port")


def sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def api(base: str, method: str, path: str, body=None,
        token: str | None = None, timeout: int = 30) -> tuple[int, object, str]:
    """One loopback HTTP call. Returns (status, parsed_json_or_None, raw)."""
    req = urllib.request.Request(base + path, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as res:
            raw = res.read()
            status = res.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    return status, parsed, text


def git_porcelain() -> str:
    """The repository's tracked-file state (`git status --porcelain`).

    Empty when git is unavailable or this is not a work tree — the caller
    then reports the check as SKIPPED rather than pretending it passed."""
    try:
        proc = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


# -------------------------------------------------------- the sandbox tree --
def build_sandbox(root: Path) -> Path:
    """A throwaway copy of the product tree the console runs from.

    Copying keeps the repository byte-identical: the console's boot record
    (web/.state.json), its settings and any runtime file land in the
    sandbox. The copy is of the REAL source — the console under test is the
    shipped web/server.py, the shipped mycore/* and the shipped lib/*."""
    sandbox = root / "sandbox"
    (sandbox / "lib").mkdir(parents=True)
    (sandbox / "mycore").mkdir(parents=True)
    (sandbox / "web").mkdir(parents=True)
    (sandbox / "overlay").mkdir(parents=True)
    for file in (ROOT / "lib").glob("*.py"):
        shutil.copyfile(file, sandbox / "lib" / file.name)
    for file in (ROOT / "mycore").glob("*.py"):
        shutil.copyfile(file, sandbox / "mycore" / file.name)
    shutil.copyfile(ROOT / "web" / "server.py", sandbox / "web" / "server.py")
    shutil.copyfile(ROOT / "web" / "settings.example.json",
                    sandbox / "web" / "settings.example.json")
    shutil.copytree(ROOT / "web" / "static", sandbox / "web" / "static")
    shutil.copytree(ROOT / "overlay" / "locales", sandbox / "overlay" / "locales")
    shutil.copyfile(ROOT / "VERSION", sandbox / "VERSION")
    # The network guard: loopback only, every other attempt is refused AND
    # recorded. Installed through sitecustomize so it runs before the console
    # imports anything.
    (sandbox / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    return sandbox


SITECUSTOMIZE = '''"""Loopback-only guard for the isolated console (test seam, not product)."""
import os
import sys

_LOG = os.environ.get("AIDOG_TEST_NETLOG", "")


def _record(event: str, detail: str) -> None:
    if not _LOG:
        return
    with open(_LOG, "a", encoding="utf-8") as handle:
        handle.write(f"{event} {detail}\\n")


def _hook(event, args):
    if event == "socket.connect":
        addr = args[1]
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        if str(host) not in ("127.0.0.1", "::1", "localhost"):
            _record("connect", str(host))
            raise PermissionError("blocked non-loopback connect")
    elif event == "socket.getaddrinfo":
        host = str(args[0] or "")
        if host not in ("127.0.0.1", "::1", "localhost", ""):
            _record("dns", host)
            raise PermissionError("blocked non-loopback name lookup")
    elif event == "socket.sendto":
        _record("sendto", "")
        raise PermissionError("blocked udp send")
    elif event in ("socket.gethostbyname", "socket.gethostbyaddr"):
        if str(args[0]) not in ("127.0.0.1", "::1", "localhost"):
            _record("dns-legacy", str(args[0]))


sys.addaudithook(_hook)
'''


def transcript_body() -> str:
    """The owner's own messages as the session transcript stores them."""
    now = 1_800_000_000.0
    day = 86400.0
    rows = [
        {"role": "user", "content": LEGACY_A, "ts": now - 2 * day},
        {"role": "user", "content": LEGACY_B, "ts": now - 2 * day},
        {"role": "user", "content": LEGACY_C, "ts": now - 5 * day},
    ]
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def make_profile(profiles: Path, name: str, token: str) -> Path:
    """A throwaway profile: own state (with its own transcripts), own
    memory slots, own secrets."""
    profile = profiles / name
    (profile / "body" / "memory").mkdir(parents=True)
    (profile / "body" / "evolution").mkdir(parents=True)
    (profile / "state" / "sessions").mkdir(parents=True)
    (profile / "state" / "sessions" / "a.jsonl").write_text(
        transcript_body(), encoding="utf-8")
    secrets = profile / "secrets.env"
    secrets.write_text(f"WEB_TOKEN={token}\n", encoding="utf-8")
    secrets.chmod(0o600)
    settings = json.loads((ROOT / "web" / "settings.example.json")
                          .read_text(encoding="utf-8"))
    # No outbound endpoint may be reachable from an isolated run.
    settings["ledger"] = {"enabled": False}
    settings["usage"] = {"metrics_url": "", "pricing_config": "", "log_dir": ""}
    settings["evolution"] = {"enabled": False}
    (profile / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return profile


class Console:
    """A real console process (python3 server.py) on its own port/profile."""

    def __init__(self, sandbox: Path, profiles: Path, name: str, token: str,
                 netlog: Path):
        self.sandbox = sandbox
        self.profiles = profiles
        self.name = name
        self.token = token
        self.netlog = netlog
        self.profile = profiles / name
        self.port = free_port()
        self.base = f"http://{HOST}:{self.port}"
        self.proc: subprocess.Popen | None = None
        self.stderr = ""
        self.stdout = ""
        self._threads: list[threading.Thread] = []

    def start(self, timeout: float = 90.0) -> bool:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.profile.parent),
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(self.sandbox),
            "AIDOG_TEST_NETLOG": str(self.netlog),
            "AIDOG_PROFILES_DIR": str(self.profiles),
            "AIDOG_PROFILE": self.name,
            "AIDOG_MC_STATE": str(self.profile / "state"),
            "AIDOG_WEB_PORT": str(self.port),
        }
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=str(self.sandbox / "web"),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for stream, sink in ((self.proc.stdout, "stdout"),
                             (self.proc.stderr, "stderr")):
            thread = threading.Thread(target=self._drain, args=(stream, sink),
                                      daemon=True)
            thread.start()
            self._threads.append(thread)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                status, _, _ = api(self.base, "GET", "/api/health", timeout=3)
                if status == 200:
                    return True
            except OSError:
                pass
            time.sleep(0.25)
        return False

    def _drain(self, stream, sink: str) -> None:
        try:
            for chunk in iter(lambda: stream.read(4096), b""):
                text = chunk.decode("utf-8", "replace")
                if sink == "stderr":
                    self.stderr += text
                else:
                    self.stdout += text
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        for thread in self._threads:
            thread.join(2)

    # -- the console's own API ------------------------------------------------
    def profile_view(self):
        return api(self.base, "GET", "/api/profiling")

    def analyze(self, token: str | None = None):
        return api(self.base, "POST", "/api/profiling",
                   body={"action": "analyze"},
                   token=self.token if token is None else token)

    def preview(self, source_id: str):
        return api(self.base, "GET", "/api/profiling/forget/preview?source="
                   + urllib.parse.quote(source_id))

    def forget(self, source_id: str, token: str | None = None):
        return api(self.base, "POST", "/api/profiling/forget",
                   body={"source_id": source_id},
                   token=self.token if token is None else token)


def keywords(payload) -> dict:
    """The keyword rows of the read model, keyed by token."""
    out: dict = {}
    for row in (payload or {}).get("keywords", []) or []:
        if isinstance(row, dict):
            out[str(row.get("token") or "")] = row
    return out


def ledger_rows(state: Path) -> list[dict]:
    """The tombstone ledger rows of one profile state root (real module)."""
    sys.path.insert(0, str(ROOT))
    try:
        from mycore import memforget
        return memforget.load_rows(state=state)
    finally:
        if sys.path[0] == str(ROOT):
            sys.path.pop(0)


def source_id_for(profile_name: str, token: str) -> str:
    """The opaque id the ledger derives for one derived item (real module).

    The profile scope is part of the id (D2), so the id is derived under
    the SAME profile the console runs as."""
    sys.path.insert(0, str(ROOT))
    saved = os.environ.get("AIDOG_PROFILE")
    os.environ["AIDOG_PROFILE"] = profile_name
    try:
        from mycore import memforget
        return memforget.source_id(memforget.CHANNEL_PROFILE, token)
    finally:
        if saved is None:
            os.environ.pop("AIDOG_PROFILE", None)
        else:
            os.environ["AIDOG_PROFILE"] = saved
        if sys.path[0] == str(ROOT):
            sys.path.pop(0)


# ------------------------------------------------------------------ main ---
def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="aidog-profile-forget-"))
    netlog = root / "network.log"
    consoles: list[Console] = []
    prod_state = ROOT / "data" / "mycore_service"
    prod_before = {
        "profile": sha256(prod_state / "profiling" / "profile.json"),
        "ledger": sha256(prod_state / "memforget" / "tombstones.jsonl"),
    }
    git_before = git_porcelain()
    try:
        sandbox = build_sandbox(root)
        profiles = root / "profiles"
        profiles.mkdir()
        make_profile(profiles, PROFILE_A, TOKEN)
        make_profile(profiles, PROFILE_B, TOKEN)
        state_a = profiles / PROFILE_A / "state"
        state_b = profiles / PROFILE_B / "state"
        transcript_a = state_a / "sessions" / "a.jsonl"
        transcript_before = transcript_a.read_bytes()

        # ---------------------------------------------------- boot + auth ---
        console_a = Console(sandbox, profiles, PROFILE_A, TOKEN, netlog)
        consoles.append(console_a)
        check("A1 the isolated console boots on its own loopback port",
              console_a.start(), "boot failed")
        if not console_a.proc or console_a.proc.poll() is not None:
            print(console_a.stderr[-2000:])
            return 1
        console_b = Console(sandbox, profiles, PROFILE_B, TOKEN, netlog)
        consoles.append(console_b)
        check("A2 a second profile's console boots on its own port",
              console_b.start(), "boot failed")

        # ---------------------------------------------- the analysis run ----
        status, body, _ = console_a.analyze()
        check("B1 POST /api/profiling analyze answers 200 with a brief report",
              status == 200 and isinstance(body, dict)
              and body.get("ok") is True, f"status={status} body={body}")
        status, view, raw_view = console_a.profile_view()
        rows = keywords(view)
        check("B2 GET /api/profiling answers 200 without a Bearer token",
              status == 200 and isinstance(view, dict), f"status={status}")
        check("B3 the read face derives the owner's keywords",
              FORGOTTEN_TOKEN in rows and SURVIVOR_TOKEN in rows,
              str(sorted(rows)))
        check("B4 every keyword carries its admission channel",
              rows and all(r.get("channel") == "profile"
                           for r in rows.values()),
              str(sorted(rows))[:200])
        check("B5 every keyword carries its opaque source id",
              rows and all(str(r.get("source_id") or "").startswith("src_")
                           for r in rows.values()), str(sorted(rows))[:200])
        check("B6 the source id is the deterministic id of the token",
              rows[FORGOTTEN_TOKEN].get("source_id")
              == source_id_for(PROFILE_A, FORGOTTEN_TOKEN),
              str(rows.get(FORGOTTEN_TOKEN))[:200])
        check("B7 the read face adds no other field to a keyword row",
              all({"token", "score", "tier", "coverage", "trait", "excerpts"}
                  <= set(r) for r in rows.values()),
              str(sorted(rows[FORGOTTEN_TOKEN]))[:200])
        survivor_score = float(rows[SURVIVOR_TOKEN].get("score") or 0.0)
        counts_before = (view.get("counts") or {})
        check("B8 the read face carries the tallies",
              isinstance(counts_before, dict) and counts_before,
              str(counts_before)[:160])

        # ------------------------------------------------- the preview (read) --
        source_id = rows[FORGOTTEN_TOKEN]["source_id"]
        status, preview, raw_preview = console_a.preview(source_id)
        check("C1 GET /api/profiling/forget/preview answers 200 without a "
              "Bearer token",
              status == 200 and isinstance(preview, dict), f"status={status}")
        check("C2 the preview carries exactly the contracted keys",
              set(preview) == {"source_id", "channel", "items", "affects",
                               "limits"}, str(sorted(preview)))
        check("C3 the preview names the source id, the channel and the items "
              "it would silence",
              preview.get("source_id") == source_id
              and preview.get("channel") == "profile"
              and preview.get("items") == 1, str(preview)[:200])
        check("C4 the preview names the surfaces the forget silences",
              preview.get("affects") == {"profile": True, "score": True,
                                         "coverage": True},
              str(preview.get("affects"))[:200])
        check("C5 the preview carries the honest D10 limits",
              "NOT deleted" in str(preview.get("limits"))
              and "transcript" in str(preview.get("limits")),
              str(preview.get("limits"))[:160])
        check("C6 the preview carries no excerpt, no file path and no "
              "credential",
              LEGACY_A not in raw_preview and LEGACY_C not in raw_preview
              and FORGOTTEN_TOKEN not in raw_preview
              and str(profiles) not in raw_preview
              and "sessions" not in raw_preview
              and TOKEN not in raw_preview, raw_preview[:200])
        check("C7 the preview wrote nothing (it is read-only)",
              not (state_a / "memforget").exists(),
              str(sorted(p.name for p in state_a.iterdir())))
        status, body, _ = console_a.preview("src_" + "0" * 32)
        check("C8 an unknown source id answers an honest error",
              status == 404 and isinstance(body, dict) and body.get("error"),
              f"status={status} body={body}")
        status, body, _ = console_a.preview("not-a-source-id")
        check("C9 a malformed source id answers an honest error",
              status == 400 and isinstance(body, dict) and body.get("error"),
              f"status={status} body={body}")

        # ------------------------------------------------ the Bearer gate --
        status, body, _ = api(console_a.base, "POST", "/api/profiling/forget",
                              body={"source_id": source_id})
        check("E1 POST /api/profiling/forget without a Bearer token -> 401",
              status == 401, f"status={status}")
        status, body, _ = console_a.forget(source_id, token="wrong-token-value")
        check("E2 POST /api/profiling/forget with a wrong Bearer token -> 401",
              status == 401, f"status={status}")
        check("E3 the refused write recorded nothing",
              not (state_a / "memforget").exists(),
              str(sorted(p.name for p in state_a.iterdir())))

        # ------------------------------------------------------ the forget --
        status, body, raw = console_a.forget(source_id)
        check("F1 POST /api/profiling/forget with the Bearer token answers ok",
              status == 200 and isinstance(body, dict)
              and body.get("ok") is True, f"status={status} body={body}")
        check("F2 the answer carries the opaque id, the channel, the already "
              "flag and the limits",
              body.get("id") == source_id and body.get("channel") == "profile"
              and body.get("already") is False
              and "NOT deleted" in str(body.get("limits")), str(body)[:200])
        check("F2b the answer carries exactly the contracted keys",
              set(body) == {"ok", "id", "channel", "already", "limits"},
              str(sorted(body)))
        check("F3 the answer holds no excerpt, no file path and no credential",
              LEGACY_A not in raw and LEGACY_C not in raw
              and FORGOTTEN_TOKEN not in raw
              and str(profiles) not in raw and "sessions" not in raw
              and TOKEN not in raw, raw[:200])
        rows_ledger = ledger_rows(state_a)
        check("F4 the ledger holds exactly one row, in the profile channel",
              len(rows_ledger) == 1
              and rows_ledger[0].get("id") == source_id
              and rows_ledger[0].get("channel") == "profile",
              str(rows_ledger)[:200])
        ledger_raw = (state_a / "memforget" / "tombstones.jsonl").read_text(
            encoding="utf-8")
        check("F5 the ledger row holds no excerpt, no path and no credential",
              LEGACY_A not in ledger_raw and LEGACY_C not in ledger_raw
              and FORGOTTEN_TOKEN not in ledger_raw
              and str(profiles) not in ledger_raw
              and "sessions" not in ledger_raw and TOKEN not in ledger_raw,
              ledger_raw[:200])
        check("F6 NOTHING was deleted — the transcript row is still there",
              transcript_a.read_bytes() == transcript_before,
              "the session transcript changed")

        # ------------------------------- the read face reacts, no restart --
        status, view2, _ = console_a.profile_view()
        rows2 = keywords(view2)
        check("G1 the forgotten item left the read face at once",
              status == 200 and FORGOTTEN_TOKEN not in rows2
              and SURVIVOR_TOKEN in rows2, str(sorted(rows2)))
        check("G2 the surviving item kept its own score",
              abs(float(rows2[SURVIVOR_TOKEN].get("score") or 0.0)
                  - survivor_score) < 1e-9,
              f'{survivor_score} -> {rows2[SURVIVOR_TOKEN].get("score")}')
        counts_after = (view2.get("counts") or {})
        check("G3 the tier/count/coverage tallies are recomputed without it",
              sum((counts_after.get("coverage") or {}).values())
              == len(view2.get("keywords") or [])
              and sum((counts_after.get("tiers") or {}).values())
              == len(view2.get("keywords") or [])
              and sum((counts_after.get("coverage") or {}).values())
              < sum((counts_before.get("coverage") or {}).values()),
              str(counts_after)[:200])
        check("G4 the surviving rows still carry their provenance",
              all(str(r.get("source_id") or "").startswith("src_")
                  and r.get("channel") == "profile"
                  for r in rows2.values()), str(sorted(rows2))[:200])

        # --------------------------------------- a rebuild cannot restore --
        status, body, _ = console_a.analyze()
        check("H1 the re-analysis answers ok",
              status == 200 and isinstance(body, dict)
              and body.get("ok") is True, f"status={status} body={body}")
        status, view3, _ = console_a.profile_view()
        rows3 = keywords(view3)
        check("H2 a re-analysis does not reintroduce the forgotten item",
              FORGOTTEN_TOKEN not in rows3 and SURVIVOR_TOKEN in rows3,
              str(sorted(rows3)))
        check("H3 the surviving item still has its score after the rebuild",
              abs(float(rows3[SURVIVOR_TOKEN].get("score") or 0.0)
                  - survivor_score) < 1e-9,
              f'{survivor_score} -> {rows3[SURVIVOR_TOKEN].get("score")}')

        # -------------------------------------------- idempotence + a second --
        status, body, _ = console_a.forget(source_id)
        check("I1 repeating the forget is a no-op that reports already=true",
              status == 200 and body.get("already") is True
              and body.get("id") == source_id, str(body)[:200])
        check("I2 the repeated forget left exactly one ledger row",
              len(ledger_rows(state_a)) == 1, str(ledger_rows(state_a))[:200])

        # ---------------------------------------------- profile isolation --
        status, body, _ = console_b.analyze()
        check("J1 the second profile analysed its own corpus",
              status == 200 and body.get("ok") is True, str(body)[:200])
        status, view_b, _ = console_b.profile_view()
        rows_b = keywords(view_b)
        check("J2 a suppression stays inside its profile",
              FORGOTTEN_TOKEN in rows_b and SURVIVOR_TOKEN in rows_b,
              str(sorted(rows_b)))
        check("J3 the other profile's items have their own source ids",
              rows_b[FORGOTTEN_TOKEN].get("source_id")
              != rows[FORGOTTEN_TOKEN].get("source_id"),
              str(rows_b.get(FORGOTTEN_TOKEN))[:200])
        status, body, _ = console_b.forget(rows_b[FORGOTTEN_TOKEN]["source_id"])
        check("J4 the other profile can forget its own item",
              status == 200 and body.get("ok") is True
              and body.get("already") is False, str(body)[:200])
        status, view_b2, _ = console_b.profile_view()
        check("J5 the other profile's item left ITS profile only",
              FORGOTTEN_TOKEN not in keywords(view_b2)
              and SURVIVOR_TOKEN in keywords(view_b2),
              str(sorted(keywords(view_b2))))
        status, view_a2, _ = console_a.profile_view()
        check("J6 the first profile is unaffected by the second profile's "
              "forget",
              FORGOTTEN_TOKEN not in keywords(view_a2)
              and SURVIVOR_TOKEN in keywords(view_a2),
              str(sorted(keywords(view_a2))))

        # ------------------------------------------------------- hygiene -----
        check("K1 no fixture excerpt reached the console stderr",
              LEGACY_A not in console_a.stderr and LEGACY_C not in console_a.stderr
              and LEGACY_A not in console_b.stderr, "stderr leak")
        check("K2 no fixture credential reached the console stderr",
              TOKEN not in console_a.stderr and TOKEN not in console_b.stderr,
              "stderr leak")
        violations = ""
        if netlog.exists():
            violations = netlog.read_text(encoding="utf-8")
        check("K3 the loopback-only guard recorded no outbound attempt",
              violations.strip() == "", f"violations={violations.strip()[:200]}")
        prod_after = {
            "profile": sha256(prod_state / "profiling" / "profile.json"),
            "ledger": sha256(prod_state / "memforget" / "tombstones.jsonl"),
        }
        check("K4 the production profile and its ledger are unchanged",
              prod_after == prod_before, "the production state dir was written")
        check("K5 the session transcripts are byte-identical",
              transcript_a.read_bytes() == transcript_before
              and (state_b / "sessions" / "a.jsonl").read_bytes()
              == transcript_before, "a session transcript changed")
        git_after = git_porcelain()
        if git_before or git_after:
            check("K6 the repository's tracked files are unchanged by this run",
                  git_after == git_before,
                  "the working tree changed during the run")
        else:
            note("K6 the repository's tracked files: SKIPPED (no git work tree "
                 "here) — the transcripts and the production state are "
                 "byte-identical instead")
    finally:
        for console in consoles:
            console.stop()
        shutil.rmtree(root, ignore_errors=True)

    print("")
    print(f"{PASSED}/{PASSED + FAILED} PASS")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
