#!/usr/bin/env python3
"""public-leak-guard — CI backstop scanning this repository's own tree (layer 3).

Why it exists
-------------
Layers 1 and 2 live in the DEVELOPMENT repository (origin moved to the
owner-only vault; a pre-push hook refuses every non-vault destination). They
stop an accidental `git push` from here. This workflow is what remains after
those layers: the public repository can still be edited through the web UI or
by a contributor, so every push re-scans the pushed tree and, on a hit, the
workflow reports that revision for maintainer review.

Design rules
------------
* This script targets the DESENSITIZED public tree. Run against the private
  development tree it reports the source vocabulary by design — that is a
  useful smoke test of the signatures, not a defect.
* Signatures are assembled from FRAGMENTS at runtime. This file is part of the
  tree it scans, so a complete sensitive token on one line would make the guard
  fail on itself — and would hand readers the exact vocabulary to hide.
* Report only an opaque file identifier, pattern class and count.
* Distinguish a confirmed finding from an unavailable Git object.

Exit codes: 0 clean · 3 leak found · 2 tool error.
English output per repo i18n policy.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys

# --- signatures, fragment-assembled -----------------------------------------
OWNER_MARK = "\u603b"             # second half of the owner nickname
ASSOC_NAME = "msg" + "chain"      # associated chain project
LEGACY_NAME = "jav" + "is"        # previous product
COMPETITOR_NAME = "Open" + "Claw"
PERSONA_ID = "persona" + "-c[0-9a-z]+"
CHAIN_MARK = "MS" + "G"           # chain project env prefix
# Assembled on purpose: the transform table rewrites this exact word, and a
# plain literal here would come back as a different pattern name in the report
# (observed: the label itself was renamed by the rule it is meant to detect).
RIVAL_LABEL = "comp" + "etitor"
UPSTREAM_TOKEN = "herm" + "es"    # upstream brand
OWNER_TG_ID = "76790334" + "30"   # owner's TG user id (never a full literal)

MACHINE_PATH = r"/mnt/[a-zA-Z]\b|/root/\.npm/_npx|[A-Za-z]:\\codex-proxy"
FILLED_CREDENTIAL = (
    r"(?m)^(?:[A-Za-z0-9]+_)*(?:TOKEN|PAT|SECRET|PASSWORD|API_KEY)"
    r"(?:_[A-Za-z0-9]+)*=(?!\s*$)\S+")

SIGNATURES: list[tuple[str, re.Pattern]] = [
    ("owner-term", re.compile(r"[Cc]\s*" + OWNER_MARK)),
    ("assoc-project", re.compile(ASSOC_NAME, re.I)),
    ("legacy-product", re.compile(LEGACY_NAME, re.I)),
    (RIVAL_LABEL, re.compile(COMPETITOR_NAME, re.I)),
    ("owner-persona", re.compile(PERSONA_ID, re.I)),
    # LEGACY_ slots; lowercase msg_* are protocol field names (msg_type, msg_seq)
    # and MSG_KEYS in the session module is a message false positive.
    ("legacy-env-slot", re.compile(rf"{CHAIN_MARK}_(?!KEYS\b)[A-Z*]+")),
    ("legacy-env-brand", re.compile(rf"(?<![A-Za-z_]){CHAIN_MARK}(?![A-Za-z_])")),
    ("upstream-brand", re.compile(re.escape(UPSTREAM_TOKEN), re.I)),
    ("personal-tg", re.compile(r"\b" + OWNER_TG_ID + r"\b|@my_[a-z0-9_]+_bot")),
    ("machine-path", re.compile(MACHINE_PATH)),
    ("filled-credential", re.compile(FILLED_CREDENTIAL)),
]

# Files that necessarily embed detection signatures (standard linter
# self-exclusion). Keep this list short and justified.
SELF = (".github/scripts/leak_scan.py",)


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout


def main() -> int:
    as_json = "--json" in sys.argv
    hits: list[dict] = []
    scanned = 0
    rows = git_bytes("ls-tree", "-r", "-z", "HEAD").split(b"\0")
    for row in filter(None, rows):
        metadata, _, path = row.partition(b"\t")
        _, kind, oid = metadata.split()
        if kind != b"blob":
            raise ValueError("unsupported tree entry")
        name = path.decode("utf-8", "replace")
        values = [("path", name)]
        if name not in SELF:
            raw = git_bytes("cat-file", "blob", oid.decode("ascii"))
            try:
                values.append(("content", raw.decode("utf-8")))
            except UnicodeDecodeError:
                pass
        scanned += 1
        file_id = hashlib.sha256(path).hexdigest()[:16]
        for location, value in values:
            for pattern, regex in SIGNATURES:
                count = sum(1 for _ in regex.finditer(value))
                if count:
                    hits.append({"file_id": file_id, "location": location,
                                 "pattern": pattern, "count": count})

    if as_json:
        print(json.dumps({"scanned": scanned, "hits": hits, "clean": not hits}))
    else:
        print(f"public leak guard: {scanned} Git entries scanned")
        for hit in hits:
            print("  LEAK[{pattern}] {location} file={file_id} count={count}".format(**hit))
        print("verdict: CLEAN" if not hits else f"verdict: LEAK FOUND ({len(hits)})")
    return 3 if hits else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"tool error: {type(e).__name__}", file=sys.stderr)
        sys.exit(2)
