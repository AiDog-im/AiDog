# OWNER ANALYSIS — owner profiling over local session history (need-A, P0)

Status: shipped in the need-A P0 car. Design authority:
[../design/owner_profiling_design.md](../design/owner_profiling_design.md)
(the full evaluation, the phased plan and the adjudication points D1–D9).

**主人分析 = the product's reading of its owner.** The assistant's own
history of what the owner asked for is the only input. A local, deterministic
engine scores those words, classifies every keyword on four dimensions, and
shows the **complete evidence** behind every row — no model call, no network
call, no claim without a source.

## What ships in P0

| Piece | Where |
|---|---|
| The analysis engine | `mycore/profiling.py` — tokenization (CJK bigrams + latin words), TF×IDF×recency×urgency scoring, the four-label classification, evidence assembly, bounded state I/O |
| The settings partition | `mycore.profiling` — `enabled`, `since`, `top_k`, `half_life_days`, `interval_s`, `topics`, `exclude` (grammar: `mycore/profiling.py::parse_config`, first error, shape-only) |
| The read model | `GET /api/profiling` (loopback, no auth) — the last stored analysis, its corpus facts, its counts, its gaps and its knowledge cards |
| The write face | `POST /api/profiling {action: analyze\|search\|push\|propose}` (Bearer) — analyze (local) · search (the egress) · push (raise the findings) · propose (returns proposals, writes nothing) |
| The console section | `#view-sniff` → **OWNER ANALYSIS** card (KEYWORDS / DIMENSIONS / GAPS / KNOWLEDGE segments) with `ANALYZE NOW`, the SEARCH switch and the PUSH switch |
| The SNIFF reopen surface | `POST /api/sniff {action: sweep\|resume}` and `/sniff [status\|run\|pause]` in any chat session |
| The service legs | `mycore/service.py::_maybe_profile` (300 s, local only, counts only) and `_maybe_prosearch` (1800 s, the only egress, every cap inside the module) |
| The search partition | `mycore.search` — `enabled` (**ships false**), `engine`, `searches_per_run`, `fetches_per_run`, `daily_cap`, `interval_s`, `priority_domains` (grammar: `mycore/prosearch.py::parse_config`, first error, shape only, **no credential field**) |

## The four dimensions (each label shows the rule that fired)

| Dimension | Values | Rule |
|---|---|---|
| **Trait** | `keen` · `retrieval-gap` · `empathy-partial` · `growth` · `known` | `keen` = a configured SNIFF source covers it OR the owner asked to be watched for it → the proactive lane. `gap` = nothing covers it → the retrieval gap. `partial` = one curated-memory hit → the empathy lane. `growth` = 2+ curated hits AND 3+ distinct days → the learning lane. `known` = 2+ curated hits, not recurring. Precedence: keen > gap > partial > growth. |
| **Topic** | owner buckets (chain · markets · product · engineering · home · health · work · learning · `other`) | lexicon match on the token first, then a per-topic count over the message body; ties break in sorted slug order. No match is `other` — never a guess. The owner extends the set through `mycore.profiling.topics`. |
| **Tier** | `L1` act now · `L2` update · `L3` background | `L1` = an urgency word AND trait `keen`; `L2` = 3+ messages AND seen within 7 days; `L3` = the rest that survived the cut. |
| **Coverage** | `gap` · `partial` · `covered` | counted against the curated relationship-memory files (`overlay/memory/*_curated.md`). |

Every row carries `n_msgs`, `n_days`, `first_seen`/`last_seen`, the score
breakdown (idf / recency / urgency), and up to three dated excerpts (≤120
chars). A keyword seen once on one day is marked `weak` and never counts as an
established interest.

## Honest boundaries (enforced in code, asserted by the suite)

- **Zero model tokens.** `mycore/profiling.py` imports no model client, no
  loop and no delivery path; the core suite asserts the absence at source
  level (the same trick `mycore.sniff` uses for its read-only promise).
- **Local only.** No transport seam and no socket exist in the module (asserted
  by AST inspection of its imports). The only writes are the runtime state
  file `<state>/profiling/profile.json` (gitignored).
- **The owner's words only.** `role == "user"` content is scored; assistant and
  tool text never shape the profile. A repeated canned prompt (a test harness)
  is capped at 3 identical messages each and the collapse is **reported**
  (`duplicates_collapsed`), never silently kept or silently dropped.
- **Fixtures are excludable by name.** `mycore.profiling.exclude` holds file-name
  globs (default `webhook_e2e*`) so e2e fixtures never shape a real profile.
- **Bounded.** ≤50,000 messages / ≤5 MB per run, top-K ≤200, ≤3 excerpts of
  ≤120 chars, ≤500 state rows. Truncation is reported (`truncated`), never
  silent.
