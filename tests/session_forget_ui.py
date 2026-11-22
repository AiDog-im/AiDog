#!/usr/bin/env python3
"""The owner-facing FORGET of a TRANSCRIPT MESSAGE — the console UI half.

What this proves, with the cached Chromium and NO product process:

  * the session search results give every hit its own FORGET action, next
    to the hit, addressed by the opaque source id the server printed with
    it (the UI never invents an address);
  * clicking FORGET opens the SAME confirmation the memory and the
    owner-profile controls use, and it FIRST shows the server's read-only
    preview (how many messages would be silenced, whether recall, the
    session list and the owner profile are affected) plus the honest
    limits sentence; the confirm button stays disabled until that preview
    has arrived;
  * cancelling writes NOTHING (no POST at all);
  * a confirmed forget writes exactly {source_id} with the product's
    Bearer header, the list is re-read from the API and the hit is gone —
    and the message says plainly that NOTHING was deleted and that the
    transcript line stays on disk;
  * the failures are honest: a refused preview, an unknown message (404),
    a 401 and a dead network all say what happened and never fake a
    success;
  * no credential ever reaches the DOM (the token lives in localStorage
    and in the request header only);
  * the layout holds at 390px, 320px and 1280px with zero page errors,
    and the copy is bilingual (en + zh, paired pack keys);
  * the SHARED dialog still serves the memory view (the reuse did not
    break the surface it was borrowed from).

The API calls are STUBBED here: nothing in the repository, the sessions
or the production state dir is read or written.

Run:  python3 tests/session_forget_ui.py     (exit 0 = all green)
Set FLEET_TEST_CHROMIUM to override the browser binary; otherwise the
newest cached chromium under ~/.cache/ms-playwright is used.
English output per repo i18n policy.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://sessforget.test"

# A fixture credential: it exists only in this browser's localStorage and
# in the request header. It must never be visible in the page.
TOKEN = "UI-FIXTURE-TOKEN-6b0e4d27"

STATIC = {
    "/": ("web/static/index.html", "text/html"),
    "/static/app.js": ("web/static/app.js", "text/javascript"),
    "/static/style.css": ("web/static/style.css", "text/css"),
    "/static/i18n/en.json": ("overlay/locales/en.json", "application/json"),
    "/static/i18n/zh.json": ("overlay/locales/zh.json", "application/json"),
}

SRC_A = "src_aaaabbbbccccdddd1111222233334444"
SRC_B = "src_bbbbccccddddeeee5555666677778888"

# The search face: every hit carries the opaque id of its message and
# nothing else (no excerpt, no path, no credential).
SEARCH = {
    "sessions": [
        {"id": "2026-11-22T10-00-00", "when": 1800000000, "size": 2048,
         "summary": "the release notes mention version 5",
         "hits": 2,
         "hit_sources": [{"source_id": SRC_A}, {"source_id": SRC_B}]},
    ],
    "count": 1,
    "truncated": False,
    "limit": 200,
    "search": {"q": "hunter2", "method": "fts5", "engine": "fts5",
               "fallback": False, "case_insensitive": True},
    "prune": {"enabled": False},
}

# The plain listing (no query): no hits, no actions.
PLAIN = {
    "sessions": [
        {"id": "2026-11-22T10-00-00", "when": 1800000000, "size": 2048,
         "summary": "the release notes mention version 5"},
    ],
    "count": 1,
    "truncated": False,
    "limit": 200,
    "prune": {"enabled": False},
}

# The server's honest limits sentence — shown VERBATIM next to the
# localized one-line summary.
LIMITS = ("The source is removed from ACTIVE memory only: prompt injection, "
          "the console memory view, the owner profile, recall and restored "
          "backups. The original session transcript, copies that already "
          "left this machine, the published repository history and backups "
          "taken before this moment are NOT deleted.")

# The memory read face (the OTHER surface of the shared dialog).
MEMORY = {
    "slots": [
        {"slot": "memory", "available": True, "file": "MEMORY_curated.md",
         "chars": 120, "truncated": False, "entries": [
             {"title": "Facts", "excerpt": "the owner prefers local tools",
              "truncated": False, "chars": 29,
              "channel": "curated:memory",
              "source_id": "src_ccccddddeeeeffff9999aaaabbbbcccc"}]},
    ],
    "entries_total": 1,
    "query": "",
    "read_only": True,
    "local_only": True,
    "editable": False,
    "note": "read-only view",
}

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED += 1
        print(f"FAIL  {name}   [{detail}]")
    return ok


def chromium_path() -> str | None:
    """The cached Chromium: the env override first, then the newest one."""
    for var in ("FLEET_TEST_CHROMIUM", "AIDOG_TEST_CHROMIUM"):
        env = os.environ.get(var)
        if env and Path(env).exists():
            return env
    root = Path.home() / ".cache" / "ms-playwright"
    best: tuple[int, Path] | None = None
    if root.is_dir():
        for entry in root.glob("chromium-*"):
            tail = entry.name.split("-")[-1]
            if not tail.isdigit():
                continue
            for rel in ("chrome-linux64/chrome", "chrome-linux/chrome"):
                cand = entry / rel
                if cand.exists() and (best is None or int(tail) > best[0]):
                    best = (int(tail), cand)
    return str(best[1]) if best else None


class Api:
    """The stubbed console API: the session faces, plus honest failures."""

    def __init__(self) -> None:
        self.forgotten: set[str] = set()
        self.writes: list[dict] = []
        self.calls: list[str] = []
        self.query = ""
        self.preview_status = 200
        self.hold_preview = False
        self.held: list[tuple] = []
        self.post_status = 200
        self.post_network = False
        self.already = False

    def sessions(self) -> dict:
        """The server's own answer: the query decides which face answers."""
        if not self.query:
            return dict(PLAIN)
        rows = []
        for row in SEARCH["sessions"]:
            sources = [h for h in row.get("hit_sources", [])
                       if h["source_id"] not in self.forgotten]
            if not sources:
                continue        # every hit of this session is silenced
            rows.append(dict(row, hits=len(sources), hit_sources=sources))
        return dict(SEARCH, sessions=rows, count=len(rows))

    def preview(self, source_id: str) -> dict:
        if source_id in self.forgotten:
            return {"source_id": source_id, "channel": "session", "items": 0,
                    "affects": {"recall": False, "session_list": False,
                                "profile": False},
                    "limits": LIMITS}
        return {"source_id": source_id, "channel": "session", "items": 1,
                "affects": {"recall": True, "session_list": True,
                            "profile": True},
                "limits": LIMITS}

    def route(self, route):
        url = urlsplit(route.request.url)
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            return route.abort()
        path = url.path
        method = route.request.method
        if method == "GET" and path in STATIC:
            rel, content_type = STATIC[path]
            return route.fulfill(
                body=(ROOT / rel).read_text(encoding="utf-8"),
                content_type=content_type)
        if method == "GET" and path == "/api/memory":
            return route.fulfill(json=MEMORY)
        if method == "GET" and path == "/api/memory/forget/preview":
            query = urllib.parse.parse_qs(url.query)
            slot = (query.get("slot") or [""])[0]
            title = (query.get("title") or [""])[0]
            entry = MEMORY["slots"][0]["entries"][0]
            return route.fulfill(json={
                "slot": slot, "title": title,
                "channel": entry["channel"], "source_id": entry["source_id"],
                "entries": 1,
                "affects": {"injection": True, "memory_view": True,
                            "profile_coverage": True},
                "limits": LIMITS})
        if method == "POST" and path == "/api/memory/forget":
            route.request.post_data
            return route.fulfill(json={"ok": True, "already": False})
        if method == "GET" and path == "/api/sessions":
            self.calls.append("GET /api/sessions")
            query = urllib.parse.parse_qs(url.query)
            self.query = (query.get("q") or [""])[0]
            return route.fulfill(json=self.sessions())
        if method == "GET" and path == "/api/sessions/forget/preview":
            self.calls.append("GET /api/sessions/forget/preview")
            query = urllib.parse.parse_qs(url.query)
            source_id = (query.get("source") or [""])[0]
            if self.hold_preview:
                # the preview stays PENDING: the confirmation must not be
                # confirmable while the server has not answered yet
                self.held.append((route, source_id))
                return
            if self.preview_status != 200:
                return route.fulfill(status=self.preview_status,
                                     json={"error": "preview unavailable"})
            return route.fulfill(json=self.preview(source_id))
        if method == "POST" and path == "/api/sessions/forget":
            try:
                doc = json.loads(route.request.post_data or "{}")
            except ValueError:
                doc = {}
            self.writes.append({
                "body": doc,
                "auth": route.request.headers.get("authorization", ""),
            })
            if self.post_network:
                return route.abort()
            if self.post_status != 200:
                return route.fulfill(status=self.post_status,
                                     json={"error": "refused"})
            source_id = str(doc.get("source_id"))
            self.forgotten.add(source_id)
            return route.fulfill(json={
                "ok": True,
                "id": source_id,
                "channel": "session",
                "already": self.already,
                "limits": LIMITS})
        if method != "GET":
            return route.fulfill(status=404, json={})
        if path.startswith("/api/"):
            return route.fulfill(json={})
        return route.abort()


