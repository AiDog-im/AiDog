#!/usr/bin/env python3
"""Diverged-install recovery drill — the offline proof for the backup-first
recovery path of docs/publish_iron_rules.md section 2.1 (finding 1 of
docs/handover/market_readiness_audit_2026_11_24.md).

    python3 tests/diverged_recovery_drill.py               # the drill (exit 0 = all PASS)
    python3 tests/diverged_recovery_drill.py --chat-smoke  # internal child mode

What is proven, against a throwaway fixture under /tmp (never the real
installation, never the network):

  (a) an installation whose history DIVERGED from its origin is refused by
      the shipped `deploy/aidog_update.py apply`: the ff-only merge fails,
      the exit code is non-zero, nothing is forced, not one worktree byte
      changes, and the boot_services restart step is never reached. A
      second fixture whose history was REBUILT (no common ancestor with
      the remote) is refused the same way.
  (b) backup-first: the original directory stays in place while its config,
      local changes and data are exported to readable artifacts — a tar of
      the worktree plus a git bundle of the local-only commits.
  (c) a fresh clone of the same remote lands in a SIBLING directory.
  (d) the restore goes into an independent profile AND an independent state
      root (AIDOG_PROFILES_DIR + AIDOG_PROFILE + AIDOG_MC_STATE); every
      resolved path is proven independent of the original installation.
  (e) before any service path switch: the FIRST REPLY works through the
      restored profile (isolated child chat smoke against a loopback mock
      model), and memory + transcript suppression still holds from the
      ledger restored into the new state root.
  (f) the original installation is byte-identical at the end (rollback).
  (g) the updater stayed fast-forward-only: no force push, no merge --no-ff,
      no reset --hard anywhere in this drill.

Offline by construction: a process audit hook denies every socket in the
parent (whitelisting only git/python children) and the chat-smoke child
allows only its own 127.0.0.1 ephemeral ports; every git command talks to
a local path remote under GIT_ALLOW_PROTOCOL=file. Stdlib only.
English comments (repo i18n policy).
"""
from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
import threading

ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"

# Fixture vocabulary — synthetic values only, never a real credential.
PROFILE = "drilldog"                      # matches [a-z0-9][a-z0-9_-]{0,31}
SEED_VERSION = "1.0.0"                    # version the diverged install holds
REMOTE_VERSION = "1.1.0"                  # version origin advances to
WEB_TOKEN = "fixture-console-token"
OPENAI_TOKEN = "fixture-gateway-token"
MODEL_KEY = "fixture-model-key"
MODEL = "drill-recovery-model"
PIECES = ("Local ", "HTTP reply.")
REPLY = "".join(PIECES)
CHAT_PROMPT = "Recovery drill prompt"
FORGOTTEN_MEMORY = "the memory the owner suppressed"
KEPT_MEMORY = "the memory the owner kept"
FORGOTTEN_MESSAGE = "the transcript message the owner forgot"
KEPT_MESSAGE = "the transcript reply the owner kept"
SESSION_STEM = "drill-restored"           # restored transcript (the chat smoke
                                          # writes its own `web.jsonl` beside it)

# The fixture .gitignore: the diverged install must carry uncommitted local
# data (config, profiles, state) WITHOUT tripping apply's dirty-worktree
# guard — ignored files are local state, not local code changes.
GITIGNORE = (
    "secrets.env\n"
    "web/settings.json\n"
    "profiles/\n"
    "data/\n"
    "*.log\n"
    "__pycache__/\n"
)

PASS: list[str] = []
FAIL: list[str] = []
# Every git command this drill runs, for the (g) force-scan.
GIT_LOG: list[list[str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail and not ok else ""))


# ------------------------------------------------------------- audit hook --
ALLOWED: set = set()      # loopback addresses a server in THIS process bound
BLOCKED: list[str] = []    # denied events — must stay empty at the end


def _spawn_allowed(args) -> bool:
    """Parent-mode child-process policy: git (all fixture plumbing) and
    python (apply + the chat-smoke child). Nothing else may be spawned."""
    argv = args[1] if len(args) > 1 and isinstance(args[1], (list, tuple)) \
        and args[1] else ([args[0]] if args[0] else [])
    if not argv:
        return False
    base = Path(str(argv[0])).name
    return base == "git" or base.startswith("python")


def make_audit(mode: str):
    """The offline guard. `parent` denies every socket (it needs none) and
    whitelists git/python children; `child` (the chat smoke) allows only the
    ephemeral loopback ports it bound itself and spawns nothing."""
    def audit(event, args):
        if event == "socket.connect":
            denied = args[1] not in ALLOWED
        elif event == "socket.getaddrinfo":
            denied = args[:2] not in ALLOWED
        elif event == "socket.sendto":
            denied = True
        elif event in {"socket.gethostbyname", "socket.gethostbyaddr",
                       "socket.getnameinfo"}:
            denied = True
        elif event in {"subprocess.Popen", "os.system", "os.exec",
                       "os.posix_spawn", "os.spawn", "os.startfile"}:
            denied = not (mode == "parent" and _spawn_allowed(args))
        else:
            return
        if denied:
            BLOCKED.append(event)
            raise PermissionError(
                f"unexpected {event} in the recovery drill ({mode})")
    return audit


