# I'm AiDog — Web Console

Zero-dependency (Python stdlib) status console. Concept = MCU-style status HUD
(stance panels, live bars, protocol tones, spoken advisory voice); visual
tokens extracted from aidog.im (owner-approved **style reference only** —
not an aidog.im clone).

## Run

```bash
python3 web/server.py            # http://127.0.0.1:19167/  (loopback only)
AIDOG_WEB_PORT=9000 python3 web/server.py   # custom port
```

## Console smoke tests

```bash
python3 tests/web_console_p3.py
python3 tests/web_console_p3.py --live-read-only http://127.0.0.1:19167
```

The default suite creates a temporary installation with fixture credentials,
loopback servers on ephemeral ports, a fake chat model and a stub transcriber.
It exercises state-changing endpoints, including pause and history clear, only
in that installation. Provider voice synthesis and watchdog process recycling
are outside this suite.

The explicit live probe sends unauthenticated `GET /api/health` and
`GET /api/console` requests only. It bypasses proxies and rejects redirects.
It does not read the secrets store or alter pause or history state.

## Routes

| Route | Purpose |
|---|---|
| `/` | status HUD (index.html) |
| `/api/health` | liveness + version |
| `/api/status` | panels (kernel/gateway/watchdog/model/telegram) + memory bars + sanitized config |
| `/api/config` | config presence view — **no credential values, ever** |
| `/api/settings` | GET: console settings · POST: Bearer-token validated merge (allow-list, idempotent) |
| `/api/usage` | model-channel cost/usage aggregation (cached, degrades honestly) |
| `/api/events` | sanitized recent event stream (brand/token redacted) |
| `/api/version` | release channel: installed vs origin manifest → upgrade banner |
| `/api/skills` | skill-armory gallery records (real SKILL.md frontmatter) |
| `/api/voice` | voice engine capability (presence only) |
| `POST /api/say` | speech synthesis → `audio/mpeg` (JSON-only, loopback Origin, rate-limited) |
| `/static/i18n/{en,zh}.json` | locale packs — default **en**, switchable **zh** (localStorage) |

## Security posture

- Binds `127.0.0.1` only.
- Writes: settings go through `Authorization: Bearer <WEB_TOKEN>` + an
  allow-list schema; `POST /api/say` is JSON-only + same-origin guarded +
  rate-limited (loopback feature, no bearer by design).
- Status probes are presence/boolean only; secrets never serialize.

## Roadmap

- Later: swap stdlib server for a fuller framework only if needed (keep
  zero-dep as the default for installer friendliness).
- Voice spec: `docs/features/voice_announcements.md`.
