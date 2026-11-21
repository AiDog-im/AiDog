# C3 — Security hardening: output-side masking and a narrow shell hard block

Status: implemented in the development tree (not yet released).

The gap this closes (audit items #16 / B-3 and #17 / B-4, from the
2026-11-09 audit plan in the private citation vault): the slot discipline
stops a secret from being **injected**, but nothing stopped a credential that
`run_bash` or a file read picked up from flowing back **out** in a reply, a
channel message or a mission report; and `run_bash` had no hard block on the
handful of command shapes that pipe a download into a privileged shell or
wipe a system path.

## L1 — output-side secret masking (`mycore/redact.py`)

A narrow, high-confidence shape set runs over **outbound text** only:

| Shape | Example |
|---|---|
| openai-like key | `sk-…` |
| github token | `ghp_…` |
| slack token | `xoxb-…` |
| aws access key id | `AKIA…` |
| azure storage key | `AccountKey=…` |
| connection-string password | `password=…` |
| assigned secret literal | `api_key = …`, `token: …` |
| private key block | `-----BEGIN … PRIVATE KEY-----` |
| telegram bot token | `123456:AA…` |
| jwt | `eyJ….….…` |

Rules:

- the mask replaces the VALUE and keeps the shape (`sk-***`), so the owner
  still sees that a credential was there;
- the matched value is never printed, logged or returned; the count is the
  only fact that leaves;
- the outbound points are the three that exist: the Telegram send
  (`ChatService._send`), the shared outbound fallback chain
  (`ChatService._origin_deliver` → channel / cron / mission) and the console
  chat reply (`web/server.py`).
- the model's INPUT is untouched (masking what the model reads would break
  legitimate work), and `run_bash` keeps its declared trust boundary — the
  2026-10-31 ruling holds: we never pretend the shell is constrained.
- a shape that does not match is left alone; the matcher never guesses.

## L2 — a narrow hard block on high-risk shell shapes

`run_bash` refuses these shapes fail-closed, with a reason and a pointer
to the human:

- a download piped into a shell (`curl … | sh`, `curl … | sudo bash`);
- a download piped into a privileged shell;
- a recursive force delete of `/`, `~` or `$HOME` (and the `sudo` variant);
- a write into `/etc` (a redirect or `tee` — reading `/etc` is fine);
- formatting a device (`mkfs`, `dd of=/dev/…`);
- `chmod 777 /`;
- shutting the machine down;
- erasing shell history or system logs.

The refusal text says plainly that **`run_bash` is not a sandbox** and that
the owner should run the command themselves or narrow it. The list is
deliberately narrow: `rm -rf ./build`, `cat /etc/hostname`,
`curl … | jq`, `echo x > /tmp/x` and the rest of an ordinary day still run.

## Scope ruling for #11 (preventive detection)

- **Candidate 1 — key-scope display** (observed provider grants vs the
  requested ones): **deferred**. It needs per-venue scope endpoints and a
  design pass per venue; it is not a generic mechanism, and a half-measure
  here would show a partial truth.
- **Candidate 2 — an unexpected-activity prompt** (a bounded local
  baseline of executed orders vs today): **its own increment**. The D2 ledger
  already records the executed orders per day; what it still needs is a
  decided baseline window and threshold, and that is a product decision, not
  an implementation detail.

Both stay inside the standing red lines: local facts only, no telemetry, no
automatic remediation, the human decides.

**Candidate 2 landed** (`26dd76d`): `mycore/exchanges.py::activity_baseline`
reads the order ledger (the same Beijing-day rows the auto-approval caps
use) and compares today's count + notional against the MAXIMUM of the
trailing days (default 7, clamped 1..30, today excluded). The console shows
ONE line on the Exchange panel's ORDER APPROVAL card when the baseline is
exceeded; with no earlier activity the baseline is an honest zero and
nothing is shown; a corrupt store degrades to the same honest zero. It is a
nudge: nothing is blocked, reversed or reported, and the product makes no
network call. It lives on the Exchange panel (the fact is about trading
activity) rather than the SECURITY card (credential/permission health).

## Test evidence

`mycore/tests/run.py::test_c3_security` (13 assertions): every high-risk
shape has a reason; ordinary commands are not refused; the refusal text
says "not a sandbox"; a secret shape is masked and the value never appears
in the output; a normal reply and a plain word ("token count is 12") are
untouched; a non-string input never raises.
