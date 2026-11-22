"""mycore.fleetgate — the decision key and the role catalog (Need-B P1a).

Two engine-side pieces of the Need-B design
(`docs/design/swarm_clones_design.md` 4.3), deliberately separated from
`mycore.fleet` (the ledger + receipt) and from every user-visible string:

  * THE DECISION KEY — a deterministic, zero-token complexity band computed
    from the briefs a parent wrote. It answers two questions the harness can
    answer without a model call: "does this warrant more than one clone?"
    and "how wide may the split be?" The band is a *band*, never a proof:
    it is reported to the owner, and only in `enforce` mode can it refuse a
    fan-out (the default `advise` mode never refuses — a wrong band must
    not damage the answer).
  * THE ROLE CATALOG — four capability profiles (scout / analyst /
    implementer / verifier). A role fixes the clone's tool policy, its
    iteration bound and its report contract. A role is NOT a persona: it
    says what the clone may touch and what it must hand back, nothing about
    tone or identity.

The fixed report schema is what lets the parent fold REPORTS instead of
transcripts (context protection) and lets the ledger record structured
fields. A clone that does not follow the schema is recorded honestly as
`free-form`, never as a failed task.

Stdlib only; English comments (repo i18n policy).
"""
from __future__ import annotations

import re
from pathlib import Path

# Bands and their fan-out ceilings. The ceiling is the widest split the
# band justifies; `enforce` refuses anything wider, `advise` only reports.
BAND_CEILINGS = {"S": 1, "M": 3, "L": 5}
BAND_ORDER = ("S", "M", "L")

# Cross-cutting verbs: a request that carries several of them is broad by
# construction ("compare X and Y, then migrate both, then audit the result").
# English + Chinese, both ASCII-escaped so the source stays CJK-free.
CROSS_CUTTING = (
    "compare", "contrast", "audit", "migrate", "refactor", "survey",
    "investigate", "benchmark", "cross-check", "and also", "then also",
    "\u5bf9\u6bd4", "\u5ba1\u8ba1", "\u8fc1\u79fb", "\u91cd\u6784",
    "\u8c03\u7814", "\u6d4b\u8bd5", "\u548c\u8fd8\u8981",
)

# A brief that names a file path is scoped to that file; several paths mean
# the clone is crossing scopes.
_PATH_RE = re.compile(r"(?:/|\b[A-Za-z]:\\)[A-Za-z0-9_./\\-]{2,}")
_ENUM_RE = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*])\s+\S")

REPORT_FIELDS = ("status", "summary", "findings", "handles", "blocker")
_STATUS_WORDS = ("ok", "done", "failed", "blocked", "partial")


def _tokens(text: str) -> list[str]:
    """Deterministic tokens for one brief (latin words + CJK bigrams). The
    profiler's tokenizer is reused so the two engines agree on what a word
    is — no model call, no segmentation dependency."""
    try:
        from .profiling import tokenize
        return tokenize(text)
    except Exception:  # noqa: BLE001 — the gate degrades to a plain split
        return [w.lower() for w in re.split(r"\W+", text or "") if w]


def band_for(briefs: list[str]) -> dict:
    """The complexity band for a set of briefs. Returns
    `{band, ceiling, score, reasons}` — `reasons` is a short, human-readable
    list of the features that produced the band (the owner sees it in the
    fleet board; the model sees the ceiling)."""
    briefs = [str(b or "") for b in briefs]
    n = len(briefs)
    chars = sum(len(b) for b in briefs)
    max_chars = max((len(b) for b in briefs), default=0)
    paths = {p for b in briefs for p in _PATH_RE.findall(b)}
    verbs = {v for v in CROSS_CUTTING if v in " ".join(briefs).lower()}
    enumerated = sum(len(_ENUM_RE.findall(b)) for b in briefs)
    topics = {t for b in briefs for t in _tokens(b)}
    score = 0
    reasons: list[str] = []
    if n >= 2:
        score += 2
        reasons.append(f"{n} parallel briefs")
    if chars >= 600:
        score += 1
        reasons.append(f"{chars} characters of briefs")
    if max_chars >= 400:
        score += 1
        reasons.append("one brief is long")
    if len(paths) >= 3:
        score += 1
        reasons.append(f"{len(paths)} distinct paths")
    if len(verbs) >= 2:
        score += 2
        reasons.append(f"{len(verbs)} cross-cutting verbs")
    if enumerated >= 4:
        score += 1
        reasons.append(f"{enumerated} enumerated items")
    if len(topics) >= 12:
        score += 1
        reasons.append(f"{len(topics)} distinct topics")
    if score >= 6:
        band = "L"
    elif score >= 3:
        band = "M"
    else:
        band = "S"
    return {"band": band, "ceiling": BAND_CEILINGS[band], "score": score,
            "reasons": reasons, "briefs": n}


def ceiling_for(count: int) -> int:
    """The widest split a band justifies (the ceiling of the band the briefs
    land in)."""
    band = band_for(["x"] * max(1, count))["band"]
    return BAND_CEILINGS[band]


