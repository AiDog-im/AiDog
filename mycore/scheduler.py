"""mycore.scheduler — the natural-language cron scheduler (Phase B2).

Design anchor (task 2, owner-approved 2026-10-20): prior-art products ship
"natural-language cron" as a headline feature; ours is a SYSTEM — a job
registry that survives restarts, one shared spec parser for both entry
points (the /cron service command and the model `cron` tool), and a due
leg in the chat-service poll loop that executes and delivers (or logs,
honestly, when no target is configured).

Entry points (dual-driven, same registry):
  * command  /cron · /cron add [message|prompt] <spec> :: <text> · /cron del <id>
             (structured management, parsed by the service — no model call)
  * model    the `cron` tool (build_registry): the model translates plain
             speech ("remind me every day at 8") into a spec string, so the
             natural-language surface costs zero parsing code here.
  * ops      python3 -m mycore.scheduler --list|--add|--del
             |--pause|--resume|--run|--catchup [--json]

Schedule spec (one grammar for all entries; Beijing time, fixed +08:00,
no DST — same convention as mycore.evolution):
    every <N>m | every <N>h     interval  (1..10080 minutes)
    daily HH:MM                 every day
    weekly <mon..sun> HH:MM     every week on that weekday
    once YYYY-MM-DD HH:MM       one-shot (a past time is refused at create)

Job kinds:
    message   the stored text is delivered as-is (a reminder)
    prompt    the stored text runs ONE model turn (tools + constitution,
              no session persistence — each fire is a fresh turn) and the
              result is delivered

Due semantics (catch-up, at most once per window, no fire bursts — the
evolution cron leg's philosophy applied per job):
    interval  now >= (last_fired or created_at) + N*60; the anchor moves to
              the actual fire time, so downtime yields ONE late fire.
    daily     now >= the latest occurrence <= now, and both last_fired and
              created_at are older than that occurrence. The single rule
              covers: created after today's time (waits for tomorrow),
              a missed window (one catch-up fire on the next opportunity),
              already fired today (silent until tomorrow).
    weekly    same rule against the latest target-weekday occurrence
              (walking back at most 7 days).
    once      now >= at and not done -> fires exactly once, then done=true.

Per-job controls (car C1, A-3 — one grammar, four entries: /cron, the
model `cron` tool, python3 -m mycore.scheduler, POST /api/cron):
    paused        bool — is_due never fires it (run-now still executes:
                  an explicit human action is not an automatic fire).
    catchup       "on" (default, the behavior above) | "off" — a window
                  that expired more than CATCHUP_GRACE_SEC ago is SKIPPED
                  instead of fired late: is_due refuses it and
                  catchup_sweep() advances the interval anchor (or marks a
                  missed one-shot done+missed) so the job keeps scheduling
                  the NEXT window instead of dying or bursting. Daily /
                  weekly need no mutation — their occurrence moves forward
                  by itself. Grace covers normal tick latency (60 s cadence
                  + slack); with catch-up off, a sub-grace interval job
                  still fires on its current window (documented).
    run_requested_at  epoch — a manual run queued by any entry point. The
                  service fire leg executes it through the SAME delivery
                  path as a scheduled fire, BEFORE due math: the window is
                  consumed (mark_fired) only when it had already arrived
                  (scheduler.window_due), so an early run never eats a
                  future one-shot (upstream: "manual run does not consume
                  one-shot") and a run of a due job replaces the tick's
                  fire instead of doubling it. kind=message runs always
                  execute (a delivery, no tokens — pause never blocks an
                  explicit human send); kind=prompt runs pass the SAME gate
                  as any new turn (pause + budget) and stay queued while it
                  holds. The TG /cron run command nudges the fire leg
                  immediately (same process); console / CLI / model-tool
                  runs land as the flag and fire at the next 60 s tick.
    missed        bool — set by catchup_sweep for a skipped one-shot
                  (displayed "(missed)" instead of "(done)").

There is no in-flight cancel: a fire is one synchronous delivery or one
model turn inside the poll loop — no long-running job process exists to
stop. `paused` holds the NEXT fire; an executing fire completes (bounded
by the turn's own timeout). Documented honestly, never pretended.

Execution order in the due leg: sweep stale catch-up-off windows, run any
queued manual request, then persist the mark FIRST for scheduled fires, then
execute — a crash mid-fire can lose one delivery but never duplicates it.

Delivery (why the notebook says "MYCORE_WD_CHAT unset -> log, no error"):
    job.origin as int (the chat that created it)   -> _send
    job.origin as str "route:target" (a channel   -> same-route channel send
      turn endpoint; an unknown route or a failed
      send falls back down this list)
    else env/slot MYCORE_WD_CHAT (int; empty env falls back
                                          to the secrets.env slot —
                                          mycore.envslots)      -> _send
    else                                             -> log only
State: <state>/cron_jobs.json — env AIDOG_MC_STATE when set, else
<repo>/data/mycore_service (next to the chat-service runtime state,
gitignored). Corrupt/garbage entries are dropped on load (fail-soft);
an unwritable file is an honest error, never a silent success.

Caps: MAX_JOBS=50 · MAX_TEXT=4000 chars · interval 1..10080 minutes.
English-only source (repo i18n policy); user-visible strings resolve
through the locale packs in the service layer.
Exit codes (CLI): 0 ok · 1 io/operation failed · 2 bad arguments.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
from lib import profile_paths as pp  # noqa: E402  (car F: profile state seam)

BEIJING = timezone(timedelta(hours=8), "+08:00")   # fixed offset, no DST
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
KINDS = ("message", "prompt")
SOURCES = ("command", "model", "ops")

STATE_FILE_NAME = "cron_jobs.json"
MAX_JOBS = 50
MAX_TEXT = 4000
MAX_NAME = 64                      # display label for a console-created job
MAX_INTERVAL_MIN = 10080                 # 7 days
# Car C1 (A-3): catch-up OFF skips a window that expired this long ago
# (seconds). 300 = 5x the 60 s fire cadence — normal operation always fires
# inside it; only real downtime / a long hold trips the skip.
CATCHUP_GRACE_SEC = 300

# Job-name grammar (console create; ONE rule for every entry point): 1..64
# visible chars, no control characters and no '/'. The '/' ban is a payload
# rule, not an aesthetic one — the console's list sweep asserts that no
# display field can carry a filesystem path.
_NAME_RE = re.compile(r"[^\x00-\x1f\x7f/]{1,64}")

_TIME_RE = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
_DATE_RE = re.compile(r"([0-9]{4})-([01][0-9])-([0-3][0-9])")
_INTERVAL_RE = re.compile(r"([0-9]+)(m|h)$", re.I)
# --origin forms: a numeric chat id, or a channel endpoint "route:target"
_ROUTE_ORIGIN_RE = re.compile(r"[a-z][a-z0-9_]*:.+")


class SpecError(ValueError):
    """Raised by parse_spec/create_job for expected refusals (honest text,
    no machine paths — the message may surface in a chat reply)."""


# ------------------------------------------------------------- spec parse ---
def _parse_time(token: str) -> str:
    if not _TIME_RE.fullmatch(token or ""):
        raise SpecError(f'bad time {token!r} — expected HH:MM (00:00-23:59)')
    return token


def parse_spec(spec: str) -> dict:
    """One spec grammar -> job schedule fields ({'type': ...}).

    Raises SpecError with an operator-facing hint on any deviation; the
    /cron command and the model tool surface the same text, so a model
    mistaking 'every 30 min' for 'every 30m' gets corrected in-line."""
    tokens = (spec or "").split()
    if not tokens:
        raise SpecError("empty schedule — forms: every 30m | daily 08:00 | "
                        "weekly mon 08:00 | once 2026-10-26 09:00")
    head = tokens[0].lower()
    if head == "every":
        if len(tokens) != 2:
            raise SpecError("usage: every <N>m | every <N>h")
        m = _INTERVAL_RE.fullmatch(tokens[1])
        if not m:
            raise SpecError(f"bad interval {tokens[1]!r} — expected 30m or 2h")
        minutes = int(m.group(1)) * (60 if m.group(2).lower() == "h" else 1)
        if not 1 <= minutes <= MAX_INTERVAL_MIN:
            raise SpecError(f"interval out of range — 1..{MAX_INTERVAL_MIN} minutes")
        return {"type": "interval", "minutes": minutes}
    if head == "daily":
        if len(tokens) != 2:
            raise SpecError("usage: daily HH:MM")
        return {"type": "daily", "time": _parse_time(tokens[1])}
    if head == "weekly":
        if len(tokens) != 3:
            raise SpecError("usage: weekly <mon..sun> HH:MM")
        wd = tokens[1].lower()
        if wd not in WEEKDAYS:
            raise SpecError(f"bad weekday {tokens[1]!r} — "
                            + ",".join(WEEKDAYS))
        return {"type": "weekly", "weekday": wd, "time": _parse_time(tokens[2])}
    if head == "once":
        if len(tokens) != 3:
            raise SpecError("usage: once YYYY-MM-DD HH:MM")
        m = _DATE_RE.fullmatch(tokens[1])
        if not m:
            raise SpecError(f"bad date {tokens[1]!r} — expected YYYY-MM-DD")
        try:
            datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            raise SpecError(f"bad date {tokens[1]!r} — not a real day") from None
        return {"type": "once",
                "at": f"{tokens[1]} {_parse_time(tokens[2])}"}
    raise SpecError(f"unknown schedule {tokens[0]!r} — forms: every 30m | "
                    "daily 08:00 | weekly mon 08:00 | once 2026-10-26 09:00")


def describe(job: dict) -> str:
    """Schedule fields -> the canonical spec string (list display/echo)."""
    typ = job.get("type")
    if typ == "interval":
        m = int(job.get("minutes") or 0)
        return f"every {m // 60}h" if m and m % 60 == 0 else f"every {m}m"
    if typ == "daily":
        return f"daily {job.get('time', '')}"
    if typ == "weekly":
        return f"weekly {job.get('weekday', '')} {job.get('time', '')}"
    if typ == "once":
        return f"once {job.get('at', '')}"
    return str(typ or "?")


# ------------------------------------------------------------------ times ---
def _bj(now: float) -> datetime:
    return datetime.fromtimestamp(now, BEIJING)


def _at_epoch(at: str) -> float | None:
    """'YYYY-MM-DD HH:MM' (Beijing) -> epoch, None when unparseable."""
    try:
        return datetime.strptime(at, "%Y-%m-%d %H:%M") \
                       .replace(tzinfo=BEIJING).timestamp()
    except (ValueError, TypeError):
        return None


def _hhmm(value: str) -> tuple[int, int]:
    try:
        return int(value[:2]), int(value[3:5])
    except (ValueError, TypeError, IndexError):
        return (-1, -1)                       # never due (fail-closed)


def occurrence(job: dict, now: float) -> float | None:
    """The latest scheduled occurrence <= now (epoch), None when none yet.

    daily: today's HH:MM if passed, else yesterday's (which the created_at
    comparison then vetoes for fresh jobs). weekly: the target weekday on or
    before today, walking back one more week when today's time is pending."""
    typ = job.get("type")
    if typ == "once":
        return _at_epoch(job.get("at") or "")
    bnow = _bj(now)
    hh, mm = _hhmm(job.get("time") or "")
    if (hh, mm) < (0, 0):
        return None
    day = bnow.date()
    if typ == "weekly":
        target = WEEKDAYS.index(job.get("weekday")) \
            if job.get("weekday") in WEEKDAYS else -1
        if target < 0:
            return None
        day = day - timedelta(days=(bnow.weekday() - target) % 7)
    for candidate in (day, day - timedelta(days=1) if typ == "daily" else None):
        if candidate is None:
            continue
        dt = datetime.combine(candidate, datetime.min.time(),
                              tzinfo=BEIJING) + timedelta(hours=hh, minutes=mm)
        if dt.timestamp() <= now:
            return dt.timestamp()
    return None


