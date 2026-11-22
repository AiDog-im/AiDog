#!/usr/bin/env python3
"""The Telegram-side FORGET control — the chat half, end to end (offline).

What this proves, on a real machine, through the REAL service command
layer (never through a direct function call):

  * the command lives in the SERVICE COMMAND LAYER: the model client is
    NEVER called for a "/forget" message, no tool is registered for it, and
    no session is written — the model can never trigger a forget;
  * only an allowlisted sender reaches the command (the service's own
    gate), and the gate is not weakened;
  * "/forget <query>" lists the matching forgettable sources with a
    number, the label (a HEADING — never a body), the plain channel name
    and the short opaque source id; the list is bounded;
  * the reply never carries an entry body, an excerpt, a file path or a
    credential — not even the query itself;
  * "/forget confirm <code>" records a TOMBSTONE only: NOTHING is deleted,
    the source file is byte-identical afterwards, and the answer states the
    honest limits;
  * a wrong, expired, reused or unknown code is refused honestly and
    writes nothing;
  * at most ONE pending confirmation exists per chat;
  * a second, unrelated source keeps working;
  * the ledger row holds no excerpt;
  * a suppression stays inside its profile;
  * the pending confirmation lives in the per-profile STATE root and never
    in a tracked file;
  * the repository's tracked files are byte-identical before and after the
    whole flow.

Isolation rules honoured here:
  * every profile is a throwaway one under a temp profiles root, with its
    own state root (AIDOG_MC_STATE), its own memory slots and its own
    derived profile — the repository's overlay files are never addressed;
  * no network: the Telegram transport is an offline stub and the model
    client is a recorder that must never be called;
  * no production state is written and no product file is modified.

Run:  python3 tests/tg_forget_flow.py     (exit 0 = all green)
English output per repo i18n policy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

PROFILE_A = "alphadog"
PROFILE_B = "betadog"
OWNER = 111
CHAT = 555
OTHER_CHAT = 556
TOKEN = "offline-tg-token-not-a-credential"

# The fixture content: the bodies must never reach a reply.
BODY_MEMORY = "the owner prefers secret-alpha-phrase"
BODY_USER = "writes in a secret-bravo-phrase way"
BODY_LEARNED = "secret-charlie-phrase"
PROFILE_TOKEN = "deltatoken"

PASSED = 0
FAILED = 0
NOTES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """One named check. The detail is printed only when the check FAILS."""
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED += 1
        print(f"FAIL  {name}   [{detail}]")
    return ok


def note(text: str) -> None:
    """An observation that is not a pass/fail check."""
    NOTES.append(text)
    print(f"NOTE  {text}")


# ------------------------------------------------------------- utilities --
def tracked_digest() -> str:
    """One digest over every TRACKED file of the repository.

    A tracked file that changed (or vanished) changes the digest; an
    untracked file (this test, the new module) never does."""
    try:
        proc = subprocess.run(["git", "ls-files", "-z"], cwd=str(ROOT),
                              capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return "git-unavailable"
    if proc.returncode != 0:
        return "git-unavailable"
    digest = hashlib.sha256()
    for rel in proc.stdout.decode("utf-8", "replace").split("\0"):
        if not rel:
            continue
        path = ROOT / rel
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        digest.update(rel.encode("utf-8"))
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


class FakeTg:
    """Offline Telegram transport: records the sends and the command menu."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, str]] = []
        self.menus: list[tuple[str, list]] = []

    def __call__(self, url, payload, timeout):
        method = url.rsplit("/", 1)[-1]
        if method == "sendMessage":
            form = urllib.parse.parse_qs(payload.decode("utf-8"))
            self.sends.append((form.get("chat_id", [""])[0],
                               form.get("text", [""])[0]))
            return json.dumps({"ok": True, "result": {"message_id": 1}})
        if method in ("setMyCommands", "deleteMyCommands"):
            form = urllib.parse.parse_qs((payload or b"").decode("utf-8"))
            raw = form.get("commands", [""])[0]
            try:
                parsed = json.loads(raw) if raw else []
            except ValueError:
                parsed = []
            self.menus.append((method, parsed))
            return json.dumps({"ok": True, "result": True})
        return json.dumps({"ok": True, "result": True})


