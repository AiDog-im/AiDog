#!/usr/bin/env python3
"""Offline Stream fleet assertions with Playwright and an installed Chromium.

The browser routes every request to fixtures or aborts it. No console process
or profile data is required. Run: python3 tests/fleet_stream_browser.py
Set FLEET_TEST_CHROMIUM to override the Playwright browser executable.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://fleet.test"
SHARED_ID = "f-shared-" + "x" * 100 + "<fixture>"
PLANS = {"plans": [
    {"id": "plan-pending", "fleet": SHARED_ID, "status": "pending", "clones": 4,
     "ts": 1795276800, "desc": "private plan description", "tools": ["write_file"]},
    {"id": SHARED_ID, "status": "consumed", "clones": 3},
    {"id": "plan-approved", "status": "approved", "clones": 2},
    {"id": "plan-cancelled", "status": "cancelled", "clones": 0},
    {"id": "plan-rejected", "status": "rejected", "clones": 1},
    {"id": "plan-legacy", "status": "constructor", "desc": "private plan description"},
]}
HISTORY = {"fleets": [
    {"id": SHARED_ID, "ts": 1795276800, "expected_clones": 4, "incomplete": True,
     "clones": [
         {"clone": "c1", "task_digest": "cafe0000beef", "task": "private task brief",
          "excerpt": "private task excerpt", "role": "analyst", "band": "L",
          "stop": "final", "failed": False, "usage_complete": True, "in_tok": 0, "out_tok": 17},
         {"clone": "c2", "stop": "deadline", "failed": True, "usage_complete": False,
          "in_tok": 99, "out_tok": 99},
         {"clone": "c3", "failed": False, "task_digest": "invalid raw digest string"},
     ]},
    {"id": "f-older", "clones": [{"clone": "c1", "stop": "final", "failed": False}]},
]}
STATIC = {
    "/": ("web/static/index.html", "text/html"),
    "/static/app.js": ("web/static/app.js", "text/javascript"),
    "/static/style.css": ("web/static/style.css", "text/css"),
    "/static/i18n/en.json": ("overlay/locales/en.json", "application/json"),
    "/static/i18n/zh.json": ("overlay/locales/zh.json", "application/json"),
}



def chromium_path() -> str | None:
    """The cached Chromium: the env override first, then the newest one.

    A gate that cannot run is a gate that does not run — the fleet browser
    test used to pass no executable at all, so Playwright looked for a
    build that is not installed here."""
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

def stream_page(browser, history=HISTORY, plans=PLANS, *, lang="en", width=320):
    page = browser.new_page(viewport={"width": width, "height": 780}, service_workers="block")
    calls, writes, errors = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route_request(route):
        url = urlsplit(route.request.url)
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            return route.abort()
        if route.request.method != "GET":
            writes.append((route.request.method, url.path))
            return route.abort()
        if url.path in STATIC:
            rel, content_type = STATIC[url.path]
            return route.fulfill(body=(ROOT / rel).read_text(encoding="utf-8"), content_type=content_type)
        if url.path.startswith("/api/fleet"):
            calls.append(url.path)
            payload = {"/api/fleet": history, "/api/fleet/plans": plans}[url.path]
            if isinstance(payload, int):
                return route.fulfill(status=payload, json={"plans": [], "fleets": []})
            if payload is None:
                return route.abort()
            return route.fulfill(json=payload)
        return route.fulfill(json={}) if url.path.startswith("/api/") else route.abort()

    page.route("**/*", route_request)
    page.goto(f"{ORIGIN}/?view=stream&lang={lang}")
    return page, calls, writes, errors


def check_settings_queue(browser):
    page, _, writes, errors = stream_page(browser)
    go = page.locator("#fleetGoApprovals")
    expect(go).to_be_visible()
    held = []
    plans_url = f"{ORIGIN}/api/fleet/plans"
    page.route(plans_url, lambda route: held.append(route))
    with page.expect_request(plans_url):
        go.click()
    empty = page.locator("#fleetPlanEmpty")
    unavailable = page.locator("#fleetPlanUnavailable")
    actions = page.locator("#fleetPlanList [data-fplan-decide]")
    status = page.locator("#fleetPlanMsg")
    refresh = page.locator("#fleetPlanRefresh")
    expect(refresh).to_be_visible()
    expect(empty).to_be_hidden()
    expect(unavailable).to_be_hidden()
    expect(actions).to_have_count(0)
    held.pop().fulfill(json=PLANS)
    expect(actions).to_have_count(2)
    page.unroute(plans_url)

    # A recorded decision refreshes automatically. Preserve its receipt while
    # dropping stale rows, even if the failed response contains a plans array.
    page.evaluate("localStorage.setItem('aidog_token', 'fixture-token')")
    decision_url = plans_url + "/plan-pending"
    page.route(decision_url, lambda route: route.fulfill(json={"ok": True}))
    page.route(plans_url, lambda route: route.fulfill(status=503, json=PLANS))
    with page.expect_request(decision_url) as decision:
        page.locator('#fleetPlanList [data-fplan-decide="reject"]').click()
    assert decision.value.method == "POST" and decision.value.post_data_json == {"action": "reject"}
    expect(unavailable).to_be_visible()
    expect(empty).to_be_hidden()
    expect(actions).to_have_count(0)
    expect(status).to_have_text("✓ recorded")

    page.unroute(plans_url)
    page.route(plans_url, lambda route: route.fulfill(json={"plans": []}))
    refresh.click()
    expect(empty).to_be_visible()
    expect(unavailable).to_be_hidden()
    expect(status).to_have_text("✓ recorded")
    page.unroute(plans_url)
    page.route(plans_url, lambda route: route.fulfill(json={"plans": [], "note": "fixture failure"}))
    refresh.click()
    expect(unavailable).to_be_visible()
    expect(empty).to_be_hidden()
    expect(status).to_have_text("✓ recorded")
    assert not writes and not errors, (writes, errors)
    page.close()


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=chromium_path(),
            args=["--disable-background-networking"],
        )
        page, calls, writes, errors = stream_page(browser)
        approvals = page.locator("#fleetApprovals")
        history = page.locator("#fleetList")
        board = page.locator(".fleet-board")
        expect(approvals.locator(".fleet-task .chip")).to_have_text(
            ["Awaiting approval", "used", "approved", "cancelled", "rejected", "Unknown"])
        expect(approvals).to_contain_text("Requested clones: 4")
        expect(history.locator("details").first).to_contain_text("3 of 4 clone runs recorded")
        expect(history.locator("details").first.locator(".fleet-task .chip")).to_have_text(
            ["Completed", "Failed", "Unknown"])
        expect(history).to_contain_text("Deadline reached")
        expect(history).to_contain_text("cafe0000beef")
        expect(history).to_contain_text("0 / 17")
        expect(history).not_to_contain_text("99 / 99")
        expect(history).not_to_contain_text("plan-pending")
        expect(approvals).not_to_contain_text("cafe0000beef")
        for hidden_text in ("private plan description", "private task brief", "private task excerpt",
                            "invalid raw digest string", "write_file"):
            expect(board).not_to_contain_text(hidden_text)
        expect(board.locator("fixture")).to_have_count(0)
        expect(board.locator("a, input, textarea, button")).to_have_count(1)
        expect(page.locator("#fleetBadge")).to_have_text("2")
        assert sorted(calls) == ["/api/fleet", "/api/fleet/plans"], calls

        # Verify narrow cards (including an unbroken reference) and expansion.
        assert board.evaluate("el => el.scrollWidth <= el.clientWidth")
        assert board.evaluate("el => [...el.querySelectorAll('.fleet-task')].every(card => card.scrollWidth <= card.clientWidth)")
        history.locator("details").last.locator("summary").click()
        expect(history.locator("details[open]")).to_have_count(2)
        zh = json.loads((ROOT / "overlay/locales/zh.json").read_text(encoding="utf-8"))
        page.locator("#langBtn").click()
        expect(history).to_contain_text(zh["fleet.done"])
        expect(approvals).to_contain_text(zh["fleet.awaitingApproval"])
        expect(history.locator("details[open]")).to_have_count(2)
        assert sorted(calls) == ["/api/fleet", "/api/fleet/plans"], calls
        page.locator("#fleetGoApprovals").click()
        expect(page.locator("#fleetPlanCard")).to_have_class("card is-focus")
        expect(page.locator("#view-settings")).to_be_visible()
        assert not writes, writes
        assert not errors, errors
        page.close()

        page, _, writes, errors = stream_page(browser, history=503, lang="zh", width=390)
        expect(page.locator("#fleetList")).to_contain_text(zh["fleet.historyUnavailable"])
        expect(page.locator("#fleetApprovals")).to_contain_text(zh["fleet.awaitingApproval"])
        expect(page.locator("#fleetGoApprovals")).to_have_text(zh["fleet.goApprovals"])
        assert page.locator(".fleet-board").evaluate("el => el.scrollWidth <= el.clientWidth")
        expect(page.locator("#fleetGoApprovals")).to_be_visible()
        assert not writes and not errors, (writes, errors)
        page.close()

        page, _, writes, errors = stream_page(browser, plans={"plans": [], "note": "fixture failure"})
        expect(page.locator("#fleetApprovals")).to_contain_text("Approval records unavailable.")
        expect(page.locator("#fleetList")).to_contain_text("Completed")
        expect(page.locator("#fleetGoApprovals")).to_be_hidden()
        assert not writes and not errors, (writes, errors)
        page.close()

        page, _, writes, errors = stream_page(browser, history={"fleets": []}, plans={"plans": []}, width=1280)
        expect(page.locator("#fleetApprovals")).to_contain_text("No fleet approvals recorded.")
        expect(page.locator("#fleetList")).to_contain_text("No clone runs recorded.")
        expect(page.locator("#fleetGoApprovals")).to_be_hidden()
        assert not writes and not errors, (writes, errors)
        check_settings_queue(browser)
        browser.close()
    print("PASS: Stream fleet cards, independent snapshots, narrow layouts, and approval queue recovery")


if __name__ == "__main__":
    main()