# ------------------------------------------------------------------- due ----
def window_due(job: dict, now: float) -> bool:
    """Raw schedule math: the window has arrived and the job is not done.
    Ignores `paused` and the catch-up grace — is_due layers those on top;
    a manual run uses this to decide whether it consumes the window."""
    if not isinstance(job, dict) or job.get("done"):
        return False
    typ = job.get("type")
    if typ == "interval":
        try:
            minutes = int(job.get("minutes"))
        except (ValueError, TypeError):
            return False
        base = job.get("last_fired") or job.get("created_at") or 0
        return now >= float(base) + minutes * 60
    occ = occurrence(job, now)
    if occ is None:
        return False
    if typ == "once":
        return now >= occ
    anchor = max(float(job.get("last_fired") or 0),
                 float(job.get("created_at") or 0))
    return anchor < occ


def _late_by(job: dict, now: float) -> float | None:
    """Seconds past the window arrival (>= 0 once due); None = no window."""
    typ = job.get("type")
    if typ == "interval":
        try:
            minutes = int(job.get("minutes"))
        except (ValueError, TypeError):
            return None
        base = job.get("last_fired") or job.get("created_at") or 0
        return now - (float(base) + minutes * 60)
    occ = occurrence(job, now)
    return None if occ is None else now - occ


