#!/usr/bin/env python3
"""aidog accept — the owner's one-command acceptance runner (console + Telegram).

WHAT IT WALKS
-------------
The two acceptance legs that are still waiting on a human:

  CONSOLE (Web) — read-only GETs against the LOCAL console
  (http://127.0.0.1:19167 by default, `--base` overrides it):
    * /api/health            liveness + the version the running build reports
    * /api/status            the five panels + the mycore runtime facts
    * /api/control           the leash: pause state, gate, daily budget
    * /api/usage             the cost display block (currency unit, and the
                             honesty rule: an unknown amount reads unknown,
                             never zero)
    * /api/mycore/endpoints?view=cooldown
                             the per-slot backoff observation (no credential)
    * /api/fleet, /api/fleet/plans
                             the read-only fleet board and the sign-off queue
    * /api/backup/agent      the agent-backup status face
    * /api/version           installed vs latest
    * /api/chat/history      the console session shape + context accounting

  TELEGRAM — the service that long-polls the dedicated bot. The runner
  checks the service's own state (/api/status mycore.service), the
  watchdog's per-target verdicts (service / console / model-e) from the
  watchdog state file, and the bot's configuration in the product's own
  credential store (presence only). It never sends a message: when the
  service is provisioned but the runner cannot verify a real round trip,
  that leg is labelled "needs the owner to send a message in the bot".

WHAT IT NEVER DOES
------------------
Read-only by construction: every request is a GET to the local console, no
file is written, no message is sent, nothing is restarted, and no
credential value is ever printed (only presence and the source a value
would come from).

VERDICTS
--------
PASS = the check ran and the observed state is the expected one.
FAIL = the check ran and the observed state is not acceptable.
SKIP = the check does not apply here (feature not provisioned) or could not
       be evaluated (a source did not answer). A SKIP is NOT a failure and
       does not change the exit code.

EXIT CODE
---------
0 when every applicable check passed, 1 when any check FAILED.

USAGE
-----
  python3 deploy/aidog_accept.py                # the full walk, human table
  python3 deploy/aidog_accept.py --json         # the same run, machine readable
  python3 deploy/aidog_accept.py --base http://127.0.0.1:19168
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent        # self-deriving
DEFAULT_BASE = "http://127.0.0.1:19167"               # the local console
VERSION_FILE = REPO / "VERSION"
WD_STATE = REPO / "deploy" / "watchdog.mycore.local.state.json"
WD_TARGETS = ("service", "console", "model-e")
WD_FRESH_SEC = 300          # the console's own freshness window
SECRETS = REPO / "secrets.env"
#: The two slots the product's own launcher (deploy/run_service.sh) derives
#: the Telegram service from. Presence only — never a value, never a length
#: of the value, never a prefix.
TG_TOKEN_SLOT = "MYCORE_TG_TOKEN"
TG_ALLOW_SLOT = "AIDOG_TG_ALLOWED_USERS"
TG_LIVE_TOKEN_SLOT = "TELEGRAM_BOT_TOKEN"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


# --------------------------------------------------------------- helpers ---

def one_line(value: object, limit: int = 140) -> str:
    """A single-line, bounded detail string (never a multi-line payload)."""
    text = " ".join(str("" if value is None else value).split())
    return text[:limit]


def repo_version() -> str:
    """The version the TREE declares ("" when the file is unreadable)."""
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def as_number(value: object) -> float | None:
    """A float when the value is a real number (bool is not a number)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def utc_stamp(epoch: object) -> str:
    """An epoch seconds value as a UTC minute stamp ("" when unusable)."""
    number = as_number(epoch)
    if number is None:
        return ""
    try:
        return datetime.fromtimestamp(number, timezone.utc).strftime(
            "%Y-%m-%d %H:%MZ")
    except (OverflowError, OSError, ValueError):
        return ""


# ------------------------------------------------------ the console client ---

