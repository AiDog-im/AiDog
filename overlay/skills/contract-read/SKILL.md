---
name: contract-read
description: Use when you need state from a view function on a Base or Ethereum contract — totalSupply, reserves, config getters read via eth_call. Not for balances by holder (chain-balances-read), not for transactions, and no call here can change state.
---

# Contract State (Read-Only)

## Scope (Web3/finance skills Phase 1 — read-only, keyless)

This skill covers **reading verified contract state** through this
product's `chain_read` tool:

- `chain_read {chain: "base", method: "eth_call", contract: "0x…",
  data: "0x…"}` — chains `base` and `ethereum`.

`eth_call` executes against the latest state as a **simulation**: nothing
is signed, nothing is broadcast, nothing changes — and the whitelist in
`mycore.exchanges.CHAIN_READ` contains no signing or send method at all,
so no call from this product can ever become a transaction. No credential
is involved.

## Calling the tool

| Parameter | Grammar (closed) |
|---|---|
| `contract` | `0x` + exactly 40 hex chars |
| `data` | `0x` + even-length hex, 1–2048 bytes |

- **Bring a 4-byte selector you can cite** (verified ABI, official docs).
  Never invent one: a wrong selector returns garbage or a revert with the
  same confidence as a correct read.
- Arguments are 32-byte words: a `uint256` argument is 64 hex chars, an
  `address` argument is left-padded to 64. No arguments → selector only
  (e.g. `0x18160ddd` = `totalSupply()`).
- The answer is `result=0x…` — decode as an **exact unsigned integer**
  when the ABI says so; a multi-value return needs the ABI's types in
  order. When unsure, quote the raw hex and say the decoding is unverified
  — never guess a decode into a number that looks authoritative.

## What this skill does NOT do

- **No balances by holder** — `chain-balances-read` /
  `token-positions-read` own that (this reads the contract, not an
  account).
- **No writes, ever** — even where a mutating function name is put in
  `data`, the call only *simulates* against latest state; this product
  never signs, so treat nothing here as an action taken.
- **No prices, no appraisal** — a price or TVL read needs a source; do
  not synthesize one from contract numbers.
- **No unverified contracts on trust** — cite where the address and
  selector came from before presenting the value.

## Common failure modes

| Symptom | Meaning / fix |
|---|---|
| `eth_call needs contract as 0x + 40 hex` | malformed contract address |
| `eth_call needs data as 0x + even-length hex, 1..2048 bytes` | selector/argument length wrong — count hex chars (2 per byte) |
| provider error line containing `revert` | the call reverted: wrong selector, wrong args, or the contract refused — a revert is data, report it, don't retry blindly |
| `result=0x` (empty) | the function returned nothing — likely a non-view function or wrong selector; verify against the ABI |
| `method must be one of …` | `eth_call` exists only on `base`/`ethereum` here; Solana has no equivalent in this whitelist |
| timeout | public RPC congestion — retry once, then report honestly |

## Required disclosures

1. The product is **permanently free**, but on-chain **gas is NOT zero** —
   and this path consumes no gas at all (it is a simulation).
2. **This is not investment advice.**
3. This path is **keyless**: no credential exists to store or echo.