def is_due(job: dict, now: float) -> bool:
    """True when this job's window has arrived and not yet been taken.
    Car C1 (A-3): a paused job never fires; catch-up OFF only fires within
    CATCHUP_GRACE_SEC of the window arrival (stale windows are skipped —
    catchup_sweep advances the anchor so the job keeps scheduling)."""
    if not window_due(job, now):
        return False
    if job.get("paused"):
        return False
    if job.get("catchup") == "off":
        late = _late_by(job, now)
        if late is not None and late > CATCHUP_GRACE_SEC:
            return False
    return True


def catchup_sweep(jobs: list[dict], now: float) -> bool:
    """Catch-up OFF bookkeeping (car C1 A-3): skip windows that expired
    beyond CATCHUP_GRACE_SEC, so a missed window is DROPPED instead of
    fired late. Mutates in place; returns True when anything changed (the
    caller persists). catch-up ON jobs are never touched (their one late
    fire is the documented default).

    interval : advance the anchor past every stale window in O(1) — without
               this the job would either never fire again (the grace check
               in is_due) or burst every missed window at once.
    once     : a missed one-shot becomes done + missed (honest display —
               it will never fire).
    daily/weekly: no mutation — the occurrence moves forward by itself and
               is_due's grace check skips today's stale window.
    Paused jobs are swept too: with catch-up OFF, windows that pass while
    held are skipped by definition (resume fires only the NEXT window)."""
    changed = False
    for job in jobs:
        if not isinstance(job, dict) or job.get("done"):
            continue
        if job.get("catchup") != "off":
            continue
        typ = job.get("type")
        if typ == "interval":
            try:
                minutes = int(job.get("minutes"))
            except (ValueError, TypeError):
                continue
            if minutes <= 0:
                continue
            step = minutes * 60
            base = float(job.get("last_fired") or job.get("created_at") or 0)
            # max j >= 1 with base + j*step + GRACE < now  (O(1), ceil so an
            # exact deadline+grace boundary stays INSIDE the grace window)
            j = math.ceil((now - CATCHUP_GRACE_SEC - base) / step) - 1
            if j >= 1:
                job["last_fired"] = base + j * step
                changed = True
        elif typ == "once":
            at = _at_epoch(job.get("at") or "")
            if at is None:
                continue
            if now > at + CATCHUP_GRACE_SEC:
                job["done"] = True
                job["missed"] = True
                changed = True
    return changed