# ------------------------------------------------- the loopback mock model --
class MockOpenAI(BaseHTTPRequestHandler):
    def do_POST(self):
        self.connection.settimeout(5)
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, self.headers.get("Authorization"),
                                  payload))
        if self.path != "/v1/chat/completions" \
                or self.headers.get("Authorization") != f"Bearer {MODEL_KEY}":
            self.send_error(400, "unexpected mock request")
            return
        if payload.get("stream") is True:
            chunks = [{"choices": [{"index": 0, "delta": {"content": piece},
                                    "finish_reason": None}]}
                      for piece in PIECES]
            chunks.append({"choices": [{"index": 0, "delta": {},
                                        "finish_reason": "stop"}]})
            frames = [json.dumps(chunk) for chunk in chunks] + ["[DONE]"]
            body = "".join(f"data: {frame}\n\n" for frame in frames).encode()
            content_type = "text/event-stream"
        else:
            body = json.dumps({"choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant",
                                                        "content": REPLY}}]}).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind would perform a reverse DNS lookup.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address
        if self.server_port == 3460:
            # The shared model proxy port is off limits, ever.
            raise RuntimeError("refusing to use the shared proxy port")


@contextmanager
def serve(handler):
    with LoopbackServer((HOST, 0), handler) as httpd:
        address = httpd.server_address
        ALLOWED.add(address)
        thread = threading.Thread(target=httpd.serve_forever,
                                  kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield httpd
        finally:
            httpd.shutdown()
            thread.join()
            ALLOWED.remove(address)


def sse_frames(body):
    """Return (event, payload); event=None for the data-only SSE dialect."""
    frames = []
    for frame in body.decode().strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines())
        data = fields["data"]
        frames.append((fields.get("event"),
                       data if data == "[DONE]" else json.loads(data)))
    return frames


# ------------------------------------------------------------- git plumbing --
def git_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("AIDOG_GH_PAT", "GITHUB_PAT", "PYTHONPATH")}
    env.update({
        "GIT_ALLOW_PROTOCOL": "file",     # every remote here is a local path
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "drill",
        "GIT_AUTHOR_EMAIL": "drill@example.invalid",
        "GIT_COMMITTER_NAME": "drill",
        "GIT_COMMITTER_EMAIL": "drill@example.invalid",
    })
    return env


def git(*args, cwd: Path | None = None, must: bool = True) -> subprocess.CompletedProcess:
    argv = ["git", *(["-C", str(cwd)] if cwd else []), *args]
    GIT_LOG.append(list(argv))
    result = subprocess.run(argv, capture_output=True, text=True,
                            env=git_env(), timeout=120)
    if must and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: "
            f"{(result.stderr or result.stdout).strip()[:300]}")
    return result


def worktree_digest(root: Path) -> str:
    """Content hash of every file in the worktree except .git — the
    before/after proof that apply touched nothing the operator owns."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if ".git" in rel.parts:
            continue
        digest.update(rel.as_posix().encode())
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            digest.update(f"unreadable:{type(exc).__name__}".encode())
        digest.update(b"\0")
    return digest.hexdigest()


@contextmanager
def profile_env(**values):
    """Set/clear the profile seams for one phase, then restore exactly."""
    keys = ("AIDOG_PROFILES_DIR", "AIDOG_PROFILE", "AIDOG_MC_STATE")
    saved = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ.pop(key, None)
    os.environ.update({key: str(value) for key, value in values.items()})
    try:
        yield
    finally:
        for key in keys:
            if saved[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved[key]


def product_modules():
    """The product modules under test, imported with the repo root on
    sys.path (namespace packages `lib` / `mycore`)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from lib import profile_paths            # noqa: PLC0415
    from mycore import memforget, session    # noqa: PLC0415
    return profile_paths, memforget, session


