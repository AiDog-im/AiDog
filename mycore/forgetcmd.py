"""mycore.forgetcmd — the owner-facing FORGET control on the chat side.

Design authority: docs/design/memory_suppression_design.md — D1 (a
tombstone, never an in-place deletion), D2 (opaque deterministic source
ids), D3 (the ledger lives in the per-profile state root), D5 (the read
paths filter the set), D10 (honest limits are part of the contract), D11 (a
suppression never crosses stores), D19 (one control, one store, one
confirmation) and D23-D25 (a derived owner-profile item is its own source).

WHAT THIS IS: the chat half of "forget this memory source". It runs in the
SERVICE COMMAND LAYER (mycore.service._command), which answers BEFORE the
model pipeline — so the model can never trigger a forget. Only an
allowlisted sender reaches it at all: the service's allowlist gate runs
before every command, and this module adds no second, weaker gate.

WHAT THIS IS NOT: it deletes nothing. A forget RECORDS A TOMBSTONE
(mycore.memforget.forget): the source stays in its file and only leaves
ACTIVE memory (prompt injection, the console memory view, the owner
profile, recall, restored backups). Every answer says so (D10).

The surface:
    /forget <query>          the matching forgettable sources, numbered,
                             each with its label, its plain channel name,
                             its short opaque source id and its own one-shot
                             confirmation code;
    /forget confirm <code>  the write for the chosen candidate;
    /forget                  the two-line usage.

The three stores the owner can address:
    curated:memory   the curated memory slot  — label = the entry HEADING
    curated:user     the curated user slot    — label = the entry HEADING
    learned          the learned memory slot  — label = the entry HEADING
    profile          a derived owner-profile keyword — label = the token
                                                  itself (it IS the item)

A heading is a label, never a body: the entry's TEXT is never shown, never
echoed and never carried in the chat. The query is not echoed either, so a
query that happens to contain body text cannot leak it back.

The pending confirmation lives in the per-profile STATE root — never in the
tracked body directory — so it never lands in a tracked file. A restart may
drop it; the code then simply no longer matches and the answer says so
honestly. At most ONE pending confirmation exists per chat: a new listing
replaces the previous one, and a confirmation consumes it entirely (every
code of that listing dies with it). The pending document is written
atomically and the command layer is a single thread, so a lost update can
only degrade to an honest refusal — never to a silent write.

Bounded by construction: at most MAX_CANDIDATES candidates, a bounded scan
and read per store, a bounded query length, a bounded number of chats with
a pending confirmation, and a short bounded confirmation code.

Stdlib only. English comments (repo i18n policy); every user-visible
string comes from the locale packs through the caller's `t` resolver.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from mycore import memforget

REPO = Path(__file__).resolve().parent.parent
from lib import profile_paths as pp  # noqa: E402  (per-profile seams)

# ----------------------------------------------------------------- bounds --
MAX_CANDIDATES = 10          # the listing never grows past this
MAX_QUERY = 120              # chars of the owner's query
MAX_BYTES = 200_000          # chars read from one store per scan
MAX_ENTRIES_PER_STORE = 200  # sections scanned per store
MAX_PROFILE_ROWS = 200       # derived keywords scanned
MAX_PENDING_CHATS = 50       # chats that may hold a pending confirmation
PENDING_TTL = 300.0          # seconds a confirmation code stays valid
CODE_LEN = 6                 # the confirmation code is short on purpose
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no ambiguous glyphs

DIR_NAME = "forgetcmd"       # <state>/forgetcmd/
PENDING_NAME = "pending.json"

ID_PREFIX = "src_"
ID_SHOW = 12                 # "src_" + 8 hex chars — the short opaque form


# ------------------------------------------------------------- utilities ---
def short_id(source_id: str) -> str:
    """The short, opaque form of a source id shown in chat.

    The full id stays in the ledger; chat shows a prefix of it — enough to
    recognise, not enough to be a content address."""
    rid = str(source_id or "")
    return rid[:ID_SHOW]


def _now_default() -> float:
    return time.time()


def _read_bounded(path: Path) -> tuple[str, str]:
    """Read one complete bounded snapshot for the confirmation code."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError:
        return "", ""
    if len(raw) > MAX_BYTES:
        return "", ""
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return text, hashlib.sha256(raw).hexdigest()


