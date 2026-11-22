"""mycore.fleetplan — owner sign-off for a WRITE-capable clone fleet (P1b).

D3 (adjudicated 2026-11-20): a fleet whose clones may WRITE must wait for
the owner's approval; a read-only fleet runs immediately. This module is the
proposal store for that approval — the same shape as the exchange order
plans (`plan` → the owner approves / rejects / cancels), NOT the settings
proposal store (`mycore.agentplan`): a fleet plan authorizes ONE dispatch,
it does not carry a settings patch.

Contract:
  * storage `<state>/fleet_plans.json` (profile seam pp.state_dir, so the
    service process that proposes and the console that approves read the
    same file);
  * bounds: at most MAX_PENDING pending plans, TERMINAL_KEEP recent closed
    rows, a bounded description (the owner's screen shows it) — overflow is
    an honest refusal, never a silent drop;
  * plan ids: `f<epoch seconds>-<4 random hex>` — sortable, collision-safe;
  * a missing/corrupt store reads as [] (plans are proposals, not data);
  * approval is a HUMAN action recorded by the console (Bearer); the
    dispatcher only READS the approved plan.

Stdlib only; English comments (repo i18n policy).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
from lib import profile_paths as pp  # noqa: E402  (per-profile state seam)

STATE_FILE = "fleet_plans.json"
MAX_PENDING = 8                  # proposals awaiting the owner, bounded
TERMINAL_KEEP = 24               # recent approved/rejected rows
TERMINAL = ("approved", "rejected", "cancelled", "consumed")
MAX_DESC = 400                  # the owner's screen shows this
# A fleet is write-capable when any clone could reach a write tool. The
# list mirrors the tools mycore.tools registers as state-changing.
WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "run_bash", "browser_click", "browser_type",
    "browser_open", "browser_close", "browser_snapshot", "browser_screenshot",
    "execute_code", "send_file", "generate_image", "exchange_prepare_order",
    "exchange_execute_order", "cron", "backup_run", "settings",
    "connector_search", "community_post", "chain_read",
})


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
    pending = [p for p in plans if p.get("status") == "pending"]
    terminal = [p for p in plans if p.get("status") in TERMINAL]
    return pending + terminal[:TERMINAL_KEEP]


def is_write_capable(tools) -> bool:
    """True when any of the given tool names may change state."""
    if not isinstance(tools, (list, tuple, set)):
        return False
    return any(str(t) in WRITE_TOOLS for t in tools)


def propose(clones: list[dict], tools: list[str], fleet_id: str = "") -> dict:
    """File one write-capable fleet proposal. -> the plan row (the caller
    refuses the dispatch and tells the owner)."""
    pending = [p for p in load_plans() if p.get("status") == "pending"]
    if len(pending) >= MAX_PENDING:
        return {"error": f"too many pending fleet plans ({MAX_PENDING}) — "
                         "the owner must review the queue first"}
    desc = ", ".join(str(c.get("summary") or c.get("task") or "")[:120]
                     for c in (clones or [])[:5])
    if len(desc) > MAX_DESC:
        desc = desc[:MAX_DESC - 3] + "..."
    plan = {"id": f"f{int(time.time())}-{os.urandom(2).hex()}",
            "ts": round(time.time(), 3), "status": "pending",
            "fleet": str(fleet_id or ""),
            "clones": len(clones or []),
            "tools": sorted(str(t) for t in (tools or []))[:12],
            "desc": desc}
    plans = load_plans()
    plans.insert(0, plan)
    _save(_trim(plans))
    return plan


def get_plan(plan_id: str) -> dict | None:
    for p in load_plans():
        if p.get("id") == plan_id:
            return p
    return None


def decide(plan_id: str, action: str) -> tuple[dict | None, str]:
    """Record the owner's decision (approve | reject | cancel). -> (plan, "").
    An unknown id or an unknown action is an honest refusal."""
    if action not in ("approve", "reject", "cancel"):
        return None, "action must be one of: approve | reject | cancel"
    plan = get_plan(plan_id)
    if plan is None:
        return None, "unknown plan id"
    if plan.get("status") != "pending":
        return None, f"plan is already {plan.get('status')}"
    plan["status"] = {"approve": "approved", "reject": "rejected",
                      "cancel": "cancelled"}[action]
    plan["decided_at"] = round(time.time(), 3)
    plans = load_plans()
    for i, p in enumerate(plans):
        if p.get("id") == plan_id:
            plans[i] = plan
            break
    _save(_trim(plans))
    return plan, ""


def consume(plan_id: str) -> bool:
    """Mark an approved plan as CONSUMED: one approval serves one dispatch.
    A second write needs a fresh approval. Returns False when the plan is
    not approved (never consumed twice)."""
    plans = load_plans()
    for i, p in enumerate(plans):
        if p.get("id") == plan_id and p.get("status") == "approved":
            p["status"] = "consumed"
            p["consumed_at"] = round(time.time(), 3)
            plans[i] = p
            _save(_trim(plans))
            return True
    return False


def public_row(plan: dict) -> dict:
    """The console-facing row: a fixed field whitelist (no internals)."""
    return {
        "id": plan.get("id"),
        "ts": plan.get("ts"),
        "status": plan.get("status"),
        "fleet": plan.get("fleet"),
        "clones": plan.get("clones"),
        "tools": plan.get("tools") or [],
        "desc": plan.get("desc") or "",
        "decided_at": plan.get("decided_at"),
    }