- **Nothing is spoken.** P0 never pushes: there is no delivery path in the
  module and no finding is synthesized from it. Proactive push and outer-web
  search are the P1 increment (adjudicated separately, search default OFF).

## The SNIFF reopen surface (need-A line 1)

The board already defaults to `enabled=true, pause=false`. The reopen paths
are what P0 adds:

- **`/sniff run`** (any chat session) — persists `enabled=true, pause=false`
  through the strict SNIFF partition gate and then sweeps once. The switch is
  persisted, so it survives a restart.
- **`/sniff`** — status (on/off/paused, source + rule counts, last sweep).
- **`/sniff pause`** — deliberate off.
- **Console** — `SWEEP NOW` with `resume`, and a `RESUME` action; both write
  the same partition and then sweep.

## Gates

- `python3 -m mycore.tests.run` — the `profiling:` lane (grammar, tokenizing,
  scoring, classification, evidence shape, duplicates/exclude/since, honest
  empty states, state I/O, the `/sniff` command, and the three absences:
  no model client, no transport, no delivery path).
- `python3 tests/web_console_p3.py` — the `mycore.profiling` settings
  partition (first-error 400s), `GET/POST /api/profiling`, and the Bearer gate.
- `python3 tests/source_lang_check.py` — the module's Chinese lexicons are
  unicode escapes, so the source stays ASCII (0 violations).

## P1 — the outer-web accumulation leg and the closed loop

P0 stops at the board. P1 adds the ONE egress surface of this feature and
the control half that closes the loop with the existing SNIFF board.

### The search leg (`mycore/prosearch.py`)

| Contract | How it is enforced |
|---|---|
| **The only egress, and it is OFF by default** | `mycore.search.enabled` ships **false**. The owner flips one switch in the SENSE view. Without it the leg does not run at all. |
| **Keyless, credential-free** | No account, no API key, no slot. The partition has **no credential field** — a value-shaped field is an unknown field and is refused. The owner's proxy policy is **reused** from `mycore.tools` (one policy, never re-implemented). |
| **What leaves is exactly the query** | A keyword query and nothing else: no session text, no excerpt, no chat id, no file name. A fetched page's URL is stored locally, never sent anywhere. |
| **Honest status** | `reachable` / `blocked` (the engine answered with an anti-bot page) / `error` / `disabled` / `daily-cap` / `interval`. A blocked engine is **reported** and the leg stops for the day — it never retries in a loop. |
| **Bounded** | Per-run search and fetch caps, a Beijing-day cap, a 500-card knowledge log, a 400-char excerpt, a 600-char page slice. |
| **Prose, never code** | `<script>`/`<style>` bodies are dropped before a card is written: a card quotes prose, not JavaScript. |

The engine is keyless RSS (`bing_rss` — the only engine, a closed enum). A
keyword is searched at most once a day; a `covered` keyword is never
re-searched.

### The closed loop (`mycore/propush.py`)

- **One delivery chain.** A raised finding is an ordinary SNIFF finding
  (`source="owner"`, `rule="owner_profile"`), spoken by the **existing** cron
  delivery path — one quiet-hours gate, one digest cap, one PAUSE, one
  mark-first write. `sniff.delivery_take` gained exactly one keyword argument
  (`extra_speak`) for this; there is no second delivery code path.
- **Passive by default (D6).** The push mode ships `passive`: the urgent rows
  land on the board and are never spoken until the owner switches to `auto`.
- **Propose-only program operations.** `add web source` · `pin keyword` ·
  `add topic term` — each returns a **proposal** and writes **nothing**. The
  owner's click is the approval, and the write then goes through the strict
  settings gate.
- **Pinned keywords.** A keyword the owner pinned by hand is `keen` by
  decision, not by a guess.

### The view

The OWNER ANALYSIS card grows two segments (**GAPS** — the retrieval gaps the
loop still has to fill, each with its proposal button; **KNOWLEDGE** — the
accumulated cards with their labels, domain, date, excerpt and an `open`
link) plus the two switches: **SEARCH** (the egress) and **PUSH** (the
delivery mode). Both read their real state from the API.

## Not in this car (honest gaps)

Model-assisted re-ranking (opt-in, metered), a vector store (deferred under
the standing D8 trigger — the corpus is ~40k chars, which keyword recall
serves), any actuation beyond the owner's own click, and any analysis of
sessions that live outside this product's own state directory.

## Forgetting a derived item (v0.5.106)

Every keyword in the owner profile is a derived item with its own source id,
and the profile view offers a forget action per keyword: the confirmation
shows a preview first (how many items would be silenced, and whether the
profile, the score and the coverage are affected) and writes a tombstone
only. Nothing is deleted — the session transcript keeps the words; the
derived item and its contribution to the profile are silenced, including
after the next re-analysis. See [memory suppression](memory_suppression.md).
