"""mycore.compact — simple context compaction (design section 14.3: simplified fold).

Budgeted folding of old turns: when the accumulated message payload exceeds
`budget_chars`, the oldest non-system turns are collapsed into a single
synthetic summary marker; system/constitution messages are NEVER dropped
(README contract 6 — the body layer outlives any kernel swap).

Deliberately dumb (summarize = one fixed line per folded turn, plus the
constraint lines the line would otherwise drop — car 5-D7): honest,
deterministic, zero dependencies. Smarter summarization can replace the
`fold` step later without changing callers.
English comments per repo i18n policy.
"""
from __future__ import annotations

import re

from .model import first_choice_text

SUMMARY_ROLE = "system"      # folded marker is a system-role note (never re-folded)
KEEP_TAIL_TURNS = 6          # always keep the newest turns intact (dialogue quality)

# Car 5-D7 item 2 (design §4.2 "token-based folding"): the char budget alone
# means a DIFFERENT token count per language (24000 chars is ~6k tokens of
# English but ~14k of Chinese), so a meter-observed chars->tokens ratio drives
# a second, token-denominated trigger. DEFAULT_TOKEN_RATIO is the estimate
# used before any observation exists — and with it the token rule is exactly
# equivalent to the char rule, i.e. no behaviour change until the model has
# actually reported prompt_tokens for this turn's payload (see loop.run_turn).
DEFAULT_TOKEN_RATIO = 0.25

# Lines whose semantics compaction must never drop (D7 iron rule: negations,
# permission edges and unfinished work survive the fold). Deliberately broad
# — a missed line loses a constraint, an extra line costs at most 120 chars.
_KEEP_RE = re.compile(
    r"(?i)(\b(?:not|no|never|don't|dont|cannot|can't|mustn't|forbidden|forbid"
    r"|only|unless|except|must|shall|always|required|todo|tbd|pending"
    r"|unfinished|remaining|awaiting|keep|preserve|stop|refuse|deny)\b"
    # no-space-script negatives kept as \u escapes so this source stays ASCII
    # (repo language lint): do-not / never / forbid / must / only / pending /
    # not-yet — a Chinese fold must lose no more negation than an English one
    r"|[\u4e0d\u52ff\u7981\u5207\u7edd\u5fc5\u4ec5\u53ea\u5f85\u672a])")
KEEP_MAX_LINES = 3           # preserved lines per folded marker
KEEP_MAX_LINE_CHARS = 120    # per preserved line
KEEP_MAX_CHARS = 3 * KEEP_MAX_LINE_CHARS
KEEP_LABEL = " | keep: "


def estimate_tokens(messages: list[dict], ratio: float | None = None) -> int:
    """Token estimate for one payload: `measure()` (chars) scaled by `ratio`.
    `ratio` is the REAL chars->tokens ratio observed from the provider's own
    prompt_tokens for this turn when available; None/unknown falls back to
    DEFAULT_TOKEN_RATIO (~4 chars per token). The result is an ESTIMATE and
    is labelled as one everywhere it surfaces — never presented as a count."""
    r = DEFAULT_TOKEN_RATIO if not isinstance(ratio, (int, float)) else ratio
    r = max(0.01, min(float(r), 5.0))
    return int(measure(messages) * r + 0.5)


def chars_for_tokens(budget_tokens: int, ratio: float) -> int:
    """The char budget equivalent to `budget_tokens` at `ratio` (+1 so that a
    plain `measure() > budget` comparison fires on the first char over)."""
    r = max(0.01, min(float(ratio), 5.0))
    return int(budget_tokens / r) + 1


def measure(messages: list[dict]) -> int:
    """Payload size proxy: sum of content/argument string lengths."""
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):            # multimodal-ish content blocks
            total += sum(len(str(c)) for c in content)
        for c in (m.get("tool_calls") or []):
            total += len(str(c))
    return total


