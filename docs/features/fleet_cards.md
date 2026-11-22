# Fleet cards — read-only approval and execution records in the Stream view

Status: shipped in **v0.5.103** (2026-11-22) as the **first slice** of the
fleet board card projection. Design authority:
[../design/prior art_gap_adoption_2026_11_21.md](../design/prior art_gap_adoption_2026_11_21.md)
§A (fleet board card slice) and the fleet design
[../design/swarm_clones_design.md](../design/swarm_clones_design.md).
Console: `web/static/app.js::renderFleet`, `renderFleetApprovals`,
`fleetCloneCard` · server: `web/server.py::collect_fleet`,
`collect_fleet_plans` · ledger: `mycore/fleet.py`, `mycore/fleetplan.py`.

**Two records, two cards, no invented link.** A clone fleet has two different
kinds of record in this product: an **approval record** (the owner allowed a
dispatch) and a **terminal run record** (what actually happened). The
dispatch data does not provide a reliable join between them — a plan id and a
fleet id are not the same thing — so the board shows them **separately** and
never guesses that a run belongs to a plan.

## The board

The Stream view (`#view-stream`) carries one FLEETS board
(`section.fleet-board`) with two independent sections:

| Section | Container | Source | What it says |
|---|---|---|---|
| **Dispatch approvals** | `#fleetApprovals` | `GET /api/fleet/plans` | permission to dispatch: plan reference, status chip, requested clone count, time |
| **Terminal execution history** | `#fleetList` | `GET /api/fleet` | what ran: fleet reference, recorded clone count, per-clone cards |

The two are read by two separate requests and are never merged into one
table. The plan list is rendered as flat cards (`fleet-task`); the execution
history is a collapsible group per fleet (`details.fleet-group`) with one card
per clone.

### The approval card

Plan reference · status chip (`awaiting approval` / `approved` / `rejected` /
`used` / `cancelled` / `Unknown`) · `Requested clones: n` · timestamp. A
status the product does not recognise renders as **Unknown** — never as the
nearest known state.

### The execution card (one per clone)

Clone reference · status chip (`Completed` / `Failed` / `Unknown`) · the
terminal reason when it is one of the known reasons (Aborted · Deadline
reached · Error · Model error · Iteration limit reached) · role · complexity
band · **task digest** (12 hex characters) · tokens in / out.

- A token pair is shown **only** when the row's usage is complete. An
  incomplete usage reads **Unknown** — the board never sums a partial count
  into a total.
- The fleet group head reads `n of m clone runs recorded` when the ledger
  shows fewer rows than the fleet expected, and `Recorded clone runs: n`
  otherwise.

## What the board does not do

- **No inferred joins.** Nothing links a plan to a run. The offline fixture
  puts the **same identifier** in both stores and the two sections still
  stay separate — the identifier is rendered once as a *Plan* row and once
  as a *Fleet* row, never as one joined row, and the history never claims to
  belong to a plan.
- **No task briefs.** The ledger's task text, task excerpt, plan description
  and tool list are **never rendered**. The digest is the reference.
- **No new writes.** The board issues **GET only**. The owner's decision
  still goes through the existing sign-off panel
  (`POST /api/fleet/plans/<id>`, Bearer `WEB_TOKEN`); the board's **REVIEW**
  button (`#fleetGoApprovals`) is a deep link that focuses that existing
  card in Settings. The browser test asserts that **no non-GET request** is
  issued from the board.
- **No new ledger, no new events, no scheduler.** This slice is a projection
  over records that already existed.

## Honest coverage

The board says what it can see:

- *Recent recorded clone results only. Turns interrupted before saving and
  results outside the read window may be missing.* — the ledger records
  **completed turns**; a turn that crashed before its reply leaves no row.
- The ledger's daily row cap (**2000 rows per day**, enforced across
  processes under a shared POSIX lock since the same release) bounds how much
  of a busy day the read window can cover.
- An unreadable ledger or plan store renders *Fleet ledger unavailable* /
  *Approval records unavailable* — an honest absence, never an empty board
  that looks healthy.

## Evidence

- **`tests/fleet_stream_browser.py`** — an offline Playwright run against the
  real console assets with every request routed to a fixture and every
  external request aborted:
  - the approval chips read `Awaiting approval, used, approved, cancelled,
    rejected, Unknown` — a legacy/unknown status stays **Unknown**;
  - the history chips read `Completed, Failed, Unknown`, the terminal reason
    *Deadline reached* is shown, the digest `cafe0000beef` is shown and the
    complete usage `0 / 17` is shown while the incomplete `99 / 99` is **not**;
  - the pending plan's id never appears in the history section and the digest
    never appears in the approvals section; the identifier that exists in
    **both** stores is rendered as a *Plan* row and a *Fleet* row — **no
    inferred join**;
  - the plan description, the task brief, the task excerpt, the raw digest
    string and the tool name appear **nowhere** on the board;
  - the board contains exactly one control (the REVIEW button) — no input,
    no link, no extra button;
  - only `GET /api/fleet` and `GET /api/fleet/plans` are requested, and
    **no write request** is ever made;
  - the layout holds at **320, 390 and 1280 px** with no horizontal overflow
    (each card fits its container), the zh pack repaints both sections, the
    REVIEW deep link focuses the existing sign-off card, and the empty and
    failing states render their honest lines.
- **Standing gates**: `python3 -m mycore.tests.run` → **2859/2859** (the
  fleet ledger's cross-process daily cap is covered by the core
  `test_fleet_daily_cap` leg) · `python3 tests/web_console_p3.py` →
  **610/610** · locale packs **1827** keys per pack.

## Honest limits

- This is the **first slice**. The wider acceptance list from the design —
  the full lifecycle (pending → approval card → approved → single dispatch),
  rejection, expiry, cancellation, failed jobs, legacy or evicted records,
  unknown spend and self-reported evidence — is **future acceptance
  criteria, not verified features of this slice**.
- Drag-and-drop scheduling, dependency enforcement, worker persistence,
  steering and resume stay outside the slice (design D7).
- A clone's **self-reported** outcome and the engine's **verified** evidence
  are different things; this board shows the ledger row, not a verification
  verdict.
