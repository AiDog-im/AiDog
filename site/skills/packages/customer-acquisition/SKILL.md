---
name: customer-acquisition
description: Use when running an acquisition pipeline: leads from your own signals, a contactability score shown with its arithmetic, one editable draft per lead, reply triage, a daily cap. Not for bought or scraped lists, sends without per-message human approval, bulk cold mail, fake engagement, or promised revenue. The console ships this as the Pipeline view.
metadata:
  version: 0.1.0
  author: aidog
compatibility: Knowledge-first workflow; the product's Pipeline view and the email channel route carry it end to end. No scripts, no credentials, no third-party system.
---

# customer-acquisition — the acquisition pipeline, run honestly

> First principle: **a pipeline is a queue of decisions, not a bulk sender.**
> Every stage below produces something a human can look at. The only stage
> allowed to touch the outside world is the send, and at most one message
> leaves per human click.

## The six stages

| Stage | Input | Output | Never |
|---|---|---|---|
| 1 · source | your own signals: a forum question, a directory listing, a person who asked you directly | a lead record (name / address / org / why) | scraping, purchased lists, guessing an address |
| 2 · qualify | the lead record | a **contactability score** with its arithmetic | a "conversion probability" (unmeasurable = not displayed) |
| 3 · draft | a qualified lead **with an address** | one editable message per lead | more drafts than the daily cap allows to send |
| 4 · approve | the draft | a human decision (`approved` / `rejected` / edited) | any model self-approval |
| 5 · send | an approved draft | one message, one click | sending while the kill switch is off, above the cap, or without the postal address |
| 6 · track | inbound replies | a class: `opt_out` / `interested` / `question` / `other` | auto-replying to anyone |

## 1 · Sourcing (own signals only)

- **Own forum / issue tracker** is the richest legitimate source: someone
  already asked a question in public, so answering them is not cold mail.
  Record the thread title as the lead's *why*; record **no** email address you
  did not receive directly.
- **Directory submissions and inbound questions** are the second source.
- If a person's address is unknown, the lead stays address-less and is *never*
  drafted for — the pipeline does not guess, infer, or enrich addresses.
- **Excluded, permanently**: purchased lists, scraped address books, "lead
  databases", harvesting from group chats, and anything that requires a
  third party to hand you personal data.

## 2 · Qualification

Score = source weight + address present + signal length + organisation, and
**show the arithmetic** next to the number (`source manual +40 · address +15 ·
signal +15 · org +10`). Two rules keep this honest:

1. The number measures *how ready a record is to be contacted* — never how
   likely a person is to buy. If you cannot substantiate a metric, do not show
   it.
2. Score does not override consent: a below-bar lead is parked (`skipped`), not
   deleted, and a suppressed address never re-enters.

## 3 · Drafting

- One message per lead, written as if you were writing to one person.
- Subject line states the truth; no urgency theatre, no fake "Re:", no fake
  thread continuation.
- Include: why you are writing (a real reference), what the product is in one
  sentence, and the opt-out line.
- **Never** promise revenue, ranking, inclusion, or results; never claim a
  feature the product does not have.
- Keep the draft in the queue until a human reads it. Editing is expected.

## 4 · Approval (the iron rule)

- The model can draft, sort, and explain. It **cannot approve** — approval is a
  human action in the console (or an explicit written instruction for a single
  named message).
- An approval covers exactly one message. There is no "approve the batch".
- A rejection is a decision, not a retry: the lead is parked.

## 5 · Sending

Before each send, re-check the whole gate:

- [ ] the kill switch is on **and** the recipient is not on the suppression list
- [ ] the message is approved (and not edited after approval)
- [ ] the email route is configured (provider, sender address, credentials in
      slots — never in a config file)
- [ ] a real **postal address** is in the body (CAN-SPAM) and the message
      carries `Reply-To` + `List-Unsubscribe`
- [ ] the day's sends are under the cap; if the cap is reached, wait — never
      "catch up" the next day
- [ ] the body is honest about what the product is (free / self-hosted / what it
      does not do)

If any box is unchecked, the correct action is to fix it or to not send.

## 6 · Tracking replies

Classify every reply with opt-out checked first:

| Class | Signal | Action |
|---|---|---|
| `opt_out` | "unsubscribe", "remove me", "do not contact", 退订 / 不要再发 | suppress the address **permanently**, reject pending drafts, never re-add |
| `interested` | pricing / "tell me more" / "sign me up" | answer personally, soon — this is the only stage with real urgency |
| `question` | a real question | answer it; a question is not consent to sell |
| `other` | anything else | read it; no automated action |

- Replying is the owner's own mail client (or the product's chat, under its own
  allowlist). A triage record is for the eyes, not a send trigger.
- Keep the record small: sender, subject, class, short excerpt. The mailbox is
  not a CRM mirror.

## Anti-patterns (reject on sight)

- "Draft 500 emails and send overnight" — violates the approval rule and the
  cap; refuse and say why.
- "Just buy a list, everyone does it" — a GDPR/lawful-basis problem and a hard
  product red line.
- "The reply asked how much, so auto-send the price sheet" — no auto-reply,
  ever; the human answers.
- "Drop the postal address, it looks spammy" — the address is what makes the
  message legal in the US; drop the send instead.
- "Report 40% reply rate" on a sample of three — say the sample size or say
  nothing.

## Where this lives in the product

- Console **Pipeline** view: sources, score with arithmetic, the approval queue
  (edit → approve → send), replies, the live feed, and the controls card (kill
  switch, interval, daily cap, signature, postal address, opt-out line).
- `GET /api/pipeline` (read model) and `POST /api/pipeline` (Bearer-gated
  actions: `run`, `source.discussions`, `lead.add`, `lead.import`,
  `lead.qualify`, `lead.suppress`, `draft.make`, `draft.edit`,
  `draft.approve`, `draft.reject`, `draft.send`, `reply.handle`).
- There is **no model tool** for this pipeline: the assistant cannot add leads,
  approve drafts or send mail. It can only explain, draft text you paste, and
  triage what you hand it.
