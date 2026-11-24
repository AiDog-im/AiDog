"""mycore.profiling — owner profiling over local session history (need-A, P0).

The owner's own words are the only input. This module reads the local session
JSONL files (active + archive) and the anonymous turn ledger, and derives a
ranked keyword profile with **every piece of evidence attached**: message
count, distinct days, first/last seen, the score breakdown that put it in the
list, and dated excerpts. Nothing leaves the machine, and nothing here spends
a model token.

Design authority: `docs/design/owner_profiling_design.md` (P0 scope — analyze
+ the OWNER ANALYSIS view + the SNIFF reopen surface; proactive search and
push are a later increment and do not exist here).

Contracts enforced here (and asserted by mycore/tests/run.py):

* ZERO MODEL TOKENS, BY CONSTRUCTION. This module imports no model client, no
  loop, no delivery path — the absence is a test assertion (the same trick
  `mycore.sniff` uses for its read-only promise). Tokenization is a fixed
  regex pass, scoring is arithmetic, classification is a rule table with an
  id per label.
* LOCAL ONLY. Reads `<state>/sessions/**`, `<state>/turns/*`; writes exactly
  one file, `<state>/profiling/profile.json` (gitignored runtime state). No
  transport seam exists at all in P0, so no code path here can open a socket.
* HONEST EVIDENCE. A keyword seen once, on one day, is marked `weak` and is
  never presented as an established interest. An empty window returns no rows
  and a reason string — never a fabricated row. Corpus truncation is reported,
  never silent.
* BOUNDED. Message/char caps per run, a top-K cap, an excerpt cap and a
  per-excerpt char cap. A huge corpus degrades in a stated way.
* HONOURED SUPPRESSIONS. A derived item is a source like any other: the
  tombstone ledger (mycore.memforget) silences it in the `profile` channel.
  A silenced token is excluded when the profile is BUILT (so the stored
  document, the score, the tallies and the keyword list are computed
  without it) and when a stored profile is READ (so a suppression takes
  effect at the next read, with no re-analysis and no restart). The
  transcript rows it was derived from are never touched (D10).
* ENGLISH SOURCE (repo i18n policy). Chinese lexicons are unicode escapes,
  exactly as `mycore.mission` does for its reporter copy.

Label vocabulary (each row carries the rule id that fired):

  trait   keen      a configured sniff source covers it, OR the owner asked to
                    be watched for it (proactive lexicon)  -> SNIFF+proactive
          gap       nothing covers it                      -> retrieval gap
          partial   1 curated-memory hit                   -> empathy partial
          growth    2+ curated hits AND recurring (3+ days) -> learning growth
          known     2+ curated hits, not recurring         -> honest remainder
  topic   owner-extensible lexicon (settings) or `other` when nothing matches
  tier    L1 urgency lexicon hit AND trait `keen`   (act now)
          L2 3+ messages AND last seen within 7 days (useful update)
          L3 everything else that survived the cut    (background)
  cover   gap | partial | covered  (against the curated memory files)
"""
from __future__ import annotations

import json
import fnmatch
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib import profile_paths as pp  # noqa: E402  (profile state seam)
from mycore import memforget  # noqa: E402  (memory suppression, D5/D7)
from mycore.session import text_digest  # noqa: E402  (the session identity form)

REPO = Path(__file__).resolve().parent.parent

# ----------------------------------------------------------------- bounds ---
MAX_MESSAGES = 50_000            # messages read per run
MAX_CHARS = 5_000_000            # content chars read per run
DEFAULT_TOP_K = 100
MAX_TOP_K = 200
MIN_TOP_K = 5
DEFAULT_HALF_LIFE = 14.0         # days; recency half-life
MAX_HALF_LIFE = 365.0
DEFAULT_INTERVAL_S = 3600        # service leg throttle
MIN_INTERVAL_S = 60
MAX_INTERVAL_S = 86400
MAX_EXCERPTS = 3
EXCERPT_CHARS = 120
MAX_TOKEN_CHARS = 40
MAX_TOPICS = 24
MAX_TOPIC_TERMS = 40
MAX_TERM_CHARS = 40
MAX_EXCLUDE = 16                 # file-name globs never read
MAX_PINNED = 64                   # owner-pinned keywords
MAX_PINNED_CHARS = 60
PUSH_MODES = ("passive", "auto")  # D6: the closed loop's push mode
MAX_MESSAGES_PER_MSG = 400       # tokens counted per message
MAX_STRONG_DAYS = 3              # `growth` needs this many distinct days
L2_MIN_MSGS = 3                  # `L2` needs this many messages
L2_MAX_AGE_DAYS = 7.0            # ... and a hit this recent
WEAK_MSGS = 1                    # evidence <= this on one day = `weak`
# A human does not send the same sentence 400 times; a test harness does.
# Identical normalized texts count at most this many times each — the rest
# are collapsed and REPORTED (`duplicates_collapsed`), never silently kept
# and never silently dropped.
MAX_DUP_COUNT = 3

STATE_DIR = "profiling"
PROFILE_FILE = "profile.json"
TIMEOUT_BUDGET_S = 20.0          # one run's wall-clock guard (stated if hit)

CONFIG_KEYS = frozenset({"enabled", "since", "top_k", "half_life_days",
                         "interval_s", "topics", "exclude", "push_mode",
                         "pinned"})