def _matches(query: str, *fields: str) -> bool:
    """Case-insensitive substring match of the query in any field."""
    if not query:
        return False
    low = query.casefold()
    for field in fields:
        if field and low in field.casefold():
            return True
    return False


def _slot_stores(repo: Path) -> list[tuple[str, Path, str]]:
    """(slot key, path, admission channel) of the three authored stores.

    The paths resolve through the ACTIVE PROFILE (lib.profile_paths), so a
    named profile never addresses the repository's own overlay files — the
    same rule the console's forget control follows (D20)."""
    root = Path(repo)
    mem, usr = pp.memory_slots(root)
    return [
        ("memory", Path(mem), memforget.CHANNEL_CURATED_MEMORY),
        ("user", Path(usr), memforget.CHANNEL_CURATED_USER),
        ("learned", Path(pp.learned_path(root)), memforget.CHANNEL_LEARNED),
    ]


def _profile_rows(state: Path) -> list[dict]:
    """The derived owner-profile rows of the active state root.

    The stored document is read RAW (not through the profiling reader): a
    tombstoned row is skipped by the caller, so a forgotten item is never
    offered again."""
    from mycore import profiling        # the constants own the layout
    path = Path(state) / profiling.STATE_DIR / profiling.PROFILE_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = doc.get("keywords") if isinstance(doc, dict) else None
    return [r for r in (rows or []) if isinstance(r, dict)]


# ----------------------------------------------------------- the listing ---
def candidates(query: str, *, repo: Path, state: Path) -> list[dict]:
    """The forgettable sources that match the query (bounded).

    Each candidate is a dict:
        {"kind": "slot"|"profile", "label", "channel", "source_id",
         "slot", "heading"}          (slot candidates)
        {"kind": "profile", "label", "channel", "source_id", "token"}
                                              (profile candidates)

    The label is a HEADING (curated / learned) or the derived token itself
    (profile). No body, no excerpt, no path and no credential is ever put
    into a candidate. A source that is already tombstoned is not offered
    again — forgetting it twice is a no-op, not a listing."""
    q = (query or "").strip()[:MAX_QUERY]
    if not q:
        return []
    snapshot = memforget.load_set(state=state)
    out: list[dict] = []
    for slot, path, channel in _slot_stores(repo):
        text, revision = _read_bounded(path)
        if not text.strip():
            continue
        _preamble, chunks = memforget.split_sections(
            text, *memforget.levels_for(channel))
        if not chunks:
            # A file with no heading at all is ONE entry (the whole file) —
            # the same shape the console's memory view uses.
            if snapshot.contains_section(channel, text):
                continue
            if _matches(q, text):
                out.append({"kind": "slot", "slot": slot, "heading": "",
                            "label": "", "channel": channel, "revision": revision,
                            "source_id": memforget.source_id(channel, text,
                                                             repo=repo)})
            if len(out) >= MAX_CANDIDATES:
                return out
            continue
        for chunk in chunks[:MAX_ENTRIES_PER_STORE]:
            if snapshot.contains_section(channel, chunk["body"]):
                continue                      # already forgotten
            body = memforget.strip_markers(chunk["body"])
            if not _matches(q, chunk["heading"], body):
                continue
            out.append({"kind": "slot", "slot": slot,
                        "heading": chunk["heading"], "label": chunk["heading"],
                        "channel": channel, "revision": revision,
                        "source_id": memforget.source_id(channel, body,
                                                         repo=repo)})
            if len(out) >= MAX_CANDIDATES:
                return out
    # The derived owner profile is its own channel (D23): a token IS the item.
    for row in _profile_rows(Path(state))[:MAX_PROFILE_ROWS]:
        token = str(row.get("token") or "")
        if not token or snapshot.contains(memforget.CHANNEL_PROFILE, token):
            continue
        if not _matches(q, token):
            continue
        out.append({"kind": "profile", "token": token, "label": token,
                    "channel": memforget.CHANNEL_PROFILE,
                    "source_id": memforget.source_id(
                        memforget.CHANNEL_PROFILE, token, repo=repo)})
        if len(out) >= MAX_CANDIDATES:
            break
    return out[:MAX_CANDIDATES]


