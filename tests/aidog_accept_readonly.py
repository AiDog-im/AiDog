#!/usr/bin/env python3
"""Run the acceptance CLI against temporary HTTP and file fixtures only."""
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from threading import Thread
from unittest import TestCase, main, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy import aidog_accept as accept

FIXTURE_TOKEN = "fixture-owner-value-719f833b"
REFUSAL_BODY = "fixture-private-diagnostic-46be5e95"
PATHS = ["/api/health", "/api/status", "/api/setup", "/api/control",
         "/api/usage", "/api/mycore/endpoints?view=cooldown", "/api/fleet",
         "/api/fleet/plans", "/api/backup/agent", "/api/version",
         "/api/chat/history", "/api/status"]


class Recorder(BaseHTTPRequestHandler):
    def parse_request(self):
        if not super().parse_request():
            return False
        self.server.requests.append((self.command, self.path, self.headers.get("Authorization"),
                                     self.headers.get("Proxy-Authorization"), self.headers.get("Cookie")))
        return True

    def do_GET(self):
        status, doc = self.server.responses.get(self.path, (503, {}))
        body = json.dumps(doc).encode()
        self.send_response(status)
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
        if status == 302:
            self.send_header("Location", f"/must-not-follow?value={REFUSAL_BODY}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class LoopbackServer(HTTPServer):
    def server_bind(self):
        # SocketServer provides the actual bind without resolving a hostname.
        from socketserver import TCPServer
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address


class ReadOnlyTest(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="aidog-accept-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.secrets = self.root / "fixture.env"
        self.secrets.write_text(f"WEB_TOKEN={FIXTURE_TOKEN}\nMYCORE_TG_TOKEN={FIXTURE_TOKEN}\n",
                                encoding="utf-8")
        self.server = LoopbackServer(("127.0.0.1", 0), Recorder)
        self.server.requests = []
        self.server.responses = {}
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
                        daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        connect = socket.create_connection

        def connect_fixture(address, *args, **kwargs):
            self.assertEqual(address, self.server.server_address)
            return connect(address, *args, **kwargs)

        self.enterContext(mock.patch.object(socket, "create_connection", connect_fixture))
        self.enterContext(mock.patch.dict(os.environ, {
            "WEB_TOKEN": FIXTURE_TOKEN, "MYCORE_TG_TOKEN": FIXTURE_TOKEN,
            "http_proxy": "http://proxy.invalid:80", "https_proxy": "http://proxy.invalid:80",
            "no_proxy": "", "NO_PROXY": "",
        }, clear=True))
        self.enterContext(mock.patch.object(accept, "WD_STATE", self.root / "watchdog.json"))
        self.addCleanup(lambda: self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                                                  [self.secrets.name]))
        before = self.secrets.read_bytes(), self.secrets.stat().st_mtime_ns
        self.addCleanup(lambda: self.assertEqual((self.secrets.read_bytes(),
                                                   self.secrets.stat().st_mtime_ns), before))

    def run_cli(self, *args, base=None):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = accept.main(["--base", base or self.base, "--secrets", str(self.secrets), *args])
        for marker in (FIXTURE_TOKEN, REFUSAL_BODY):
            self.assertNotIn(marker, stdout.getvalue() + stderr.getvalue())
        return rc, stdout.getvalue(), stderr.getvalue()

    def assert_readonly_requests(self):
        self.assertEqual(self.server.requests, [("GET", path, None, None, None) for path in PATHS])

    def test_auth_refusal_is_failure_and_never_retried_with_credentials(self):
        for status in (401, 403):
            for args in ((), ("--json",)):
                with self.subTest(status=status, args=args):
                    self.server.requests.clear()
                    self.server.responses["/api/setup"] = (status, {"error": REFUSAL_BODY})
                    rc, out, err = self.run_cli(*args)
                    self.assertEqual((rc, err), (1, ""))
                    if args:
                        row = self.setup_row(out)
                        self.assertEqual((row["status"], row["detail"]), ("FAIL", f"HTTP {status}"))
                    else:
                        self.assertRegex(out, rf"FAIL\s+setup\.readiness\s+HTTP {status}")
                    self.assert_readonly_requests()

    @staticmethod
    def setup_row(output):
        return next(row for row in json.loads(output)["checks"] if row["name"] == "setup.readiness")

    def test_setup_readiness_and_legacy_404(self):
        for status, doc, expected in ((200, {"items": [], "ready": True}, "PASS"),
                                      (200, {"items": [], "ready": False}, "FAIL"),
                                      (200, {"items": [], "ready": "yes"}, "FAIL"),
                                      (404, {}, "SKIP")):
            with self.subTest(status=status, doc=doc):
                self.server.requests.clear()
                self.server.responses["/api/setup"] = status, doc
                _, out, _ = self.run_cli("--json")
                self.assertEqual(self.setup_row(out)["status"], expected)
                self.assert_readonly_requests()

    def test_redirect_is_refused_without_requesting_the_location(self):
        self.server.responses["/api/setup"] = 302, {}
        rc, out, _ = self.run_cli("--json")
        self.assertEqual(rc, 1)
        row = self.setup_row(out)
        self.assertEqual((row["status"], row["detail"]), ("FAIL", "HTTP 302"))
        self.assert_readonly_requests()

    def test_credential_bearing_and_non_origin_bases_are_refused(self):
        for base in (f"http://owner:{FIXTURE_TOKEN}@127.0.0.1:{self.server.server_port}",
                     f"{self.base}?secret={FIXTURE_TOKEN}", f"{self.base}/path"):
            rc, out, err = self.run_cli(base=base)
            self.assertEqual((rc, out), (2, ""))
            self.assertIn("http(s) origin without credentials", err)
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    main(verbosity=2)
