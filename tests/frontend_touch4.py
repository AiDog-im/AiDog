#!/usr/bin/env python3
"""The batch 4-5 "frontend touch four" — the console UI half.

What this proves, with the cached Chromium and NO product process:

  1. Ctrl/Cmd+K opens the command palette; the query ranks the right GROUP
     first (views / models / actions), Arrow keys move aria-activedescendant,
     Enter runs the entry (a view jump, a model pick), Esc closes and focus
     returns to the element that opened it, and Tab is trapped inside the
     layer (the input is the only tabbable node);
  2. consecutive tool frames collapse into ONE compact timeline line whose
     segments keep each tool's own name, count and honest mark (read x2 ok,
     bash failed) — a text delta starts a new burst, and different tools are
     never lumped into one indistinguishable blob;
  3. reasoning folds to ONE quiet line by default (aria-expanded=false, the
     body hidden) and expands on click — both for the LIVE turn (the text the
     model narrated before its next tool call, which the `done` frame used to
     wipe) and for a HISTORY run of interim assistant rows (the rows stay
     direct children of #chatLog so the older-page prepend still lands above
     the fold);
  4. the amber WRITE TARGET banner always names the profile the console
     writes to (multi-profile: "studio"; default profile: "default"), and
     every new text pair clears WCAG AA — measured in-page with the
     relative-luminance rule, not read off a screenshot;
  5. keyboard reachability (the trigger is Tab-reachable, Enter opens it),
     the shared confirm dialog is a spring bottom sheet below 760px with a
     drag-to-dismiss grip, the bilingual packs carry every new key, and the
     whole run finishes with zero page errors.

The API calls are STUBBED here: nothing in the repository, the sessions or
the production state dir is read or written.

Run:  python3 tests/frontend_touch4.py     (exit 0 = all green)
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

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://ft4.test"

TOKEN = "UI-FIXTURE-TOKEN-2f7c9a41d5e0"

STATIC = {
    "/": ("web/static/index.html", "text/html"),
    "/static/app.js": ("web/static/app.js", "text/javascript"),
    "/static/style.css": ("web/static/style.css", "text/css"),
    "/static/i18n/en.json": ("overlay/locales/en.json", "application/json"),
    "/static/i18n/zh.json": ("overlay/locales/zh.json", "application/json"),
}

# /api/status: the write destination the amber banner must name.
STATUS_STUDIO = {"profile": "studio", "version": "0.0.0-test",
                 "panels": [], "advisory": {"level": "info", "reasons": []},
                 "system": {}, "memory": {}, "config": {},
                 "time_utc": "2026-11-28 00:00:00"}
STATUS_DEFAULT = dict(STATUS_STUDIO, profile="")

SETTINGS = {"refresh_sec": 30, "sound": False, "voice_volume": 80,
            "alert_preview": "off", "language": "en",
            "mycore": {"models": ["local-alpha", "local-beta"]}}

# A transcript whose FIRST run is a folded pair, so the fold button is the
# first content child of #chatLog — the case the older-page prepend ref must
# not step over.
HISTORY_PAGE1 = {
    "messages": [
        {"id": 3, "role": "assistant", "content": "hmm, let me look at this"},
        {"id": 4, "role": "assistant", "content": "the answer is four"},
        {"id": 5, "role": "user", "content": "and the second one?"},
        {"id": 6, "role": "assistant", "content": "the answer is seven"},
    ],
    "count": 4, "has_more": True,
}
HISTORY_OLDER = {
    "messages": [
        {"id": 1, "role": "assistant", "content": "an even earlier answer"},
        {"id": 2, "role": "user", "content": "the very first question"},
    ],
    "count": 2, "has_more": False,
}

# One turn, two tool bursts separated by narration — the SSE shape the real
# server sends (tool_call with ok:null, then tool_result with ok:bool).
SSE = (
    'event: delta\ndata: {"text":"Let me look at the files first."}\n\n'
    'event: tool\ndata: {"name":"read","ok":null}\n\n'
    'event: tool\ndata: {"name":"read","ok":true}\n\n'
    'event: tool\ndata: {"name":"read","ok":null}\n\n'
    'event: tool\ndata: {"name":"read","ok":true}\n\n'
    'event: tool\ndata: {"name":"bash","ok":null}\n\n'
    'event: tool\ndata: {"name":"bash","ok":false}\n\n'
    'event: delta\ndata: {"text":" Second pass."}\n\n'
    'event: tool\ndata: {"name":"write","ok":null}\n\n'
    'event: tool\ndata: {"name":"write","ok":true}\n\n'
    'event: done\ndata: {"text":"The answer is 42.","stop":"final",'
    '"iterations":3,"tool_calls":5,"ids":{},"queued":0}\n\n'
)

# WCAG relative-luminance contrast for one selector, computed IN the page.
CONTRAST_JS = r"""
(sel) => {
  const parse = str => {
    const m = String(str).match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(",").map(s => parseFloat(s.trim()));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const over = (s, d) => ({ r: s.r * s.a + d.r * (1 - s.a),
                            g: s.g * s.a + d.g * (1 - s.a),
                            b: s.b * s.a + d.b * (1 - s.a), a: 1 });
  const lin = c => { const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4); };
  const lum = c => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  const bgOf = el => {
    const layers = []; let n = el;
    while (n && n.nodeType === 1) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0) { layers.push(c); if (c.a >= 1) break; }
      n = n.parentElement;
    }
    let out = { r: 255, g: 255, b: 255, a: 1 };
    for (let i = layers.length - 1; i >= 0; i--) out = over(layers[i], out);
    return out;
  };
  const el = document.querySelector(sel);
  if (!el) return { sel: sel, missing: true };
  const cs = getComputedStyle(el);
  const fg = parse(cs.color);
  const bg = bgOf(el);
  if (!fg) return { sel: sel, missing: true };
  const la = lum(fg), lb = lum(bg);
  const ratio = (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  return { sel: sel, ratio: Math.round(ratio * 100) / 100,
           fg: cs.color,
           bg: "rgb(" + Math.round(bg.r) + "," + Math.round(bg.g) + "," +
               Math.round(bg.b) + ")",
           size: parseFloat(cs.fontSize) };
}
"""

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
    """The stubbed console API: static files plus the four faces this test
    drives. Everything else answers the honest empty object."""

    def __init__(self, paged_history: bool = False) -> None:
        self.status = dict(STATUS_STUDIO)
        self.paged_history = paged_history
        self.history_page = 0
        self.posts: list[dict] = []

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
        if method == "GET" and path == "/api/status":
            return route.fulfill(json=dict(self.status))
        if method == "GET" and path == "/api/settings":
            return route.fulfill(json=dict(SETTINGS))
        if method == "GET" and path == "/api/chat/history":
            if not self.paged_history:
                return route.fulfill(json={"messages": [], "count": 0,
                                           "has_more": False})
            query = urllib.parse.parse_qs(url.query)
            before = (query.get("before") or [""])[0]
            self.history_page += 1
            return route.fulfill(json=dict(HISTORY_OLDER if before
                                           else HISTORY_PAGE1))
        if method == "POST" and path == "/api/chat":
            try:
                doc = json.loads(route.request.post_data or "{}")
            except ValueError:
                doc = {}
            self.posts.append(doc)
            if doc.get("action") in ("clear", "rewind", "fork"):
                return route.fulfill(json={"ok": True})
            return route.fulfill(body=SSE, content_type="text/event-stream")
        if method != "GET":
            return route.fulfill(status=404, json={})
        if path.startswith("/api/"):
            return route.fulfill(json={})
        return route.abort()


def open_console(browser, api: Api, *, lang: str = "en", width: int = 1280,
                 view: str = "chat", status: dict | None = None):
    if status is not None:
        api.status = dict(status)
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


def contrast(page, sel: str) -> dict:
    return page.evaluate(CONTRAST_JS, sel)


# --------------------------------------------------------------- 1. cmdk ---
def test_palette(browser, api: Api) -> list[str]:
    page, errors = open_console(browser, api, view="overview")
    page.wait_for_selector("#cmdkBtn")

    # the trigger is reachable and announces itself
    meta = page.evaluate("""() => {
      const b = document.getElementById('cmdkBtn');
      return { haspopup: b.getAttribute('aria-haspopup'),
               expanded: b.getAttribute('aria-expanded'),
               title: b.title, tab: b.tabIndex };
    }""")
    check("P1 the trigger carries aria-haspopup / aria-expanded / a title",
          meta["haspopup"] == "dialog" and meta["expanded"] == "false"
          and len(meta["title"]) > 5 and meta["tab"] >= 0, meta)

    # --- keyboard: Ctrl+K ------------------------------------------------
    page.keyboard.press("Control+K")
    page.wait_for_selector("#cmdkLayer", state="visible", timeout=4000)
    check("P2 Ctrl+K opens the palette",
          page.is_visible("#cmdkLayer"), "")
    check("P3 focus starts in the combobox",
          page.evaluate("() => document.activeElement.id") == "cmdkInput",
          page.evaluate("() => document.activeElement.id"))

    # --- query ranking ---------------------------------------------------
    page.fill("#cmdkInput", "settings")
    page.wait_for_timeout(120)
    first = page.evaluate("""() => {
      const o = document.querySelector('#cmdkList [role="option"]');
      if (!o) return null;
      return { group: o.dataset.group,
               label: o.querySelector('.cmdk-label').textContent };
    }""")
    check("P4 a view query ranks a VIEWS entry first",
          first and first["group"] == "views"
          and "settings" in first["label"].lower(), str(first))

    page.fill("#cmdkInput", "local-beta")
    page.wait_for_timeout(120)
    first = page.evaluate("""() => {
      const o = document.querySelector('#cmdkList [role="option"]');
      return o ? { group: o.dataset.group,
                   label: o.querySelector('.cmdk-label').textContent } : null;
    }""")
    check("P5 a model query ranks a MODELS entry first",
          first and first["group"] == "models"
          and first["label"] == "local-beta", str(first))

    page.fill("#cmdkInput", "zxqjv")
    page.wait_for_timeout(120)
    check("P6 an empty result says so instead of showing a stale list",
          page.evaluate("() => (document.querySelector('.cmdk-empty') || {})"
                        ".textContent || ''").strip() == "no match",
          page.evaluate("() => (document.querySelector('.cmdk-empty') || {})"
                        ".textContent || ''"))

    # --- arrow navigation + Enter ---------------------------------------
    page.fill("#cmdkInput", "")
    page.wait_for_timeout(120)
    start = page.evaluate(
        "() => document.getElementById('cmdkInput')"
        ".getAttribute('aria-activedescendant')")
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(80)
    moved = page.evaluate(
        "() => document.getElementById('cmdkInput')"
        ".getAttribute('aria-activedescendant')")
    check("P7 ArrowDown moves aria-activedescendant",
          start and moved and start != moved, f"{start} -> {moved}")
    active_group = page.evaluate(
        """() => { const o = document.querySelector(
            '#cmdkList [role="option"].is-on');
          return o ? o.dataset.group : ''; }""")
    check("P8 the highlighted row is the one aria-activedescendant names",
          active_group in ("views", "models", "actions"), active_group)

    # walk to the "focus the chat composer" action and run it with Enter
    page.fill("#cmdkInput", "focus")
    page.wait_for_timeout(120)
    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    ran = page.evaluate("""() => ({
      closed: document.getElementById('cmdkLayer').hidden,
      view: document.querySelector('.view.is-active').dataset.view,
      focus: document.activeElement.id })""")
    check("P9 Enter runs the entry: palette closed, Chat opened, input focused",
          ran["closed"] and ran["view"] == "chat"
          and ran["focus"] == "chatInput", str(ran))

    # --- Esc returns focus to whatever opened it -------------------------
    # (here the chat composer — the shortcut remembers the focused element,
    #  it does not blindly throw focus at the topbar)
    page.keyboard.press("Control+K")
    page.wait_for_selector("#cmdkLayer", state="visible", timeout=4000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    esc = page.evaluate("""() => ({
      closed: document.getElementById('cmdkLayer').hidden,
      focus: document.activeElement.id })""")
    check("P10 Esc closes and focus returns to the element that opened it",
          esc["closed"] and esc["focus"] == "chatInput", str(esc))

    # --- Tab is trapped --------------------------------------------------
    page.keyboard.press("Control+K")
    page.wait_for_selector("#cmdkLayer", state="visible", timeout=4000)
    page.keyboard.press("Tab")
    page.wait_for_timeout(120)
    trapped = page.evaluate("""() => {
      const a = document.activeElement;
      return { id: a.id, inside: !!document.getElementById('cmdkLayer')
                                .contains(a) };
    }""")
    check("P11 Tab stays inside the layer (focus trap)",
          trapped["inside"] and trapped["id"] == "cmdkInput", str(trapped))

    # --- the click path + backdrop ---------------------------------------
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    page.click("#cmdkBtn")
    page.wait_for_selector("#cmdkLayer", state="visible", timeout=4000)
    check("P12 the topbar button opens the palette too",
          page.evaluate("() => document.getElementById('cmdkBtn')"
                        ".getAttribute('aria-expanded')") == "true", "")
    box = page.locator("#cmdkLayer").bounding_box()
    page.mouse.click(box["x"] + 6, box["y"] + box["height"] - 6)
    page.wait_for_timeout(200)
    check("P13 clicking the backdrop closes it",
          page.evaluate("() => document.getElementById('cmdkLayer').hidden"),
          "")

    check("P14 zero page errors across the palette run",
          not errors, str(errors[:3]))
    page.close()
    return errors


# -------------------------------------------------- 2+3. stream rendering ---
def test_stream_render(browser, api: Api) -> list[str]:
    page, errors = open_console(browser, api, view="chat")
    page.wait_for_selector("#chatSend")
    page.wait_for_timeout(600)

    # --- 4. amber banner (multi-profile) ---------------------------------
    banner = page.evaluate("""() => {
      const b = document.getElementById('writeBanner');
      const t = document.getElementById('wbText');
      return { visible: !!b && !b.hidden && b.getBoundingClientRect().height > 8,
               text: t.textContent, profile: (t.querySelector('.wb-profile')||{}).textContent };
    }""")
    check("B1 the write banner names the ACTIVE profile on screen",
          banner["visible"] and banner["profile"] == "studio"
          and "studio" in banner["text"], str(banner))
    for sel, need in (("#writeBanner", 4.5), ("#wbText", 4.5),
                      (".wb-tag", 4.5), (".wb-profile", 4.5)):
        c = contrast(page, sel)
        check(f"B2 contrast {sel} clears WCAG AA ({need}:1)",
              (not c.get("missing")) and c["ratio"] >= need, str(c))

    # --- the live turn ---------------------------------------------------
    page.fill("#chatInput", "what is the answer?")
    page.click("#chatSend")
    page.wait_for_function(
        "() => document.querySelectorAll('.chat-tools-row.chat-timeline')"
        ".length >= 2", timeout=8000)
    page.wait_for_timeout(500)

    tl = page.evaluate("""() => [...document.querySelectorAll(
        '.chat-tools-row.chat-timeline')].map(el => ({
        text: el.textContent.trim(),
        segs: [...el.querySelectorAll('.tl-seg')].map(s => s.textContent.trim())
      }))""")
    check("T1 consecutive tool frames collapse into ONE line per burst",
          len(tl) == 2, str(tl))
    if tl:
        check("T2 the first line keeps each tool's own name and count",
              "read" in tl[0]["text"] and "×2" in tl[0]["text"]
              and "bash" in tl[0]["text"] and "✗" in tl[0]["text"],
              tl[0]["text"])
        check("T3 different tools stay separate segments (never one blob)",
              len(tl[0]["segs"]) == 2
              and any("read" in s for s in tl[0]["segs"])
              and any("bash" in s for s in tl[0]["segs"]), str(tl[0]["segs"]))
    if len(tl) > 1:
        check("T4 a text delta starts a NEW burst line (chronology kept)",
              "write" in tl[1]["text"], tl[1]["text"])
    check("T5 the tool line is a compact line, not one row per SSE frame",
          page.locator(".chat-row .chat-tools-row").count() == 0, "")

    # --- 3a. live reasoning fold ----------------------------------------
    fold = page.evaluate("""() => [...document.querySelectorAll(
        '.chat-fold')].map(el => ({
        open: el.getAttribute('aria-expanded'),
        tag: (el.querySelector('.cr-tag')||{}).textContent,
        prev: (el.querySelector('.cr-prev')||{}).textContent,
        full: (el.querySelector('.cr-prev')||{}).textContent }))""")
    live = [f for f in fold if f["tag"] and "reasoning" in f["tag"]]
    check("F1 the pre-tool narration folds to a line (default: collapsed)",
          len(live) == 2 and all(f["open"] == "false" for f in live),
          str(fold))
    if live:
        check("F2 the collapsed line shows ONE quiet first line, not a box",
              live[0]["prev"].startswith("Let me look at the files first.")
              and len(live[0]["prev"]) <= 120, live[0]["prev"])
        check("F3 the fold is keyboard-focusable with an aria-expanded state",
              page.evaluate("""() => {
                const b = document.querySelector('.chat-fold');
                return b.tagName === 'BUTTON' && b.tabIndex >= 0
                  && b.getAttribute('aria-expanded') === 'false'; }"""), "")
        page.locator(".chat-fold").first.click()
        page.wait_for_timeout(150)
        opened = page.evaluate("""() => {
          const b = document.querySelector('.chat-fold');
          const p = b.querySelector('.cr-prev');
          return { open: b.getAttribute('aria-expanded'),
                   full: p.classList.contains('is-full'),
                   text: p.textContent }; }""")
        check("F4 clicking expands: aria flips and the whole text shows",
              opened["open"] == "true" and opened["full"]
              and "Let me look at the files first." in opened["text"],
              str(opened))
        page.locator(".chat-fold").first.click()
        page.wait_for_timeout(150)
        check("F5 clicking again folds it back to one line",
              page.evaluate("""() => document.querySelector('.chat-fold')
                .getAttribute('aria-expanded')""") == "false", "")

    # --- the answer itself survived the folds ----------------------------
    ans = page.evaluate("""() => {
      const rows = [...document.querySelectorAll('.chat-row.assistant')];
      const last = rows[rows.length - 1];
      return last ? last.querySelector('.chat-bubble').textContent : ''; }""")
    check("F6 the authoritative answer still lands in the bubble",
          ans == "The answer is 42.", ans)
    check("F7 the fold/timeline lines are siblings of the rows, never inside "
          "them — the ids identity method is untouched",
          page.evaluate("""() => document.querySelectorAll(
            '.chat-row .chat-fold, .chat-row .chat-tools-row').length === 0
            && [...document.querySelectorAll('.chat-fold')].every(
              f => f.parentElement && f.parentElement.id === 'chatLog')"""), "")

    for sel, need in ((".chat-fold", 4.5), (".cr-tag", 4.5),
                      (".cr-prev", 4.5), (".tl-seg", 4.5)):
        c = contrast(page, sel)
        check(f"B6 contrast {sel} clears WCAG AA ({need}:1)",
              (not c.get("missing")) and c["ratio"] >= need, str(c))

    check("F8 zero page errors across the stream run",
          not errors, str(errors[:3]))
    page.close()
    return errors


# ------------------------------------------------- 3b. history fold + page ---
def test_history_fold(browser, api: Api) -> list[str]:
    page, errors = open_console(browser, api, view="chat")
    page.wait_for_selector("#chatLog .chat-row", state="attached",
                           timeout=6000)

    state = page.evaluate("""() => {
      const log = document.getElementById('chatLog');
      const kids = [...log.children];
      const first = kids.find(k => k.id !== 'chatMore');
      const folds = [...log.querySelectorAll('.chat-fold')];
      return {
        firstKind: first ? (first.classList.contains('chat-fold')
          ? 'fold' : 'row') : 'none',
        folds: folds.length,
        open: folds.map(f => f.getAttribute('aria-expanded')),
        tag: folds.map(f => (f.querySelector('.cr-tag')||{}).textContent),
        prev: folds.map(f => (f.querySelector('.cr-prev')||{}).textContent),
        hiddenRows: log.querySelectorAll('.chat-row.is-folded').length,
        visibleAnswer: [...log.querySelectorAll('.chat-row.assistant')]
          .filter(r => !r.classList.contains('is-folded'))
          .map(r => r.querySelector('.chat-bubble').textContent),
      }; }""")
    check("H1 a history run folds its interim rows into ONE line",
          state["folds"] == 1 and state["hiddenRows"] == 1
          and state["open"] == ["false"], str(state))
    check("H2 the fold line is the first content child (prepend must not "
          "land between it and its rows)",
          state["firstKind"] == "fold", str(state))
    check("H3 the collapsed line previews the interim text",
          state["prev"] and state["prev"][0].startswith("hmm, let me look"),
          str(state["prev"]))
    check("H4 only the ANSWER rows stay visible",
          state["visibleAnswer"] == ["the answer is four",
                                     "the answer is seven"],
          str(state["visibleAnswer"]))

    # expand -> the interim row comes back, in place
    page.locator(".chat-fold").first.click()
    page.wait_for_timeout(150)
    expanded = page.evaluate("""() => {
      const log = document.getElementById('chatLog');
      const order = [...log.children]
        .filter(k => k.classList.contains('chat-row')
                  || k.classList.contains('chat-fold'))
        .map(k => k.classList.contains('chat-fold') ? 'FOLD'
          : k.querySelector('.chat-bubble').textContent.slice(0, 16));
      return { order,
        hidden: log.querySelectorAll('.chat-row.is-folded').length }; }""")
    check("H5 expanding unhides the row without reordering the transcript",
          expanded["hidden"] == 0
          and expanded["order"][:3] == ["FOLD", "hmm, let me look",
                                        "the answer is fo"],
          str(expanded))
    page.locator(".chat-fold").first.click()
    page.wait_for_timeout(120)

    # --- the older page lands ABOVE the fold -----------------------------
    page.click("#chatMore")
    page.wait_for_function("""() => {
      const log = document.getElementById('chatLog');
      return [...log.querySelectorAll('.chat-bubble')]
        .some(b => b.textContent === 'the very first question'); }""",
        timeout=6000)
    older = page.evaluate("""() => {
      const log = document.getElementById('chatLog');
      const row = [...log.querySelectorAll('.chat-bubble')]
        .find(b => b.textContent === 'the very first question')
        .closest('.chat-row');
      const fold = log.querySelector('.chat-fold');
      return {
        before: !!(row.compareDocumentPosition(fold)
                   & Node.DOCUMENT_POSITION_FOLLOWING),
        folds: log.querySelectorAll('.chat-fold').length,
        hidden: log.querySelectorAll('.chat-row.is-folded').length,
      }; }""")
    check("H6 the older page prepends ABOVE the fold (ref = first content "
          "child)", older["before"], str(older))
    check("H7 re-grouping after the prepend keeps exactly one folded pair",
          older["folds"] == 1 and older["hidden"] == 1, str(older))

    check("H8 zero page errors across the history run",
          not errors, str(errors[:3]))
    page.close()
    return errors


# ------------------------------------------- 4b. default profile + bilingual ---
def test_banner_default_and_packs(browser, api: Api) -> list[str]:
    page, errors = open_console(browser, api, view="overview",
                                status=STATUS_DEFAULT)
    page.wait_for_timeout(500)
    text = page.evaluate(
        "() => document.getElementById('wbText').textContent")
    check("B3 the default profile is named plainly too",
          "default" in text, text)
    c = contrast(page, "#wbText")
    check("B4 the default-profile line also clears WCAG AA",
          (not c.get("missing")) and c["ratio"] >= 4.5, str(c))
    page.close()

    # bilingual: every new key exists in BOTH packs
    packs = {lang: json.loads((ROOT / "overlay" / "locales" / f"{lang}.json")
                              .read_text(encoding="utf-8"))
             for lang in ("en", "zh")}
    wanted = ["wb.tag", "wb.body", "chat.foldReason", "chat.foldTool",
              "chat.foldRows", "chat.foldToggle", "cmdk.open", "cmdk.title",
              "cmdk.ph", "cmdk.hint", "cmdk.none", "cmdk.groupViews",
              "cmdk.groupModels", "cmdk.groupActions", "cmdk.focus",
              "cmdk.lang", "cmdk.upd"]
    missing_en = [k for k in wanted if k not in packs["en"]]
    missing_zh = [k for k in wanted if k not in packs["zh"]]
    check("B5 every new key exists in the en pack", not missing_en,
          str(missing_en))
    check("B6 every new key exists in the zh pack (parity)", not missing_zh,
          str(missing_zh))
    cjk = [k for k in wanted if not any("\u4e00" <= ch <= "\u9fff"
                                        for ch in packs["zh"][k])]
    check("B7 the zh values really are Chinese", not cjk, str(cjk))
    check("B8 the en pack carries no Chinese",
          not [k for k in wanted
               if any("\u4e00" <= ch <= "\u9fff" for ch in packs["en"][k])], "")

    # a zh switch repaints the banner and the palette chrome
    page2, _ = open_console(browser, api, view="overview", lang="zh",
                            status=STATUS_STUDIO)
    page2.wait_for_timeout(700)
    zh = page2.evaluate("""() => ({
      tag: document.querySelector('.wb-tag').textContent,
      body: document.getElementById('wbText').textContent,
      btn: document.getElementById('cmdkBtn').title })""")
    check("B9 a zh session shows the banner in Chinese with the profile kept",
          any("\u4e00" <= c <= "\u9fff" for c in zh["tag"])
          and "studio" in zh["body"], str(zh))
    page2.keyboard.press("Control+K")
    page2.wait_for_selector("#cmdkLayer", state="visible", timeout=4000)
    zh2 = page2.evaluate("""() => ({
      title: document.getElementById('cmdkTitle').textContent,
      ph: document.getElementById('cmdkInput').placeholder,
      foot: document.getElementById('cmdkFoot').textContent,
      groups: [...document.querySelectorAll('#cmdkList [role=group]')]
        .map(g => g.getAttribute('aria-label')) })""")
    check("B10 the palette chrome follows the language switch",
          any("\u4e00" <= c <= "\u9fff" for c in zh2["title"])
          and any("\u4e00" <= c <= "\u9fff" for c in zh2["foot"])
          and len(zh2["groups"]) >= 1, str(zh2))
    page2.close()
    return errors


# --------------------------------------- 5. keyboard reach + spring sheet ---
def test_a11y_and_sheet(browser, api: Api) -> list[str]:
    page, errors = open_console(browser, api, view="overview")
    page.wait_for_selector("#cmdkBtn")

    # Tab order: the trigger sits between the clock and the update check
    page.focus("#updateCheckBtn")
    page.keyboard.press("Shift+Tab")
    page.wait_for_timeout(100)
    where = page.evaluate("() => document.activeElement.id")
    check("K1 the palette trigger is Tab-reachable from the topbar",
          where == "cmdkBtn", where)
    page.keyboard.press("Enter")
    page.wait_for_selector("#cmdkLayer", state="visible", timeout=4000)
    check("K2 Enter on the focused trigger opens it",
          page.is_visible("#cmdkLayer"), "")
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    check("K3 Escape from the trigger returns focus to the trigger",
          page.evaluate("() => document.activeElement.id") == "cmdkBtn",
          page.evaluate("() => document.activeElement.id"))
    page.close()

    # --- spring bottom sheet --------------------------------------------
    sheet, sheet_errors = open_console(browser, api, view="overview",
                                       width=390)
    sheet.wait_for_selector("#memForgetDialog", state="attached")
    sheet.evaluate("() => document.getElementById('memForgetDialog').showModal()")
    sheet.wait_for_timeout(700)
    geom = sheet.evaluate("""() => {
      const d = document.getElementById('memForgetDialog');
      const cs = getComputedStyle(d);
      const g = document.getElementById('memGrip');
      const gs = getComputedStyle(g);
      const gr = g.getBoundingClientRect();
      return { radius: cs.borderTopLeftRadius, position: cs.position,
               bottom: cs.bottom,
               width: Math.round(d.getBoundingClientRect().width),
               grip: { w: Math.round(gr.width), h: Math.round(gr.height),
                       display: gs.display !== 'none' && gr.height > 0 },
               overflow: d.scrollWidth <= d.clientWidth + 1 }; }""")
    check("S1 below 760px the confirm dialog is a bottom sheet",
          geom["radius"] == "16px" and geom["position"] == "fixed"
          and geom["bottom"] == "0px"
          and 360 <= geom["width"] <= 390, str(geom))
    check("S2 the drag grip is present and sized for a finger",
          geom["grip"]["display"] and geom["grip"]["w"] >= 30
          and geom["grip"]["h"] <= 12, str(geom["grip"]))
    check("S3 the sheet never overflows horizontally",
          geom["overflow"], str(geom))

    grip = sheet.locator("#memGrip").bounding_box()
    gx = grip["x"] + grip["width"] / 2
    gy = grip["y"] + grip["height"] / 2

    # a short drag only nudges it (still open, springs back)
    sheet.mouse.move(gx, gy)
    sheet.mouse.down()
    sheet.mouse.move(gx, gy + 30, steps=6)
    sheet.mouse.up()
    sheet.wait_for_timeout(500)
    nudged = sheet.evaluate("""() => {
      const d = document.getElementById('memForgetDialog');
      return { open: d.open, transform: d.style.transform }; }""")
    check("S4 a short drag springs back without closing",
          nudged["open"] and nudged["transform"] == "", str(nudged))

    # a long drag dismisses it
    sheet.mouse.move(gx, gy)
    sheet.mouse.down()
    sheet.mouse.move(gx, gy + 150, steps=8)
    sheet.mouse.up()
    sheet.wait_for_timeout(700)
    check("S5 dragging past the threshold closes the sheet",
          sheet.evaluate("() => !document.getElementById('memForgetDialog').open"),
          "")

    # the desktop layout still centers the dialog (no sheet above 760px)
    wide, wide_errors = open_console(browser, api, view="overview", width=1280)
    wide.wait_for_selector("#memForgetDialog", state="attached")
    wide.evaluate("() => document.getElementById('memForgetDialog').showModal()")
    wide.wait_for_timeout(300)
    desk = wide.evaluate("""() => {
      const cs = getComputedStyle(document.getElementById('memForgetDialog'));
      return { radius: cs.borderTopLeftRadius, position: cs.position }; }""")
    check("S6 above 760px the dialog keeps its centred card form",
          desk["position"] == "fixed" and desk["radius"] != "16px", str(desk))
    wide.close()

    check("K4 zero page errors across the a11y/sheet run",
          not errors and not sheet_errors,
          str((errors + sheet_errors)[:3]))
    sheet.close()
    return errors + sheet_errors


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=chromium_path(),
            args=["--disable-background-networking"])
        errors: list[str] = []
        try:
            errors += test_palette(browser, Api())
            errors += test_stream_render(browser, Api())
            errors += test_history_fold(browser, Api(paged_history=True))
            errors += test_banner_default_and_packs(browser, Api())
            errors += test_a11y_and_sheet(browser, Api())
        finally:
            browser.close()
    print(f"\n{PASSED}/{PASSED + FAILED} checks passed"
          + ("" if not errors else f"; page errors: {errors[:3]}"))
    return 1 if (FAILED or errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