def _preserved_lines(content: str, already: str) -> list[str]:
    """Constraint lines of `content` that `already` (the 160-char snippet the
    marker keeps) does NOT already contain — negations, permission edges,
    unfinished items, keep-these orders. Bounded by KEEP_MAX_LINES /
    KEEP_MAX_LINE_CHARS / KEEP_MAX_CHARS so a fold can never grow unbounded.
    Deterministic: first matching lines win, in source order."""
    out: list[str] = []
    total = 0
    for raw in content.split("\n"):
        line = " ".join(raw.split())
        if not line or line.startswith("["):
            continue
        if line in already:
            continue
        if not _KEEP_RE.search(line):
            continue
        if len(line) > KEEP_MAX_LINE_CHARS:
            line = line[:KEEP_MAX_LINE_CHARS] + "…"
        if total + len(line) > KEEP_MAX_CHARS:
            break
        out.append(line)
        total += len(line)
        if len(out) >= KEEP_MAX_LINES:
            break
    return out


def _fold_turn(msg: dict, idx: int = 0) -> dict:
    """One old turn -> one summary marker (kept small, kept forever).

    Car 5-D7: the marker carries provenance (role + source position) and the
    constraint lines the snippet would otherwise lose (` | keep: ...`), so a
    fold never silently drops a negation, a permission edge or unfinished
    work — the D7 semantics guarantee, asserted by the offline suite."""
    role = msg.get("role", "?")
    content = msg.get("content")
    if isinstance(content, list):
        content = " ".join(str(c.get("text", c)) if isinstance(c, dict) else str(c)
                           for c in content)
    content = (content or "").strip()
    calls = msg.get("tool_calls") or []
    if calls:
        names = ", ".join((c.get("function") or {}).get("name", "?") for c in calls)
        return {"role": SUMMARY_ROLE,
                "content": f"[compacted {role} turn #{idx}: tools used -> {names}]"}
    snippet = content[:160] + ("…" if len(content) > 160 else "")
    kept = _preserved_lines(content, snippet)
    body = f"[compacted {role} turn #{idx}: {snippet}"
    if kept:
        body += KEEP_LABEL + " / ".join(kept)
    return {"role": SUMMARY_ROLE, "content": body + "]"}


def _fold_orphans(result: list[dict]) -> tuple[list[dict], int]:
    """Restore assistant(tool_calls) <-> tool pairing after folding.

    Owner incident 2026-11-07: folding an assistant row that carried
    tool_calls while its tool responses survive (or the reverse) leaves an
    orphan `tool` row, and strict gateways reject the whole payload with
    HTTP400 — "messages with role 'tool' must be a response to a preceeding
    message with 'tool_calls'". Walks the rows while tracking the pending
    announcement: tool rows consume it (a batch of tools after one
    assistant is legal, answered or not), any other row — or the end of
    the list — means the announcement went unanswered, and a tool row with
    no live announcement means its assistant was folded away. Either
    broken side is folded into a marker (content stays visible as marker
    text), so the payload stays valid. Validity outranks the keep_tail
    preference here (a protected orphan would break every future call);
    user rows are never touched, so the live-question rule stands.
    Returns (rows, folded_count).
    """
    out: list[dict] = []
    folded = 0
    pending = -1            # index in `out` of an assistant awaiting tools
    answered = False        # its tool batch has been seen
    for m in result:
        if not isinstance(m, dict):
            out.append(m)
            continue
        role = m.get("role")
        if role == "tool":
            if pending >= 0:
                out.append(m)                    # part of the announced batch
                answered = True
                continue
            out.append(_fold_turn(m, len(out)))  # orphan response -> marker
            folded += 1
            continue
        if role == "assistant" and m.get("tool_calls"):
            if pending >= 0 and not answered:
                out[pending] = _fold_turn(out[pending], pending)
                folded += 1                      # previous announcement empty
            out.append(m)
            pending = len(out) - 1
            answered = False
            continue
        if pending >= 0 and not answered:
            out[pending] = _fold_turn(out[pending], pending)
            folded += 1                          # announcement never answered
        pending = -1
        out.append(m)
    if pending >= 0 and not answered:
        out[pending] = _fold_turn(out[pending], pending)
        folded += 1
    return out, folded


def _is_marker(m: object) -> bool:
    """True when m is a compaction marker produced by this module."""
    return (isinstance(m, dict) and m.get("role") == SUMMARY_ROLE
            and isinstance(m.get("content"), str)
            and m["content"].startswith("[compacted "))