def open_console(browser, api: Api, *, lang: str = "en", width: int = 1280,
                 view: str = "chat"):
    page = browser.new_page(viewport={"width": width, "height": 900},
                             service_workers="block")
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(
        "window.localStorage.setItem('aidog_token', %s);"
        % json.dumps(TOKEN))
    page.route("**/*", api.route)
    page.goto(f"{ORIGIN}/?view={view}&lang={lang}&skipboot=1")
    return page, errors


def search(page, query: str = "hunter2"):
    """Run a session search and wait for the hits to paint."""
    page.fill("#sessSearch", query)
    page.click("#sessGo")
    page.wait_for_selector(".sess-hits")
    return page.locator(".sess-hits").first


def open_hits(page):
    """Expand the hits list of the first search row (it is collapsed)."""
    details = page.locator(".sess-hits").first
    if details.get_attribute("open") is None:
        details.locator("summary").click()
    return details


def main() -> int:
    packs = {lang: json.loads((ROOT / "overlay" / "locales" / f"{lang}.json")
                               .read_text(encoding="utf-8"))
             for lang in ("en", "zh")}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=chromium_path(),
            args=["--disable-background-networking"])

        # ---------------------------------------------- the happy path ------
        api = Api()
        api.hold_preview = True
        page, errors = open_console(browser, api)
        page.wait_for_selector("#sessList .ctl-row")
        check("A1 the plain listing carries no FORGET action (no hits)",
              page.locator(".sess-forget-btn").count() == 0,
              str(page.locator(".sess-forget-btn").count()))
        search(page)
        check("A2 the search result lists its hits",
              page.locator(".sess-hits").count() == 1,
              str(page.locator(".sess-hits").count()))
        open_hits(page)
        buttons = page.locator(".sess-forget-btn")
        check("A2 every hit carries its own FORGET action",
              buttons.count() == 2, str(buttons.count()))
        stamps = page.locator(".sess-hit .mem-src-id")
        check("A3 every hit shows its short opaque source id",
              stamps.count() == 2
              and "src_aaaabbbb" in stamps.nth(0).inner_text()
              and "src_bbbbcccc" in stamps.nth(1).inner_text(),
              stamps.nth(0).inner_text())
        check("A4 the short id is shortened (no full id dump)",
              SRC_A not in page.locator("#sessList").inner_text(),
              page.locator("#sessList").inner_text()[:200])

        buttons.nth(0).click()
        dialog = page.locator("#memForgetDialog")
        expect(dialog).to_be_visible()
        check("B1 the confirmation opens on FORGET", dialog.is_visible(), "")
        check("B2 the confirm button is disabled until the preview arrived",
              page.locator("#memForgetConfirm").is_disabled(), "")
        check("B3 nothing was written while the preview was loading",
              api.writes == [], str(api.writes))

        # the server answers the pending preview
        held_route, held_source = api.held[0]
        check("B3b the preview asked for the source id of the hit",
              held_source == SRC_A, held_source)
        held_route.fulfill(json=api.preview(held_source))

        preview = page.locator("#memForgetPreview")
        expect(preview).to_contain_text(packs["en"]["sess.forget.itemsOne"])
        expect(preview).to_contain_text(packs["en"]["sess.forget.affectRecall"])
        expect(preview).to_contain_text(packs["en"]["sess.forget.affectList"])
        expect(preview).to_contain_text(packs["en"]["sess.forget.affectProfile"])
        check("B4 the preview says how many messages would be silenced",
              "1 message would be silenced" in preview.inner_text(),
              preview.inner_text()[:160])
        check("B5 the preview names the three affected surfaces in plain words",
              page.locator(".mem-pv-flag.is-on").count() == 3
              and page.locator(".mem-pv-flag.is-off").count() == 0,
              str(page.locator(".mem-pv-item").count()))
        check("B6 the preview carries the honest limits sentence",
              "NOT deleted" in preview.inner_text()
              and packs["en"]["sess.forget.limitsSummary"]
              in preview.inner_text(), preview.inner_text()[:240])
        check("B7 the confirm button is enabled once the preview is shown",
              page.locator("#memForgetConfirm").is_enabled(), "")
        check("B8 the dialog names the session channel in plain words",
              packs["en"]["sess.forget.chanSession"] in preview.inner_text(),
              preview.inner_text()[:240])
        check("B9 the confirmation shows the short id, not the full one",
              SRC_A not in preview.inner_text()
              and "src_aaaabbbb" in preview.inner_text(),
              preview.inner_text()[:160])

        page.click("#memForgetCancel")
        expect(dialog).to_be_hidden()
        check("C1 cancelling writes nothing", api.writes == [], str(api.writes))
        check("C2 cancelling leaves the hit in the list",
              page.locator(".sess-forget-btn").count() == 2,
              str(page.locator(".sess-forget-btn").count()))

        api.hold_preview = False
        reads_before = api.calls.count("GET /api/sessions")
        page.locator(".sess-forget-btn").nth(0).click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            packs["en"]["sess.forget.itemsOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#sessMsg")).to_contain_text(
            packs["en"]["sess.forget.done"].replace("{id}", "src_aaaabbbb…"))
        check("D1 the confirmed forget wrote exactly one request",
              len(api.writes) == 1, str(api.writes))
        check("D1b the list was re-read from the API",
              api.calls.count("GET /api/sessions") == reads_before + 1,
              str(api.calls.count("GET /api/sessions")))
        write = api.writes[0] if api.writes else {"body": {}, "auth": ""}
        check("D2 the write carries the source id and nothing else",
              write["body"] == {"source_id": SRC_A}, str(write["body"]))
        check("D3 the write uses the product's Bearer helper",
              write["auth"] == "Bearer " + TOKEN, write["auth"])
        expect(page.locator(".sess-forget-btn")).to_have_count(1)
        check("D4 the list was re-read and the hit is gone",
              SRC_A not in page.locator("#sessList").inner_text(),
              page.locator("#sessList").inner_text()[:200])
        check("D5 the confirmation says that NOTHING was deleted",
              "Nothing was deleted" in
              page.locator("#sessMsg").inner_text(),
              page.locator("#sessMsg").inner_text())
        check("D6 the confirmation says the transcript line stays on disk",
              "transcript line stays on disk"
              in page.locator("#sessMsg").inner_text(),
              page.locator("#sessMsg").inner_text())
        check("D7 the access token never reached the DOM",
              TOKEN not in page.content(), "the token is visible in the page")
        check("D8 no page error", not errors, str(errors[:2]))
        page.close()

        # ------------------------------------------------ the honest errors --
        api = Api()
        api.preview_status = 404
        page, errors = open_console(browser, api)
        search(page)
        open_hits(page)
        page.locator(".sess-forget-btn").nth(0).click()
        expect(page.locator("#memForgetError")).to_contain_text(
            packs["en"]["sess.forget.previewMissing"])
        check("E1 an unknown message says so and writes nothing",
              api.writes == []
              and page.locator("#memForgetConfirm").is_disabled(),
              str(api.writes))
        check("E1b the refusal says the message is no longer available",
              "no longer available" in
              page.locator("#memForgetError").inner_text(),
              page.locator("#memForgetError").inner_text())
        page.click("#memForgetCancel")
        page.close()

        api = Api()
        api.preview_status = 500
        page, errors = open_console(browser, api)
        search(page)
        open_hits(page)
        page.locator(".sess-forget-btn").nth(0).click()
        expect(page.locator("#memForgetError")).to_contain_text(
            packs["en"]["sess.forget.previewFailed"])
        check("E2 a refused preview says so and writes nothing",
              api.writes == []
              and page.locator("#memForgetConfirm").is_disabled(),
              str(api.writes))
        page.click("#memForgetCancel")
        page.close()

        api = Api()
        api.post_status = 401
        page, errors = open_console(browser, api)
        search(page)
        open_hits(page)
        page.locator(".sess-forget-btn").nth(0).click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            packs["en"]["sess.forget.itemsOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#memForgetError")).to_contain_text(
            packs["en"]["sess.forget.unauthorized"])
        check("E3 a 401 asks for sign in and fakes no success",
              "sign in required" in page.locator("#memForgetError").inner_text()
              and page.locator("#sessMsg").inner_text() == ""
              and page.locator("#memForgetDialog").is_visible(),
              page.locator("#sessMsg").inner_text())
        page.click("#memForgetCancel")
        page.close()

        api = Api()
        api.post_network = True
        page, errors = open_console(browser, api)
        search(page)
        open_hits(page)
        page.locator(".sess-forget-btn").nth(0).click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            packs["en"]["sess.forget.itemsOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#memForgetError")).to_contain_text(
            packs["en"]["sess.forget.network"])
        check("E4 a dead network says so and fakes no success",
              page.locator("#sessMsg").inner_text() == ""
              and page.locator("#memForgetDialog").is_visible(),
              page.locator("#sessMsg").inner_text())
        page.close()

        # -------------------------------------------------- the idempotence --
        api = Api()
        api.already = True
        page, errors = open_console(browser, api)
        search(page)
        open_hits(page)
        page.locator(".sess-forget-btn").nth(0).click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            packs["en"]["sess.forget.itemsOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#sessMsg")).to_contain_text(
            packs["en"]["sess.forget.already"])
        check("F1 an already-forgotten message says so, and still says that "
              "nothing was deleted",
              "Nothing was deleted" in page.locator("#sessMsg").inner_text(),
              page.locator("#sessMsg").inner_text())
        page.close()

        # ------------------------------------------- the shared dialog still --
        api = Api()
        page, errors = open_console(browser, api, view="overview")
        page.click("#memBrowseBtn")
        page.wait_for_selector(".mem-entry")
        entry = page.locator(".mem-entry").first
        if entry.get_attribute("open") is None:
            entry.locator("summary").click()
        page.locator(".mem-forget-btn").first.click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            packs["en"]["memory.forget.entriesOne"])
        check("G1 the shared dialog still serves the memory view",
              page.locator("#memForgetConfirm").is_enabled(), "")
        page.click("#memForgetCancel")
        page.close()

        # ------------------------------------------------------- the layout --
        for width in (320, 390, 1280):
            api = Api()
            page, errors = open_console(browser, api, width=width)
            search(page)
            open_hits(page)
            page.locator(".sess-forget-btn").nth(0).click()
            expect(page.locator("#memForgetPreview")).to_contain_text(
                packs["en"]["sess.forget.itemsOne"])
            fits = page.evaluate(
                "() => document.documentElement.scrollWidth <= "
                "document.documentElement.clientWidth + 1 && "
                "document.body.scrollWidth <= document.body.clientWidth + 1")
            check(f"H1@{width} the page does not overflow horizontally",
                  fits, str(page.evaluate(
                      "() => [document.documentElement.scrollWidth, "
                      "document.documentElement.clientWidth, "
                      "document.body.scrollWidth, "
                      "document.body.clientWidth]")))
            check(f"H2@{width} the session card does not overflow",
                  page.locator("#sessList").evaluate(
                      "el => el.scrollWidth <= el.clientWidth + 1"), "")
            check(f"H3@{width} the confirmation does not overflow",
                  page.locator("#memForgetDialog").evaluate(
                      "el => el.scrollWidth <= el.clientWidth + 1"), "")
            check(f"H4@{width} no page error", not errors, str(errors[:2]))
            page.close()

        # ------------------------------------------------------ the zh pack --
        api = Api()
        page, errors = open_console(browser, api, lang="zh")
        search(page)
        open_hits(page)
        check("I1 the Chinese pack carries the action label",
              page.locator(".sess-forget-btn").first.text_content().strip()
              == packs["zh"]["sess.forget.btn"],
              page.locator(".sess-forget-btn").first.text_content())
        check("I2 the Chinese pack carries the hits label",
              packs["zh"]["sess.forget.hitsLabel"].replace("{n}", "2")
              in page.locator(".sess-hits").first.inner_text(),
              page.locator(".sess-hits").first.inner_text())
        page.locator(".sess-forget-btn").first.click()
        preview = page.locator("#memForgetPreview")
        expect(preview).to_contain_text(packs["zh"]["sess.forget.itemsOne"])
        check("I3 the Chinese pack carries the preview copy",
              packs["zh"]["sess.forget.limitsSummary"] in preview.inner_text(),
              preview.inner_text()[:200])
        check("I4 the Chinese pack carries the action labels",
              page.locator("#memForgetConfirm").inner_text().strip()
              == packs["zh"]["sess.forget.confirm"]
              and page.locator("#memForgetCancel").inner_text().strip()
              == packs["zh"]["sess.forget.cancel"],
              page.locator("#memForgetConfirm").inner_text())
        page.click("#memForgetCancel")
        check("I5 no page error (zh)", not errors, str(errors[:2]))
        page.close()

        browser.close()

    print("")
    print(f"{PASSED}/{PASSED + FAILED} PASS")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