def next_fire(job: dict, now: float) -> float | None:
    """Display-only next scheduled fire (epoch); None = nothing ahead."""
    if not isinstance(job, dict) or job.get("done"):
        return None
    typ = job.get("type")
    if typ == "interval":
        try:
            minutes = int(job.get("minutes"))
        except (ValueError, TypeError):
            return None
        base = float(job.get("last_fired") or job.get("created_at") or now)
        return base + minutes * 60
    if is_due(job, now):
        return now
    if typ == "once":
        return _at_epoch(job.get("at") or "")
    hh, mm = _hhmm(job.get("time") or "")
    if (hh, mm) < (0, 0):
        return None
    bnow = _bj(now)
    day = bnow.date()
    if typ == "weekly":
        target = WEEKDAYS.index(job.get("weekday")) \
            if job.get("weekday") in WEEKDAYS else -1
        if target < 0:
            return None
        day = day - timedelta(days=(bnow.weekday() - target) % 7)
    dt = datetime.combine(day, datetime.min.time(),
                          tzinfo=BEIJING) + timedelta(hours=hh, minutes=mm)
    if dt.timestamp() <= now:
        dt += timedelta(days=1 if typ == "daily" else 7)
    return dt.timestamp()


def fmt_beijing(epoch: float | None) -> str:
    """Epoch -> 'YYYY-MM-DD HH:MM' (Beijing); '-' when unknown."""
    if epoch is None:
        return "-"
    return _bj(epoch).strftime("%Y-%m-%d %H:%M")


