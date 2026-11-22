# Fleet daily row cap — one storage bound across every process

Status: shipped in **v0.5.103** (2026-11-22). Module: `mycore/fleet.py`
(stdlib only). Companion page: [fleet_cards.md](fleet_cards.md) (the board
that reads the rows this cap bounds). Design authority:
[../design/swarm_clones_design.md](../design/swarm_clones_design.md) §4.2
(the ledger contract) and §11 (the cap-accuracy limitation this car closed).

**What it does.** The clone ledger writes one JSONL row per clone under
`<state>/fleet/`. Its daily row cap is the bound that keeps that file
finite: at most **2000 clone rows per UTC day**. Before this car the count
lived in a **process-local dict**, so two processes could each believe they
had room and the day file could grow past the cap. The cap is now taken
under a **shared POSIX lock** and counted **from the on-disk file**, so every
writer that takes the lock reads the same number, and none of them can push
the day past it.

## The contract

| Item | Value |
|---|---|
| Cap | `MAX_ROWS_PER_DAY = 2000` clone rows per UTC day — a **module constant**, not a setting; there is no console control for it |
| Day file | `<state>/fleet/<YYYY-MM-DD>.jsonl`; the day is **UTC** (`datetime.now(timezone.utc)`). A row may name its own day — the string must match `YYYY-MM-DD` or the row is dropped |
| Lock file | `<state>/fleet/.lock` — one file, one inode, shared by every writer. It is never unlinked, so every process locks the same inode |
| Lock | `fcntl.flock(LOCK_EX | LOCK_NB)`, retried every 5 ms for at most **0.25 s** (`_LOCK_WAIT`) |
| Regular-file check | the lock handle must be a regular file (`S_ISREG`); it is opened `O_NONBLOCK | O_NOFOLLOW`, so a symlink or a directory is refused |
| Count source | the **on-disk** JSONL, read under the lock: the physical newline count of the day file (damaged rows included) |
| Size ceiling | `MAX_ROWS_PER_DAY * (4 * MAX_ROW_CHARS + 2)` = **16,388,000 bytes** per day file. A legacy file above it is **refused**, never trimmed |
| Row cap | every serialized row ≤ `MAX_ROW_CHARS = 2048` characters. A longer row is **dropped**, not truncated |
| Retention | day files older than **30 days** are pruned on activation, under the same lock |

### Who writes

The console (`web/server.py`), the chat service (`mycore/service.py`) and
the CLI (`mycore/cli.py`) each call `fleet.activate()` at startup and each
resolves the same state root (`AIDOG_MC_STATE` > `<profile>/state` >
`<repo>/data/mycore_service`) — so all three land in the same
`<state>/fleet/` and contend on the same `.lock` inode.

### Fail-closed, every time

`record_clone` returns `False` — the row is dropped and **the turn is never
failed** — when:

- the ledger is not activated (it is inert by default);
- the lock cannot be taken within 0.25 s (another writer holds it);
- `fcntl` is unavailable (no POSIX locking backend);
- the lock file is not a regular file;
- the lock call fails with a permanent error;
- the day file cannot be opened or read;
- the day file is already at (or above) the cap, or is over the size
  ceiling;
- the row's own day string is malformed.

A dropped row is **silent by design**: the ledger never raises, and the
turn's receipt still counts the clones it dispatched.

## What the cap is not

- **Not a spend bound.** The day-level guardrails (`mycore/leash.py`) remain
  the spend control. This is a **storage** bound on one file.
- **Not a per-turn fleet-size bound.** A turn may dispatch more clones than
  the day has room for; the surplus rows are dropped and the receipt still
  reports the dispatch.
- **Not a retention policy.** The 30-day prune removes whole day files.

## What is stored, and what the board shows

Each row is one clone's terminal record: the clone id, the fleet id, the
origin (chat / cron / mission / tg), the stop reason, the tool-call count,
the token counts, the model, the role, the complexity band, the tool names,
the task digest and the parsed report.

- **The task text is never stored raw.** `fleet.task_excerpt()` returns a
  **12-hex digest** of the full task plus an excerpt of at most **200**
  characters, with machine paths masked to `<path>`. A lint hit (a secret
  assignment, a secret shape, a private key, a bearer token, a bot handle)
  stores **no excerpt at all** — only the digest — and a lint that cannot
  run fails closed the same way.
- **Every row is bounded at 2048 characters as a whole**, so no single field
  can grow without bound.
- The stored report is the clone's **own** output (its fixed report block),
  never the owner's task text.

The console board (`GET /api/fleet`) reads a **bounded public projection**,
never a raw row: `fleet.public_clone()` whitelists the fields, filters tool
names through a read-only allow-list, constrains the model label and the
role/band strings, and carries the digest instead of any text. The read is
bounded too (≤ 400 rows, ≤ 256 KiB per day file, ≤ 40 fleets). The task
excerpt, the report and the raw failure text never reach the board — see
[fleet_cards.md](fleet_cards.md) for the card contract.

## Evidence

- **`mycore/tests/run.py::test_fleet_daily_cap`** — 9 checks; the core suite
  is green at the **v0.5.103 released baseline: 2859/2859**.
  - **The cross-process measurement.** Six independent interpreters are
    started against one pre-existing day file that already holds 1990 rows;
    each makes 12 attempts (**72 in total**) after a shared start barrier.
    The day file ends at **exactly 2000 physical rows**, exactly **10** rows
    are accepted, and every accepted row is one the writer reported.
    Measured on **WSL**.
  - another day keeps its own independent room;
  - a lock held by another process **drops** the row (the bounded wait
    expires) and writing resumes as soon as the holder releases;
  - a missing locking backend, a permanent lock error and an unreadable day
    file each **fail closed** — the file is byte-identical afterwards;
  - a torn (unterminated) tail consumes one slot and stays a separate row;
  - a legacy file over the size ceiling is refused.
- **Standing gates**: `python3 -m mycore.tests.run` → **2859/2859** ·
  `python3 tests/web_console_p3.py` → **610/610** · locale packs **1827**
  keys per pack.

## Honest limits

- **The cap only binds writers that take the lock.** Every writer in the
  fleet must be running the v0.5.103 ledger or later. An **older process**
  still running a pre-0.5.103 `mycore` counts rows in its own memory and
  appends without the lock, so an installation with a mixed fleet can still
  push a day file past 2000 rows. This is a **version-skew** hole, not a
  lock hole: for every writer that uses the lock, the guarantee holds.
- **Native Windows has no `fcntl`, so it writes nothing.** A native Windows
  Python without `fcntl` fails the lock closed and every ledger write is
  dropped — the turn still succeeds and the receipt still reports the
  clones. The Windows desktop client attaches to the WSL core, which does
  have `fcntl`; a native Windows *core* is a no-write ledger, not a capped
  one.
- **macOS is untested.** The lock path is written for POSIX (Linux/WSL and
  macOS), but the six-writer measurement was taken on **WSL only**; macOS is
  not verified.
- **A refused row is invisible.** The cap drops rows silently by design, so
  the board can show fewer clones than the turn dispatched while the
  receipt still counts them.
- **The cap is per UTC day file.** A row may name its own day, so a row
  filed under another day counts against that day, not against today.
- The design record's §11 still carries the pre-v0.5.103 note that
  cross-process writes could race a process-local count. This car replaced
  the process-local count with the shared lock and the on-disk count
  described above.