def run_apply(repo: Path) -> subprocess.CompletedProcess:
    """The shipped updater, run from the FIXTURE clone (REPO self-locates
    via __file__). No PAT in the environment: a local path remote needs no
    credentials and the fetch must succeed anonymously."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("AIDOG_GH_PAT", "GITHUB_PAT", "PYTHONPATH")}
    env.update({"GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0"})
    return subprocess.run(
        [sys.executable, str(repo / "deploy" / "aidog_update.py"), "apply"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=300)


# ------------------------------------------------------------------ fixture --
def build_fixture(tmp: Path) -> dict:
    """A local bare `remote`, a diverged `install`, a `rebuilt` clone with
    no common ancestor, and the seed used to advance the remote."""
    paths = {"tmp": tmp, "seed": tmp / "seed", "remote": tmp / "remote.git",
             "install": tmp / "install", "rebuilt": tmp / "install-rebuilt",
             "boot_marker": tmp / "boot_services_called.marker"}
    seed, remote, install, rebuilt = (paths["seed"], paths["remote"],
                                      paths["install"], paths["rebuilt"])

    git("-c", "init.defaultBranch=main", "init", str(seed))
    (seed / "deploy").mkdir()
    (seed / "VERSION").write_text(SEED_VERSION + "\n", encoding="utf-8")
    (seed / "release.json").write_text(
        json.dumps({"version": SEED_VERSION, "date": "2026-01-01",
                    "notes": "fixture release"}) + "\n", encoding="utf-8")
    (seed / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (seed / "README.md").write_text("fixture installation\n", encoding="utf-8")
    # The REAL updater under test, copied verbatim (REPO then self-locates
    # inside the fixture clone).
    shutil.copyfile(ROOT / "deploy" / "aidog_update.py",
                    seed / "deploy" / "aidog_update.py")
    boot_stub = (
        "#!/bin/sh\n"
        "# Fixture stub: records any invocation so the drill can prove a\n"
        "# diverged apply never reaches the restart step.\n"
        f'echo invoked >> "{paths["boot_marker"]}"\n'
        "exit 0\n")
    boot = seed / "deploy" / "boot_services.sh"
    boot.write_text(boot_stub, encoding="utf-8")
    boot.chmod(0o755)

    git("add", ".", cwd=seed)
    git("commit", "-m", "fixture release " + SEED_VERSION, cwd=seed)
    git("clone", "--bare", str(seed), str(remote))
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "origin", "main", cwd=seed)
    git("clone", str(remote), str(install))
    return paths


def seed_local_state(paths: dict, pp, mf, session) -> dict:
    """The diverged install's LOCAL, uncommitted state: config, credentials
    (fixture values), profile body, sessions, data — plus the two
    suppressions that later prove the ledger travelled with the backup."""
    install = paths["install"]
    (install / "web").mkdir()
    (install / "data").mkdir()
    settings = {"mycore": {"base_url": "http://127.0.0.1:9/v1",
                           "model": MODEL, "timeout": 5}}
    settings_text = json.dumps(settings) + "\n"
    (install / "web" / "settings.json").write_text(settings_text,
                                                    encoding="utf-8")
    secrets_text = (f"WEB_TOKEN={WEB_TOKEN}\n"
                    f"OPENAI_API_TOKEN={OPENAI_TOKEN}\n"
                    f"MODEL_API_KEY={MODEL_KEY}\n")
    (install / "secrets.env").write_text(secrets_text, encoding="utf-8")
    (install / "secrets.env").chmod(0o600)
    (install / "data" / "local_state.txt").write_text(
        "local runtime data the operator owns\n", encoding="utf-8")
    # A tracked local commit: the divergence itself.
    (install / "local_notes.txt").write_text(
        "local operator note kept by the diverged install\n", encoding="utf-8")
    git("add", "local_notes.txt", cwd=install)
    git("commit", "-m", "local operator change", cwd=install)

    profile = install / "profiles" / PROFILE
    (profile / "body" / "memory").mkdir(parents=True)
    (profile / "state" / "sessions").mkdir(parents=True)
    (profile / "settings.json").write_text(settings_text, encoding="utf-8")
    (profile / "secrets.env").write_text(secrets_text, encoding="utf-8")
    (profile / "secrets.env").chmod(0o600)
    memory_text = ("# Curated memory (fixture)\n\n"
                   f"## Suppressed note\n{FORGOTTEN_MEMORY}\n\n"
                   f"## Kept note\n{KEPT_MEMORY}\n")
    (profile / "body" / "memory" / "MEMORY_curated.md").write_text(
        memory_text, encoding="utf-8")
    (profile / "body" / "memory" / "USER_curated.md").write_text(
        "# USER\n\n## Kept user\nthe user text the owner kept\n",
        encoding="utf-8")
    transcript = (json.dumps({"meta": {"created": 1766600000, "model": MODEL,
                                       "format": 1}}) + "\n"
                  + json.dumps({"role": "user",
                                "content": FORGOTTEN_MESSAGE}) + "\n"
                  + json.dumps({"role": "assistant",
                                "content": KEPT_MESSAGE}) + "\n")
    transcript_path = (profile / "state" / "sessions"
                       / f"{SESSION_STEM}.jsonl")
    transcript_path.write_text(transcript, encoding="utf-8")

    # Two tombstones under the ORIGINAL install's profile: one curated
    # memory entry (addressed by its exact stored section body) and one
    # transcript message (addressed by its normalised identity).
    with profile_env(AIDOG_PROFILES_DIR=profile.parent,
                     AIDOG_PROFILE=PROFILE):
        mf.invalidate_cache()
        _, chunks = mf.split_sections(
            memory_text, *mf.levels_for(mf.CHANNEL_CURATED_MEMORY))
        target = next(chunk for chunk in chunks if FORGOTTEN_MEMORY
                      in chunk["body"])
        first = mf.forget(mf.CHANNEL_CURATED_MEMORY, target["body"])
        identity = session.message_identity(
            {"role": "user", "content": FORGOTTEN_MESSAGE})
        second = mf.forget(mf.CHANNEL_SESSION, identity)
        ledger = mf.ledger_path()
        row_count = len(mf.load_rows())
    return {"profile": profile, "settings_text": settings_text,
            "secrets_text": secrets_text, "memory_text": memory_text,
            "transcript": transcript, "transcript_path": transcript_path,
            "ledger": ledger, "ledger_rows": row_count,
            "forget_ok": bool(first.get("ok") and second.get("ok")),
            "identity": identity}


# ------------------------------------------------------------- the drill -----
def drill() -> None:
    pp, mf, session = product_modules()
    tmp = Path(tempfile.mkdtemp(prefix="aidog-diverged-drill-"))
    keep = False
    try:
        # ---------------------------------------------------- fixture ----
        paths = build_fixture(tmp)
        install, remote, rebuilt = (paths["install"], paths["remote"],
                                    paths["rebuilt"])
        local = seed_local_state(paths, pp, mf, session)
        check("fixture: two tombstones recorded under the diverged profile",
              local["forget_ok"] and local["ledger_rows"] == 2,
              f"rows={local['ledger_rows']}")
        check("fixture: updater copy is byte-identical to the shipped one",
              (install / "deploy" / "aidog_update.py").read_bytes()
              == (ROOT / "deploy" / "aidog_update.py").read_bytes())

        baseline_digest = worktree_digest(install)
        baseline_head = git("rev-parse", "HEAD", cwd=install).stdout.strip()

        # Origin advances to a NEW release while the install holds its own
        # local commit — the two histories now diverge.
        seed = paths["seed"]
        (seed / "VERSION").write_text(REMOTE_VERSION + "\n", encoding="utf-8")
        (seed / "release.json").write_text(
            json.dumps({"version": REMOTE_VERSION, "date": "2026-01-02",
                        "notes": "fixture update"}) + "\n", encoding="utf-8")
        git("add", ".", cwd=seed)
        git("commit", "-m", "fixture release " + REMOTE_VERSION, cwd=seed)
        git("push", "origin", "main", cwd=seed)
        # From this point on, ORIGIN must never change again: every later
        # step (apply, clone, bundle, ls-remote) is read-only against it.
        baseline_remote = git("ls-remote", "--refs", str(remote)).stdout

        # A second installation whose history was REBUILT with no common
        # ancestor at all (the other divergence shape of the finding).
        git("-c", "init.defaultBranch=main", "init", str(rebuilt))
        (rebuilt / "deploy").mkdir()
        shutil.copyfile(ROOT / "deploy" / "aidog_update.py",
                        rebuilt / "deploy" / "aidog_update.py")
        (rebuilt / "deploy" / "boot_services.sh").write_text(
            (paths["boot_marker"].parent / "seed" / "deploy"
             / "boot_services.sh").read_text(encoding="utf-8"), encoding="utf-8")
        (rebuilt / "deploy" / "boot_services.sh").chmod(0o755)
        (rebuilt / "VERSION").write_text(SEED_VERSION + "\n", encoding="utf-8")
        (rebuilt / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        (rebuilt / "README.md").write_text("rebuilt installation\n",
                                           encoding="utf-8")
        git("add", ".", cwd=rebuilt)
        git("commit", "-m", "rebuilt history, no common ancestor", cwd=rebuilt)
        git("remote", "add", "origin", str(remote), cwd=rebuilt)

        # ------------------------------------------------------- (a) -----
        diverged = run_apply(install)
        out = diverged.stdout + diverged.stderr
        check("a1 diverged install: apply exits non-zero",
              diverged.returncode != 0, f"rc={diverged.returncode}")
        check("a2 diverged install: refused at the ff-only merge "
              "(not the dirty guard, not the fetch)",
              "refusing: worktree" not in diverged.stdout
              and "fetch failed" not in diverged.stdout
              and "code updated" not in diverged.stdout
              and any(token in out for token in
                      ("fast-forward", "diverging", "aborting")),
              diverged.stdout.strip()[:300])
        check("a3 diverged install: HEAD and the local commit untouched",
              git("rev-parse", "HEAD", cwd=install).stdout.strip()
              == baseline_head
              and (install / "local_notes.txt").is_file())
        check("a4 diverged install: no worktree byte changed",
              worktree_digest(install) == baseline_digest)
        check("a5 diverged install: boot_services restart never reached",
              not paths["boot_marker"].exists())

        rebuilt_before = git("rev-parse", "HEAD", cwd=rebuilt).stdout.strip()
        rebuilt_run = run_apply(rebuilt)
        rebuilt_out = rebuilt_run.stdout + rebuilt_run.stderr
        check("a6 rebuilt history (no common ancestor): apply exits "
              "non-zero", rebuilt_run.returncode != 0
              and "refusing: worktree" not in rebuilt_run.stdout
              and "fetch failed" not in rebuilt_run.stdout,
              rebuilt_run.stdout.strip()[:300])
        check("a7 rebuilt history: HEAD untouched, restart never reached, "
              "refusal names the unrelated histories",
              git("rev-parse", "HEAD", cwd=rebuilt).stdout.strip()
              == rebuilt_before
              and "unrelated" in rebuilt_out
              and not paths["boot_marker"].exists())

        # ------------------------------------------------------- (b) -----
        backup = tmp / "backup"
        backup.mkdir()
        tar_path = backup / "install-backup.tar"

        def drop_git(tarinfo):
            return None if ".git" in Path(tarinfo.name).parts else tarinfo

        with tarfile.open(tar_path, "w") as tar:
            tar.add(install, arcname="install", filter=drop_git)
        check("b1 backup: the tar artifact exists and is readable",
              tar_path.is_file() and tar_path.stat().st_size > 0
              and tarfile.is_tarfile(tar_path))
        extract = tmp / "backup_extract"
        with tarfile.open(tar_path, "r") as tar:
            names = tar.getmembers()
            tar.extractall(extract, filter="data")
        member_names = {member.name for member in names}
        expected = {
            "install/secrets.env",
            "install/web/settings.json",
            "install/data/local_state.txt",
            "install/local_notes.txt",
            f"install/profiles/{PROFILE}/settings.json",
            f"install/profiles/{PROFILE}/secrets.env",
            f"install/profiles/{PROFILE}/body/memory/MEMORY_curated.md",
            f"install/profiles/{PROFILE}/state/sessions/"
            f"{SESSION_STEM}.jsonl",
            f"install/profiles/{PROFILE}/state/memforget/tombstones.jsonl",
        }
        extracted = extract / "install"
        readable = all(name in member_names for name in expected) and all(
            (extracted / Path(name).relative_to("install")).is_file()
            for name in expected)
        check("b2 backup: config, local changes and data all carried and "
              "byte-identical on extract", readable and all(
                  (extracted / rel).read_bytes() == (install / rel).read_bytes()
                  for rel in ("secrets.env", "web/settings.json",
                              "data/local_state.txt", "local_notes.txt",
                              f"profiles/{PROFILE}/settings.json",
                              f"profiles/{PROFILE}/secrets.env",
                              f"profiles/{PROFILE}/body/memory/"
                              f"MEMORY_curated.md",
                              f"profiles/{PROFILE}/state/sessions/"
                              f"{SESSION_STEM}.jsonl",
                              f"profiles/{PROFILE}/state/memforget/"
                              f"tombstones.jsonl")))
        bundle = backup / "install-history.bundle"
        git("bundle", "create", str(bundle), "main", cwd=install)
        verify = git("bundle", "verify", str(bundle), must=False, cwd=install)
        heads = git("bundle", "list-heads", str(bundle), cwd=install).stdout
        check("b3 backup: git bundle verifies and carries the local-only "
              "commit", bundle.is_file() and verify.returncode == 0
              and baseline_head in heads)
        check("b4 backup: the original directory was left untouched",
              worktree_digest(install) == baseline_digest)

        # ------------------------------------------------------- (c) -----
        sibling = tmp / "install-fresh"
        git("clone", str(remote), str(sibling))
        sibling_head = git("rev-parse", "HEAD", cwd=sibling).stdout.strip()
        remote_head = git("ls-remote", "--refs", str(remote), cwd=sibling).stdout.split()[0]
        check("c1 fresh clone landed in a sibling directory of the original",
              sibling.parent == install.parent and sibling != install
              and sibling.is_dir())
        check("c2 fresh clone matches the remote head (new release) and "
              "carries no local commit",
              (sibling / "VERSION").read_text().strip() == REMOTE_VERSION
              and sibling_head == remote_head
              and not (sibling / "local_notes.txt").exists())

        # ------------------------------------------------------- (d) -----
        # Restore from the BACKUP ARTIFACT into an independent profile root
        # and an independent state root (config/body under the profile, the
        # state tree hoisted to AIDOG_MC_STATE).
        recover = tmp / "recover"
        prof_src = extracted / "profiles" / PROFILE
        prof_dst = recover / "profiles" / PROFILE
        prof_dst.mkdir(parents=True)
        shutil.copy2(prof_src / "settings.json", prof_dst / "settings.json")
        shutil.copy2(prof_src / "secrets.env", prof_dst / "secrets.env")
        shutil.copytree(prof_src / "body", prof_dst / "body")
        shutil.copytree(prof_src / "state", recover / "state")
        (recover / "data").mkdir(parents=True)
        shutil.copy2(extracted / "data" / "local_state.txt",
                     recover / "data" / "local_state.txt")
        state_root = recover / "state"

        with profile_env(AIDOG_PROFILES_DIR=recover / "profiles",
                         AIDOG_PROFILE=PROFILE,
                         AIDOG_MC_STATE=state_root):
            mf.invalidate_cache()
            check("d1 restore: config, memory, sessions and ledger present "
                  "under the new profile",
                  (prof_dst / "settings.json").is_file()
                  and (prof_dst / "secrets.env").is_file()
                  and (prof_dst / "body" / "memory" / "MEMORY_curated.md")
                  .is_file()
                  and (state_root / "sessions" / f"{SESSION_STEM}.jsonl")
                  .is_file()
                  and (state_root / "memforget" / "tombstones.jsonl").is_file())
            settings_path = pp.settings_path()
            state_path = pp.state_dir()
            slots = pp.memory_slots()
            check("d2 settings path resolves under the restored profile",
                  settings_path == prof_dst / "settings.json")
            check("d3 state root resolves to the independent "
                  "AIDOG_MC_STATE", state_path == state_root)
            check("d4 memory slots resolve under the restored profile body",
                  all(slot.is_relative_to(prof_dst / "body" / "memory")
                      for slot in slots))
            check("d5 every resolved path is independent of the original "
                  "install and of the production tree",
                  not settings_path.is_relative_to(install)
                  and not state_path.is_relative_to(install)
                  and not settings_path.is_relative_to(ROOT)
                  and not state_path.is_relative_to(ROOT)
                  and not any(slot.is_relative_to(install) or
                              slot.is_relative_to(ROOT) for slot in slots))
            scope = mf.profile_scope()
            os.environ["AIDOG_PROFILE"] = "Bad Name!"
            try:
                pp.profile_name()
                fail_closed = False
            except pp.ProfileError:
                fail_closed = True
            os.environ["AIDOG_PROFILE"] = PROFILE
            check("d6 profile scope is the profile NAME (ids portable "
                  "across paths) and an invalid name fails closed",
                  scope == PROFILE and fail_closed)
            restored_ledger = (state_root / "memforget"
                               / "tombstones.jsonl").read_bytes()
            check("d7 restored ledger bytes equal the backed-up ledger",
                  restored_ledger
                  == (extracted / "profiles" / PROFILE / "state"
                      / "memforget" / "tombstones.jsonl").read_bytes())

            # --------------------------------------------------- (e) -----
            # ② memory + transcript suppression, from the restored ledger.
            mem_path = prof_dst / "body" / "memory" / "MEMORY_curated.md"
            mem_text = mem_path.read_text(encoding="utf-8")
            filtered = mf.filter_slot(mem_path, mem_text)
            filtered_explicit = mf.filter_slot(mem_path, mem_text,
                                               state=state_root)
            check("e1 suppression survives the restore: filter_slot drops "
                  "the forgotten entry",
                  FORGOTTEN_MEMORY not in filtered and filtered != mem_text)
            check("e2 suppression keeps the entries the owner kept",
                  KEPT_MEMORY in filtered)
            check("e3 env-resolved and explicit state root filter alike",
                  filtered == filtered_explicit)
            rows = mf.load_rows()
            check("e4 both tombstones (curated + session) travelled with "
                  "the backup",
                  len(rows) == 2
                  and {row["channel"] for row in rows}
                  == {mf.CHANNEL_CURATED_MEMORY, mf.CHANNEL_SESSION},
                  str(sorted(row["channel"] for row in rows)))
            identity = local["identity"]
            session_row = next(row for row in rows
                               if row["channel"] == mf.CHANNEL_SESSION)
            check("e5 the tombstone id stays valid under the restored "
                  "profile scope",
                  session_row["id"] == mf.source_id(mf.CHANNEL_SESSION,
                                                    identity))
            transcript_path = (state_root / "sessions"
                               / f"{SESSION_STEM}.jsonl")
            raw_before = transcript_path.read_bytes()
            docs = [json.loads(line) for line in
                    raw_before.decode("utf-8").splitlines()]
            messages = docs[1:]
            suppression = session.message_suppression(
                directory=transcript_path.parent)
            visible = session.visible_session_messages(
                messages, directory=transcript_path.parent)
            check("e6 the transcript read path drops the forgotten message "
                  "and keeps the reply",
                  not suppression.empty
                  and len(visible) == len(messages) - 1
                  and [m.get("content") for m in visible] == [KEPT_MESSAGE])
            check("e7 the transcript file itself is never rewritten",
                  transcript_path.read_bytes() == raw_before)

            # ① first reply — isolated child process, loopback mock model.
            sessions_before = set(
                (state_root / "sessions").glob("*.jsonl"))
            child_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            smoke = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()),
                 "--chat-smoke", str(recover / "profiles"), str(state_root)],
                env=child_env, capture_output=True, text=True, timeout=300)
            smoke_out = smoke.stdout + smoke.stderr
            check("e8 first reply through the restored profile "
                  "(isolated chat smoke child)",
                  smoke.returncode == 0
                  and "SMOKE-OK first-reply" in smoke.stdout,
                  smoke_out[-2000:])
            new_sessions = set((state_root / "sessions").glob("*.jsonl")) \
                - sessions_before
            check("e9 the chat smoke wrote its session under the "
                  "independent state root",
                  any(path.name == "web.jsonl"
                      and path.is_relative_to(state_root)
                      for path in new_sessions),
                  str(sorted(p.name for p in new_sessions)))
            secrets_restored = (prof_dst / "secrets.env").read_text(
                encoding="utf-8")
            check("e10 the restored profile carries the chat fixture slots",
                  all(slot in secrets_restored for slot in
                      ("WEB_TOKEN=", "OPENAI_API_TOKEN=", "MODEL_API_KEY=")))

        # ------------------------------------------------------- (f) -----
        check("f1 the original installation still exists and is "
              "byte-identical (rollback target intact)",
              install.is_dir() and worktree_digest(install)
              == baseline_digest)
        check("f2 the original HEAD is unchanged",
              git("rev-parse", "HEAD", cwd=install).stdout.strip()
              == baseline_head)

        # ------------------------------------------------------- (g) -----
        updater = (install / "deploy" / "aidog_update.py").read_bytes()
        check("g1 the shipped updater is fast-forward-only",
              b"--ff-only" in updater
              and b"--force" not in updater
              and b"reset --hard" not in updater
              and b"--no-ff" not in updater)
        banned = [cmd for cmd in GIT_LOG
                  if any(token in cmd for token in
                         ("--force", "--no-ff", "reset"))]
        check("g2 no drill command forced anything "
              "(force / no-ff / reset scan over every git call)",
              not banned, str(banned))
        check("g3 the fixture remote refs are unchanged by the whole drill",
              git("ls-remote", "--refs", str(remote)).stdout
              == baseline_remote)
        check("g4 audit hook: no network or unexpected child process "
              "attempted", not BLOCKED, str(BLOCKED))
    except Exception:  # noqa: BLE001 — a drill must REPORT, not crash
        keep = True
        traceback.print_exc()
        check("drill completed without an internal error", False,
              "exception raised, see traceback above")
    finally:
        if keep or FAIL:
            print(f"\nfixture kept for inspection: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------- the chat smoke -----
def chat_smoke(profiles_dir: Path, state_dir: Path) -> int:
    """Child mode: the FIRST REPLY through the restored profile.

    Isolated in its own process because web/server.py inserts its repo root
    at sys.path[0] on import; the sandbox below is a copy of lib/mycore/web
    plus overlay/locales and VERSION, so even a hard-coded repo-relative
    path cannot reach the production tree."""
    sys.addaudithook(make_audit("child"))
    failures: list[str] = []

    def smoke(name: str, ok: bool, detail: str = "") -> None:
        print(("SMOKE-OK " if ok else "SMOKE-FAIL ") + name
              + (f"  ({detail})" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    try:
        with tempfile.TemporaryDirectory(prefix="aidog-drill-chat-") as tmp:
            sandbox = Path(tmp) / "repo"
            for name in ("lib", "mycore", "web"):
                (sandbox / name).mkdir(parents=True)
                for file in (ROOT / name).glob("*.py"):
                    shutil.copyfile(file, sandbox / name / file.name)
            shutil.copytree(ROOT / "overlay/locales",
                            sandbox / "overlay" / "locales")
            shutil.copyfile(ROOT / "VERSION", sandbox / "VERSION")

            profile = Path(profiles_dir) / PROFILE
            tokens = dict(line.split("=", 1)
                          for line in (profile / "secrets.env")
                          .read_text(encoding="utf-8").splitlines()
                          if "=" in line)
            settings_file = profile / "settings.json"
            restored_settings = json.loads(settings_file.read_text(
                encoding="utf-8"))

            with serve(MockOpenAI) as upstream:
                upstream.calls = []
                # Pin only base_url to the loopback mock; every other
                # restored key must survive untouched.
                restored_settings.setdefault("mycore", {})
                base_url = restored_settings["mycore"].get("base_url")
                smoke("restored settings present before the smoke",
                      isinstance(base_url, str))
                model = restored_settings["mycore"].get("model", MODEL)
                restored_settings["mycore"]["base_url"] = \
                    f"http://{HOST}:{upstream.server_port}/v1"
                settings_file.write_text(
                    json.dumps(restored_settings) + "\n", encoding="utf-8")

                with patch_env({"HOME": tmp, "USERPROFILE": tmp,
                                "XDG_CONFIG_HOME": tmp, "XDG_DATA_HOME": tmp,
                                "XDG_CACHE_HOME": tmp,
                                "AIDOG_PROFILES_DIR": str(profiles_dir),
                                "AIDOG_PROFILE": PROFILE,
                                "AIDOG_MC_STATE": str(state_dir),
                                "AIDOG_WEB_PORT": "0",
                                "MYCORE_STREAM_READ_TIMEOUT": "5"}):
                    saved_path = list(sys.path)
                    spec = importlib.util.spec_from_file_location(
                        "drill_smoke_server", sandbox / "web" / "server.py")
                    server = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(server)
                    # Undo the REPO-at-sys.path[0] insertion; the sandbox
                    # root stays resolvable at the END so a late import can
                    # only ever resolve to the sandbox, never production.
                    sys.path[:] = saved_path + [str(sandbox)]

                    with serve(server.Handler) as web:
                        port = web.server_port
                        status, body, ctype = request(
                            port, "/api/chat", {"text": CHAT_PROMPT}, None)
                        smoke("console endpoint rejects a missing token",
                              status == 401, f"status={status}")
                        status, body, ctype = request(
                            port, "/api/chat", {"text": CHAT_PROMPT},
                            WEB_TOKEN)
                        smoke("console endpoint accepted the restored "
                              "WEB_TOKEN",
                              status == 200
                              and ctype == "text/event-stream",
                              f"status={status} ctype={ctype}")
                        frames = sse_frames(body)
                        smoke("first reply: streaming frames arrive",
                              [event for event, _ in frames]
                              == ["meta", "delta", "delta", "done"],
                              str([event for event, _ in frames]))
                        smoke("first reply: model echoes the settings model",
                              frames and frames[0][1].get("model") == model,
                              json.dumps(frames[:1]))
                        smoke("first reply: streamed text and final payload",
                              tuple(data["text"] for _, data in frames[1:-1])
                              == PIECES
                              and (frames[-1][1]["text"], frames[-1][1]["stop"])
                              == (REPLY, "final"),
                              json.dumps(frames[-1][1])[:300])
                        smoke("loopback mock served the call with the "
                              "fixture model key",
                              len(upstream.calls) >= 1
                              and all(path == "/v1/chat/completions"
                                      and auth == f"Bearer {MODEL_KEY}"
                                      and data.get("model") == model
                                      for path, auth, data in
                                      upstream.calls))
                        session_path = server.chat_session_path()
                        smoke("session landed under the independent state "
                              "root", session_path.is_relative_to(state_dir),
                              str(session_path))
                        if session_path.is_file():
                            header, user, assistant = map(
                                json.loads,
                                session_path.read_text(
                                    encoding="utf-8").splitlines())
                            smoke("session transcript records the exchange",
                                  header["meta"]["model"] == model
                                  and (user["role"], user["content"])
                                  == ("user", CHAT_PROMPT)
                                  and (assistant["role"], assistant["content"])
                                  == ("assistant", REPLY))
                        else:
                            smoke("session transcript records the exchange",
                                  False, "no session file")
                smoke("no unexpected network or child process in the smoke",
                      not BLOCKED, str(BLOCKED))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print("SMOKE-FAIL exception (see traceback)")
        return 1
    if failures:
        print(f"SMOKE RESULT: {len(failures)} failure(s)")
        return 1
    print("SMOKE-OK first-reply")
    return 0


@contextmanager
def patch_env(values: dict):
    """Set the listed env vars for one block, then restore exactly."""
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def request(port: int, path: str, payload: dict,
            token: str | None) -> tuple[int, bytes, str]:
    """POST one console request; return (status, body, content-type)."""
    conn = http.client.HTTPConnection(HOST, port, timeout=10)
    try:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request("POST", path, json.dumps(payload), headers)
        response = conn.getresponse()
        body = response.read()
        return response.status, body, response.headers.get_content_type()
    finally:
        conn.close()


# ------------------------------------------------------------------ entry ----
def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--chat-smoke":
        return chat_smoke(Path(sys.argv[2]), Path(sys.argv[3]))
    sys.addaudithook(make_audit("parent"))
    drill()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name in FAIL:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
