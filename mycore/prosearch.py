"""mycore.prosearch — outer-web accumulation for the owner's profile (need-A, P1).

P0 (`mycore.profiling`) is local-only: it reads the owner's own words and
stops there. P1 adds the ONE egress surface of this feature — it turns the
profile's retrieval gaps into accumulated knowledge:

    profile gap/partial keyword
        -> one keyless search (no credential, a named product UA, capped)
        -> fetch the top result pages (GET only, bounded, no cookies)
        -> file a knowledge card (four labels + provenance + an excerpt)
        -> optionally raise a SNIFF finding the OWN delivery chain speaks

Design authority: `docs/design/owner_profiling_design.md` §3.5/§3.8.

Contracts enforced here (and asserted by mycore/tests/run.py):

* THE ONLY EGRESS, AND IT IS OFF BY DEFAULT. `mycore.search.enabled` ships
  false; the owner flips one switch in the SENSE view. Nothing in this module
  runs unless that switch is on.
* KEYLESS AND CREDENTIAL-FREE. The search needs no account, no API key and
  no slot; the partition has no credential grammar at all (a value-shaped
  field is an unknown field). The proxy policy is NOT re-implemented here:
  `mycore.tools.outbound_config/opener/reason` is the single policy, reused.
* WHAT LEAVES IS EXACTLY THE QUERY. A keyword query and nothing else — no
  session text, no excerpt, no chat id, no file name. The URL of a fetched
  page is stored locally, never sent anywhere.
* HONEST STATUS. `reachable` / `blocked` (the engine answered with an
  anti-bot page) / `error` (transport) / `disabled` / `daily-cap` /
  `interval`. A blocked engine is REPORTED and the leg stops for the day —
  it never retries in a loop.
* BOUNDED. Per-run search and fetch caps, a Beijing-day cap, a bounded
  knowledge log, a bounded excerpt, a bounded page read.
* ZERO MODEL TOKENS. The extraction and the filing are rules (a regex over
  an RSS feed, a fixed meta/heading/text extractor). No model client, no
  loop, no prompt string.

English-only source (repo i18n policy); Chinese lexicons are unicode escapes
where a Chinese term is needed.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib import profile_paths as pp  # noqa: E402  (profile state seam)

REPO = Path(__file__).resolve().parent.parent

# ----------------------------------------------------------------- bounds ---
MAX_RESULTS = 10                  # one search answer's rows
MAX_FETCH_BYTES = 400_000         # bytes read from one page
MAX_EXCERPT = 400                 # stored excerpt cap (chars)
MAX_PAGE_TEXT = 600               # slice extracted from a page
MAX_CARDS = 500                   # knowledge log bound (oldest dropped)
MAX_DOMAINS = 32                  # priority-domain list cap
MAX_DOMAIN_CHARS = 120
MAX_PINNED = 64                   # owner-pinned keywords
MAX_PINNED_CHARS = 60
DEFAULT_SEARCHES_PER_RUN = 5
DEFAULT_FETCHES_PER_RUN = 10
DEFAULT_DAILY_CAP = 20
DEFAULT_INTERVAL_S = 3600
MIN_INTERVAL_S = 300
MAX_INTERVAL_S = 86400
HTTP_TIMEOUT_S = 12.0
SEARCH_ENGINE = "bing_rss"        # the only engine (closed enum)
USER_AGENT = "AidogProfile/1.0 (+https://aidog.im)"
SEARCH_ENDPOINT = "https://cn.bing.com/search"
ERR_SNIPPET = 160

STATE_DIR = "profiling"
KB_FILE = "kb.json"
SEARCH_STATE_FILE = "search_state.json"

CONFIG_KEYS = frozenset({"enabled", "engine", "searches_per_run",
                         "fetches_per_run", "daily_cap", "interval_s",
                         "priority_domains"})

DEFAULT_SETTINGS: dict = {
    "enabled": False,              # the kill switch ships OFF (D3)
    "engine": SEARCH_ENGINE,
    "searches_per_run": DEFAULT_SEARCHES_PER_RUN,
    "fetches_per_run": DEFAULT_FETCHES_PER_RUN,
    "daily_cap": DEFAULT_DAILY_CAP,
    "interval_s": DEFAULT_INTERVAL_S,
    "priority_domains": [],
}

# Honest status vocabulary (the view and the report use exactly these words).
ST_DISABLED = "disabled"
ST_REACHABLE = "reachable"
ST_BLOCKED = "blocked"
ST_ERROR = "error"
ST_CAPPED = "daily-cap"
ST_NOTHING = "nothing-to-search"

_BJ = timezone(timedelta(hours=8))       # owner timezone (cron/quiet parity)
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S | re.I)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_LINK_RE = re.compile(r"<link>(.*?)</link>", re.S | re.I)
_DESC_RE = re.compile(r"<description>(.*?)</description>", re.S | re.I)
# Script and style BODIES are dropped before the text is taken: their
# content is code, not prose, and a card must never quote JavaScript.
_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>",
                        re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_HTML_META_RE = re.compile(
    r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"'](.*?)[\"']",
    re.S | re.I)
_HTML_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_WS_RE = re.compile(r"\s+")

# Words that mean "this page is an anti-bot answer, not results".
# The Chinese markers are unicode escapes so this source file stays ASCII
# for tests/source_lang_check.py.
_CHALLENGE_MARKERS = ("captcha", "unusual traffic", "are you a robot",
                      "verify you are human", "\u8bf7\u8f93\u5165\u9a8c\u8bc1\u7801",
                      "\u8bbf\u95ee\u9a8c\u8bc1", "security check",
                      "blocked by", "rate limit")


class SearchError(RuntimeError):
    """Chat-safe configuration failure: field names and expected shapes only,
    never a value."""


# ------------------------------------------------------------ config parse ---
def _int(block: dict, key: str, default: int, lo: int, hi: int) -> int:
    val = block.get(key, default)
    if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
        raise SearchError(f"mycore.search.{key} must be an integer {lo}..{hi}")
    return val


def parse_config(block: object) -> dict:
    """Parse settings `mycore.search`. First error raises SearchError.

    There is NO credential field: an unknown field is refused, so a
    value-shaped field can never be stored here."""
    if not isinstance(block, dict):
        raise SearchError("mycore.search must be an object")
    extra = sorted(set(block) - CONFIG_KEYS)
    if extra:
        raise SearchError(f"mycore.search: unknown field '{extra[0]}'")
    enabled = block.get("enabled", False)
    if not isinstance(enabled, bool):
        raise SearchError("mycore.search.enabled must be true or false")
    engine = block.get("engine", SEARCH_ENGINE)
    if not isinstance(engine, str) or engine != SEARCH_ENGINE:
        raise SearchError(f"mycore.search.engine must be {SEARCH_ENGINE}")
    doms_raw = block.get("priority_domains", [])
    if doms_raw is None:
        doms_raw = []
    if not isinstance(doms_raw, list) or len(doms_raw) > MAX_DOMAINS:
        raise SearchError("mycore.search.priority_domains must be a list of "
                          f"at most {MAX_DOMAINS} domains")
    domains: list[str] = []
    for dom in doms_raw:
        if not isinstance(dom, str) or not dom.strip() \
                or len(dom) > MAX_DOMAIN_CHARS:
            raise SearchError("mycore.search.priority_domains: every entry "
                              "must be a non-empty domain string")
        clean = dom.strip().casefold()
        clean = re.sub(r"^https?://", "", clean).strip("/")
        if not clean or "/" in clean:
            raise SearchError("mycore.search.priority_domains: every entry "
                              "must be a bare domain (no path)")
        if clean not in domains:
            domains.append(clean)
    return {
        "enabled": enabled,
        "engine": engine,
        "searches_per_run": _int(block, "searches_per_run",
                                  DEFAULT_SEARCHES_PER_RUN, 1, 20),
        "fetches_per_run": _int(block, "fetches_per_run",
                                 DEFAULT_FETCHES_PER_RUN, 1, 40),
        "daily_cap": _int(block, "daily_cap", DEFAULT_DAILY_CAP, 1, 200),
        "interval_s": _int(block, "interval_s", DEFAULT_INTERVAL_S,
                           MIN_INTERVAL_S, MAX_INTERVAL_S),
        "priority_domains": domains,
    }


def settings_block(repo: Path | None = None) -> dict:
    """The `mycore.search` settings partition. {} on any read/parse failure."""
    try:
        doc = json.loads(pp.settings_path(repo or REPO).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    mc = doc.get("mycore")
    block = mc.get("search") if isinstance(mc, dict) else None
    return block if isinstance(block, dict) else {}


# ----------------------------------------------------------------- egress ---
def _opener():
    """The owner's proxy policy — REUSED from mycore.tools, never
    re-implemented here (two policies drift; one is the point)."""
    from . import tools  # local import: keeps this module's import surface flat
    cfg = tools.outbound_config()
    if cfg["state"] == "ready":
        return tools.outbound_opener(cfg), cfg
    return None, cfg


def http_get(url: str, *, timeout: float = HTTP_TIMEOUT_S,
             max_bytes: int = MAX_FETCH_BYTES) -> tuple[str, str]:
    """One GET -> (body, status). status is `ok` | `blocked` | `error`.

    A blocked answer (an anti-bot page) is a REAL outcome, not an error: the
    caller reports it and stops the leg for the day."""
    opener, proxy_cfg = _opener()
    if proxy_cfg.get("state") == "bad_url":
        return "", "error"              # fail-closed: never fall back direct
    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/rss+xml,application/xml,*/*"})
    try:
        if opener is not None:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read(max_bytes)
        else:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(max_bytes)
    except urllib.error.HTTPError as e:
        return "", "error"
    except (urllib.error.URLError, TimeoutError, OSError):
        return "", "error"
    text = body.decode("utf-8", "replace")
    low = text[:4000].casefold()
    if any(marker in low for marker in _CHALLENGE_MARKERS):
        return text, "blocked"
    return text, "ok"


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", html.unescape(text or "")).strip()


def parse_rss(body: str) -> list[dict]:
    """RSS items -> [{url, title, snippet}]. A regex over a bounded body —
    no XML parser (no entity-expansion risk) and no third-party feed shape
    is trusted beyond the three fields we actually use."""
    out: list[dict] = []
    for chunk in _ITEM_RE.findall(body or "")[:MAX_RESULTS]:
        title = _TITLE_RE.search(chunk)
        link = _LINK_RE.search(chunk)
        desc = _DESC_RE.search(chunk)
        url = _clean(link.group(1)) if link else ""
        if not url.startswith(("http://", "https://")):
            continue
        out.append({
            "url": url,
            "title": _clean(title.group(1))[:200] if title else "",
            "snippet": _clean(desc.group(1))[:MAX_EXCERPT] if desc else "",
        })
    return out


def search(query: str, *, timeout: float = HTTP_TIMEOUT_S) -> dict:
    """One keyless search -> {status, results, query}.

    The query is the ONLY thing that leaves the machine."""
    q = " ".join(str(query or "").split())[:120]
    if not q:
        return {"status": ST_NOTHING, "results": [], "query": ""}
    url = f"{SEARCH_ENDPOINT}?q={urllib.parse.quote(q)}&format=rss"
    body, status = http_get(url, timeout=timeout)
    if status == "blocked":
        return {"status": ST_BLOCKED, "results": [], "query": q}
    if status == "error":
        return {"status": ST_ERROR, "results": [], "query": q}
    results = parse_rss(body)
    if not results:
        low = body[:4000].casefold()
        if any(marker in low for marker in _CHALLENGE_MARKERS):
            return {"status": ST_BLOCKED, "results": [], "query": q}
        return {"status": ST_REACHABLE, "results": [], "query": q}
    return {"status": ST_REACHABLE, "results": results, "query": q}


def extract_page(body: str) -> dict:
    """One page -> {title, description, heading, text}. Rule-based: the first
    <title>, the meta description, the first <h1>, and the visible opening
    slice. Script/style bodies are dropped first — a card quotes PROSE, never
    code. Nothing is interpreted, summarized or generated."""
    title = _HTML_TITLE_RE.search(body or "")
    desc = _HTML_META_RE.search(body or "")
    h1 = _HTML_H1_RE.search(body or "")
    prose = _SCRIPT_RE.sub(" ", body or "")
    stripped = _TAG_RE.sub(" ", prose)
    text = _clean(stripped)[:MAX_PAGE_TEXT]
    return {
        "title": _clean(title.group(1))[:200] if title else "",
        "description": _clean(desc.group(1))[:MAX_EXCERPT] if desc else "",
        "heading": _clean(h1.group(1))[:200] if h1 else "",
        "text": text,
    }


def domain_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""


# ------------------------------------------------------------------- cards ---
def card_id(keyword: str, url: str) -> str:
    import hashlib
    raw = f"{keyword}\x1f{url}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def classify_card(card: dict, cfg: dict, *, keyword: dict) -> str:
    """The filing rule (shown in the view, so the tier is never a mystery):
      L1  the domain is on the owner's priority list
      L2  the page still talks about the keyword (enough overlap)
      L3  background
    The keyword's own tier is the floor: a keyword the profile already rates
    L1 never lands as background."""
    domain = str(card.get("domain") or "")
    if domain and domain in cfg.get("priority_domains", []):
        tier = "L1"
    else:
        hay = " ".join([str(card.get("title") or ""),
                        str(card.get("description") or ""),
                        str(card.get("text") or "")]).casefold()
        needle = str(keyword.get("token") or "").casefold()
        tier = "L2" if needle and needle in hay else "L3"
    own = str(keyword.get("tier") or "L3")
    order = {"L3": 0, "L2": 1, "L1": 2}
    return tier if order[tier] >= order.get(own, 0) else own


def make_card(keyword: dict, result: dict, page: dict, cfg: dict) -> dict:
    """One knowledge card: the keyword's four labels + the page's provenance
    + a bounded excerpt. No session text, no chat id, nothing but what the
    page itself said."""
    card = {
        "id": card_id(str(keyword.get("token") or ""), str(result.get("url") or "")),
        "keyword": str(keyword.get("token") or ""),
        "topic": str(keyword.get("topic") or "other"),
        "trait": str(keyword.get("trait") or "gap"),
        "coverage": "partial",                 # the card FILLS the gap
        "query": str(result.get("query") or ""),
        "url": str(result.get("url") or ""),
        "domain": domain_of(str(result.get("url") or "")),
        "title": str(page.get("title") or result.get("title") or "")[:200],
        "description": str(page.get("description") or "")[:MAX_EXCERPT],
        "excerpt": str(page.get("text") or "")[:MAX_EXCERPT],
        "fetched_at": time.time(),
    }
    card["tier"] = classify_card(card, cfg, keyword=keyword)
    return card


# --------------------------------------------------------------- knowledge ---
def kb_path() -> Path:
    return pp.state_dir(REPO) / STATE_DIR / KB_FILE


def search_state_path() -> Path:
    return pp.state_dir(REPO) / STATE_DIR / SEARCH_STATE_FILE


def load_kb(path: Path | None = None) -> list[dict]:
    """The knowledge log. Absent/corrupt -> [] (an honest empty board)."""
    try:
        doc = json.loads((path or kb_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = doc.get("cards") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return []
    return [r for r in rows[:MAX_CARDS] if isinstance(r, dict)
            and isinstance(r.get("id"), str)]


def save_kb(cards: list[dict], path: Path | None = None) -> None:
    """Atomic persist (tmp + os.replace); OSError propagates to the caller."""
    target = path or kb_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps({"cards": cards[:MAX_CARDS]},
                              ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    os.replace(tmp, target)


def load_search_state(path: Path | None = None) -> dict:
    try:
        doc = json.loads((path or search_state_path()).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    return {
        "last_run": float(doc.get("last_run") or 0.0),
        "day": str(doc.get("day") or ""),
        "day_count": int(doc.get("day_count") or 0),
        "searched": doc.get("searched") if isinstance(doc.get("searched"), dict)
        else {},
        "blocked_until_day": str(doc.get("blocked_until_day") or ""),
    }


def save_search_state(state: dict, path: Path | None = None) -> None:
    target = path or search_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    os.replace(tmp, target)


def _today(now: float) -> str:
    return datetime.fromtimestamp(now, _BJ).strftime("%Y-%m-%d")


# -------------------------------------------------------------------- tick ---
def pick_keywords(profile: dict, *, limit: int,
                  already: dict | None = None,
                  today: str = "") -> list[dict]:
    """The keywords that still need filing: coverage `gap` first, then
    `partial`, by score. A `covered` keyword is never re-searched, and a
    keyword searched on the given day is skipped (the day ledger owns that).

    `today` is passed in (never read from the clock here) so a caller with an
    injected `now` gets a deterministic answer."""
    rows = profile.get("keywords") if isinstance(profile.get("keywords"), list) \
        else []
    day = today or _today(time.time())
    out: list[dict] = []
    for order in ("gap", "partial"):
        picked = [r for r in rows
                  if isinstance(r, dict) and r.get("coverage") == order]
        picked.sort(key=lambda r: (-float(r.get("score") or 0.0),
                                   str(r.get("token") or "")))
        for row in picked:
            token = str(row.get("token") or "")
            if not token:
                continue
            if already and str((already.get(token) or {}).get("day") or "") \
                    == today:
                continue            # already searched today
            out.append(row)
            if len(out) >= limit:
                return out
    return out


def tick(now: float | None = None, *, block: dict | None = None,
         profile: dict | None = None, cards: list[dict] | None = None,
         state: dict | None = None, force: bool = False,
         searcher=None, fetcher=None) -> dict:
    """The search leg: search the profile's gaps, fetch, file, (optionally)
    raise a finding.

    Every seam is injectable so the suite runs hermetically: `searcher(query)`
    and `fetcher(url)` default to the real egress. `profile` / `cards` /
    `state` are the owned containers — passing them means "read nothing,
    write nothing"."""
    t = float(now if now is not None else time.time())
    own_cards = cards is None
    own_state = state is None
    raw = block if block is not None else settings_block()
    try:
        cfg = parse_config(raw)
    except SearchError as e:
        return {"skipped": "config", "error": str(e)[:ERR_SNIPPET], "at": t}
    if not cfg["enabled"]:
        return {"skipped": ST_DISABLED, "at": t}
    st = load_search_state() if state is None else state
    if not isinstance(st, dict):
        st = load_search_state()
    if own_state and st.get("blocked_until_day") == _today(t):
        return {"skipped": "blocked", "at": t}
    if not force and st.get("last_run") and t >= float(st["last_run"]) \
            and t - float(st["last_run"]) < cfg["interval_s"]:
        return {"skipped": "interval", "at": t}
    day = _today(t)
    if st.get("day") != day:
        st["day"] = day
        st["day_count"] = 0
    if int(st.get("day_count") or 0) >= cfg["daily_cap"]:
        return {"skipped": ST_CAPPED, "at": t}

    prof = profile if profile is not None else {}
    keywords = pick_keywords(prof, limit=cfg["searches_per_run"],
                             already=st.get("searched"), today=day)
    if not keywords:
        return {"skipped": ST_NOTHING, "at": t}

    rows = list(cards) if cards is not None else load_kb()
    known = {str(r.get("id")) for r in rows if isinstance(r, dict)}
    search_fn = searcher or search
    fetch_fn = fetcher or (lambda url: http_get(url))
    searched = st.get("searched") if isinstance(st.get("searched"), dict) else {}
    new_cards: list[dict] = []
    status = ST_REACHABLE
    searched_now = 0
    fetched_now = 0
    for kw in keywords:
        if searched_now >= cfg["searches_per_run"]:
            break
        if int(st.get("day_count") or 0) + searched_now >= cfg["daily_cap"]:
            status = ST_CAPPED
            break
        answer = search_fn(str(kw.get("token") or ""))
        searched_now += 1
        searched[str(kw.get("token") or "")] = {"day": day, "ts": t}
        st["day_count"] = int(st.get("day_count") or 0) + 1
        if answer.get("status") == ST_BLOCKED:
            status = ST_BLOCKED
            st["blocked_until_day"] = day
            break
        if answer.get("status") != ST_REACHABLE:
            status = str(answer.get("status") or ST_ERROR)
            continue
        for result in (answer.get("results") or [])[:MAX_RESULTS]:
            if fetched_now >= cfg["fetches_per_run"]:
                break
            body, fetch_status = fetch_fn(str(result.get("url") or ""))
            fetched_now += 1
            if fetch_status == "blocked":
                status = ST_BLOCKED
                st["blocked_until_day"] = day
                break
            if fetch_status != "ok":
                continue
            page = extract_page(body)
            card = make_card(kw, dict(result, query=answer.get("query")),
                             page, cfg)
            if card["id"] in known:
                continue
            known.add(card["id"])
            rows.append(card)
            new_cards.append(card)
        if status == ST_BLOCKED:
            break

    if len(rows) > MAX_CARDS:
        rows = rows[-MAX_CARDS:]
    st["last_run"] = t
    st["searched"] = searched
    report = {
        "at": t,
        "skipped": "",
        "status": status,
        "searched": searched_now,
        "fetched": fetched_now,
        "new_cards": len(new_cards),
        "cards_total": len(rows),
        "dropped": max(0, len(rows) - MAX_CARDS),
        "day_count": int(st.get("day_count") or 0),
        "daily_cap": cfg["daily_cap"],
    }
    if own_cards:
        try:
            save_kb(rows)
        except OSError as e:  # noqa: BLE001 — a full disk is not a crash
            report["error"] = f"persist failed: {type(e).__name__}"[:ERR_SNIPPET]
    if own_state:
        try:
            save_search_state(st)
        except OSError as e:  # noqa: BLE001
            report["error"] = (report.get("error") or
                               f"persist failed: {type(e).__name__}")[:ERR_SNIPPET]
    return report
