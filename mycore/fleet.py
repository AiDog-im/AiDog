"""mycore.fleet — the visible fleet: a per-clone ledger and the split receipt.

Why this module exists (need-B, design `docs/design/swarm_clones_design.md`
§4.2). The shipped `delegate` path fans out sub-agents but writes NOTHING
down, so the owner can never learn how a turn was split, what each clone did,
what it cost, or whether it failed; and every clone's output is a
self-report, so a parent turn may simply not mention the split at all.

Four contracts, each deliberately small:

  * LEDGER — one bounded JSONL row per clone under `<state>/fleet/`
    (`<YYYY-MM-DD>.jsonl`, the meter's day-file pattern). Activation-gated
    (inert by default), retention-pruned, row-capped, and it NEVER raises:
    a ledger problem degrades to "no record", never to a failed turn.
  * RECEIPT — a system-generated one-line summary of the turn's fleets,
    appended to the owner's reply by the CALLER. The model cannot omit it
    (the delegate tool result is capped and model-written; this is not).
  * LINT — a clone's task text is credential-linted (evolution.lint_text)
    and path-redacted before it is stored; only a digest of the full text is
    kept, and a secret-class hit stores NO excerpt at all.
  * NO TOKEN, NO TRANSPORT — this module never calls a model and never
    touches the network; it only records what the turn already spent. The
    owner-facing string is produced from the locale packs (the caller passes
    a translator), so the receipt speaks the owner's language.

Stdlib only; English comments (repo i18n policy).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
from lib import profile_paths as pp  # noqa: E402  (car F: per-profile seam)

RETENTION_DAYS = 30          # day files kept (pruned on activate)
MAX_ROW_CHARS = 2048         # hard cap per JSONL row
MAX_ROWS_PER_DAY = 2000      # hard cap on clone rows per day
EXCERPT_CAP = 200            # hard cap on the stored task excerpt
READ_MAX_BYTES = 256 * 1024  # per-file read cap (largest file -> truncated)
READ_MAX_ROWS = 400          # hard cap on the rows one read may return
MAX_FLEETS = 40              # hard cap on the fleets one board read returns
PUBLIC_TOOL_NAMES = frozenset(("read_file", "grep", "glob", "http_fetch",
                               "skill_route"))

# Lint classes that must never reach the disk (evolution.lint_text ids).
SECRET_LINTS = ("secret-assignment", "secret-shape", "private-key",
                "bearer", "bot-handle")
# Keep a task excerpt only when every local lint is clean; this makes new lint
# classes fail closed by default. Path hits carry an owner-specific path too.

_PATH_RE = re.compile(r"(?:/mnt/|/home/|/root/|/Users/)[^\s'\"`,;)]*")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s'\"`,;)]*")

_lock = threading.Lock()
_dir: Path | None = None     # None = inactive (the default)
_counts: dict[str, int] = {}  # day -> rows written (the per-day cap)
_local = threading.local()  # the CURRENT turn's pending fleets


# --------------------------------------------------------------- activation --
def activate(directory: Path | str | None = None) -> Path:
    """Enable the ledger under `<state>/fleet/` (or `directory`, a test
    seam) and prune files older than RETENTION_DAYS. Idempotent; returns the
    active directory. Call at process startup (service / cli / console)."""
    global _dir
    d = Path(directory) if directory is not None else (
        pp.state_dir(REPO) / "fleet")
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
        return d
    with _lock:
        _dir = d
        _counts.clear()
        today = _today()
        current = d / f"{today}.jsonl"
        try:
            with current.open("rb") as fh:
                _counts[today] = sum(1 for _ in fh)
        except OSError:
            _counts[today] = 0
    return d


def deactivate() -> None:
    """Stop writing (the receipt keeps working — it is in-memory)."""
    global _dir
    with _lock:
        _dir = None
        _counts.clear()


def active() -> bool:
    """True when the ledger is writing (the receipt does not need this)."""
    with _lock:
        return _dir is not None


def active_dir() -> Path | None:
    """The active ledger directory (None = inactive)."""
    with _lock:
        return _dir


# ------------------------------------------------------------------ writing --
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def record_clone(row: dict) -> bool:
    """Append ONE clone row. Never raises; an inactive ledger, a full day
    or an unwritable file drops the row (honest absence, never a failure).
    Returns True when the row was written."""
    with _lock:
        d = _dir
        if d is None:
            return False
        day = str(row.get("day") or _today())
        n = _counts.get(day, 0)
        if n >= MAX_ROWS_PER_DAY:
            return False
        try:
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":"),
                              default=str)
        except (TypeError, ValueError):
            return False
        if len(line) > MAX_ROW_CHARS:
            return False
        try:
            with (d / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            return False
        _counts[day] = n + 1
        return True


def begin_turn() -> None:
    """Clear stale thread-local receipts at a top-level turn entry. Do not
    call from nested clone turns (a one-clone dispatch runs in the parent's
    thread)."""
    _local.fleets = []


def note_fleet(fleet: dict, parent_usage: dict | None = None) -> None:
    """Register one dispatched fleet for the CURRENT turn. Called by the
    delegate path in the parent thread (the clone rows are collected before
    the fold returns), so the pending list is thread-local by construction:
    a clone thread never touches it."""
    pending = getattr(_local, "fleets", None)
    if pending is None:
        pending = []
        _local.fleets = pending
    pending.append({**fleet, "parent_usage": dict(parent_usage or {
        "usage_complete": False})})


def take_fleets(origin: str) -> list[dict]:
    """Pop the current turn's fleets, stamp the origin and persist their
    clone rows. Returns the fleet list (empty when nothing was dispatched)."""
    pending = getattr(_local, "fleets", None)
    if not pending:
        return []
    _local.fleets = []
    for fleet_rec in pending:
        fleet_rec["origin"] = str(origin or "")[:32]
        for row in fleet_rec.get("clones") or []:
            row["fleet"] = fleet_rec.get("id", "")
            row["origin"] = fleet_rec["origin"]
            record_clone(row)
    return pending


# ------------------------------------------------------------------ reading --
def _read_tail(path: Path, cap: int = READ_MAX_BYTES) -> list[dict]:
    """Read the tail of one day file; torn lines are skipped, never fatal."""
    rows: list[dict] = []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > cap:
                fh.seek(size - cap)
                fh.readline()          # drop the partial first line
            for raw in fh:
                try:
                    doc = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(doc, dict):
                    rows.append(doc)
    except OSError:
        return []
    return rows


def read_rows(limit: int = READ_MAX_ROWS) -> list[dict]:
    """Recent clone rows, newest first (bounded: days x bytes x rows)."""
    try:
        limit = max(1, min(int(limit), READ_MAX_ROWS))
    except (TypeError, ValueError):
        limit = READ_MAX_ROWS
    with _lock:
        d = _dir
    if d is None:
        return []
    days: list[Path] = []
    try:
        days = sorted(d.glob("*.jsonl"), reverse=True)
    except OSError:
        return []
    rows: list[dict] = []
    for path in days:
        for row in reversed(_read_tail(path)):
            rows.append(row)
            if len(rows) >= limit:
                return rows
    return rows


# --------------------------------------------------------------- the digest --
def digest(text: str) -> str:
    """A short, stable digest of a task text (never the text itself)."""
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()[:12]


def redact_paths(text: str) -> str:
    """Mask machine paths in a task excerpt (the owner's tasks legitimately
    mention paths; a path is not the secret, but it is not public either)."""
    out = _PATH_RE.sub("<path>", str(text))
    return _WIN_PATH_RE.sub("<path>", out)


def task_excerpt(task: str) -> tuple[str, str]:
    """(digest, excerpt, lint) for a clone task. A secret-class lint stores
    NO excerpt (the digest still proves which task ran); a path-class lint
    keeps a path-redacted excerpt."""
    from .evolution import lint_text   # local import: keeps the graph acyclic
    try:
        hits = list(lint_text(str(task)))
    except Exception:  # noqa: BLE001 — lint errors fail closed
        return digest(task), "", "lint-unavailable"
    note = ",".join(hits)
    if hits:
        return digest(task), "", note
    text = redact_paths(task).strip()
    if len(text) > EXCERPT_CAP:
        text = text[:EXCERPT_CAP]
    return digest(task), text, note


# ---------------------------------------------------------- the receipt ------
def fleet_totals(fleets: list[dict], parent_usage: dict | None = None) -> dict:
    """Aggregate one turn's fleet and parent model usage. Any missing
    provider usage makes that subtotal unknown (never a partial exact total)."""
    clones = 0
    ok = 0
    failed = 0
    clone_tokens = 0
    complete = True
    for fleet in fleets:
        for row in fleet.get("clones") or []:
            clones += 1
            if str(row.get("stop")) == "final":
                ok += 1
            else:
                failed += 1
            it = row.get("in_tok")
            ot = row.get("out_tok")
            if row.get("usage_complete") is True and type(it) is int \
                    and type(ot) is int:
                clone_tokens += it + ot
            else:
                complete = False
    parent_complete = bool(parent_usage and parent_usage.get("usage_complete") is True
                           and type(parent_usage.get("in_tok")) is int
                           and type(parent_usage.get("out_tok")) is int)
    parent_tokens = (parent_usage["in_tok"] + parent_usage["out_tok"]
                     if parent_complete else None)
    clones_value = clone_tokens if complete and clones else None
    combined = (parent_tokens + clones_value
                if parent_tokens is not None and clones_value is not None else None)
    return {"fleets": len(fleets), "clones": clones, "ok": ok,
            "failed": failed, "parent_tokens": parent_tokens,
            "clone_tokens": clones_value, "tokens": combined}


def _token_text(total: int | None) -> str:
    if total is None:
        return "?"
    if total >= 1000:
        return f"{total / 1000:.1f}k"
    return str(total)


def receipt_text(fleets: list[dict], t=None,
                 parent_usage: dict | None = None) -> str:
    """The owner-visible receipt line for one turn's fleets. Localized
    through the locale packs (`t` is the caller's translator); without a
    translator the English default is used."""
    if not fleets:
        return ""
    agg = fleet_totals(fleets, parent_usage or fleets[0].get("parent_usage"))
    one = agg["fleets"] == 1
    ident = str(fleets[0].get("id") or "") if one else ""
    kw = {"fleets": agg["fleets"], "clones": agg["clones"], "ok": agg["ok"],
          "failed": agg["failed"], "tokens": _token_text(agg["tokens"]),
          "parent_tokens": _token_text(agg["parent_tokens"]),
          "clone_tokens": _token_text(agg["clone_tokens"]), "id": ident}
    key = "fleet.receipt.one" if one else "fleet.receipt.many"
    body = ""
    if t is not None:
        try:
            body = t(key, **kw)
        except Exception:  # noqa: BLE001 — a locale gap must not break a reply
            body = ""
    if not body:
        body = _default_receipt(key, kw)
    claims = ""
    if agg["clones"]:
        try:
            claims = t("fleet.receipt.claims") if t is not None else ""
        except Exception:  # noqa: BLE001
            claims = ""
        if not claims:
            claims = " · clone claims are self-reported (unverified)"
    return body + claims


def _default_receipt(key: str, kw: dict) -> str:
    """The English fallback receipt (used when no translator is supplied)."""
    if key == "fleet.receipt.one":
        head = (f"fleet {kw['id']} · {kw['clones']} clone(s) · "
                f"{kw['ok']} ok / {kw['failed']} failed")
    else:
        head = (f"{kw['fleets']} fleets · {kw['clones']} clone(s) · "
                f"{kw['ok']} ok / {kw['failed']} failed")
    return (f"🧬 {head} · parent {kw['parent_tokens']} tokens · "
            f"clones {kw['clone_tokens']} tokens · combined {kw['tokens']} tokens")


def attach_receipt(text: str, origin: str, t=None, on_fleets=None,
                   parent_usage: dict | None = None) -> str:
    """Append the turn's fleet receipt to the owner's reply. Called by the
    reply path (the model cannot omit it). A turn without clones returns
    the text unchanged. `on_fleets` (optional) receives the popped fleet
    list exactly once — the caller's seam for a log line / hook event."""
    fleets = take_fleets(origin)
    if not fleets:
        return text
    if on_fleets is not None:
        try:
            on_fleets(fleets)
        except Exception:  # noqa: BLE001 — reporting never breaks a reply
            pass
    line = receipt_text(fleets, t, parent_usage)
    if not line:
        return text
    body = (text or "").strip()
    return f"{body}\n\n{line}" if body else line


# -------------------------------------------------------------- the board ----
def public_clone(row: dict) -> dict:
    """A fixed field whitelist for one clone row (the board never echoes a
    raw ledger row)."""
    raw_tools = row.get("tools")
    safe_tools = [name for name in raw_tools if name in PUBLIC_TOOL_NAMES] \
        if isinstance(raw_tools, list) else []
    raw_model = row.get("model")
    safe_model = raw_model if isinstance(raw_model, str) and len(raw_model) <= 80 \
        and re.fullmatch(r"[A-Za-z0-9._:/-]+", raw_model) else "custom"
    return {
        "clone": row.get("clone"),
        "fleet": row.get("fleet"),
        "origin": row.get("origin"),
        "stop": row.get("stop"),
        "tool_calls": row.get("tool_calls"),
        "in_tok": row.get("in_tok"),
        "out_tok": row.get("out_tok"),
        "usage_complete": row.get("usage_complete") is True,
        "model": safe_model,
        "role": (row.get("role") if isinstance(row.get("role"), str)
                 and len(str(row.get("role"))) <= 20 else ""),
        "band": (row.get("band") if isinstance(row.get("band"), str)
                 and len(str(row.get("band"))) <= 4 else ""),
        "tools": safe_tools,
        "task_digest": row.get("task_digest"),
        "lint": row.get("lint"),
        "failed": bool(row.get("stop") not in ("final", "", None)),
    }


def summary(limit: int = MAX_FLEETS) -> dict:
    """The fleet board payload: recent fleets with their clones, an
    aggregate and the honest coverage note. Fail-soft: an inactive or
    unreadable ledger answers the honest empty structure, never an error."""
    rows = read_rows(limit=READ_MAX_ROWS)
    fleets: list[dict] = []
    index: dict[str, dict] = {}
    for row in rows:
        ident = str(row.get("fleet") or "")
        entry = index.get(ident)
        if entry is None:
            entry = {"id": ident, "origin": row.get("origin"),
                     "ts": row.get("ts"), "clone_total": row.get("clone_total"),
                     "clones": []}
            index[ident] = entry
            fleets.append(entry)
        entry["clones"].append(public_clone(row))
    fleets = fleets[:max(1, min(int(limit), MAX_FLEETS))]
    for item in fleets:
        expected = max((int(c.get("clone_total") or 0) for c in rows
                        if str(c.get("fleet") or "") == item["id"]), default=0)
        item["incomplete"] = bool(expected and len(item["clones"]) < expected)
        item["expected_clones"] = expected or len(item["clones"])
    clones = sum(len(f["clones"]) for f in fleets)
    tokens = 0
    known = False
    complete = True
    for f in fleets:
        for c in f["clones"]:
            if c.get("usage_complete") is not True:
                complete = False
            for key in ("in_tok", "out_tok"):
                if type(c.get(key)) is int:
                    tokens += c[key]
                    known = True
    failed = sum(1 for f in fleets for c in f["clones"] if c["failed"])
    return {
        "fleets": fleets,
        "count": len(fleets),
        "clones": clones,
        "failed": failed,
        "clone_tokens": tokens if known and complete else None,
        "incomplete": any(f.get("incomplete") for f in fleets),
        "local_only": True,
        "coverage": ("some fleet rows are outside the bounded read window; "
                     "visible clone lists may be incomplete" if any(
                         f.get("incomplete") for f in fleets) else
                     "the ledger records completed turns only — a turn that "
                     "crashed before its reply leaves no row"),
    }
