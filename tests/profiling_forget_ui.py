#!/usr/bin/env python3
"""The owner-facing FORGET of a derived owner-profile item — the console
UI half, verified offline.

What this proves, with the cached Chromium and NO product process:

  * every keyword of the OWNER ANALYSIS view shows its PROVENANCE — the
    admission channel in plain words plus the short opaque source id —
    and carries its own FORGET action;
  * clicking FORGET opens the SAME confirmation the memory view uses, and
    it FIRST shows the server's read-only preview (how many derived
    items would be silenced, whether the profile, the score and the
    coverage are affected) plus the honest limits sentence; the confirm
    button stays disabled until that preview has arrived;
  * cancelling writes NOTHING (no POST at all);
  * a confirmed forget writes exactly {source_id} with the product's
    Bearer header, the profile is re-read from the API and the keyword is
    gone — and the message says plainly that NOTHING was deleted;
  * the failures are honest: a refused preview, an unknown source, a
    missing token, a 401 and a dead network all say what happened and
    never fake a success;
  * no credential ever reaches the DOM (the token lives in localStorage
    and in the request header only);
  * the layout holds at 390px, 320px and 1280px with zero page errors,
    and the copy is bilingual (en + zh, paired pack keys);
  * the SHARED dialog still serves the memory view (the reuse did not
    break the surface it was borrowed from).

The three API calls are STUBBED here: nothing in the repository, the
profile or the production state dir is read or written.

Run:  python3 tests/profiling_forget_ui.py     (exit 0 = all green)
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
ORIGIN = "http://profforget.test"

# A fixture credential: it exists only in this browser's localStorage and
# in the request header. It must never be visible in the page.
TOKEN = "UI-FIXTURE-TOKEN-4b7e2a91"

STATIC = {
    "/": ("web/static/index.html", "text/html"),
    "/static/app.js": ("web/static/app.js", "text/javascript"),
    "/static/style.css": ("web/static/style.css", "text/css"),
    "/static/i18n/en.json": ("overlay/locales/en.json", "application/json"),
    "/static/i18n/zh.json": ("overlay/locales/zh.json", "application/json"),
}

# The OWNER ANALYSIS read model: every keyword now carries the channel it
# came through and the opaque id a forget would address.
PROFILE = {
    "enabled": True,
    "since": "2026-01-01",
    "top_k": 100,
    "push_mode": "passive",
    "analyzed_since": "2026-01-01",
    "window_changed": False,
    "truncated": False,
    "reason": "",
    "keywords_total": 3,
    "corpus": {"messages_used": 42, "days": 7, "channels": {"web": 12, "tg": 30}},
    "counts": {"traits": {"keen": 1}, "topics": {"work": 2},
               "tiers": {"L1": 1}, "coverage": {"gap": 1}},
    "search": {"enabled": False},
    "gaps": [],
    "cards": [],
    "keywords": [
        {"token": "deploy", "trait": "keen", "topic": "work", "tier": "L1",
         "coverage": "gap", "weak": False, "tf": 9, "days": 4,
         "last_day": "2026-02-01", "score": 3.25,
         "excerpts": [{"day": "2026-02-01", "text": "deploy the console"}],
         "channel": "profile",
         "source_id": "src_aaaabbbbccccdddd1111222233334444"},
        {"token": "console", "trait": "known", "topic": "work", "tier": "L2",
         "coverage": "partial", "weak": False, "tf": 5, "days": 3,
         "last_day": "2026-02-02", "score": 1.5,
         "excerpts": [{"day": "2026-02-02", "text": "the console page"}],
         "channel": "profile",
         "source_id": "src_bbbbccccddddeeee5555666677778888"},
        {"token": "retrofit", "trait": "growth", "topic": "build", "tier": "L3",
         "coverage": "covered", "weak": True, "tf": 2, "days": 1,
         "last_day": "2026-02-03", "score": 0.4,
         "excerpts": [],
         "channel": "profile",
         "source_id": "src_ccccddddeeeeffff9999aaaabbbbcccc"},
    ],
}

# The server's honest limits sentence — shown VERBATIM next to the
# localized one-line summary.
LIMITS = ("The derived item is removed from the owner profile, the owner "
          "score and the coverage map only. The session text that produced "
          "it is NOT deleted, and the session transcript keeps it.")

MEM_LIMITS = ("The source is removed from ACTIVE memory only. The original "
              "session transcript is NOT deleted.")

MEMORY = {
    "slots": [
        {"slot": "memory", "available": True, "file": "MEMORY_curated.md",
         "chars": 120, "truncated": False, "entries": [
             {"title": "Facts", "excerpt": "the owner prefers local tools",
              "truncated": False, "chars": 29,
              "channel": "curated:memory",
              "source_id": "src_aaaabbbbccccdddd1111222233334444"}]},
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
    """The stubbed console API: the three calls, plus honest failures."""

    def __init__(self) -> None:
        self.forgotten: set[str] = set()
        self.writes: list[dict] = []
        self.calls: list[str] = []
        self.profile_fixture = PROFILE
        self.preview_status = 200
        self.preview_delay = 0.0
        self.hold_preview = False
        self.held: list[tuple] = []
        self.post_status = 200
        self.post_network = False
        self.already = False
        self.keep_listed = False        # the server keeps the row listed
        self.break_read = False         # GET /api/profiling breaks after a write

    # ---- the read faces -------------------------------------------------
    def profile(self) -> dict:
        rows = []
        for row in self.profile_fixture["keywords"]:
            if row["source_id"] in self.forgotten and not self.keep_listed:
                continue
            rows.append(row)
        doc = dict(self.profile_fixture)
        doc["keywords"] = rows
        doc["keywords_total"] = len(rows)
        return doc

    def preview(self, source_id: str) -> dict:
        row = None
        for item in self.profile_fixture["keywords"]:
            if item["source_id"] == source_id:
                row = item
        return {"source_id": source_id,
                "channel": (row or {}).get("channel", "profile"),
                "items": 1,
                "affects": {"profile": True, "score": True, "coverage": True},
                "limits": LIMITS}

    def mem_preview(self, slot: str, title: str) -> dict:
        return {"slot": slot, "title": title, "channel": "curated:memory",
                "source_id": "src_aaaabbbbccccdddd1111222233334444",
                "entries": 1,
                "affects": {"injection": True, "memory_view": True,
                            "profile_coverage": True},
                "limits": MEM_LIMITS}

    # ---- the router -----------------------------------------------------
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
        if method == "GET" and path == "/api/profiling":
            self.calls.append("GET /api/profiling")
            if self.break_read and self.forgotten:
                return route.fulfill(status=500, json={"error": "unavailable"})
            return route.fulfill(json=self.profile())
        if method == "GET" and path == "/api/profiling/forget/preview":
            self.calls.append("GET /api/profiling/forget/preview")
            if self.preview_delay:
                time.sleep(self.preview_delay)
            query = urllib.parse.parse_qs(url.query)
            source = (query.get("source") or [""])[0]
            if self.hold_preview:
                # the preview stays PENDING: the confirmation must not be
                # confirmable while the server has not answered yet
                self.held.append((route, source))
                return
            if self.preview_status != 200:
                return route.fulfill(status=self.preview_status,
                                     json={"error": "preview unavailable"})
            return route.fulfill(json=self.preview(source))
        if method == "POST" and path == "/api/profiling/forget":
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
            self.forgotten.add(str(doc.get("source_id")))
            return route.fulfill(json={
                "ok": True,
                "id": str(doc.get("source_id")),
                "channel": "profile",
                "already": self.already,
                "limits": LIMITS})
        if method == "GET" and path == "/api/memory":
            self.calls.append("GET /api/memory")
            return route.fulfill(json=MEMORY)
        if method == "GET" and path == "/api/memory/forget/preview":
            self.calls.append("GET /api/memory/forget/preview")
            query = urllib.parse.parse_qs(url.query)
            slot = (query.get("slot") or [""])[0]
            title = (query.get("title") or [""])[0]
            return route.fulfill(json=self.mem_preview(slot, title))
        if method != "GET":
            return route.fulfill(status=404, json={})
        if path.startswith("/api/"):
            return route.fulfill(json={})
        return route.abort()


def open_console(browser, api: Api, *, lang: str = "en", width: int = 1280,
                  token: str | None = TOKEN, view: str = "sniff"):
    page = browser.new_page(viewport={"width": width, "height": 900},
                             service_workers="block")
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    if token:
        page.add_init_script(
            "window.localStorage.setItem('aidog_token', %s);"
            % json.dumps(token))
    page.route("**/*", api.route)
    page.goto(f"{ORIGIN}/?view={view}&lang={lang}&skipboot=1")
    return page, errors


def open_keywords(page):
    """Open the OWNER ANALYSIS view and wait for the keyword rows."""
    page.wait_for_selector("#profKeywords .prof-row")
    return page.locator("#profKeywords .prof-row")


def forget_btn(page, token_text: str):
    """The FORGET button of one keyword row."""
    row = page.locator("#profKeywords .prof-row").filter(
        has_text=token_text).first
    return row.locator(".prof-forget-btn")


def main() -> int:
    packs = {lang: json.loads((ROOT / "overlay" / "locales" / f"{lang}.json")
                               .read_text(encoding="utf-8"))
             for lang in ("en", "zh")}
    en, zh = packs["en"], packs["zh"]

    # the packs must carry the same keys — the parity lint owns the rule,
    # this test proves the keys this UI needs exist in BOTH packs.
    want = [k for k in en if k.startswith("prof.forget.")]
    check("A0 both packs carry the same prof.forget.* keys",
          sorted(want) == sorted(k for k in zh if k.startswith("prof.forget."))
          and len(want) >= 30, f"{len(want)} en keys")
    check("A0b the packs are the same size (parity)", len(en) == len(zh),
          f"{len(en)} vs {len(zh)}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=chromium_path(),
            args=["--disable-background-networking"])

        # ---------------------------------------------- the happy path ------
        api = Api()
        api.hold_preview = True
        page, errors = open_console(browser, api)
        rows = open_keywords(page)
        check("A1 every keyword of the owner profile is listed",
              rows.count() == 3, str(rows.count()))

        prov = rows.first.locator(".prof-prov")
        check("A2 the keyword shows its channel in plain words",
              "Owner profile" in prov.inner_text(), prov.inner_text())
        check("A3 the keyword shows the short opaque source id",
              "src_aaaabbbb" in prov.inner_text(), prov.inner_text())
        check("A4 the short id is shortened (no full id dump)",
              "src_aaaabbbbccccdddd" not in prov.inner_text(),
              prov.inner_text())
        check("A5 every keyword carries its own FORGET action",
              page.locator("#profKeywords .prof-forget-btn").count() == 3,
              str(page.locator("#profKeywords .prof-forget-btn").count()))
        check("A6 the action is addressed by the server's opaque id",
              forget_btn(page, "deploy").get_attribute("data-prof-forget")
              == "src_aaaabbbbccccdddd1111222233334444",
              str(forget_btn(page, "deploy").get_attribute("data-prof-forget")))
        check("A7 the action carries the keyword for the honest message",
              forget_btn(page, "deploy").get_attribute("data-prof-token")
              == "deploy",
              str(forget_btn(page, "deploy").get_attribute("data-prof-token")))

        forget_btn(page, "deploy").click()
        dialog = page.locator("#memForgetDialog")
        expect(dialog).to_be_visible()
        check("B1 the confirmation opens on FORGET", dialog.is_visible(), "")
        check("B2 the dialog is the shared one, titled for this surface",
              en["prof.forget.title"] in page.locator("#memForgetTitle")
              .inner_text(),
              page.locator("#memForgetTitle").inner_text())
        check("B3 the confirm button is disabled until the preview arrived",
              page.locator("#memForgetConfirm").is_disabled(), "")
        check("B4 nothing was written while the preview was loading",
              api.writes == [], str(api.writes))

        # the server answers the pending preview
        held_route, held_source = api.held[0]
        check("B5 the preview asked for the source id of the keyword",
              held_source == "src_aaaabbbbccccdddd1111222233334444",
              held_source)
        held_route.fulfill(json=api.preview(held_source))

        preview = page.locator("#memForgetPreview")
        expect(preview).to_contain_text(en["prof.forget.itemsOne"])
        expect(preview).to_contain_text(en["prof.forget.affectProfile"])
        expect(preview).to_contain_text(en["prof.forget.affectScore"])
        expect(preview).to_contain_text(en["prof.forget.affectCoverage"])
        check("B6 the preview says how many items would be silenced",
              "1 item would be silenced" in preview.inner_text(),
              preview.inner_text()[:160])
        check("B7 the preview names the three affected surfaces",
              page.locator(".mem-pv-flag.is-on").count() == 3
              and page.locator(".mem-pv-flag.is-off").count() == 0,
              str(page.locator(".mem-pv-item").count()))
        check("B8 the preview carries the honest limits sentence",
              "NOT deleted" in preview.inner_text()
              and en["prof.forget.limitsSummary"] in preview.inner_text(),
              preview.inner_text()[:240])
        check("B9 the confirm button is enabled once the preview is shown",
              page.locator("#memForgetConfirm").is_enabled(), "")

        page.click("#memForgetCancel")
        expect(dialog).to_be_hidden()
        check("C1 cancelling writes nothing", api.writes == [], str(api.writes))
        check("C2 cancelling leaves the keyword listed",
              page.locator("#profKeywords .prof-row").count() == 3,
              str(page.locator("#profKeywords .prof-row").count()))

        api.hold_preview = False
        forget_btn(page, "deploy").click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            en["prof.forget.itemsOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#profForgetStatus")).to_contain_text(
            en["prof.forget.done"].replace("{token}", "deploy"))
        check("D1 the confirmed forget wrote exactly one request",
              len(api.writes) == 1, str(api.writes))
        write = api.writes[0] if api.writes else {"body": {}, "auth": ""}
        check("D2 the write carries the source id and nothing else",
              write["body"] == {"source_id": "src_aaaabbbbccccdddd1111222233334444"},
              str(write["body"]))
        check("D3 the write uses the product's Bearer helper",
              write["auth"] == "Bearer " + TOKEN, write["auth"])
        check("D4 the profile was re-read from the API",
              api.calls.count("GET /api/profiling") >= 2,
              str(api.calls))
        expect(page.locator("#profKeywords .prof-row")).to_have_count(2)
        check("D5 the list was re-read and the keyword is gone",
              "deploy" not in page.locator("#profKeywords").inner_text(),
              page.locator("#profKeywords").inner_text()[:160])
        check("D6 the confirmation says that NOTHING was deleted",
              "Nothing was deleted" in
              page.locator("#profForgetStatus").inner_text(),
              page.locator("#profForgetStatus").inner_text())
        check("D7 the access token never reached the DOM",
              TOKEN not in page.content(), "the token is visible in the page")
        check("D8 no page error", not errors, str(errors[:2]))
        page.close()

        # ------------------------------------------------ the honest errors --
        api = Api()
        api.preview_status = 500
        page, errors = open_console(browser, api)
        open_keywords(page)
        forget_btn(page, "deploy").click()
        expect(page.locator("#memForgetError")).to_contain_text(
            en["prof.forget.previewFailed"])
        check("E1 a refused preview says so and writes nothing",
              api.writes == []
              and page.locator("#memForgetConfirm").is_disabled(),
              str(api.writes))
        page.click("#memForgetCancel")
        page.close()

        api = Api()
        api.preview_status = 404
        page, errors = open_console(browser, api)
        open_keywords(page)
        forget_btn(page, "deploy").click()
        expect(page.locator("#memForgetError")).to_contain_text(
            en["prof.forget.previewMissing"])
        check("E2 an unknown source says so and writes nothing",
              api.writes == []
              and page.locator("#memForgetConfirm").is_disabled(),
              str(api.writes))
        page.click("#memForgetCancel")
        page.close()

        api = Api()
        api.post_status = 401
        page, errors = open_console(browser, api)
        open_keywords(page)
        forget_btn(page, "deploy").click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            en["prof.forget.itemsOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#memForgetError")).to_contain_text(
            en["prof.forget.unauthorized"])
        check("E3 a 401 asks for sign in and fakes no success",
              "sign in required" in page.locator("#memForgetError").inner_text()
              and page.locator("#profForgetStatus").inner_text() == ""
              and page.locator("#memForgetDialog").is_visible(),
              page.locator("#profForgetStatus").inner_text())
        page.click("#memForgetCancel")
        page.close()

        api = Api()
        page, errors = open_console(browser, api, token=None)
        open_keywords(page)
        forget_btn(page, "deploy").click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            en["prof.forget.itemsOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#memForgetError")).to_contain_text(
            en["prof.forget.needToken"])
        check("E4 a missing token asks for sign in and fakes no success",
              api.writes == []
              and page.locator("#profForgetStatus").inner_text() == "",
              str(api.writes))
        page.click("#memForgetCancel")
        page.close()

        api = Api()
        api.post_network = True
        page, errors = open_console(browser, api)
        open_keywords(page)
        forget_btn(page, "deploy").click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            en["prof.forget.itemsOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#memForgetError")).to_contain_text(
            en["prof.forget.network"])
        check("E5 a dead network says so and fakes no success",
              page.locator("#profForgetStatus").inner_text() == ""
              and page.locator("#memForgetDialog").is_visible(),
              page.locator("#profForgetStatus").inner_text())
        page.close()

        # -------------------------------------------------- the idempotence --
        api = Api()
        api.already = True
        page, errors = open_console(browser, api)
        open_keywords(page)
        forget_btn(page, "deploy").click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            en["prof.forget.itemsOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#profForgetStatus")).to_contain_text(
            en["prof.forget.already"])
        check("F1 an already-forgotten item says so, and still says that "
              "nothing was deleted",
              "Nothing was deleted"
              in page.locator("#profForgetStatus").inner_text(),
              page.locator("#profForgetStatus").inner_text())
        page.close()

        # ------------------------------ the honest edges of the re-read ----
        api = Api()
        api.keep_listed = True
        page, errors = open_console(browser, api)
        open_keywords(page)
        forget_btn(page, "deploy").click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            en["prof.forget.itemsOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#profForgetStatus")).to_contain_text(
            en["prof.forget.stillListed"])
        check("G1 an item the server still lists is said so honestly",
              "still listed" in page.locator("#profForgetStatus").inner_text()
              and "Nothing was deleted"
              in page.locator("#profForgetStatus").inner_text(),
              page.locator("#profForgetStatus").inner_text())
        page.close()

        api = Api()
        api.break_read = True
        page, errors = open_console(browser, api)
        open_keywords(page)
        forget_btn(page, "deploy").click()
        expect(page.locator("#memForgetPreview")).to_contain_text(
            en["prof.forget.itemsOne"])
        page.click("#memForgetConfirm")
        expect(page.locator("#profForgetStatus")).to_contain_text(
            en["prof.forget.rereadFailed"])
        check("G2 a profile that cannot be re-read is said so honestly",
              "could not be re-read"
              in page.locator("#profForgetStatus").inner_text(),
              page.locator("#profForgetStatus").inner_text())
        page.close()

        # ------------------------- the shared dialog still serves memory ----
        api = Api()
        page, errors = open_console(browser, api, view="overview")
        page.click("#memBrowseBtn")
        page.wait_for_selector(".mem-entry")
        entry = page.locator(".mem-entry").first
        if entry.get_attribute("open") is None:
            entry.locator("summary").click()      # the action lives inside
        page.locator(".mem-forget-btn").first.click()
        expect(page.locator("#memForgetDialog")).to_be_visible()
        check("R1 the memory view still uses the shared dialog",
              en["memory.forget.title"]
              in page.locator("#memForgetTitle").inner_text(),
              page.locator("#memForgetTitle").inner_text())
        expect(page.locator("#memForgetPreview")).to_contain_text(
            en["memory.forget.entriesOne"])
        check("R2 the memory preview still renders its own surfaces",
              page.locator(".mem-pv-flag.is-on").count() == 3,
              str(page.locator(".mem-pv-item").count()))
        page.click("#memForgetCancel")
        check("R3 the memory surface still writes nothing on cancel",
              api.writes == [], str(api.writes))
        check("R4 no page error (memory)", not errors, str(errors[:2]))
        page.close()

        # ------------------------------------------------------- the layout --
        for width in (320, 390, 1280):
            api = Api()
            page, errors = open_console(browser, api, width=width)
            open_keywords(page)
            forget_btn(page, "deploy").click()
            expect(page.locator("#memForgetPreview")).to_contain_text(
                en["prof.forget.itemsOne"])
            fits = page.evaluate(
                "() => document.documentElement.scrollWidth <= "
                "document.documentElement.clientWidth + 1 && "
                "document.body.scrollWidth <= document.body.clientWidth + 1")
            check(f"G3@{width} the page does not overflow horizontally",
                  fits, str(page.evaluate(
                      "() => [document.documentElement.scrollWidth, "
                      "document.documentElement.clientWidth, "
                      "document.body.scrollWidth, "
                      "document.body.clientWidth]")))
            check(f"G4@{width} the owner-analysis card does not overflow",
                  page.locator("#profKeywords").evaluate(
                      "el => el.scrollWidth <= el.clientWidth + 1"), "")
            check(f"G5@{width} the keyword row does not overflow",
                  page.locator("#profKeywords .prof-row").first.evaluate(
                      "el => el.scrollWidth <= el.clientWidth + 1"), "")
            check(f"G6@{width} the confirmation does not overflow",
                  page.locator("#memForgetDialog").evaluate(
                      "el => el.scrollWidth <= el.clientWidth + 1"), "")
            check(f"G7@{width} no page error", not errors, str(errors[:2]))
            page.close()

        # ------------------------------------------------------ the zh pack --
        api = Api()
        page, errors = open_console(browser, api, lang="zh")
        open_keywords(page)
        prov = page.locator("#profKeywords .prof-prov").first
        expect(prov).to_contain_text(zh["prof.forget.chanProfile"])
        check("H1 the Chinese pack carries the channel label",
              zh["prof.forget.chanProfile"] in prov.inner_text(),
              prov.inner_text())
        check("H2 the Chinese pack carries the action label",
              page.locator("#profKeywords .prof-forget-btn").first
              .text_content().strip() == zh["prof.forget.btn"],
              page.locator("#profKeywords .prof-forget-btn").first
              .text_content())
        forget_btn(page, "deploy").click()
        preview = page.locator("#memForgetPreview")
        expect(preview).to_contain_text(zh["prof.forget.itemsOne"])
        check("H3 the Chinese pack carries the preview copy",
              zh["prof.forget.limitsSummary"] in preview.inner_text(),
              preview.inner_text()[:200])
        check("H4 the Chinese pack carries the action labels",
              page.locator("#memForgetConfirm").inner_text().strip()
              == zh["prof.forget.confirm"]
              and page.locator("#memForgetCancel").inner_text().strip()
              == zh["prof.forget.cancel"],
              page.locator("#memForgetConfirm").inner_text())
        page.click("#memForgetConfirm")
        expect(page.locator("#profForgetStatus")).to_contain_text(
            zh["prof.forget.done"].replace("{token}", "deploy"))
        check("H5 the Chinese pack carries the honest confirmation",
              "\u6ca1\u6709\u5220\u9664\u4efb\u4f55\u5185\u5bb9" in page.locator("#profForgetStatus").inner_text(),
              page.locator("#profForgetStatus").inner_text())
        check("H6 no page error (zh)", not errors, str(errors[:2]))
        page.close()

        browser.close()

    print("")
    print(f"{PASSED}/{PASSED + FAILED} PASS")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