DEFAULT_SETTINGS: dict = {
    "enabled": True,
    "since": "",                 # "" = the earliest retained owner message
    "top_k": DEFAULT_TOP_K,
    "half_life_days": DEFAULT_HALF_LIFE,
    "interval_s": DEFAULT_INTERVAL_S,
    "topics": {},
    # Session FILE-NAME globs never read: e2e/webhook fixtures write real
    # session files full of canned prompts. Excluding them by name is
    # cheaper and more honest than guessing from the content.
    "exclude": ["webhook_e2e*"],
    # Push mode for the closed loop (D6): `passive` records the urgent rows
    # on the board and never speaks them; `auto` lets the owner delivery
    # chain speak them. It ships PASSIVE.
    "push_mode": "passive",
    # Keywords the owner pinned by hand. A pinned keyword is `keen` by the
    # owner's own decision, not by a guess.
    "pinned": [],
}

_BJ = timezone(timedelta(hours=8))       # owner timezone (cron/quiet parity)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")

# ------------------------------------------------------------- lexicons ---
# Chinese terms are unicode escapes so this source file stays ASCII for
# tests/source_lang_check.py (same convention as mycore.mission's ZH table).
URGENCY_ZH = (
    "\u5c3d\u5feb",              # as soon as possible
    "\u9a6c\u4e0a",              # right away
    "\u8d76\u7d27",              # hurry
    "\u7acb\u523b",              # immediately
    "\u73b0\u5728\u5c31",        # starting now
    "\u622a\u6b62",              # deadline
    "\u4eca\u5929\u8981",        # needed today
    "\u522b\u5fd8\u4e86",        # do not forget
    "\u63d0\u9192\u6211",        # remind me
    "\u6025",                    # urgent
    "\u91cd\u8981",              # important
)
URGENCY_EN = ("asap", "deadline", "today", "tonight", "urgent", "must",
              "immediately", "soon")

PROACTIVE_ZH = (
    "\u76ef\u7740",              # keep an eye on
    "\u5173\u6ce8",              # pay attention to
    "\u901a\u77e5\u6211",        # notify me
    "\u6709\u53d8\u5316",        # if it changes
    "\u5e2e\u6211\u770b\u7740",  # watch it for me
    "\u76d1\u6d4b",              # monitor
    "\u6ce8\u610f",              # watch out for
)
PROACTIVE_EN = ("monitor", "watch", "notify", "alert", "remind",
                "keep an eye")

# Chinese particles, pronouns and light verbs. A single character here is
# never a token; a bigram made of two such characters is dropped too.
STOP_ZH_CHARS = frozenset((
    "\u7684", "\u4e86", "\u5462", "\u5427", "\u4e48", "\u90fd",
    "\u5c31", "\u4f60", "\u6211", "\u4ed6", "\u5979", "\u5b83",
    "\u4eec", "\u8fd9", "\u90a3", "\u4e0d", "\u5f88", "\u4e5f",
    "\u8fd8", "\u5728", "\u662f", "\u548c", "\u4e0e", "\u6216",
    "\u6709", "\u8981", "\u4f1a", "\u80fd", "\u628a", "\u88ab",
    "\u7ed9", "\u5bf9", "\u4ece", "\u5230", "\u8bf4", "\u53bb",
    "\u6765", "\u4e0a", "\u4e0b", "\u4e2a", "\u4e9b", "\u4ec0",
    "\u600e", "\u5417", "\u5440", "\u554a", "\u561b", "\u54e6",
    "\u55ef", "\u8ba9", "\u4f3c", "\u518d", "\u53c8", "\u6700",
    "\u66f4", "\u5f97", "\u7740", "\u4f46", "\u800c", "\u5374",
))

STOP_EN = frozenset("""
a about after all also an and any are as at be been before but by can could
do does did for from get got had has have how i if in into is it its just
like make may me might more most much my need no not now of on once only or
other our out over own same see should so some such than that the their them
then there these they this those through to too under up us use very want
was we were what when where which while who why will with would you your
yes ok okay please thanks thank hi hello hey
""".split())

# Built-in topic lexicon — a starting shape the owner can extend through
# settings `mycore.profiling.topics` (slug -> list of terms).
DEFAULT_TOPICS: dict[str, tuple[str, ...]] = {
    "chain": ("\u516c\u94fe", "\u94fe\u4e0a", "\u94b1\u5305", "\u5408\u7ea6",
              "\u4ee3\u5e01", "\u8d28\u62bc", "chain", "wallet", "contract",
              "token"),
    "markets": ("\u884c\u60c5", "\u4ef7\u683c", "\u4ea4\u6613\u6240",
                "\u4ed3\u4f4d", "\u76c8\u4e8f", "market", "price", "trade",
                "position"),
    "product": ("\u4ea7\u54c1", "\u7248\u672c", "\u53d1\u7248", "\u529f\u80fd",
                "\u63a7\u5236\u53f0", "\u667a\u80fd\u72d7", "product",
                "release", "version", "console"),
    "engineering": ("\u4ee3\u7801", "\u6d4b\u8bd5", "\u7f16\u8bd1",
                    "\u90e8\u7f72", "\u63a5\u53e3", "\u6570\u636e\u5e93",
                    "code", "test", "build", "deploy", "api", "database"),
    "home": ("\u5bb6\u91cc", "\u5bb6\u7535", "\u4f20\u611f\u5668",
             "\u6e29\u5ea6", "home", "sensor", "temperature", "light"),
    "health": ("\u5065\u5eb7", "\u7761\u7720", "\u5fc3\u7387", "\u8fd0\u52a8",
               "\u4f53\u68c0", "health", "sleep", "heart", "exercise"),
    "work": ("\u5ba2\u6237", "\u83b7\u5ba2", "\u90ae\u4ef6", "\u4f1a\u8bae",
             "\u5408\u540c", "\u6c47\u62a5", "customer", "lead", "email",
             "meeting", "contract"),
    "learning": ("\u5b66\u4e60", "\u7814\u7a76", "\u8bba\u6587",
                 "\u6559\u7a0b", "\u6587\u6863", "learn", "research",
                 "paper", "tutorial", "docs"),
}

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]{1,%d}" % (MAX_TOKEN_CHARS - 1))
_WORD_BOUNDARY = r"(?<![a-z0-9])%s(?![a-z0-9])"


