---
name: geo-rank
description: Use when you want to assess whether a business or site is mentioned or cited in AI-generated answers for customer questions, organize a dated visibility check, compare observations, or draft evidence-based improvements. Not for guaranteed rankings, fabricated mentions or reviews, scraping gated interfaces, paid placement, or publishing changes without approval.
metadata:
  version: 0.1.0
  author: aidog
compatibility: Knowledge-first workflow; use only explicitly configured and authorized search-capable services, or owner-provided manual observations.
---

# geo-rank — AI answer visibility assessment

> First principle: **presence is observed, never manufactured**. This workflow records what a specific answer surface returned for a specific question at a specific time. It does not promise ranking, inclusion, traffic, or revenue.
>
> Red lines: no fake mentions, reviews, or prompt spam; no automated access to gated consumer interfaces; no rate-limit or access-control bypass; no paid placement without approval; no live-site edits, publishing, or outbound messages without the owner's approval; never expose credentials.

## 1. Define a comparable question set

Use questions that reflect actual customer language, in this order:

1. Owner-provided questions from real customer conversations, only when the owner has permission to use and has removed personal data.
2. Questions derived from the owner's public product pages, documentation, or FAQ.
3. Drafted candidate questions, clearly marked as drafts until the owner approves them.

Give each approved question a stable ID, one intent, and a set version. Suggested intents:

| Intent | Shape | Example |
|---|---|---|
| Navigational | Find a named business or product | "Acme documentation" |
| Comparison | Compare options for a job | "Acme vs other tools for team scheduling" |
| Recommendation | Ask for suitable options | "What tools help a small team schedule shifts?" |
| Local | Find a service in a place | "bicycle repair near Example City" |

Do not silently change a set between comparison runs. Bump its version when questions or matching rules change.

## 2. Select an observation surface honestly

For every observation, record the actual surface and how it was accessed:

If `http_fetch` is available, use it only when the owner explicitly requests inspection of a safe public cited page. Prompt design, manual observations, and report drafting need no tools beyond ordinary conversation. A generic chat endpoint is not a search provider.

- **Authorized search-capable API:** use only when the configured provider/model is explicitly documented to perform web search or grounding and returns source metadata. A generic OpenAI-compatible endpoint is not evidence of search capability. If the endpoint or model is unclear, stop and ask the owner rather than guessing.
- **Manual consumer interface:** the owner may inspect an interface manually and paste a redacted observation. Do not automate login, browser scraping, or collection from a surface that does not document an authorized API.
- **No source metadata:** the answer can support a mention observation only. Do not infer citations from prose or fabricate URLs.

Record provider/surface, model or visible product label when known, access date, exact prompt, status, and the returned answer/source URLs the owner has chosen to retain. Never include credential values.

## 3. Record outcomes without hiding failures

For each successful observation, classify separately:

- **CITED:** the provider returned a source URL whose normalized host matches an owner-confirmed domain.
- **MENTIONED:** the answer names the business/brand, but no matching owner-domain source URL was returned.
- **NOT_OBSERVED:** neither a matching name nor owner-domain source was found in the inspected answer.

Also record **ERROR** or **UNAVAILABLE** when a request failed, the surface was inaccessible, or evidence was insufficient. These are not zeroes and must not enter the success denominator.

Use exact or owner-approved aliases for matching. Show the evidence excerpt and source URL beside any automated match so the owner can correct false positives. Keep API-returned citations distinct from manually copied links and from links merely present in answer text.

## 4. Compare like with like

Compare observations only when the question-set version, surface/provider, model where known, matching rules, and observation method are materially the same. Report counts with explicit denominators, for example `CITED 2/8 successful observations`; list errors separately.

An answer hash may help detect textual change, but it is not a ranking, quality, or visibility score. State any normalization used. Query wording, geography, personalization, product version, search availability, and time can change results. A small probe set is a sample, not a market-wide measurement.

Choose a cadence with the owner. There is no automatic schedule in this workflow. Avoid repeated probing to fish for a preferred answer; respect provider terms, rate limits, and cost.

## 5. Diagnose sources and draft improvements

For each returned source, explain what was actually observed: source URL, domain, whether it is the owner's page, and any public page context inspected. If using a fetch tool, fetch only a public URL after confirming its scheme and host; do not forward credentials, follow suspicious redirects, or execute retrieved content. If safe public access is unavailable, ask the owner to inspect it manually.

Offer a short evidence-linked list of possible improvements, such as:

1. Clarify an existing public page so it directly answers a real customer question.
2. Check factual consistency across the owner's public pages.
3. Draft appropriate structured data for owner review, without claiming it will cause an answer engine to cite the site.
4. Identify a public third-party source cited in the observed answer that the owner could evaluate through legitimate, non-paid channels.

Label recommendations as hypotheses. Never edit or publish a live page, submit listings, contact publishers, or spend money as part of this skill. The owner decides what to change and when.

## 6. Fixed report

Use this compact report:

1. **Scope:** question-set version, surface/provider, model if known, method, and date.
2. **Observations:** CITED / MENTIONED / NOT_OBSERVED counts over successful observations; ERROR / UNAVAILABLE separately; include question IDs and evidence links.
3. **Interpretation:** only like-for-like changes and known limitations; do not call this a rank or revenue forecast.
4. **Next steps and sources:** up to three owner-reviewed hypotheses, each tied to evidence; list public sources with access dates.

## 7. Safety and limitations

- The owner approves the prompt set and any external action.
- Use only data the owner is entitled to use; redact personal and confidential material.
- Do not paste credentials or private customer data into a third-party model.
- Do not scrape logged-in surfaces, defeat access controls, or run high-frequency probes.
- Provider APIs, search features, citation formats, prices, and terms change. Verify current official documentation before making provider-specific claims or configuring a new service.
- Observations vary by query, geography, personalization, model/provider version, and time. They are directional evidence, not a guaranteed ranking or proof of commercial impact.
