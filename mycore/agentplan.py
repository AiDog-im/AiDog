"""mycore.agentplan — owner-approved settings proposals (car C, agent
foundation).

The `settings` tool lets the assistant propose configuration changes on the
owner's behalf — it NEVER writes settings itself. A proposal lands here, in
a bounded plan store the console owns; the owner approves (or rejects) it in
the console, and approval applies it through the console's own validator —
the SAME code path as a hand-typed save (one truth, one gate).

Contract:
  * storage `<state>/agent_settings_plans.json` (profile seam pp.state_dir,
    so the service process that runs the tool and the console that approves
    read the same file);
  * bounds: at most MAX_PENDING pending plans, a patch of at most
    MAX_PATCH_CHARS characters (JSON on the wire), TERMINAL_KEEP recent
    terminal rows kept for the audit face — overflow is an honest refusal,
    never a silent drop;
  * a missing/corrupt store reads as [] (plans are proposals, not data) and
    the next write rebuilds it; every write is atomic (tmp + os.replace);
  * plan ids: `p<epoch seconds>-<4 random hex>` — sortable, collision-safe;
  * the tool may withdraw its OWN pending plan; only the console (Bearer)
    records approve/reject — approval re-validates through the console's
    `validate_settings_patch`, so an invalid plan can never reach
    settings.json.
Stdlib only; English comments (repo i18n policy).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
from lib import profile_paths as pp  # noqa: E402  (per-profile state seam)

STATE_FILE = "agent_settings_plans.json"
MAX_PENDING = 16                 # proposals awaiting the owner, bounded
MAX_PATCH_CHARS = 8000           # one proposal's JSON size ceiling
TERMINAL_KEEP = 24               # recent approved/rejected/withdrawn rows
TERMINAL = ("approved", "rejected", "withdrawn")


def _path() -> Path:
    return pp.state_dir(REPO) / STATE_FILE


def load_plans() -> list[dict]:
    """Newest-first plan rows; a missing or corrupt store is honestly []."""
    try:
        doc = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(doc, list):
        return []
    return [p for p in doc if isinstance(p, dict) and p.get("id")]


def _save(plans: list[dict]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(plans, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _trim(plans: list[dict]) -> list[dict]:
    """Newest-first ordering is maintained by insert-at-0; the terminal ring
    keeps the most recent TERMINAL_KEEP closed rows (pending rows are capped
    at propose time — a defensive drop here would hide an overflow bug)."""
    pending = [p for p in plans if p.get("status") == "pending"]
    terminal = [p for p in plans if p.get("status") in TERMINAL]
    return pending + terminal[:TERMINAL_KEEP]


def top_keys(patch: dict) -> list[str]:
    """Compact summary for the console card: top-level keys, with mycore
    sub-keys inlined (a mycore-only patch would otherwise read as one word)."""
    if not isinstance(patch, dict):
        return []
    out: list[str] = []
    for key in patch:
        if key == "mycore" and isinstance(patch[key], dict):
            out.extend(f"mycore.{k}" for k in list(patch[key])[:6])
        else:
            out.append(str(key))
    return out[:10]


def propose(patch) -> tuple[dict | None, str]:
    """File one proposal. -> (plan, "") on success or (None, reason)."""
    if not isinstance(patch, dict) or not patch:
        return None, "patch must be a non-empty JSON object"
    try:
        blob = json.dumps(patch, ensure_ascii=False)
    except (TypeError, ValueError):
        return None, "patch is not JSON-serializable"
    if len(blob) > MAX_PATCH_CHARS:
        return None, (f"patch too large ({len(blob)} > {MAX_PATCH_CHARS} "
                      "chars) — split it into smaller proposals")
    plans = load_plans()
    pending = [p for p in plans if p.get("status") == "pending"]
    if len(pending) >= MAX_PENDING:
        return None, (f"too many pending plans ({MAX_PENDING}) — the owner "
                      "must review the queue first")
    if any(p.get("patch") == patch for p in pending):
        return None, ("an identical plan is already pending — the owner has "
                      "not reviewed it yet")
    plan = {"id": f"p{int(time.time())}-{os.urandom(2).hex()}",
            "ts": round(time.time(), 3), "status": "pending", "patch": patch}
    plans.insert(0, plan)
    _save(_trim(plans))
    return plan, ""


def get_plan(plan_id: str) -> dict | None:
    for p in load_plans():
        if p.get("id") == plan_id:
            return p
    return None


def withdraw(plan_id: str) -> tuple[bool, str]:
    """The agent retracts its own pending plan (owner untouched)."""
    plans = load_plans()
    for p in plans:
        if p.get("id") == plan_id:
            if p.get("status") != "pending":
                return False, f"plan is {p.get('status')}, not pending"
            p["status"] = "withdrawn"
            p["closed_ts"] = round(time.time(), 3)
            _save(_trim(plans))
            return True, ""
    return False, "unknown plan id"


def record(plan_id: str, status: str, result: str = "") -> tuple[bool, str]:
    """Console side only (Bearer): approve | reject a pending plan."""
    if status not in ("approved", "rejected"):
        return False, "status must be approved or rejected"
    plans = load_plans()
    for p in plans:
        if p.get("id") == plan_id:
            if p.get("status") != "pending":
                return False, f"plan is {p.get('status')}, not pending"
            p["status"] = status
            p["closed_ts"] = round(time.time(), 3)
            if result:
                p["result"] = str(result)[:200]
            _save(_trim(plans))
            return True, ""
    return False, "unknown plan id"


def summarize(limit: int = 20) -> list[dict]:
    """Console + tool `list` rows: identity, age and summary — never the raw
    patch (the console fetches a plan by id when it needs the payload)."""
    rows: list[dict] = []
    for p in load_plans()[: max(1, min(int(limit), TERMINAL_KEEP + MAX_PENDING))]:
        patch = p.get("patch") if isinstance(p.get("patch"), dict) else {}
        rows.append({"id": str(p.get("id")),
                     "ts": p.get("ts"),
                     "status": str(p.get("status") or "pending"),
                     "keys": top_keys(patch),
                     "result": str(p.get("result") or "")})
    return rows