class ProfilingError(RuntimeError):
    """Chat-safe configuration failure: field names and expected shapes only,
    never a value (settings carry no credentials here, and none are read)."""


# ------------------------------------------------------------ config parse ---
def _int(block: dict, key: str, default: int, lo: int, hi: int) -> int:
    val = block.get(key, default)
    if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
        raise ProfilingError(f"mycore.profiling.{key} must be an integer "
                             f"{lo}..{hi}")
    return val


def _half_life(block: dict) -> float:
    val = block.get("half_life_days", DEFAULT_HALF_LIFE)
    if isinstance(val, bool) or not isinstance(val, (int, float)) \
            or not 1 <= float(val) <= MAX_HALF_LIFE:
        raise ProfilingError("mycore.profiling.half_life_days must be a "
                             f"number 1..{int(MAX_HALF_LIFE)}")
    return float(val)


def parse_config(block: object) -> dict:
    """Parse settings `mycore.profiling`. First error raises ProfilingError
    (the console write gate refuses a bad document whole)."""
    if not isinstance(block, dict):
        raise ProfilingError("mycore.profiling must be an object")
    extra = sorted(set(block) - CONFIG_KEYS)
    if extra:
        raise ProfilingError(f"mycore.profiling: unknown field '{extra[0]}'")
    enabled = block.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ProfilingError("mycore.profiling.enabled must be true or false")
    since = block.get("since", "")
    if since is None:
        since = ""
    if not isinstance(since, str) or (since and not _DATE_RE.match(since)):
        raise ProfilingError("mycore.profiling.since must be \"\" or "
                             "YYYY-MM-DD")
    if since:
        try:
            datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            raise ProfilingError("mycore.profiling.since is not a real date"
                                 ) from None
    topics_raw = block.get("topics", {})
    if topics_raw is None:
        topics_raw = {}
    topics: dict[str, list[str]] = {}
    if isinstance(topics_raw, dict):
        # Hand-edit shape: {"slug": ["term", ...]}.
        items = list(topics_raw.items())
    elif isinstance(topics_raw, list):
        # Console shape: [{"slug": "mine", "terms": ["alpha", ...]}].
        # A LIST (like sniff's sources/rules) is REPLACED by the settings
        # merge; a dict would be merged key-by-key and could never be
        # cleared from the console. The list shape is therefore the one the
        # console writes, and both shapes are accepted on read.
        items = []
        for entry in topics_raw:
            if not isinstance(entry, dict):
                raise ProfilingError("mycore.profiling.topics: every entry "
                                     "must be an object with slug + terms")
            unknown = sorted(set(entry) - {"slug", "terms"})
            if unknown:
                raise ProfilingError("mycore.profiling.topics: unknown field "
                                     f"'{unknown[0]}'")
            items.append((entry.get("slug"), entry.get("terms")))
    else:
        raise ProfilingError("mycore.profiling.topics must be a list of "
                             "{slug, terms} objects")
    if len(items) > MAX_TOPICS:
        raise ProfilingError(f"mycore.profiling.topics: at most {MAX_TOPICS} "
                             "topics")
    for slug, terms in items:
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise ProfilingError("mycore.profiling.topics: every key must be "
                                 "a slug (a-z, 0-9, -, _)")
        if not isinstance(terms, list) or not terms:
            raise ProfilingError(f"mycore.profiling.topics.{slug} must be a "
                                 "non-empty list of terms")
        if len(terms) > MAX_TOPIC_TERMS:
            raise ProfilingError(f"mycore.profiling.topics.{slug}: at most "
                                 f"{MAX_TOPIC_TERMS} terms")
        clean: list[str] = []
        for term in terms:
            if not isinstance(term, str) or not term.strip() \
                    or len(term) > MAX_TERM_CHARS:
                raise ProfilingError(
                    f"mycore.profiling.topics.{slug}: every term must be a "
                    f"non-empty string of at most {MAX_TERM_CHARS} chars")
            clean.append(term.strip())
        topics[slug] = clean
    exclude_raw = block.get("exclude", DEFAULT_SETTINGS["exclude"])
    if exclude_raw is None:
        exclude_raw = []
    if not isinstance(exclude_raw, list) or len(exclude_raw) > MAX_EXCLUDE:
        raise ProfilingError("mycore.profiling.exclude must be a list of at "
                             f"most {MAX_EXCLUDE} file-name patterns")
    exclude: list[str] = []
    for pat in exclude_raw:
        if not isinstance(pat, str) or not pat.strip() \
                or len(pat) > MAX_TERM_CHARS:
            raise ProfilingError("mycore.profiling.exclude: every pattern "
                                 "must be a non-empty string of at most "
                                 f"{MAX_TERM_CHARS} chars")
        exclude.append(pat.strip())
    push_mode = block.get("push_mode", DEFAULT_SETTINGS["push_mode"])
    if push_mode is None:
        push_mode = DEFAULT_SETTINGS["push_mode"]
    if push_mode not in PUSH_MODES:
        raise ProfilingError("mycore.profiling.push_mode must be one of: "
                             + "|".join(PUSH_MODES))
    pinned_raw = block.get("pinned", [])
    if pinned_raw is None:
        pinned_raw = []
    if not isinstance(pinned_raw, list) or len(pinned_raw) > MAX_PINNED:
        raise ProfilingError(f"mycore.profiling.pinned must be a list of at "
                             f"most {MAX_PINNED} keywords")
    pinned: list[str] = []
    for kw in pinned_raw:
        if not isinstance(kw, str) or not kw.strip() \
                or len(kw) > MAX_PINNED_CHARS:
            raise ProfilingError("mycore.profiling.pinned: every keyword must "
                                 f"be a non-empty string of at most "
                                 f"{MAX_PINNED_CHARS} chars")
        clean = kw.strip()
        if clean not in pinned:
            pinned.append(clean)
    return {
        "enabled": enabled,
        "since": since,
        "top_k": _int(block, "top_k", DEFAULT_TOP_K, MIN_TOP_K, MAX_TOP_K),
        "half_life_days": _half_life(block),
        "interval_s": _int(block, "interval_s", DEFAULT_INTERVAL_S,
                           MIN_INTERVAL_S, MAX_INTERVAL_S),
        "topics": topics,
        "exclude": exclude,
        "push_mode": push_mode,
        "pinned": pinned,
    }


