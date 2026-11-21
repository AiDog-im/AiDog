# Site visibility (aidog.im): PWA icons, AI-readable surface, visit counter

Scope: everything under `site/` that makes the marketing site installable,
machine-readable and countable. Design decisions and degradation semantics
for the three pieces shipped together (owner instruction, 2026-10-22).

## 1. PWA — installable, correct "Add to Home Screen" logo

Problem: the page only linked a 128 px `favicon.png`; mobile install prompts
therefore picked a wrong/low-res image. The canonical logo is
`site/assets/logo.jpg` (1192x1167, opaque, background already the page white).

Generated assets (`site/assets/icons/`, derived from `logo.jpg` with PIL —
regenerate any time by re-running the same script; sizes are asserted by
`deploy/deploy_site.sh --check` reaching them over the network):

| File | Size | Treatment |
|---|---|---|
| `icon-192.png` | 192x192 | square centre crop, full bleed |
| `icon-512.png` | 512x512 | square centre crop, full bleed |
| `icon-maskable-512.png` | 512x512 | logo at ~76% (12% safe-zone padding) on opaque `#fcfcfa`, `purpose: maskable` |
| `apple-touch-icon.png` | 180x180 | opaque `#fcfcfa` base + cover logo (JPEG has no alpha, so the base colour is painted first) |

`site/manifest.webmanifest`: name `I'm AiDog`, short name `AiDog`,
`start_url: /`, `display: standalone`, `background_color: #fcfcfa`,
`theme_color: #2892ff`, all four icons (maskable carries
`"purpose": "maskable"`). `<head>` links: `rel=manifest`,
`rel=apple-touch-icon`, `<meta name="theme-color" content="#2892ff">`.

## 2. AI-readable surface (pure English, verifiable facts only)

- `site/llms.txt` — llmstxt.org format: H1 + one-line blockquote positioning,
  then `## Product` / `## Interfaces` / `## Live stats` / `## Links`.
  Policy: only facts a reader can verify on the live site (permanently free,
  local-first console, self-developed core, Telegram-first channels, section
  anchors, the visits endpoint contract). No version numbers unless verified,
  no third-party brand words (the publish gate scans for them), no claims
  about endpoints that do not exist.
- `site/robots.txt` — `User-agent: * / Allow: /` plus the sitemap line.
- `site/sitemap.xml` — single URL `https://aidog.im/` with `lastmod`.

## 3. Visit counter (record + public display, Cloudflare free tier)

### Architecture

- **Storage**: Cloudflare **D1** database `aidog-site-ledger` (free tier:
  100k rows written/day vs KV's 1,000 — the binding constraint that forced
  the D2 migration; see `docs/design/usage_ledger_design.md`). The counter
  lives in table `site_visits`; its historical KV count was seeded once
  (max-seed, idempotent, re-run by `deploy/deploy_site.sh`) so the public
  number continues instead of restarting at zero. The legacy KV namespace
  `aidog-site-visits` and its `VISITS` binding stay provisioned this car —
  zero cost, instant rollback to the previous `visits.js`.
- **Endpoint**: `site/functions/api/visits.js`, a Pages Function (bundled into
  the Pages project; no extra Worker, no separate hostname).
  - `GET /api/visits` → `200`
    `{"count":<int>,"unit":"page views","note":"approximate, includes crawlers and refreshes"}`
  - `GET /api/visits?format=text` → `200` `text/plain` body `visits: <int>`
    (the AI/CLI-friendly form).
  - `Cache-Control: no-store` on every response.
  - **Failure semantics (red line)**: D1 binding missing or a D1 call throws →
    `503 {"error":"counter not configured"}`. The function never returns a
    fabricated number; the frontend never renders a fabricated number.
- **Binding**: the D1 database is attached to the Pages project's
  `deployment_configs` (both `production` and `preview`) as binding name
  `LEDGER`, via the same GET → merge → PATCH pattern as the KV binding, so
  re-runs are idempotent and can never drop other bindings/vars. The schema
  (`deploy/d1_migrations/0001_init.sql`, `CREATE ... IF NOT EXISTS`) is
  applied idempotently by the deploy script via `wrangler d1 execute --remote`.
- **Wrangler quirk (measured)**: `wrangler pages deploy` resolves the Pages
  Functions directory as `<process cwd>/functions`, *not* `<output>/functions`.
  The deploy step therefore runs from `site/`, where `functions/api/visits.js`
  lives. The functions directory is on wrangler's static-asset ignore list, so
  it is compiled into the Functions bundle only — fetching
  `/functions/api/visits.js` publicly falls back to `index.html` (no source
  exposure), which the deploy verification relies on.

### Counting semantics (honest scope)

One increment per endpoint GET. The footer block fetches once per page load,
so loads, refreshes and crawlers all count — approximate by design, stated in
the `note` field and in `llms.txt`. Deploy verification itself performs two
GETs (that is how increment is asserted).

### Frontend + i18n

- `site/index.html` footer: `<span id="visitsWrap" hidden>` wrapping a
  `data-i18n="footer.views"` label and `<span id="visits">` count.
