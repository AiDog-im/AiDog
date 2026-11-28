#!/usr/bin/env python3
"""The owner-facing FORGET control for a TRANSCRIPT MESSAGE — end to end.

What this proves, on a real machine, through the REAL console endpoints
(never through a direct function call):

  * the session search face names every hit with the opaque, deterministic
    source id of its message (the `session` admission channel) and nothing
    else: no excerpt, no file path, no credential;
  * GET /api/sessions/forget/preview is READ-ONLY: it answers what a
    forget WOULD do (the channel, the opaque id, how many messages it
    would silence, the surfaces it silences, the honest D10 limits) and it
    writes NOTHING; an unknown source id answers an HONEST error, never a
    silent empty;
  * POST /api/sessions/forget is Bearer-gated (401 without the token, 401
    with a wrong one) and records a TOMBSTONE only: NOTHING is deleted —
    the transcript file is byte-identical before and after — and the
    message leaves the search results, the session list, the per-session
    export, the fold-time recall and the owner profile at once;
  * a REBUILT profile cannot regenerate the forgotten message, and an
    unrelated message keeps working;
  * repeating the forget is idempotent (already=true, one ledger row);
  * a suppression stays inside its profile: another profile's transcripts
    are unaffected;
  * the ledger row, the preview and every answer carry no message text,
    no file path and no credential;
  * the REPOSITORY's tracked files and the production state are untouched
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

Run:  python3 tests/session_forget_api.py     (exit 0 = all green)
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
RESERVED_PORTS = {19167, 3460}          # the production console + shared proxy

# Fixture credential (throwaway, profile-local, never printed).
TOKEN = "E2E-SESSION-FORGET-TOKEN-GAMMA"

PROFILE_A = "sessalpha"
PROFILE_B = "sessbeta"

# The owner's own words. The FORGOTTEN message is a user message; the
# SURVIVOR is an unrelated one that must keep working.
LEGACY_SECRET = "the secret launch code is hunter2"
LEGACY_ASSISTANT = "understood, the secret launch code"
LEGACY_SURVIVOR = "the unrelated release notes mention version 5"
# The console's OWN live session and the chat service's session carry their
# OWN forgotten message, so every read path is proved on its own source (and
# the counts of the other faces stay exactly what they were).
LEGACY_CONSOLE_SECRET = "the console launch phrase is omega9"
LEGACY_CONSOLE_ANSWER = "understood, the console launch phrase"
LEGACY_CONSOLE_TURN = "another unrelated console turn"
LEGACY_SERVICE_SECRET = "the service launch phrase is delta7"
LEGACY_SERVICE_TURN = "the unrelated service turn"

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


class FakeModel:
    """A loopback, OpenAI-shaped endpoint that RECORDS what the model was
    actually sent and answers with a canned completion.

    It is the only network endpoint this test allows: it binds on 127.0.0.1
    (the sandbox's guard refuses every other destination), so the one-shot
    runner can be exercised over its REAL transport without a single
    outbound byte."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.port = free_port()
        self._server = None
        self._thread = None

    def start(self) -> bool:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):        # keep the stderr clean
                return

            def do_POST(self):                   # noqa: N802 (http.server API)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    outer.requests.append(json.loads(raw.decode("utf-8")))
                except ValueError:
                    outer.requests.append({})
                body = json.dumps({
                    "choices": [{"message": {"role": "assistant",
                                             "content": "pong"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                              "total_tokens": 2}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer((HOST, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(2)

    @property
    def base(self) -> str:
        return f"http://{HOST}:{self.port}/v1"

    def seen_text(self) -> str:
        """Every message text the endpoint was sent, joined."""
        out: list[str] = []
        for req in self.requests:
            for msg in req.get("messages") or []:
                content = msg.get("content")
                if isinstance(content, str):
                    out.append(content)
        return " ".join(out)


SERVICE_RUNNER = '''"""Read-path probe: the chat service's model context and turn count.

One turn with an INJECTED chat callable (no network at all) and the /status
reply — the two service faces this pass closed. The probe prints ONE line:
"PROBE " + JSON.
"""
import json
import sys

from mycore.service import ChatService

SEEN = []


def _chat(messages=None, tools=None, **kw):
    SEEN.append(list(messages or []))
    return {"choices": [{"message": {"role": "assistant", "content": "pong"},
                         "finish_reason": "stop"}]}


def main():
    state = sys.argv[1]
    svc = ChatService(state, frozenset({111}), chat=_chat, now=lambda: 0.0)
    svc._pipeline_body(111, "what is the plan?")
    status = svc._command(111, "/status")
    print("PROBE " + json.dumps({"messages": SEEN[0] if SEEN else [],
                                 "status": status}, ensure_ascii=False))


main()
'''


def transcript_body() -> str:
    """The owner's own messages as the session transcript stores them."""
    now = 1_800_000_000.0
    day = 86400.0
    rows = [
        {"role": "user", "content": LEGACY_SECRET, "ts": now - 2 * day},
        {"role": "assistant", "content": LEGACY_ASSISTANT, "ts": now - 2 * day},
        {"role": "user", "content": LEGACY_SURVIVOR, "ts": now - 5 * day},
    ]
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def console_body() -> str:
    """The console's OWN live session (the chat history face reads it)."""
    now = 1_800_000_000.0
    day = 86400.0
    rows = [
        {"role": "user", "content": LEGACY_CONSOLE_SECRET, "ts": now - 2 * day},
        {"role": "assistant", "content": LEGACY_CONSOLE_ANSWER,
         "ts": now - 2 * day},
        {"role": "user", "content": LEGACY_CONSOLE_TURN, "ts": now - 5 * day},
    ]
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def service_body() -> str:
    """The chat service's session (the TG legs read it)."""
    now = 1_800_000_000.0
    day = 86400.0
    rows = [
        {"role": "user", "content": LEGACY_SERVICE_SECRET, "ts": now - 2 * day},
        {"role": "user", "content": LEGACY_SERVICE_TURN, "ts": now - 5 * day},
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
    # the console's OWN live session + the chat service's session
    (profile / "state" / "sessions" / "web.jsonl").write_text(
        console_body(), encoding="utf-8")
    (profile / "state" / "sessions" / "111.jsonl").write_text(
        service_body(), encoding="utf-8")
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
    def search(self, query: str):
        return api(self.base, "GET", "/api/sessions?q="
                   + urllib.parse.quote(query))

    def sessions(self):
        return api(self.base, "GET", "/api/sessions")

    def export(self, sid: str):
        return api(self.base, "GET", f"/api/sessions?id={urllib.parse.quote(sid)}"
                   "&format=json")

    def analyze(self, token: str | None = None):
        return api(self.base, "POST", "/api/profiling",
                   body={"action": "analyze"},
                   token=self.token if token is None else token)

    def profile_view(self):
        return api(self.base, "GET", "/api/profiling")

    def preview(self, source_id: str):
        return api(self.base, "GET", "/api/sessions/forget/preview?source="
                   + urllib.parse.quote(source_id))

    def forget(self, source_id: str, token: str | None = None):
        return api(self.base, "POST", "/api/sessions/forget",
                   body={"source_id": source_id},
                   token=self.token if token is None else token)

    # -- the read faces this pass closed ------------------------------------
    def history(self):
        """GET /api/chat/history — the console's live chat history."""
        return api(self.base, "GET", "/api/chat/history")

    def chat_action(self, action: str, msg_id: object, token: str | None = None):
        """POST /api/chat {action, id} — the rewind / fork / resume legs."""
        return api(self.base, "POST", "/api/chat",
                   body={"action": action, "id": msg_id},
                   token=self.token if token is None else token)


def hits_of(payload) -> list[dict]:
    """The source-id rows of every hit of the first session row."""
    rows = (payload or {}).get("sessions") or []
    if not rows:
        return []
    return [h for h in (rows[0].get("hit_sources") or [])
            if isinstance(h, dict)]


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
    root = Path(tempfile.mkdtemp(prefix="aidog-session-forget-"))
    netlog = root / "network.log"
    consoles: list[Console] = []
    model: FakeModel | None = None
    prod_state = ROOT / "data" / "mycore_service"
    prod_before = {
        "ledger": sha256(prod_state / "memforget" / "tombstones.jsonl"),
        "profile": sha256(prod_state / "profiling" / "profile.json"),
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
        console_live = state_a / "sessions" / "web.jsonl"
        console_live_before = console_live.read_bytes()
        service_live = state_a / "sessions" / "111.jsonl"
        service_live_before = service_live.read_bytes()

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

        # ---------------------------------------------- the search face -----
        status, search, raw_search = console_a.search("hunter2")
        check("B1 GET /api/sessions?q= answers 200 without a Bearer token",
              status == 200 and isinstance(search, dict), f"status={status}")
        check("B2 the search face reports the real full-text search",
              (search.get("search") or {}).get("method") == "fts5",
              str(search.get("search"))[:160])
        hits = hits_of(search)
        check("B3 every hit carries the opaque source id of its message",
              len(hits) == 1
              and str(hits[0].get("source_id") or "").startswith("src_")
              and len(hits[0].get("source_id") or "") == 36,
              str(hits)[:200])
        check("B4 the hit carries nothing but the source id",
              set(hits[0]) == {"source_id"}
              and LEGACY_SECRET not in json.dumps(hits)
              and str(state_a) not in json.dumps(hits)
              and "a.jsonl" not in json.dumps(hits),
              json.dumps(hits)[:200])
        source_id = hits[0]["source_id"]

        # --------------------------------------------- the preview (read) ---
        status, preview, raw_preview = console_a.preview(source_id)
        check("C1 GET /api/sessions/forget/preview answers 200 without a "
              "Bearer token",
              status == 200 and isinstance(preview, dict), f"status={status}")
        check("C2 the preview carries exactly the contracted keys",
              set(preview) == {"source_id", "channel", "items", "affects",
                               "limits"}, str(sorted(preview)))
        check("C3 the preview names the source id, the channel and what it "
              "would silence",
              preview.get("source_id") == source_id
              and preview.get("channel") == "session"
              and preview.get("items") == 1
              and preview.get("affects") == {"recall": True,
                                             "session_list": True,
                                             "profile": True},
              str(preview)[:240])
        check("C4 the preview carries the honest D10 limits",
              "NOT deleted" in str(preview.get("limits"))
              and "transcript" in str(preview.get("limits")),
              str(preview.get("limits"))[:160])
        check("C5 the preview carries no message text, no file path and no "
              "credential",
              LEGACY_SECRET not in raw_preview and LEGACY_ASSISTANT not in raw_preview
              and str(state_a) not in raw_preview and "a.jsonl" not in raw_preview
              and TOKEN not in raw_preview, raw_preview[:200])
        check("C6 the preview wrote nothing (it is read-only)",
              not (state_a / "memforget").exists(),
              str(sorted(p.name for p in state_a.iterdir())))
        status, body, _ = console_a.preview("src_" + "0" * 32)
        check("C7 an unknown source id answers an honest error",
              status == 404 and isinstance(body, dict) and body.get("error"),
              f"status={status} body={body}")
        status, body, _ = console_a.preview("not-a-source-id")
        check("C8 a malformed source id answers an honest error",
              status == 400 and isinstance(body, dict) and body.get("error"),
              f"status={status} body={body}")

        # ------------------------------------------------ the Bearer gate --
        status, body, _ = api(console_a.base, "POST", "/api/sessions/forget",
                              body={"source_id": source_id})
        check("E1 POST /api/sessions/forget without a Bearer token -> 401",
              status == 401, f"status={status}")
        status, body, _ = console_a.forget(source_id, token="wrong-token-value")
        check("E2 POST /api/sessions/forget with a wrong Bearer token -> 401",
              status == 401, f"status={status}")
        check("E3 the refused write recorded nothing",
              not (state_a / "memforget").exists(),
              str(sorted(p.name for p in state_a.iterdir())))

        # ------------------------------------------------------ the forget --
        status, body, raw = console_a.forget(source_id)
        check("F1 POST /api/sessions/forget with the Bearer token answers ok",
              status == 200 and isinstance(body, dict)
              and body.get("ok") is True, f"status={status} body={body}")
        check("F2 the answer carries the opaque id, the channel, the already "
              "flag and the limits",
              body.get("id") == source_id and body.get("channel") == "session"
              and body.get("already") is False
              and "NOT deleted" in str(body.get("limits")), str(body)[:240])
        check("F2b the answer carries exactly the contracted keys",
              set(body) == {"ok", "id", "channel", "already", "limits"},
              str(sorted(body)))
        check("F3 the answer holds no message text, no file path and no "
              "credential",
              LEGACY_SECRET not in raw and LEGACY_ASSISTANT not in raw
              and str(state_a) not in raw and "a.jsonl" not in raw
              and TOKEN not in raw, raw[:200])
        rows_ledger = ledger_rows(state_a)
        check("F4 the ledger holds exactly one row, in the session channel",
              len(rows_ledger) == 1
              and rows_ledger[0].get("id") == source_id
              and rows_ledger[0].get("channel") == "session",
              str(rows_ledger)[:200])
        ledger_raw = (state_a / "memforget" / "tombstones.jsonl").read_text(
            encoding="utf-8")
        check("F5 the ledger row holds only a digest — no text, no path, no "
              "credential",
              LEGACY_SECRET not in ledger_raw and LEGACY_ASSISTANT not in ledger_raw
              and str(state_a) not in ledger_raw and "a.jsonl" not in ledger_raw
              and TOKEN not in ledger_raw, ledger_raw[:200])
        check("F6 NOTHING was deleted — the transcript is byte-identical",
              transcript_a.read_bytes() == transcript_before,
              "the session transcript changed")

        # ------------------------------- the read faces react, no restart --
        status, search2, _ = console_a.search("hunter2")
        check("G1 the forgotten message left the search results",
              status == 200 and not (search2.get("sessions") or [])
              and search2.get("count") == 0, str(search2)[:240])
        status, listing, raw_listing = console_a.sessions()
        summary = " ".join(str(r.get("summary") or "")
                           for r in (listing.get("sessions") or []))
        check("G2 the forgotten message left the session list",
              LEGACY_SECRET not in summary and LEGACY_SURVIVOR in summary,
              summary[:200])
        status, exported, raw_export = console_a.export("a")
        check("G3 the forgotten message left the per-session export",
              status == 200 and LEGACY_SECRET not in raw_export
              and LEGACY_SURVIVOR in raw_export, raw_export[:200])
        status, search3, _ = console_a.search("release")
        hits3 = hits_of(search3)
        check("G4 a second, unrelated message still works",
              status == 200 and (search3.get("count") or 0) == 1
              and len(hits3) == 1
              and hits3[0].get("source_id") != source_id, str(search3)[:240])

        # --------------------------------------- a rebuild cannot restore --
        status, body, _ = console_a.analyze()
        check("H1 the re-analysis answers ok",
              status == 200 and isinstance(body, dict)
              and body.get("ok") is True, f"status={status} body={body}")
        status, face, _ = console_a.profile_view()
        tokens = {str(r.get("token") or "")
                  for r in (face.get("keywords") or [])}
        check("H2 a rebuilt profile cannot regenerate the forgotten message",
              "hunter2" not in tokens and "secret" not in tokens
              and "release" in tokens, str(sorted(tokens)))

        # -------------------------------------------- idempotence + a second --
        status, body, _ = console_a.forget(source_id)
        check("I1 repeating the forget is a no-op that reports already=true",
              status == 200 and body.get("already") is True
              and body.get("id") == source_id, str(body)[:200])
        check("I2 the repeated forget left exactly one ledger row",
              len(ledger_rows(state_a)) == 1, str(ledger_rows(state_a))[:200])

        # ================= the console's live chat history (the face) ======
        # The console's OWN session carries its own forgotten message, so
        # this face is proved on its own source (the other faces keep their
        # own counts).
        status, hist0, raw_hist0 = console_a.history()
        check("L0 GET /api/chat/history answers 200 without a Bearer token",
              status == 200 and isinstance(hist0, dict), f"status={status}")
        check("L1 the console history shows the message while nothing is "
              "forgotten",
              LEGACY_CONSOLE_SECRET in raw_hist0
              and LEGACY_CONSOLE_TURN in raw_hist0
              and hist0.get("count") == 3, raw_hist0[:240])
        ctx0 = hist0.get("context") or {}
        status, s_con, _ = console_a.search("omega9")
        hits_con = hits_of(s_con)
        check("L2 the search face names the console message's source id",
              len(hits_con) == 1
              and str(hits_con[0].get("source_id") or "").startswith("src_")
              and len(str(hits_con[0].get("source_id") or "")) == 36,
              str(hits_con)[:200])
        console_source = hits_con[0]["source_id"]
        status, body, raw = console_a.forget(console_source)
        check("L3 the console message is forgotten through the same control",
              status == 200 and isinstance(body, dict)
              and body.get("ok") is True and body.get("already") is False
              and body.get("id") == console_source,
              f"status={status} body={body}")
        status, hist1, raw_hist1 = console_a.history()
        check("L4 the forgotten message left the console's live chat history",
              LEGACY_CONSOLE_SECRET not in raw_hist1
              and LEGACY_CONSOLE_ANSWER in raw_hist1
              and LEGACY_CONSOLE_TURN in raw_hist1, raw_hist1[:240])
        check("L5 the console history keeps its shape and its counts",
              set(hist1) == {"messages", "count", "context", "has_more"}
              and hist1.get("count") == 2
              and isinstance(hist1.get("has_more"), bool)
              and all(set(m) == {"id", "role", "content"}
                      for m in hist1.get("messages") or []),
              str(sorted(hist1))[:160])
        check("L6 the console history carries no path and no credential",
              str(state_a) not in raw_hist1 and "web.jsonl" not in raw_hist1
              and TOKEN not in raw_hist1, raw_hist1[:240])
        ctx1 = hist1.get("context") or {}
        check("L7 the context usage counts only the visible turns",
              ctx1.get("available") is True
              and ctx1.get("messages") == (ctx0.get("messages") or 0) - 1
              and (ctx1.get("session_chars") or 0)
              < (ctx0.get("session_chars") or 0),
              f"{ctx0.get('messages')} -> {ctx1.get('messages')}")
        check("L8 the console's own live transcript is byte-identical after "
              "the forget",
              console_live.read_bytes() == console_live_before,
              "the console's live session changed")

        # ================== the console's fork / rewind / resume ==========
        live_before = console_live.read_bytes()
        status, body, raw = console_a.chat_action("fork", 1)
        check("M1 POST /api/chat {action:'fork'} answers ok",
              status == 200 and isinstance(body, dict)
              and body.get("ok") is True, f"status={status} body={body}")
        fork_path = state_a / "sessions" / f"{body.get('id')}.jsonl"
        fork_text = fork_path.read_text(encoding="utf-8")
        check("M2 the fork does not copy the forgotten message",
              LEGACY_CONSOLE_SECRET not in fork_text
              and LEGACY_CONSOLE_ANSWER in fork_text, fork_text[:240])
        check("M3 the fork leaves the live session byte-identical",
              console_live.read_bytes() == live_before,
              "the live session changed")
        status, body, raw = console_a.chat_action("rewind", 2)
        check("M4 POST /api/chat {action:'rewind'} answers ok",
              status == 200 and isinstance(body, dict)
              and body.get("ok") is True, f"status={status} body={body}")
        live_rows = [json.loads(line) for line in
                     console_live.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
        check("M5 the rewind does not carry the forgotten message into the "
              "new live session",
              all(LEGACY_CONSOLE_SECRET not in str(r.get("content") or "")
                  for r in live_rows)
              and any(r.get("content") == LEGACY_CONSOLE_ANSWER
                      for r in live_rows), str(live_rows)[:240])
        archived = sorted((state_a / "sessions" / "archive").glob("web.*.jsonl"))
        check("M6 the pre-rewind transcript survives byte-identically in "
              "archive/ (D10)",
              any(a.read_bytes() == live_before for a in archived),
              str([a.name for a in archived]))
        target = state_a / "sessions" / "b.jsonl"
        target.write_text(console_body(), encoding="utf-8")
        target_before = target.read_bytes()
        status, body, raw = console_a.chat_action("resume", "b")
        check("M7 POST /api/chat {action:'resume'} answers ok",
              status == 200 and isinstance(body, dict)
              and body.get("ok") is True and body.get("resumed") is True,
              f"status={status} body={body}")
        check("M8 the resume is a byte-level move — the forgotten line stays "
              "on disk (D31/D10)",
              console_live.read_bytes() == target_before,
              "the resumed copy differs")
        status, hist2, raw_hist2 = console_a.history()
        check("M9 the resumed session is still filtered on read",
              LEGACY_CONSOLE_SECRET not in raw_hist2
              and LEGACY_CONSOLE_TURN in raw_hist2, raw_hist2[:240])

        # ============ the chat service's history and turn-count faces =====
        # A SECOND PROCESS: the service reads the same profile ledger, so
        # the suppression crosses the process boundary.
        status, s_srv, _ = console_a.search("delta7")
        hits_srv = hits_of(s_srv)
        check("N1 the search face names the service message's source id",
              len(hits_srv) == 1
              and str(hits_srv[0].get("source_id") or "").startswith("src_"),
              str(hits_srv)[:200])
        status, body, raw = console_a.forget(hits_srv[0]["source_id"])
        check("N2 the service message is forgotten through the same control",
              status == 200 and isinstance(body, dict)
              and body.get("ok") is True, f"status={status} body={body}")
        runner = root / "service_probe.py"
        runner.write_text(SERVICE_RUNNER, encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(profiles),
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(sandbox),
            "AIDOG_TEST_NETLOG": str(netlog),
            "AIDOG_PROFILES_DIR": str(profiles),
            "AIDOG_PROFILE": PROFILE_A,
            "AIDOG_MC_STATE": str(state_a),
            "MYCORE_TG_TOKEN": "e2e-service-probe-token-not-real",
        }
        proc = subprocess.run([sys.executable, str(runner), str(state_a)],
                              cwd=str(sandbox), env=env, capture_output=True,
                              text=True, timeout=180)
        probe: dict = {}
        for line in proc.stdout.splitlines():
            if line.startswith("PROBE "):
                probe = json.loads(line[len("PROBE "):])
        msgs = probe.get("messages") or []
        seen_text = " ".join(str(m.get("content") or "") for m in msgs)
        check("N3 the service's model context never carries the forgotten "
              "message",
              bool(msgs) and LEGACY_SERVICE_SECRET not in seen_text,
              seen_text[:240])
        check("N4 the service's model context keeps the unrelated turn",
              LEGACY_SERVICE_TURN in seen_text, seen_text[:240])
        status_line = str(probe.get("status") or "")
        raw_rows = len([line for line in service_live.read_text(
            encoding="utf-8").splitlines() if line.strip()])
        turns = None
        for token in status_line.split():
            if token.startswith("turns="):
                turns = int(token.split("=", 1)[1] or 0)
        check("N5 the service's turn count skips the forgotten message",
              turns is not None and turns == raw_rows - 1,
              f"turns={turns} raw={raw_rows} status={status_line[:80]}")
        check("N6 the service's turn-count face carries no message text",
              LEGACY_SERVICE_SECRET not in status_line
              and str(state_a) not in status_line, status_line[:160])
        check("N7 the service's transcript keeps the forgotten line on disk "
              "(append-only, D31)",
              service_live.read_bytes().startswith(service_live_before)
              and LEGACY_SERVICE_SECRET in service_live.read_text(
                  encoding="utf-8"), "the service transcript changed")

        # ==================== the one-shot runner's session view ==========
        model = FakeModel()
        check("O1 the loopback model endpoint starts", model.start(),
              "the fake endpoint did not bind")
        cli_session = root / "cli_session.jsonl"
        cli_session.write_text(transcript_body(), encoding="utf-8")
        cli_before = cli_session.read_bytes()
        env_cli = dict(env)
        env_cli["MYCORE_BASE_URL"] = model.base
        env_cli["MYCORE_MODEL"] = "e2e-fake-model"
        proc = subprocess.run(
            [sys.executable, "-m", "mycore.cli", "--session",
             str(cli_session), "--quiet", "what is the plan?"],
            cwd=str(sandbox), env=env_cli, capture_output=True, text=True,
            timeout=180)
        check("O2 the one-shot runner answers through the loopback endpoint",
              proc.returncode == 0 and bool(model.requests),
              proc.stderr[-300:])
        seen = model.seen_text()
        check("O3 the one-shot runner's session view drops the forgotten "
              "message",
              LEGACY_SECRET not in seen and LEGACY_SURVIVOR in seen, seen[:240])
        check("O4 the one-shot runner's session file keeps the forgotten line "
              "on disk (append-only, D31)",
              cli_session.read_bytes().startswith(cli_before)
              and LEGACY_SECRET in cli_session.read_text(encoding="utf-8"),
              "the CLI session changed")
        model.stop()

        # ---------------------------------------------- profile isolation --
        status, body, _ = console_b.analyze()
        check("J1 the second profile analysed its own corpus",
              status == 200 and isinstance(body, dict)
              and body.get("ok") is True, str(body)[:200])
        status, search_b, _ = console_b.search("hunter2")
        hits_b = hits_of(search_b)
        check("J2 a suppression stays inside its profile",
              status == 200 and (search_b.get("count") or 0) == 1
              and len(hits_b) == 1
              and hits_b[0].get("source_id") != source_id, str(search_b)[:240])
        status, face_b, _ = console_b.profile_view()
        tokens_b = {str(r.get("token") or "")
                    for r in (face_b.get("keywords") or [])}
        check("J3 the other profile's profile still holds the message's words",
              "hunter2" in tokens_b, str(sorted(tokens_b)))
        status, body, _ = console_b.forget(hits_b[0]["source_id"])
        check("J4 the other profile can forget its own copy",
              status == 200 and body.get("ok") is True
              and body.get("already") is False, str(body)[:200])
        status, search_b2, _ = console_b.search("hunter2")
        check("J5 the other profile's message left ITS search results only",
              status == 200 and not (search_b2.get("sessions") or []),
              str(search_b2)[:240])

        # ------------------------------------------------------- hygiene -----
        check("K1 no fixture excerpt reached the console stderr",
              LEGACY_SECRET not in console_a.stderr
              and LEGACY_SECRET not in console_b.stderr, "stderr leak")
        check("K2 no fixture credential reached the console stderr",
              TOKEN not in console_a.stderr and TOKEN not in console_b.stderr,
              "stderr leak")
        violations = ""
        if netlog.exists():
            violations = netlog.read_text(encoding="utf-8")
        check("K3 the loopback-only guard recorded no outbound attempt",
              violations.strip() == "", f"violations={violations.strip()[:200]}")
        prod_after = {
            "ledger": sha256(prod_state / "memforget" / "tombstones.jsonl"),
            "profile": sha256(prod_state / "profiling" / "profile.json"),
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
        if model is not None:
            model.stop()
        shutil.rmtree(root, ignore_errors=True)

    print("")
    print(f"{PASSED}/{PASSED + FAILED} PASS")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