class Console:
    """Read-only HTTP client for the local console.

    Only GET is ever issued. The opener bypasses any configured proxy: the
    console is local, and an outbound proxy hop is a network call this
    runner must not make. Errors are reported as a status code or an
    exception CLASS name — never a body, never a URL, never a credential.
    """

    def __init__(self, base: str, timeout: float) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path: str) -> tuple[dict | None, str]:
        """-> (document, error). An empty error means the read succeeded."""
        request = urllib.request.Request(
            f"{self.base}{path}", method="GET",
            headers={"Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                code = response.status
                body = response.read()
        except urllib.error.HTTPError as exc:
            return None, f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 — never echo a traceback
            return None, type(exc).__name__
        if code != 200:
            return None, f"HTTP {code}"
        try:
            doc = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return None, "the answer was not JSON"
        if not isinstance(doc, dict):
            return None, "the answer was not a JSON object"
        return doc, ""


# ------------------------------------------------------------- the rows -----

@dataclass
class Row:
    section: str
    name: str
    status: str
    detail: str


@dataclass
class Report:
    base: str
    tree_version: str
    rows: list[Row] = field(default_factory=list)

    def add(self, section: str, name: str, status: str, detail: str) -> None:
        self.rows.append(Row(section, name, status, one_line(detail)))

    def counts(self) -> dict:
        out = {PASS: 0, FAIL: 0, SKIP: 0}
        for row in self.rows:
            out[row.status] = out.get(row.status, 0) + 1
        return out


# ------------------------------------------------ the credential store ------

def default_secrets_path() -> Path:
    """The product's OWN credential store for the active profile
    (profiles/<name>/secrets.env, else <repo>/secrets.env) — the same seam
    the console and the service read. An invalid profile name falls back to
    the repo store instead of guessing another profile's slots."""
    try:
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from lib import profile_paths  # noqa: PLC0415 — lazy on purpose
        return Path(profile_paths.secrets_path(REPO))
    except Exception:  # noqa: BLE001 — the runner keeps working
        return SECRETS


def store_state(path: Path) -> str:
    """plain | encrypted | locked | absent | unknown — through the ONE shared
    reader (mycore.secrets_store), so an encrypted store is never mis-parsed
    as an env file."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        from mycore import secrets_store  # noqa: PLC0415 — lazy on purpose
        return str(secrets_store.state(path))
    except Exception:  # noqa: BLE001 — the runner keeps working without it
        return "unknown"


def slot_value(path: Path, name: str) -> str:
    """One own-line slot of the product's own store, through the product's own
    reader (mycore.envslots). The value never leaves this function: the
    caller only ever asks whether it is empty."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        from mycore import envslots  # noqa: PLC0415 — lazy on purpose
        return str(envslots.slot_value(name, path))
    except Exception:  # noqa: BLE001 — fall back to a plain own-line read
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        match = re.search(rf"(?m)^{re.escape(name)}=(\S[^\r\n]*)$", text)
        return match.group(1).strip() if match else ""


def bot_slots(path: Path) -> dict:
    """The bot's configuration, as the product itself resolves it.

    The process environment wins over the store (the same precedence
    mycore.envslots applies), so an operator override is never reported as
    an absent slot. Only PRESENCE and the source are returned — no value,
    no length, no prefix. `distinct` is None when the live-gateway token is
    absent (nothing to compare against).
    """
    def resolve(name: str) -> tuple[bool, str]:
        stored = slot_value(path, name)
        if stored:
            return True, "store"
        if os.environ.get(name, "").strip():
            return True, "environment"
        return False, ""

    token, token_source = resolve(TG_TOKEN_SLOT)
    allow, allow_source = resolve(TG_ALLOW_SLOT)
    poll = slot_value(path, TG_TOKEN_SLOT) or os.environ.get(
        TG_TOKEN_SLOT, "").strip()
    live = slot_value(path, TG_LIVE_TOKEN_SLOT) or os.environ.get(
        TG_LIVE_TOKEN_SLOT, "").strip()
    distinct: bool | None = None
    if poll and live:
        distinct = poll != live
    return {"token": token, "token_source": token_source,
            "allow": allow, "allow_source": allow_source,
            "distinct": distinct}


# ------------------------------------------------- the watchdog view -------

def read_watchdog_state() -> tuple[dict | None, str]:
    """-> (state document, error). Read-only; the file is never written."""
    try:
        raw = WD_STATE.read_text(encoding="utf-8")
    except OSError:
        return None, "no watchdog state file"
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, "the watchdog state file is not JSON"
    if not isinstance(doc, dict):
        return None, "the watchdog state file is not an object"
    return doc, ""


def watchdog_targets(doc: dict) -> dict:
    """The per-target verdicts of a watchdog state document.

    A target entry is either a plain state string or an object carrying a
    `state` field (the shape the watchdog writes); both are read the same
    way here, and only the STATE string is ever used.
    """
    targets = doc.get("targets")
    return targets if isinstance(targets, dict) else {}


def target_state(entry: object) -> str:
    """The verdict of one watchdog target ("" when it carries none)."""
    if isinstance(entry, dict):
        return one_line(entry.get("state"))
    if isinstance(entry, str):
        return one_line(entry)
    return ""


def watchdog_age(doc: dict) -> int | None:
    """Age of the watchdog state file in seconds (None when unreadable)."""
    try:
        return int(time.time() - WD_STATE.stat().st_mtime)
    except OSError:
        return None


# ------------------------------------------------------------ the legs ------

def leg_console(report: Report, console: Console, tree_version: str) -> None:
    """CONSOLE — reachability and build identity."""
    section = "console"
    started = time.time()
    doc, err = console.get("/api/health")
    elapsed = (time.time() - started) * 1000.0
    if err:
        report.add(section, "console.reachable", FAIL,
                   f"the console did not answer ({err})")
        report.add(section, "console.health", FAIL,
                   "no health document to read")
        report.add(section, "console.version", FAIL,
                   "no health document to read")
        return
    report.add(section, "console.reachable", PASS,
               f"/api/health answered in {elapsed:.0f} ms")
    status = one_line(doc.get("status"))
    report.add(section, "console.health", PASS if status == "ok" else FAIL,
               f"status={status or '(absent)'}")
    running = one_line(doc.get("version"))
    if not tree_version:
        report.add(section, "console.version", SKIP,
                   "the tree declares no version (VERSION file unreadable)")
    elif running == tree_version:
        report.add(section, "console.version", PASS,
                   f"the running console is the tree build {running}")
    else:
        report.add(section, "console.version", FAIL,
                   f"the console runs {running or '(unknown)'} while the tree "
                   f"declares {tree_version} — restart the console before "
                   "accepting")


def leg_status(report: Report, console: Console) -> None:
    """STATUS — the panels and the mycore runtime facts."""
    section = "status"
    doc, err = console.get("/api/status")
    if err:
        for name in ("status.panels", "status.advisory",
                     "status.mycore.facts"):
            report.add(section, name, FAIL,
                       f"the console did not answer ({err})")
        return
    panels = doc.get("panels")
    panels = panels if isinstance(panels, list) else []
    if not panels:
        report.add(section, "status.panels", FAIL,
                   "the status payload carries no panels")
    else:
        states = {}
        for panel in panels:
            if isinstance(panel, dict):
                states[one_line(panel.get("id"))] = one_line(
                    panel.get("state"))
        down = [f"{k}={v}" for k, v in states.items() if v == "down"]
        unknown = [f"{k}={v}" for k, v in states.items()
                   if v not in ("up", "down")]
        if down:
            report.add(section, "status.panels", FAIL,
                       f"down: {', '.join(down)}")
        elif unknown:
            report.add(section, "status.panels", FAIL,
                       f"cannot confirm: {', '.join(unknown)}")
        else:
            report.add(section, "status.panels", PASS,
                       f"{len(states)}/{len(states)} up "
                       f"({', '.join(sorted(states))})")
    advisory = doc.get("advisory")
    level = one_line(advisory.get("level")) if isinstance(advisory, dict) else ""
    expected = "urgent" if any(one_line(p.get("state")) == "down"
                               for p in panels if isinstance(p, dict)) else (
        "warn" if any(one_line(p.get("state")) not in ("up", "down")
                      for p in panels if isinstance(p, dict)) else "info")
    if not level:
        report.add(section, "status.advisory", SKIP,
                   "the console reported no advisory level")
    elif level == expected:
        report.add(section, "status.advisory", PASS,
                   f"level={level}, consistent with the panels")
    else:
        report.add(section, "status.advisory", FAIL,
                   f"level={level} but the panels imply {expected}")
    mycore = doc.get("mycore")
    if not isinstance(mycore, dict):
        report.add(section, "status.mycore.facts", FAIL,
                   "the status payload carries no mycore runtime facts")
        return
    service = mycore.get("service")
    watchdog = mycore.get("watchdog")
    service = service if isinstance(service, dict) else {}
    watchdog = watchdog if isinstance(watchdog, dict) else {}
    if not service:
        report.add(section, "status.mycore.facts", FAIL,
                   "no mycore.service facts in the payload")
        return
    svc_state = one_line(service.get("state"))
    wd_state = one_line(watchdog.get("state"))
    bits = [f"service={svc_state or '(absent)'}"]
    pid = service.get("pid")
    if pid:
        bits.append(f"pid {one_line(pid)}")
    lines = as_number(service.get("log_lines"))
    if lines is not None:
        bits.append(f"{int(lines)} log lines")
    if wd_state:
        age = as_number(watchdog.get("age_sec"))
        bits.append(f"watchdog={wd_state}"
                    + (f" (age {int(age)}s)" if age is not None else ""))
    if svc_state == "running":
        report.add(section, "status.mycore.facts", PASS, "; ".join(bits))
    elif svc_state.startswith("pending"):
        report.add(section, "status.mycore.facts", SKIP,
                   f"{'; '.join(bits)} — the chat service is not provisioned "
                   "here (fail-closed)")
    else:
        report.add(section, "status.mycore.facts", FAIL, "; ".join(bits))


def leg_control(report: Report, console: Console) -> None:
    """CONTROL — the leash: pause state, gate, daily budget."""
    section = "control"
    doc, err = console.get("/api/control")
    if err:
        for name in ("control.paused", "control.gate", "control.budget"):
            report.add(section, name, FAIL,
                       f"the console did not answer ({err})")
        return
    paused = bool(doc.get("paused"))
    reason = one_line(doc.get("reason"))
    if paused:
        stamp = utc_stamp(doc.get("since"))
        report.add(section, "control.paused", FAIL,
                   f"paused{(' since ' + stamp) if stamp else ''}"
                   + (f" ({reason})" if reason else ""))
    else:
        report.add(section, "control.paused", PASS,
                   f"not paused (tail={one_line(doc.get('tail_state'))})")
    gate = doc.get("gate")
    gate = gate if isinstance(gate, dict) else {}
    allowed = bool(gate.get("allowed"))
    code = one_line(gate.get("code"))
    detail = one_line(gate.get("detail"))
    if allowed:
        report.add(section, "control.gate", PASS,
                   "a new turn would be answered right now")
    else:
        report.add(section, "control.gate", FAIL,
                   f"the gate refuses new turns: {code or '(no code)'}"
                   + (f" — {detail}" if detail else ""))
    budget = doc.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    limits = budget.get("limits")
    limits = limits if isinstance(limits, dict) else {}
    usd_limit = as_number(limits.get("usd_day")) or 0.0
    token_limit = as_number(limits.get("token_day")) or 0.0
    if usd_limit <= 0 and token_limit <= 0:
        report.add(section, "control.budget", PASS,
                   "no daily cap is armed (the leash does not block on cost)")
    else:
        states = []
        for key in ("usd", "tokens"):
            row = budget.get(key)
            if isinstance(row, dict):
                states.append(f"{key}={one_line(row.get('state'))}")
        over = any(one_line(row.get("state")) == "over"
                   for key in ("usd", "tokens")
                   for row in [budget.get(key)] if isinstance(row, dict))
        if over:
            report.add(section, "control.budget", FAIL,
                       f"a daily cap is armed and exceeded ({', '.join(states)})")
        else:
            report.add(section, "control.budget", PASS,
                       f"a daily cap is armed and not exceeded "
                       f"({', '.join(states)})")


def leg_usage(report: Report, console: Console) -> None:
    """USAGE — the cost display block and the unknown-amount rule."""
    section = "usage"
    doc, err = console.get("/api/usage")
    if err:
        for name in ("usage.display", "usage.cost_honesty"):
            report.add(section, name, FAIL,
                       f"the console did not answer ({err})")
        return
    display = doc.get("display")
    display = display if isinstance(display, dict) else {}
    currency = one_line(display.get("currency"))
    symbol = one_line(display.get("symbol"))
    source = one_line(display.get("source"))
    rate = as_number(display.get("rate_cny_per_unit"))
    if not currency:
        report.add(section, "usage.display", FAIL,
                   "the usage payload carries no display block")
    else:
        bits = [f"{currency} ({symbol})" if symbol else currency]
        if source == "native":
            bits.append("source currency, no conversion")
        elif rate is not None:
            bits.append(f"rate {rate} ({source})")
        else:
            bits.append(f"rate unknown ({source})")
        if source == "unavailable" and currency != "CNY":
            bits.append("the console shows an unknown cost, never a zero")
        report.add(section, "usage.display", PASS, ", ".join(bits))
    pricing = doc.get("pricing")
    pricing = pricing if isinstance(pricing, dict) else {}
    priced = bool(pricing.get("available"))
    today = doc.get("today")
    today = today if isinstance(today, dict) else None
    cost = as_number(today.get("cost")) if today else None
    if today and cost is not None and not priced:
        report.add(section, "usage.cost_honesty", FAIL,
                   f"today's cost reads {cost} while no price is configured "
                   "— an unknown amount must read unknown, never zero")
    elif today and cost is not None and cost < 0:
        report.add(section, "usage.cost_honesty", FAIL,
                   "today's cost is negative")
    elif today and cost is None:
        report.add(section, "usage.cost_honesty", PASS,
                   "today's amount is unknown (no price configured) — not zero")
    elif today and cost is not None:
        unit = currency or "the display currency"
        report.add(section, "usage.cost_honesty", PASS,
                   f"today's amount is a real number in {unit}")
    else:
        report.add(section, "usage.cost_honesty", PASS,
                   "no request log for today — the amount reads unknown, "
                   "not zero")


def leg_endpoints(report: Report, console: Console) -> None:
    """MODEL SLOTS — the per-slot cooldown observation (no credential)."""
    section = "model-slots"
    doc, err = console.get("/api/mycore/endpoints?view=cooldown")
    if err:
        report.add(section, "endpoints.cooldown", FAIL,
                   f"the console did not answer ({err})")
        return
    if not doc.get("available"):
        report.add(section, "endpoints.cooldown", FAIL,
                   "the console cannot observe the per-slot backoff "
                   "(the view reports itself unavailable)")
        return
    endpoints = doc.get("endpoints")
    endpoints = endpoints if isinstance(endpoints, list) else []
    allowed = {"index", "state", "label", "remaining_s"}
    states: list[str] = []
    leaks: list[str] = []
    bad_state = False
    broken = 0
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            broken += 1
            continue
        slots = endpoint.get("slots")
        if not isinstance(slots, list):
            broken += 1
            continue
        for slot in slots:
            if not isinstance(slot, dict):
                broken += 1
                continue
            extra = sorted(set(slot) - allowed)
            if extra:
                leaks.append(",".join(extra))
            state = one_line(slot.get("state"))
            if state not in ("not-cooling", "cooling", "unobserved"):
                bad_state = True
            label = one_line(slot.get("label"))
            states.append(f"{label or 'slot'}={state}")
    if leaks:
        report.add(section, "endpoints.cooldown", FAIL,
                   "the cooldown view carries fields beyond the observation "
                   f"contract: {', '.join(sorted(set(leaks)))}")
    elif bad_state:
        report.add(section, "endpoints.cooldown", FAIL,
                   "the cooldown view carries a state outside the contract")
    elif broken:
        report.add(section, "endpoints.cooldown", FAIL,
                   f"{broken} endpoint/slot row(s) are not the documented "
                   "shape")
    else:
        cooling = [s for s in states if s.endswith("=cooling")]
        detail = (f"{len(endpoints)} endpoint(s), {len(states)} slot(s): "
                  + (", ".join(states) if states else "none observed"))
        if cooling:
            detail += " — cooling now"
        report.add(section, "endpoints.cooldown", PASS, detail)


def leg_fleet(report: Report, console: Console) -> None:
    """FLEET — the read-only board and the sign-off queue."""
    section = "fleet"
    doc, err = console.get("/api/fleet")
    if err:
        report.add(section, "fleet.board", FAIL,
                   f"the console did not answer ({err})")
    else:
        note = one_line(doc.get("note"))
        fleets = doc.get("fleets")
        clones = as_number(doc.get("clones"))
        if note:
            report.add(section, "fleet.board", FAIL,
                       f"the fleet board cannot be read: {note}")
        elif not isinstance(fleets, list):
            report.add(section, "fleet.board", FAIL,
                       "the fleet board carries no fleet list")
        else:
            report.add(section, "fleet.board", PASS,
                       f"{len(fleets)} fleet(s), {int(clones or 0)} clone(s)"
                       f" (local, read-only)")
    doc, err = console.get("/api/fleet/plans")
    if err:
        report.add(section, "fleet.plans", FAIL,
                   f"the console did not answer ({err})")
        return
    plans = doc.get("plans")
    plans = plans if isinstance(plans, list) else None
    if plans is None:
        report.add(section, "fleet.plans", FAIL,
                   "the plan queue carries no plan list")
    elif doc.get("read_only") is not True:
        report.add(section, "fleet.plans", FAIL,
                   "the plan queue is not marked read-only")
    else:
        report.add(section, "fleet.plans", PASS,
                   f"{len(plans)} plan(s) awaiting sign-off (read-only)")


def leg_backup(report: Report, console: Console) -> None:
    """BACKUP — the agent-backup status face."""
    section = "backup"
    doc, err = console.get("/api/backup/agent")
    if err:
        for name in ("backup.agent.node", "backup.agent.password"):
            report.add(section, name, FAIL,
                       f"the console did not answer ({err})")
        return
    node = doc.get("node")
    node = node if isinstance(node, dict) else {}
    if node.get("ok"):
        report.add(section, "backup.agent.node", PASS,
                   f"the agent node answers ({one_line(node.get('version'))})")
    else:
        report.add(section, "backup.agent.node", FAIL,
                   "the agent node does not answer: "
                   + (one_line(node.get("reason")) or "no reason given"))
    slot = doc.get("password_slot")
    slot = slot if isinstance(slot, dict) else {}
    if slot.get("set"):
        report.add(section, "backup.agent.password", PASS,
                   "the agent-backup passphrase slot is set (value not shown)")
    else:
        report.add(section, "backup.agent.password", SKIP,
                   "no agent-backup passphrase slot — the agent backup is "
                   "not armed on this host")


def leg_version(report: Report, console: Console, tree_version: str) -> None:
    """VERSION — installed vs latest."""
    section = "version"
    doc, err = console.get("/api/version")
    if err:
        for name in ("version.installed", "version.update"):
            report.add(section, name, FAIL,
                       f"the console did not answer ({err})")
        return
    installed = one_line(doc.get("installed"))
    if not tree_version:
        report.add(section, "version.installed", SKIP,
                   "the tree declares no version (VERSION file unreadable)")
    elif installed == tree_version:
        report.add(section, "version.installed", PASS,
                   f"installed {installed} — the tree build")
    else:
        report.add(section, "version.installed", FAIL,
                   f"installed {installed or '(unknown)'} while the tree "
                   f"declares {tree_version}")
    latest = one_line(doc.get("latest"))
    available = doc.get("update_available")
    if available is True:
        report.add(section, "version.update", FAIL,
                   f"an update is available: latest {latest or '(unknown)'}"
                   f" > installed {installed or '(unknown)'}")
    elif available is False:
        report.add(section, "version.update", PASS,
                   f"up to date ({installed or 'installed version unknown'})")
    else:
        report.add(section, "version.update", SKIP,
                   "the latest released version is unknown (the release "
                   "channel did not answer)")


def leg_chat(report: Report, console: Console) -> None:
    """CHAT — the console session shape and its context accounting."""
    section = "chat"
    doc, err = console.get("/api/chat/history")
    if err:
        for name in ("chat.history", "chat.context"):
            report.add(section, name, FAIL,
                       f"the console did not answer ({err})")
        return
    messages = doc.get("messages")
    messages = messages if isinstance(messages, list) else None
    if messages is None:
        report.add(section, "chat.history", FAIL,
                   "the history payload carries no message list")
    else:
        bad = []
        for message in messages:
            if not isinstance(message, dict):
                bad.append("a message is not an object")
                continue
            role = one_line(message.get("role"))
            if role not in ("user", "assistant"):
                bad.append(f"role={role or '(absent)'}")
            elif not isinstance(message.get("content"), str):
                bad.append(f"role={role} content is not a string")
            elif not isinstance(message.get("id"), int):
                bad.append(f"role={role} has no id")
        count = as_number(doc.get("count"))
        if bad:
            report.add(section, "chat.history", FAIL,
                       "the history shape is off: " + "; ".join(
                           sorted(set(bad))[:3]))
        elif count is not None and int(count) != len(messages):
            report.add(section, "chat.history", FAIL,
                       f"the history count says {int(count)} but the list "
                       f"carries {len(messages)}")
        else:
            report.add(section, "chat.history", PASS,
                       f"{len(messages)} turn(s), each with an id, a role and "
                       "text")
    context = doc.get("context")
    context = context if isinstance(context, dict) else {}
    if not context:
        report.add(section, "chat.context", FAIL,
                   "the history payload carries no context accounting")
        return
    if context.get("available"):
        used = as_number(context.get("used_chars"))
        budget = as_number(context.get("budget_chars"))
        over = context.get("over_budget")
        detail = (f"{int(used)}/{int(budget)} chars used"
                  if used is not None and budget is not None
                  else "the context counters are present")
        if over:
            detail += " — over the compaction budget"
        report.add(section, "chat.context", PASS, detail)
    else:
        report.add(section, "chat.context", FAIL,
                   "the console cannot measure its own context: "
                   + (one_line(context.get("note")) or "no reason given"))


def leg_telegram(report: Report, console: Console, secrets: Path) -> None:
    """TELEGRAM — the service that long-polls the dedicated bot.

    The runner observes the service, the watchdog verdicts and the bot's
    configuration; it never sends a message, so a real round trip stays the
    owner's step. The leg says so out loud.
    """
    section = "telegram"
    doc, err = console.get("/api/status")
    if err:
        report.add(section, "telegram.service", SKIP,
                   "the console did not answer — the service state could not "
                   "be read")
    else:
        mycore = doc.get("mycore")
        mycore = mycore if isinstance(mycore, dict) else {}
        service = mycore.get("service")
        service = service if isinstance(service, dict) else {}
        state = one_line(service.get("state"))
        if state == "running":
            pid = service.get("pid")
            lines = as_number(service.get("log_lines"))
            bits = ["running"]
            if pid:
                bits.append(f"pid {one_line(pid)}")
            if lines is not None:
                bits.append(f"{int(lines)} log lines")
            report.add(section, "telegram.service", PASS,
                       f"the poll service is {'; '.join(bits)}")
        elif state.startswith("pending"):
            report.add(section, "telegram.service", SKIP,
                       "the Telegram leg is not provisioned on this host "
                       "(fail-closed: no bot token / allowlist)")
        elif state == "stopped":
            report.add(section, "telegram.service", FAIL,
                       "the poll service is stopped (it is provisioned but "
                       "not running)")
        else:
            report.add(section, "telegram.service", FAIL,
                       f"the poll service state is {state or '(absent)'}")
    state_doc, state_err = read_watchdog_state()
    if state_doc is None:
        report.add(section, "telegram.watchdog.targets", FAIL,
                   f"the watchdog state is unreadable ({state_err}) — the "
                   "supervision verdicts cannot be confirmed")
    else:
        targets = watchdog_targets(state_doc)
        missing = [name for name in WD_TARGETS if name not in targets]
        down = [f"{name}={target_state(targets.get(name))}"
                for name in WD_TARGETS
                if name in targets and target_state(targets.get(name)) != "up"]
        age = watchdog_age(state_doc)
        detail = ", ".join(f"{name}={target_state(targets.get(name))}"
                           for name in WD_TARGETS if name in targets)
        if age is not None:
            detail += f" (state age {age}s)"
        if missing:
            report.add(section, "telegram.watchdog.targets", FAIL,
                       f"no verdict for {', '.join(missing)}; {detail}")
        elif down:
            report.add(section, "telegram.watchdog.targets", FAIL,
                       f"not up: {', '.join(down)}; {detail}")
        elif age is not None and age > WD_FRESH_SEC:
            report.add(section, "telegram.watchdog.targets", FAIL,
                       f"the watchdog state is stale ({age}s > {WD_FRESH_SEC}s)")
        else:
            report.add(section, "telegram.watchdog.targets", PASS, detail)
    store = store_state(secrets)
    if store == "locked":
        report.add(section, "telegram.bot.config", SKIP,
                   "the credential store is locked — the bot configuration "
                   "is unknown (fail-closed)")
        report.add(section, "telegram.roundtrip", SKIP,
                   "needs the owner to send a message in the bot — the "
                   "runner never sends one")
        return
    slots = bot_slots(secrets)
    token, allow, distinct = slots["token"], slots["allow"], slots["distinct"]
    if not token and not allow:
        report.add(section, "telegram.bot.config", SKIP,
                   "the bot is not configured in this product's own store "
                   "(no poll token, no owner allowlist)")
        report.add(section, "telegram.roundtrip", SKIP,
                   "not provisioned — there is no bot for the owner to write to")
        return
    problems = []
    if not token:
        problems.append(f"the {TG_TOKEN_SLOT} slot is empty")
    if not allow:
        problems.append(f"the {TG_ALLOW_SLOT} slot is empty")
    if distinct is False:
        problems.append("the poll token equals the live gateway token — two "
                        "pollers on one token steal each other's updates")
    if problems:
        report.add(section, "telegram.bot.config", FAIL,
                   "; ".join(problems) + " (values never shown)")
    else:
        bits = [f"the {TG_TOKEN_SLOT} slot is set ({slots['token_source']})",
                f"the {TG_ALLOW_SLOT} slot is set ({slots['allow_source']})"]
        if distinct is True:
            bits.append("the poll token is distinct from the live gateway one")
        elif distinct is None:
            bits.append("no live-gateway token to compare against")
        report.add(section, "telegram.bot.config", PASS, "; ".join(bits))
    report.add(section, "telegram.roundtrip", SKIP,
               "needs the owner to send a message in the bot — the runner "
               "never sends one")


# ------------------------------------------------------------- rendering ----

def render_text(report: Report) -> str:
    """The human table."""
    lines = [
        "AiDog acceptance — owner walkthrough (read-only)",
        f"console: {report.base} · tree version: "
        f"{report.tree_version or '(unknown)'} · "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%MZ')}",
        "",
    ]
    width = max((len(row.name) for row in report.rows), default=0)
    section = None
    for row in report.rows:
        if row.section != section:
            section = row.section
            lines.append(SECTION_TITLES.get(section, section.upper()))
        lines.append(f"  {row.status:<4}  {row.name:<{width}}  {row.detail}")
    counts = report.counts()
    lines.append("")
    lines.append(f"{len(report.rows)} checks · {counts[PASS]} PASS · "
                 f"{counts[FAIL]} FAIL · {counts[SKIP]} SKIP")
    if counts[FAIL]:
        lines.append("Result: FAIL — the rows marked FAIL above need the "
                     "owner's attention.")
    else:
        lines.append("Result: PASS — every applicable check passed; a SKIP "
                     "means the check does not apply here, not that it passed.")
    return "\n".join(lines)


SECTION_TITLES = {
    "console": "CONSOLE — reachability and build identity",
    "status": "STATUS — panels and mycore runtime facts",
    "control": "CONTROL — the leash (pause, gate, budget)",
    "usage": "USAGE — the cost display block",
    "model-slots": "MODEL SLOTS — per-slot cooldown (no credential)",
    "fleet": "FLEET — the read-only board and the sign-off queue",
    "backup": "BACKUP — the agent-backup status face",
    "version": "VERSION — installed vs latest",
    "chat": "CHAT — the console session",
    "telegram": "TELEGRAM — the bot leg (the runner never sends a message)",
}


def render_json(report: Report) -> str:
    """The machine-readable document."""
    counts = report.counts()
    return json.dumps({
        "tool": "aidog accept",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "console_base": report.base,
        "tree_version": report.tree_version,
        "read_only": True,
        "checks": [{"section": row.section, "name": row.name,
                    "status": row.status, "detail": row.detail}
                   for row in report.rows],
        "summary": {"total": len(report.rows), "pass": counts[PASS],
                    "fail": counts[FAIL], "skip": counts[SKIP]},
        "result": "FAIL" if counts[FAIL] else "PASS",
        "exit_code": 1 if counts[FAIL] else 0,
    }, indent=2, ensure_ascii=False)


# ------------------------------------------------------------------ main ----

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aidog_accept.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "sections\n"
            "  console    /api/health: liveness and the version the running "
            "build reports (compared with the tree's VERSION).\n"
            "  status     /api/status: the five panels, the advisory level "
            "and the mycore runtime facts.\n"
            "  control    /api/control: pause state, the gate a new turn "
            "would meet, and the daily budget.\n"
            "  usage      /api/usage: the currency unit and the honesty "
            "rule (an unknown amount reads unknown, never zero).\n"
            "  model-slots\n"
            "            /api/mycore/endpoints?view=cooldown: the per-slot "
            "backoff observation (slot names only).\n"
            "  fleet      /api/fleet and /api/fleet/plans: the read-only "
            "board and the sign-off queue.\n"
            "  backup     /api/backup/agent: the agent-backup status face.\n"
            "  version    /api/version: installed vs latest.\n"
            "  chat       /api/chat/history: the session shape and the "
            "context accounting.\n"
            "  telegram   the service that long-polls the dedicated bot, the "
            "watchdog verdicts (service/console/model-e) and the bot's "
            "configuration in the product's own store. The runner never "
            "sends a message: that step stays with the owner.\n"
            "\n"
            "exit code\n"
            "  0  every applicable check passed (a SKIP does not count as a "
            "failure)\n"
            "  1  at least one check FAILED\n"
        ))
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help="the local console base URL "
                             f"(default: {DEFAULT_BASE})")
    parser.add_argument("--timeout", type=float, default=8.0,
                        help="per-request timeout in seconds (default: 8)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output instead of the table")
    parser.add_argument("--secrets", type=Path, default=None,
                        help="the product's own credential store "
                             "(default: the active profile's secrets.env)")
    return parser


