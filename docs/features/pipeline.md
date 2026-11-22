# Pipeline — the acquisition pipeline (owner concept: 自动获客)

Status: shipped in the pipeline car (2026-11-17). Design authority:
[../design/acquisition_pipeline_design.md](../design/acquisition_pipeline_design.md)
— §7 is the owner adjudication (D1–D6) and **§9 is the engineering decision
record** this page documents. Module: `mycore/pipeline.py`. Console view:
`Pipeline` (`GET /api/pipeline`, `POST /api/pipeline`).

**A pipeline, not a bot that mails strangers.** Six stages run as pure
functions over ONE local state document; four of them are safe to automate, the
fifth (send) is always a human click, and the sixth (track) only classifies what
already arrived. There is **no pipeline tool in the model tool registry** — the
model cannot add a lead, approve a draft or send a message; the console routes
(Bearer `WEB_TOKEN`) and the service leg are the only entries.

## The six stages

| Stage | What runs | What it can never do |
|---|---|---|
| `source` | the owner adds one lead, pastes a list, or the service reads **our own public forum threads** (`mycore.community.list_discussions`) and records each author as a lead with **no address** | no scraping, no purchased list, no guessed email address (D5 + §4.2) |
| `qualify` | a deterministic **contactability score** 0–100 = source weight (manual 40 / import 35 / forum 25) + address present 15 + signal text 15 + org 10; `qualified` at ≥50 | no model call, no "conversion probability" (a number the product cannot substantiate is never displayed — see `pipe.gate` copy) |
| `draft` | one editable draft per qualified lead **with an address**, bounded by the remaining daily budget, deterministic template, always ends with the opt-out line | cannot exceed the cap, cannot draft for a suppressed or address-less lead, does not send |
| `approve` | the owner's click, Bearer-gated route | the model holds no path; approval alone never sends |
| `send` | the owner's second click → `pipeline.send_approved()` re-checks **every** gate and calls the email route's `send_message()` | cannot run while the kill switch is off, above the daily cap, without the postal address, for a suppressed address, or on an unapproved draft |
| `track` | inbound email from a **known lead** is classified (`opt_out` / `interested` / `question` / `other`) for the owner's review | never answers, never sends; an `opt_out` suppresses the address permanently and rejects pending mail |

Classification is bilingual (en + zh) and opt-out is checked **first** — a
message that says "stop writing, and by the way how much?" is an opt-out.

## The send gate (all of it, in one function)

`pipeline.send_approved(doc, draft_id, settings, channel)` refuses — with a
readable reason, never an exception — when any of these is true:

1. `pipeline.enabled` is false (the **kill switch**, shipped off);
2. the draft is not `approved`;
3. the lead has no valid address, or the address is on the **suppression list**;
4. `pipeline.physical_address` is empty — CAN-SPAM requires a real postal
   address, so the product asks for it instead of inventing one;
5. the `channels.email` route is disabled or not configured (the refusal names
   the missing slot/setting, never a value);
6. today (Beijing day) already has `pipeline.daily_cap` sends.

Every message carries `Reply-To` and `List-Unsubscribe: <mailto:…?subject=unsubscribe>`
headers, and **both compliance lines are re-asserted at send time**: the postal
address and the effective opt-out line (the owner's `opt_out_note` override or
the standard one) are appended if an owner edit deleted them — the draft text is
never trusted to still contain them. `mycore.channels` validates header names and rejects
CRLF in values — an injected `Bcc:` is a refusal, not a silent sanitization.

## Concurrency (one writer at a time)

Every read-modify-write runs under `pipeline.mutation_lock()`:

1. **in-process** — a `threading.Lock` (the console server is a
   ThreadingHTTPServer; two POSTs must not both pass the cap check);
2. **cross-process** — a best-effort `flock` on `<state>/pipeline/.lock`, so
   the service leg's tick (a second process) can never overwrite a `sent`
   status with a stale copy. Where `fcntl` does not exist (native Windows) the
   second layer is a documented no-op — the console is still the only sender
   there, so the cap remains atomic.

The lock is blocking by design: writers are a local file plus, at worst, one
SMTP call (~1 s); skipping a write would be silent data loss.

## State, bounds and privacy

- One atomic JSON document: `<state>/pipeline/pipeline.json` (temp +
  `os.replace`, the cron-registry convention). Corrupt or missing state reads as
  an empty pipeline — never a guessed partial — and a corrupted id counter is
  repaired upward so an id can never be reused.
- Bounded lists: 500 leads · 500 drafts · 200 replies · 200 events.
- **PII bounds**: a lead stores name / address / org / source / the ≤400-char
  signal text — never a mailbox body. A reply stores sender, a ≤200-char subject
  and a ≤200-char excerpt plus its class; the full message stays in the chat
  session IMAP already delivered it to. The suppression list stores addresses
  only and is bounded at 500 (a full table evicts the oldest entry with an
  explicit event — a suppression that cannot be recorded must never look like
  one that was).
