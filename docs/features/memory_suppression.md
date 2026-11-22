# Memory suppression, stage 1 — the tombstone ledger

Status: shipped in **v0.5.103** (2026-11-22) — **stage 1 only**: the storage
and restore contract. Design authority:
[../design/memory_suppression_design.md](../design/memory_suppression_design.md)
(D1–D12 and the stage-1 decision table). Module: `mycore/memforget.py`
(stdlib only). Companion page:
[memory_and_routing.md](memory_and_routing.md) (the dual-layer memory and the
skill router).

**A suppression, not a deletion.** The authoritative memory files are
append-only and human-reviewed, so "forget this source" is recorded as a
**tombstone** and the read paths filter it out. Nothing is deleted; the
original transcript, the copies that already left the machine, the public
repository history and the backups taken **before** the suppression stay
exactly as they were — and every report says so.

**No user-facing control ships yet.** Stage 1 is the ledger and the three
filtering points. A forget is recorded through the module's CLI; the console
has no button for it, and that is deliberate — the contract must be
demonstrably durable across every supported read path before the owner is
offered a switch.

## The ledger

| Item | Value |
|---|---|
| Location | `<state>/memforget/tombstones.jsonl` — the **per-profile** state root, so a suppression does not leak across profiles |
| Row | one JSON object per line: `id`, `channel`, `scope`, `digest`, `ts` |
| Row contents | **no content, no path, no credential** — a ledger row cannot be read back into the source it silences |
| Cap | 20000 rows; a full ledger refuses the write rather than silently dropping one |
| Write lock | the same POSIX `flock` mutation lock the pipeline and fleet ledgers use — a bounded wait (0.25 s) and a **fail-closed** result when `fcntl` is unavailable |

## Source identity

A source id is `src_<32 hex>` = `sha256(profile scope ‖ admission channel ‖
content digest)`.

- **Opaque** — it carries no path, no excerpt and no credential.
- **Deterministic** — re-admitting the same content later maps to the same
  id, so a forgotten source stays forgotten instead of returning under a new
  identity. The identity form folds whitespace, blank lines and list markers,
  so it is a fixed point under the learned-memory writer (which re-renders an
  entry with a `- ` prefix and a two-space indent).
- **Per store** — the admission channel is part of the id, so the same text
  in the learned store and in a curated slot are **two different sources**.
  Forgetting the learned entry does not silence the curated copy; the owner
  forgets each store that holds the content.

| Channel | Store |
|---|---|
| `learned` | `EVOLUTION_learned.md` entries |
| `curated:memory` | `MEMORY_curated.md` |
| `curated:user` | `USER_curated.md` |
| `curated:other` | any other curated file |

| Scope | Which surfaces the tombstone silences |
|---|---|
| `memory` | the learned store plus the derived stores |
| `curated-entry` | one curated entry, everywhere it shows |

A curated entry may carry an explicit marker on the line under its heading —
`<!-- aidog-src: src_… -->` — which **pins the id across edits**. Without
the marker, editing an entry changes its id and the entry returns. The
marker line is never shown to the reader.

## The three filtering points

| Point | What happens |
|---|---|
| **Admission** | a new item whose source id is tombstoned is **refused**, and the refusal is reported — never a silent drop. `mycore/evolution.py` gains the `source-suppressed` rule; the console learn route answers **409 `source suppressed`** with a reason that says *this source is suppressed*, explicitly **not** a lint failure |
| **Read** | prompt injection (`mycore/assembly.py::_read_memory_slot`), the console memory view (`mycore/memoryview.py`) and the owner profile (`mycore/profiling.py`) skip the tombstoned items. The filter runs **before** the slot cap, so a suppressed entry is dropped even when it sits past the cap |
| **Restore** | a restored memory slot is filtered through the suppression set **before** it is written |

Derived stores are **filtered, never edited**: the profiling profile and the
session search index are rebuilt from the authoritative files, and the
suppression is decided at admission and read time, not by editing derived
files.

## The set travels with backups

A backup payload carries `payload["tombstones"]` **alongside the memory
section** — it rides the section it protects, so the envelope's `apply`
grammar does not change. On restore:

1. the restored text is filtered through **local ∪ traveling** tombstones
   before it is written, so a restore can neither reintroduce a forgotten
   source nor lose the suppression afterwards;
2. the traveling rows are merged into the local ledger (idempotently —
   a row that is already known is skipped).

Without this, restoring an old backup on another machine would silently
un-forget a source.

## What it does not do (honest limits)

- **Nothing is deleted.** A suppression removes a source from *active* memory:
  injection, the memory view, profiling, recall and restored backups. The
  original session transcript, copies that already left the machine, the
  public repository history and backups taken before the suppression are
  untouched.
- **Session transcripts stay out of this slice.** Removing transcript content
  means rewriting append-only files and invalidating the search index — a
  larger change than read filtering. The FTS5 index is built from transcripts
  only, so no learned or curated source is in it and there is nothing to purge
  from it here.
- **A restore writes the ACTIVE profile's body root.** Exercise a restore
  under a temporary profile (`AIDOG_PROFILE` + `AIDOG_PROFILES_DIR`); never
  against the installation's own curated slots.
- `deploy/aidog_memory.py` still reads the repository's curated slots by a
  fixed path — that isolation break is **not** fixed by this stage.
- The learned-memory **write** path had no cross-process lock at the v0.5.103
  release, so two concurrent learns could lose one entry — the same defect
  class the pipeline and fleet ledgers already fixed. The tombstone ledger
  does **not** repeat it: it takes the shared POSIX mutation lock. (A later
  car may close the write-path gap; this page describes the v0.5.103
  behaviour.)

## CLI

