#!/usr/bin/env python3
"""Console backup -> restore round trip for MEMORY SUPPRESSION (D5/D6).

What this proves, end to end, on a real machine:

  * the shipped acceptance criterion "Restoring a backup taken before the
    suppression keeps the item suppressed" (docs/design/
    memory_suppression_design.md, D5/D6 + criterion 7) holds through the
    REAL console endpoints — Bearer-authenticated GET /api/backup/export and
    POST /api/backup/restore — never through a direct function call;
  * the suppression set TRAVELS with the backup: a payload exported AFTER the
    forget carries the tombstone set, and a restore into a DIFFERENT profile
    keeps the source suppressed, matched by CONTENT DIGEST (the opaque id
    embeds the profile scope, so the id cannot match across profiles);
  * neither the ledger, nor the travelling tombstone set, nor the restore
    response ever carries the suppressed excerpt or a credential;
  * the restore RESPONSE now says what the suppression did — how many
    entries the LOCAL ledger filtered, how many the TRAVELLING set
    filtered, how many entries were filtered in total, and whether any
    set was in force at all (D17) — and it states the honest cross-machine
    limit as a warning line when neither a travelling set nor a local
    ledger was there to filter with (D18).

Isolation rules honoured here:
  * the console runs from a THROWAWAY SANDBOX (a copy of lib/ + mycore/ +
    web/ + overlay/locales + VERSION) — the repository tree, the production
    profile, the production state dir and the repo's curated memory are never
    written; the sandbox's web/.state.json absorbs the boot record;
  * every profile is a throwaway one under a temp profiles root, with its own
    state root (AIDOG_MC_STATE) and its own secrets.env — no production
    secrets.env is read, no production secret is written;
  * the network guard (sitecustomize.py in the sandbox) allows loopback only
    and records every other attempt — the run fails if anything reaches out;
  * no product file is modified by this test; the only new file is this one.

Run:  python3 tests/console_restore_suppression.py     (exit 0 = all green)
English output per repo i18n policy.
"""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
RESERVED_PORTS = {19167, 3460}          # the production console + shared proxy

# Fixture credentials (throwaway, profile-local, never printed).
TOKEN_A = "E2E-SUPPRESS-RESTORE-TOKEN-ALPHA"
TOKEN_B = "E2E-SUPPRESS-RESTORE-TOKEN-BETA"
TOKEN_C = "E2E-SUPPRESS-RESTORE-TOKEN-GAMMA"

# The two learned-memory entries. ALPHA is the one that gets forgotten;
# BETA is the control that must survive every restore.
TEXT_A = "E2E suppression canary alpha, the owner asked for a quiet morning briefing"
TEXT_B = "E2E suppression canary beta, the owner asked for a weekly digest at eight"

PROFILE_A = "suppressa"
PROFILE_B = "suppressb"
PROFILE_C = "suppressc"

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


def api(base: str, method: str, path: str, body=None, token: str | None = None,
        timeout: int = 20) -> tuple[int, object, str]:
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


