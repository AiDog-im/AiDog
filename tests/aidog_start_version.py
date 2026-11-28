#!/usr/bin/env python3
"""Offline behavior test for `boot_services.sh start` self-repair.

Two defects are locked here, both found during the v0.5.111 release:

1. Version sync — `aidog update apply` calls boot_services.sh (start verb),
   but start used to report "already running" for a live session, so the
   console kept running the OLD build while the updater claimed a restart.
   start now compares the running build's /api/health `version` against the
   VERSION file and recycles OUR session once on mismatch.
2. Foreign port holder — a foreign process squatting on the console port is
   reported by pid and never killed; start fails honestly.

The fixture is self-contained under /tmp: the REAL boot_services.sh runs
against a throwaway repo with a tmux/pgrep shim on PATH, so no real session,
console or process of this machine is touched. No network (loopback only).
Run: python3 tests/aidog_start_version.py
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TMUX_SHIM = r'''#!/usr/bin/env python3
"""Fake tmux: sessions are files under $FAKE_TMUX_STATE; only aidog_web is
really executed (so the fixture console comes up), everything else is recorded."""
import os, signal, subprocess, sys, time
from pathlib import Path
state = Path(os.environ["FAKE_TMUX_STATE"])
state.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
if not args:
    sys.exit(0)
cmd, rest = args[0], args[1:]
def opt(name):
    return rest[rest.index(name) + 1] if name in rest else ""
if cmd == "has-session":
    sys.exit(0 if (state / opt("-t")).exists() else 1)
if cmd == "new":
    name = opt("-s")
    command = rest[-1] if rest else ""
    if (state / "fail_new").exists():
        sys.exit(1)
    (state / name).write_text("", encoding="utf-8")
    if name == "aidog_web":
        proc = subprocess.Popen(command, shell=True, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.4)
        if proc.poll() is not None:          # died at once (e.g. port busy)
            (state / (name + ".pid")).unlink(missing_ok=True)
            (state / name).unlink(missing_ok=True)
        else:
            (state / (name + ".pid")).write_text(str(proc.pid), encoding="utf-8")
    sys.exit(0)
if cmd == "kill-session":
    name = opt("-t")
    pidf = state / (name + ".pid")
    if pidf.exists():
        try:
            pid = int(pidf.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = 0
        if pid:
            # `new` used shell=True + start_new_session, so pid is the group
            # leader: kill the WHOLE group or the python child survives and
            # keeps holding the port (that is exactly what tmux does).
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
        pidf.unlink(missing_ok=True)
    (state / name).unlink(missing_ok=True)
    sys.exit(0)
if cmd == "display-message":
    print("999999")
    sys.exit(0)
sys.exit(0)
'''

PGREP_SHIM = "#!/bin/sh\nexit 1\n"

CONSOLE = r'''#!/usr/bin/env python3
"""Fixture console: /api/health reports the VERSION file (like the real one,
which reads VERSION at import)."""
import json, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
PORT = int(sys.argv[1])

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", "version": VERSION,
                           "ts": time.time()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

HTTPServer(("127.0.0.1", PORT), H).serve_forever()
'''


def build_fixture(root: Path, port: int) -> None:
    (root / "deploy").mkdir(parents=True, exist_ok=True)
    (root / "web").mkdir(parents=True, exist_ok=True)
    (root / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "deploy" / "boot_services.sh", root / "deploy" / "boot_services.sh")
    (root / "deploy" / "run_service.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (root / "web" / "server.py").write_text(
        CONSOLE.replace("sys.argv[1]", str(port)), encoding="utf-8")
    tmux = root / "bin" / "tmux"
    tmux.write_text(TMUX_SHIM, encoding="utf-8")
    tmux.chmod(0o755)
    pgrep = root / "bin" / "pgrep"
    pgrep.write_text(PGREP_SHIM, encoding="utf-8")
    pgrep.chmod(0o755)


def run_start(root: Path, state: Path, port: int) -> tuple[int, str]:
    env = dict(os.environ)
    env.update({
        "PATH": f"{root / 'bin'}:{env.get('PATH', '')}",
        "FAKE_TMUX_STATE": str(state),
        "AIDOG_WEB_PORT": str(port),
        "AIDOG_BOOT_SETTLE_SEC": "1",
        "AIDOG_REPAIR_GRACE": "3",
    })
    for key in ("AIDOG_MC_STATE", "AIDOG_PROFILE", "AIDOG_PROFILES_DIR"):
        env.pop(key, None)
    proc = subprocess.run(["bash", str(root / "deploy" / "boot_services.sh"), "start"],
                          capture_output=True, text=True, env=env, timeout=90)
    return proc.returncode, proc.stdout + proc.stderr


def health(port: int) -> dict:
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=4) as r:
        return json.load(r)


def cleanup_fixture(state: Path, root: Path) -> None:
    """Kill anything the fixture spawned. The temp dir is about to vanish,
    which would otherwise leave orphaned consoles holding their ports."""
    pidf = state / "aidog_web.pid"
    if pidf.exists():
        try:
            pid = int(pidf.read_text(encoding="utf-8").strip())
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ValueError, OSError, ProcessLookupError):
            pass
        pidf.unlink(missing_ok=True)
    prefix = str(root.parent)
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cwd = os.readlink(f"/proc/{entry.name}/cwd")
        except OSError:
            continue
        if cwd.startswith(prefix):
            try:
                os.kill(int(entry.name), signal.SIGTERM)
            except OSError:
                pass


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="aidog_start_ver_") as td:
        root = Path(td) / "install"
        state = Path(td) / "tmux_state"
        port = free_port()
        build_fixture(root, port)
        try:

            # ---- 1. fresh start brings the console up on the tree version ----
            rc, out = run_start(root, state, port)
            check("v1 fresh start exits 0 and reports all green",
                  rc == 0 and "all green" in out, out[-300:])
            check("v2 fresh console reports the tree version",
                  health(port).get("version") == "1.0.0", str(health(port)))
            check("v3 no recycle on a matching version",
                  "recycling" not in out, out[-300:])

            # ---- 2. VERSION bump on disk -> start must recycle to the new code ----
            (root / "VERSION").write_text("2.0.0\n", encoding="utf-8")
            rc, out = run_start(root, state, port)
            check("v4 stale build is detected and recycled",
                  rc == 0 and "recycling to load the new code" in out, out[-400:])
            check("v5 console now reports the new tree version",
                  health(port).get("version") == "2.0.0", str(health(port)))
            check("v6 recycle names both versions honestly",
                  "running 1.0.0 but tree is 2.0.0" in out, out[-400:])

            # ---- 3. idempotent: a second start recycles nothing ----
            rc, out = run_start(root, state, port)
            check("v7 second start is a no-op (idempotent)",
                  rc == 0 and "recycling" not in out and "all green" in out, out[-300:])

            # ---- 4. foreign holder on the port: reported, never killed, rc != 0 ----
            (state / "aidog_web").unlink(missing_ok=True)
            pidf = state / "aidog_web.pid"
            if pidf.exists():
                import signal as _sig
                try:
                    _pid = int(pidf.read_text().strip())
                    os.killpg(os.getpgid(_pid), _sig.SIGTERM)   # group: shell + python
                except (ProcessLookupError, ValueError, PermissionError, OSError):
                    try:
                        os.kill(_pid, _sig.SIGTERM)
                    except (ProcessLookupError, ValueError, OSError):
                        pass
                pidf.unlink(missing_ok=True)
            import time as _t
            for _ in range(30):                      # wait for the port to free
                with socket.socket() as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        probe.bind(("127.0.0.1", port))
                        break
                    except OSError:
                        _t.sleep(0.25)
            import http.server, threading, time as _time
            class Squatter(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(404)
                    self.end_headers()
                def log_message(self, *a):
                    pass
            foreign = http.server.HTTPServer(("127.0.0.1", port), Squatter)
            t = threading.Thread(target=foreign.serve_forever, daemon=True)
            t.start()
            _time.sleep(0.5)
            try:
                rc, out = run_start(root, state, port)
                check("v8 foreign holder makes start fail honestly (rc != 0)",
                      rc != 0, f"rc={rc}")
                check("v9 foreign holder is reported, never killed",
                      "held by ANOTHER process" in out and "NOT killed" in out,
                      out[-400:])
                check("v10 no false 'all green' while a squatter holds the port",
                      "all green" not in out, out[-300:])
            finally:
                foreign.shutdown()
                foreign.server_close()
                _time.sleep(0.5)

            # ---- 5. after the squatter leaves, start recovers ----
            rc, out = run_start(root, state, port)
            check("v11 start recovers once the port is free",
                  rc == 0 and "all green" in out, out[-300:])

        finally:
            cleanup_fixture(state, root)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