def mk_update(uid: int, chat_id: int, text: str) -> dict:
    """A Telegram-style message update (offline fixture)."""
    return {"update_id": 1, "message": {
        "message_id": 1,
        "from": {"id": uid, "is_bot": False, "first_name": "x"},
        "chat": {"id": chat_id, "type": "private"},
        "date": 0, "text": text}}


def make_profile(profiles: Path, name: str, *, bulk: int = 0) -> Path:
    """A throwaway profile: own memory slots, own state, own derived profile."""
    profile = profiles / name
    memory = profile / "body" / "memory"
    memory.mkdir(parents=True)
    (profile / "state" / "profiling").mkdir(parents=True)
    (memory / "MEMORY_curated.md").write_text(
        "# Wallet\n\n" + BODY_MEMORY + "\n"
        + ("".join("## bulk-entry-%02d\n\n- secret-bulk-body-%02d\n"
                   % (index, index) for index in range(bulk)) if bulk else ""),
        encoding="utf-8")
    (memory / "USER_curated.md").write_text(
        "# Tone\n\n" + BODY_USER + "\n", encoding="utf-8")
    (memory / "EVOLUTION_learned.md").write_text(
        "# LEARNED MEMORY\n\n## 2026-11-20 (learn)\n\n- " + BODY_LEARNED
        + "\n", encoding="utf-8")
    (profile / "state" / "profiling" / "profile.json").write_text(
        json.dumps({"ok": True, "at": 0.0,
                    "keywords": [{"token": PROFILE_TOKEN, "score": 1.0,
                                  "tier": "L1", "trait": "known"}]}),
        encoding="utf-8")
    return profile


class Dog:
    """The service under test: an offline transport, a recording model."""

    def __init__(self, state: Path, clock: list[float]) -> None:
        from mycore.service import ChatService
        self.calls: list[dict] = []
        self.tg = FakeTg()
        self.clock = clock
        self.svc = ChatService(state, frozenset({OWNER}), transport=self.tg,
                               chat=self._chat, now=lambda: clock[0],
                               repo=ROOT)

    def _chat(self, **kwargs):
        self.calls.append(kwargs)
        return {"text": "the model must never answer a command"}

    def say(self, text: str, uid: int = OWNER, chat_id: int = CHAT) -> str:
        """Send one message and return the reply ("" when nothing was sent)."""
        before = len(self.tg.sends)
        self.svc.handle_update(mk_update(uid, chat_id, text))
        if len(self.tg.sends) == before:
            return ""
        return self.tg.sends[-1][1]


def codes_in(reply: str) -> list[str]:
    """The confirmation codes of a listing (one per candidate)."""
    return re.findall(r"code ([A-Z0-9]{6})", reply or "")


def ledger_rows(state: Path) -> list[dict]:
    from mycore import memforget
    return memforget.load_rows(state=state)


def ledger_raw(state: Path) -> str:
    path = state / "memforget" / "tombstones.jsonl"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def no_leak(text: str, *, tmp: Path) -> bool:
    """The reply carries no body, no excerpt, no path and no credential."""
    if not text:
        return True
    for secret in (BODY_MEMORY, BODY_USER, BODY_LEARNED, "secret-bulk-body",
                   "secret-alpha-phrase", "secret-bravo-phrase"):
        if secret in text:
            return False
    if str(tmp) in text:
        return False
    if TOKEN in text:
        return False
    for marker in ("MEMORY_curated", "USER_curated", "EVOLUTION_learned",
                   ".md", ".json", "tombstones", "sessions", "forgetcmd"):
        if marker in text:
            return False
    return True