```bash
python3 -m mycore.memforget list
python3 -m mycore.memforget forget --channel learned --text "<the source text>"
python3 -m mycore.memforget forget --slot memory --heading "<the entry heading>"
```

`list` prints the count, the channels and the tombstone ids — **ids only, no
excerpt**. `forget` prints the new id, the channel, whether it was already
recorded, and the honest limits note. A refusal (unknown channel, missing
text, a full ledger, an unavailable lock) exits 1 with a reason that names
the rule, never the content.

## Evidence

- **`mycore/tests/run.py::test_memforget`** (core suite, **2859/2859** at the
  v0.5.103 baseline) covers, in an isolated profile and state root:
  - with nothing forgotten, the learned and curated slots are injected
    byte-for-byte, the memory view is unchanged, the owner profile still sees
    the curated slots, and **the read paths create no ledger file at all**;
  - a forget records an opaque `src_<32 hex>` id, the report carries the
    honest limits, and the injected learned slot drops the forgotten source
    while a second source keeps working;
  - a forgotten source is **refused at admission** with the
    `source-suppressed` reason, and a repeated forget is idempotent (one row,
    `already: true`);
  - the ledger holds **no excerpt, no path and no credential** — a suppressed
    secret (a machine path and a credential-shaped value) never reaches the
    file, and the row still carries its channel, scope and timestamp;
  - the curated view and the owner profile drop the tombstoned entry, and the
    suppression survives a **process restart** and an **index rebuild**;
  - restoring an **old backup** keeps the item suppressed, and the traveling
    set is merged into the local ledger;
  - **profile isolation**: a suppression in one profile does not affect
    another;
  - repeated and concurrent forgets leave a well-formed ledger.
- **Independent cross-process verification** (outside the product suite) —
  a tombstone written by a separate process, an injection in a fresh process,
  the admission refusal, ledger hygiene, per-store scope, the curated filter,
  the memory view, restore filtering with a traveling set, and idempotent
  repeats.
- **Standing gates**: `python3 -m mycore.tests.run` → **2859/2859** ·
  `python3 tests/web_console_p3.py` → **610/610** · locale packs **1861** keys
  per pack.

## Stage 3 — the owner-facing control

- `GET /api/memory/forget/preview?slot=&title=` — read-only; reports the
  slot, the heading, the channel, the opaque source id, the number of
  sections that would be filtered, which surfaces are affected and the
  honest limits sentence. No entry body, no path, no credential.
- `POST /api/memory/forget` — Bearer `WEB_TOKEN`; `{slot, title}`; records
  a tombstone for that entry. Repeating it is a no-op that reports
  `already`. The slot is resolved through the ACTIVE PROFILE's seams, so a
  named profile never addresses the repository's overlay files.
- The console's memory view shows the channel and the short source id per
  entry and opens a confirmation that shows the preview before the write.

## Honest limits of the evidence

- The console **does** have a user-facing control since stage 3: the memory
  view lists each entry with its channel and opaque source id, and a
  confirmed FORGET records a tombstone (nothing is deleted). Covered by
  `tests/memory_forget_api.py` (39/39) and `tests/memory_forget_ui.py`
  (48/48, offline browser).
- Stage 1 is deliberately incomplete on the transcript store; a source that
  exists **only** in a session transcript is not suppressed by this slice.

## Stage 4 — the derived owner profile

- The owner profile derives keywords from session transcripts; each one is a
  source of its own (channel `profile`, opaque source id) and can be
  forgotten from the profile view with the same preview-then-confirm gate.
- A silenced derived item is excluded where the profile is BUILT (so the
  stored document, the score, the traits, the tiers and the coverage are
  computed without it, and a re-analysis cannot reintroduce it) and where a
  stored profile is READ (so the forget takes effect immediately, with no
  tick and no restart).
- The session transcript keeps the words: the derived item and its score
  contribution are silenced, the original text is not deleted, and the
  session search index still holds the transcript row.
- Evidence: `tests/profiling_forget_api.py` (50/50) and
  `tests/profiling_forget_ui.py` (61/61, offline browser).

## Stage 5 — forgetting from the chat

- `/forget <query>` lists matching sources and hands out a one-shot code per
  candidate; `/forget confirm <code>` performs the write. The command is
  answered by the service's command layer, which runs BEFORE the model
  pipeline, and no tool is registered for it — the model cannot forget
  anything, only the owner can.
- Candidates are labelled, never quoted: a heading for a stored entry, the
  token for a derived item. No body, no excerpt, no path and no credential.
- The pending list is one per chat, expires after five minutes, and lives in
  the per-profile state root.
- Evidence: `tests/tg_forget_flow.py` (47/47), including an assertion that
  the model client is never called for a forget command.

## Stage 6 — a session message can be forgotten

- A transcript message is a source of its own, addressed by the digest of its
  normalised text. The console's session search shows the message; a FORGET
  action sits next to the result, so the owner recognises the source without
  the product printing it anywhere new.
- The transcript file is never rewritten. The filter sits at every point where
  messages are read: the search index, the recall that runs after a fold, the
  session list, the per-session export, the owner profile corpus, the
  console's live chat history, its resume/fork/rewind faces, the chat
  service's history and the CLI's session view.
- The chat command list does NOT offer message sources: a chat reply quoting
  past messages would move private text into another surface.
- Honest limits: the transcript line stays on disk (and so does anything
  already copied off the machine, committed to the public history, or captured
  in a backup taken before the forget); normalisation makes the identity
  case- and whitespace-insensitive, so "Hello" and "hello" are one source and
  an edited message is a new one; two messages with identical text are one
  source, so forgetting one silences both.
- Evidence: `tests/session_forget_api.py` (72/72) and
  `tests/session_forget_ui.py` (50/50, offline browser).
