# Observed key scope — asking the venue what the key may really do

Status: implemented in the development tree (not yet released).

Audit item #11, candidate 1 ("key-scope display"). The product asks a venue
for READ access. Until now the console showed only what the product
**requested** — the scope checklist per provider and the read-only
TEST CONNECTION probe. What the venue actually **granted** was invisible,
so a key that had been widened to trading (or to withdrawal) looked exactly
like one that had not.

This feature puts the two side by side: the venue is asked, with the
owner's own key, what that key is granted, and the answer is shown next to
what the product uses.

## What it is

- `mycore/exchange_scope.py` — one module, one job.
- `GET /api/exchange/scope?venue=<key>` — the console's read surface. Its
  own sliding window (6 probes / 60 s), separate from the market-quote and
  speech budgets.
- A per-venue **KEY SCOPE** row inside the Exchange view's venue cards,
  with a CHECK SCOPE button. The six keyless sources (Jupiter, Velora,
  Hyperliquid, dYdX, GMX, public RPC) have no account and therefore no
  scope to observe — they get no row, not an empty one.

## The three queries (the whole module's surface)

| Venue | Endpoint | What it reports |
|---|---|---|
| OKX | `GET /api/v5/account/config` | `perm` (READ_ONLY / TRADE / …), `permDisp`, level, account level, label |
| Binance | `GET /sapi/v1/account/apiRestrictions` | `enableReading`, `enableWithdrawals`, `enableSpotAndMarginTrading`, `enableFutures`, `enableMargin`, `enableInternalTransfer`, `permits[]` |
| Kraken | `POST /0/private/QueryApiKeys` | `readOnly`, `permissions[]` |

Kraken does **not** recognize that method on the public API today (it
answers `EGeneral:Unknown method`), so the honest answer for Kraken is
`not_observable` — the product never guesses what a key may do. The call is
still attempted, so if a key type gains the endpoint it starts reporting
real grants without a code change.

## Red lines

- **Read-only by construction.** These three account-permission queries are
  the module's entire network surface. There is no order path, no transfer
  path, no withdrawal path — asserted on the source: every path literal in
  the module must be one of the three endpoints, and the only names it may
  import from `mycore.exchanges` are the read-only helpers.
- **The key never leaves the module.** Only whitelisted, non-identifying
  fields are returned. A uid, an IP whitelist, a timestamp, a balance or a
  slot value is dropped at the boundary; the response is names and
  booleans.
- **Nothing is sent when there is nothing to send.** Without a configured
  key the probe reports `not_configured` and issues **no request** — it does
  not probe a vendor with a half-configured credential.
- **Honest degradation.** A malformed secret, a dead upstream or an
  unrecognized method degrades to an `error` / `not_observable` row; the
  console never raises.
- **Local facts only.** The only outbound traffic is to the venue's own
  documented API, with the venue's own key — the same pattern as the
  existing TEST CONNECTION probe. No telemetry, no third party.

## The requested side

`REQUESTED` states, per venue, what the product actually uses the key for
(balances, positions, orders) and how an order ever happens:
`prepare -> owner approval -> execute`. Read access is sufficient for
everything the product does on its own.

## Flags

A grant wider than the product's own use is flagged, not blocked:

- `trading-grant` — the key can trade, while the product only reads (and
  only ever places one owner-approved order at a time).
- `withdraw-grant` — the key can move funds out.

The flag is a **nudge**, never an action: the key may be there for the
owner's own manual trading. What the flag says is that the key is wider
than the product needs, which is worth knowing.

## Honest limits

- The three venues' endpoint shapes were confirmed against the vendors'
  public documentation and by unauthenticated calls (each endpoint
  recognized the request and answered an auth error). The **response shapes
  under a real key were not observed on this machine** — it holds no
  exchange credentials. The path is proven end to end against a local fake
  vendor (real HTTP, real HMAC signing, real whitelist reduction), not
  against a live venue.
- Kraken's scope endpoint is not publicly reachable; the module reports
  `not_observable` rather than inventing a verdict.
