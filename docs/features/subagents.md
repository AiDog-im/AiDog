# Sub-agent delegation (`delegate`) — car 2-D6 (v0.5.23)

One `delegate` call hands 1–3 independent tasks to sub-agents that run **in
parallel threads** with an isolated context and an isolated tool set, so a
fan-out task costs the parent one tool result instead of N nested turns.

## Surfaces

- Model tool `delegate` (11th core tool, `mycore/tools.py`).
- Implementation: `mycore/subagent.py`; wiring: `loop.run_turn` binds the
  active turn's `chat` + budget into the registry (`registry.bind_turn`),
  the handler reads them (`registry.turn_binding`) — a sub-agent never holds
  credentials of its own.

## Guarantees

1. **Isolated context** — each sub-agent gets a fresh two-message transcript
   (one fixed system line + the task). The parent conversation never enters;
   the sub-agent's transcript never returns. The parent context pays only for
   the folded per-task block.
2. **Isolated tools** — default read-only set: `read_file`, `grep`, `glob`,
   `http_fetch`, `skill_route`. Write-capable tools are granted **only when
   named explicitly** in the `tools` argument (validated against the parent
   registry; an unknown name is an honest error, never a silent drop).
3. **Recursion guard** — `delegate` and `execute_code` can never be granted to
   a sub-agent (no nested delegation, no nested folding). Required also
   because `execute_code` closes over the registry it was built in, so
   re-using it under a subset would leak the parent tool set.
4. **Bounded fan-out** — at most 3 tasks per call (a 4th is an honest
   refusal), ≤12 model iterations per sub-agent, 6000 chars per task result.
5. **Failure isolation** — one sub-agent's model error becomes an honest
   `[i/N · model_error]` block; siblings still run and the fold still returns.
6. **Thread-safety** — checkpoint id allocation is serialized
   (`checkpoint._SNAP_LOCK`) and the cron tool is serialized
   (`tools._CRON_LOCK`), so parallel sub-agents writing/creating through
   granted tools cannot corrupt shared state.

## Example

```
delegate(
  tasks = ["Use read_file on mycore/loop.py and report its first line",
           "Use grep to count lines containing TurnResult in mycore/loop.py"],
  tools = []                 # read-only default; omit `tools` entirely
)
-> [1/2 · final · 1 tool call(s)]
   ...
   [2/2 · final · 1 tool call(s)]
   ...
```

## Per-task model selection (car 4a-M3)

Each `tasks` item is a plain string **or** an object `{task, model}`:

```
delegate(tasks = ["summarize this log",
                  {task: "draft the changelog entry", model: "big-model"}])
```

Rules (no silent fallback, ever):

- `model` must appear in the settings whitelist `web/settings.json`
  `mycore.models` — one source of truth, editable from the console since
  0.5.40 (Settings → Model `#mycModels`, schema-validated ≤32 single-line
  ids; `[]` = whitelist off). Unknown id → that task alone returns
  `[i/N] error: model '…' is not in the mycore.models whitelist (…)`;
  siblings still run.
- No `mycore.models` list stored → an explicit not-configured
  error block (the task does not silently inherit the default model).
- The override swaps the model **name only** on the turn's own connection —
  base URL and credentials stay the bound parent's (`bind_turn` path);
  sub-agents never read credentials.
- Malformed entries (object without a non-empty `task`, non-string `model`)
  are honest `ERROR:` refusals at validation time; the legacy string-only
  form is unchanged.

## Not in scope (honest)

- No cross-task data sharing (tasks are independent by design).
- No sub-agent → user direct delivery (results always route through the
  parent's reply).
- No persistent sub-agent sessions; every `delegate` call starts fresh.

## Fleet observability (Need-B P0, implemented in the development tree)

This increment adds an owner-visible receipt and a bounded local ledger without
changing the model-facing fan-out contract:

- `mycore/fleet.py` records one bounded JSONL row per clone under
  `<state>/fleet/YYYY-MM-DD.jsonl`; activation is explicit and the service,
  console and CLI resolve the same profile state directory.
- `GET /api/fleet` is read-only and loopback-only. Its fixed whitelist omits
  task text, chat ids, raw errors and credentials; custom model labels and tool
  names are filtered. Incomplete bounded reads are labelled.
- The console Stream view has a FLEETS section; Telegram has read-only
  `/fleet`. A system-generated receipt is appended to chat/cron/channel
  replies, and mission reports carry a fleet event subject to mission reporting
  tiers, quiet hours and throttling.
- Token fields are provider-reported input/output facts. Receipts distinguish
  parent-turn usage, clone usage and their combined total; any missing usage
  makes the corresponding total `?`, never a guessed partial total.
- Console Chat STOP uses a web-process-local cancellation event. It does not
  increment the persisted service `stop_seq` observed by TG turns. PAUSE and
  RESUME remain cross-surface persisted controls. A synchronous model request
  cannot be force-killed; cancellation is observed at loop checkpoints and a
  late return is reported as aborted rather than successful.
- Each clone receives an equal share of the parent's context-compaction
  threshold. This is a context bound, **not** a billing/token spend cap;
  daily guardrails remain the spend control.

P0 verification in the development tree: core 2718/2718, web smoke 607/607,
Stream browser probe 6/6, locale packs 1733/1733, source-language scan 0, and
publish gate READY(0). This is not yet included in a release.

## Evidence

- Whitelist carrier + console editor (0.5.40): `tmp/e2e_activation.py` `L2`
  (accept/reject matrix + the raw-file consumer `subagent._allowed_models`
  reading the same document) and `tmp/e2e_activation_ui.js` `U6`
  (`#mycModelAdd` chips add → server read-back) — see
  [frontend_coverage.md](frontend_coverage.md) §4.
