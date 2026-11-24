#!/usr/bin/env python3
"""Exercise the real setup UI in Chromium with every request intercepted."""
from collections import deque
from unittest import TestCase, main
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from memory_forget_ui import ROOT, STATIC, chromium_path

ORIGIN = "http://setup-fixture.test"
FIXTURE_BODY = "fixture-denial-body"
READY = {"ready": True, "items": [{"id": "web_token", "state": "set"}],
         "service": "running"}


class ActivationRetryTest(TestCase):
    @classmethod
    def setUpClass(cls):
        playwright = sync_playwright().start()
        cls.addClassCleanup(playwright.stop)
        cls.browser = playwright.chromium.launch(executable_path=chromium_path(),
                                                 args=["--disable-background-networking"])
        cls.addClassCleanup(cls.browser.close)

    def open_console(self, responses):
        pending = deque(responses)
        requests, writes, errors = [], [], []
        self.addCleanup(lambda: self.assertEqual(errors, []))
        self.addCleanup(lambda: self.assertEqual(writes, []))
        self.addCleanup(lambda: self.assertFalse(pending))
        page = self.browser.new_page(service_workers="block")
        self.addCleanup(page.close)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            request = route.request
            url = urlsplit(request.url)
            if f"{url.scheme}://{url.netloc}" != ORIGIN:
                return route.abort()
            if request.method != "GET":
                writes.append((request.method, url.path))
                return route.abort()
            if url.path in STATIC:
                path, content_type = STATIC[url.path]
                return route.fulfill(body=(ROOT / path).read_text(encoding="utf-8"),
                                     content_type=content_type)
            if url.path == "/api/setup":
                requests.append((request.method, request.headers.get("authorization")))
                status, body = pending.popleft() if pending else (500, {})
                return route.fulfill(status=status, json=body)
            if url.path.startswith("/api/"):
                return route.fulfill(json={})
            return route.abort()

        page.route("**/*", serve)
        page.goto(f"{ORIGIN}/?view=overview&lang=en&skipboot=1")
        return page, requests

    def test_first_failure_then_manual_retry(self):
        page, requests = self.open_console([(500, {"error": FIXTURE_BODY}), (200, READY)])
        expect(page.locator("#actCard")).to_be_visible()
        expect(page.locator("#actMsg")).to_contain_text("HTTP 500")
        expect(page.locator("#actMsg")).not_to_contain_text(FIXTURE_BODY)
        expect(page.locator("#actStart")).to_contain_text("./aidog setup --check")
        expect(page.locator("#actBadge")).to_have_text("—")
        expect(page.locator("#actService")).to_be_hidden()
        expect(page.locator("#actList [data-act-item]")).to_have_count(0)
        self.assertEqual(requests, [("GET", None)])
        page.locator("#actRecheck").click()
        expect(page.locator("#actBadge")).to_have_text("READY ✓")
        expect(page.locator("#actMsg")).to_be_empty()
        expect(page.locator("#actStart")).to_be_hidden()
        expect(page.locator("#actService")).to_be_visible()
        expect(page.locator("#actList [data-act-item=web_token]")).to_be_visible()
        expect(page.locator("#actRecheck")).to_be_enabled()
        self.assertEqual(requests, [("GET", None)] * 2)

    def test_retry_failure_keeps_snapshot_and_legacy_clears_it(self):
        page, requests = self.open_console([(200, READY), (403, {}), (404, {})])
        expect(page.locator("#actBadge")).to_have_text("READY ✓")
        page.locator("#actRecheck").click()
        expect(page.locator("#actMsg")).to_contain_text("HTTP 403")
        expect(page.locator("#actBadge")).to_have_text("READY ✓")
        expect(page.locator("#actList [data-act-item=web_token]")).to_be_visible()
        page.locator("#actRecheck").click()
        expect(page.locator("#actCard")).to_be_hidden()
        expect(page.locator("#actMsg")).to_be_empty()
        expect(page.locator("#actList [data-act-item]")).to_have_count(0)
        self.assertEqual(requests, [("GET", None)] * 3)

    def test_malformed_readiness_is_not_rendered(self):
        page, requests = self.open_console([(200, {"ready": "yes", "items": []})])
        expect(page.locator("#actCard")).to_be_visible()
        expect(page.locator("#actMsg")).to_contain_text("invalid response")
        expect(page.locator("#actStart")).to_contain_text("./aidog setup --check")
        self.assertEqual(requests, [("GET", None)])


if __name__ == "__main__":
    main(verbosity=2)
