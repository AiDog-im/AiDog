"""mycore.meter — self-metered model usage log (v0.5.55 gateway car).

The product's own usage/cost data source: every chat attempt appends one
JSONL row to `<state>/model_usage/<YYYY-MM-DD>.jsonl`, aggregated by the
console (`GET /api/usage/self`) into per-day / per-endpoint / per-model
totals plus an optional CNY cost (pricing table lives in settings
`usage.model_pricing`; no price for a model -> honest null, never a made-up
number).

Design contract:
  * INERT BY DEFAULT — nothing is written until `activate()` runs (service /
    cli / console startup), so the 1600+ offline tests have zero file side
    effects; `record()` on an inactive meter is a cheap no-op.
  * ZERO ECHO — rows carry endpoint URLs, model ids, slot-free facts and
    token counts; API keys, slot names of VALUES and prompts never enter a
    row (the source data never reaches this module).
  * NEVER BREAKS A CALL — every write is wrapped: a full disk or a read-only
    state dir degrades to a dropped row, never a chat failure.
  * BOUNDED — daily files, `RETENTION_DAYS` pruned on activate; each row is
    capped (`MAX_ROW_CHARS`) so a corrupt field cannot bloat the log.

English-only source (repo i18n policy).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
from lib import profile_paths as pp  # noqa: E402  (profile path seam)

RETENTION_DAYS = 90          # daily files kept (pruned on activate)
MAX_ROW_CHARS = 2048         # hard cap per JSONL row
READ_DAYS = 7                # aggregation window for GET /api/usage/self
READ_MAX_BYTES = 512 * 1024  # per-file read cap (largest file -> truncated)
# C2 B-1: an explicit range read can never ask for more days than the log
# keeps — the clamp is the retention window itself, reported honestly.
RANGE_MAX_DAYS = RETENTION_DAYS

_lock = threading.Lock()
_dir: Path | None = None     # None = inactive (the default)


def activate(directory: Path | str | None = None) -> Path:
    """Enable logging under `<state>/model_usage/` (or `directory`, a test
    seam) and prune files older than RETENTION_DAYS. Idempotent; returns the
    active directory. Call at process startup (service / cli / console)."""
    global _dir
    d = Path(directory) if directory is not None else (
        pp.state_dir(REPO) / "model_usage")
    try:
        d.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - RETENTION_DAYS * 86400
        for old in d.glob("*.jsonl"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
    except OSError:
        pass                      # activation is best-effort, never fatal
    with _lock:
        _dir = d
    return d


def deactivate() -> None:
    """Turn logging off (test seam / shutdown)."""
    global _dir
    with _lock:
        _dir = None


def active() -> bool:
    with _lock:
        return _dir is not None


def active_dir() -> Path | None:
    with _lock:
        return _dir


def record(endpoint: str, model: str, *, ok: bool, latency_ms: int | None = None,
           in_tok: int | None = None, out_tok: int | None = None,
           cached: int | None = None, error_class: str = "") -> None:
    """Append ONE usage row. All fields are facts the caller already holds;
    nothing is derived or estimated here. Inactive meter, a bad field or an
    unwritable file -> the row is dropped silently (never raises).

    `error_class` is a status/exception class name (e.g. "HTTP 429",
    "URLError") — never a message body, never a URL query, never a key.
    Token fields are None when the provider reported none (honest absence,
    not zero)."""
    with _lock:
        d = _dir
    if d is None:
        return
    now = time.time()
    row: dict = {
        "ts": round(now, 3),
        "day": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"),
        "ep": str(endpoint)[:512],
        "model": str(model)[:200],
        "ok": bool(ok),
    }
    if latency_ms is not None:
        row["ms"] = max(0, int(latency_ms))
    if in_tok is not None:
        row["in"] = max(0, int(in_tok))
    if out_tok is not None:
        row["out"] = max(0, int(out_tok))
    if cached is not None:
        row["cached"] = max(0, int(cached))
    if not ok and error_class:
        row["err"] = str(error_class)[:80]
    try:
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    if len(line) > MAX_ROW_CHARS:
        return
    path = d / f"{row['day']}.jsonl"
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _read_day(path: Path) -> tuple[list[dict], bool]:
    """Rows of one daily file + a truncated flag. Oversized files are read
    from the TAIL (recent rows matter more); a torn first line after the cut
    is dropped, not repaired."""
    try:
        size = path.stat().st_size
        if size > READ_MAX_BYTES:
            with open(path, "rb") as fh:
                fh.seek(size - READ_MAX_BYTES)
                raw = fh.read()
            cut = raw.find(b"\n")
            raw = raw[cut + 1:] if cut >= 0 else b""
            truncated = True
        else:
            raw = path.read_bytes()
            truncated = False
    except OSError:
        return ([], False)
    rows: list[dict] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue                # torn/corrupt row: skipped, never raised
        if isinstance(doc, dict) and "ts" in doc:
            rows.append(doc)
    return (rows, truncated)


def read(days: int = READ_DAYS) -> dict:
    """Aggregate the last `days` daily files (UTC day names, today included).

    Returns {days:[...], rows:int, truncated:bool, requests, ok, fail,
    in_tokens, out_tokens, cached, cached_ratio, by_endpoint:[...], by_model:[...],
    by_day:{day: {...}}} — token totals count ONLY rows that reported them
    (`no_usage` rows are counted separately, never guessed as 0).
    `cached_ratio` (car 5-D7 item 3) is the provider-reported prompt-cache hit
    rate = cached / in_tokens over the window, rounded to 4 decimals, or None
    when nothing reported an input count — a measured fact, never a target and
    never a promise; we still send no cache directive of our own.
    An arbitrary UTC range (C2 B-1) is `read_range()` — this relative-days
    face keeps its historical behaviour exactly."""
    with _lock:
        d = _dir
    if d is None:
        return {"available": False, "reason": "inactive"}
    today = datetime.now(timezone.utc)
    wanted = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
              for i in range(days)]
    return _aggregate(d, wanted)


def read_range(start: str, end: str) -> dict:
    """Aggregate ONE UTC-day range, `start`..`end` inclusive (YYYY-MM-DD).

    Same shape as read(), so every consumer (panel, cost, export) works on
    either face. Contract:
      * inactive meter -> {available:false, reason:"inactive"}
      * a date strptime cannot parse -> {available:false, reason:"bad_date"}
      * start after end -> {available:false, reason:"empty_range"}
      * a span wider than RANGE_MAX_DAYS clamps `start` to the retention
        window and sets `clamped:true` — the answer is never silently wider
        than the log can back.
    Both failures are answered honestly instead of guessing a default; the
    HTTP layer maps them to 400."""
    with _lock:
        d = _dir
    if d is None:
        return {"available": False, "reason": "inactive"}
    try:
        s = datetime.strptime(str(start), "%Y-%m-%d").date()
        e = datetime.strptime(str(end), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return {"available": False, "reason": "bad_date"}
    if s > e:
        return {"available": False, "reason": "empty_range"}
    span = (e - s).days + 1
    clamped = span > RANGE_MAX_DAYS
    if clamped:
        s = e - timedelta(days=RANGE_MAX_DAYS - 1)
        span = RANGE_MAX_DAYS
    # descending (newest first) so _aggregate's `days` reversal yields the
    # same oldest-first list read() has always returned
    wanted = [(e - timedelta(days=i)).strftime("%Y-%m-%d")
              for i in range(span)]
    agg = _aggregate(d, wanted)
    agg["clamped"] = clamped
    return agg


def _aggregate(d: Path, wanted: list[str]) -> dict:
    """Shared body of read()/read_range(): read the `wanted` daily files
    (day names, newest first) and fold them into the panel dict."""
    all_rows: list[dict] = []
    truncated = False
    found = 0
    for day in wanted:
        path = d / f"{day}.jsonl"
        if not path.exists():
            continue
        found += 1
        rows, tr = _read_day(path)
        all_rows.extend(rows)
        truncated = truncated or tr
    agg = {
        "available": True,
        "days": list(reversed(wanted)),
        "files": found,
        "truncated": truncated,
        "requests": len(all_rows),
        "ok": 0,
        "fail": 0,
        "in_tokens": 0,
        "out_tokens": 0,
        "cached": 0,
        "cached_ratio": None,
        "no_usage": 0,
        "by_endpoint": [],
        "by_model": [],
        "by_day": {},
    }
    eps: dict[str, dict] = {}
    models: dict[str, dict] = {}
    daymap: dict[str, dict] = {}

    def blank() -> dict:
        return {"requests": 0, "ok": 0, "fail": 0, "in_tokens": 0,
                "out_tokens": 0, "no_usage": 0}

    for row in all_rows:
        ok = bool(row.get("ok"))
        agg["ok" if ok else "fail"] += 1
        ep = str(row.get("ep") or "?")[:512]
        model = str(row.get("model") or "?")[:200]
        day = str(row.get("day") or "?")[:10]
        it = row.get("in")
        ot = row.get("out")
        cached = row.get("cached")
        # global totals exactly once per row — the bucket loop below only
        # touches its own dict (a row feeds three buckets: ep/model/day)
        if isinstance(it, int):
            agg["in_tokens"] += it
        if isinstance(ot, int):
            agg["out_tokens"] += ot
        if not isinstance(it, int) and not isinstance(ot, int):
            agg["no_usage"] += 1
        if isinstance(cached, int):
            agg["cached"] += cached
        for bucket in (eps.setdefault(ep, blank()),
                       models.setdefault(model, blank()),
                       daymap.setdefault(day, blank())):
            bucket["requests"] += 1
            bucket["ok" if ok else "fail"] += 1
            if isinstance(it, int):
                bucket["in_tokens"] += it
            if isinstance(ot, int):
                bucket["out_tokens"] += ot
            if not isinstance(it, int) and not isinstance(ot, int):
                bucket["no_usage"] += 1
            if isinstance(cached, int):
                bucket.setdefault("cached", 0)
                bucket["cached"] += cached
    agg["by_endpoint"] = [{"ep": k, **v} for k, v in sorted(eps.items())]
    agg["by_model"] = [{"model": k, **v} for k, v in sorted(models.items())]
    agg["by_day"] = dict(sorted(daymap.items()))
    if agg["in_tokens"] > 0:
        agg["cached_ratio"] = round(agg["cached"] / agg["in_tokens"], 4)
    return agg


def estimate_cost(usage: dict, pricing: dict) -> dict:
    """CNY cost per model from `usage.model_pricing` settings:
    {model_id|\"default\": {\"in\": ¥/1M, \"out\": ¥/1M}}.

    Exact model match first, then `default`; a model with neither reports
    cost=null AND is listed in `unpriced` — the panel shows "no price
    configured" instead of inventing 0. Returns {total_cny, per_model, unpriced}
    where total_cny is null when NOTHING could be priced."""
    per_model: dict[str, dict] = {}
    unpriced: list[str] = []
    total: float = 0.0
    priced_any = False
    table = pricing if isinstance(pricing, dict) else {}
    default = table.get("default") if isinstance(table.get("default"), dict) else None

    def price_of(model: str) -> tuple[float, float] | None:
        row = table.get(model)
        if not isinstance(row, dict):
            row = default
        if not isinstance(row, dict):
            return None
        try:
            pin = float(row.get("in"))
            pout = float(row.get("out"))
        except (TypeError, ValueError):
            return None
        if pin < 0 or pout < 0:
            return None
        return (pin, pout)

    for entry in (usage.get("by_model") or []):
        model = str(entry.get("model") or "?")
        got = price_of(model)
        if got is None:
            unpriced.append(model)
            per_model[model] = {"cost_cny": None}
            continue
        pin, pout = got
        cost = (entry.get("in_tokens", 0) / 1e6 * pin
                + entry.get("out_tokens", 0) / 1e6 * pout)
        per_model[model] = {"cost_cny": round(cost, 6),
                            "in_per_1m": pin, "out_per_1m": pout}
        total += cost
        priced_any = True
    return {"total_cny": round(total, 6) if priced_any else None,
            "per_model": per_model, "unpriced": unpriced}
