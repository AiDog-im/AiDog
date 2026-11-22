#!/usr/bin/env python3
"""Offline cooldown read contract: real ChatClient, fake model transport."""
from __future__ import annotations

from contextlib import ExitStack
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mycore import cli, model, modeltier
from web import server as web


class EndpointCooldownTest(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.enter = stack.enter_context
        self.addCleanup(modeltier.reset_health)
        modeltier.reset_health()
        self.now = 100.0
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "settings.json"
        self.base = "https://user:password@primary.invalid/private?token=hidden#frag"
        self.route = "https://route.invalid/private?token=also-hidden"
        self.legacy_name = "CUSTOM_PRIVATE_SLOT"
        self.enter(patch.dict(os.environ, {
            "MYCORE_BASE_URL": self.base,
            "MYCORE_UPSTREAMS": (
                f"{self.route}|MODEL_UP_K1,MODEL_UP_K2,{self.legacy_name},"
                "https://inherited.invalid/v1"),
        }))
        self.enter(patch.object(cli.pp, "settings_path", return_value=self.path))
        self.enter(patch.object(modeltier.SHARED_HEALTH, "_now", lambda: self.now))
        self.key_read = self.enter(patch.object(model, "_slot_key", return_value="test-no-echo-value"))
        self.enter(patch.object(model.meter, "record"))

        class QuietHandler(web.Handler):
            def log_message(self, *_args):
                pass

        self.server = self.enter(web.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.shutdown)
        thread.start()

    def get_snapshot(self):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            conn.request("GET", "/api/mycore/endpoints?view=cooldown")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            self.assertEqual(res.getheader("Cache-Control"), "no-store")
            raw = res.read().decode()
        finally:
            conn.close()
        for marker in ("https://", "password", "hidden", self.legacy_name,
                       "test-no-echo-value", "unconfigured"):
            self.assertNotIn(marker, raw)
        return json.loads(raw)

    def test_runtime_attempts_across_clients_and_slot_expiry(self):
        # Ensure the cooldown view cannot call the endpoint editor, status
        # probes, or credential reads (the model turns alone may read slots).
        editor = self.enter(patch.object(web, "collect_endpoints", side_effect=AssertionError))
        probe = self.enter(patch.object(web, "probe_endpoint", side_effect=AssertionError))
        status = self.enter(patch.object(web, "collect_status", side_effect=AssertionError))
        reads = self.enter(patch.object(web, "_slot_present", side_effect=AssertionError))
        before = self.get_snapshot()
        self.assertEqual(before["scope"], "console-process")
        self.assertTrue(before["available"])
        self.assertEqual(before["endpoints"], [
            {"index": 1, "slots": [{"index": 1, "label": "MODEL_API_KEY", "state": "unobserved"}]},
            {"index": 2, "slots": [
                {"index": 1, "label": "MODEL_UP_K1", "state": "unobserved"},
                {"index": 2, "label": "MODEL_UP_K2", "state": "unobserved"},
                {"index": 3, "state": "unobserved"},
            ]},
            {"index": 3, "slots": [{"index": 1, "label": "MODEL_API_KEY", "state": "unobserved"}]},
        ])

        # A different process (or explicitly isolated client) is not visible.
        isolated = model.ChatClient(cli.resolve_model_config(), health=modeltier.HealthRegistry())
        isolated.health.record_success(isolated.health.identity(self.route, "MODEL_UP_K1"))
        modeltier.SHARED_HEALTH.record_success(modeltier.HealthRegistry.identity(
            "https://unconfigured.invalid/v1", "MODEL_UP_K2"))
        self.assertEqual(before, self.get_snapshot())

        def transport(url, *_args):
            if url.startswith(self.base):
                raise model.ModelError("HTTP 503")
            # The real loop rotates from K1 to K2 on auth failure.
            if self.key_read.call_args.args[0] == "MODEL_UP_K1":
                raise model.ModelError("HTTP 401")
            return '{"choices": [{"message": {"content": "ok"}}]}'

        for attempt in range(2):
            client = model.ChatClient(cli.resolve_model_config(), transport=transport)
            self.assertIs(client.health, modeltier.SHARED_HEALTH)
            client.chat([{"role": "user", "content": "fixture"}])
            if attempt == 0:
                first = self.get_snapshot()["endpoints"][1]["slots"][0]
                self.assertEqual(first, {"index": 1, "label": "MODEL_UP_K1",
                                        "state": "not-cooling"})
        active = self.get_snapshot()["endpoints"][1]["slots"]
        self.assertEqual(active[0], {"index": 1, "label": "MODEL_UP_K1",
                                     "state": "cooling", "remaining_s": 30.0})
        self.assertEqual(active[1]["state"], "not-cooling")
        self.assertEqual(active[2]["state"], "unobserved")
        self.now += 30.0
        expired = self.get_snapshot()["endpoints"][1]["slots"]
        self.assertEqual(expired[0], {"index": 1, "label": "MODEL_UP_K1",
                                      "state": "not-cooling"})
        for mocked in (editor, probe, status, reads):
            mocked.assert_not_called()

    def test_structured_inheritance_precedence_and_unavailable(self):
        self.path.write_text(json.dumps({"mycore": {
            "endpoints": [{"url": "https://structured.invalid/v1"}],
            "upstreams": "https://ignored.invalid/v1|MODEL_UP_K8",
        }}))
        self.assertEqual(len(self.get_snapshot()["endpoints"]), 3)  # env wins
        os.environ.pop("MYCORE_UPSTREAMS")
        modeltier.SHARED_HEALTH.record_success(modeltier.HealthRegistry.identity(
            "https://structured.invalid/v1", "MODEL_API_KEY"))
        self.assertEqual(self.get_snapshot()["endpoints"][1], {
            "index": 2, "slots": [
                {"index": 1, "label": "MODEL_API_KEY", "state": "not-cooling"}],
        })
        with patch.object(cli, "resolve_model_config", side_effect=RuntimeError("hidden")):
            self.assertEqual(self.get_snapshot(), {
                "scope": "console-process", "available": False, "endpoints": [],
            })


if __name__ == "__main__":
    unittest.main(verbosity=2)