def settings_block(repo: Path | None = None) -> dict:
    """The `mycore.profiling` settings partition. {} on any read/parse failure
    (fail closed on absence, never crash — the service reader's contract)."""
    try:
        doc = json.loads(pp.settings_path(repo or REPO).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    mc = doc.get("mycore")
    block = mc.get("profiling") if isinstance(mc, dict) else None
    return block if isinstance(block, dict) else {}


# ------------------------------------------------------------ tokenizing ---
def _ascii_terms(text: str) -> list[str]:
    out: list[str] = []
    for word in _ASCII_RE.findall(text):
        low = word.lower().strip(".-")
        if len(low) < 2 or low in STOP_EN:
            continue
        out.append(low)
    return out


def _cjk_terms(text: str) -> list[str]:
    """Bigrams are the phrase unit (no segmentation library, no model); a
    lone character survives only when it is not a particle/pronoun."""
    out: list[str] = []
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            if run not in STOP_ZH_CHARS:
                out.append(run)
            continue
        for i in range(len(run) - 1):
            pair = run[i:i + 2]
            if pair[0] in STOP_ZH_CHARS and pair[1] in STOP_ZH_CHARS:
                continue
            out.append(pair)
    return out


def tokenize(text: str) -> list[str]:
    """Deterministic tokens for one message: latin words + CJK bigrams."""
    return _ascii_terms(text) + _cjk_terms(text)


# ---------------------------------------------------------- suppression ---
# A derived profile item is a SOURCE like any other (D2/D5/D7): its opaque
# id is the usual one over (profile scope, channel, content digest), where
# the content is the token text. The ledger never holds the token itself —
# only its digest — so the match is by digest.
#
# Two rules make the suppression honest:
#   * BUILD time — a silenced token never enters the profile, so the stored
#     document, the score, the tallies and the keyword list are computed
#     WITHOUT it (a re-analysis cannot reintroduce it);
#   * READ time — a stored profile is filtered on the way out, so the
#     suppression takes effect at the next read, with no tick and no
#     restart.
#
# The transcript rows the token was derived from are never touched (D10).
def profile_source_id(token: str, *, repo: Path | None = None) -> str:
    """The opaque, deterministic id of a derived profile item (D2).

    `memforget.source_id(CHANNEL_PROFILE, token)` — the same derivation as
    every other source, so a derived item is addressable with the same
    opaque id. The id carries no path, no excerpt and no credential."""
    return memforget.source_id(memforget.CHANNEL_PROFILE, token or "",
                                repo=repo)


class _Suppression:
    """Which derived tokens the ledger silences (D5/D7).

    The ledger holds digests, not tokens, so a token is matched by its own
    content digest. The digest is computed ONCE per distinct token — a run
    sees the same token in thousands of messages — and the whole check is
    skipped while the profile channel holds no tombstone at all (the normal
    state: nothing is forgotten)."""
    __slots__ = ("_digests", "_ids", "_cache", "_possible")

    def __init__(self, snapshot: memforget.TombstoneSet | None = None):
        self._digests: set[str] = set()
        self._ids: frozenset = frozenset()
        self._cache: dict[str, bool] = {}
        self._possible = False
        if snapshot is None:
            return
        for channel, digest in (snapshot.pairs or ()):
            if channel == memforget.CHANNEL_PROFILE:
                self._digests.add(digest)
        self._ids = frozenset(snapshot.ids or ())
        rows = list(snapshot.rows or ())
        if rows:
            # The rows carry the channel: a tombstone in ANOTHER store can
            # never silence a derived item (D11), so the whole check can be
            # skipped while no profile-channel row exists.
            self._possible = any(r.get("channel") == memforget.CHANNEL_PROFILE
                                 for r in rows)
        else:
            # A merged set carries no rows (the channels are unknown then):
            # assume the worst and check.
            self._possible = bool(self._digests or self._ids)

    @property
    def empty(self) -> bool:
        """Can this set silence a derived item at all? (The normal state:
        nothing is forgotten — the check costs nothing then.)"""
        return not self._possible

    def suppresses(self, token: str) -> bool:
        """Is this derived token silenced right now?"""
        if not token or self.empty:
            return False
        hit = self._cache.get(token)
        if hit is None:
            hit = (memforget.digest_of(token) in self._digests
                   or memforget.source_id(memforget.CHANNEL_PROFILE, token)
                   in self._ids)
            self._cache[token] = hit
        return hit


def _has_term(text_low: str, term: str) -> bool:
    """Substring test for CJK terms, word-boundary test for latin ones (so
    `watch` never matches inside `watcher` by accident... except that it is
    the same intent — the boundary keeps it honest either way)."""
    if not term:
        return False
    if _CJK_RUN_RE.search(term):
        return term in text_low
    return re.search(_WORD_BOUNDARY % re.escape(term.lower()), text_low,
                     re.IGNORECASE) is not None


def _urgency_hit(text_low: str) -> bool:
    return any(_has_term(text_low, t) for t in URGENCY_ZH + URGENCY_EN)


def _proactive_hit(text_low: str) -> bool:
    return any(_has_term(text_low, t) for t in PROACTIVE_ZH + PROACTIVE_EN)


# Scoring lexicons are function words, not interests: a token that IS one of
# them never enters the keyword list (it shapes the urgency/proactive flag of
# the message, nothing else).
_SCORING_TERMS = frozenset(
    t.casefold() for t in (URGENCY_ZH + URGENCY_EN + PROACTIVE_ZH
                           + PROACTIVE_EN))


# --------------------------------------------------------------- reading ---
def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, _BJ).strftime("%Y-%m-%d")


