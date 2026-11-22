"""mycore.pipeline — the acquisition pipeline (owner concept: automatic lead
acquisition, "B car").

Design anchor: ``docs/design/acquisition_pipeline_design.md`` — §9 records the
engineering decisions this module implements (storage, resumability, the send
gate, compliance, PII bounds, and the two honest gaps: the model drafting brain
is a deliberately unwired seam, and real-SMTP acceptance is still blocked on
credentials).

Pipeline shape (six stages, one document):

    source   -> leads     (owner adds one, imports a list, or the service
                           collects our OWN public forum threads)
    qualify  -> score     (a deterministic contactability score, with its
                           arithmetic shown as the reason — never a prediction)
    draft    -> drafts    (a template body the owner can edit; nothing sends)
    approve  -> approved  (owner click in the console, Bearer-gated route)
    send     -> sent      (owner click again; every gate re-checked here)
    track    -> replies   (inbound mail matching a known lead is classified;
                           nothing is ever auto-answered)

State is ONE atomic JSON document at ``<state>/pipeline/pipeline.json`` (temp +
``os.replace``, the cron-registry / SENSE-board convention). Every stage is a
pure function over that document, which is exactly why resumability needs no
proof beyond the file: a restart re-reads the same document and continues.

Hard gates (mechanically enforced, never by convention):

  * ``pipeline.enabled`` is the kill switch — shipped **false**; while it is
    off nothing sources, drafts or sends.
  * A send needs ``status == "approved"``, a ready ``channels.email`` route, a
    non-empty physical postal address (CAN-SPAM), a non-suppressed recipient,
    and headroom under ``pipeline.daily_cap`` (Beijing day).
  * There is no pipeline tool in the model tool registry: the model cannot
    reach this module. Only the owner's console routes and the service leg do.
  * ``List-Unsubscribe`` + ``Reply-To`` headers ride every message and the body
    always ends with the opt-out line; an inbound ``opt_out`` suppresses the
    address permanently.

PII bounds (documented in §9 of the design): a lead keeps name / address /
org / source / the ≤400-char signal text, never a mailbox body; a reply keeps
sender, a ≤200-char subject and a ≤200-char excerpt plus its class. The list
caps are enforced on write (500 leads / 500 drafts / 200 replies / 200 events).

English source per repo i18n policy; the console renders the locale packs.
Exit codes elsewhere (no CLI here): the service leg and the console route are
the only entries, and both are fail-soft.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from pathlib import Path

try:                                   # POSIX advisory lock (Linux/macOS);
    import fcntl                       # native Windows has no fcntl — see
except ImportError:                    # mutation_lock()'s honest fallback
    fcntl = None

REPO = Path(__file__).resolve().parent.parent
from lib import profile_paths as pp  # noqa: E402  (car F: profile state seam)

DIR_NAME = "pipeline"
DOC_NAME = "pipeline.json"
DOC_VERSION = 1

# --------------------------------------------------------------- settings ---
# The console's settings partition (`pipeline`), validated fail-closed by
# web.server's settings grammar. Shipped OFF: the kill switch is the default.
DEFAULT_SETTINGS = {
    "enabled": False,          # kill switch — off = no sourcing, no drafting, no sending
    "auto_source": False,      # the service leg may collect our own forum threads
    "interval_min": 60,        # service-leg cadence while enabled (5..1440)
    "daily_cap": 5,            # sends per Beijing day (1..50); drafts are bounded by it too
    "from_name": "",           # signature name (owner's own wording)
    "physical_address": "",    # CAN-SPAM postal address — required before any send
    "opt_out_note": "",        # override for the standard English opt-out line
}

ENABLED_MIN, ENABLED_MAX = 5, 1440
CAP_MIN, CAP_MAX = 1, 50
FROM_NAME_MAX = 120
ADDRESS_MAX = 200
OPT_OUT_MAX = 240

DEFAULT_OPT_OUT = ('If this is not relevant, reply "unsubscribe" and I will not '
                   'write again.')

# ------------------------------------------------------------ hard bounds ---
MAX_LEADS = 500
MAX_DRAFTS = 500
MAX_REPLIES = 200
EVENT_CAP = 200
# The suppression list is addresses only and grows only through opt-outs, but
# it is still bounded: an oversized (hand-edited) file truncates to the
# newest entries, and a full table evicts the oldest with an honest event —
# a suppression that cannot be recorded must never look like one that was.
SUPPRESSED_MAX = 500
NAME_MAX = 120
EMAIL_MAX = 160
ORG_MAX = 120
SIGNAL_MAX = 400
SUBJECT_MAX = 140
BODY_MAX = 4000
NOTE_MAX = 200
ERROR_MAX = 160

SOURCES = ("manual", "import", "discussions")
STAGES = ("source", "qualify", "draft", "approve", "send", "track")
LEAD_STATUS = ("new", "qualified", "drafted", "contacted", "replied",
               "suppressed", "skipped")
DRAFT_STATUS = ("draft", "approved", "rejected", "sent", "failed")
REPLY_CLASSES = ("opt_out", "interested", "question", "other")

# Contactability score (NOT a prediction — see the design §9 row): the weights
# are the entire rule, and the reason string spells them out for the owner.
QUALIFY_MIN = 50
SOURCE_WEIGHT = {"manual": 40, "import": 35, "discussions": 25}
ADDRESS_POINTS = 15
SIGNAL_POINTS = 15
ORG_POINTS = 10
SIGNAL_POINTS_MIN_LEN = 20

EMAIL_RE = re.compile(r"^[^@\s<>]{1,64}@[^@\s<>.]{1,120}\.[^@\s<>]{2,24}$")

# Reply classification: opt-out is checked FIRST (a "stop, and by the way how
# much" message is an opt-out, not an opportunity). Bilingual because the
# product ships en + zh and a prospect answers in either.
OPT_OUT_WORDS = ("unsubscribe", "opt out", "opt-out", "remove me",
                 "stop writing", "stop emailing", "do not contact",
                 "don't contact", "no more email", "take me off",
                 "\u9000\u8ba2", "\u53d6\u6d88\u8ba2\u9605",
                 "\u4e0d\u8981\u518d\u53d1", "\u522b\u518d\u53d1")
INTERESTED_WORDS = ("interested", "tell me more", "how much", "pricing",
                    "price", "cost", "sign me up", "sign up", "buy",
                    "purchase", "demo", "yes please", "sounds good",
                    "\u611f\u5174\u8da3", "\u591a\u5c11\u94b1", "\u4ef7\u683c",
                    "\u62a5\u4ef7", "\u600e\u4e48\u4e70", "\u8bd5\u7528",
                    "\u8be6\u60c5")
QUESTION_WORDS = ("?", "\uff1f", "how ", "what ", "why ", "when ", "where ",
                  "can you", "could you", "does it", "is it",
                  "\u5982\u4f55", "\u600e\u4e48", "\u4e3a\u4ec0\u4e48",
                  "\u80fd\u4e0d\u80fd")


class PipelineError(ValueError):
    """Validation / gate refusal. The message is owner-facing and never
    carries a credential or a mailbox body."""


# ----------------------------------------------------------- mutation lock ---
# One read-modify-write at a time, across BOTH writers: the web console
# (threads in one process) and the service leg (a second process). Without it,
# two concurrent `draft.send` calls could each pass the daily-cap check before
# either save lands (auditor P2-1), and a service tick that overlaps a send
# could overwrite the `sent` status with a stale copy.
MUTATION_LOCK = threading.Lock()   # layer 1 of mutation_lock(); public for the tests


@contextlib.contextmanager
def mutation_lock(repo: Path | None = None):
    """Serialize pipeline read-modify-write sections.

    Layer 1 (always): an in-process `threading.Lock` — the console server is a
    ThreadingHTTPServer, so two POSTs race inside one process.
    Layer 2 (POSIX, best-effort): `flock` on `<state>/pipeline/.lock` — the
    service leg runs in ANOTHER process. Where `fcntl` is unavailable (native
    Windows) this layer is a documented no-op: the in-process lock still holds
    and the console is the only sender, so the cap stays atomic there too.

    Blocking by design: writers are short (a local file RMW; the SMTP call
    happens inside a send, ~1s worst case), and skipping a write would be a
    silent data loss — worse than a brief wait.
    """
    path = state_dir(repo) / ".lock"
    with MUTATION_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a+", encoding="utf-8")
        except OSError:
            handle = None          # unwritable state dir: the inner save will
        try:                       # raise the honest error anyway
            if handle is not None and fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if handle is not None:
                if fcntl is not None:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                handle.close()


# ------------------------------------------------------------------ files ---
def state_dir(repo: Path | None = None) -> Path:
    return pp.state_dir(Path(repo) if repo is not None else REPO) / DIR_NAME


def doc_path(repo: Path | None = None) -> Path:
    return state_dir(repo) / DOC_NAME


def _now(now: float | None) -> float:
    return float(now) if isinstance(now, (int, float)) else time.time()


def empty() -> dict:
    """A fresh document. Every list is present so no caller branch has to
    treat absence as a value."""
    return {"version": DOC_VERSION,
            "seq": {"leads": 0, "drafts": 0, "replies": 0},
            "leads": [], "drafts": [], "replies": [], "suppressed": [],
            "events": [],
            "last_source_ts": 0.0, "last_tick_ts": 0.0, "updated_at": 0.0}


def load(path: Path | None = None) -> dict:
    """Read the document. Missing / unreadable / malformed state is an honest
    empty pipeline — never a guessed partial (the cron registry's contract)."""
    try:
        raw = (Path(path) if path is not None else doc_path()).read_text(
            encoding="utf-8")
    except OSError:
        return empty()
    try:
        doc = json.loads(raw)
    except ValueError:
        return empty()
    if not isinstance(doc, dict):
        return empty()
    out = empty()
    rows: dict[str, list[dict]] = {}
    for key in ("leads", "drafts", "replies", "suppressed", "events"):
        value = doc.get(key)
        rows[key] = [row for row in value if isinstance(row, dict)] \
            if isinstance(value, list) else []
    # Documented caps hold on LOAD too (auditor P2-3): a syntactically valid
    # but oversized hand-edited file is truncated to the same bound the write
    # path enforces — newest-first for the lists the writer evicts from the
    # front (replies, events), insertion order for the ones it refuses to grow
    # past (leads, drafts, suppressed).
    out["leads"] = rows["leads"][:MAX_LEADS]
    out["drafts"] = rows["drafts"][:MAX_DRAFTS]
    out["replies"] = rows["replies"][-MAX_REPLIES:]
    out["suppressed"] = rows["suppressed"][-SUPPRESSED_MAX:]
    out["events"] = rows["events"][-EVENT_CAP:]
    seq = doc.get("seq")
    if isinstance(seq, dict):
        for key in ("leads", "drafts", "replies"):
            val = seq.get(key)
            out["seq"][key] = val if isinstance(val, int) and val >= 0 else 0
            # Never hand out a colliding id after a corrupted/sparse counter
            # (auditor P2-2): the floor is the list LENGTH AND the highest
            # numeric suffix already on file — a file holding [L1, L3] with
            # seq=1 must resume at L4, not at L3.
            highest = 0
            for row in out[key]:
                m = re.fullmatch(r"[LDR](\d+)", str(row.get("id") or ""))
                if m:
                    highest = max(highest, int(m.group(1)))
            out["seq"][key] = max(out["seq"][key], len(out[key]), highest)
    for key in ("last_source_ts", "last_tick_ts", "updated_at"):
        val = doc.get(key)
        out[key] = float(val) if isinstance(val, (int, float)) else 0.0
    return out


def save(doc: dict, path: Path | None = None) -> None:
    """Atomic write (temp + os.replace). Raises OSError: an unwritable state
    file is an honest error, never a silent success."""
    target = Path(path) if path is not None else doc_path()
    doc["updated_at"] = _now(doc.get("updated_at") or None)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / (target.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


# --------------------------------------------------------------- settings ---
def settings_doc(repo: Path | None = None) -> dict:
    """The full settings document (``pipeline`` plus the channel block the
    compliance headers read). {} on any read/parse failure — fail closed on
    absence, exactly like the SENSE board's reader."""
    try:
        doc = json.loads(pp.settings_path(Path(repo) if repo is not None
                                         else REPO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _clip(value, cap: int) -> str:
    text = value if isinstance(value, str) else ""
    text = text.strip()
    return text[:cap]


def _int_in(value, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    num = int(value)
    return num if low <= num <= high else default


def normalize(settings: dict | None) -> dict:
    """The validated ``pipeline`` block. Absent or hand-edited junk collapses
    to the fail-closed default (enabled=false) per field — a bad cap never
    becomes an unlimited cap."""
    block = (settings or {}).get("pipeline") if isinstance(settings, dict) else None
    block = block if isinstance(block, dict) else {}
    out = dict(DEFAULT_SETTINGS)
    out["enabled"] = bool(block.get("enabled")) if isinstance(
        block.get("enabled"), bool) else DEFAULT_SETTINGS["enabled"]
    out["auto_source"] = bool(block.get("auto_source")) if isinstance(
        block.get("auto_source"), bool) else DEFAULT_SETTINGS["auto_source"]
    out["interval_min"] = _int_in(block.get("interval_min"),
                                  DEFAULT_SETTINGS["interval_min"],
                                  ENABLED_MIN, ENABLED_MAX)
    out["daily_cap"] = _int_in(block.get("daily_cap"),
                               DEFAULT_SETTINGS["daily_cap"],
                               CAP_MIN, CAP_MAX)
    out["from_name"] = _clip(block.get("from_name"), FROM_NAME_MAX)
    out["physical_address"] = _clip(block.get("physical_address"), ADDRESS_MAX)
    out["opt_out_note"] = _clip(block.get("opt_out_note"), OPT_OUT_MAX)
    return out


def _email_from_settings(settings: dict | None) -> str:
    chans = (settings or {}).get("channels") if isinstance(settings, dict) else None
    email = chans.get("email") if isinstance(chans, dict) else None
    return _clip(email.get("from") if isinstance(email, dict) else "", EMAIL_MAX)


# ---------------------------------------------------------------- helpers ---
def normalize_email(value) -> str:
    return _clip(value, EMAIL_MAX).lower()


def email_ok(value) -> bool:
    text = normalize_email(value)
    return bool(text) and EMAIL_RE.match(text) is not None


def _beijing_day(ts: float) -> str:
    """Beijing calendar day of an epoch (the caps are per owner-day; the repo
    convention is a fixed +08:00 offset, no DST — scheduler/evolution parity)."""
    return time.strftime("%Y-%m-%d", time.gmtime(float(ts) + 8 * 3600))


def _event(doc: dict, stage: str, lead_id: str, note: str, now: float) -> None:
    doc.setdefault("events", []).append({
        "ts": now, "stage": stage, "lead": _clip(lead_id, 16),
        "note": _clip(note, NOTE_MAX)})
    if len(doc["events"]) > EVENT_CAP:
        del doc["events"][:len(doc["events"]) - EVENT_CAP]


def _next_id(doc: dict, kind: str) -> str:
    seq = doc.setdefault("seq", {"leads": 0, "drafts": 0, "replies": 0})
    seq[kind] = int(seq.get(kind) or 0) + 1
    prefix = {"leads": "L", "drafts": "D", "replies": "R"}[kind]
    return f"{prefix}{seq[kind]}"


def _find(rows: list[dict], key: str, value) -> dict | None:
    for row in rows:
        if row.get(key) == value:
            return row
    return None


def suppressed_set(doc: dict) -> set[str]:
    return {row.get("email") for row in doc.get("suppressed") or []
            if isinstance(row.get("email"), str) and row["email"]}


def sends_today(doc: dict, now: float | None = None) -> int:
    day = _beijing_day(_now(now))
    return sum(1 for row in doc.get("drafts") or []
               if row.get("status") == "sent"
               and isinstance(row.get("sent_at"), (int, float))
               and _beijing_day(row["sent_at"]) == day)


# ------------------------------------------------------------ stage: source ---
def add_lead(doc: dict, name="", email="", org="", signal="",
             source="manual", now: float | None = None) -> dict:
    """Add one lead. Refuses (PipelineError) an invalid address, a suppressed
    address, a duplicate, and a full table — with the reason the console
    shows. `email` may be empty for a forum-sourced signal (the pipeline never
    invents an address); such a lead qualifies but can never be drafted."""
    now = _now(now)
    if source not in SOURCES:
        raise PipelineError("unknown source")
    name = _clip(name, NAME_MAX)
    email_n = normalize_email(email)
    org = _clip(org, ORG_MAX)
    signal = _clip(signal, SIGNAL_MAX)
    if not name and not email_n and not signal:
        raise PipelineError("a lead needs at least one of name, email or signal")
    if email_n and not email_ok(email_n):
        raise PipelineError("invalid email address")
    if email_n and email_n in suppressed_set(doc):
        raise PipelineError("address is on the suppression list")
    if email_n and _find(doc.get("leads") or [], "email", email_n):
        raise PipelineError("a lead with that address already exists")
    if len(doc.get("leads") or []) >= MAX_LEADS:
        raise PipelineError(f"lead table is full (max {MAX_LEADS})")
    row = {"id": _next_id(doc, "leads"), "name": name, "email": email_n,
           "org": org, "signal": signal, "source": source, "status": "new",
           "score": None, "reason": "", "created_at": now, "updated_at": now}
    doc.setdefault("leads", []).append(row)
    _event(doc, "source", row["id"], f"{source}: {name or email_n or 'signal'}",
           now)
    return row


IMPORT_LINE_MAX = 4000
IMPORT_ROWS_MAX = 200


def import_leads(doc: dict, text, source="import",
                 now: float | None = None) -> dict:
    """Bulk paste, one lead per line:

        name <email@example.com> | org | signal

    The address is the only required part (`name` may be blank, pipes may be
    omitted). Invalid lines are REPORTED, never silently dropped; rows past the
    table cap are refused with the count."""
    now = _now(now)
    raw = text if isinstance(text, str) else ""
    lines = [ln.strip() for ln in raw.splitlines()[:IMPORT_ROWS_MAX + 1]]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    added, errors = 0, []
    for line in lines[:IMPORT_ROWS_MAX]:
        parts = [p.strip() for p in line.split("|")]
        head = parts[0] if parts else ""
        org = parts[1] if len(parts) > 1 else ""
        signal = parts[2] if len(parts) > 2 else ""
        m = re.search(r"<([^<>]+)>", head) or re.search(r"(\S+@\S+)", head)
        if not m:
            errors.append("line without an email address")
            continue
        email = m.group(1)
        name = (head[:m.start()] + head[m.end():]).replace("<", "").replace(
            ">", "").strip()
        try:
            add_lead(doc, name=name, email=email, org=org, signal=signal,
                     source=source, now=now)
            added += 1
        except PipelineError as e:
            errors.append(str(e))
    if len(lines) > IMPORT_ROWS_MAX:
        errors.append(f"row limit reached (max {IMPORT_ROWS_MAX} per paste)")
    return {"added": added, "errors": errors[:20], "scanned": len(lines)}


FORUM_SCAN_LIMIT = 30
FORUM_SIGNAL_MAX = 200


def source_discussions(doc: dict, fetch=None, now: float | None = None) -> dict:
    """Collect our OWN public forum threads as lead signals (D5: own-site
    signals only — no scraping, no purchased lists). A thread author becomes a
    lead with **no address** and the thread title as the signal: the pipeline
    does not guess anyone's email. Rows already seen (same author + title) are
    skipped, so a re-run is idempotent."""
    now = _now(now)
    if fetch is None:
        try:
            from . import community as community_mod
        except Exception as e:                      # noqa: BLE001
            return {"added": 0, "scanned": 0, "available": False,
                    "note": f"community module unavailable ({type(e).__name__})"}
        fetch = community_mod.list_discussions
    try:
        rows = fetch(FORUM_SCAN_LIMIT)
    except Exception as e:                          # noqa: BLE001 — network
        return {"added": 0, "scanned": 0, "available": False,
                "note": f"forum read failed ({type(e).__name__})"}
    if not isinstance(rows, list) or not rows:
        return {"added": 0, "scanned": 0, "available": False,
                "note": "no forum threads returned"}
    added, scanned = 0, 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        scanned += 1
        author = _clip(row.get("author") or row.get("user") or "", NAME_MAX)
        title = _clip(row.get("title"), FORUM_SIGNAL_MAX)
        if not title:
            continue
        seen = any(l.get("source") == "discussions"
                   and l.get("name") == author
                   and title in (l.get("signal") or "")
                   for l in doc.get("leads") or [])
        if seen:
            continue
        try:
            add_lead(doc, name=author, signal=title, source="discussions",
                     now=now)
            added += 1
        except PipelineError:
            break                                   # table full / dedupe
    doc["last_source_ts"] = now
    if added:
        _event(doc, "source", "", f"forum: {added} new signal(s)", now)
    return {"added": added, "scanned": scanned, "available": True, "note": ""}


# ----------------------------------------------------------- stage: qualify ---
def score_lead(lead: dict) -> tuple[int, str]:
    """(score, reason). The reason IS the arithmetic — the console shows it
    next to the number so the owner never mistakes it for a prediction."""
    parts, score = [], 0
    weight = SOURCE_WEIGHT.get(str(lead.get("source")), 0)
    score += weight
    parts.append(f"source {lead.get('source') or '—'} +{weight}")
    if lead.get("email"):
        score += ADDRESS_POINTS
        parts.append(f"address +{ADDRESS_POINTS}")
    signal = lead.get("signal") or ""
    if len(signal) >= SIGNAL_POINTS_MIN_LEN:
        score += SIGNAL_POINTS
        parts.append(f"signal +{SIGNAL_POINTS}")
    if lead.get("org"):
        score += ORG_POINTS
        parts.append(f"org +{ORG_POINTS}")
    score = max(0, min(100, score))
    return score, " \u00b7 ".join(parts)


def qualify_all(doc: dict, now: float | None = None) -> int:
    """Score every lead that is still `new` (or re-score a `qualified` one so a
    later address edit lands). Returns the number that newly qualify."""
    now = _now(now)
    newly = 0
    for lead in doc.get("leads") or []:
        if lead.get("status") not in ("new", "qualified"):
            continue
        score, reason = score_lead(lead)
        lead["score"], lead["reason"] = score, reason
        if score >= QUALIFY_MIN:
            if lead.get("status") == "new":
                newly += 1
            lead["status"] = "qualified"
        elif lead.get("status") == "new":
            lead["status"] = "skipped"
        lead["updated_at"] = now
    if newly:
        _event(doc, "qualify", "", f"{newly} lead(s) qualified", now)
    return newly


# ------------------------------------------------------------ stage: draft ---
def render_subject(lead: dict) -> str:
    name = (lead.get("name") or "").strip()
    return _clip(f"Hello from I'm AiDog" if not name
                 else f"{name}, a note from I'm AiDog", SUBJECT_MAX)


def render_body(lead: dict, block: dict) -> str:
    """Deterministic template (v1). The owner edits it in the queue before
    approving; the model-brain seam is deliberately unwired (§9). The body
    always carries the opt-out line and the postal address — `send_approved`
    refuses to send without the address, so the footer is never half-empty."""
    name = (lead.get("name") or "").strip()
    greeting = f"Hi {name}," if name else "Hi,"
    lines = [greeting, ""]
    signal = (lead.get("signal") or "").strip()
    if lead.get("source") == "discussions" and signal:
        lines += [f'Your post in our forum ("{_clip(signal, 160)}") is exactly '
                  'the kind of question I exist to answer.', ""]
    lines += [
        "I'm AiDog is a free, self-hosted AI companion: it runs on your own "
        "machine, keeps its memory in files you own, and asks before it sends "
        "or spends anything.",
        "",
        "It is free for any use, with no paid tier and no usage fee. If you "
        "want to see how it behaves before installing anything, the console "
        "and the docs are public.",
        "",
    ]
    opt_out = block.get("opt_out_note") or DEFAULT_OPT_OUT
    lines += [opt_out, ""]
    signature = (block.get("from_name") or "").strip()
    address = (block.get("physical_address") or "").strip()
    if signature:
        lines.append(signature)
    if address:
        lines.append(address)
    return _clip("\n".join(lines).strip(), BODY_MAX)


def draft_ready(doc: dict, settings: dict | None, now: float | None = None,
                brain=None) -> int:
    """Draft for every qualified lead that has an address and no live draft.

    Bounded by the remaining daily budget (cap − sent today − already-approved),
    so drafting can never build a backlog the cap forbids sending. The `brain`
    seam is accepted and unused in v1 (see §9); passing one changes nothing
    today, which is asserted by the core suite."""
    now = _now(now)
    block = normalize(settings)
    cap = block["daily_cap"]
    budget = cap - sends_today(doc, now)
    budget -= sum(1 for d in doc.get("drafts") or []
                  if d.get("status") == "approved")
    if budget <= 0:
        return 0
    live = {d.get("lead_id") for d in doc.get("drafts") or []
            if d.get("status") in ("draft", "approved", "sent")}
    made = 0
    for lead in doc.get("leads") or []:
        if made >= budget:
            break
        if lead.get("status") != "qualified" or not lead.get("email"):
            continue
        if lead.get("id") in live or lead.get("email") in suppressed_set(doc):
            continue
        if len(doc.get("drafts") or []) >= MAX_DRAFTS:
            break
        row = {"id": _next_id(doc, "drafts"), "lead_id": lead.get("id"),
               "subject": render_subject(lead),
               "body": render_body(lead, block),
               "status": "draft", "created_at": now, "decided_at": 0.0,
               "sent_at": 0.0, "error": "",
               "edited": False, "brain": "template"}
        doc.setdefault("drafts", []).append(row)
        lead["status"] = "drafted"
        lead["updated_at"] = now
        made += 1
        _event(doc, "draft", lead.get("id") or "", f"draft {row['id']}", now)
    return made


def require_draft(doc: dict, draft_id: str) -> dict:
    """Look a draft up or raise PipelineError("unknown draft"). The console
    routes use this to answer 400 for a stale/unknown id BEFORE any action —
    `send_approved` keeps its own defensive refusal for direct callers."""
    row = _find(doc.get("drafts") or [], "id", draft_id)
    if row is None:
        raise PipelineError("unknown draft")
    return row


def edit_draft(doc: dict, draft_id: str, subject=None, body=None,
               now: float | None = None) -> dict:
    """Owner edit of a pending draft (the only stage where text may change)."""
    now = _now(now)
    row = _find(doc.get("drafts") or [], "id", draft_id)
    if row is None:
        raise PipelineError("unknown draft")
    if row.get("status") != "draft":
        raise PipelineError("only a pending draft can be edited")
    if subject is not None:
        text = _clip(subject, SUBJECT_MAX)
        if not text:
            raise PipelineError("subject cannot be empty")
        row["subject"] = text
    if body is not None:
        text = _clip(body, BODY_MAX)
        if not text:
            raise PipelineError("body cannot be empty")
        row["body"] = text
    row["edited"] = True
    row["decided_at"] = 0.0
    _event(doc, "draft", _lead_id_of(doc, row), f"draft {draft_id} edited", now)
    return row


def _lead_id_of(doc: dict, draft: dict) -> str:
    lead = _find(doc.get("leads") or [], "id", draft.get("lead_id"))
    return str(lead.get("id")) if lead else ""


def approve(doc: dict, draft_id: str, now: float | None = None) -> dict:
    """Owner approval. Checks the compliance fields up front so the owner
    learns about a missing address HERE, not after clicking send."""
    now = _now(now)
    row = _find(doc.get("drafts") or [], "id", draft_id)
    if row is None:
        raise PipelineError("unknown draft")
    if row.get("status") != "draft":
        raise PipelineError("only a pending draft can be approved")
    row["status"] = "approved"
    row["decided_at"] = now
    _event(doc, "approve", _lead_id_of(doc, row), f"draft {draft_id}", now)
    return row


def reject(doc: dict, draft_id: str, reason="",
           now: float | None = None) -> dict:
    now = _now(now)
    row = _find(doc.get("drafts") or [], "id", draft_id)
    if row is None:
        raise PipelineError("unknown draft")
    if row.get("status") not in ("draft", "approved"):
        raise PipelineError("only a pending or approved draft can be rejected")
    row["status"] = "rejected"
    row["decided_at"] = now
    row["error"] = _clip(reason, ERROR_MAX)
    lead = _find(doc.get("leads") or [], "id", row.get("lead_id"))
    if lead and lead.get("status") in ("drafted", "qualified"):
        lead["status"] = "skipped"
        lead["updated_at"] = now
    _event(doc, "reject", _lead_id_of(doc, row),
           _clip(reason or f"draft {draft_id}", NOTE_MAX), now)
    return row


# ------------------------------------------------------------- stage: send ---
def compliance_headers(settings: dict | None, recipient: str) -> dict:
    """The headers every outreach message carries. A missing `from` address is
    not papered over: `send_approved` refuses before this is reached."""
    sender = _email_from_settings(settings)
    headers = {"Reply-To": sender}
    if sender:
        headers["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    return headers


def send_approved(doc: dict, draft_id: str, settings: dict | None, channel,
                  now: float | None = None) -> dict:
    """The ONLY send path. Every gate is re-checked here (kill switch, status,
    route readiness, compliance fields, suppression, daily cap), and a refusal
    is a returned reason — never a raise, never a silent drop.

    Returns {"ok": bool, "draft": row|None, "reason": str, "sent": bool}."""
    now = _now(now)
    block = normalize(settings)
    row = _find(doc.get("drafts") or [], "id", draft_id)
    if row is None:
        return {"ok": False, "draft": None, "sent": False,
                "reason": "unknown draft"}
    lead = _find(doc.get("leads") or [], "id", row.get("lead_id"))
    if lead is None:
        return {"ok": False, "draft": row, "sent": False,
                "reason": "draft has no lead"}
    if not block["enabled"]:
        return {"ok": False, "draft": row, "sent": False,
                "reason": "pipeline is off (kill switch)"}
    if row.get("status") != "approved":
        return {"ok": False, "draft": row, "sent": False,
                "reason": "draft is not approved"}
    target = normalize_email(lead.get("email"))
    if not email_ok(target):
        return {"ok": False, "draft": row, "sent": False,
                "reason": "lead has no valid address"}
    if target in suppressed_set(doc):
        return {"ok": False, "draft": row, "sent": False,
                "reason": "address is on the suppression list"}
    if not block["physical_address"]:
        return {"ok": False, "draft": row, "sent": False,
                "reason": "compliance: set a physical postal address first"}
    if channel is None:
        return {"ok": False, "draft": row, "sent": False,
                "reason": "email route unavailable"}
    try:
        if not bool(getattr(channel, "enabled", False)):
            return {"ok": False, "draft": row, "sent": False,
                    "reason": "email route is disabled in settings"}
        ok_ready, why = channel.configured()
        if not ok_ready:
            return {"ok": False, "draft": row, "sent": False,
                    "reason": f"email route not configured: {why}"}
    except Exception as e:                          # noqa: BLE001
        return {"ok": False, "draft": row, "sent": False,
                "reason": f"email route check failed ({type(e).__name__})"}
    if sends_today(doc, now) >= block["daily_cap"]:
        return {"ok": False, "draft": row, "sent": False,
                "reason": f"daily cap reached ({block['daily_cap']})"}
    body = row.get("body") or ""
    # Compliance lines are re-asserted at SEND time, not trusted from the
    # draft (auditor P2-4): an owner edit may delete them, and the capability
    # page promises both. The EFFECTIVE opt-out line (the owner's override or
    # the standard one) must be in the body; so must the postal address.
    opt_out = block.get("opt_out_note") or DEFAULT_OPT_OUT
    if opt_out and opt_out not in body:
        body = f"{body}\n{opt_out}".strip()
    footer = block.get("physical_address") or ""
    if footer and footer not in body:
        body = f"{body}\n{footer}".strip()     # never ship without the address
    headers = compliance_headers(settings, target)
    try:
        delivered = bool(channel.send_message(target, body,
                                             subject=row.get("subject") or "",
                                             headers=headers))
        err = "" if delivered else "route refused the message (see channel log)"
    except Exception as e:                          # noqa: BLE001 — sanitized
        delivered, err = False, f"send failed ({type(e).__name__})"
    row["sent_at"] = now if delivered else 0.0
    row["status"] = "sent" if delivered else "failed"
    row["error"] = _clip(err, ERROR_MAX)
    if delivered:
        lead["status"] = "contacted"
    lead["updated_at"] = now
    _event(doc, "send", _lead_id_of(doc, row),
           f"draft {draft_id} " + ("sent" if delivered else f"failed: {err}"),
           now)
    return {"ok": delivered, "draft": row, "sent": delivered,
            "reason": "" if delivered else err}


# ------------------------------------------------------------ stage: track ---
def classify_reply(text) -> str:
    lowered = (text or "").lower() if isinstance(text, str) else ""
    for word in OPT_OUT_WORDS:
        if word in lowered:
            return "opt_out"
    for word in INTERESTED_WORDS:
        if word in lowered:
            return "interested"
    for word in QUESTION_WORDS:
        if word in lowered:
            return "question"
    return "other"


def observe_inbound(doc: dict, sender, subject="", text="",
                    now: float | None = None) -> dict | None:
    """Record an inbound message **only when the sender is a known lead** — an
    unknown sender is not tracked (the pipeline is not a mailbox scraper).
    Records the class, a bounded subject and a bounded excerpt; the full
    message stays in the chat session IMAP already delivered it to. Never
    answers, never sends."""
    now = _now(now)
    addr = normalize_email(sender)
    if not email_ok(addr):
        return None
    lead = _find(doc.get("leads") or [], "email", addr)
    if lead is None:
        return None
    cls = classify_reply(text or subject)
    row = {"id": _next_id(doc, "replies"), "lead_id": lead.get("id"),
           "from": addr, "cls": cls,
           "subject": _clip(subject, NOTE_MAX),
           "excerpt": _clip(" ".join((text or "").split()), NOTE_MAX),
           "ts": now, "handled": False}
    doc.setdefault("replies", []).append(row)
    if len(doc["replies"]) > MAX_REPLIES:
        del doc["replies"][:len(doc["replies"]) - MAX_REPLIES]
    if cls == "opt_out":
        suppress(doc, addr, note="inbound opt-out", now=now)
    else:
        lead["status"] = "replied"
        lead["updated_at"] = now
    _event(doc, "track", lead.get("id") or "", f"reply {row['id']}: {cls}", now)
    return row


def suppress(doc: dict, email, note="", now: float | None = None) -> dict:
    """Add an address to the permanent suppression list (idempotent). Any
    pending draft for that address is rejected on the spot."""
    now = _now(now)
    addr = normalize_email(email)
    if not email_ok(addr):
        raise PipelineError("invalid email address")
    existing = _find(doc.get("suppressed") or [], "email", addr)
    if existing:
        return existing
    row = {"email": addr, "note": _clip(note, NOTE_MAX), "ts": now}
    doc.setdefault("suppressed", []).append(row)
    lead = _find(doc.get("leads") or [], "email", addr)
    if len(doc["suppressed"]) > SUPPRESSED_MAX:
        # Dropping the oldest suppression is the least-bad full-table policy:
        # the alternatives (refusing a new opt-out, or dropping the newest)
        # are worse for compliance. The eviction is recorded, never silent.
        del doc["suppressed"][:len(doc["suppressed"]) - SUPPRESSED_MAX]
        _event(doc, "track", lead.get("id") if lead else "",
               "suppression list full: oldest entry evicted", now)
    if lead:
        lead["status"] = "suppressed"
        lead["updated_at"] = now
        for draft in doc.get("drafts") or []:
            if draft.get("lead_id") == lead.get("id") and \
                    draft.get("status") in ("draft", "approved"):
                draft["status"] = "rejected"
                draft["error"] = "recipient opted out"
                draft["decided_at"] = now
    _event(doc, "track", lead.get("id") if lead else "", f"suppressed {addr}", now)
    return row


def handle_reply(doc: dict, reply_id: str, now: float | None = None) -> dict:
    now = _now(now)
    row = _find(doc.get("replies") or [], "id", reply_id)
    if row is None:
        raise PipelineError("unknown reply")
    row["handled"] = True
    row["handled_at"] = now
    return row


# ------------------------------------------------------------------- tick ---
def auto_tick(doc: dict, settings: dict | None, fetch=None,
              now: float | None = None, brain=None) -> dict:
    """One automatic pass: source (when `auto_source`), qualify, draft. It NEVER
    sends — sending requires the owner's per-message click. Returns the counts
    so the service leg can log one honest line."""
    now = _now(now)
    block = normalize(settings)
    if not block["enabled"]:
        return {"ok": False, "reason": "disabled", "sourced": 0,
                "qualified": 0, "drafted": 0, "sent": 0}
    sourced, note = 0, ""
    if block["auto_source"]:
        report = source_discussions(doc, fetch=fetch, now=now)
        sourced, note = int(report.get("added") or 0), str(report.get("note") or "")
    qualified = qualify_all(doc, now=now)
    drafted = draft_ready(doc, settings, now=now, brain=brain)
    doc["last_tick_ts"] = now
    return {"ok": True, "reason": "", "sourced": sourced,
            "qualified": qualified, "drafted": drafted, "sent": 0,
            "note": note}


# ------------------------------------------------------------------ view ----
LEADS_PAGE = 50
DRAFTS_PAGE = 50
REPLIES_PAGE = 20
EVENTS_PAGE = 30


def collect(settings: dict | None, doc: dict | None = None) -> dict:
    """GET /api/pipeline read model. Counters are derived from records; a stage
    with nothing recorded reports 0 and the view renders `—` for the derived
    rate figures. No credential material, no mailbox bodies."""
    doc = doc if isinstance(doc, dict) else load()
    block = normalize(settings)
    leads = list(doc.get("leads") or [])
    drafts = list(doc.get("drafts") or [])
    replies = list(doc.get("replies") or [])
    now = time.time()
    counts = {
        "leads": len(leads),
        "new": sum(1 for l in leads if l.get("status") == "new"),
        "qualified": sum(1 for l in leads if l.get("status") == "qualified"),
        "drafted": sum(1 for d in drafts if d.get("status") == "draft"),
        "approved": sum(1 for d in drafts if d.get("status") == "approved"),
        "sent": sum(1 for d in drafts if d.get("status") == "sent"),
        "rejected": sum(1 for d in drafts if d.get("status") == "rejected"),
        "failed": sum(1 for d in drafts if d.get("status") == "failed"),
        "replies": len(replies),
        "opt_out": sum(1 for r in replies if r.get("cls") == "opt_out"),
        "suppressed": len(doc.get("suppressed") or []),
        "sends_today": sends_today(doc, now),
    }
    lead_rows = sorted(leads, key=lambda r: float(r.get("created_at") or 0),
                       reverse=True)[:LEADS_PAGE]
    draft_rows = sorted(drafts, key=lambda r: float(r.get("created_at") or 0),
                        reverse=True)[:DRAFTS_PAGE]
    reply_rows = sorted(replies, key=lambda r: float(r.get("ts") or 0),
                        reverse=True)[:REPLIES_PAGE]
    events = list(doc.get("events") or [])[-EVENTS_PAGE:]
    events.reverse()
    return {
        "ok": True,
        "enabled": block["enabled"],
        "auto_source": block["auto_source"],
        "interval_min": block["interval_min"],
        "daily_cap": block["daily_cap"],
        "from_name": block["from_name"],
        "physical_address_set": bool(block["physical_address"]),
        "opt_out_note": block["opt_out_note"],
        "qualify_min": QUALIFY_MIN,
        "stages": list(STAGES),
        "counts": counts,
        "leads": lead_rows,
        "drafts": draft_rows,
        "replies": reply_rows,
        "events": events,
        "last_source_ts": float(doc.get("last_source_ts") or 0.0),
        "last_tick_ts": float(doc.get("last_tick_ts") or 0.0),
        "updated_at": float(doc.get("updated_at") or 0.0),
        "checked_at": now,
    }
