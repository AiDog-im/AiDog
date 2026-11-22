# Display currency — one cost unit for the whole console

Status: shipped in **v0.5.103** (2026-11-22). Design authority:
[../design/currency_display_design.md](../design/currency_display_design.md).
Carrier: settings `currency` + `fx_rate` · server: `web/server.py::display_fx`,
`collect_usage`, `collect_usage_self` · client: `web/static/app.js::dispInfo`,
`fmtCost`, `renderUsage`, `renderSelf`, `renderSelfBudget`, `renderControl`.

**One unit, chosen by the owner.** Before this car the console printed money
in whatever unit the number happened to carry, so a CNY-priced meter and a USD
budget could sit next to each other with no unit on screen. Now the model and
token costs of the whole console follow ONE settings-selected unit and carry
its ISO code, and every number that is not in that unit says so in words.

## The contract

| Setting | Meaning |
|---|---|
| `currency` | the display unit — `USD` `CNY` `EUR` `GBP` `JPY` (default `USD`; an unknown value is refused with 400) |
| `fx_rate` | CNY per **one** display unit; `0` = ask the automatic rate. A number in `0..100000`; a bool, a string or a negative value is refused with 400 |

- Prices and stored model costs stay in **source CNY**. The displayed amount
  is `source_cost / rate_cny_per_unit`.
- `CNY` is the native unit: rate `1.0`, source `native`, no network call.
- A manual `fx_rate` wins over the automatic rate (source `manual`).
- The automatic rate is fetched from a public, key-free FX endpoint and
  cached (6 h on success, 5 min on failure). **A cache hit only counts when
  the cached currency is the currency being asked for.**
- Amounts carry the **currency code** (`USD 12.34`, not `$12.34`). The
  server also reports a symbol, and `CNY` and `JPY` share `¥` — which is
  exactly why the code, not the symbol, is what identifies the unit.

## Where the unit applies

- **Channel usage** (`GET /api/usage`) — the "cost today" tile and the
  7-day comparison line (`charge = today ÷ 7-day avg`).
- **The toolbar** — the `TODAY n req · <amount>` line.
- **Self-metered window totals** (`GET /api/usage/self`) — the panel total
  and the per-model rows.
- **The current month** block of the same panel.
- The usage KPI label is unit-neutral (`COST TODAY`), never hardcoded `CNY`.

## Where it does NOT apply — and says so

- **Guardrail spend budgets** stay **USD** and are labelled `USD` in the
  console (`SPEND PER DAY (USD)`, `USD 2.00 / USD 10.00`). The guardrails use
  their own CNY/USD resolver (`mycore/leash.py::usd_rate`); the manual display
  rate is used there **only when the selected display currency is USD**.
- **Token caps** have no currency unit at all — a token count is a count.
- **Exchange limits** (per-transaction and daily caps, the threshold chips)
  stay USD and are labelled `USD` in words.
- The **anonymous usage ledger** reports counts, not money. No new monetary
  field and no new reporting payload were introduced by this car.

## Unknown is not zero

The rule that makes the unit trustworthy: **a number the product cannot
compute is unknown, and unknown never renders as `0`.**

- A **missing price** (a model with no configured price) produces a `null`
  cost, not `0.0`. The API returns `null` and the console shows `?`.
- A **missing required FX rate** (offline, provider down, or a currency with
  no rate) produces an unknown amount with the chosen unit still named — the
  note reads *rate unavailable — cost unknown*. There is no fallback currency
  and no silent CNY.
- An **absent unpriced self-meter total** keeps its existing dash.
- An **unknown budget spend** never draws a zero-valued arc and never claims
  `USD 0.00`.

## The four defects this car removed

| # | Defect | Fix |
|---|---|---|
| 1 | The automatic rate cache served **one currency's number under another currency's label** — a EUR lookup could return the USD rate | a cache hit requires the same currency; otherwise the rate is fetched again |
| 2 | A **cached usage aggregate kept a stale unit** — the 20 s usage cache returned the display block computed for the previous settings | the cache now refreshes the display metadata from the caller's current settings on every hit |
| 3 | A **missing legacy channel price became a false zero** — the daily cost read `0.00` and looked like "free" | the API returns `null` cost and the console reads `?` |
| 4 | An **unknown budget coerced null to zero** — the budget ring drew an empty arc and claimed `USD 0.00` | null stays null: an unknown spend draws no arc and claims no amount |

## API

| Route | Field |
|---|---|
| `GET /api/usage` | `display` = `{currency, symbol, rate_cny_per_unit, source, updated, src_currency}` — `source` is `native` \| `manual` \| `auto` \| `unavailable` |
| `GET /api/usage/self` | the same `display` block; `pricing.total_cny` is the source-CNY total and `null` when unpriced |
| `POST /api/settings` | `currency` (one of the five) · `fx_rate` (number `0..100000`); both are validated before the store is touched, and the settings document stays byte-identical on a refusal |

## The settings surface

Settings → Usage carries the currency select (`#curSelect`), the rate field
(`#fxInput`, "FX rate (0 = auto)") and a scope note (`ctl.currencyScope`):

> Model and token costs use this display currency. Guardrail spend budgets and
> exchange limits remain USD. The rate is CNY per display unit; unavailable
> rates show unknown costs.

Persistence rides the existing save flow — the display unit is a normal
settings key, not a separate store.

## Evidence

- **`tests/web_console_p3.py`** (console smoke, 610/610 at the v0.5.103
  baseline) — the currency legs: *FX cache is isolated by display currency*
  (USD → EUR → EUR again must not reuse the USD rate), the manual rate
  overrides the automatic one, the CNY native path returns rate `1.0`, an
  offline/unavailable rate degrades honestly, *usage cache refreshes display
  currency without stale units* (a cached aggregate switches units when the
  settings change), a missing price returns `null`, and the settings
  refusals (`currency: "XXX"` → 400, `fx_rate: -1` → 400, `fx_rate: true` →
  400).
- **`tmp/probe_currency_display.js`** — a read-only Chromium probe that loads
  the real console assets, stubs the API payloads and blocks every external
  request; **24 checks at 390 px** across USD, CNY, EUR, JPY and an
  unavailable GBP. It asserts the channel and self/month amounts, the
  explicitly-USD budgets, the unit-neutral label, the unknown-cost `?` and the
  "unknown budget never invents zero" rule, with zero page errors.
- **`tmp/e2e_settings_matrix.py`** `L3` — the seven top-level keys including
  `currency` and `fx_rate` round-trip, and `/api/usage` answers with the
  `display` block and the CNY native path intact.
- **Standing gates**: `python3 -m mycore.tests.run` → **2859/2859** ·
  `python3 tests/web_console_p3.py` → **610/610** · locale packs
  **1827** keys per pack.

## Honest limits

- The automatic rate comes from a **public third-party endpoint**
  (`open.er-api.com`, no key). It is best-effort: offline, the console shows
  unknown costs rather than a stale or invented rate. The probe verifies the
  **rendering**, not a live FX provider.
- The unit is a **display** unit. Nothing in the product's own accounting
  changes: prices, stored costs and the guardrail budget keep their source
  semantics, and a restore of an old settings document restores the old unit
  with it.
- A user who wants the guardrail budget in another currency does not have it
  — that budget is USD by design and says so.
