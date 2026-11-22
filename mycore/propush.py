"""mycore.propush — the closed loop: an owner's gap becomes a spoken finding.

P1 of the need-A car. The pieces live in three modules on purpose:

    mycore.profiling   local analysis (no egress, no model)
    mycore.prosearch   the ONE egress surface (search + fetch + knowledge)
    mycore.propush     THIS module — the control half

The control half is deliberately tiny and deliberately proposes: it turns the
profile's urgent needs into SNIFF findings the OWN delivery chain already
speaks, and it hands the owner **proposals** for program operations (a web
source, a pinned keyword, a topic term). Every proposal is a click: nothing
here writes a setting without the owner clicking it, and nothing here can
actuate a device.

Contracts (asserted by mycore/tests/run.py):

* ONE DELIVERY CHAIN. A raised finding is an ordinary SNIFF finding
  (`source="owner"`, `rule="owner_profile"`); it is spoken by the existing
  cron delivery path with its quiet hours, digest cap, PAUSE and mark-first
  semantics. No second delivery code path exists here.
* PASSIVE BY DEFAULT. The push mode ships `passive`: findings land on the
  board and are never spoken until the owner switches the mode to `auto`.
* PROPOSE-ONLY. The program operations return a PROPOSAL (a settings patch
  the owner can apply); they never write a setting themselves.
* ZERO MODEL TOKENS. No model client, no loop, no prompt string.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import sniff  # noqa: E402  (the findings log + the delivery chain)

REPO = Path(__file__).resolve().parent.parent

# The one rule id the profiling lane owns. The owner's rule list never has
# to mention it: the service passes it as an extra speakable rule when the
# push mode says so.
RULE_ID = "owner_profile"
SOURCE_ID = "owner"

PUSH_MODES = ("passive", "auto")
DEFAULT_PUSH_MODE = "passive"

MAX_PROPOSALS = 12
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
MAX_TOKEN_CHARS = 60


class PushError(RuntimeError):
    """Chat-safe failure: field names and expected shapes only."""


def parse_push_mode(value: object) -> str:
    """The push mode. An unknown value falls back to the shipped default
    (`passive`) — never to `auto`: a hand-edited file must not start
    speaking on its own."""
    if value is None:
        return DEFAULT_PUSH_MODE
    if not isinstance(value, str) or value not in PUSH_MODES:
        return DEFAULT_PUSH_MODE
    return value


def wants_push(profile: dict, *, mode: str, limit: int = 20) -> list[dict]:
    """The rows that deserve to be spoken: tier L1 AND trait keen (the
    owner's own mapping), by score. Everything else stays passive."""
    if mode != "auto":
        return []
    rows = profile.get("keywords") if isinstance(profile.get("keywords"), list) \
        else []
    out = [r for r in rows
           if isinstance(r, dict) and r.get("tier") == "L1"
           and r.get("trait") == "keen"]
    out.sort(key=lambda r: (-float(r.get("score") or 0.0),
                             str(r.get("token") or "")))
    return out[:limit]


def finding_row(keyword: dict, *, now: float) -> dict:
    """One synthesized SNIFF finding: the same shape as every other finding
    in the log, so the board, the dedup signature and the delivery chain
    need no special case. The payload carries the keyword and its counts —
    never a session excerpt."""
    token = str(keyword.get("token") or "")
    day = str(keyword.get("last_day") or "")
    trigger = f"{token}@{day}"
    summary = (f"owner interest needs a look: {token} "
               f"({keyword.get('topic') or 'other'}, "
               f"{keyword.get('coverage') or 'gap'})")
    return {
        "source": SOURCE_ID,
        "rule": RULE_ID,
        "severity": "high" if keyword.get("tier") == "L1" else "info",
        "summary": summary[:sniff.SUMMARY_CAP],
        "trigger": trigger[:sniff.TRIGGER_CAP],
        "payload": {
            "keyword": token[:MAX_TOKEN_CHARS],
            "topic": str(keyword.get("topic") or "other")[:32],
            "coverage": str(keyword.get("coverage") or "gap")[:16],
            "msgs": int(keyword.get("tf") or 0),
            "days": int(keyword.get("days") or 0),
        },
        "first_seen": now,
        "last_seen": now,
        "count": 1,
        "ack": False,
        "delivered": False,
    }


def raise_findings(profile: dict, *, mode: str, now: float | None = None,
                   findings: list[dict] | None = None) -> dict:
    """Raise the L1+keen rows as SNIFF findings.

    `findings` passed = hermetic (nothing is read, nothing is written);
    passing None loads the log and persists it (the ownership rule every
    engine here follows). Returns {raised, total, findings}."""
    t = float(now if now is not None else time.time())
    own = findings is None
    rows = sniff.load_findings() if findings is None else findings
    if not isinstance(rows, list):
        rows = []
    wanted = wants_push(profile, mode=mode)
    raised = 0
    for kw in wanted:
        row = finding_row(kw, now=t)
        rows, created = sniff.upsert_finding(
            rows, source=row["source"], rule=row["rule"],
            severity=row["severity"], summary=row["summary"],
            trigger=row["trigger"], payload=row["payload"], now=t)
        if created:
            raised += 1
    if own and raised:
        try:
            sniff.save_findings(rows)
        except OSError as e:  # noqa: BLE001 — a full disk is not a crash
            return {"raised": raised, "total": len(rows),
                    "error": f"persist failed: {type(e).__name__}"[:160]}
    return {"raised": raised, "total": len(rows)}


# ------------------------------------------------- propose-only operations ---
def propose_source(keyword: str, url: str, *, source_id: str = "") -> dict:
    """Propose a SNIFF web source for one keyword+url. A PROPOSAL: the
    owner clicks, the console writes it through the strict sniff partition
    gate. Nothing here writes anything."""
    token = str(keyword or "").strip()[:MAX_TOKEN_CHARS]
    target = str(url or "").strip()
    if not token:
        raise PushError("a keyword is required")
    if not target.startswith(("http://", "https://")):
        raise PushError("the url must be http(s)")
    sid = source_id or re.sub(r"[^a-z0-9_-]+", "-", token.casefold()).strip("-")
    if not sid or not _SLUG_RE.match(sid):
        raise PushError("the source id must be a slug (a-z, 0-9, -, _)")
    return {
        "kind": "web",
        "id": sid[:32],
        "enabled": True,
        "rate_cap_per_min": 30,
        "url": target[:256],
        "regex": "",
        "field": "value",
    }


def propose_pin(keyword: str) -> dict:
    """Propose pinning one keyword: a pinned keyword is `keen` by the
    owner's own hand (the trait table's first lane)."""
    token = str(keyword or "").strip()
    if not token:
        raise PushError("a keyword is required")
    if len(token) > MAX_TOKEN_CHARS:
        raise PushError(f"the keyword must be at most {MAX_TOKEN_CHARS} chars")
    return {"pin": token}


def propose_topic(keyword: str, topic: str) -> dict:
    """Propose adding one term to an owner topic bucket."""
    token = str(keyword or "").strip()
    slug = str(topic or "").strip()
    if not token:
        raise PushError("a keyword is required")
    if not slug or not _SLUG_RE.match(slug):
        raise PushError("the topic must be a slug (a-z, 0-9, -, _)")
    if len(token) > MAX_TOKEN_CHARS:
        raise PushError(f"the term must be at most {MAX_TOKEN_CHARS} chars")
    return {"topic": {"slug": slug, "terms": [token]}}


def proposals_for(keyword: str, *, url: str = "") -> list[dict]:
    """The proposal menu for one keyword — every entry is a click, none of
    them acts."""
    out: list[dict] = []
    if url:
        try:
            out.append({"kind": "source", "label": "add web source",
                        "payload": propose_source(keyword, url)})
        except PushError:
            pass
    try:
        out.append({"kind": "pin", "label": "pin keyword",
                    "payload": propose_pin(keyword)})
    except PushError:
        pass
    try:
        out.append({"kind": "topic", "label": "add topic term",
                    "payload": propose_topic(keyword, "interest")})
    except PushError:
        pass
    return out[:MAX_PROPOSALS]