# ----------------------------------------------------------- the role catalog
ROLE_CATALOG: dict[str, dict] = {
    "scout": {
        "tools": ("read_file", "grep", "glob", "http_fetch", "skill_route"),
        "max_iters": 8,
        "report": "read-only reconnaissance: what exists, where, and what "
                  "is missing",
        "system": ("You are a scouting sub-agent. Gather facts from the "
                   "files and pages you are given and report them plainly. "
                   "Do not change anything. End with the fixed report "
                   "block."),
    },
    "analyst": {
        "tools": ("read_file", "grep", "glob", "http_fetch", "skill_route"),
        "max_iters": 14,
        "report": "an analysis: the question, the evidence, the conclusion",
        "system": ("You are an analysis sub-agent. Reason from the evidence "
                   "you read, state what the evidence does and does not "
                   "support, and end with the fixed report block."),
    },
    "implementer": {
        "tools": ("read_file", "grep", "glob", "write_file", "edit_file",
                  "run_bash"),
        "max_iters": 20,
        "report": "what was changed, where, and how it was verified",
        "system": ("You are an implementation sub-agent. Make the change you "
                   "were asked for, inside the tools you were given, and end "
                   "with the fixed report block naming the files you "
                   "touched."),
    },
    "verifier": {
        "tools": ("read_file", "grep", "glob", "run_bash"),
        "max_iters": 10,
        "report": "a deterministic check: the command, its exit status, and "
                  "what it proves",
        "system": ("You are a verification sub-agent. Check the claim with a "
                   "command you can run or a file you can read. Report the "
                   "command and its exit status. Do not fix anything. End "
                   "with the fixed report block."),
    },
}

DEFAULT_ROLE = "scout"
# A role may never widen the tools the parent granted: the intersection is
# the truth, and the recursion guard still applies on top.
NEVER_GRANTED = ("delegate", "execute_code")


def role_for(name: str | None) -> tuple[str, dict]:
    """(role name, role record). An unknown role falls back to the default
    and says so in the record's `fallback` flag — never a silent upgrade."""
    key = str(name or "").strip().lower()
    if key in ROLE_CATALOG:
        return key, dict(ROLE_CATALOG[key])
    return DEFAULT_ROLE, dict(ROLE_CATALOG[DEFAULT_ROLE], fallback=True)


def role_tools(role: dict) -> tuple[str, ...]:
    """The tool names a role may use."""
    return tuple(t for t in role.get("tools", ()) if t not in NEVER_GRANTED)


def role_max_iters(role: dict, default: int) -> int:
    """The iteration bound a role imposes (never above the parent's own)."""
    try:
        bound = int(role.get("max_iters") or 0)
    except (TypeError, ValueError):
        bound = 0
    return max(1, min(bound, default)) if bound else default


# --------------------------------------------------------- the report schema
REPORT_TEMPLATE = (
    "REPORT status: <ok|done|failed|blocked|partial>\n"
    "REPORT summary: <one line>\n"
    "REPORT findings: <bulleted facts, or none>\n"
    "REPORT handles: <paths/ids/commands that prove the work, or none>\n"
    "REPORT blocker: <what stopped you, or none>")


def report_prompt(role: dict) -> str:
    """The fixed report block a clone is asked to end with."""
    return (f"{REPORT_TEMPLATE}\n"
            f"(What this role must deliver: {role.get('report', '')}.)")


def parse_report(text: str) -> dict:
    """Parse a clone's fixed report block. A clone that did not follow the
    schema is reported honestly as `free-form` with whatever fields it did
    provide — never as a failed task."""
    out = {"format": "free-form", "status": "", "summary": "",
           "findings": [], "handles": [], "blocker": ""}
    raw = str(text or "")
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("REPORT "):
            continue
        body = stripped[7:].strip()
        if ":" not in body:
            continue
        key, _, value = body.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key not in REPORT_FIELDS:
            continue
        if key in ("findings", "handles"):
            items = [v.strip(" -*\t") for v in value.splitlines()]
            items = [v for v in items if v and v.lower() != "none"]
            if items:
                out[key] = items
                out["format"] = "fixed"
        else:
            if value and value.lower() != "none":
                out[key] = value
                out["format"] = "fixed"
    if out["status"].lower() not in _STATUS_WORDS:
        out["status"] = ""
    return out


def resolve_mode(block: object) -> str:
    """The auto-split mode from a settings block: `off`, `advise` (default)
    or `enforce`. An unknown value degrades to `advise` (never to refuse)."""
    if not isinstance(block, dict):
        return "advise"
    mode = str(block.get("auto_split") or "advise").strip().lower()
    return mode if mode in ("off", "advise", "enforce") else "advise"


def resolve_signoff(block: object) -> str:
    """The sign-off mode from a settings block: `off`, `roles` (default) or
    `writes`.

      off    — no fleet ever waits for the owner;
      roles  — a WRITE-capable fleet that NAMED a role waits for the
               owner's approval (the new multi-role surface);
      writes — every write-capable fleet waits, including the plain
               delegate path.

    The default is `roles` on purpose: granting `write_file` to a clone has
    been the shipped delegate contract since 0.5.x, and silently changing it
    would break every existing workflow. The owner can widen the gate with
    one setting (`mycore.fleet.require_signoff = "writes"`)."""
    if not isinstance(block, dict):
        return "roles"
    mode = str(block.get("require_signoff") or "roles").strip().lower()
    return mode if mode in ("off", "roles", "writes") else "roles"


def settings_block(repo: Path | None = None) -> dict:
    """The `mycore.fleet` settings partition (empty when absent). The
    loader is the shared profile-aware one (`lib.profile_paths`), the same
    path seam every other settings reader uses."""
    import json
    try:
        from lib import profile_paths as pp
        root = Path(repo) if repo is not None else Path(__file__).resolve(
            ).parent.parent
        stored = json.loads(pp.settings_path(root).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — an unreadable document = feature off
        return {}
    if not isinstance(stored, dict):
        return {}
    block = stored.get("mycore")
    if not isinstance(block, dict):
        return {}
    fleet = block.get("fleet")
    return fleet if isinstance(fleet, dict) else {}