def fmt_delta(target: float | None, now: float) -> str:
    """Relative countdown to `target` — language-neutral, locale-free units
    ('+2h13m', '+45s', '+3d4h'); '' = unknown (nothing scheduled).
    A target already reached renders '+0s' (due now: the scheduler fires it
    at the next tick, so the countdown floor is zero, never negative)."""
    if target is None:
        return ""
    secs = int(round(target - now))
    if secs <= 0:
        return "+0s"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    nz = [(v, u) for v, u in ((days, "d"), (hours, "h"), (mins, "m"),
                              (secs, "s")) if v]
    out = f"{nz[0][0]}{nz[0][1]}"
    if len(nz) > 1:
        out += f"{nz[1][0]}{nz[1][1]}"
    return "+" + out


def mark_fired(job: dict, now: float) -> None:
    """Advance the job past this fire (mutates; caller persists)."""
    if job.get("type") == "once":
        job["done"] = True
    job["last_fired"] = now


# --------------------------------------------------------------- registry ---
def default_jobs_path() -> Path:
    return pp.state_dir(REPO) / STATE_FILE_NAME


def _valid_job(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    if raw.get("type") not in ("interval", "daily", "weekly", "once"):
        return False
    if not isinstance(raw.get("id"), str) or not raw["id"]:
        return False
    return isinstance(raw.get("text"), str) and bool(raw["text"].strip())


def load_jobs(path: str | Path) -> list[dict]:
    """Registry -> job list. Absent/corrupt file or garbage entries are
    dropped (fail-soft: a bad line must not take the scheduler down)."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, list):
        return []
    return [j for j in jobs if _valid_job(j)]


def save_jobs(path: str | Path, jobs: list[dict]) -> None:
    """Atomic persist (tmp + os.replace); OSError propagates to the caller
    (an honest failure — the caller maps it to an error line/log)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=1)
                   + "\n", encoding="utf-8")
    os.replace(tmp, path)


def new_id(jobs: list[dict]) -> str:
    """Short unique id (6 hex chars, collision-checked against the table)."""
    taken = {j.get("id") for j in jobs}
    while True:
        cand = uuid.uuid4().hex[:6]
        if cand not in taken:
            return cand


def create_job(jobs: list[dict], spec: str, text: str, *,
               kind: str = "message", source: str = "command",
               origin: int | str | None = None,
               now: float | None = None, name: str | None = None) -> dict:
    """Validate + append one job (mutates `jobs`, returns it).

    Refuses (SpecError): bad spec, once in the past, empty/oversized text,
    unknown kind, an invalid `name`, or a full table. The new job's `id`
    is assigned here.
    `origin` is an int chat id or a str channel endpoint "route:target"
    (anything else becomes None — JSON serialization keeps both).
    `name` is an OPTIONAL display label (console create): when given it is
    validated against NAME grammar and stored as `job["name"]`; when omitted
    the job dict is byte-identical to the pre-label shape, so the NL / CLI /
    ops entry points keep their exact behavior and output."""
    fields = parse_spec(spec)
    if kind not in KINDS:
        raise SpecError(f"unknown kind {kind!r} — message | prompt")
    label = name.strip() if isinstance(name, str) else ""
    if name is not None and not _NAME_RE.fullmatch(label or ""):
        raise SpecError(f"bad name — 1..{MAX_NAME} chars, no control "
                        "characters and no '/'")
    body = (text or "").strip()
    if not body:
        raise SpecError("empty job text")
    if len(body) > MAX_TEXT:
        raise SpecError(f"job text too long — {MAX_TEXT} chars max")
    if len(jobs) >= MAX_JOBS:
        raise SpecError(f"job limit reached ({MAX_JOBS}) — delete one first")
    now = time.time() if now is None else now
    if fields["type"] == "once":
        at = _at_epoch(fields["at"])
        if at is None:
            raise SpecError("bad once time")
        if at <= now:
            raise SpecError("once time has already passed")
    job = {"id": new_id(jobs), **fields, "text": body, "kind": kind,
           "origin": origin if isinstance(origin, (int, str)) else None,
           "source": source if source in SOURCES else "command",
           "created_at": now, "last_fired": None, "done": False}
    if label:
        job["name"] = label
    jobs.append(job)
    return job


def delete_job(jobs: list[dict], job_id: str) -> bool:
    """Remove by id (mutates); False when nothing matched."""
    for i, j in enumerate(jobs):
        if j.get("id") == job_id:
            del jobs[i]
            return True
    return False


