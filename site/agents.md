# Agents — aidog.im

This is the public site for **I'm AiDog**, a personal embodied AI you run
yourself: a constitution for a personality, curated memory for a life, and a
self-developed core that answers to you alone. Permanently free, local-first.

## Read first

- [llms.txt](llms.txt) — site map and product facts in Markdown.
- [llms-full.txt](llms-full.txt) — the full-length version.
- [Documentation](https://aidog.im/docs) — Telegram command reference, every
  capability document, and the installation guides.
- [openapi.json](openapi.json) — OpenAPI 3.1 spec for the public site API.

## Public API (no auth, aggregates only)

- `GET /api/stats` — usage aggregates (all-time totals, last seven UTC days,
  per-model / per-scenario breakdowns). Never a per-install record.
- `GET /api/visits` — approximate page-view counter.
- `POST /api/ledger` — one anonymous daily usage report per install
  (whitelist-exact schema; retries are idempotent).

## Machine-readable surfaces

- `/.well-known/mcp` — MCP endpoint declaration.
- `/robots.txt` — explicit allow for known AI crawlers (search, hybrid and
  training families) plus the sitemap reference.
- `/sitemap.xml` — page inventory with lastmod dates.

## Product in one paragraph

The console binds loopback-only on a machine you control; Telegram is the
first-class messaging channel; model endpoints and credentials are configured
by the operator (bring-your-own key). Orders in the Web3 exchange surface only
ever run prepare → human console approval → execute; withdrawals and on-chain
transfers do not exist in the core. Source: https://github.com/AiDog-im/AiDog

## Honesty notes

- Numbers on this site come from the release gates of the matching version —
  see `release.json` in the repository for the current one.
- Usage stats are opt-in, aggregated, and contain no prompts, files, paths or
  credentials.