def run_leg(report: Report, name: str, fn, *fn_args) -> None:
    """Run one leg; a crash becomes one honest FAIL row, never a lost run."""
    try:
        fn(report, *fn_args)
    except Exception as exc:  # noqa: BLE001 — never echo a traceback
        report.add(name, f"{name}.leg", FAIL,
                   f"the check crashed ({type(exc).__name__})")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = str(args.base).rstrip("/")
    if not base.startswith(("http://", "https://")):
        print("aidog accept: --base must be an http(s) URL", file=sys.stderr)
        return 2
    console = Console(base, max(0.5, float(args.timeout)))
    report = Report(base=base, tree_version=repo_version())
    secrets = Path(args.secrets) if args.secrets else default_secrets_path()

    run_leg(report, "console", leg_console, console, report.tree_version)
    run_leg(report, "status", leg_status, console)
    run_leg(report, "control", leg_control, console)
    run_leg(report, "usage", leg_usage, console)
    run_leg(report, "model-slots", leg_endpoints, console)
    run_leg(report, "fleet", leg_fleet, console)
    run_leg(report, "backup", leg_backup, console)
    run_leg(report, "version", leg_version, console, report.tree_version)
    run_leg(report, "chat", leg_chat, console)
    run_leg(report, "telegram", leg_telegram, console, secrets)

    print(render_json(report) if args.json else render_text(report))
    return 1 if report.counts()[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
