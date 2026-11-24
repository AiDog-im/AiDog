"""Profile-local, single-use owner approvals for write-capable fleets."""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import tempfile
import time
from pathlib import Path

from lib import profile_paths as pp

REPO = Path(__file__).resolve().parent.parent
STATE_FILE = "fleet_plans.json"
MAX_PENDING = 8
TERMINAL_KEEP = 24
TERMINAL = ("approved", "rejected", "cancelled", "consumed", "expired")
TTL_SECONDS = 86400
MAX_PREVIEW_BYTES = 64 * 1024
UNAVAILABLE = "fleet plan store unavailable"
WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "run_bash", "browser_click", "browser_type",
    "browser_open", "browser_close", "browser_snapshot", "browser_screenshot",
    "execute_code", "send_file", "generate_image", "exchange_prepare_order",
    "exchange_execute_order", "cron", "backup_run", "settings",
    "connector_search", "community_post", "chain_read",
})


def _path() -> Path:
    return pp.state_dir(REPO).resolve() / STATE_FILE


def _load(path: Path) -> list[dict]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if path.is_symlink():
            raise OSError(UNAVAILABLE) from None
        return []
    if not isinstance(doc, list):
        raise ValueError(UNAVAILABLE)
    ids = set()
    for row in doc:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"] or row["id"] in ids
                or row.get("status") not in ("pending", *TERMINAL)):
            raise ValueError(UNAVAILABLE)
        for field in ("ts", "decided_at"):
            if field in row:
                value = row[field]
                if type(value) is not int and not (
                        isinstance(value, float) and math.isfinite(value)):
                    raise ValueError(UNAVAILABLE)
        if "fleet" in row and not isinstance(row["fleet"], str):
            raise ValueError(UNAVAILABLE)
        if "clones" in row and (type(row["clones"]) is not int or row["clones"] < 0):
            raise ValueError(UNAVAILABLE)
        if "tools" in row and (not isinstance(row["tools"], list)
                               or not all(isinstance(t, str) for t in row["tools"])):
            raise ValueError(UNAVAILABLE)
        ids.add(row["id"])
    return doc


def _save(path: Path, plans: list[dict]) -> None:
    fd, name = tempfile.mkstemp(prefix=".fleet_plans-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(plans, handle, ensure_ascii=True, indent=1, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        Path(name).unlink(missing_ok=True)


def _lock(path: Path):
    from .evolution import mutation_lock
    return mutation_lock(path, REPO)


def _expires(plan: dict) -> bool:
    ts = plan.get("expires_at")
    return (type(ts) not in (int, float) or not math.isfinite(ts)
            or time.time() >= ts)


def load_plans() -> list[dict]:
    plans = _load(_path())
    for plan in plans:
        if (plan.get("digest") and plan["status"] in ("pending", "approved")
                and _expires(plan)):
            plan["status"] = "expired"
    return plans


def _trim(plans: list[dict]) -> list[dict]:
    for plan in plans:
        if (plan.get("digest") and plan["status"] in ("pending", "approved")
                and _expires(plan)):
            plan["status"] = "expired"
    pending = [p for p in plans if p["status"] == "pending"]
    terminal = [p for p in plans if p["status"] in TERMINAL]
    return pending + terminal[:TERMINAL_KEEP]


def is_write_capable(tools) -> bool:
    return isinstance(tools, (list, tuple, set)) and any(
        str(t) in WRITE_TOOLS for t in tools)


def _canonical(preview: dict) -> bytes:
    return json.dumps(preview, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _digest(preview: dict) -> str:
    return hashlib.sha256(_canonical(preview)).hexdigest()


def _matches(plan: dict, digest: str) -> bool:
    preview = plan.get("preview")
    return (isinstance(preview, dict) and isinstance(digest, str) and bool(digest)
            and plan.get("digest") == digest == _digest(preview))


def propose(preview: dict) -> dict:
    """Store the full bounded owner preview. Never approve a truncated brief."""
    if len(_canonical(preview)) > MAX_PREVIEW_BYTES:
        return {"error": "fleet approval preview exceeds size limit"}
    try:
        path = _path()
        with _lock(path):
            plans = _trim(_load(path))
            if sum(p["status"] == "pending" for p in plans) >= MAX_PENDING:
                return {"error": f"too many pending fleet plans ({MAX_PENDING}) — "
                                 "the owner must review the queue first"}
            now = time.time()
            tasks = preview["tasks"]
            plan = {"id": f"f{int(now)}-{secrets.token_hex(16)}",
                    "ts": now, "expires_at": now + TTL_SECONDS,
                    "status": "pending", "clones": len(tasks),
                    "tools": sorted({t for task in tasks for t in task["tools"]}),
                    "preview": preview, "digest": _digest(preview)}
            _save(path, _trim([plan, *plans]))
            return plan
    except (OSError, ValueError, RuntimeError, OverflowError):
        return {"error": UNAVAILABLE}


def get_plan(plan_id: str) -> dict | None:
    return next((p for p in load_plans() if p["id"] == plan_id), None)


def decide(plan_id: str, action: str, digest: str = "") -> tuple[dict | None, str]:
    if action not in ("approve", "reject", "cancel"):
        return None, "action must be one of: approve | reject | cancel"
    try:
        path = _path()
        with _lock(path):
            plans = _load(path)
            plan = next((p for p in plans if p["id"] == plan_id), None)
            if plan is None:
                return None, "unknown plan id"
            if plan["status"] != "pending":
                return None, f"plan is already {plan['status']}"
            if action == "approve" and (not _matches(plan, digest) or _expires(plan)):
                return None, "plan preview is missing, changed or expired"
            plan["status"] = {"approve": "approved", "reject": "rejected",
                              "cancel": "cancelled"}[action]
            plan["decided_at"] = time.time()
            _save(path, _trim(plans))
            return plan, ""
    except (OSError, ValueError, RuntimeError, OverflowError):
        return None, UNAVAILABLE


def consume(plan_id: str, preview: dict) -> bool:
    """Claim once under the same lock as proposals and decisions."""
    try:
        digest = _digest(preview)
        path = _path()
        with _lock(path):
            plans = _load(path)
            plan = next((p for p in plans if p["id"] == plan_id), None)
            if (not plan or plan["status"] != "approved" or _expires(plan)
                    or not _matches(plan, digest)):
                return False
            plan["status"] = "consumed"
            plan["consumed_at"] = time.time()
            _save(path, _trim(plans))
            return True
    except (OSError, ValueError, RuntimeError, OverflowError):
        return False


def owner_preview(plan_id: str) -> dict | None:
    plan = get_plan(plan_id)
    if (not plan or plan["status"] != "pending"
            or not _matches(plan, plan.get("digest", ""))):
        return None
    return {"id": plan_id, "digest": plan["digest"],
            "expires_at": plan["expires_at"], "preview": plan["preview"]}


def public_row(plan: dict) -> dict:
    """Unauthenticated queue metadata. Legacy excerpts are private too."""
    return {
        "id": plan.get("id"),
        "ts": plan.get("ts"),
        "status": plan.get("status"),
        "fleet": plan.get("fleet"),
        "clones": plan.get("clones"),
        "tools": plan.get("tools") or [],
        "desc": "",
        "decided_at": plan.get("decided_at"),
    }
