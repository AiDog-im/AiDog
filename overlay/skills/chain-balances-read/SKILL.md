---
name: chain-balances-read
description: Use when you need the native balance of one address on Base, Ethereum or Solana — wallet checks, portfolio snapshots, checking how much an address holds. Not for token-level holdings (token-positions-read), not for prices or valuation, not for moving funds.
---

# Chain Balances (Read-Only)

## Scope (Web3/finance skills Phase 1 — read-only, keyless)

This skill covers **reading native balances** through this product's
`chain_read` tool, implemented in `mycore/exchanges.py`:

- `chain_read {chain: "base", method: "eth_getBalance", address: "0x…"}`
  — EVM balance in **wei** (`base`, `ethereum`).
- `chain_read {chain: "solana", method: "getBalance", address: "…"}`
  — Solana balance in **lamports**, with the answering context slot.
- `chain_read {chain: "base", method: "eth_blockNumber"}` /
  `chain_read {chain: "solana", method: "getSlot"}` — chain head, when all
  you need is liveness.

**No signing and no broadcasting exists on this path.** The method
whitelist in `mycore.exchanges.CHAIN_READ` admits read methods only, and
the core suite pins that absence against the source file. There is no
credential anywhere on this path: the endpoints are public and keyless.

## Calling the tool

| Parameter | Grammar (closed — the error names the rule) |
|---|---|
| `chain` | one of `base`, `ethereum`, `solana` |
| `method` | must belong to that chain's whitelist |
| `address` (EVM) | `0x` + exactly 40 hex chars |
| `address` (Solana) | base58, 32–44 chars |

- Output is one compact line per value, capped at 40 rows — cite it, don't
  dump it.
- **Integers are exact.** `balance=17735394826886630085 wei` is the raw
  chain value: never round it, never convert it with float arithmetic, and
  keep the unit in the sentence (wei / lamports).
- A zero balance is a real zero (`balance=0 wei`), not an empty result; a
  missing or malformed result is an honest error line instead — never a
  guessed number.

## What this skill does NOT do

- **No prices, no valuation** — no price source exists on this path; never
  multiply a balance by a remembered price.
- **No token-level holdings** — use `token-positions-read`.
- **No NFTs, no transaction history** — separate skills; EVM
  address-scoped history is honestly unavailable keyless (see
  `tx-history-read`).
- **Nothing ever moves.** If asked to send, swap, bridge or approve
  something: decline the write — no setting permits it, and hunting for one
  is the wrong move.

## Common failure modes

| Symptom | Meaning / fix |
|---|---|
| `chain must be one of base\|ethereum\|solana` | typo in `chain` — the grammar is closed on purpose |
| `method must be one of …` | that method is not on that chain's whitelist — use a name from the list in the error |
| `needs address as 0x + 40 hex` / `needs a base58 address` | malformed address (checksum casing is fine, length is not) — fix the value, don't guess |
| `… unexpected balance …` | upstream shape changed or a gateway intercepted — report it as-is, never invent a number |
| provider error line (bounded to 160 chars) | the endpoint's own message (rate limit, bad request) — read it literally; no credential can appear in it because none exists |
| timeout | public RPC congestion — retry once, then report the failure honestly |

## Required disclosures

1. The product is **permanently free**, but on-chain **gas is NOT zero** —
   the user settles it with the network.
2. **This is not investment advice.**
3. This path is **keyless**: there is no credential to store, echo or
   leak — slot names never appear here because no slot exists.
