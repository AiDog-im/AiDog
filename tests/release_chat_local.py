#!/usr/bin/env python3
"""Run an isolated HTTP chat release smoke: python3 -I tests/release_chat_local.py."""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
from socketserver import TCPServer
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
MODEL = "release-smoke-model"
WEB_TOKEN = "fixture-console-token"
OPENAI_TOKEN = "fixture-gateway-token"
MODEL_KEY = "fixture-model-key"
PIECES = ("Local ", "HTTP reply.")
REPLY = "".join(PIECES)
CONSOLE_PATH = "/api/chat"
OPENAI_PATH = "/v1/chat/completions"


class MockOpenAI(BaseHTTPRequestHandler):
    def do_POST(self):
        self.connection.settimeout(5)
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, self.headers.get("Authorization"), payload))
        if self.path != OPENAI_PATH or self.headers.get("Authorization") != f"Bearer {MODEL_KEY}":
            self.send_error(400, "unexpected mock request")
            return
        if payload.get("stream") is True:
            chunks = [{"choices": [{"index": 0, "delta": {"content": piece},
                                    "finish_reason": None}]} for piece in PIECES]
            chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            frames = [json.dumps(chunk) for chunk in chunks] + ["[DONE]"]
            body = "".join(f"data: {frame}\n\n" for frame in frames).encode()
            content_type = "text/event-stream"
        else:
            body = json.dumps({"choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant", "content": REPLY}}]}).encode()
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
            raise RuntimeError("refusing to use the shared proxy port")


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@contextmanager
def serve(handler):
    with LoopbackServer((HOST, 0), handler) as httpd:
        address = httpd.server_address
        ALLOWED.add(address)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield httpd
        finally:
            httpd.shutdown()
            thread.join()
            ALLOWED.remove(address)


def sse_frames(body):
    """Return (event, payload), with event=None for the OpenAI data-only dialect."""
    frames = []
    for frame in body.decode().strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines())
        data = fields["data"]
        frames.append((fields.get("event"), data if data == "[DONE]" else json.loads(data)))
    return frames


