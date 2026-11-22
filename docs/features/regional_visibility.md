# Regional visibility (the `region` tier): explicit choice, honest tiers, channel equality

Scope: everything car I (owner item **#16**, 2026-10-31; rulings 2026-11-03)
ships for mainland-China adaptation — the `region` settings partition, the
feature-visibility tiers it drives, the activation requiredness change, and
the channel-equality pass. Design decisions and full construction log live in
`docs/design/regional_car_design.md`; this document is the capability
contract.

## 1. The region setting

- `web/settings.json` → `region: {code: "global" | "cn"}`. Default `global`
  = the shipped behavior **byte-for-byte**; an absent block reads as global.
- Grammar lives in `web/server.py` (`DEFAULT_SETTINGS` / `ALLOWED_TOP` /
  `REGION_CODES` / `validate_settings_patch`): one key, two enum values,
  all-or-nothing 400s that name `region.code` and never echo a value.
- **It is always an explicit owner choice** (owner ruling 2026-11-03, Q1):
  - the console **Region** card (Settings → General) at any time;
  - one optional question in the interactive `aidog setup` wizard
    (ENTER = keep current = global; non-interactive `--check` / `--yes`
    never prompt and never write region).
- **No detection of any kind**: no geo-IP lookup, no locale/timezone
  sniffing, no outbound request learns or suggests the value. The setting
  never leaves the machine.

## 2. What `cn` changes (the tiers)

| Face | `global` (default) | `cn` |
|---|---|---|
| Exchange/Web3 panel | today's behavior (`exNav` auto/show/hide rule) | **hidden entirely** — nav item never shows, `setView("exchange")` resolves to Overview (deep links, journey buttons, orchestrator jumps), the personal `exNav=show` preference is outranked |
| Restricted channel rows (`telegram`, `discord`, `slack`, `whatsapp`, `signal`) | plain rows | **dimmed + one honest note**: *"may need a network route you provide yourself in this region"* (deliberately "may" — the product claims no per-route certainty) |
| Domestic/neutral rows (`dingtalk`, `feishu`, `wechat`, `email`, `webhook`) | plain rows | unchanged |
| Activation requiredness | `WEB_TOKEN` + `MYCORE_TG_TOKEN` + `AIDOG_TG_ALLOWED_USERS` + reachable model endpoint | the **two Telegram slots drop out** (`TG_SLOTS_OPTIONAL_UNDER_CN`) — a Feishu/DingTalk-only install reaches READY; everything else unchanged |
| Activation welcome copy | neutral 3-step line (step 2 = connect a chat channel) | `act.start.cn` variant — names Feishu/DingTalk/WeChat before Telegram |
| Bot & Service card | slots + restart only | adds one honest note (same route wording); the card is never hidden — it owns real slots |

The console Region card states exactly this list when `cn` is selected — the
single "honest line" left behind for the hidden panel (owner ruling Q3).

## 3. Channel equality (the Q4 pass)

- The **Channels card shows a `telegram` row** with the same visual
  treatment as the nine adapter rows: state words only (`GET /api/setup`
  presence → chip), no value, clicking the row focuses the Bot & Service
  card where the slots actually live. It is the missing tenth row — TG used
  to live only in its own card, which is the "default bot" asymmetry the
  owner named.
- Neutral copy, both locales, global effect: `act.start` step 2, the three
  cron hints (`cron.createHint` / `cron.hint` / `cron.empty`), and the
  marketing site (`hero.lead`, `start.sub`, `start.step3Body`,
  `plat.mobile`, `caps.c6B`, `console.f3T`, meta description).
- Facts stay facts: `onb.tgStep*` (the TG-specific swap procedure),
  `act.item.tg_*` slot labels, the TELEGRAM services status row and
  watchdog-alert wording are unchanged — they describe real mechanisms, and
  probes pin them.

## 4. Boundaries (what this feature does NOT do)

- **No capability removal.** A tier changes visibility and requiredness
  only; every API, tool and adapter keeps working. The console surface is
  the tiered face — the local single-owner APIs are not region-blocked
  (the owner's machine is the trust boundary; nothing here is a network
  control).
- **No geo claims.** "Unavailable in your region" is never asserted as a
  legal verdict; restricted rows say they *may* need a route the operator
  provides.
- **No telemetry.** Zero egress is a property of the feature itself, not a
  promise: no code path in this car reads or sends the region.
- **Free/single-owner/approval gates untouched**; region is a plain enum
  (never a credential — it is safe under the `settings get` full-echo rule).

## 5. Config surface

| Surface | Path |
|---|---|
| Stored value | `web/settings.json` → `region.code` (gitignored, machine-local) |
| Console | Settings → General → **Region** card (select + sticky SAVE) |
| Wizard | `aidog setup` interactive prompt after the slots; `--check` prints `settings region: …` and derives pending from it |
| API | `POST /api/settings {region:{code}}` (Bearer), `GET /api/settings` (merged default), `GET /api/setup` (per-region `required` flags) |
| Client engine | `web/static/app.js`: `regionCode()` → `applyRegion()` / `paintRegionNotes()` / `paintChanTgRow()`; the Exchange gate is inside `exNavVisible()` + `setView()` |

## 6. Gates

- `python3 tmp/e2e_car_i.py` — API contract: grammar accept/refuse pairs,
  per-region `GET /api/setup` tiers (non-vacuous both ways), wizard mirror
  constants, capabilities never serializes the block.
- `node tmp/e2e_car_i_ui.js` — the UI tiers end to end on an isolated
  console (preview → save → reload gate → deep-link walk-off → dim set →
  TG row → welcome variant → full restore).
- `python3 tests/web_console_p3.py` — region grammar legs (400 pairs +
  read-back + setup-tier sanity) among the smoke suite.
- `python3 tests/setup_wizard_b3.py` — region prompt legs (write + retry +
  keep, `--check` pending under both regions).
