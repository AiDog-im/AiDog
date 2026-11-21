---
name: outbound-proxy
description: Use when aidog's own http_fetch requests must leave through a proxy you host yourself — configuring the outbound_proxy settings block, testing the endpoint, or diagnosing refused/timeout/DNS failures. Not for proxying every app on the machine, borrowing someone else's endpoint, or reverse-engineering any client.
metadata:
  version: 1.0.0
  author: aidog
compatibility: Optional product seam: needs the outbound_proxy settings block; any self-hosted HTTP(S) proxy endpoint you control.
---

# outbound-proxy — aidog's own outbound traffic through YOUR proxy

## When to use / when NOT to use

Use when:

- aidog's `http_fetch` (web reads, research, catalog checks) is blocked on
  your network, or must appear from a specific network you control;
- you RUN a proxy endpoint of your own (home server, VPS, LAN box) and want
  aidog's fetches to leave through it;
- a fetch failed and you need to tell "proxy down" from "target down" from
  "DNS broken".

Do NOT use when:

- you want EVERY app on the machine proxied — that is an OS/application
  setting, outside aidog by design;
- you want to borrow someone else's endpoint or reverse-engineer a client —
  rejected by ruling (legal + liability), not technical preference;
- you need chat/model, Telegram, or deploy traffic proxied — this seam
  covers `http_fetch` only (scope below is honest).

## Honest scope (read before enabling)

- **Opt-in, default direct.** With `enabled: false` (the shipped default)
  behavior is byte-identical to a stock install.
- **Only aidog's own `http_fetch` requests** route through the proxy. Model
  calls, the Telegram channel, site deploys and other apps do NOT.
- **Config stays local** — the URL lives in your settings file; it is never
  uploaded, never logged, never echoed back (reports are words only).
- **Fail-closed when enabled.** If the proxy refuses or times out, the
  request is NOT silently sent direct; you get an honest error word. Your
  policy said "leave by the proxy" — a direct fallback would betray it. To
  go direct again, disable the block.
- **Ambient `NO_PROXY` keeps its standard meaning**: hosts it exempts
  (typically `127.0.0.1` / localhost) stay direct — that env is your own
  machine's choice, and proxying loopback is never useful. Every other
  host obeys the fail-closed rule above.

## Configure (console or API)

Console: **Settings → Outbound proxy** card — toggle + URL + **TEST**,
saved through the normal settings save.

Or `POST /api/settings` (Bearer WEB_TOKEN):

```json
{"outbound_proxy": {"enabled": true, "url": "http://127.0.0.1:7890"}}
```

URL forms:

- `http://host:port` — plain HTTP proxy;
- `https://host:port` — TLS to the proxy itself.

Only `http://` / `https://` proxy URLs are accepted (stdlib transport;
SOCKS is not supported — said honestly rather than half-working).

**Never embed credentials in the URL.** The settings file carries no
credentials by design (they would otherwise reach the model-facing
`settings get`). If your proxy requires authentication, set the
**`AIDOG_PROXY_AUTH` slot** — console: the credential row on this card —
with the value `user:pass`. The credential rides the proxy hop only and is
never echoed anywhere. One honest limitation: stdlib cannot carry that
credential on an `https://` CONNECT tunnel without delivering it to the
origin, so an `https://` fetch through an AUTHENTICATED proxy refuses with
the `auth` word — test with an `http://` target instead (the diagnostics
below do exactly that).

## Test

- Console: the **TEST** button on the Outbound proxy card.
- API: `POST /api/outbound_proxy/test` (Bearer WEB_TOKEN) →
  `{"ok": true, "ms": 412}` or `{"ok": false, "reason_word": "timeout"}`.

The test fetches an owned endpoint through the proxy with an 8-second
ceiling; the URL is never echoed. That endpoint is `https://`, so with
the `AIDOG_PROXY_AUTH` slot armed the test answers the honest word `auth`
by design (see the limitation above) — verify those proxies with an
`http://` target, e.g. `POST /api/outbound_proxy/test {"target":
"http://example.com/"}` or the curl checks below.

## Diagnostics (run_bash)

```bash
# 1. proxy reachable and CONNECT-capable (ANY HTTP code means the tunnel works)
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 8 -x http://127.0.0.1:7890 https://aidog.im/

# 2. same target direct (direct failing + proxied working = the seam is doing its job)
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 8 https://aidog.im/

# 3. is the proxy port even listening? (python3 runs wherever aidog runs)
python3 -c "import socket; s=socket.socket(); s.settimeout(3); print('open' if s.connect_ex(('127.0.0.1', 7890))==0 else 'closed')"

# 4. DNS for the TARGET (not the proxy) — a broken resolver looks like a proxy bug
python3 -c "import socket; print(socket.gethostbyname('aidog.im'))"
```

Reading the failure words:

| word | meaning | first fix |
|---|---|---|
| `bad_url` | settings URL malformed, or it embeds credentials | scheme must be `http://`/`https://`; auth goes in the `AIDOG_PROXY_AUTH` slot |
| `auth` | the proxy needs credentials, or the target is `https://` while the auth slot is armed | set `user:pass` in `AIDOG_PROXY_AUTH`; test with an `http://` target |
| `refused` | nothing accepted on that host:port | is the proxy running? right port? |
| `timeout` | no answer within the ceiling | firewall/NAT, or proxy overloaded |
| `tls` | TLS to the proxy failed | you wrote `https://` but the proxy is plain `http://` |
| `dns` | target name did not resolve | fix resolvers on THIS machine |
| `unreachable` | anything else on the proxy path | proxy logs + the curl checks above |

## Standing up your own endpoint (self-hosted only)

- Any HTTP proxy software you legitimately run on hardware you control —
  your OS's, your router's, a container on your own server. It must accept
  `CONNECT` (that is how `https://` targets tunnel).
- Expose it only where aidog runs: localhost, LAN, or an IP allow-list; if
  it leaves your machine, require auth (`user:pass@` in the URL) and a
  firewall.
- aidog never ships, recommends, or automates anyone else's service. No
  borrowed subscriptions, no shared exits with strangers, no reverse-
  engineered clients — by ruling, not by accident.

## Red lines

- Never echo or log the full proxy URL (userinfo is a credential).
  Reports are words; host-level detail only on the owner's ask.
- Installing this skill changes no permissions: spending, publishing,
  outbound messages and credential writes still pass the approval gate.
- The proxy is aidog's, not the machine's: other processes are unaffected
  by this block — by design.