@unittest.skipUnless(__name__ == "__main__", "Run directly to install the process-wide audit hook")
class ReleaseChatLocal(unittest.TestCase):
    def test_http_chat(self):
        with tempfile.TemporaryDirectory(prefix="aidog-release-chat-") as tmp:
            sandbox = Path(tmp)
            # Load source from a clean tree so even hard-coded repo-relative
            # paths cannot pick up production settings, sessions or credentials.
            for name in ("lib", "mycore", "web"):
                (sandbox / name).mkdir()
                for file in (ROOT / name).glob("*.py"):
                    shutil.copyfile(file, sandbox / name / file.name)
            shutil.copytree(ROOT / "overlay/locales", sandbox / "overlay/locales")
            shutil.copyfile(ROOT / "VERSION", sandbox / "VERSION")
            profile = sandbox / "profiles" / "release-smoke"
            profile.mkdir(parents=True)
            (profile / "secrets.env").write_text(
                f"WEB_TOKEN={WEB_TOKEN}\nOPENAI_API_TOKEN={OPENAI_TOKEN}\nMODEL_API_KEY={MODEL_KEY}\n")
            (profile / "secrets.env").chmod(0o600)
            with patch.dict(os.environ, {
                "HOME": tmp, "USERPROFILE": tmp, "XDG_CONFIG_HOME": tmp,
                "XDG_DATA_HOME": tmp, "XDG_CACHE_HOME": tmp,
                "AIDOG_PROFILES_DIR": str(profile.parent), "AIDOG_PROFILE": profile.name,
                "AIDOG_WEB_PORT": "0", "MYCORE_STREAM_READ_TIMEOUT": "5",
            }, clear=True), working_directory(sandbox):
                with serve(MockOpenAI) as upstream:
                    upstream.calls = []
                    (profile / "settings.json").write_text(json.dumps({
                        "mycore": {"base_url": f"http://{HOST}:{upstream.server_port}/v1",
                                   "model": MODEL, "timeout": 5},
                    }))
                    spec = importlib.util.spec_from_file_location("release_smoke_server", sandbox / "web/server.py")
                    server = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(server)
                    with serve(server.Handler) as web:
                        self.check_chat(web.server_port, server.chat_session_path(), profile)
                        self.assertEqual(len(upstream.calls), 3)
                        for (path, auth, data), text, streams in zip(
                            upstream.calls, ("Console prompt", "Gateway prompt", "Gateway prompt"),
                            (True, False, True),
                        ):
                            self.assertEqual(path, OPENAI_PATH)
                            self.assertEqual(auth, f"Bearer {MODEL_KEY}")
                            self.assertEqual(data["model"], MODEL)
                            self.assertEqual(data["messages"][-1], {"role": "user", "content": text})
                            self.assertIs(data.get("stream", False), streams)
        self.assertEqual(BLOCKED, [], "unexpected network or child process attempt")

    def request(self, port, path, payload, token, *, status=200, content_type="application/json"):
        conn = http.client.HTTPConnection(HOST, port, timeout=10)
        try:
            conn.request("POST", path, json.dumps(payload), {
                "Content-Type": "application/json", "Authorization": f"Bearer {token}",
            })
            response = conn.getresponse()
            body = response.read()
            self.assertEqual(response.status, status, body)
            self.assertEqual(response.headers.get_content_type(), content_type, body)
            return body
        finally:
            conn.close()

    def check_chat(self, port, session_path, profile):
        self.assertTrue(session_path.is_relative_to(profile / "state"), session_path)
        console = {"text": "Console prompt"}
        gateway = {"messages": [{"role": "user", "content": "Gateway prompt"}]}
        self.request(port, CONSOLE_PATH, console, OPENAI_TOKEN, status=401)
        self.request(port, OPENAI_PATH, gateway, WEB_TOKEN, status=401)
        self.assertFalse(session_path.exists())

        body = self.request(port, CONSOLE_PATH, console, WEB_TOKEN, content_type="text/event-stream")
        frames = sse_frames(body)
        self.assertEqual([event for event, _ in frames], ["meta", "delta", "delta", "done"])
        self.assertEqual(frames[0][1]["model"], MODEL)
        self.assertEqual(tuple(data["text"] for _, data in frames[1:-1]), PIECES)
        done = frames[-1][1]
        self.assertEqual((done["text"], done["stop"], done["tool_calls"]), (REPLY, "final", 0))
        self.assertEqual(done["ids"], {"user": 0, "assistant": 1})
        self.assertTrue(session_path.is_file())
        session_before = {file: file.read_bytes() for file in session_path.parent.glob("*.jsonl")}
        header, user, assistant = map(json.loads, session_before[session_path].splitlines())
        self.assertEqual(header["meta"]["model"], MODEL)
        self.assertEqual((user["role"], user["content"]), ("user", console["text"]))
        self.assertEqual((assistant["role"], assistant["content"]), ("assistant", REPLY))

        body = self.request(port, OPENAI_PATH, gateway, OPENAI_TOKEN)
        completion = json.loads(body)
        self.assertEqual(completion["object"], "chat.completion")
        self.assertEqual(completion["model"], MODEL)
        choice, = completion["choices"]
        self.assertEqual(choice["message"], {"role": "assistant", "content": REPLY})
        self.assertEqual(choice["finish_reason"], "stop")

        body = self.request(port, OPENAI_PATH, {**gateway, "stream": True,
                                              "stream_options": {"include_usage": True}},
                            OPENAI_TOKEN, content_type="text/event-stream")
        events = sse_frames(body)
        self.assertTrue(all(event is None for event, _ in events))
        self.assertEqual(events[-1][1], "[DONE]")
        role, first, second, finish, usage = [data for _, data in events[:-1]]
        for chunk in (role, first, second, finish, usage):
            self.assertEqual(chunk["object"], "chat.completion.chunk")
            self.assertEqual(chunk["model"], MODEL)
            self.assertEqual(chunk["id"], role["id"])
        self.assertEqual(role["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(tuple(chunk["choices"][0]["delta"]["content"] for chunk in (first, second)), PIECES)
        self.assertEqual(finish["choices"][0]["finish_reason"], "stop")
        self.assertEqual(usage["choices"], [])
        self.assertEqual(usage["usage"], completion["usage"])
        self.assertEqual({file: file.read_bytes() for file in session_path.parent.glob("*.jsonl")},
                         session_before, "the v1 gateway must not write a session")


ALLOWED = set()
BLOCKED = []


def local_only(event, args):
    # Audit before the kernel connect or DNS lookup. Only our two ephemeral
    # ports are allowed, including when an endpoint fallback is misconfigured.
    if event == "socket.connect":
        denied = args[1] not in ALLOWED
    elif event == "socket.sendto":
        denied = True
    elif event == "socket.getaddrinfo":
        denied = args[:2] not in ALLOWED
    elif event in {"socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
                   "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
        denied = True
    else:
        return
    if denied:
        BLOCKED.append(event)
        raise PermissionError(f"unexpected {event} in isolated chat smoke")


if __name__ == "__main__":
    sys.addaudithook(local_only)
    unittest.main()