# ---------------------------------------------------- per-job controls ----
# Car C1 (A-3): one registry, four entries (/cron, the model `cron` tool,
# the ops CLI, POST /api/cron) — these four helpers are the ONLY mutation
# each entry calls, so the grammar can never drift between them.

def get_job(jobs: list[dict], job_id: str) -> dict | None:
    """Find by id; None when absent (every entry maps that to its own
    honest not-found line — the id is echoed, never a path)."""
    for j in jobs:
        if j.get("id") == job_id:
            return j
    return None


def set_paused(jobs: list[dict], job_id: str, paused: bool) -> bool:
    """Pause/resume one job (mutates); False when nothing matched.
    A paused job never fires automatically; run-now still executes it."""
    job = get_job(jobs, job_id)
    if job is None:
        return False
    if paused:
        job["paused"] = True
    else:
        job.pop("paused", None)
    return True


def set_catchup(jobs: list[dict], job_id: str, value: str) -> bool:
    """Per-job missed-window policy: 'on' (default behavior) | 'off'
    (skip windows stale beyond CATCHUP_GRACE_SEC). Mutates; False when
    nothing matched or the value is not on/off (fail-closed)."""
    if value not in ("on", "off"):
        return False
    job = get_job(jobs, job_id)
    if job is None:
        return False
    if value == "off":
        job["catchup"] = "off"
    else:
        job.pop("catchup", None)
    return True


def request_run(jobs: list[dict], job_id: str, now: float | None = None) -> bool:
    """Queue a manual run (mutates); False when nothing matched.
    Sets `run_requested_at` — the service fire leg executes it through the
    same delivery path as a scheduled fire (window consumed only when it
    had already arrived, see module doc). The console/CLI answer honestly:
    queued, fires at the next 60 s tick; the TG command nudges the leg
    in-process for an immediate run."""
    job = get_job(jobs, job_id)
    if job is None:
        return False
    job["run_requested_at"] = time.time() if now is None else now
    return True


def take_run_requests(jobs: list[dict]) -> list[dict]:
    """Jobs with a queued manual run, registry order (read-only view —
    the fire leg clears the flag after executing)."""
    return [j for j in jobs if isinstance(j, dict)
            and j.get("run_requested_at") is not None]


def due_jobs(jobs: list[dict], now: float) -> list[dict]:
    """Jobs whose window has arrived, in registry order."""
    return [j for j in jobs if is_due(j, now)]


# ---------------------------------------------------------- origin context --
_ORIGIN: dict[str, int | str | None] = {}


def set_origin(chat_id: int | str | None) -> None:
    """Remember which conversation this turn belongs to (single-threaded
    poll loop; the web console leaves it unset -> jobs get origin=None).
    Values: an int Telegram chat id, or a str channel endpoint
    "route:target" — a job created in a channel turn answers on it."""
    _ORIGIN["chat"] = chat_id


def clear_origin() -> None:
    _ORIGIN.pop("chat", None)


def get_origin() -> int | str | None:
    value = _ORIGIN.get("chat")
    return value if isinstance(value, (int, str)) else None


