#!/usr/bin/env python3
"""The owner-facing FORGET control — the API half, end to end over HTTP.

What this proves, on a real machine, through the REAL console endpoints
(never through a direct function call):

  * the read-only memory view carries the PROVENANCE of every entry — the
    admission channel (curated:memory | curated:user | learned) and the
    opaque, deterministic source id (mycore.memforget.source_id) of the
    text the entry owns — and nothing more: no path, no credential;
  * GET /api/memory/forget/preview is READ-ONLY: it answers what a forget
    WOULD do (the channel, the opaque id, the number of sections it would
    filter, the surfaces it silences, the honest D10 limits) and it writes
    nothing; it never carries the entry text, a file path or a credential;
  * an unknown slot or an unknown title answers an HONEST error — never a
    silent empty;
  * POST /api/memory/forget is Bearer-gated (401 without the token) and
    records a TOMBSTONE only: NOTHING is deleted, the source stays in its
    file, and the read paths drop it from the next request on — no
    restart, no reload;
  * repeating the forget is idempotent (already=true, one ledger row), a
    second source keeps working, and the learned slot is addressable too;
  * the PROFILE's own slots are addressed — the repository's curated memory
    and the production state root are never written.

Isolation rules honoured here:
  * the console runs from a THROWAWAY SANDBOX (a copy of lib/ + mycore/ +
    web/ + overlay/locales + VERSION) — the repository tree, the production
    profile, the production state dir and the repo's curated memory are
    never written;
  * every profile is a throwaway one under a temp profiles root, with its
    own state root (AIDOG_MC_STATE), its own body/memory slots and its own
    secrets.env — no production secrets.env is read, no production secret
    is written;
  * the network guard (sitecustomize.py in the sandbox) allows loopback
    only and records every other attempt — the run fails if anything
    reaches out;
  * no product file is modified by this test; the only new file is this one.

Run:  python3 tests/memory_forget_api.py     (exit 0 = all green)
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
TOKEN = "E2E-FORGET-API-TOKEN-ALPHA"

PROFILE = "forgetapi"

# The two curated entries of the throwaway profile. FACTS is the one that
# gets forgotten; NOTES is the control that must survive.
FACTS_TITLE = "Facts"
FACTS_TEXT = "the owner prefers local tools"
NOTES_TITLE = "Notes"
NOTES_TEXT = "second entry about the console"
USER_TITLE = "Profile"
USER_TEXT = "writes in Chinese"
LEARNED_TITLE = "2026-01-02 (learn)"
LEARNED_TEXT = "the deploy pipeline runs on fridays"

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
        token: str | None = None, timeout: int = 20) -> tuple[int, object, str]:
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


def preview_url(slot: str, title: str) -> str:
    return ("/api/memory/forget/preview?slot=" + urllib.parse.quote(slot)
            + "&title=" + urllib.parse.quote(title))


# -------------------------------------------------------- the sandbox tree --
def build_sandbox(root: Path) -> Path:
    """A throwaway copy of the product tree the console runs from.

    Copying keeps the repository byte-identical: the console's boot record
    (web/.state.json), its settings and any runtime file land in the sandbox.
    The copy is of the REAL source — the console under test is the shipped
    web/server.py, the shipped mycore/* and the shipped lib/*."""
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


def make_profile(profiles: Path, name: str, token: str) -> Path:
    """A throwaway profile: own body/memory slots, own state, own secrets."""
    profile = profiles / name
    (profile / "body" / "memory").mkdir(parents=True)
    (profile / "body" / "evolution").mkdir(parents=True)
    (profile / "state").mkdir(parents=True)
    (profile / "body" / "memory" / "MEMORY_curated.md").write_text(
        f"# {FACTS_TITLE}\n\n{FACTS_TEXT}\n\n# {NOTES_TITLE}\n\n{NOTES_TEXT}\n",
        encoding="utf-8")
    (profile / "body" / "memory" / "USER_curated.md").write_text(
        f"# {USER_TITLE}\n\n{USER_TEXT}\n", encoding="utf-8")
    (profile / "body" / "memory" / "EVOLUTION_learned.md").write_text(
        f"# LEARNED MEMORY\n\n## {LEARNED_TITLE}\n\n- {LEARNED_TEXT}\n\n",
        encoding="utf-8")
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

    def start(self, timeout: float = 60.0) -> bool:
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
        for stream, sink in ((self.proc.stdout, "stdout"), (self.proc.stderr, "stderr")):
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
    def view(self, q: str = ""):
        return api(self.base, "GET", "/api/memory" + (f"?q={q}" if q else ""))

    def preview(self, slot: str, title: str):
        return api(self.base, "GET", preview_url(slot, title))

    def forget(self, slot: str, title: str, token: str | None = None):
        _, preview, _ = self.preview(slot, title)
        return api(self.base, "POST", "/api/memory/forget",
                   body={"slot": slot, "title": title,
                         "source_id": preview.get("source_id"),
                         "revision": preview.get("revision")},
                   token=self.token if token is None else token)


def view_entries(payload) -> list[dict]:
    """Every entry of the memory view, flattened."""
    out: list[dict] = []
    for slot in (payload or {}).get("slots", []) or []:
        for entry in slot.get("entries", []) or []:
            row = dict(entry)
            row["slot"] = slot.get("slot")
            out.append(row)
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


# ------------------------------------------------------------------ main ---
def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="aidog-forget-api-"))
    netlog = root / "network.log"
    consoles: list[Console] = []
    repo_memory = ROOT / "overlay" / "memory"
    prod_before = {name: sha256(repo_memory / name)
                   for name in ("MEMORY_curated.md", "USER_curated.md")}
    default_ledger = (ROOT / "data" / "mycore_service" / "memforget"
                      / "tombstones.jsonl")
    default_ledger_before = sha256(default_ledger)
    repo_state_before = sha256(ROOT / "web" / ".state.json")
    try:
        sandbox = build_sandbox(root)
        profiles = root / "profiles"
        profiles.mkdir()
        make_profile(profiles, PROFILE, TOKEN)
        state = profiles / PROFILE / "state"
        memory_file = profiles / PROFILE / "body" / "memory" / "MEMORY_curated.md"
        memory_before = memory_file.read_bytes()

        # ---------------------------------------------------- boot + auth ---
        console = Console(sandbox, profiles, PROFILE, TOKEN, netlog)
        consoles.append(console)
        check("A1 the isolated console boots on its own loopback port",
              console.start(), "boot failed")
        if not console.proc or console.proc.poll() is not None:
            print(console.stderr[-2000:])
            return 1

        # ------------------------------------------------- the memory view --
        status, view, raw_view = console.view()
        entries = view_entries(view)
        check("B1 GET /api/memory answers 200 without a Bearer token",
              status == 200 and isinstance(view, dict), f"status={status}")
        check("B2 the view carries the admission channel of every entry",
              entries and all(e.get("channel") in
                              ("curated:memory", "curated:user")
                              for e in entries), str(entries)[:200])
        check("B3 the view carries the opaque source id of every entry",
              entries and all(str(e.get("source_id") or "").startswith("src_")
                              for e in entries), str(entries)[:200])
        facts = [e for e in entries if e.get("title") == FACTS_TITLE]
        check("B4 the view lists the entry that is about to be forgotten",
              len(facts) == 1 and facts[0].get("slot") == "memory",
              str(entries)[:200])

        # ------------------------------------------------- the preview (read) --
        status, preview, raw_preview = console.preview("memory", FACTS_TITLE)
        check("C1 GET /api/memory/forget/preview answers 200 without a "
              "Bearer token",
              status == 200 and isinstance(preview, dict), f"status={status}")
        check("C2 the preview names the slot, the heading, the channel and the "
              "opaque id",
              preview.get("slot") == "memory"
              and preview.get("title") == FACTS_TITLE
              and preview.get("channel") == "curated:memory"
              and preview.get("source_id") == facts[0].get("source_id"),
              str(preview)[:200])
        check("C3 the preview counts the sections a forget would filter",
              preview.get("entries") == 1, str(preview)[:200])
        check("C4 the preview names the surfaces the forget silences",
              preview.get("affects") == {"injection": True,
                                         "memory_view": True,
                                         "profile_coverage": True},
              str(preview.get("affects"))[:200])
        check("C5 the preview carries the honest D10 limits",
              "NOT deleted" in str(preview.get("limits"))
              and "transcript" in str(preview.get("limits")),
              str(preview.get("limits"))[:160])
        check("C5b the preview carries exactly the contracted keys",
              set(preview) == {"slot", "title", "channel", "source_id",
                               "revision", "entries", "affects", "limits"},
              str(sorted(preview)))
        check("C6 the preview carries no excerpt, no file path and no "
              "credential",
              FACTS_TEXT not in raw_preview and NOTES_TEXT not in raw_preview
              and str(profiles) not in raw_preview
              and "MEMORY_curated.md" not in raw_preview
              and TOKEN not in raw_preview, raw_preview[:200])
        check("C7 the preview wrote nothing (it is read-only)",
              not (state / "memforget").exists(),
              str(sorted(p.name for p in state.iterdir())))

        # ------------------------------------------------- the honest errors --
        status, body, _ = console.preview("nope", FACTS_TITLE)
        check("D1 an unknown slot answers an honest error",
              status == 400 and isinstance(body, dict)
              and "unknown memory slot" in str(body.get("error")),
              f"status={status} body={body}")
        status, body, _ = console.preview("memory", "No Such Heading")
        check("D2 an unknown title answers an honest error, never a silent "
              "empty",
              status == 404 and isinstance(body, dict) and body.get("error"),
              f"status={status} body={body}")

        # ------------------------------------------------ the Bearer gate --
        status, body, _ = api(console.base, "POST", "/api/memory/forget",
                              body={"slot": "memory", "title": FACTS_TITLE})
        check("E1 POST /api/memory/forget without a Bearer token -> 401",
              status == 401, f"status={status}")
        status, body, _ = console.forget("memory", FACTS_TITLE,
                                         token="wrong-token-value")
        check("E2 POST /api/memory/forget with a wrong Bearer token -> 401",
              status == 401, f"status={status}")
        check("E3 the refused write recorded nothing",
              not (state / "memforget").exists(),
              str(sorted(p.name for p in state.iterdir())))

        # ------------------------------------------------------ the forget --
        status, body, raw = console.forget("memory", FACTS_TITLE)
        check("F1 POST /api/memory/forget with the Bearer token answers ok",
              status == 200 and isinstance(body, dict) and body.get("ok") is True,
              f"status={status} body={body}")
        check("F2 the answer carries the opaque id, the channel, the already "
              "flag and the limits",
              body.get("id") == facts[0].get("source_id")
              and body.get("channel") == "curated:memory"
              and body.get("already") is False
              and "NOT deleted" in str(body.get("limits")), str(body)[:200])
        check("F2b the answer carries exactly the contracted keys",
              set(body) == {"ok", "id", "channel", "already", "limits"},
              str(sorted(body)))
        check("F3 the answer holds no excerpt, no file path and no credential",
              FACTS_TEXT not in raw and NOTES_TEXT not in raw
              and str(profiles) not in raw
              and "MEMORY_curated.md" not in raw and TOKEN not in raw,
              raw[:200])
        check("F4 NOTHING was deleted — the entry is still in its file",
              memory_file.read_bytes() == memory_before,
              memory_file.read_text()[:160])
        rows = ledger_rows(state)
        check("F5 the ledger holds exactly one row, with the id and the "
              "channel",
              len(rows) == 1 and rows[0].get("id") == facts[0].get("source_id")
              and rows[0].get("channel") == "curated:memory", str(rows)[:200])
        ledger_raw = (state / "memforget" / "tombstones.jsonl").read_text(
            encoding="utf-8")
        check("F6 the ledger row holds no excerpt, no path and no credential",
              FACTS_TEXT not in ledger_raw and NOTES_TEXT not in ledger_raw
              and str(profiles) not in ledger_raw
              and "MEMORY_curated.md" not in ledger_raw and TOKEN not in ledger_raw,
              ledger_raw[:200])

        # ------------------------------- the read paths react, no restart --
        status, view2, _ = console.view()
        titles = [e.get("title") for e in view_entries(view2)]
        check("G1 the forgotten entry left the memory view at once",
              status == 200 and FACTS_TITLE not in titles
              and NOTES_TITLE in titles and USER_TITLE in titles, str(titles))
        check("G2 the view's surviving entries still carry their provenance",
              all(str(e.get("source_id") or "").startswith("src_")
                  for e in view_entries(view2)), str(view_entries(view2))[:200])

        # -------------------------------------------- idempotence + a second --
        status, body, _ = console.forget("memory", FACTS_TITLE)
        check("H1 repeating the forget is a no-op that reports already=true",
              status == 200 and body.get("already") is True
              and body.get("id") == facts[0].get("source_id"), str(body)[:200])
        check("H2 the repeated forget left exactly one ledger row",
              len(ledger_rows(state)) == 1, str(ledger_rows(state))[:200])
        status, body, _ = console.forget("memory", NOTES_TITLE)
        titles = [e.get("title") for e in view_entries(console.view()[1])]
        check("H3 a second source keeps working",
              status == 200 and body.get("ok") is True
              and NOTES_TITLE not in titles and USER_TITLE in titles
              and len(ledger_rows(state)) == 2, str(titles))

        # ------------------------------------------------ the learned slot --
        status, body, _ = console.preview("learned", LEARNED_TITLE)
        check("I1 the learned slot is addressable and reports the surfaces it "
              "touches",
              status == 200 and body.get("channel") == "learned"
              and body.get("affects") == {"injection": True,
                                          "memory_view": False,
                                          "profile_coverage": False},
              str(body)[:200])
        status, body, _ = console.forget("learned", LEARNED_TITLE)
        check("I2 the learned entry is forgotten too",
              status == 200 and body.get("ok") is True
              and body.get("channel") == "learned", str(body)[:200])
        check("I3 the learned forget left the ledger well-formed",
              len(ledger_rows(state)) == 3
              and all(r.get("id") for r in ledger_rows(state)),
              str(ledger_rows(state))[:200])

        # ------------------------------------------------------- hygiene -----
        check("J1 no fixture excerpt reached the console stderr",
              FACTS_TEXT not in console.stderr and NOTES_TEXT not in console.stderr
              and LEARNED_TEXT not in console.stderr, "stderr leak")
        check("J2 no fixture credential reached the console stderr",
              TOKEN not in console.stderr, "stderr leak")
        violations = ""
        if netlog.exists():
            violations = netlog.read_text(encoding="utf-8")
        check("J3 the loopback-only guard recorded no outbound attempt",
              violations.strip() == "", f"violations={violations.strip()[:200]}")
        prod_after = {name: sha256(repo_memory / name)
                      for name in ("MEMORY_curated.md", "USER_curated.md")}
        check("J4 the repository's curated memory is byte-identical",
              prod_after == prod_before, "the repo's curated memory changed")
        check("J5 the production profile's tombstone ledger is unchanged",
              sha256(default_ledger) == default_ledger_before,
              "the production state dir was written")
        check("J6 the repository's console boot record was not written by this "
              "run",
              sha256(ROOT / "web" / ".state.json") == repo_state_before,
              "the repository's web/ was written")
    finally:
        for console in consoles:
            console.stop()
        shutil.rmtree(root, ignore_errors=True)

    print("")
    print(f"{PASSED}/{PASSED + FAILED} PASS")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