def compact(messages: list[dict], budget_chars: int,
            keep_tail: int = KEEP_TAIL_TURNS) -> tuple[list[dict], int]:
    """Fold oldest non-system turns until `measure() <= budget_chars`.

    Returns (new_messages, folded_count). Never touches system messages,
    never folds the newest `keep_tail` messages, and never folds the newest
    user message — that is the live question being answered right now
    (every caller appends it last; only assistant/tool rows land after it
    mid-turn), so folding it would hide the task from the model AND drop
    the user's row from the persisted session delta (system markers are
    filtered out on save — owner 2026-11-05: tool-heavy turns lost the
    user's messages). Deterministic: same input -> same output. If the
    protected region alone exceeds the budget, nothing is folded (honest:
    we do not delete live dialogue — the caller may raise the budget or
    the model call will simply be long).
    """
    if budget_chars <= 0:
        raise ValueError("budget_chars must be positive")
    if measure(messages) <= budget_chars:
        return messages, 0

    result = list(messages)
    # The live question = the newest user-role row (callers append it last;
    # only assistant/tool rows arrive after it mid-turn). It is never
    # foldable: the model must keep seeing the task, and the session delta
    # persists user rows by identity — a folded question becomes a system
    # marker and silently vanishes from the record (owner 2026-11-05).
    live_user = -1
    for i in range(len(result) - 1, -1, -1):
        m = result[i]
        if isinstance(m, dict) and m.get("role") == "user":
            live_user = i
            break
    folded = 0
    while measure(result) > budget_chars:
        # fold the oldest foldable turn: not system, not an already-folded
        # marker, not the live question, and inside the region kept intact
        # at the tail
        target = None
        for i in range(len(result) - keep_tail):
            m = result[i]
            if not isinstance(m, dict):
                continue
            if m.get("role") == "system":
                continue                                # constitution never folds
            if i == live_user:
                continue                                # never fold the live question
            content = m.get("content")
            if isinstance(content, str) and content.startswith("[compacted "):
                continue                                # never re-fold a marker
            target = i
            break
        if target is None:
            break                    # only system/newest remain — honest stop
        result[target] = _fold_turn(result[target], target)
        folded += 1

    # Pair repair (owner incident 2026-11-07): the fold loop above can stop
    # between an assistant(tool_calls) and its tool responses (budget met
    # or the responses sit inside the protected tail), and the orphan that
    # results makes strict gateways answer HTTP400 for every later call.
    # Fold the unpaired side into a marker too — content preserved, pairing
    # restored — before the marker consolidation below.
    result, repaired = _fold_orphans(result)
    folded += repaired

    # Consolidation: the markers themselves are content and can outgrow the
    # budget. Merge foldable markers once into a single line (never nested,
    # never touching the live tail). If still over after that, stop honestly:
    # live dialogue is never deleted.
    if measure(result) > budget_chars:
        hi = len(result) - keep_tail
        idxs = [i for i in range(hi) if _is_marker(result[i])]
        if len(idxs) >= 2:
            snippets = []
            keeps: list[str] = []
            for i in idxs:
                s = result[i]["content"]
                s = s[len("[compacted "):-1] if s.endswith("]") else s
                # split the constraint clause off FIRST so it can never be
                # swallowed by the snippet cap below (D7: keep it verbatim)
                if KEEP_LABEL in s:
                    s, keep = s.split(KEEP_LABEL, 1)
                    if keep.strip():
                        keeps.append(keep.strip())
                # "user turn: xxx" -> keep the payload part, strip any nesting
                payload = s.split(": ", 1)[1] if ": " in s else s
                snippets.append(payload[len("[compacted "):] if payload.startswith("[compacted ") else payload)
            joined = "; ".join(snippets)
            if len(joined) > 460:
                joined = joined[:460] + "…"
            body = "[compacted %d turns: %s" % (len(idxs), joined)
            if keeps:
                kept = " / ".join(keeps)
                if len(kept) > KEEP_MAX_CHARS:
                    kept = kept[:KEEP_MAX_CHARS] + "…"
                body += KEEP_LABEL + kept
            merged = {"role": SUMMARY_ROLE, "content": body + "]"}
            removed = set(idxs[1:])
            result = [merged if i == idxs[0] else m
                      for i, m in enumerate(result) if i not in removed]
            folded += len(idxs) - 1

    return result, folded


def summarize_choice(doc: dict) -> str:
    """One-line summary from a model completion (used by future smarter folding)."""
    return (first_choice_text(doc) or "").strip()[:300]
