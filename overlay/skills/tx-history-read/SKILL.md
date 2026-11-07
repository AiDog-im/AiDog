---
name: tx-history-read
description: Use when you need a transaction receipt by hash on Base or Ethereum, or the recent signature history of a Solana address. Not for EVM address-scoped history — no keyless source exists here, so say so honestly — not for balances.
---

# Transaction History (Read-Only)

## Scope (Web3/finance skills Phase 1 — read-only, keyless)

This skill covers **receipts and signature history** through this
product's `chain_read` tool:

- `chain_read {chain: "base", method: "eth_getTransactionReceipt",
  tx_hash: "0x…"}` — receipts on `base` and `ethereum`.
- `chain_read {chain: "solana", method: "getSignaturesForAddress",
  address: "…"}` — the latest signatures involving a Solana address
  (limit 20, newest first).

**No signing and no broadcasting exists on this path** — reads only, no
credential, public endpoints.

## Reading a receipt

- `tx_hash` grammar: `0x` + exactly 64 hex chars.
- The head line carries exact values: `block=26344802 status=success
  from=0x… to=0x… value_wei=0 gas_used=1678313 logs=6`, followed by up to
  39 `log[i] address=… topic0=…` lines (output is capped at 40 rows).
- `status=success` / `status=failed` comes from the chain's own status
  field; `gas_used` and `value_wei` are exact integers — never round them.
- `receipt not found for that hash` is an honest answer: not yet mined,
  wrong hash, or wrong chain. Report it as such — never as an empty
  history.

## Reading Solana signature history

- Lines look like `sig=… slot=465489519 err=none time=1793835619`.
- `err=none` = the transaction succeeded on-chain; any other JSON in `err`
  is the chain's own error detail (e.g. an `InstructionError`) — quote it,
  don't paraphrase it into "failed" alone.
- `time=-` means the entry carries no block time; the header states the
  true count and the limit (20).

## What is honestly NOT available

**EVM address-scoped transaction history** (every transaction involving an
`0x…` address) has **no keyless source in this product**: chain RPCs
cannot enumerate an address's transactions, and no explorer/indexer
endpoint has been verified for this track (owner ruling D3: no credential
slot may be added without asking, and unverified sources do not ship).

If asked for it, answer exactly that — *not available with keyless sources
on this chain* — and offer what IS available (receipt by hash, Solana
signature history). Never present an empty list as history, and never
route around the rule by fetching an unverified explorer with `http_fetch`.

## Common failure modes

| Symptom | Meaning / fix |
|---|---|
| `eth_getTransactionReceipt needs tx_hash as 0x + 64 hex` | malformed hash — usually a truncated paste |
| `receipt not found for that hash` | not mined / wrong hash / wrong chain — say which is unknown, don't guess |
| `method must be one of …` | receipts exist on `base`/`ethereum`; `getSignaturesForAddress` only on `solana` |
| `no signatures for that address (honest empty result, not an error)` | real empty — report as empty |
| provider error line (bounded to 160 chars) | the endpoint's own message (rate limit, etc.), read literally |
| someone demands full EVM wallet history | point at the scope limit above; a paid explorer key would need the owner's explicit approval first |

## Required disclosures

1. The product is **permanently free**, but on-chain **gas is NOT zero** —
   the user settles it with the network.
2. **This is not investment advice.**
3. This path is **keyless**: no credential exists to store or echo.
