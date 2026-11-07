"""mycore.cli — one-shot runner for the dual-brain A/B harness (tests/ab_protocol.md).

    python3 -m mycore.cli "prompt"                     # body layer assembled, live model
    python3 -m mycore.cli --session /tmp/s1.jsonl ...  # persist turns (JSONL)
    python3 -m mycore.cli --no-body "..."              # raw system stack off
    MYCORE_BASE_URL=... MYCORE_MODEL=...               # endpoint overrides

Credential rule (README contract 2): the key is read from the environment
inside the process; argv carries prompts only, never secrets.
Exit codes: 0 = final answer, 1 = model/transport failure, 2 = bad arguments.
English output per repo i18n policy.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .assembly import (build_system_stack, resolve_capability_path,
                         resolve_context_dirs, stack_digest)
from .modeltier import parse_upstreams, parse_endpoints, MAX_RPM, MAX_TPM
from .loop import run_turn
from .model import ChatClient, ModelConfig, ModelError
from .session import Session
from .tools import build_registry, resolve_disabled_tools

DEFAULT_BUDGET_CHARS = 24000        # ~6k tokens of conversation payload
REPO = Path(__file__).resolve().parent.parent
from lib import profile_paths as pp  # noqa: E402  (car F: per-profile settings)


DEFAULT_RUN_DEADLINE = 1800     # L3 (owner2026-11-07): turn ceiling, seconds


def resolve_run_deadline() -> int | None:
    """L3 long-task rule (owner 2026-11-07; prior art's separate run
    deadline): the whole TURN is bounded even while single calls wait out
    long silences (L1). settings `mycore.run_deadline`: int0..86400 —
    absent -> DEFAULT_RUN_DEADLINE,0 -> None (off), invalid -> default (a
    broken knob never silently disables the bound)."""
    try:
        stored = json.loads(pp.settings_path(REPO).read_text())
        mc = stored.get("mycore") if isinstance(stored, dict) else None
        raw = mc.get("run_deadline") if isinstance(mc, dict) else None
        if isinstance(raw, int) and not isinstance(raw, bool) \
                and 0 <= raw <= 86400:
            return raw or None
    except Exception:  # noqa: BLE001 — unreadable settings keep the bound
        pass
    return DEFAULT_RUN_DEADLINE


def resolve_model_config() -> ModelConfig:
    """Endpoint precedence (owner 2026-10-18 user-configurable decision):
    env MYCORE_* > CONSOLE settings (web/settings.json mycore block) > defaults.
    Failover chain precedence (v0.5.55 gateway car, dual-track):
    env MYCORE_UPSTREAMS > settings mycore.endpoints (structured list) >
    settings mycore.upstreams (one-line spec, unchanged grammar). An invalid
    spec never raises — the chain turns off and the reason is reported through
    ModelConfig.upstreams_note (visible in describe(), i.e. never silent).
    The structured path is STRICTER about key slots (KEY_SLOT_ALLOW_RE) while
    the one-line/env path keeps its operator-level grammar — both are
    deliberate, see mycore.modeltier.parse_endpoints."""
    cfg = ModelConfig()
    raw_upstreams: str | None = None
    structured: object | None = None
    try:
        stored = json.loads(pp.settings_path(REPO).read_text())
        mc = stored.get("mycore") if isinstance(stored, dict) else None
        if isinstance(mc, dict):
            if isinstance(mc.get("base_url"), str) and mc["base_url"]:
                cfg.base_url = mc["base_url"]
            if isinstance(mc.get("model"), str) and mc["model"]:
                cfg.model = mc["model"]
            if isinstance(mc.get("endpoints"), list) and mc["endpoints"]:
                structured = mc["endpoints"]
            if isinstance(mc.get("upstreams"), str) and mc["upstreams"].strip():
                raw_upstreams = mc["upstreams"]
            # v0.5.55: candidate rotation + global rate caps. Anything not
            # exactly on the allowlist leaves the default (today's behavior).
            if mc.get("rotation") in ("ordered", "round_robin"):
                cfg.rotation = mc["rotation"]
            for key, attr, ceiling in (("max_rpm", "max_rpm", MAX_RPM),
                                       ("max_tpm", "max_tpm", MAX_TPM)):
                val = mc.get(key)
                if type(val) is int and 0 <= val <= ceiling:
                    setattr(cfg, attr, val)
            # X15 (Wave 6 delta2): opt-in per-model request param. Settings
            # mycore.reasoning_effort is the only producer; anything else
            # (absent / "" / hand-edited junk) leaves extra_body empty, so the
            # request payload stays byte-identical to the pre-X15 wire.
            eff = mc.get("reasoning_effort")
            if isinstance(eff, str) and eff in ("low", "medium", "high"):
                cfg.extra_body = {"reasoning_effort": eff}
            # backlog (owner 2026-11-02): per-request model timeout. The
            # settings mycore.timeout is the only producer; anything absent
            # or out of the 5..600 window leaves DEFAULT_TIMEOUT (the
            # pre-backlog wire behavior, byte-identical).
            tmo = mc.get("timeout")
            if type(tmo) is int and 5 <= tmo <= 600:
                cfg.timeout = float(tmo)
            # console fix car (owner 2026-11-05): per-request output cap.
            # The settings mycore.max_tokens is the only producer; anything
            # absent or out of the 128..65536 window leaves
            # ModelConfig.max_tokens (4096) byte-identical.
            mxt = mc.get("max_tokens")
            if type(mxt) is int and 128 <= mxt <= 65536:
                cfg.max_tokens = mxt
    except (OSError, ValueError):
        pass
    if os.environ.get("MYCORE_BASE_URL"):
        cfg.base_url = os.environ["MYCORE_BASE_URL"]
    if os.environ.get("MYCORE_MODEL"):
        cfg.model = os.environ["MYCORE_MODEL"]
    if os.environ.get("MYCORE_UPSTREAMS", "").strip():
        raw_upstreams = os.environ["MYCORE_UPSTREAMS"]
        structured = None              # env wins over BOTH settings tracks
    if structured is not None:
        try:
            cfg.upstreams = parse_endpoints(structured)
        except ValueError as e:
            cfg.upstreams = ()
            cfg.upstreams_note = str(e)
    elif raw_upstreams:
        try:
            cfg.upstreams = parse_upstreams(raw_upstreams)
        except ValueError as e:
            cfg.upstreams = ()
            cfg.upstreams_note = str(e)
    return cfg


def resolve_runtime_facts(model: str | None = None) -> str:
    """The `[runtime facts]` system slot (hotfix 0.5.76): product version
    from the VERSION file + the EFFECTIVE model id for this turn.

    WHY this exists: SOUL tells the model to answer "what model" from its
    runtime context only — but the stack never carried that fact, so on
    2026-11-04 the model went filesystem-diving and confidently reported a
    stale upstream config (wrong model AND a machine-absolute path in the
    reply). Facts must be injected; rules alone cannot invent them.

    `model` = the caller's already-resolved effective id (per-chat /model
    override and the decision router both run BEFORE this call on every
    production turn path, so the block names the model actually used).
    None -> resolve_model_config().model (same source as the ChatClient).
    VERSION is read at call time (never cached) so a release bump is live
    on the very next turn. A missing VERSION degrades honestly to
    "unknown" — the slot never raises and never fabricates."""
    if model is None:
        model = resolve_model_config().model
    try:
        version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = "unknown"
    return (
        "[runtime facts — answer \"what version\" / \"what model\" questions "
        "ONLY from this block; never source them from config files, file "
        "paths, logs, engine/vendor names, or older conversation messages]\n"
        f"product: I'm AiDog version {version}\n"
        f"model: {model}\n"
        "If older messages in this conversation state a different version or "
        "model, THIS block wins — they are stale. Scrub machine-absolute "
        "paths and internal names before quoting any source (constitution "
        "scrubbing rule)."
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mycore.cli", description="mycore one-shot runner")
    ap.add_argument("prompt", nargs="?", help="user prompt (one shot)")
    ap.add_argument("--max-iters", type=int, default=25)
    ap.add_argument("--system", default=None, help="extra system message text")
    ap.add_argument("--no-body", action="store_true",
                    help="skip the body layer (constitution/memory assembly)")
    ap.add_argument("--session", default=None,
                    help="JSONL session file: load history, append this turn")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET_CHARS,
                    help="compaction budget in chars (0 = never compact)")
    ap.add_argument("--quiet", action="store_true", help="print only the final answer")
    args = ap.parse_args(argv)
    prompt = (args.prompt or "").strip() or sys.stdin.read().strip()
    if not prompt:
        print("usage: python3 -m mycore.cli \"prompt\"", file=sys.stderr)
        return 2

    cfg = resolve_model_config()
    client = ChatClient(cfg)
    # v0.5.55: one-shot runs meter too (same state dir as the service; a
    # never-activated meter would silently drop these rows).
    from . import meter as _meter
    _meter.activate()
    # D2: the daily usage ledger activates too (one-shot runs contribute
    # their meter rows to the day's anonymous report).
    from . import ledger as _ledger
    _ledger.activate()

    # ---- message stack: persisted history + body layer + extra system + user ----
    sess: Session | None = None
    messages: list[dict] = []
    if args.session:
        sess = Session(args.session)
        messages = sess.load()
        if not messages and not sess.path.exists():
            sess = Session.create(sess.path, meta={"model": cfg.model})
        elif sess.corrupt and not args.quiet:
            print(f"[mycore] session: skipped {sess.corrupt} corrupt line(s)",
                  file=sys.stderr)

    body: list[dict] = []
    if not args.no_body:
        body = build_system_stack(context_dirs=resolve_context_dirs(),
                                   capability_path=resolve_capability_path(),
                                   runtime_facts=resolve_runtime_facts(cfg.model))
    system_msgs = body + ([{"role": "system", "content": args.system}]
                          if args.system else [])
    # Session files never persist system messages (the body stack is rebuilt
    # fresh each run — constitution/memory must reflect the repo, not a stale
    # snapshot). Strip any system lines a legacy file may carry, then prepend.
    history = [m for m in messages if m.get("role") != "system"]
    messages = system_msgs + history
    persist_from = len(messages)          # user prompt must persist with the turn
    messages.append({"role": "user", "content": prompt})

    if not args.quiet:
        print(f"[mycore] {cfg.describe()}", file=sys.stderr)
        print(f"[mycore] body_stack={stack_digest(system_msgs)} "
              f"budget={args.budget or 'off'} session={args.session or 'none'}",
              file=sys.stderr)

    def on_event(kind: str, data: dict) -> None:
        if args.quiet:
            return
        if kind == "tool_call":
            print(f"[tool] {data['name']} {str(data['args'])[:120]}", file=sys.stderr)
        elif kind == "compact":
            print(f"[compact] folded {data['folded']}: {data['from']} -> {data['to']}",
                  file=sys.stderr)
        elif kind == "stop":
            print(f"[stop] {data['reason']} after {data['iterations']} iter(s)",
                  file=sys.stderr)

    start_len = persist_from
    try:
        result = run_turn(messages, client.chat,
                          build_registry(disabled=resolve_disabled_tools()),
                          max_iters=args.max_iters, on_event=on_event,
                          budget_chars=args.budget or None)
    except ModelError as e:
        print(f"model error: {e}", file=sys.stderr)
        return 1

    if sess is not None:
        sess.append_many(messages[start_len:])   # persist this turn's delta
        if not args.quiet:
            print(f"[mycore] session appended -> {sess.path} "
                  f"({sess.size()} bytes total)", file=sys.stderr)

    print(result.text)
    return 0 if result.stop == "final" else 1


if __name__ == "__main__":
    sys.exit(main())
