# Feature spec — Natural-language cron (task 2 / Phase B2)

Status: **implemented** (2026-10-20, ships in v0.5.20). Design anchor:
`docs/design/im_aidog_product_design.md` line 27 (prior art checklist —
"natural-language cron") and the task-2 roadmap slice owner-approved 2026-10-20.

**Honest boundary:** this is a scheduler, not a semantics engine. Plain
speech is translated by the **model** into one canonical spec string (the
`cron` tool's description teaches the grammar); everything after that
translation — validation, persistence, due math, delivery — is deterministic,
offline-tested code in `mycore/scheduler.py`. No speech → no job; a job
created without a configured target still fires and is logged
(`delivered=none`), never an error.

## 1. One spec grammar (`mycore/scheduler.py`)

Every entry point — `/cron`, the model `cron` tool, `python3 -m
mycore.scheduler` — feeds the same parser:

| Spec | Meaning | Caps |
|---|---|---|
| `every <N>m` / `every <N>h` | interval | 1..10080 minutes |
| `daily HH:MM` | every day | Beijing time |
| `weekly <mon..sun> HH:MM` | every week on that weekday | Beijing time |
| `once YYYY-MM-DD HH:MM` | one-shot | future only — a past time is refused at create |

All times are **Beijing fixed UTC+08:00** (owner timezone, no DST — the
evolution loop's convention). Anything else is a `SpecError` with an
operator-facing hint and no machine paths.

## 2. Jobs (registry: `<state>/cron_jobs.json`)

State dir: env `AIDOG_MC_STATE`, else `<repo>/data/mycore_service` — the
same gitignored directory as the chat-service state. Atomic writes
(tmp + `os.replace`); a corrupt file or garbage entries load as empty/partial
(fail-soft — a bad line never takes the scheduler down).

| Field | Meaning |
|---|---|
| `id` | 6 hex chars, unique in the table |
| `type` + schedule fields | the parsed spec (`minutes`/`time`/`weekday`/`at`) |
| `text` | ≤ 4000 chars — the reminder text or the prompt |
| `kind` | `message` = delivered verbatim · `prompt` = runs **one** fresh model turn (constitution stack + tools, **no session persistence**) and the reply is delivered |
| `origin` | chat that created it (delivery target when present) |
| `source` | `command` (·`/cron`) / `model` (·`cron` tool) / `ops` (·CLI) |
| `created_at` / `last_fired` / `done` | due math + one-shot bookkeeping |
| `paused` | **car C1** — bool; `is_due` never fires it (a manual run still executes; absent = running) |
| `catchup` | **car C1** — `"off"` skips windows stale beyond `CATCHUP_GRACE_SEC` (300 s); absent/`"on"` = the fire-late-once default above |
| `run_requested_at` | **car C1** — epoch of a queued manual run; cleared by the fire leg after executing (or kept while a prompt run is gate-held) |
| `missed` | **car C1** — set when catch-up off skipped a one-shot (displayed `(missed)`, never `(done)`) |

Caps: **MAX_JOBS = 50** · text 4000 · interval 10080 min. Exceeding any cap
is a refusal, not a truncation.

## 3. Due math (catch-up, at most once per window, no fire bursts)

| Type | Due when | After firing |
|---|---|---|
| interval | `now >= (last_fired or created_at) + N*60` | anchor = actual fire time → downtime yields **one** late fire, never a burst |
| daily / weekly | `now >=` the latest occurrence ≤ now **and** `max(last_fired, created_at)` is older than it | `last_fired = now` → silent for the rest of the window |
| once | `now >= at` and not done | `done = true` (stays listed until `/cron del`) |

One rule covers the awkward cases: created after today's time (waits for
tomorrow), created after this week's weekday (waits a week), service down
across a window (one catch-up fire on the next opportunity — the evolution
cron leg's philosophy, applied per job).

The service leg (`_maybe_fire_cron`, same 60 s cadence as the evolution leg)
**persists the mark before executing** — a crash mid-fire can lose one
delivery but never duplicates one.

## 4. Surfaces

- **Telegram** (allowlist-gated like every command):
  - `/cron` — list (`id · spec · kind · next <BJ> (+Δ) · text[:40]`, `(done)`
    / `(missed)` / ` (paused)` markers). The relative countdown (car 4a-M3) is a language-neutral suffix
    appended after the absolute time — `(+2h13m)`, `(+45s)`, `(+3d4h)`,
    two units max, largest first; a job already due renders `(+0s)` (the
    scheduler fires it at the next tick, so the floor is zero, never
    negative) and a done/no-next job renders no countdown. Format lives in
    `scheduler.fmt_delta` — deliberately **locale-free** (unit letters only,
    no `cmd.cron.*` keys added); the absolute Beijing time stays primary.
  - `/cron add [message|prompt] <spec> :: <text>` — create; stamps this chat
    as the job's origin
  - `/cron del <id>` — remove
  - **Car C1 (A-3) controls** — same registry helpers as every other entry:
    - `/cron pause <id>` / `/cron resume <id>` — stop/restore automatic
      fires (a paused job never becomes due; `next` still displays)
    - `/cron run <id>` — manual run: sets the queued flag and nudges the
      fire leg **in-process** (same process as the poll loop), so the run
      executes immediately through ONE shared path — it can never
      double-fire against the scheduler. The window is consumed only when
      it had already arrived, so an early run never eats a future one-shot
      (upstream: *manual run does not consume one-shot*). Replies honestly:
      `ran now` / held (pause or budget) / registry error.
    - `/cron catchup <id> on|off` — missed-window policy per job (see §3a)
  - **Natural path**: just talk — *"remind me every day at 8"* → the model
    calls the `cron` tool with `daily 08:00`; the turn's origin chat is
    recorded automatically (service pins it around every pipeline turn).
- **Model tool** `cron` (`action: create|list|delete|pause|resume|run|catchup`,
  9th core tool): shared grammar + registry with `/cron`, `source=model`;
  `catchup` takes its `on|off` value in the `spec` field. `run` only queues
  the flag — the service fires it within 60 s through the same path.
- **Console chat** gets the same tool (origin = none → falls through to
  `MYCORE_WD_CHAT` / log-only).
- **Console**: Overview `#cronList` renders the registry; since 0.5.40 each
  row carries `#cronDelBtn` — confirm → `POST /api/cron
  {action:"del", id}` → the list refreshes and the row disappears, mirroring
  the CLI `--del` semantics exactly (unknown id → 404, nothing written when
  nothing matched). Since 0.5.50 the Console also has a create form → `POST
  /api/cron {action:"create", name, schedule, prompt}` — the SAME shared
  registry as the natural-language/CLI/Telegram paths, which remain unchanged
  (the form is an addition, not a replacement). **Car C1 (A-3)**: each row
  now carries pause/resume · run · catch-up buttons → `POST /api/cron
  {action:"pause"|"resume"|"run", id}` and `{action:"catchup", id,
  value:"on"|"off"}` — the same scheduler helpers, `200` + the updated
  `cron_row` (unknown id → 404, bad value → 400). `run` answers
  `queued:true` because the **service** process fires it at its next 60 s
  tick (this server never delivers to a chat itself) — the UI note says
  "within a minute" instead of pretending instant.
- **CLI/ops**: `python3 -m mycore.scheduler [--list | --add SPEC --text … |
  --del ID | --pause ID | --resume ID | --run ID | --catchup ID on|off]
  [--json]` (exit 0/1/2, evolution parity) — also how the E2E probe job is
  injected.

## 3a. Catch-up policy (per job, car C1 A-3)

Default (`catchup` absent = **on**): a window missed while the service was
 down fires **once, late**, on the next opportunity — the behavior §3
 describes. With **off**, a window that expired more than
`CATCHUP_GRACE_SEC = 300 s` ago is **skipped, never fired late**:

| Type | What catch-up off does |
|---|---|
| interval | `is_due` refuses the stale window and `catchup_sweep()` advances the anchor past every expired window in O(1) — without it the job would either never fire again (the grace check) or burst every missed window at once |
| once | a missed one-shot becomes `done` + `missed` (shown `(missed)` — it never fires) |
| daily / weekly | no mutation: the occurrence moves forward by itself and the grace check skips today's stale window; tomorrow fires normally |

The 300 s grace (5× the 60 s fire cadence) means normal tick latency always
fires — only real downtime or a long hold trips the skip. The sweep runs at
the top of the service fire leg, before due math, so a display and a fire can
never disagree about a stale window.

**No in-flight cancel (honest boundary):** a fire is one synchronous
delivery or one model turn inside the poll loop — there is no long-running
job process to stop (upstream's `cron cancel` has nothing to cancel here).
`paused` holds the NEXT fire; an executing fire completes, bounded by the
turn's own timeout. Never pretended otherwise.

## 5. Delivery on fire

```
job.origin (the chat that asked)  ──►  service._send
        else env MYCORE_WD_CHAT (int) ──►  service._send
        else                          ──►  log only (delivered=none)
```

`MYCORE_WD_CHAT` is the operator-provisioned slot the watchdog alerts use
(integer chat id; garbage → log-only, fail-soft). Every fire writes one
`CRON_FIRE id=… kind=… type=… delivered=… preview=…` line to `service.log`
(preview path-scrubbed, ≤ 80 chars — same redaction class as `MODEL_ERR`).
`prompt` jobs map model stops to the same honest chat lines as regular
turns (`final` → reply, `max_iters`/`model_error` → the localized short
line, detail to the log only). Dry-run evaluates + marks + logs, never sends.

## 6. Gates & tests

- `python3 -m mycore.tests.run` → `test_scheduler()`: spec grammar (4 forms
  + 16 refusals), the due matrix (daily/weekly/interval/once incl. created-
  late / downtime catch-up / no-burst), registry round-trip + fail-soft, caps,
  the `cron` tool paths, `/cron` command paths, delivery fallbacks
  (origin → wd → none), prompt execution, dry-run, throttle, and the
  speech → tool → job end-to-end with origin stamping; registry count 9.
- `python3 tests/source_lang_check.py` → locale parity for the `cmd.cron.*`
  keys (packs 194 → 204).
*Development-repository only: `deploy/check_publish.py` is release tooling of
the development checkout, never part of a user install.*

- `python3 deploy/check_publish.py` → `data/` is runtime state (gitignored),
  nothing new tracked; leak scan unchanged.
- **Console coverage (frontend car, 0.5.39)**: the registry became READABLE
  from the console — Overview `#cronList` renders `GET /api/cron`
  (id/spec/text/origin/next/due, fixed field whitelist so no absolute path
  can enter the payload; absent/corrupt file → `count: 0`). Creation still
  defaults to the natural-language/CLI/Telegram paths — the console create
  form (0.5.50) writes through that same registry, so it is an addition,
  not a replacement.
  Evidence: `tmp/e2e_frontend_coverage.py` `L6` (seed → count/fields → CLI
  delete) and `tmp/e2e_frontend_ui.js` `U5` (list renders).
- **Delete button (activation car, 0.5.40)**: per-row `#cronDelBtn` + the
  `cron.createHint` line. Evidence: `tmp/e2e_activation.py` `L7` (seed →
  console delete → count 0 → second delete 404 → `add` refused 400 → 401
  without a bearer) and `tmp/e2e_activation_ui.js` `U5` (confirm → row
  disappears → server count 0) — see [frontend_coverage.md](frontend_coverage.md)
  §4.
