# Command reference

Two command surfaces exist:

- **Telegram commands** — sent to your bot from an allowlisted chat. Commands
  are parsed only when the message text starts with `/`; messages from
  accounts outside the allowlist are ignored like any other message.
- **Local CLI** — run on the machine that hosts the service.

Send `/help` in Telegram to get the same list, one line per command, from the
bot itself.

## Telegram commands

### `/help`

- **Purpose:** list every command with one-line descriptions.
- **Example:** `/help`

### `/reset`

- **Purpose:** archive this chat's session file (rotated into
  `sessions/archive/`) and start a fresh session.
- **Example:** `/reset`

### `/rewind [n]` · `/rewind` (as a reply)

- **Purpose:** message-level session rewind (car C1, A-2). `/rewind <n>`
  drops the last **n visible (user/assistant)** messages — tool rows are not
  counted, since the chat never saw them. `/rewind` sent **as a reply** to a
  message drops that message and everything after it (matched from the end
  by content), so you can re-ask from that point.
- **Safety:** the pre-rewind transcript is rotated into
  `sessions/archive/` first (crash-safe: prefix → temp file → rotate →
  atomic rename); a rewind that would empty the chat is refused — use
  `/reset` for that.
- **Example:** `/rewind 3` · reply to an old message with `/rewind`

### `/model [id]`

- **Purpose:** with no argument, show the current model id and endpoint host
  (never the key). With an argument, set a **per-chat** model override.
- **Allowed ids:** the ids in `web/settings.json` `mycore.models` when that
  key is present, otherwise only the currently configured id — anything else
  is rejected with the allowed list.
- **Examples:**
  - `/model` — show what this chat is using
  - `/model <id>` — switch this chat to an allowed id

### `/status`

- **Purpose:** one line of session state — turn count, current model,
  service uptime.
- **Example:** `/status` → `turns=12 model=… uptime=3d 04:11`

### `/lang [code]`

- **Purpose:** with no argument, show the reply language and the available
  packs. With a code, switch the language **service-wide** and persist it to
  `<state>/lang.json`. Packs are discovered in `overlay/locales/`, default
  `en`.
- **Examples:**
  - `/lang` — show current language + available codes
  - `/lang zh` — switch to Chinese

### `/learn memory <text>` · `/learn skill <slug> :: <desc> :: <body>`

- **Purpose:** record one experience as an institutional artifact (the
  self-evolution loop). **The lint gate and the `skill_route` pre-screen run
  BEFORE any write**; a rejection changes nothing and is logged to
  `service.log` only. Slug charset: `a-z0-9._-`.
- **Examples:**
  - `/learn memory Ships the daily report at 09:00 — ask for it after breakfast.`
  - `/learn skill ship-report :: Ships the daily report at 09:00 :: Fetch the metrics, format them as three bullets, and send them to the owner chat.`

### `/reflect [daily|weekly|monthly]`

- **Purpose:** generate the period reflect skeleton — facts only (skills,
  entries, counts); the **Human conclusion** section stays the owner's to
  write. Defaults to the current period when omitted.
- **Examples:**
  - `/reflect`
  - `/reflect weekly`
  - `/reflect monthly`

### `/evolution`

- **Purpose:** self-evolution loop status — schedule (daily / weekly /
  monthly), whether a period is due, last runs, artifact counts and the log
  tail.
- **Example:** `/evolution`

### `/cron [add [message|prompt] <spec> :: <text> | del <id> | pause <id> | resume <id> | run <id> | catchup <id> on|off]`

- **Purpose:** natural-language cron job registry (fail-closed until
  enabled). No argument lists the jobs (id · spec · kind · next fire · text
  preview).
- **Specs** (Beijing time): `every 30m` · `daily 08:00` ·
  `weekly mon 08:00` · `once YYYY-MM-DD HH:MM`
- **Kinds:** `message` = the text is delivered as-is · `prompt` = one model
  turn runs and its result is delivered.
- **Per-job controls (0.5.84, same grammar on every entry — the model
  `cron` tool, the ops CLI and the console buttons): `pause <id>` /
  `resume <id>` stop and restore automatic fires (a manual run still works);
  `run <id>` executes it now through the scheduler's own fire path (the
  schedule is never consumed early — an early run does not eat a future
  one-shot); `catchup <id> on|off` sets the missed-window policy — `off`
  skips a window stale beyond five minutes instead of firing it late.
- **Delivery on fire:** the origin chat that created the job, else
  `MYCORE_WD_CHAT`, else log-only — never an error; the fire still runs and
  is recorded as `CRON_FIRE` in `service.log`.
- **Examples:**
  - `/cron` — list jobs
  - `/cron add daily 08:00 :: drink water`
  - `/cron add message every 30m :: check the build`
  - `/cron add prompt daily 21:00 :: summarize today's session for me`
  - `/cron add weekly mon 08:00 :: send the weekly digest`
  - `/cron add once 2026-11-11 09:00 :: remind me of the release`
  - `/cron del 3` — remove a job by id

### `/checkpoint [restore <id>]`

- **Purpose:** `write_file` / `edit_file` snapshot the previous file before
  every write. No argument lists the recent checkpoints (newest first);
  `restore <id>` puts that file back. `run_bash` changes are **not**
  covered.
- **Examples:**
  - `/checkpoint` — list recent checkpoints
  - `/checkpoint restore 42` — put checkpoint #42 back

### `/version`

- **Purpose:** installed vs latest release — honest `unknown` when the
  release repository cannot be reached. Once per UTC day the loop also
  stages a one-shot "new release" notice for your next message.
- **Example:** `/version`

## Local CLI

| Command | What it does |
|---|---|
| `bash deploy/install.sh` | One command: provision + boot + verify. Exit `0` ready · `2` credential/model slots still pending (fail-closed) · `1` hard failure. Idempotent — safe to re-run. |
| `aidog setup` | Guided wizard that prompts only for slots still empty, hides input, validates every write, never echoes a value. Flags: `--check` (read-only report) · `--yes` (non-interactive) · `--no-boot` (provision without starting services). |
| `aidog doctor` | Health gate — the checks every release runs green on. |
| `aidog update apply` | Upgrade: dirty-tree guard → fetch → fast-forward merge → service restart (forwards to `deploy/aidog_update.py`). Use `aidog update` for a read-only version-channel query. |

Full install guide: [`docs/installation.md`](installation.md).

---

**37 capability documents** — one honest doc per feature (what it does and
where its boundaries are):
https://github.com/AiDog-im/AiDog/tree/main/docs/features
