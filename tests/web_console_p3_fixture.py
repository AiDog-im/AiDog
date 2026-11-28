"""Launcher and server for the disposable P3 web console smoke installation."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave


REPO = Path(__file__).resolve().parent.parent
SMOKE = Path("tests/web_console_p3.py")
FIXTURE = Path("tests/web_console_p3_fixture.py")
# Copy current source files (including uncommitted changes) without copying
# settings, credentials, runtime data, worktree metadata, or owner overlays.
SOURCE = (
    "VERSION", "release.json", "secrets.env.example", "lib/*.py", "mycore/*.py",
    "web/server.py", "web/settings.example.json", "web/static/manifest.webmanifest",
    "overlay/locales/*.json",
    "overlay/agent/CAPABILITY_MAP.md", "deploy/aidog_setup.py",
    "deploy/aidog_update.py", "deploy/bridge/README.md", "deploy/bridge/worker.js",
    "deploy/ckx/*.mjs", "docs/features/capabilities.md",
    "docs/features/openai_api_server.md", str(SMOKE), str(FIXTURE),
)


def launch() -> int:
    with tempfile.TemporaryDirectory(prefix="aidog-console-smoke-") as tmp:
        root = Path(tmp)
        for pattern in SOURCE:
            for source in REPO.glob(pattern):
                if source.resolve() != source or not source.is_file():
                    continue
                path = root / source.relative_to(REPO)
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, path)
        for name in ("home", "tmp"):
            (root / name).mkdir()
        secrets_fd = os.open(root / "secrets.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(secrets_fd, "w") as stream:
            stream.write(f"WEB_TOKEN={secrets.token_urlsafe(32)}\n"
                         "MODEL_API_KEY=fixture-model-key\n")
        # Only pass host utilities and locale information. In particular, no
        # inherited proxies, provider keys, profile overrides, or tmux socket.
        env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL")
               if k in os.environ}
        env.update(HOME=str(root / "home"), USERPROFILE=str(root / "home"),
                   TMPDIR=str(root / "tmp"), TMP=str(root / "tmp"), TEMP=str(root / "tmp"),
                   TMUX_TMPDIR=str(root / "tmp"), XDG_CONFIG_HOME=str(root / "home" / ".config"),
                   XDG_DATA_HOME=str(root / "home" / ".local" / "share"),
                   AIDOG_SMOKE_FIXTURE_ROOT=str(root))
        try:
            return subprocess.run([sys.executable, "-I", "-B", str(root / FIXTURE)],
                                  cwd=root, env=env, check=False).returncode
        except KeyboardInterrupt:
            return 130


class ModelFixture(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self.send_error(404)
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Queueing steering (batch 4-2): a prompt carrying this marker makes
        # the fixture hold the turn open, so a second POST can arrive while a
        # run is genuinely in flight (the real product's long task).
        if "HOLD-THE-RUN" in json.dumps(request.get("messages") or []):
            time.sleep(3.0)
        body = json.dumps({"model": request["model"], "choices": [{
            "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop",
        }], "usage": {"prompt_tokens": 12, "completion_tokens": 1,
                       "total_tokens": 13}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def restrict_network(ports: set[int]) -> None:
    """Restrict this interpreter's socket calls to fixture ports. Use a
    network namespace for isolation of native code and subprocesses too."""
    def audit(event, args):
        if event == "socket.connect":
            _, address = args
            if (not isinstance(address, tuple) or address[0] != "127.0.0.1"
                    or address[1] not in ports):
                raise OSError("outside the console smoke fixtures")
        elif event == "socket.getaddrinfo":
            host, port, *_ = args
            if host != "127.0.0.1" or port not in ports:
                raise OSError("outside the console smoke fixtures")
    sys.addaudithook(audit)


def serve() -> None:
    # The worker owns the write end. If it is killed mid-test, there is no
    # pause recovery action and the disposable HTTP server exits on EOF.
    def exit_on_parent_loss():
        sys.stdin.buffer.read(1)
        os._exit(0)

    threading.Thread(target=exit_on_parent_loss, daemon=True).start()
    from web import server
    from mycore import profiling, prosearch, stt

    def transcribe(data, *, model):
        try:
            with wave.open(io.BytesIO(data), "rb") as wav:
                return {"text": "console smoke fixture", "language": "en",
                        "duration": wav.getnframes() / wav.getframerate()}
        except (EOFError, wave.Error) as exc:
            raise stt.SttError("invalid fixture audio") from exc

    stt.available = lambda: True
    stt.transcribe = transcribe
    server._tmux_session_exists = lambda name: False

    def no_process_restart():
        return 409, {"error": "process restart disabled in the console smoke fixture"}

    server.restart_watchdog_recycle = no_process_restart
    server.restart_console_self = no_process_restart
    server.restart_chat_service = no_process_restart
    # Skip boot-time host/process probes. Use real HTTP handlers, the real
    # session/control/settings stores, and the real model transport.
    with ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture) as model, \
            ThreadingHTTPServer(("127.0.0.1", 0), server.Handler) as console:
        thread = threading.Thread(target=model.serve_forever, daemon=True)
        thread.start()
        model_port = model.server_port
        restrict_network({model_port})
        server.PORT = console.server_port
        server.PORT_SOURCE = "file"
        (REPO / "web/port.local").write_text(f"{console.server_port}\n", encoding="utf-8")
        (REPO / "web/settings.json").write_text(json.dumps({
            "currency": "CNY", "fx_rate": 7,
            "mycore": {"base_url": f"http://127.0.0.1:{model_port}/v1",
                       "profiling": {**profiling.DEFAULT_SETTINGS, "topics": []},
                       "search": prosearch.DEFAULT_SETTINGS},
        }), encoding="utf-8")
        server.MYCORE_WD_STATE.write_text(json.dumps({"targets": {}}), encoding="utf-8")
        server.model_meter.activate()
        from mycore import fleet
        fleet.activate()
        print(console.server_port, flush=True)
        console.serve_forever()


@contextmanager
def installation():
    """The only HTTP target the mutating suite can receive is our child."""
    with tempfile.TemporaryFile(mode="w+t") as log, subprocess.Popen(
        [sys.executable, "-I", "-B", str(REPO / FIXTURE), "serve"],
        cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True,
    ) as proc:
        try:
            port = int(proc.stdout.readline())
            yield port
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            elif proc.returncode:
                log.seek(0)
                print(log.read(), file=sys.stderr)


def run() -> int:
    if (not REPO.name.startswith("aidog-console-smoke-")
            or os.environ.get("AIDOG_SMOKE_FIXTURE_ROOT") != str(REPO)
            or Path.cwd() != REPO):
        raise SystemExit("use tests/web_console_p3.py to start a disposable fixture")
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "tests"))
    if sys.argv[1:] == ["serve"]:
        serve()
        return 0
    import web_console_p3 as smoke
    with installation() as port:
        smoke.BASE = f"http://127.0.0.1:{port}"
        smoke.FIXTURE_PORTS.add(port)
        restrict_network(smoke.FIXTURE_PORTS)
        smoke.check_live_isolation()
        return smoke.mutating_suite()


if __name__ == "__main__":
    sys.exit(run())