- `site/assets/main.js`: one `fetch("/api/visits", {cache:"no-store"})` on
  load; on any failure (network error, non-2xx, non-numeric count) the whole
  block stays `hidden`. Success reveals it with the integer.
- Locale packs (`site/i18n/en.json`, `zh.json`, 307 keys each, same-name
  parity): `footer.views` = `"Page views:"` / `"页面浏览："`.

### Degradation ladder (what happens when a permission is missing)

1. D1 `D1 Edit` on the API token → database + binding + schema ensured → live
   200 counter (current state).
2. Binding/schema ensure fails → deploy still succeeds; `/api/visits`
   answers `503` honestly, `deploy_site.sh --check` prints `PENDING` (not a
   failure), footer block hidden, report carries the missing permission.
3. Worker custom-domain fallback and "hide the block" are the last resorts;
   no path ever displays a fake zero.

## 4. Verification gates

- `bash deploy/deploy_site.sh --check` (no writes) publicly asserts:
  `/llms.txt` 200 + first line `# I'm AiDog`, `/robots.txt`, `/sitemap.xml`,
  `/manifest.webmanifest` 200 + valid JSON with `name`/`icons`,
  `/assets/icons/icon-512.png`, `/assets/icons/apple-touch-icon.png`,
  `/api/stats` 200 with the aggregate shape (or `PENDING` on an honest 503),
  `/api/visits` 200 with two-GET increment + `?format=text` (or `PENDING`
  with an `error` body when the binding is absent). All curl calls use
  `--noproxy '*'`.
- `node tmp/shoot_site.js` (Playwright, local static server) — 62 assertions,
  including per-shot: manifest link in head, apple-touch-icon in head,
  theme-color `#2892ff`, footer visits block `hidden` without an endpoint or
  strictly numeric with one. The local server has no Pages Function, so that
  one legitimate 404 is filtered out of the js-clean console check (with a
  comment in the script); the real counter is asserted against the public
  domain by the deploy check instead.

## 5. Usage ledger + public charts (D2 car, 2026-10-31)

The same D1 database backs the anonymous usage ledger: installs report once
per UTC day (`mycore/ledger.py`, owner collect-by-default),
`POST /api/ledger` upserts the whitelist-exact payload, `GET /api/stats`
serves the public aggregates and the models-section chart renders them
(`site/assets/main.js`, honest placeholder until the endpoint answers).
Full design — schema, privacy whitelist, scenario rules, quota account,
rollback: `docs/design/usage_ledger_design.md`.

## 6. Site dynamics (car G, 2026-11-02): pulse snapshot, /models, platforms, PWA

- **Dated GitHub pulse** — `deploy/deploy_site.sh` stage 0 writes
  `site/data/gh.json` (fetch date, release version + first-line teaser,
  stars/forks/watchers, best-effort discussions count) straight from the
  GitHub API using the env-only `AIDOG_COMMUNITY_PAT`; **zero CF cost** (the
  page reads this static file, never KV — the free-tier KV write ceiling
  stays reserved for the visits counter). API failure keeps the previous
  snapshot: stale-but-honest, always carrying its own `fetched_at`.
  The `#pulse` card on the home page hides entirely when no snapshot exists;
  values render only from the file, so a stale number is visibly dated and
  nothing is ever fabricated client-side. `site/data/` is gitignored (machine
  state, not source); the deploy verify step asserts `/data/gh.json` 200 +
  shape after every upload.
- **`/models` statistics page** — a self-contained sibling of `skills.html`
  rendering the FULL `/api/stats` document (all-time totals, last seven UTC
  days, per-model and per-scenario breakdowns) behind
  `models.html` + the home-page Models section links to it; listed in
  `sitemap.xml` (one `<loc>` per `<url>`) and `llms.txt`. Failure states
  copy the home chart: unreachable says so, zero data waits for the first
  report.
- **Platforms matrix** — an honest `#platforms` section on the home page
  (Linux reference path / Windows via WSL2 / macOS verified on arm64 /
  iOS-Android via bot + site), mirrored by a `Platforms:` line in
  `llms.txt` and a paragraph in both READMEs; the nav stays at its nine
  anchors (the section indices run 01…09 with `platforms`=02, `pulse`=09 —
  pinned by `tmp/e2e_site_redesign.js` and `tmp/roundc_site_probe.js`).
- **Console PWA (manifest only)** — `web/static/manifest.webmanifest`
  (served `application/manifest+json` via the `MIME` map) + manifest link,
  `theme-color` and apple-touch-icon in the console head, PWA icons in
  `web/static/`, and an honest LAN note in the Access card
  (`onb.accessLan`: loopback-only, SSH tunnel, no cloud endpoint; the
  install chip only appears once the page answers over HTTPS). **No service
  worker, by owner adjudication** — the console always reflects live local
  state, so there is deliberately no offline copy of it.
- **Gates**: `tmp/roundc_site_probe.js` carries the car G legs (34 checks),
  `tmp/e2e_frontend_ui.js` leg U14 covers the console PWA surface, and
  `deploy_site.sh` verify gained `/models` + `/data/gh.json`. The E2E suite
  inventory stays **22** — car G extended existing probes, never added one.
