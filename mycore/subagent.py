"""mycore.subagent — isolated parallel task delegation (car 2-D6).

The `delegate` tool (registered in tools.py) hands 1..3 independent tasks to
sub-agents that run IN PARALLEL threads with:

  * isolated context — each sub-agent gets a fresh message list (one fixed
    system line + the task); the parent conversation never enters, and the
    sub-agent's transcript never returns — the parent context pays only for
    the condensed per-task block in the delegate result;
  * isolated tools — a read-only default set (read_file/grep/glob/
    http_fetch/skill_route); write-capable tools only when the caller names
    them in `tools` (validated against the parent registry);
  * a recursion guard — `delegate` and `execute_code` can NEVER be granted to
    a sub-agent (no nested delegation, no nested folding; also required
    because execute_code's handler closes over the registry it was built in,
    so re-using it under a subset would leak the parent tool set);
  * the parent turn's model connection — `run_turn` binds chat + budget into
    the registry (loop.py), the delegate handler passes them through; a
    sub-agent never reads credentials itself.

Each task runs through the SAME loop.run_turn (honest stop reasons, bounded
iterations); one task's model failure does not fail its siblings, and no
sub-agent ever raises into the parent. Results are capped per task so the
folded delegate output always fits the tool-output budget.

Stdlib only; English comments (repo i18n policy).
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .loop import run_turn
from .tools import KNOWN_TOOL_NAMES, ToolError, ToolRegistry, _shell_spec
from . import fleet
from . import fleetgate
from . import fleetplan
from lib import profile_paths as pp  # noqa: E402  (car F: per-profile settings)

MAX_PARALLEL = 3                    # alignment target: 3 parallel slots
SUB_MAX_ITERS = 12                  # bounded sub-turn (parent loop: 25)
TASK_RESULT_CAP = 6000              # per-task text cap inside the fold
DEFAULT_READ_ONLY = ("read_file", "grep", "glob", "http_fetch", "skill_route")
NEVER_GRANTED = ("delegate", "execute_code")   # recursion / leak guard

SYSTEM_LINE = ("You are an isolated sub-agent of the main assistant. "
               "Complete EXACTLY the task you are given with the tools "
               "provided; do not ask the user questions — make reasonable "
               "assumptions and state them. Reply with the result only: "
               "concise, factual, ready for the parent to use.")


def _allowed_models() -> list[str] | None:
    """Whitelist for per-task model selection (car 4a-M3): the
    web/settings.json `mycore.models` list (non-empty strings), None = not
    configured. Read lazily like service.ChatService._allowed_models so the
    console list and this gate can never disagree; no env precedence (a
    credential-free name list, settings-only on purpose)."""
    try:
        from . import cli  # local import: keeps the module graph acyclic
        stored = json.loads(pp.settings_path(cli.REPO)
                            .read_text(encoding="utf-8"))
        mc = stored.get("mycore") if isinstance(stored, dict) else None
        raw = mc.get("models") if isinstance(mc, dict) else None
        if isinstance(raw, list):
            out = [m for m in raw if isinstance(m, str) and m]
            return out or None
    except (OSError, ValueError, AttributeError, ImportError):
        pass
    return None


def _select(registry: ToolRegistry, wanted: list[str]) -> list[str]:
    """Resolve the sub-agent tool set: read-only default + caller-named
    extras. Raises ToolError with an honest message on an unknown name or a
    recursion-guarded name (never silently drops what the caller asked for)."""
    available = set(registry.names())
    unknown = [n for n in wanted if n not in available]
    if unknown:
        raise ToolError(f"unknown tool(s): {', '.join(sorted(unknown))}")
    blocked = [n for n in wanted if n in NEVER_GRANTED]
    if blocked:
        raise ToolError("sub-agents cannot be granted: "
                        f"{', '.join(sorted(blocked))} (recursion guard)")
    granted = [n for n in wanted if n not in NEVER_GRANTED]
    return sorted(set(DEFAULT_READ_ONLY) | set(granted))


def _limits() -> tuple[int, int]:
    """Owner-configurable sub-agent bounds (car C): settings block
    `mycore.subagent = {max_parallel, max_iters}` — both clamped to the
    shipped safety bounds (1..5 parallel, 1..25 iters) by the console's
    validator; this reader clamps AGAIN so a hand-edited file can never
    exceed them. Absent/invalid -> the shipped defaults (3 / 12). Read
    lazily at call time like every other settings seam, never cached."""
    par, iters = MAX_PARALLEL, SUB_MAX_ITERS
    try:
        from . import cli  # local import: keeps the module graph acyclic
        stored = json.loads(pp.settings_path(cli.REPO)
                            .read_text(encoding="utf-8"))
        blk = stored.get("mycore", {}).get("subagent") \
            if isinstance(stored, dict) else None
        if isinstance(blk, dict):
            v = blk.get("max_parallel")
            if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5:
                par = v
            v = blk.get("max_iters")
            if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 25:
                iters = v
    except Exception:  # noqa: BLE001 — unreadable settings -> defaults
        pass
    return par, iters


def _one(task: str, idx: int, total: int, sub_registry: ToolRegistry,
         chat: Callable[..., dict], budget: int | None,
         model: str | None = None, max_iters: int = SUB_MAX_ITERS,
         should_stop: Callable[[], bool] | None = None,
         run_deadline: float | None = None,
         role: str | None = None, role_name: str | None = None,
         band: str = "") -> dict:
    """Run one sub-agent turn. Never raises; every outcome is structured.
    The provider's usage block is captured from the existing chat response;
    no extra model call is made. `role` fixes the clone's system line and
    report contract; the tool set is the role's policy intersected with what
    the parent granted."""
    import time
    # No role named -> today's behavior exactly: the shared system line and
    # the task text verbatim. A role is opt-in, never a silent change.
    role_record = fleetgate.ROLE_CATALOG.get(role or "", {})
    system_line = role_record.get("system") or SYSTEM_LINE
    brief = task
    if role_record:
        brief = f"{task}\n\n{fleetgate.report_prompt(role_record)}"
    messages = [{"role": "system", "content": system_line},
                {"role": "user", "content": brief}]
    usage = {"in_tok": None, "out_tok": None, "complete": True}
    base = chat

    def counted_chat(**kw):
        doc = base(**kw)
        block = doc.get("usage") if isinstance(doc, dict) else None
        if isinstance(block, dict):
            in_value = next((block[k] for k in ("prompt_tokens", "input_tokens")
                             if type(block.get(k)) is int), None)
            out_value = next((block[k] for k in ("completion_tokens", "output_tokens")
                              if type(block.get(k)) is int), None)
            if in_value is None or out_value is None:
                usage["complete"] = False
            if in_value is not None:
                usage["in_tok"] = (usage["in_tok"] or 0) + max(0, in_value)
            if out_value is not None:
                usage["out_tok"] = (usage["out_tok"] or 0) + max(0, out_value)
        else:
            usage["complete"] = False
        return doc

    def selected_chat(**kw):
        if model:
            kw["model"] = model
        return counted_chat(**kw)

    started = time.time()
    started_mono = time.monotonic()
    try:
        res = run_turn(messages, selected_chat, sub_registry,
                       max_iters=max_iters, budget_chars=budget,
                       should_stop=should_stop, run_deadline=run_deadline)
        stop = res.stop
        body = res.text.strip()
        if stop == "final" and should_stop is not None:
            try:
                if should_stop():
                    stop = "aborted"
                    body = "[mycore: sub-agent stopped by the operator (STOP) after the active model call returned]"
            except Exception:  # noqa: BLE001 — a broken observer is not success evidence
                pass
        if stop == "final" and run_deadline is not None \
                and (time.monotonic() - started_mono) >= run_deadline:
            stop = "deadline"
            body = f"[mycore: sub-agent call returned after its {run_deadline:.1f}s deadline]"
        if not body:
            body = f"(no output; stopped: {stop})"
        elif stop != "final" and not body.startswith("[mycore:"):
            body += f"\n(stop: {stop})"
        calls = res.tool_calls
        error = ""
    except Exception as e:  # noqa: BLE001 — belt: run_turn never raises
        stop = "error"
        body = f"failed ({type(e).__name__})"
        calls = 0
        error = type(e).__name__
    if len(body) > TASK_RESULT_CAP:
        body = body[:TASK_RESULT_CAP] + f"... [+{len(body) - TASK_RESULT_CAP} chars]"
    task_digest, excerpt, lint = fleet.task_excerpt(task)
    row_model = model or "parent"
    row = {"clone": f"c{idx}", "task_digest": task_digest,
           "task_excerpt": excerpt, "lint": lint, "model": row_model,
           "role": role_name or "", "report": fleetgate.parse_report(body),
           "band": band,
           "tools": list(sub_registry.names()), "stop": stop,
           "tool_calls": calls,
           "in_tok": usage["in_tok"] if usage["complete"] else None,
           "out_tok": usage["out_tok"] if usage["complete"] else None,
           "usage_complete": usage["complete"], "ts": started,
           "ended": time.time(), "failure_class": error}
    return {"text": f"[{idx}/{total} · {stop} · {calls} tool call(s)]\n{body}",
            "row": row}


def run_tasks(tasks: list, registry: ToolRegistry, extra_tools: list | None,
              chat: Callable[..., dict] | None, budget: int | None, *,
              should_stop: Callable[[], bool] | None = None,
              run_deadline: float | None = None,
              approval_id: str = "") -> str:
    """Validate + fan out up to MAX_PARALLEL tasks in parallel, fold the
    results. Raises ToolError for caller mistakes (validation), never for a
    sub-agent's runtime outcome. A task entry is a plain string or an object
    `{"task": str, "model": str}`; a model outside the settings
    `mycore.models` whitelist (or a missing whitelist) becomes that TASK's
    honest `[i/N] error:` block — siblings still run, no silent fallback."""
    if not isinstance(tasks, list) or not tasks:
        raise ToolError("tasks must be a non-empty array of task strings "
                        "or {task, model} objects")
    max_parallel, max_iters = _limits()   # car C: owner-configurable bounds
    if len(tasks) > max_parallel:
        raise ToolError(f"at most {max_parallel} tasks per call "
                        f"(got {len(tasks)})")
    clean: list[tuple[str, str | None, str | None]] = []
    for i, t in enumerate(tasks, 1):
        model = None
        role = None
        if isinstance(t, str):
            if not t.strip():
                raise ToolError(f"task {i} must be a non-empty string")
        elif isinstance(t, dict):
            text = t.get("task")
            if not isinstance(text, str) or not text.strip():
                raise ToolError(f"task {i} needs a non-empty 'task' string")
            model = t.get("model")
            if model is not None and (not isinstance(model, str)
                                      or not model.strip()):
                raise ToolError(f"task {i}: 'model' must be a non-empty "
                                "string when present")
            role = t.get("role")
            if role is not None and (not isinstance(role, str)
                                     or not role.strip()):
                raise ToolError(f"task {i}: 'role' must be a non-empty "
                                "string when present")
            t = text
        else:
            raise ToolError(f"task {i} must be a non-empty string")
        clean.append((t.strip(), model.strip() if model else None,
                      role.strip().lower() if role else None))
    if chat is None:
        raise ToolError("no model connection bound to this turn "
                        "(delegate is only available inside a run_turn)")
    if extra_tools is not None and not isinstance(extra_tools, list):
        raise ToolError("tools must be an array of tool names")
    names = _select(registry, [t for t in (extra_tools or [])
                               if isinstance(t, str)])
    non_str = [t for t in (extra_tools or []) if not isinstance(t, str)]
    if non_str:
        raise ToolError("tools entries must be tool-name strings")
    sub_registry = registry.select(names)
    allowed = _allowed_models() if any(m for _, m, _ in clean) else None
    plan: list[tuple[int, str, str | None, str | None]] = []
    for i, (text, model, role) in enumerate(clean, 1):
        why = None
        if model:
            if not allowed:
                why = ("mycore.models is not configured in settings — "
                       f"cannot select model {model!r} for this task")
            elif model not in allowed:
                why = (f"model {model!r} is not in the mycore.models "
                       f"whitelist ({', '.join(allowed)}) — no silent "
                       "fallback")
        plan.append((i, text, model, why))
    total = len(clean)
    # The decision key + the sign-off gate both read the same settings block.
    gate = fleetgate.settings_block()
    mode = fleetgate.resolve_mode(gate)
    # D3 sign-off gate (P1b): a WRITE-capable fleet waits for the owner's
    # approval. The gate's scope is the owner's setting: `roles` (default)
    # covers the new multi-role surface, `writes` covers every write-capable
    # fleet, `off` disables it. A read-only fleet is never gated.
    granted_names = list(sub_registry.names())
    signoff = fleetgate.resolve_signoff(gate)
    named_role = any(r for _, _, r in clean)
    gate_write = (signoff == "writes") or (signoff == "roles" and named_role)
    # Resolve each role once. Approval and execution use the same registries.
    roles = [fleetgate.role_for(role) for _, _, role in clean]
    registries = [sub_registry.select(
        [t for t in granted_names if t in fleetgate.role_tools(record)]
        if role else granted_names)
        for (_, _, role), (_, record) in zip(clean, roles)]
    effective_names = {t for reg in registries for t in reg.names()}
    write_names = (effective_names & fleetplan.WRITE_TOOLS) | (
        effective_names - KNOWN_TOOL_NAMES)
    approval = str(approval_id or "").strip()
    needs_claim = bool(write_names and (gate_write or approval))
    # THE DECISION KEY (Need-B P1a): a deterministic, zero-token band over
    # the briefs. `advise` (default) only reports; `enforce` may refuse a
    # split wider than the band justifies; `off` skips the band entirely.
    band = (fleetgate.band_for([t for t, _, _ in clean]) if mode != "off"
            else {"band": "-", "ceiling": total, "score": 0, "reasons": []})
    if mode == "enforce" and total > int(band.get("ceiling") or total):
        raise ToolError(
            f"this request does not warrant {total} clones (band "
            f"{band.get('band')}, ceiling {band.get('ceiling')}: "
            f"{'; '.join(band.get('reasons') or ['narrow request'])} — "
            "do it inline or dispatch fewer briefs")
    preview = None
    if needs_claim:
        if any(why for _, _, _, why in plan):
            raise ToolError("cannot approve a fleet with an unavailable model")
        # Fail closed for handlers without a reviewable, pinned target.
        if write_names - {"write_file", "edit_file", "run_bash"}:
            raise ToolError("fleet approval cannot bind the target for: "
                            + ", ".join(sorted(write_names - {
                                "write_file", "edit_file", "run_bash"})))
        if registry.fleet_scope is None:
            raise ToolError("fleet target scope is unavailable")
        roots, state = registry.fleet_scope
        preview = {
            "version": 1,
            "scope": {"roots": roots, "state": state, "cwd": str(Path.cwd())},
            "tasks": [{"brief": task, "model": model, "requested_role": role,
                       "role": effective if role else None, "tools": reg.names()}
                      for (task, model, role), (effective, _), reg
                      in zip(clean, roles, registries)],
        }
        if "run_bash" in write_names:
            from dataclasses import asdict
            from . import terminal
            spec = _shell_spec()
            try:
                backend = terminal.parse(spec)
            except ValueError as exc:
                raise ToolError(f"cannot approve fleet shell: {exc}") from None
            preview["scope"]["shell"] = {
                **asdict(backend), "cwd": str(terminal.REPO), "sandboxed": False}
            for reg in registries:
                reg.pin_fleet_shell(spec)

    fleet_id = f"f{int(time.time())}{secrets.token_hex(2)}"
    # budget_chars is a context-compaction threshold, not a spend cap. Split
    # the parent's threshold across clones so a fan-out cannot multiply it.
    clone_budget = max(1, budget // total) if budget else None
    remaining = max(0.0, run_deadline) if run_deadline is not None else None
    launch = time.monotonic()
    rows: list[dict | None] = [None] * total

    def _run(item: tuple[int, str, str | None, str | None]) -> str:
        i, text, model, why = item
        role_name, role_record = roles[i - 1]
        role_registry = registries[i - 1]
        role_iters = fleetgate.role_max_iters(role_record, max_iters) \
            if clean[i - 1][2] else max_iters
        if why:
            row = {"clone": f"c{i}", "task_digest": fleet.digest(text),
                   "task_excerpt": "", "lint": "", "model": "unavailable",
                   "role": role_name,
                   "tools": list(sub_registry.names()), "stop": "error",
                   "tool_calls": 0, "in_tok": None, "out_tok": None,
                   "ts": time.time(), "ended": time.time(),
                   "failure_class": "model_not_allowed"}
            rows[i - 1] = row
            return f"[{i}/{total}] error: {why}"
        deadline = None
        if remaining is not None:
            deadline = remaining - (time.monotonic() - launch)
        if deadline is not None and deadline <= 0:
            task_digest, excerpt, lint = fleet.task_excerpt(text)
            rows[i - 1] = {"clone": f"c{i}", "task_digest": task_digest,
                           "task_excerpt": excerpt, "lint": lint,
                           "model": model or "parent",
                           "role": clean[i - 1][2] or "",
                           "tools": list(sub_registry.names()),
                           "stop": "deadline", "tool_calls": 0,
                           "in_tok": None, "out_tok": None,
                           "ts": time.time(), "ended": time.time(),
                           "failure_class": "run_deadline"}
            return (f"[{i}/{total} · deadline · 0 tool call(s)]\\n"
                    "[mycore: sub-agent was not started because the parent "
                    "turn deadline had elapsed]")
        result = _one(text, i, total, role_registry, chat, clone_budget, model,
                      max_iters=role_iters, should_stop=should_stop,
                      run_deadline=deadline,
                      role=role_name if clean[i - 1][2] else None,
                      role_name=role_name,
                      band=str(band.get("band", "")))
        rows[i - 1] = result["row"]
        return result["text"]

    if preview is not None:
        if ((should_stop is not None and should_stop()) or
                (remaining is not None and time.monotonic() - launch >= remaining)):
            raise ToolError("fleet was stopped or its deadline elapsed before dispatch")
        if not approval:
            proposal = fleetplan.propose(preview)
            if proposal.get("error"):
                raise ToolError(proposal["error"])
            raise ToolError(
                f"this fleet may write ({', '.join(sorted(write_names))}) "
                f"and needs your approval first — plan {proposal['id']} is waiting "
                'for you (review it in the console, then retry with approval="<plan id>")')
        if not fleetplan.consume(approval, preview):
            raise ToolError("the approval for this fleet is not valid or could not be claimed")
    if total == 1:
        parts = [_run(plan[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(max_parallel, total)) as pool:
            parts = list(pool.map(_run, plan))
    for i, row in enumerate(rows):
        if row is None:
            text = clean[i][0]
            task_digest, excerpt, lint = fleet.task_excerpt(text)
            rows[i] = {"clone": f"c{i + 1}", "task_digest": task_digest,
                       "task_excerpt": excerpt, "lint": lint,
                       "model": clean[i][1] or "parent",
                       "tools": list(sub_registry.names()), "stop": "error",
                       "tool_calls": 0, "in_tok": None, "out_tok": None,
                       "ts": time.time(), "ended": time.time(),
                       "failure_class": "worker_no_result"}
    for row in rows:
        if row is not None:
            row["clone_total"] = total
    parent_usage = registry.turn_usage()
    fleet.note_fleet({"id": fleet_id, "clones": rows, "band": band,
                     "mode": mode}, parent_usage)
    return "\n\n".join(parts)