# ------------------------------------------------- the pending confirmation -
def pending_path(state: Path) -> Path:
    """<state>/forgetcmd/pending.json — the pending confirmations."""
    return Path(state) / DIR_NAME / PENDING_NAME


def _load_pending(state: Path) -> dict:
    """The pending document ({} when absent or damaged — never a crash)."""
    try:
        raw = pending_path(state).read_text(encoding="utf-8")
    except OSError:
        return {"chats": {}}
    try:
        doc = json.loads(raw)
    except ValueError:
        return {"chats": {}}
    chats = doc.get("chats") if isinstance(doc, dict) else None
    return {"chats": chats if isinstance(chats, dict) else {}}


def _save_pending(state: Path, doc: dict) -> bool:
    """Persist the pending document atomically (tmp + replace)."""
    path = pending_path(state)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(doc, ensure_ascii=True, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def _new_code(used: set[str]) -> str:
    """A short random confirmation code, unique inside the listing."""
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if code not in used:
            used.add(code)
            return code


def _store_pending(chat_id, found: list[dict], *, state: Path,
                   now: float) -> list[tuple[str, dict]]:
    """Record the listing as THE pending confirmation of this chat.

    One chat holds at most one pending confirmation: a new listing replaces
    the previous one, so every code of the old listing stops matching."""
    used: set[str] = set()
    codes: dict[str, dict] = {}
    for cand in found:
        codes[_new_code(used)] = cand
    doc = _load_pending(state)
    chats = doc["chats"]
    chats[str(chat_id)] = {"at": float(now), "codes": codes}
    if len(chats) > MAX_PENDING_CHATS:
        oldest = sorted(chats.items(),
                        key=lambda kv: float((kv[1] or {}).get("at") or 0.0))
        for key, _row in oldest[:len(chats) - MAX_PENDING_CHATS]:
            chats.pop(key, None)
    _save_pending(state, doc)
    return list(codes.items())


def _take_pending(chat_id, code: str, *, state: Path,
                  now: float) -> tuple[str, dict | None]:
    """Consume the pending confirmation of this chat.

    Returns ("ok", candidate) | ("none", None) | ("bad", None).
    The confirmation is consumed ONCE: a wrong, unknown, expired or
    already-used code changes nothing."""
    doc = _load_pending(state)
    chats = doc["chats"]
    key = str(chat_id)
    record = chats.get(key)
    if not isinstance(record, dict):
        return "none", None
    codes = record.get("codes")
    wanted = str(code or "").strip().upper()
    if not isinstance(codes, dict) or wanted not in codes:
        return "bad", None
    at = float(record.get("at") or 0.0)
    age = float(now) - at
    if age > PENDING_TTL or age < -PENDING_TTL:
        chats.pop(key, None)          # an expired listing dies here
        _save_pending(state, doc)
        return "bad", None
    candidate = codes.get(wanted)
    # ONE-SHOT: the whole pending confirmation is consumed by one confirm.
    chats.pop(key, None)
    _save_pending(state, doc)
    if not isinstance(candidate, dict):
        return "bad", None
    return "ok", candidate


# ------------------------------------------------------------- the write ---
def _write(candidate: dict, *, repo: Path, state: Path,
           now: float) -> tuple[str, dict | None]:
    """Record the tombstone for the chosen candidate.

    Returns ("ok", result) | ("gone", None) | ("changed", None) |
    ("fail", None).

    The write goes through mycore.memforget: forget_entry() for the
    curated and learned stores (it resolves the entry body and calls
    memforget.forget(channel, text)), and memforget.forget() with the
    PROFILE channel for a derived keyword (D23). Nothing is deleted."""
    try:
        if candidate.get("kind") == "profile":
            token = str(candidate.get("token") or "")
            if not token:
                return "gone", None
            result = memforget.forget(memforget.CHANNEL_PROFILE, token,
                                      repo=repo, state=state, now=now)
            return "ok", result
        slot = str(candidate.get("slot") or "")
        heading = str(candidate.get("heading") or "")
        for key, path, _channel in _slot_stores(repo):
            if key != slot:
                continue
            if not candidate.get("source_id") or not candidate.get("revision"):
                return "changed", None
            try:
                result = memforget.forget_entry(
                    path, heading, repo=repo, state=state, now=now,
                    expected_id=candidate["source_id"], revision=candidate["revision"])
            except memforget.StaleEntry:
                return "changed", None
            return "ok", result
    except memforget.MemforgetError:
        return "fail", None
    return "gone", None


# ------------------------------------------------------------- the surface -
def handle(chat_id, arg: str, *, t, repo: Path, state: Path,
           now=None) -> str:
    """The whole `/forget` surface. `t` is the caller's locale resolver."""
    clock = now or _now_default
    text = (arg or "").strip()
    if not text:
        return t("cmd.forget.usage")
    parts = text.split(None, 1)
    if parts[0].lower() == "confirm":
        code = (parts[1] if len(parts) > 1 else "").strip()
        if not code:
            return t("cmd.forget.confirm_usage")
        return _confirm(chat_id, code, t=t, repo=repo, state=state,
                        now=clock)
    return _query(chat_id, text, t=t, repo=repo, state=state, now=clock)


def _query(chat_id, query: str, *, t, repo: Path, state: Path,
           now) -> str:
    """`/forget <query>` — the listing and the confirmation codes."""
    try:
        found = candidates(query, repo=repo, state=state)
    except memforget.LedgerUnavailable:
        return t("cmd.forget.fail")
    if not found:
        return t("cmd.forget.none")
    codes = _store_pending(chat_id, found, state=state, now=float(now()))
    lines = [t("cmd.forget.head", n=len(found))]
    for index, (code, cand) in enumerate(codes, 1):
        label = str(cand.get("label") or "") or t("cmd.forget.untitled")
        lines.append(t("cmd.forget.item", n=index, label=label,
                       channel=cand.get("channel", ""),
                       id=short_id(cand.get("source_id", "")),
                       code=code))
    return "\n".join(lines)


def _confirm(chat_id, code: str, *, t, repo: Path, state: Path,
             now) -> str:
    """`/forget confirm <code>` — the write for the chosen candidate."""
    status, candidate = _take_pending(chat_id, code, state=state,
                                      now=float(now()))
    if status == "none":
        return t("cmd.forget.need_confirm")
    if status != "ok" or candidate is None:
        return t("cmd.forget.bad_code")
    outcome, result = _write(candidate, repo=repo, state=state,
                             now=float(now()))
    if outcome == "gone":
        return t("cmd.forget.gone")
    if outcome == "changed":
        return t("cmd.forget.changed")
    if outcome != "ok" or result is None:
        return t("cmd.forget.fail")
    label = str(candidate.get("label") or "") or t("cmd.forget.untitled")
    key = "cmd.forget.already" if result.get("already") else "cmd.forget.ok"
    head = t(key, label=label, channel=result.get("channel", ""),
             id=short_id(result.get("id", "")))
    return head + "\n" + t("cmd.forget.limits")