# ------------------------------------------------------------------ CLI -----
def _parse_origin(value: str) -> int | str:
    """argparse type for --origin: a numeric chat id (-> int) or a channel
    endpoint "route:target" (-> str). Anything else raises
    ArgumentTypeError, which argparse reports as exit code 2 with this
    honest message (no machine paths, no credentials)."""
    raw = (value or "").strip()
    if re.fullmatch(r"[0-9]+", raw):
        return int(raw)
    if _ROUTE_ORIGIN_RE.fullmatch(raw):
        return raw
    raise argparse.ArgumentTypeError(
        f"bad origin {value!r} — expected a numeric chat id or "
        f"route:target (e.g. discord:C1)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="mycore.scheduler",
        description="natural-language cron: job registry "
                    "(list/add/del/pause/resume/run/catchup)")
    ap.add_argument("--list", action="store_true", help="list jobs (default)")
    ap.add_argument("--add", metavar="SPEC", help="create a job from a spec")
    ap.add_argument("--text", default="", help="job text (with --add)")
    ap.add_argument("--kind", default="message", choices=KINDS)
    ap.add_argument("--source", default="ops", help="command|model|ops")
    ap.add_argument("--origin", type=_parse_origin, default=None,
                    help="origin chat id or route:target channel endpoint")
    ap.add_argument("--del", dest="delete", metavar="ID", help="delete a job")
    # Car C1 (A-3): per-job controls — same registry helpers the /cron
    # command, the model tool and POST /api/cron call (one grammar).
    ap.add_argument("--pause", metavar="ID", help="pause a job (no auto fire)")
    ap.add_argument("--resume", metavar="ID", help="resume a paused job")
    ap.add_argument("--run", dest="run_now", metavar="ID",
                    help="queue a manual run (fires at the service's next "
                         "tick; the schedule is not consumed early)")
    ap.add_argument("--catchup", nargs=2, metavar=("ID", "on|off"),
                    help="set the missed-window policy for a job")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args(argv)

    path = default_jobs_path()
    # --- per-job control ops (car C1) share one load-modify-save shape ---
    ctrl = None
    if args.pause:
        ctrl = (args.pause, "pause", None)
    elif args.resume:
        ctrl = (args.resume, "resume", None)
    elif args.run_now:
        ctrl = (args.run_now, "run", None)
    elif args.catchup:
        ctrl = (args.catchup[0], "catchup", args.catchup[1])
    if ctrl:
        job_id, op, value = ctrl
        try:
            jobs = load_jobs(path)
            if op == "pause":
                ok = set_paused(jobs, job_id, True)
            elif op == "resume":
                ok = set_paused(jobs, job_id, False)
            elif op == "run":
                ok = request_run(jobs, job_id)
            else:
                ok = set_catchup(jobs, job_id, value or "")
            if ok:
                save_jobs(path, jobs)
        except OSError as e:
            print(f"{op} failed: {type(e).__name__}", file=sys.stderr)
            return 1
        if not ok:
            if op == "catchup" and value not in ("on", "off"):
                print("catchup failed: expected on|off", file=sys.stderr)
            else:
                print(f"no job with id {job_id}", file=sys.stderr)
            return 1
        if op == "pause":
            print(f"paused {job_id} — no automatic fires; --run still works")
        elif op == "resume":
            print(f"resumed {job_id}")
        elif op == "run":
            print(f"queued {job_id} — runs at the service's next tick "
                  "(the schedule is unchanged)")
        else:
            print(f"catchup {value} set for {job_id}")
        return 0
    if args.add:
        try:
            jobs = load_jobs(path)
            job = create_job(jobs, args.add, args.text, kind=args.kind,
                             source=args.source, origin=args.origin)
            save_jobs(path, jobs)
        except SpecError as e:
            print(f"add failed: {e}", file=sys.stderr)
            return 2
        except OSError as e:
            print(f"add failed: {type(e).__name__}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(job, ensure_ascii=False, indent=2))
        else:
            print(f"created {job['id']}: {describe(job)} · {job['kind']}"
                  f" · next {fmt_beijing(next_fire(job, time.time()))}")
        return 0
    if args.delete:
        try:
            jobs = load_jobs(path)
            ok = delete_job(jobs, args.delete)
            if ok:
                save_jobs(path, jobs)
        except OSError as e:
            print(f"del failed: {type(e).__name__}", file=sys.stderr)
            return 1
        if not ok:
            print(f"no job with id {args.delete}", file=sys.stderr)
            return 1
        print(f"deleted {args.delete}")
        return 0

    jobs = load_jobs(path)
    now = time.time()
    if args.json:
        print(json.dumps(
            [{"id": j.get("id"), "spec": describe(j), "kind": j.get("kind"),
              "type": j.get("type"), "text": j.get("text"),
              "origin": j.get("origin"), "source": j.get("source"),
              "done": bool(j.get("done")),
              "paused": bool(j.get("paused")),
              "catchup": "off" if j.get("catchup") == "off" else "on",
              "missed": bool(j.get("missed")),
              "run_requested": j.get("run_requested_at") is not None,
              "next": fmt_beijing(next_fire(j, now)),
              "due": is_due(j, now)} for j in jobs],
            ensure_ascii=False, indent=2))
    else:
        if not jobs:
            print("cron: no jobs")
        for j in jobs:
            state = " (missed)" if j.get("missed") else \
                    " (done)" if j.get("done") else ""
            if j.get("paused"):
                state += " (paused)"
            if j.get("catchup") == "off":
                state += " (catchup off)"
            print(f"{j['id']} · {describe(j)} · {j.get('kind')} · "
                  f"next {fmt_beijing(next_fire(j, now))}{state} · "
                  f"{j.get('text', '')[:40]}")
        print(f"cron: {len(jobs)} job(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