SITECUSTOMIZE = r'''"""Loopback-only guard for the isolated console (test seam, not product)."""
import os
import sys

_LOG = os.environ.get("AIDOG_TEST_NETLOG", "")


def _record(event: str, detail: str) -> None:
    if not _LOG:
        return
    with open(_LOG, "a", encoding="utf-8") as handle:
        handle.write(f"{event} {detail}\n")


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
    """A throwaway profile: own body, own state, own secrets, own settings."""
    profile = profiles / name
    (profile / "body" / "memory").mkdir(parents=True)
    (profile / "body" / "evolution").mkdir(parents=True)
    (profile / "state").mkdir(parents=True)
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

    def isolated_env(self) -> dict[str, str]:
        return {
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

    def start(self, timeout: float = 60.0) -> bool:
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=str(self.sandbox / "web"),
            env=self.isolated_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
    def export(self, include: str = "memory,settings", token: str | None = None):
        return api(self.base, "GET", f"/api/backup/export?include={include}",
                   token=self.token if token is None else token)

    def restore(self, envelope: dict, token: str | None = None):
        return api(self.base, "POST", "/api/backup/restore", body=envelope,
                   token=self.token if token is None else token)

    def learn(self, text: str):
        return api(self.base, "POST", "/api/learn",
                   body={"kind": "memory", "text": text}, token=self.token)


def ledger_rows(state: Path) -> list[dict]:
    """The tombstone ledger rows of one profile state root (real module)."""
    sys.path.insert(0, str(ROOT))
    try:
        from mycore import memforget
        return memforget.load_rows(state=state)
    finally:
        if sys.path[0] == str(ROOT):
            sys.path.pop(0)


def record_tombstone(state: Path, profile: str, text: str) -> dict:
    """Record one tombstone through mycore.memforget.forget (the real call).

    The profile seams are set exactly like the console's, so the recorded id
    and the state root are the ones the console itself will resolve."""
    saved = {key: os.environ.get(key) for key in
             ("AIDOG_PROFILE", "AIDOG_MC_STATE", "AIDOG_PROFILES_DIR")}
    os.environ["AIDOG_PROFILE"] = profile
    os.environ["AIDOG_MC_STATE"] = str(state)
    os.environ["AIDOG_PROFILES_DIR"] = str(state.parent.parent)
    sys.path.insert(0, str(ROOT))
    try:
        from mycore import memforget
        return memforget.forget(memforget.CHANNEL_LEARNED, text)
    finally:
        sys.path.pop(0)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def source_id_for(profile: str, text: str) -> str:
    """The opaque source id THIS profile would compute for the source.

    The id embeds the profile scope, so two profiles never agree on it —
    which is exactly why a travelling set has to match by CONTENT DIGEST."""
    saved = os.environ.get("AIDOG_PROFILE")
    os.environ["AIDOG_PROFILE"] = profile
    sys.path.insert(0, str(ROOT))
    try:
        from mycore import memforget
        return memforget.source_id(memforget.CHANNEL_LEARNED, text)
    finally:
        sys.path.pop(0)
        if saved is None:
            os.environ.pop("AIDOG_PROFILE", None)
        else:
            os.environ["AIDOG_PROFILE"] = saved


def envelope(payload: dict, apply_sections: dict) -> dict:
    """The restore envelope (schema + version + payload + apply)."""
    return {"schema": "aidog.backup.v1", "schemaVersion": "1",
            "payload": payload, "apply": apply_sections}


def learned_text(profile: Path) -> str:
    path = profile / "body" / "memory" / "EVOLUTION_learned.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def check_learn_restore_contention(console: Console, old_memory: str) -> None:
    """Hold the kernel writer's lock until both HTTP calls have returned."""
    slot = "EVOLUTION_learned.md"
    newer = "The owner prefers a brief offline contention report on Fridays."
    status, body, _ = console.learn(newer)
    before = learned_text(console.profile)
    check("K1 learn writes a newer entry before restore",
          status == 200 and (body or {}).get("ok") is True and newer in before)
    restore = envelope({"memory": {slot: old_memory}}, {"memory": True})
    worker_code = """
import sys
from mycore import evolution
with evolution.mutation_lock(evolution.learned_memory_path()):
    print('locked', flush=True)
    sys.stdin.read(1)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", worker_code], cwd=console.sandbox,
        env=console.isolated_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(holder.stdout, selectors.EVENT_READ)
            ready = bool(selector.select(10)) and holder.stdout.readline() == b"locked\n"
        if not check("K2 another process holds the kernel writer's lock", ready):
            return
        status, body, _ = console.learn("A separate entry must wait for the active writer.")
        check("K3 learn refuses the held lock",
              status == 400 and (body or {}).get("details") == ["io"], str(body))
        status, body, _ = console.restore(restore)
        memory = (body or {}).get("memory", {})
        check("K4 restore reports the contended file without changing the slot",
              status == 503 and not memory.get("files")
              and any(f.get("name") == slot and "lock" in f.get("reason", "")
                      for f in memory.get("failed", []))
              and learned_text(console.profile) == before, str(memory))
    finally:
        holder.stdin.close()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait()
        holder.stdout.close()
    status, body, _ = console.restore(restore)
    check("K5 retry after release restores the whole file and replaces the newer entry",
          status == 200 and (body or {}).get("memory", {}).get("files") == [slot]
          and learned_text(console.profile) == old_memory)


# ------------------------------------------------------------------ main ---
def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="aidog-suppress-restore-"))
    netlog = root / "network.log"
    consoles: list[Console] = []
    prod_before = {name: sha256(ROOT / "overlay" / "memory" / name)
                   for name in ("MEMORY_curated.md", "USER_curated.md")}
    prod_learned_before = sha256(ROOT / "overlay" / "memory" / "EVOLUTION_learned.md")
    default_ledger = (ROOT / "data" / "mycore_service" / "memforget"
                      / "tombstones.jsonl")
    default_ledger_before = sha256(default_ledger)
    try:
        sandbox = build_sandbox(root)
        profiles = root / "profiles"
        profiles.mkdir()
        for name, token in ((PROFILE_A, TOKEN_A), (PROFILE_B, TOKEN_B),
                            (PROFILE_C, TOKEN_C)):
            make_profile(profiles, name, token)

        # ---------------------------------------------------- boot + auth ---
        con_a = Console(sandbox, profiles, PROFILE_A, TOKEN_A, netlog)
        consoles.append(con_a)
        check("B1 the isolated console boots on its own loopback port",
              con_a.start(), "boot failed")
        if not con_a.proc or con_a.proc.poll() is not None:
            print(con_a.stderr[-2000:])
            return 1
        status, _, _ = api(con_a.base, "GET", "/api/backup/export")
        check("B2 GET /api/backup/export without a Bearer token -> 401",
              status == 401, f"status={status}")
        status, _, _ = con_a.export(token="wrong-token-value")
        check("B3 GET /api/backup/export with a wrong Bearer token -> 401",
              status == 401, f"status={status}")
        status, _, _ = api(con_a.base, "POST", "/api/backup/restore",
                           body=envelope({}, {"memory": True}))
        check("B4 POST /api/backup/restore without a Bearer token -> 401",
              status == 401, f"status={status}")

        # ------------------------------------------ the two learned entries --
        status, body, _ = con_a.learn(TEXT_A)
        check("C1 the console learn route accepted the first entry",
              status == 200 and isinstance(body, dict) and body.get("ok") is True,
              f"status={status} body={body}")
        status, body, _ = con_a.learn(TEXT_B)
        check("C2 the console learn route accepted the second entry",
              status == 200 and isinstance(body, dict) and body.get("ok") is True,
              f"status={status} body={body}")
        learned = learned_text(profiles / PROFILE_A)
        check("C3 the learned file holds both entries before any suppression",
              TEXT_A in learned and TEXT_B in learned, "learned file content")

        # ------------------------------------------------- the OLD payload ---
        status, env_old, raw_old = con_a.export("memory,settings")
        payload_old = (env_old or {}).get("payload") or {}
        check("C4 the export envelope is the backup contract",
              status == 200 and (env_old or {}).get("schema") == "aidog.backup.v1"
              and (env_old or {}).get("schemaVersion") == "1",
              f"status={status}")
        check("C5 the OLD payload carries the memory section",
              TEXT_A in (payload_old.get("memory") or {}).get(
                  "EVOLUTION_learned.md", ""),
              "learned slot missing from the payload")
        check("C6 the OLD payload carries NO tombstone section "
              "(nothing was forgotten yet)",
              "tombstones" not in payload_old,
              f"tombstones={payload_old.get('tombstones')}")

        check_learn_restore_contention(
            con_a, (payload_old.get("memory") or {}).get("EVOLUTION_learned.md", ""))

        # ------------------------------------------------ record a tombstone --
        result = record_tombstone(profiles / PROFILE_A / "state", PROFILE_A, TEXT_A)
        check("C7 mycore.memforget.forget recorded the tombstone",
              result.get("ok") is True and result.get("already") is False
              and str(result.get("id", "")).startswith("src_"),
              f"result={result}")
        rows = ledger_rows(profiles / PROFILE_A / "state")
        check("C8 the ledger holds exactly one row",
              len(rows) == 1, f"rows={len(rows)}")
        check("C9 the ledger row carries the channel and the digest, "
              "not the text",
              bool(rows) and rows[0].get("channel") == "learned"
              and rows[0].get("digest") and TEXT_A not in json.dumps(rows[0]),
              f"row={rows[0] if rows else None}")
        ledger_raw = (profiles / PROFILE_A / "state" / "memforget"
                      / "tombstones.jsonl").read_text(encoding="utf-8")
        check("C10 the ledger file carries no excerpt, no path and no credential",
              TEXT_A not in ledger_raw and TOKEN_A not in ledger_raw
              and "EVOLUTION_learned.md" not in ledger_raw
              and str(profiles) not in ledger_raw,
              "ledger leak")
        check("C11 the recorded id is opaque (no excerpt inside it)",
              TEXT_A[:20] not in result.get("id", ""), "id leaks the text")

        # ------------- the payload that TRAVELS (exported after the forget) --
        # Exported BEFORE the case-1 restore: a tombstone never deletes the
        # source from the file (D1), so the raw bytes are still there and the
        # payload carries BOTH the content and the set. That is what makes
        # the case-2 suppression attributable to the TRAVELLING SET alone.
        status, env_new, raw_new = con_a.export("memory,settings")
        payload_new = (env_new or {}).get("payload") or {}
        travelling = payload_new.get("tombstones")

        # ------------------- CASE 1: restore the OLD payload, same profile ---
        status, body, raw = con_a.restore(envelope(payload_old, {"memory": True}))
        check("C12 the restore of the OLD payload returned 200/ok",
              status == 200 and isinstance(body, dict) and body.get("ok") is True,
              f"status={status} body={body}")
        memory_out = (body or {}).get("memory") or {}
        check("C13 the restore reports the memory section and no failure",
              memory_out.get("files") == ["EVOLUTION_learned.md"]
              and memory_out.get("failed") == [],
              f"memory={memory_out}")
        check("C14 the restore response carries no excerpt and no credential",
              TEXT_A not in raw and TEXT_B not in raw and TOKEN_A not in raw,
              "response leak")
        learned_after = learned_text(profiles / PROFILE_A)
        check("C15 THE CRITERION: the forgotten source is still absent after "
              "the restore of a backup taken BEFORE the suppression",
              TEXT_A not in learned_after, "the forgotten source came back")
        check("C16 the non-forgotten content was restored",
              TEXT_B in learned_after, "the kept source is missing")
        rows_after = ledger_rows(profiles / PROFILE_A / "state")
        check("C17 the ledger survived the restore",
              len(rows_after) == 1 and rows_after[0].get("id") == rows[0].get("id"),
              f"rows={rows_after}")
        if "tombstones" not in memory_out:
            note("O1 the restore response of an OLD payload carries NO "
                 "tombstone section: the set that travels with a backup is "
                 "absent from a backup taken before the forget existed, so "
                 "the suppression here came from the LOCAL ledger (the "
                 "filter still applied — C15 passed). The response does not "
                 "tell the operator that a section was suppressed.")
        else:
            note("O1 the restore response of an OLD payload carried a "
                 f"tombstone section: {memory_out.get('tombstones')}")

        # ---- R: the restore RESPONSE reports the suppression it applied ----
        sup_c = memory_out.get("suppression")
        check("R1 the restore response carries a suppression report",
              isinstance(sup_c, dict), f"suppression={sup_c}")
        check("R2 the report counts the entry the LOCAL ledger filtered, the "
              "one the TRAVELLING set filtered and the merged count",
              isinstance(sup_c, dict)
              and sup_c.get("filtered_local") == 1
              and sup_c.get("filtered_travelling") == 0
              and sup_c.get("filtered_merged") == 1
              and sup_c.get("applied") is True
              and sup_c.get("local_set_size") == 1
              and sup_c.get("travelling_set_size") == 0,
              f"suppression={sup_c}")
        check("R3 the report states the suppression in plain words",
              isinstance(sup_c, dict)
              and "suppression applied" in str(sup_c.get("note"))
              and "ledger" in str(sup_c.get("note"))
              and "travelled" in str(sup_c.get("note")),
              f"note={sup_c.get('note') if isinstance(sup_c, dict) else None}")
        check("R4 the suppression report carries no id, no excerpt, no path "
              "and no credential",
              isinstance(sup_c, dict)
              and "src_" not in json.dumps(sup_c)
              and TEXT_A not in json.dumps(sup_c)
              and TEXT_B not in json.dumps(sup_c)
              and TOKEN_A not in json.dumps(sup_c)
              and str(profiles) not in json.dumps(sup_c),
              "suppression report leak")
        check("R5 a suppression set WAS in force, so no cross-machine warning "
              "is raised",
              memory_out.get("warnings") == [],
              f"warnings={memory_out.get('warnings')}")
        check("R6 the memory section keeps its old keys (the report is ADDITIVE)",
              isinstance(memory_out.get("files"), list)
              and isinstance(memory_out.get("bytes"), int)
              and memory_out.get("failed") == []
              and set(memory_out) >= {"files", "bytes", "failed", "suppression"},
              f"keys={sorted(memory_out)}")
        check("R7 the filtered count reconciles with the filter's own output",
              isinstance(sup_c, dict) and sup_c.get("counts_exact") is True,
              f"suppression={sup_c}")

        check("D0 the travelling payload still carries the forgotten source "
              "(a tombstone deletes nothing) — so the case-2 suppression can "
              "only come from the travelling set",
              TEXT_A in (payload_new.get("memory") or {}).get(
                  "EVOLUTION_learned.md", ""),
              "the payload no longer holds the source")
        check("D1 the payload exported AFTER the forget carries the tombstone set",
              status == 200 and isinstance(travelling, list) and len(travelling) == 1,
              f"tombstones={travelling}")
        check("D2 the travelling set carries no excerpt and no credential",
              isinstance(travelling, list)
              and TEXT_A not in json.dumps(travelling)
              and TOKEN_A not in json.dumps(travelling),
              "travelling set leak")

        # ------------------ CASE 2: restore into a DIFFERENT profile (B) ----
        con_b = Console(sandbox, profiles, PROFILE_B, TOKEN_B, netlog)
        consoles.append(con_b)
        check("D3 a second console boots in a different profile",
              con_b.start(), "boot failed")
        learned_b_before = learned_text(profiles / PROFILE_B)
        check("D4 the second profile starts without the learned entries",
              TEXT_A not in learned_b_before and TEXT_B not in learned_b_before,
              "the second profile is not empty")
        status, body, raw = con_b.restore(envelope(payload_new, {"memory": True}))
        check("D5 the restore into the other profile returned 200/ok",
              status == 200 and isinstance(body, dict) and body.get("ok") is True,
              f"status={status} body={body}")
        memory_b = (body or {}).get("memory") or {}
        check("D6 the restore response REPORTS the travelling tombstone set",
              isinstance(memory_b.get("tombstones"), dict)
              and memory_b.get("tombstones", {}).get("imported") == 1
              and memory_b.get("tombstones", {}).get("skipped") == 0,
              f"tombstones={memory_b.get('tombstones')}")
        check("D7 the restore response carries no excerpt and no credential",
              TEXT_A not in raw and TEXT_B not in raw and TOKEN_B not in raw,
              "response leak")
        learned_b = learned_text(profiles / PROFILE_B)
        check("D8 THE TRAVELLING SET: the forgotten source stays suppressed in "
              "the other profile (matched by content digest)",
              TEXT_A not in learned_b, "the forgotten source came back")
        check("D9 the non-forgotten content was restored in the other profile",
              TEXT_B in learned_b, "the kept source is missing")
        rows_b = ledger_rows(profiles / PROFILE_B / "state")
        check("D10 the travelling set was merged into the other profile's ledger",
              len(rows_b) == 1 and rows_b[0].get("digest") == rows[0].get("digest"),
              f"rows={rows_b}")
        id_b = source_id_for(PROFILE_B, TEXT_A)
        check("D11 the cross-profile match is by DIGEST, not by id: the id "
              "profile B computes for the source differs from the travelled "
              "row (the id embeds the profile scope), the digest is equal",
              bool(rows_b) and id_b != rows_b[0].get("id")
              and rows_b[0].get("digest") == rows[0].get("digest"),
              f"id_b={id_b} row_id={rows_b[0].get('id') if rows_b else None}")
        check("D12 no excerpt, path or credential in the other profile's ledger",
              TEXT_A not in json.dumps(rows_b) and TOKEN_B not in json.dumps(rows_b)
              and "EVOLUTION_learned.md" not in json.dumps(rows_b),
              "ledger leak")

        # ---- R: the travelling-set restore reports its own attribution -----
        sup_b = memory_b.get("suppression")
        check("R8 the travelling-set restore reports the entry the TRAVELLING "
              "set filtered and none by the local ledger",
              isinstance(sup_b, dict)
              and sup_b.get("filtered_travelling") == 1
              and sup_b.get("filtered_local") == 0
              and sup_b.get("filtered_merged") == 1
              and sup_b.get("applied") is True
              and sup_b.get("travelling_set_size") == 1,
              f"suppression={sup_b}")
        check("R9 the travelling-set import report and the suppression report "
              "agree",
              memory_b.get("tombstones", {}).get("imported") == 1
              and isinstance(sup_b, dict)
              and sup_b.get("filtered_travelling") == 1,
              f"tombstones={memory_b.get('tombstones')} suppression={sup_b}")
        check("R10 the travelling-set restore raises no cross-machine warning",
              memory_b.get("warnings") == [],
              f"warnings={memory_b.get('warnings')}")

        # a second restore of the same payload is idempotent
        status, body, raw = con_b.restore(envelope(payload_new, {"memory": True}))
        memory_b2 = (body or {}).get("memory") or {}
        check("D13 a repeated restore is idempotent "
              "(imported=0, skipped=1) and stays suppressed",
              status == 200 and memory_b2.get("tombstones", {}).get("imported") == 0
              and memory_b2.get("tombstones", {}).get("skipped") == 1
              and TEXT_A not in learned_text(profiles / PROFILE_B),
              f"tombstones={memory_b2.get('tombstones')}")
        sup_b2 = memory_b2.get("suppression")
        check("R11 the repeated restore reports the MERGED count as the number "
              "of distinct entries filtered (1), not the sum of the two sets (2)",
              isinstance(sup_b2, dict)
              and sup_b2.get("filtered_local") == 1
              and sup_b2.get("filtered_travelling") == 1
              and sup_b2.get("filtered_merged") == 1,
              f"suppression={sup_b2}")

        # ------- CASE 3 (measurement): the OLD payload into a third profile --
        con_c = Console(sandbox, profiles, PROFILE_C, TOKEN_C, netlog)
        consoles.append(con_c)
        check("E1 a third console boots in another fresh profile",
              con_c.start(), "boot failed")
        status, body, raw = con_c.restore(envelope(payload_old, {"memory": True}))
        learned_c = learned_text(profiles / PROFILE_C)
        came_back = TEXT_A in learned_c
        check("E2 the restore of the OLD payload into a fresh profile "
              "(no local ledger) was executed",
              status == 200 and isinstance(body, dict), f"status={status}")
        if came_back:
            note("O2 an OLD backup restored into a profile that has no ledger "
                 "brings the forgotten source BACK (measured: the excerpt is "
                 "present in the restored file): the set that travels with a "
                 "backup does not exist in a backup taken before the forget. "
                 "Same-profile restores are safe (C15); a cross-machine "
                 "restore is safe only with a payload exported after the "
                 "forget (D8).")
        else:
            note("O2 an OLD backup restored into a fresh profile did NOT bring "
                 "the forgotten source back (measured: the excerpt is absent "
                 "from the restored file).")
        check("E3 the OLD payload's restore response carries no excerpt",
              TEXT_A not in raw and TOKEN_C not in raw, "response leak")

        # ---- R: no set at all -> the honest "nothing could be filtered" ----
        memory_e = (body or {}).get("memory") or {}
        sup_e = memory_e.get("suppression")
        check("R12 a restore with NO set in force says so plainly "
              "('no suppression set applied') instead of implying a clean "
              "restore",
              isinstance(sup_e, dict)
              and sup_e.get("applied") is False
              and sup_e.get("filtered_local") == 0
              and sup_e.get("filtered_travelling") == 0
              and sup_e.get("filtered_merged") == 0
              and sup_e.get("local_set_size") == 0
              and sup_e.get("travelling_set_size") == 0
              and "no suppression set applied" in str(sup_e.get("note")),
              f"suppression={sup_e}")
        warn_e = memory_e.get("warnings") or []
        check("R13 the cross-machine limit is stated as a warning line (the "
              "payload carried no set and this profile has no ledger)",
              len(warn_e) == 1
              and "not filtered" in warn_e[0]
              and "no suppression set" in warn_e[0],
              f"warnings={warn_e}")
        check("R14 the warning is raised exactly when the source came back",
              (came_back and len(warn_e) == 1) or (not came_back and not warn_e),
              f"came_back={came_back} warnings={warn_e}")
        check("R15 the new fields carry no id, no excerpt, no path and no "
              "credential",
              "src_" not in json.dumps(sup_e)
              and TEXT_A not in json.dumps(sup_e)
              and TEXT_B not in json.dumps(sup_e)
              and TOKEN_C not in json.dumps(sup_e)
              and str(profiles) not in json.dumps(sup_e)
              and "src_" not in json.dumps(warn_e),
              "leak in the new fields")

        # ------------------------------------------------------- hygiene -----
        check("H1 no fixture excerpt reached any console stderr",
              all(TEXT_A not in c.stderr and TEXT_B not in c.stderr
                  for c in consoles), "stderr leak")
        check("H2 no fixture credential reached any console stderr",
              all(TOKEN_A not in c.stderr and TOKEN_B not in c.stderr
                  and TOKEN_C not in c.stderr for c in consoles), "stderr leak")
        violations = ""
        if netlog.exists():
            violations = netlog.read_text(encoding="utf-8")
        check("H3 the loopback-only guard recorded no outbound attempt",
              violations.strip() == "", f"violations={violations.strip()[:200]}")
        prod_after = {name: sha256(ROOT / "overlay" / "memory" / name)
                      for name in ("MEMORY_curated.md", "USER_curated.md")}
        check("H4 the repository's curated memory is byte-identical",
              prod_after == prod_before, "the repo's curated memory changed")
        check("H5 the repository's learned memory is untouched",
              sha256(ROOT / "overlay" / "memory" / "EVOLUTION_learned.md")
              == prod_learned_before, "the repo's learned memory changed")
        check("H6 the production profile's tombstone ledger is unchanged",
              sha256(default_ledger) == default_ledger_before,
              "the production state dir was written")
    finally:
        for console in consoles:
            console.stop()
        shutil.rmtree(root, ignore_errors=True)

    print("")
    print(f"{PASSED}/{PASSED + FAILED} PASS")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
