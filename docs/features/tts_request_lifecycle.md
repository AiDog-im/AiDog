# TTS request lifecycle — the AiDog side

Status: shipped in **v0.5.103** (2026-11-22). Incident record:
[../design/tts_request_lifecycle_incident.md](../design/tts_request_lifecycle_incident.md)
(2026-11-21). Companion pages: [tts_engines.md](tts_engines.md) (the engine
matrix and the selection grammar), [voice_announcements.md](voice_announcements.md)
(the advisory speech chain), [voice_session.md](voice_session.md) (spoken
replies). Server: `web/server.py::_tts_byok_cfg`, `_synth_say_byok`,
`_handle_say` · client: `web/static/app.js::speakLocal`, `speak`.

**What happened.** The console access log recorded the smoke suite's explicit
BYOK speech request returning **503** at 17:11:15 and 17:21:11. Two product
defects sat behind it: the BYOK leg **inherited the chat endpoint** when its
own endpoint was empty (sending the speech request to the shared chat
gateway), and the smoke suite submitted a **real synthesis on every run**
with no opt-in. This page documents the product-side corrections.

**Scope.** Everything here is the **AiDog side**. The shared gateway is a
different system: it was repaired separately under an explicit owner
authorization, and this car changed nothing in it.

## The BYOK configuration (corrected)

| Item | Source (first non-empty wins) |
|---|---|
| base URL | env `AIDOG_TTS_URL` → settings `media.tts_base_url` |
| key | slot `TTS_API_KEY` only |

**The chat endpoint is never inherited.** `settings.mycore.base_url` is no
longer consulted for speech, and `MODEL_API_KEY` is no longer a fallback for
`TTS_API_KEY`. A BYOK request with no dedicated endpoint is **unconfigured**
and fails closed without touching any network.

- A **loopback** base may still run keyless (the same policy image
  generation applies to local gateways); any **remote** endpoint without the
  `TTS_API_KEY` slot is fail-closed (`byok: false` in the capability block).
- The voice endpoint field's placeholder says so in both locales:
  *required for BYOK; never inherits chat endpoint*.

## The request lifecycle

| Rule | Behaviour |
|---|---|
| **Single-flight** | one BYOK synthesis at a time. A second overlapping request is refused immediately (`byok tts already in progress`) — it does not queue behind the first. The lock is released on every path, including a timeout and an invalid audio answer |
| **Cooldown after an upstream error** | after an upstream HTTP error the next request is refused for **30 s** (**60 s** after a 429); a numeric `Retry-After` on a 429/503 is honoured, capped at **one hour**. A transport failure (DNS, timeout, refused) and an invalid audio response also impose a 30 s cooldown |
| **No retry** | the product never re-sends a failed synthesis. The caller gets the honest failure and decides |
| **No upstream body relayed** | the error is `byok tts upstream HTTP <code>` — the upstream's response body is **not** echoed, not sanitized-and-truncated, not shown. A transport failure reports the **exception class** only (`byok tts unreachable: TimeoutError`) |
| **No silent fallback** | a BYOK failure answers **503** with the real reason. The product never pretends the edge engine spoke |

The audio contract is unchanged and still enforced: the response must be
`audio/*`, and it is read under the 5 MB cap with a 25 s timeout.

## Browser notices use on-device voices only

The console's **automated** spoken notices — the voice greeting, the
advisory level line and the urgent-intervention line — now use **only browser
voices marked `localService`**:

- no on-device voice for the page language → the notice is **skipped**
  (no server synthesis, no network call, no silent substitute);
- the UI sound switch still gates the greeting and the microphone reply
  speech;
- an **explicit microphone reply** may still use the configured engine — the
  on-device rule applies to the automated notices, not to a reply the owner
  asked for.

## The smoke suite speaks only when asked

Real speech synthesis consumes provider quota, so it is now **opt-in**:

- `AIDOG_SMOKE_LIVE_VOICE=edge|byok` runs **one** request against the
  explicitly selected engine and asserts `200` + an `audio/*` content type +
  a non-trivial body;
- with the variable unset, the leg prints `SKIP live voice synthesis
  (AIDOG_SMOKE_LIVE_VOICE=edge|byok)` and **no synthesis happens**;
- any other value fails the run rather than being ignored.

This does **not** make the whole smoke suite offline — it still contains real
chat and other integration operations.

## Evidence

- **`tests/tts_lifecycle_regression.py`** — **12 isolated tests**, no real
  upstream and no credential reads (the transport is denied by default, so an
  accidental call fails the test):
  - the chat endpoint alone never causes an upstream call, and the model key
    is not inherited;
  - the dedicated env URL and the dedicated key are used when configured;
  - a loopback keyless success returns audio, and the request shape is
    checked;
  - a 429 with a `Retry-After` sets a cooldown and the request is refused
    during it, then recovers;
  - the single-flight lock rejects a second concurrent request;
  - a timeout performs **no retry** and still releases the lock;
  - an invalid audio response cools down and releases the lock;
  - the handler answers 503 with **no upstream call** when BYOK is
    unconfigured, serves the audio on success, and maps an upstream 429 to a
    503 with the cooldown set;
  - the browser behaviour is checked in an isolated Node VM.
- **`tests/web_console_p3.py`** — the live-voice leg is now gated by
  `AIDOG_SMOKE_LIVE_VOICE` and skips by default; the `/api/say` validation
  matrix (bad content type → 415, cross-origin → 403, bad engine → 400,
  bad lang → 400) is unchanged.
- **Standing gates**: `python3 -m mycore.tests.run` → **2859/2859** ·
  `python3 tests/web_console_p3.py` → **610/610** · locale packs **1827** keys
  per pack.

## Honest limits

- The cooldown is **process-local** and in memory: a console restart forgets
  it. It bounds this product's own retries, not the upstream's quota.
- The single-flight lock covers **this console process**. Another process
  (the Telegram service) has its own state.
- The upstream error code is relayed, the upstream **body** is not. That is
  a deliberate loss of detail in exchange for not passing an unknown
  provider payload to the console.
- The incident record's gateway-side observations (active TTS labels, group
  account availability, upstream success/failure counters) are **aggregate
  observations**, not proof that any particular model or upstream is usable.
  Upstream account quotas and provider-side cooldowns cannot be reset by
  this local fix.
- No shared-gateway runtime configuration was changed by this car, and none
  may be changed without the owner's explicit maintenance-window approval.
