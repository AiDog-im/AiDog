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

## The decision key and the role catalog (Need-B P1a)

Two engine-side pieces, both in `mycore/fleetgate.py`. Neither adds a
user-visible string, and neither spends a token.

**The decision key** — a deterministic complexity band over the briefs a
parent wrote: parallel briefs, brief size, distinct paths, cross-cutting
verbs, enumerated items and distinct topics. The band (S/M/L) yields a
fan-out ceiling and a short human-readable reason list. It is a heuristic
band, never a proof.

`mycore.fleet.auto_split` decides what the band may do:

| Mode | Behavior |
|---|---|
| `off` | the band is not computed at all |
| `advise` (default) | the band is computed and recorded; **never** a refusal |
| `enforce` | a fan-out wider than the band's ceiling is refused, with the reason |

An unknown mode degrades to `advise` (never to `enforce`); a bad value is
refused at settings-write time.

**The role catalog** — `scout`, `analyst`, `implementer`, `verifier`. A role
fixes a clone's tool policy, its iteration bound and its report contract, and
asks for a fixed report block (`status`, `summary`, `findings`, `handles`,
`blocker`) so the parent folds reports instead of transcripts. A clone that
ignores the schema is recorded honestly as `free-form`, never as a failed
task.

A role is **opt-in**: a task without `role` keeps exactly today's behavior
(the granted tools, the shared system line, the brief verbatim). A role may
narrow the granted tools, never widen them, and never grant `delegate` or
`execute_code`.

```json
delegate(tasks = [
  {"task": "list every call site of the parser", "role": "scout"},
  {"task": "run the parser test suite and report the exit status",
   "role": "verifier"}])
```

The band, the roles and the parsed report are recorded in the local fleet
ledger. The surfaces that SHOW them (the receipt, the board) and the
sign-off gate before a write-capable fleet are the next increment (P1b).

## The sign-off gate (Need-B P1b)

A fleet whose clones may WRITE waits for the owner. `mycore.fleetplan.py`
holds the proposal (the same shape as an exchange order plan: approve /
reject / cancel — it authorizes ONE dispatch and carries no settings patch).

`mycore.fleet.require_signoff` decides the gate's scope:

| Mode | Behavior |
|---|---|
| `off` | no fleet ever waits |
| `roles` (default) | a write-capable fleet that NAMED a role waits for the owner |
| `writes` | every write-capable fleet waits, including the plain delegate path |

The default keeps the shipped delegate contract (granting `write_file` to a
clone has worked since 0.5.x); widen it with one setting. The gate reads the
tools the fleet would ACTUALLY use (a role may narrow them), so a read-only
role is never gated. A refusal names the plan id; once the owner approves it
in the console, the model re-dispatches with `approval="<plan id>"`. One
approval authorizes ONE dispatch and is then CONSUMED.

## The fleet board and the sign-off queue (console)

- The Stream view's FLEETS card lists recent fleets with each clone's
  status, its **role** and the decision key's **band** as chips, and the
  exact token facts (`in/out`, or `?` when the provider reported none).
- Settings carries a "Clone fleet approvals" card (separate from "Agent
  plans" on purpose: a fleet plan authorizes one dispatch, a settings plan
  carries a patch) with APPROVE / REJECT, each Bearer-gated.

## Evidence

- Whitelist carrier + console editor (0.5.40): `tmp/e2e_activation.py` `L2`
  (accept/reject matrix + the raw-file consumer `subagent._allowed_models`
  reading the same document) and `tmp/e2e_activation_ui.js` `U6`
  (`#mycModelAdd` chips add → server read-back) — see
  [frontend_coverage.md](frontend_coverage.md) §4.