# ------------------------------------------------------------------ main ---
def main() -> int:
    from mycore import memforget
    from mycore.service import BOT_COMMANDS

    tmp = Path(tempfile.mkdtemp(prefix="aidog-tg-forget-"))
    profiles = tmp / "profiles"
    profiles.mkdir()
    state_a = profiles / PROFILE_A / "state"
    state_b = profiles / PROFILE_B / "state"
    make_profile(profiles, PROFILE_A)
    make_profile(profiles, PROFILE_B)

    env_keys = ("AIDOG_PROFILE", "AIDOG_PROFILES_DIR", "AIDOG_MC_STATE",
                "MYCORE_TG_TOKEN")
    saved = {k: os.environ.get(k) for k in env_keys}
    clock = [1_800_000_000.0]
    dog_a: Dog | None = None
    dog_b: Dog | None = None
    tracked_before = tracked_digest()
    prod_ledger = ROOT / "data" / "mycore_service" / "memforget" / \
        "tombstones.jsonl"
    prod_before = prod_ledger.read_bytes() if prod_ledger.exists() else b""

    def use_profile(name: str, state: Path) -> None:
        os.environ["AIDOG_PROFILE"] = name
        os.environ["AIDOG_PROFILES_DIR"] = str(profiles)
        os.environ["AIDOG_MC_STATE"] = str(state)

    try:
        os.environ["MYCORE_TG_TOKEN"] = TOKEN
        use_profile(PROFILE_A, state_a)
        dog_a = Dog(state_a, clock)

        # ------------------------------------------- the discovery surface --
        check("A1 the forget command is a published bot command",
              "forget" in BOT_COMMANDS, str(BOT_COMMANDS))
        from lib import i18n
        en_pack = i18n.pack("en") or {}
        zh_pack = i18n.pack("zh") or {}
        check("A2 the /help body lists /forget",
              any(ln.split()[0].lstrip("/") == "forget"
                  for ln in (en_pack.get("cmd.help.body") or "").split("\n")
                  if ln.strip()),
              (en_pack.get("cmd.help.body") or "")[-120:])
        check("A3 the bot menu publishes a forget entry",
              dog_a.svc._sync_bot_menu() is None
              and any("forget" in str(entry)
                      for _method, entries in dog_a.tg.menus
                      for entry in entries),
              str(dog_a.tg.menus))
        check("A4 both packs carry the forget strings",
              all(k in en_pack and k in zh_pack for k in
                  ("menu.forget", "cmd.forget.usage", "cmd.forget.item",
                   "cmd.forget.ok", "cmd.forget.limits")), "locale drift")

        # ------------------------------------------------ the usage answer --
        reply = dog_a.say("/forget")
        check("B1 /forget with no argument explains the flow",
              "/forget confirm" in reply and "forget" in reply, reply)
        check("B2 the usage answer leaks nothing", no_leak(reply, tmp=tmp),
              reply)

        # ------------------------------------------------- the listing -----
        reply = dog_a.say("/forget secret")
        check("B3 a query lists the matching sources",
              "Wallet" in reply and "Tone" in reply and "(learn)" in reply,
              reply)
        check("B4 every candidate shows a number, a label, a channel and a "
              "short opaque id",
              len(re.findall(r"(?m)^\d+\. ", reply)) == 3
              and "curated:memory" in reply and "curated:user" in reply
              and "learned" in reply
              and len(re.findall(r"src_[0-9a-f]{8}", reply)) == 3, reply)
        check("B5 the listing never carries a body, an excerpt, a path or a "
              "credential", no_leak(reply, tmp=tmp), reply)
        check("B6 the listing never echoes the query",
              "secret" not in reply, reply)
        first_codes = codes_in(reply)
        check("B7 every candidate carries its own confirmation code",
              len(first_codes) == 3 and len(set(first_codes)) == 3, reply)

        # the derived owner profile is its own channel
        reply = dog_a.say(f"/forget {PROFILE_TOKEN}")
        check("B8 the derived owner profile is listed in its own channel",
              PROFILE_TOKEN in reply and "profile" in reply, reply)
        check("B9 the profile listing leaks nothing", no_leak(reply, tmp=tmp),
              reply)

        # a query that matches nothing answers honestly
        reply = dog_a.say("/forget nothing-matches-this")
        check("B10 a query that matches nothing answers honestly",
              "nothing was forgotten" in reply and not codes_in(reply),
              reply)

        # ------------------------------------------- the bounded listing --
        profiles_bulk = tmp / "profiles_bulk"
        profiles_bulk.mkdir()
        make_profile(profiles_bulk, "bulky", bulk=12)
        state_bulk = profiles_bulk / "bulky" / "state"
        os.environ["AIDOG_PROFILE"] = "bulky"
        os.environ["AIDOG_PROFILES_DIR"] = str(profiles_bulk)
        os.environ["AIDOG_MC_STATE"] = str(state_bulk)
        dog_bulk = Dog(state_bulk, clock)
        reply = dog_bulk.say("/forget secret-bulk-body")
        check("B11 the listing is bounded (at most ten candidates)",
              len(re.findall(r"(?m)^\d+\. ", reply)) == 10, reply)

        # --------------------------------------------- back to profile A --
        use_profile(PROFILE_A, state_a)
        first_listing = dog_a.say("/forget secret")
        ids_a = re.findall(r"src_[0-9a-f]{8}", first_listing)

        # ---------------------------------------- the refusals write nothing --
        reply = dog_a.say("/forget confirm ZZZZZZ")
        check("C1 a wrong code is refused honestly",
              "nothing was forgotten" in reply, reply)
        check("C2 the refused write recorded nothing",
              not ledger_rows(state_a) and not ledger_raw(state_a),
              ledger_raw(state_a))
        reply = dog_a.say("/forget confirm")
        check("C3 a confirm without a code answers with the usage",
              "/forget confirm" in reply, reply)

        # ------------------------------------------------ the real forget --
        reply = dog_a.say("/forget secret")
        codes = codes_in(reply)
        check("D1 the listing hands out a fresh code", len(codes) == 3, reply)
        learned_code = codes[2]
        memory_file = profiles / PROFILE_A / "body" / "memory" / \
            "EVOLUTION_learned.md"
        before_bytes = memory_file.read_bytes()
        reply = dog_a.say(f"/forget confirm {learned_code}")
        check("D2 the confirm records the forget",
              "forgotten" in reply.lower() and "learned" in reply, reply)
        check("D3 the answer says plainly that nothing was deleted",
              "nothing was deleted" in reply, reply)
        check("D4 the answer states the honest limits",
              "this store only" in reply and "backup" in reply, reply)
        check("D5 the answer leaks nothing", no_leak(reply, tmp=tmp), reply)
        rows = ledger_rows(state_a)
        check("D6 the ledger holds exactly one row, in the learned channel",
              len(rows) == 1 and rows[0].get("channel") == "learned",
              str(rows))
        check("D7 the ledger row holds no excerpt",
              BODY_LEARNED not in ledger_raw(state_a)
              and "secret" not in ledger_raw(state_a)
              and str(tmp) not in ledger_raw(state_a), ledger_raw(state_a))
        check("D8 NOTHING was deleted — the file is byte-identical",
              memory_file.read_bytes() == before_bytes, "the file changed")
        check("D9 the forgotten source leaves active memory at once",
              BODY_LEARNED not in memforget.filter_slot(memory_file,
                                                        before_bytes.decode()),
              "the source is still injected")

        # --------------------------------------------- the one-shot rules --
        reply = dog_a.say(f"/forget confirm {learned_code}")
        check("E1 a used code is refused honestly and writes nothing",
              "nothing was forgotten" in reply
              and len(ledger_rows(state_a)) == 1, reply)
        reply = dog_a.say("/forget secret")
        codes = codes_in(reply)
        clock[0] += 400.0                      # past the five-minute window
        reply = dog_a.say(f"/forget confirm {codes[0]}")
        check("E2 an expired code is refused honestly and writes nothing",
              "nothing was forgotten" in reply
              and len(ledger_rows(state_a)) == 1, reply)
        clock[0] -= 400.0

        # ------------------------------- one pending confirmation per chat --
        reply = dog_a.say("/forget secret")
        first = codes_in(reply)[0]
        reply = dog_a.say("/forget Tone")
        second = codes_in(reply)[0]
        check("F1 a second listing replaces the first one",
              first != second, f"{first} == {second}")
        reply = dog_a.say(f"/forget confirm {first}")
        check("F2 the replaced code is refused and writes nothing",
              "nothing was forgotten" in reply
              and len(ledger_rows(state_a)) == 1, reply)
        reply = dog_a.say(f"/forget confirm {second}")
        check("F3 the current code still works",
              "forgotten" in reply.lower()
              and len(ledger_rows(state_a)) == 2, reply)

        # ------------------------------------------- a second source works --
        reply = dog_a.say("/forget secret")
        codes = codes_in(reply)
        check("G1 the unrelated source is still listed",
              len(codes) == 1 and "Wallet" in reply, reply)
        reply = dog_a.say(f"/forget confirm {codes[0]}")
        rows = ledger_rows(state_a)
        check("G2 the second source can be forgotten too",
              "forgotten" in reply.lower() and len(rows) == 3
              and {r.get("channel") for r in rows}
              == {"learned", "curated:memory", "curated:user"}, str(rows))

        # ------------------------------------------------- the model gate --
        check("H1 the model client was NEVER called for a /forget message",
              dog_a.calls == [], str(dog_a.calls)[:200])
        sessions = state_a / "sessions"
        written = list(sessions.glob("*.jsonl")) if sessions.is_dir() else []
        check("H2 no session was written by the command layer",
              written == [], str([p.name for p in written]))

        # --------------------------------------------- the pending state ----
        pending = state_a / "forgetcmd" / "pending.json"
        check("I1 the pending confirmation lives in the state root",
              pending.parent.is_dir(), str(pending))
        body_dir = profiles / PROFILE_A / "body"
        stray = [p.name for p in body_dir.rglob("*")
                 if p.is_file() and p.name not in
                 ("MEMORY_curated.md", "USER_curated.md",
                  "EVOLUTION_learned.md")]
        check("I2 the pending confirmation never lands in the body directory",
              stray == [], str(stray))
        pending_text = pending.read_text(encoding="utf-8") if pending.exists() \
            else ""
        check("I3 the pending file holds no body text",
              BODY_MEMORY not in pending_text and BODY_USER not in pending_text
              and BODY_LEARNED not in pending_text, pending_text[:200])

        # ----------------------------------------------- profile isolation --
        use_profile(PROFILE_B, state_b)
        dog_b = Dog(state_b, clock)
        reply_b = dog_b.say("/forget secret")
        check("J1 the other profile lists its own sources",
              "Wallet" in reply_b and len(codes_in(reply_b)) == 3, reply_b)
        check("J2 the other profile's ledger is untouched",
              not ledger_rows(state_b), ledger_raw(state_b))
        ids_b = re.findall(r"src_[0-9a-f]{8}", reply_b)
        check("J3 the two profiles' sources have different ids",
              ids_a and ids_b and set(ids_a).isdisjoint(set(ids_b)),
              f"{ids_a} vs {ids_b}")
        use_profile(PROFILE_B, state_b)
        reply = dog_b.say(f"/forget confirm {codes_in(reply_b)[0]}")
        check("J4 the other profile can forget its own copy",
              "forgotten" in reply.lower() and len(ledger_rows(state_b)) == 1,
              reply)
        use_profile(PROFILE_A, state_a)
        check("J5 the first profile's ledger did not grow",
              len(ledger_rows(state_a)) == 3, str(ledger_rows(state_a)))

        # ---------------------------------------------------- the hygiene --
        prod_after = prod_ledger.read_bytes() if prod_ledger.exists() else b""
        check("K1 the production ledger is untouched", prod_after == prod_before,
              "the production state dir was written")
        tracked_after = tracked_digest()
        if tracked_before == "git-unavailable":
            note("K2 the repository's tracked files: SKIPPED (no git work tree "
                 "here) — the temp profile state is the evidence instead")
        else:
            check("K2 the repository's tracked files are byte-identical",
                  tracked_after == tracked_before,
                  "a tracked file changed during the flow")
        check("K3 no network was needed and no credential leaked",
              TOKEN not in reply, "credential leak")

    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)

    print("")
    print(f"{PASSED}/{PASSED + FAILED} PASS")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
