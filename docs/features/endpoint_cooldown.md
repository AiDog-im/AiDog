# Endpoint cooldown — a per-slot observation in the console's own process

Status: shipped in **v0.5.103** (2026-11-22). Companion pages:
[model_failover.md](model_failover.md) (the failover policy and the health
registry), [model_gateway.md](model_gateway.md) (the endpoint editor),
[display_currency.md](display_currency.md) (the console's other read faces).
Server: `web/server.py::collect_endpoint_cooldown` · route:
`GET /api/mycore/endpoints?view=cooldown` · client: `web/static/app.js::
refreshEndpointCooldown`, `renderEndpointCooldown`.

**A cooldown you cannot see is a cooldown you cannot act on.** The failover
policy cools a key slot after repeated failures — but until this car the
console showed only the *configured* chain, never the state of the cooling.
This is the read face of that state, and it is deliberately narrow: it shows
**what this console process observed**, nothing else.

## The contract

`GET /api/mycore/endpoints?view=cooldown` — loopback, no auth, and the
console's standard `Cache-Control: no-store`.

```json
{"scope": "console-process",
 "available": true,
 "endpoints": [
   {"index": 1, "slots": [{"index": 1, "label": "MODEL_API_KEY", "state": "not-cooling"}]},
   {"index": 2, "slots": [{"index": 1, "label": "MODEL_UP_K1",
                           "state": "cooling", "remaining_s": 30.0}]}]}
```

- **Positions are 1-based.** Endpoint `1` is the primary route; index `2` and
  beyond are the configured upstreams, in chain order.
- **Slot positions** are 1-based inside their endpoint. A slot carries a
  `label` **only when it is a catalog model slot** (`MODEL_API_KEY`,
  `OPENAI_API_TOKEN`, `MODEL_UP_K1`…`MODEL_UP_K8`). The legacy operator-level
  upstream grammar allows arbitrary slot names, and such a name is **not**
  echoed — the position is shown instead.
- **Three states, and they mean three different things:**

| State | Meaning |
|---|---|
| `unobserved` | this process has no record for that unit — **not** a verdict about the endpoint |
| `not-cooling` | the unit was observed and is not cooling — **not** a health claim either |
| `cooling` | the unit is in its backoff window; `remaining_s` carries the seconds left at capture time |

- `available: false` with an empty `endpoints` list means the chain could not
  be resolved in this process. The console then renders *Cooldown snapshot
  unavailable* — never an empty, healthy-looking list.
- When the configuration cannot be read, the route answers the same honest
  shape; the failure is written to stderr as an exception **class name only**.

## What it deliberately does not do

- **No probing.** Reading the snapshot never calls a provider. The route
  never touches the endpoint editor, the status probes or the credential
  reads — the regression test asserts each of those is not called.
- **No credential exposure.** The response carries no URL, no model id, no
  custom slot name and no key value — not even a length or a presence flag.
  The URL and the slot name are used **for lookup only**.
- **No mutation.** The registry is read, never written, and the cooldown
  state is not persisted (it is process-local by policy, unchanged by this
  car).
- **No health claim.** `not-cooling` means "not cooling", not "healthy".

## Scope: this console process only

The snapshot reads the **same module-level `SHARED_HEALTH` registry** that
the web chat path and the console's OpenAI-compatible handler use, so the
console sees the cooldowns its own turns caused. Per-turn `ChatClient`
instances default to that same registry.

What is **outside** the scope, and is not guessed at:

- the Telegram service, the CLI and any other process — they keep their own
  registries;
- previous console processes (a restart starts fresh);
- unsaved edits in the endpoint editor — the snapshot describes the
  **effective saved configuration**;
- provider-side health, quotas or bans.

## The console surface

Settings → Model → **Key slot cooldowns** (`<details id="endpointCooldown">`):

- the panel opens on a fresh snapshot, and **REFRESH SNAPSHOT**
  (`#endpointCooldownRefresh`) re-reads it;
- the scope note (`myc.cooldownScope`) states the process scope, that unsaved
  edits are not included, that other services are not shown, and that **this
  does not test connectivity**;
- the rows are rendered into `#modeltierBox`: the endpoint head (`PRIMARY` /
  `Upstream n`), the slot name (the label when it is a catalog slot, else
  `Slot n`), and the state (`cooling {s}s` / `not cooling` / `not observed
  yet`);
- a failed read renders *Cooldown snapshot unavailable* — an honest absence
  beats a guessed state.

The explicit connectivity test stays a separate, Bearer-gated action
(`POST /api/mycore/upstreams/test`); the cooldown panel is **passive**.

## Evidence

- **`tests/endpoint_cooldown_regression.py`** — an offline contract test with
  a real `ChatClient` and a fake transport:
  - the snapshot never calls `collect_endpoints`, `probe_endpoint`,
    `collect_status` or `_slot_present` (each is patched to fail the test if
    called);
  - the response carries no `https://`, no `password`, no `hidden`, no custom
    slot name and no key value;
  - the initial shape is exactly `unobserved` for every unit; an isolated
    `ChatClient` with its own registry and an unrelated identity do not move
    the snapshot — **another process's state is invisible**;
  - a real turn through the loop marks the slot `not-cooling`, a second
    consecutive failure marks it `cooling` with `remaining_s`, and after the
    window expires it returns to `not-cooling`;
  - the structured-endpoint configuration resolves its own inheritance
    (env wins over settings; a structured endpoint without a pool inherits
    the primary slot), and an unreadable configuration answers
    `available: false` with an empty list.
- **Standing gates**: `python3 -m mycore.tests.run` → **2859/2859** ·
  `python3 tests/web_console_p3.py` → **610/610** · locale packs **1827** keys
  per pack.

## Honest limits

- The snapshot is **point-in-time**: a cooldown can start or expire between
  two reads, and `remaining_s` is the value at capture time, not a promise.
- The scope is narrow on purpose. If the operator wants to know why the
  Telegram service is failing, this panel is not the instrument — a
  per-process registry cannot answer for another process.
- No cross-process persistence was added by this car; the failover policy
  still keeps its cooldown state in memory only.