- **Caps hold on load too**: a syntactically valid but oversized hand-edited
  state file truncates to the same bounds the write path enforces, and a
  corrupted id counter repairs past the highest existing numeric suffix, so an
  id can never be re-issued.
- **Reply observation sits before the channel allowlist gate** — recording a
  triage row is a local write, not a reply, so it cannot weaken the fail-closed
  allowlist that governs whether the model may answer an inbound message at all.

## Settings (`pipeline` partition)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | kill switch — off means no sourcing, no drafting, **no sending** |
| `auto_source` | `false` | let the service leg read our own forum on its interval |
| `interval_min` | `60` | service-leg cadence while enabled (5–1440) |
| `daily_cap` | `5` | sends per Beijing day (1–50); also bounds drafting |
| `from_name` | `""` | signature line in the body |
| `physical_address` | `""` | **required before the first send** (CAN-SPAM) |
| `opt_out_note` | `""` | override for the standard opt-out line |
| `drafting` | `"template"` | `"template"` (today) or `"model"` (the drafting brain) |
| `model_drafts_per_day` | `5` | the brain's own money-burn cap (0–50), independent of `daily_cap` |

The service leg (`_maybe_pipeline`, every `interval_min`) sources, qualifies and
drafts only: `pipeline.auto_tick()` takes **no channel parameter**, so the
automatic half of the pipeline cannot send by construction. Everything it does
is one `PIPELINE_TICK` line in `service.log`; a failure is one `PIPELINE_ERR`
line and the loop continues.

## The drafting brain (`mycore/pipeline_brain.py`)

Opt-in, off by default. With `pipeline.drafting = "model"`, each draft
costs **one** model call: the model writes the **message body only**, and
the product appends the opt-out line, the signature and the postal address
itself — compliance cannot be lost to a model that forgot it.

| Rule | What happens |
|---|---|
| the lead's text is untrusted data | the system message says so explicitly; the data sits in a `<lead_data>` block and may never act as an instruction |
| no tools on the call | an injected instruction cannot make the model *do* anything — at worst it changes text the owner reads before sending |
| the recipient is never model-chosen | it comes from the lead record; the model never sees an address |
| a foreign address in the output | the body is **rejected** — the template is used instead |
| executable markup (`<script>`, `javascript:` …) | rejected |
| a model error, a timeout, an unusable answer | **fallback to the template**, with the reason recorded on the draft |
| the money-burn cap | checked **before** the call; a lead that misses the cap still gets a template draft, and the row says `template (model cap reached)` |
| token accounting | per draft and per day; an incomplete provider usage reads `?`, never a made-up number |

The draft's body is the model's text; the footer is the product's. With the
switch off, or with no model configured, the output is byte-identical to
the template's.

Design and adjudication (D1–D9):
`docs/design/pipeline_drafting_brain_design.md`.

## API

| Route | Auth | Returns |
|---|---|---|
| `GET /api/pipeline` | loopback, none (read model, like `/api/sniff`) | stages, counts, leads, drafts, replies, events, `email_route{enabled,configured,note}` |
| `POST /api/pipeline` | Bearer `WEB_TOKEN` | `run` · `source.discussions` · `lead.add` · `lead.import` · `lead.qualify` · `lead.suppress` · `draft.make` · `draft.edit` · `draft.approve` (`send:true` approves **and** sends once) · `draft.reject` · `draft.send` · `reply.handle` |

`POST /api/pipeline` returns the fresh read model with every mutation, so the
view never has to guess what happened.

## Honest gaps (queued, not hidden)

- **Real-provider SMTP acceptance is still pending credentials** (D6). The send
  path is exercised against a local SMTP sink in the core suite and in the smoke
  tests; the channel route itself has been shipping since 0.5.5x.
- **The drafting brain does not know the recipient.** It reacts to the lead's
  text, but it cannot research the company, and it must never invent facts —
  so a thin lead signal yields a short honest note rather than a clever hook.
- **A drafting brain needs a model endpoint.** With none configured the
  factory builds nothing and the pipeline stays on the template; the switch
  being on is never enough to make a call happen.
- **No auto-reply.** A triaged reply is for the owner's eyes; replying is done in
  the owner's own mail client (or through the existing chat path, which is
  governed by its own allowlist).
- **N8N is not embedded** (D3): the display reads our own `/api/pipeline`, and
  there is no third-party canvas, no foreign workflow engine and no second
  system to secure.

## Gates

`mycore/tests/run.py` → `test_pipeline` (80 checks: settings normalization,
lead validation, import, forum sourcing idempotence, scoring arithmetic,
drafting bounds, the draft state machine, every send gate, reply triage,
suppression, persistence/corruption incl. sparse-id repair and load-time caps,
the mutation-lock race (two threads, cap 1, exactly one send), the structural
absences, and the channel subject/header seam). Console smoke adds the read
model, the auth gate, the input refusals and the settings partition
round-trip; the view's own E2E is `tmp/e2e_pipeline_view.js` (25 checks:
deep link, stage rail, honest empty states, kill switch, both send-gate
refusals, zh pack, 390 px, zero pageerror).