def _iter_jsonl(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            continue                     # a half-written tail line is skipped
        if isinstance(doc, dict):
            yield doc


def session_files(root: Path, exclude: tuple[str, ...] = ()) -> list[Path]:
    """Every session JSONL under one state root: the live dir + its archive.

    `exclude` holds file-name globs (settings `mycore.profiling.exclude`): an
    e2e/webhook fixture writes real session files full of canned prompts, and
    excluding them by name is cheaper and more honest than guessing from the
    content. Matching is case-insensitive on the file NAME only."""
    pats = tuple(p.casefold() for p in exclude if p)
    out: list[Path] = []
    for base in (root / "sessions", root / "sessions" / "archive"):
        try:
            for path in sorted(base.glob("*.jsonl")):
                if not path.is_file():
                    continue
                name = path.name.casefold()
                if any(fnmatch.fnmatchcase(name, pat) for pat in pats):
                    continue
                out.append(path)
        except OSError:
            continue
    return out


def _dedup_key(text: str) -> str:
    """Normalized text identity: whitespace collapsed, casefolded. A harness
    repeating one canned prompt lands on one key; the owner's own prose is
    naturally distinct."""
    return " ".join(text.split()).casefold()


def _channel_of(name: str) -> str:
    """Which surface a session file belongs to, WITHOUT exposing the chat id:
    the console's own sessions are `web.<ts>.jsonl`, every other file is a
    chat session. The owner must be able to see WHERE a keyword came from
    (a load test is not an interest), and the identifier itself never has to
    leave the machine to say that."""
    stem = name[:-6] if name.endswith(".jsonl") else name
    head = stem.split(".", 1)[0].casefold()
    return "console" if head == "web" else "chat"


def collect_messages(root: Path, *, since: str = "",
                     exclude: tuple[str, ...] = (),
                     max_messages: int = MAX_MESSAGES,
                     max_chars: int = MAX_CHARS,
                     suppression=None) -> dict:
    """Owner messages from the local session history.

    Only `role == "user"` content counts: the assistant's and the tools' text
    is the product's words, not the owner's, and must never shape his profile.
    Identical normalized texts are capped at MAX_DUP_COUNT each (a harness
    repeating one prompt is not evidence of interest) and the collapse is
    reported. Returns {messages, corpus, truncated} where each message is
    (ts, day, text, had_urgency, had_proactive) — text already capped.

    A FORGOTTEN message never enters the corpus (D31): the profile is built
    from the owner's own words, so a message the owner silenced must not
    regenerate a profile item — not now, and not on the next rebuild. The
    message is checked in its IDENTITY FORM (the normalised text the
    session index uses), so the check agrees with the ledger on both sides.
    """
    since_ts = 0.0
    if since:
        try:
            since_ts = datetime.strptime(since, "%Y-%m-%d").replace(
                tzinfo=_BJ).timestamp()
        except ValueError:
            since_ts = 0.0
    if suppression is None:
        suppression = memforget.ChannelSuppression(memforget.CHANNEL_SESSION,
                                                   None)
    messages: list[tuple[float, str, str, bool, bool]] = []
    total_chars = 0
    scanned = 0
    truncated = False
    collapsed = 0
    channels: dict[str, int] = {}
    seen_texts: dict[str, int] = {}
    files = session_files(root, exclude)
    for path in files:
        channel = _channel_of(path.name)
        for doc in _iter_jsonl(path):
            if doc.get("role") != "user":
                continue
            text = doc.get("content")
            if not isinstance(text, str) or not text.strip():
                continue
            if not suppression.empty and suppression.suppresses_digest(
                    text_digest(text)):
                continue              # a forgotten message is not evidence
            ts = doc.get("ts")
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                ts = 0.0
            ts = float(ts)
            if since and ts and ts < since_ts:
                continue
            scanned += 1
            if len(messages) >= max_messages or total_chars >= max_chars:
                truncated = True
                break
            body = text.strip()[:2000]
            key = _dedup_key(body)
            hits = seen_texts.get(key, 0)
            if hits >= MAX_DUP_COUNT:
                collapsed += 1
                continue
            seen_texts[key] = hits + 1
            total_chars += len(body)
            channels[channel] = channels.get(channel, 0) + 1
            low = body.casefold()
            messages.append((ts, _day_of(ts) if ts else "", body,
                             _urgency_hit(low), _proactive_hit(low)))
        if truncated:
            break
    days = sorted({m[1] for m in messages if m[1]})
    return {
        "messages": messages,
        "corpus": {
            "files": len(files),
            "messages_scanned": scanned,
            "messages_used": len(messages),
            "duplicates_collapsed": collapsed,
            "chars": total_chars,
            "channels": dict(sorted(channels.items())),
            "days": days,
            "first_day": days[0] if days else "",
            "last_day": days[-1] if days else "",
        },
        "truncated": truncated,
    }


def collect_rhythm(root: Path) -> dict:
    """The anonymous turn ledger: per-day turn counts and the scenario mix.
    It carries no content by contract (`mycore.ledger`), so it can only ever
    contribute rhythm — never a keyword."""
    counts: dict[str, int] = {}
    scenarios: dict[str, int] = {}
    for path in sorted((root / "turns").glob("*.jsonl")) \
            if (root / "turns").is_dir() else []:
        for doc in _iter_jsonl(path):
            day = doc.get("day")
            if isinstance(day, str) and _DATE_RE.match(day):
                counts[day] = counts.get(day, 0) + 1
            kind = doc.get("scenario")
            if isinstance(kind, str) and kind:
                scenarios[kind] = scenarios.get(kind, 0) + 1
    return {"turns_by_day": dict(sorted(counts.items())),
            "scenarios": dict(sorted(scenarios.items()))}


def curated_memory_text(repo: Path | None = None) -> str:
    """The two curated relationship-memory files, concatenated (casefolded).
    Absent files are simply absent — a missing profile is not an error.

    Two fixes live here. The slots resolve through the ACTIVE PROFILE
    (pp.memory_slots) instead of the fixed <repo>/overlay/memory path, so a
    profile's profile is built from ITS OWN curated slots — the default
    profile resolves to the very same files as before. And every
    tombstoned entry is filtered out (D5), so a rebuilt profile cannot
    reintroduce a forgotten source (D7)."""
    root = Path(repo) if repo is not None else REPO
    mem, usr = pp.memory_slots(root)
    parts: list[str] = []
    for path in (usr, mem):          # the historical order: user, then memory
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            parts.append(memforget.filter_slot(path, text))
        except memforget.LedgerUnavailable:
            return ""
    return "\n".join(parts).casefold()


def sniff_source_terms(repo: Path | None = None) -> tuple[str, ...]:
    """Ids and entity globs of the configured sniff sources: a keyword the
    owner is already watching is `keen` by definition."""
    from . import sniff  # local import: keeps this module's import surface flat
    terms: list[str] = []
    try:
        block = sniff.settings_block(repo or REPO)
        cfg = sniff.parse_config(block, strict_rules=False)
    except Exception:  # noqa: BLE001 — an unreadable board is not an error here
        return ()
    for src in cfg["sources"]:
        if not src.get("enabled", True):
            continue
        if src.get("id"):
            terms.append(str(src["id"]).casefold())
        for pattern in src.get("entities", []) or []:
            clean = str(pattern).strip().casefold().rstrip("*").rstrip(".")
            if len(clean) >= 3:
                terms.append(clean)
    return tuple(terms)


# ------------------------------------------------------------ classifying ---
def _topic_of(token: str, text_low: str, topics: dict) -> str:
    """Topic by lexicon match, in two deterministic passes: the token's own
    match first (the strongest signal), then a per-topic count over the
    message body it came from — the highest count wins, ties go to the slug
    in sorted order. No match is `other` — never a guess."""
    token_low = token.casefold()
    for slug in sorted(topics):
        for term in topics[slug]:
            if token_low == term.casefold():
                return slug
    best_slug, best_hits = "", 0
    for slug in sorted(topics):
        hits = sum(1 for term in topics[slug] if _has_term(text_low, term))
        if hits > best_hits:
            best_slug, best_hits = slug, hits
    return best_slug or "other"


def _coverage(token: str, curated_low: str) -> tuple[str, int]:
    """gap / partial / covered against the curated memory files."""
    if not curated_low:
        return "gap", 0
    hits = curated_low.count(token.casefold()) if token else 0
    if hits >= 2:
        return "covered", hits
    if hits == 1:
        return "partial", hits
    return "gap", 0


def _snippet(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= EXCERPT_CHARS:
        return flat
    return flat[:EXCERPT_CHARS].rstrip() + "\u2026"


def analyze(now: float | None = None, *, block: dict | None = None,
            root: Path | None = None, repo: Path | None = None,
            curated: str | None = None,
            source_terms: tuple[str, ...] | None = None,
            pinned: tuple[str, ...] | None = None,
            tombstones: memforget.TombstoneSet | None = None) -> dict:
    """One analysis run. Pure over its inputs (every seam is injectable), so
    the suite runs hermetically and production passes nothing.

    `tombstones` is the suppression set (mycore.memforget.TombstoneSet);
    without it the ledger of the state root this run reads decides which
    derived items are silenced. A hermetic run passes an empty set (or a
    temp state root) so it never reads the production ledger.

    Returns the report the caller persists:
      {ok, at, config, corpus, truncated, reason, keywords[], rhythm}
    """
    started = time.time()
    t = float(now if now is not None else time.time())
    raw = block if block is not None else settings_block(repo)
    try:
        cfg = parse_config(raw)
    except ProfilingError as e:
        return {"ok": False, "at": t, "reason": "config",
                "error": str(e)[:160], "keywords": [], "corpus": {},
                "truncated": False, "rhythm": {}}
    state_root = root if root is not None else pp.state_dir(repo or REPO)
    # ONE ledger read serves both channels: the derived profile items and
    # the transcript messages the corpus is built from (D31/D34).
    try:
        snapshot = (tombstones if tombstones is not None
                    else memforget.load_set(repo, state=state_root))
    except memforget.LedgerUnavailable:
        return {"ok": False, "at": t, "reason": "suppression-unavailable",
                "error": memforget.LEDGER_UNAVAILABLE, "keywords": [],
                "corpus": {}, "truncated": False, "rhythm": {}}
    session_suppression = memforget.ChannelSuppression(
        memforget.CHANNEL_SESSION, snapshot)
    got = collect_messages(state_root, since=cfg["since"],
                           exclude=tuple(cfg["exclude"]),
                           suppression=session_suppression)
    corpus = got["corpus"]
    messages = got["messages"]
    rhythm = collect_rhythm(state_root)
    if not messages:
        reason = (f"no data since {cfg['since']}" if cfg["since"]
                  else "no data yet")
        return {"ok": True, "at": t, "reason": reason, "config": cfg,
                "corpus": corpus, "truncated": got["truncated"],
                "keywords": [], "rhythm": rhythm}

    curated_low = (curated if curated is not None
                   else curated_memory_text(repo))
    sniff_terms = (source_terms if source_terms is not None
                   else sniff_source_terms(repo))
    pin_set = {str(p).casefold() for p in
               (pinned if pinned is not None else cfg["pinned"]) if p}
    topics = {slug: list(terms) for slug, terms in DEFAULT_TOPICS.items()}
    for slug, terms in cfg["topics"].items():      # owner additions win
        topics[slug] = list(terms)

    # ---- accumulate evidence per token -----------------------------------
    # The ledger of the state root this run reads (the profile's own state
    # root in production) decides which DERIVED items are silenced: a
    # silenced token never enters the profile, so the stored document, the
    # score, the tallies and the keyword list are computed WITHOUT it and a
    # re-analysis cannot reintroduce it (D7).
    suppression = _Suppression(snapshot)
    docs = {day: set() for day in corpus["days"]}
    per_token: dict[str, dict] = {}
    for ts, day, text, urg, pro in messages:
        low = text.casefold()
        tokens = tokenize(text)[:MAX_MESSAGES_PER_MSG]
        seen_here: set[str] = set()
        for token in tokens:
            if token in _SCORING_TERMS:      # urgency/proactive words: flags only
                continue
            if suppression.suppresses(token):   # a forgotten derived item
                continue
            row = per_token.get(token)
            if row is None:
                row = per_token[token] = {
                    "token": token, "tf": 0, "first": ts or t,
                    "last": ts or t, "days": set(),
                    "urgency": False, "proactive": False,
                    "excerpts": [], "sample": low}
            row["tf"] += 1
            if ts:
                row["first"] = min(row["first"], ts)
                row["last"] = max(row["last"], ts)
            if day:
                row["days"].add(day)
            row["urgency"] = row["urgency"] or urg
            row["proactive"] = row["proactive"] or pro
            if token not in seen_here and len(row["excerpts"]) < MAX_EXCERPTS:
                row["excerpts"].append({"day": day,
                                        "ts": ts or 0.0,
                                        "text": _snippet(text)})
            seen_here.add(token)
        for token in seen_here:
            if day:
                docs.setdefault(day, set()).add(token)

    n_days = max(1, len(corpus["days"]))
    half_life = cfg["half_life_days"]
    ranked: list[dict] = []
    for token, row in per_token.items():
        df = sum(1 for day, toks in docs.items() if token in toks)
        df = max(1, df)
        idf = math.log(1.0 + n_days / df)
        age_days = max(0.0, (t - row["last"]) / 86400.0)
        recency = 0.5 ** (age_days / half_life)
        urgency = 1.3 if row["urgency"] else 1.0
        score = row["tf"] * idf * recency * urgency
        coverage, curated_hits = _coverage(token, curated_low)
        token_low = token.casefold()
        watched = any(token_low in term or term in token_low
                      for term in sniff_terms if term)
        pinned_hit = token_low in pin_set
        if watched or pinned_hit or row["proactive"]:
            trait = "keen"
        elif coverage == "gap":
            trait = "gap"
        elif coverage == "partial":
            trait = "partial"
        elif len(row["days"]) >= MAX_STRONG_DAYS:
            trait = "growth"
        else:
            trait = "known"
        if row["urgency"] and trait == "keen":
            tier = "L1"
        elif row["tf"] >= L2_MIN_MSGS and age_days <= L2_MAX_AGE_DAYS:
            tier = "L2"
        else:
            tier = "L3"
        ranked.append({
            "token": token,
            "score": round(score, 6),
            "tf": row["tf"],
            "df_days": df,
            "days": len(row["days"]),
            "first_seen": row["first"],
            "last_seen": row["last"],
            "first_day": _day_of(row["first"]) if row["first"] else "",
            "last_day": _day_of(row["last"]) if row["last"] else "",
            "idf": round(idf, 4),
            "recency": round(recency, 4),
            "urgency": round(urgency, 2),
            "trait": trait,
            "topic": _topic_of(token, row["sample"], topics),
            "tier": tier,
            "coverage": coverage,
            "curated_hits": curated_hits,
            "watched": bool(watched),
            "pinned": bool(pinned_hit),
            "weak": row["tf"] <= WEAK_MSGS and len(row["days"]) <= 1,
            "excerpts": row["excerpts"][:MAX_EXCERPTS],
        })
    ranked.sort(key=lambda r: (-r["score"], r["token"]))
    keywords = ranked[:cfg["top_k"]]
    reason = ""
    if got["truncated"]:
        reason = "corpus truncated (message/char cap reached)"
    if time.time() - started > TIMEOUT_BUDGET_S:
        reason = (reason + "; " if reason else "") + "run exceeded the time budget"
    out = {"ok": True, "at": t, "reason": reason, "config": cfg,
           "corpus": corpus, "truncated": got["truncated"],
           "keywords": keywords, "rhythm": rhythm,
           "counts": _counts(keywords)}
    return out


def _counts(keywords: list[dict]) -> dict:
    def tally(key: str) -> dict:
        out: dict[str, int] = {}
        for row in keywords:
            val = str(row.get(key) or "")
            if val:
                out[val] = out.get(val, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))
    return {"traits": tally("trait"), "topics": tally("topic"),
            "tiers": tally("tier"), "coverage": tally("coverage"),
            "weak": sum(1 for r in keywords if r["weak"])}


# ----------------------------------------------------------------- state ---
def state_dir(repo: Path | None = None) -> Path:
    return pp.state_dir(repo or REPO) / STATE_DIR


def profile_path() -> Path:
    return state_dir() / PROFILE_FILE


def load_profile(path: Path | None = None, *,
                 tombstones: memforget.TombstoneSet | None = None,
                 repo: Path | None = None,
                 state: Path | str | None = None) -> dict:
    """Last stored profile. Absent/corrupt -> {} (an honest empty board).

    A DERIVED item that a tombstone silences is FILTERED OUT here (D7):
    the suppression takes effect at the next read — no re-analysis, no
    tick, no restart — and the tallies are recomputed over the rows that
    are actually visible, so a forgotten item never lingers in a count.

    `tombstones` is the suppression set; without it the ledger of the
    active profile's state root decides."""
    try:
        doc = json.loads((path or profile_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    return filter_profile(doc, tombstones=tombstones, repo=repo, state=state)


def filter_profile(doc: dict, *,
                   tombstones: memforget.TombstoneSet | None = None,
                   repo: Path | None = None,
                   state: Path | str | None = None) -> dict:
    """Drop every silenced derived item from a stored profile document.

    The document is returned UNCHANGED (byte for byte, same object) while
    nothing is silenced — the normal state before anything is forgotten.
    Otherwise the silenced rows are dropped and the counts are recomputed
    over the visible rows, so the tallies always describe what the reader
    actually sees."""
    if not isinstance(doc, dict):
        return {}
    rows = doc.get("keywords")
    if not isinstance(rows, list) or not rows:
        return doc
    try:
        snapshot = (tombstones if tombstones is not None
                    else memforget.load_set(repo, state))
    except memforget.LedgerUnavailable:
        return {"keywords": [], "error": memforget.LEDGER_UNAVAILABLE,
                "suppression": "unavailable"}
    suppression = _Suppression(snapshot)
    if suppression.empty:
        return doc
    kept = [r for r in rows
            if not (isinstance(r, dict)
                    and suppression.suppresses(str(r.get("token") or "")))]
    if len(kept) == len(rows):
        return doc
    out = dict(doc)
    out["keywords"] = kept
    out["counts"] = _counts([r for r in kept if isinstance(r, dict)])
    return out


def save_profile(doc: dict, path: Path | None = None) -> None:
    """Atomic persist (tmp + os.replace); OSError propagates to the caller."""
    target = path or profile_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    os.replace(tmp, target)


def tick(now: float | None = None, *, block: dict | None = None,
         profile: dict | None = None, force: bool = False) -> dict:
    """The service leg / console button: analyze when due, persist, report.

    `profile` (passed) keeps the call hermetic — nothing is read for it and
    nothing is written back; passing None loads and persists (the ownership
    rule every engine in this repo follows)."""
    t = float(now if now is not None else time.time())
    own = profile is None
    stored = load_profile() if profile is None else profile
    raw = block if block is not None else settings_block()
    try:
        cfg = parse_config(raw)
    except ProfilingError as e:
        return {"skipped": "config", "error": str(e)[:160], "at": t}
    if not cfg["enabled"]:
        return {"skipped": "disabled", "at": t}
    last = float(stored.get("at") or 0.0) if isinstance(stored, dict) else 0.0
    if not force and last and t >= last and t - last < cfg["interval_s"]:
        return {"skipped": "interval", "at": t}
    report = analyze(t, block=raw)
    if report.get("ok"):
        if own:
            try:
                save_profile(report)
            except OSError as e:  # noqa: BLE001 — a full disk is not a crash
                report["error"] = (f"persist failed: {type(e).__name__}"
                                   )[:160]
    return report
