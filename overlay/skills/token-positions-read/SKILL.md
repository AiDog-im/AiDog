---
name: token-positions-read
description: Use when you need which tokens an address holds — ERC-20 balances on Base or Ethereum, SPL token accounts on Solana. Not for native balances (chain-balances-read), not for prices or valuation, not for NFTs, not for moving tokens.
---

# Token Positions (Read-Only)

## Scope (Web3/finance skills Phase 1 — read-only, keyless)

This skill covers **reading fungible token holdings** for one address
through this product's `chain_read` tool:

- **EVM (`base`, `ethereum`)** — `method: "eth_call"` against an ERC-20
  contract with `balanceOf(address)`.
- **Solana** — `method: "getTokenAccountsByOwner"` (classic SPL program;
  the output says so, and says Token-2022 is not covered).

**No signing and no broadcasting exists on this path** — the whitelist in
`mycore.exchanges.CHAIN_READ` admits reads only, and no credential is
involved (public endpoints).

## EVM: reading an ERC-20 balance

1. `contract` = the token contract (`0x` + 40 hex). Use only an address you
   can cite; a wrong address returns a wrong balance with identical
   confidence. Cheap sanity check before trusting one:
   `chain_read {chain: "base", method: "eth_call", contract: "<token>",
   data: "0x18160ddd"}` (`totalSupply`) should return a plausible supply.
   (Example verified 2026-11-05: Base WETH `0x4200000000000000000000000000000000000006`.)
2. `data` = `0x70a08231` (the public ERC-20 `balanceOf(address)` selector)
   + the holder address left-padded to 32 bytes (64 hex chars). For holder
   `0x1111…1111`:

   ```
   0x70a082310000000000000000000000001111111111111111111111111111111111111111
   ```

3. The answer is `result=0x…` — decode it as an **exact unsigned
   integer** (base units). Convert to a human unit only when you have the
   token's `decimals` from a source you can cite; otherwise state base
   units. For large numbers, quote the raw hex alongside the decimal so
   the transcript stays checkable.

## Solana: reading SPL token accounts

- `chain_read {chain: "solana", method: "getTokenAccountsByOwner",
  address: "<owner base58>"}` returns lines like
  `mint=… amount=157145.478888 decimals=6 account=…`.
- The header carries the **true total** and states how many rows are
  shown (output is capped) — never present the shown rows as the whole
  holdings when the header says otherwise.
- Dust and spam tokens appear as they are; do not filter them silently.

## What this skill does NOT do

- **No prices, no valuation** — no price source here; never multiply a
  balance by a remembered price, never produce a portfolio total you
  cannot source.
- **No native balances** — `chain-balances-read`.
- **No NFTs** — this reads fungible token accounts only.
- **Nothing ever moves.** Transfer, swap, approve, bridge: decline the
  write; no setting permits it.

## Common failure modes

| Symptom | Meaning / fix |
|---|---|
| `eth_call needs contract as 0x + 40 hex` | malformed contract address |
| `eth_call needs data as 0x + even-length hex, 1..2048 bytes` | calldata wrong: selector is 8 hex chars, each argument word is 64 — count them |
| `result=0x0` | genuinely zero holdings **or** a wrong contract/selector — verify the contract with `totalSupply` before reporting zero |
| `… Token-2022 not covered` | deliberate scope limit of the classic SPL program — say so, don't imply completeness |
| provider error line (bounded) | the endpoint's own message, read literally; rate limits mean back off, not retry-storm |
| `no token accounts for that address (honest empty result, not an error)` | real empty — report it as empty, not as a failure and not as data |

## Required disclosures

1. The product is **permanently free**, but on-chain **gas is NOT zero** —
   the user settles it with the network.
2. **This is not investment advice.**
3. This path is **keyless**: no credential exists to store or echo.
