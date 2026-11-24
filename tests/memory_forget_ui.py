#!/usr/bin/env python3
"""The owner-facing FORGET control — the console UI half, verified offline.

What this proves, with the cached Chromium and NO product process:

  * every browsable memory entry shows its PROVENANCE — the admission
    channel in plain words plus the short opaque source id — and carries
    its own FORGET action;
  * clicking FORGET opens a confirmation that FIRST shows the server's
    read-only preview (how many entries would be filtered, whether prompt
    injection, the memory view and the owner profile coverage are
    affected) and the honest limits sentence; the confirm button stays
    disabled until that preview has arrived;
  * cancelling writes NOTHING (no POST at all);
  * a confirmed forget writes exactly {slot, title} with the product's
    Bearer header, the list is re-read from the API and the entry is
    gone — and the message says plainly that NOTHING was deleted;
  * the failures are honest: a refused preview, a 401 and a dead network
    all say what happened and never fake a success;
  * no credential ever reaches the DOM (the token lives in localStorage
    and in the request header only);
  * the layout holds at 390px, 320px and 1280px with zero page errors,
    and the copy is bilingual (en + zh, paired pack keys).

The three API calls are STUBBED here: nothing in the repository, the
profile or the production state dir is read or written.

Run:  python3 tests/memory_forget_ui.py     (exit 0 = all green)
Set FLEET_TEST_CHROMIUM to override the browser binary; otherwise the
newest cached chromium under ~/.cache/ms-playwright is used.
English output per repo i18n policy.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://memforget.test"

# A fixture credential: it exists only in this browser's localStorage and
# in the request header. It must never be visible in the page.
TOKEN = "UI-FIXTURE-TOKEN-8f2c1d0b"

STATIC = {
    "/": ("web/static/index.html", "text/html"),
    "/static/app.js": ("web/static/app.js", "text/javascript"),
    "/static/style.css": ("web/static/style.css", "text/css"),
    "/static/i18n/en.json": ("overlay/locales/en.json", "application/json"),
    "/static/i18n/zh.json": ("overlay/locales/zh.json", "application/json"),
}

MEMORY = {
    "slots": [
        {"slot": "memory", "available": True, "file": "MEMORY_curated.md",
         "chars": 120, "truncated": False, "entries": [
             {"title": "Facts", "excerpt": "the owner prefers local tools",
              "truncated": False, "chars": 29,
              "channel": "curated:memory",
              "source_id": "src_aaaabbbbccccdddd1111222233334444"},
             {"title": "Notes", "excerpt": "second entry about the console",
              "truncated": False, "chars": 30,
              "channel": "curated:memory",
              "source_id": "src_bbbbccccddddeeee5555666677778888"},
         ]},
        {"slot": "user", "available": True, "file": "USER_curated.md",
         "chars": 40, "truncated": False, "entries": [
             {"title": "Profile", "excerpt": "writes in Chinese",
              "truncated": False, "chars": 16,
              "channel": "curated:user",
              "source_id": "src_ccccddddeeeeffff9999aaaabbbbcccc"},
         ]},
    ],
    "entries_total": 3,
    "query": "",
    "read_only": True,
    "local_only": True,
    "editable": False,
    "note": "read-only view",
}

LIMITS = ("The source is removed from ACTIVE memory only: prompt injection, "
          "the console memory view, the owner profile, recall and restored "
          "backups. The original session transcript and the copies that "
          "already left this machine are NOT deleted.")

CHANNEL_BY_SLOT = {"memory": "curated:memory", "user": "curated:user",
                   "learned": "learned"}

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
    """The stubbed console API: the three calls, plus honest failures."""

    def __init__(self) -> None:
        self.forgotten: set[tuple[str, str]] = set()
        self.writes: list[dict] = []
        self.calls: list[str] = []
        self.memory_fixture = MEMORY
        self.preview_status = 200
        self.preview_delay = 0.0
        self.hold_preview = False
        self.held: list[tuple] = []
        self.post_status = 200
        self.post_network = False
        self.already = False

    def memory(self) -> dict:
        slots = []
        for slot in self.memory_fixture["slots"]:
            entries = [e for e in slot["entries"]
                       if (slot["slot"], e["title"]) not in self.forgotten]
            slots.append(dict(slot, entries=entries,
                              entries_total=len(entries)))
        return dict(self.memory_fixture, slots=slots,
                    entries_total=sum(len(s["entries"]) for s in slots))

    def preview(self, slot: str, title: str) -> dict:
        entry = None
        for group in self.memory_fixture["slots"]:
            for item in group["entries"]:
                if item["title"] == title:
                    entry = item
        return {"slot": slot, "title": title,
                "channel": (entry or {}).get("channel")
                           or CHANNEL_BY_SLOT.get(slot, ""),
                "source_id": (entry or {}).get("source_id", ""),
                "revision": "a" * 64,
                "entries": 1,
                "affects": {"injection": True,
                            "memory_view": slot in ("memory", "user"),
                            "profile_coverage": slot in ("memory", "user")},
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
            self.calls.append("GET /api/memory")
            return route.fulfill(json=self.memory())
        if method == "GET" and path == "/api/memory/forget/preview":
            self.calls.append("GET /api/memory/forget/preview")
            if self.preview_delay:
                time.sleep(self.preview_delay)
            query = urllib.parse.parse_qs(url.query)
            slot = (query.get("slot") or [""])[0]
            title = (query.get("title") or [""])[0]
            if self.hold_preview:
                # the preview stays PENDING: the confirmation must not be
                # confirmable while the server has not answered yet
                self.held.append((route, slot, title))
                return
            if self.preview_status != 200:
                return route.fulfill(status=self.preview_status,
                                     json={"error": "preview unavailable"})
            return route.fulfill(json=self.preview(slot, title))
        if method == "POST" and path == "/api/memory/forget":
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
            self.forgotten.add((str(doc.get("slot")), str(doc.get("title"))))
            return route.fulfill(json={
                "ok": True,
                "id": self.preview(str(doc.get("slot")),
                                   str(doc.get("title")))["source_id"],
                "channel": CHANNEL_BY_SLOT.get(str(doc.get("slot")), ""),
                "already": self.already,
                "limits": LIMITS})
        if method != "GET":
            return route.fulfill(status=404, json={})
        if path.startswith("/api/"):
            return route.fulfill(json={})
        return route.abort()


def open_console(browser, api: Api, *, lang: str = "en", width: int = 1280):
    page = browser.new_page(viewport={"width": width, "height": 900},
                             service_workers="block")
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(
        "window.localStorage.setItem('aidog_token', %s);"
        % json.dumps(TOKEN))
    page.route("**/*", api.route)
    page.goto(f"{ORIGIN}/?view=overview&lang={lang}&skipboot=1")
    return page, errors


def open_browse(page):
    """Open the memory browser and wait for the entries to paint."""
    page.click("#memBrowseBtn")
    page.wait_for_selector(".mem-entry")
    return page.locator(".mem-entry")


def first_forget(page, title: str):
    """The FORGET button of one entry (the entry is expanded first)."""
    entry = page.locator(".mem-entry").filter(has_text=title).first
    if entry.get_attribute("open") is None:
        entry.locator("summary").click()
    return entry.locator(".mem-forget-btn")


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
        entries = open_browse(page)
        check("A1 every entry of the memory view is listed",
              entries.count() == 3, str(entries.count()))

        prov = entries.first.locator(".mem-entry-prov")
        check("A2 the entry shows its channel in plain words",
              "Curated memory" in prov.inner_text(), prov.inner_text())
        check("A3 the entry shows the short opaque source id",
              "src_aaaabbbb" in prov.inner_text(), prov.inner_text())
        check("A4 the short id is the opaque id, shortened (no full dump)",
              "src_aaaabbbbccccdddd" not in prov.inner_text(),
              prov.inner_text())
        check("A5 every entry carries its own FORGET action",
              page.locator(".mem-forget-btn").count() == 3,
              str(page.locator(".mem-forget-btn").count()))
        check("A6 the user slot shows its own channel",
              "User profile" in page.locator(".mem-entry").nth(2)
              .locator(".mem-entry-prov").inner_text(),
              page.locator(".mem-entry").nth(2)
              .locator(".mem-entry-prov").inner_text())

        button = first_forget(page, "Facts")
        button.click()
        dialog = page.locator("#memForgetDialog")
        expect(dialog).to_be_visible()
        check("B1 the confirmation opens on FORGET", dialog.is_visible(), "")
        check("B2 the confirm button is disabled until the preview arrived",
              page.locator("#memForgetConfirm").is_disabled(), "")
        check("B3 nothing was written while the preview was loading",
              api.writes == [], str(api.writes))

        # the server answers the pending preview
        held_route, held_slot, held_title = api.held[0]
        check("B3b the preview asked for the slot and the title of the entry",
              held_slot == "memory" and held_title == "Facts",
              f"{held_slot} / {held_title}")
        held_route.fulfill(json=api.preview(held_slot, held_title))

        preview = page.locator("#memForgetPreview")
        expect(preview).to_contain_text(packs["en"]["memory.forget.entriesOne"])
        expect(preview).to_contain_text(packs["en"]["memory.forget.affectInjection"])
        expect(preview).to_contain_text(packs["en"]["memory.forget.affectView"])
        expect(preview).to_contain_text(packs["en"]["memory.forget.affectProfile"])
        check("B4 the preview says how many entries would be filtered",
              "1 entry would be filtered" in preview.inner_text(),
              preview.inner_text()[:160])
        check("B5 the preview names the three affected surfaces",
              page.locator(".mem-pv-flag.is-on").count() == 3
              and page.locator(".mem-pv-flag.is-off").count() == 0,
              str(page.locator(".mem-pv-item").count()))
        check("B6 the preview carries the honest limits sentence",
              "NOT deleted" in preview.inner_text()
              and packs["en"]["memory.forget.limitsSummary"]
              in preview.inner_text(), preview.inner_text()[:200])
        check("B7 the confirm button is enabled once the preview is shown",
              page.locator("#memForgetConfirm").is_enabled(), "")

        page.click("#memForgetCancel")
        expect(dialog).to_be_hidden()
        check("C1 cancelling writes nothing", api.writes == [], str(api.writes))
        check("C2 cancelling leaves the entry in the list",
              page.locator(".mem-entry").count() == 3,
              str(page.locator(".mem-entry").count()))

        api.hold_preview = False
        first_forget(page, "Facts").click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            packs["en"]["memory.forget.entriesOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#memForgetStatus")).to_contain_text(
            packs["en"]["memory.forget.done"].replace("{title}", "Facts"))
        check("D1 the confirmed forget wrote exactly one request",
              len(api.writes) == 1, str(api.writes))
        write = api.writes[0] if api.writes else {"body": {}, "auth": ""}
        check("D2 the write carries the exact source ID and preview revision",
              write["body"] == {"slot": "memory", "title": "Facts",
                                "source_id": "src_aaaabbbbccccdddd1111222233334444",
                                "revision": "a" * 64},
              str(write["body"]))
        check("D3 the write uses the product's Bearer helper",
              write["auth"] == "Bearer " + TOKEN, write["auth"])
        expect(page.locator(".mem-entry")).to_have_count(2)
        check("D4 the list was re-read and the entry is gone",
              "Facts" not in page.locator("#memBrowseList").inner_text(),
              page.locator("#memBrowseList").inner_text()[:160])
        check("D5 the confirmation says that NOTHING was deleted",
              "Nothing was deleted" in
              page.locator("#memForgetStatus").inner_text(),
              page.locator("#memForgetStatus").inner_text())
        check("D6 the access token never reached the DOM",
              TOKEN not in page.content(), "the token is visible in the page")
        check("D7 no page error", not errors, str(errors[:2]))
        page.close()

        # ------------------------------------------------ the honest errors --
        api = Api()
        api.preview_status = 500
        page, errors = open_console(browser, api)
        open_browse(page)
        first_forget(page, "Facts").click()
        expect(page.locator("#memForgetError")).to_contain_text(
            packs["en"]["memory.forget.previewFailed"])
        check("E1 a refused preview says so and writes nothing",
              api.writes == []
              and page.locator("#memForgetConfirm").is_disabled(),
              str(api.writes))
        page.click("#memForgetCancel")
        page.close()

        api = Api()
        api.preview_status = 404
        page, errors = open_console(browser, api)
        open_browse(page)
        first_forget(page, "Facts").click()
        expect(page.locator("#memForgetError")).to_contain_text(
            packs["en"]["memory.forget.previewMissing"])
        check("E1b a preview that cannot find the entry says so and writes "
              "nothing",
              api.writes == []
              and page.locator("#memForgetConfirm").is_disabled(),
              str(api.writes))
        page.click("#memForgetCancel")
        page.close()

        api = Api()
        api.post_status = 401
        page, errors = open_console(browser, api)
        open_browse(page)
        first_forget(page, "Facts").click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            packs["en"]["memory.forget.entriesOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#memForgetError")).to_contain_text(
            packs["en"]["memory.forget.unauthorized"])
        check("E2 a 401 asks for sign in and fakes no success",
              "sign in required" in page.locator("#memForgetError").inner_text()
              and page.locator("#memForgetStatus").inner_text() == ""
              and page.locator("#memForgetDialog").is_visible(),
              page.locator("#memForgetStatus").inner_text())
        page.click("#memForgetCancel")
        page.close()

        api = Api()
        api.post_network = True
        page, errors = open_console(browser, api)
        open_browse(page)
        first_forget(page, "Facts").click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            packs["en"]["memory.forget.entriesOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#memForgetError")).to_contain_text(
            packs["en"]["memory.forget.network"])
        check("E3 a dead network says so and fakes no success",
              page.locator("#memForgetStatus").inner_text() == ""
              and page.locator("#memForgetDialog").is_visible(),
              page.locator("#memForgetStatus").inner_text())
        page.close()

        # -------------------------------------------------- the idempotence --
        api = Api()
        api.already = True
        page, errors = open_console(browser, api)
        open_browse(page)
        first_forget(page, "Facts").click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            packs["en"]["memory.forget.entriesOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#memForgetStatus")).to_contain_text(
            packs["en"]["memory.forget.already"])
        check("F1 an already-forgotten source says so, and still says that "
              "nothing was deleted",
              "Nothing was deleted"
              in page.locator("#memForgetStatus").inner_text(),
              page.locator("#memForgetStatus").inner_text())
        page.close()

        # -------------------------------------------- the learned channel ---
        api = Api()
        api.memory_fixture = json.loads(json.dumps(MEMORY))
        api.memory_fixture["slots"].append({
            "slot": "learned", "available": True, "file": "EVOLUTION_learned.md",
            "chars": 60, "truncated": False, "entries": [
                {"title": "2026-01-02 (learn)", "excerpt": "learned note",
                 "truncated": False, "chars": 12,
                 "channel": "learned",
                 "source_id": "src_ddddeeeeffff00001111222233334444"}]})
        api.hold_preview = True
        page, errors = open_console(browser, api)
        open_browse(page)
        first_forget(page, "2026-01-02 (learn)").click()
        held_route, held_slot, held_title = api.held[0]
        check("I1 the learned channel is addressed as the learned slot",
              held_slot == "learned" and held_title == "2026-01-02 (learn)",
              f"{held_slot} / {held_title}")
        held_route.fulfill(json=api.preview(held_slot, held_title))
        expect(page.locator("#memForgetPreview")).to_contain_text(
            packs["en"]["memory.forget.entriesOne"])
        check("I2 the learned slot's preview says which surfaces it touches",
              page.locator(".mem-pv-flag.is-on").count() == 1
              and page.locator(".mem-pv-flag.is-off").count() == 2,
              str(page.locator(".mem-pv-item").count()))
        page.click("#memForgetCancel")
        check("I3 no page error (learned)", not errors, str(errors[:2]))
        page.close()

        # ------------------------------------------------------- the layout --
        for width in (320, 390, 1280):
            api = Api()
            page, errors = open_console(browser, api, width=width)
            open_browse(page)
            first_forget(page, "Facts").click()
            expect(page.locator("#memForgetPreview")).to_contain_text(
                packs["en"]["memory.forget.entriesOne"])
            fits = page.evaluate(
                "() => document.documentElement.scrollWidth <= "
                "document.documentElement.clientWidth + 1 && "
                "document.body.scrollWidth <= document.body.clientWidth + 1")
            check(f"G1@{width} the page does not overflow horizontally",
                  fits, str(page.evaluate(
                      "() => [document.documentElement.scrollWidth, "
                      "document.documentElement.clientWidth, "
                      "document.body.scrollWidth, "
                      "document.body.clientWidth]")))
            check(f"G2@{width} the memory card does not overflow",
                  page.locator("#memBrowseCard").evaluate(
                      "el => el.scrollWidth <= el.clientWidth + 1"), "")
            check(f"G3@{width} the confirmation does not overflow",
                  page.locator("#memForgetDialog").evaluate(
                      "el => el.scrollWidth <= el.clientWidth + 1"), "")
            check(f"G4@{width} no page error", not errors, str(errors[:2]))
            page.close()

        # ------------------------------------------------------ the zh pack --
        api = Api()
        page, errors = open_console(browser, api, lang="zh")
        open_browse(page)
        prov = page.locator(".mem-entry-prov").first
        check("H1 the Chinese pack carries the channel label",
              packs["zh"]["memory.forget.chanMemory"] in prov.inner_text(),
              prov.inner_text())
        check("H2 the Chinese pack carries the action label",
              page.locator(".mem-forget-btn").first.text_content().strip()
              == packs["zh"]["memory.forget.btn"],
              page.locator(".mem-forget-btn").first.text_content())
        first_forget(page, "Facts").click()
        preview = page.locator("#memForgetPreview")
        expect(preview).to_contain_text(
            packs["zh"]["memory.forget.entriesOne"])
        check("H3 the Chinese pack carries the preview copy",
              packs["zh"]["memory.forget.limitsSummary"] in preview.inner_text(),
              preview.inner_text()[:160])
        check("H4 the Chinese pack carries the action labels",
              page.locator("#memForgetConfirm").inner_text().strip()
              == packs["zh"]["memory.forget.confirm"]
              and page.locator("#memForgetCancel").inner_text().strip()
              == packs["zh"]["memory.forget.cancel"],
              page.locator("#memForgetConfirm").inner_text())
        page.click("#memForgetCancel")
        check("H5 no page error (zh)", not errors, str(errors[:2]))
        page.close()

        browser.close()

    print("")
    print(f"{PASSED}/{PASSED + FAILED} PASS")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
