#!/usr/bin/env python3
"""aidog_memory — productized dual-layer memory manager (budget-aware snapshot sync).

Feature spec (docs/features/memory_and_routing.md):
  Layer 1 (curated, budget-limited): memories/MEMORY.md  <= 2200 chars
                                       memories/USER.md   <= 1375 chars
  Layer 2 (archive, unlimited):       memories/ARCHIVE/*  read via file tools
  Source of truth during dual-brain phase: pi project memory (one-way snapshot).

Memory home resolution (P5-4, docs/features/memory_and_routing.md) — the
DST side of sync/status follows a portable 4-step chain, first hit wins:
  1. env AIDOG_MEMORY_HOME
  2. MEMORY_HOME in deploy/memory_sources.local (gitignored machine config)
  3. $HOME/.aidog/memories — post-swap default; step hits only when the
     portable home already exists (materialized by sync/import)
  4. <AIDOG_PROFILE_DIR>/memories from deploy/upstream_shim.env — LEGACY
     transition fallback, honored ONLY while that shim file exists (P5-5
     deletes it; afterwards step 3 becomes the final fallback and `sync`
     creates the home on fresh machines).
This module is the single source of truth for that chain: aidog_persist.py
imports resolve_memory_home() instead of re-implementing it.

Usage:
  python3 aidog_memory.py sync     # refresh snapshot from source (idempotent)
  python3 aidog_memory.py status   # budget report + provenance (no content echo)
Exit codes: 0 ok · 1 source/target error · 2 budget exceeded.
English comments per repo i18n policy.

Memory suppression: every product-side copy this module writes (the two
curated slots and the ARCHIVE copy of the external source) is filtered
through the tombstone ledger (mycore/memforget.py) BEFORE it is written,
so a source the owner suppressed cannot come back through a sync. The
external source home keeps its original bytes — it is the owner's own
copy, and only the product-side copy is filtered.

Profile isolation: the curated slots are read through the ACTIVE PROFILE
(lib/profile_paths.py), the same seam the console and mycore/profiling.py
use, so a named profile syncs ITS OWN curated memory and never the
repository's. See curated_slots() for the one narrow fallback.
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import re as _re

REPO = Path(__file__).resolve().parent.parent  # self-deriving: no machine path


def _local_kv(name: str) -> str | None:
    """Read deploy/memory_sources.local (gitignored machine-local config)."""
    try:
        for line in (REPO / "deploy/memory_sources.local").read_text().splitlines():
            m = _re.match(r"^\s*([A-Z0-9_]+)=(.*?)\s*$", line)
            if m and m.group(1) == name and m.group(2) and not m.group(2).startswith("#"):
                return m.group(2)
    except OSError:
        return None
    return None


def _src_kv(name: str) -> str | None:
    """Resolve a memory source key: env AIDOG_<name> first, then the local file.

    Self-containment chain (product rule, cross-machine revival): environment
    override > deploy/memory_sources.local > unset (None). Set-but-empty env
    counts as unset.
    """
    v = os.environ.get("AIDOG_" + name)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return _local_kv(name)


SRC = Path(_src_kv("MEMORY_SRC") or "")
PERSONA = Path(_src_kv("MEMORY_PERSONA") or "")


def _legacy_profile_dir() -> Path | None:
    """LEGACY transition profile dir (deploy/upstream_shim.env → AIDOG_PROFILE_DIR).

    Honored ONLY while the shim file exists — it is deleted in the P5-5
    same-car swap; every caller must treat a None return as "no legacy home".
    No machine path is ever written here: the value is parsed from the shim.
    """
    try:
        for _line in (REPO / "deploy/upstream_shim.env").read_text().splitlines():
            _m = _re.match(r'^\s*export\s+AIDOG_PROFILE_DIR="([^"]+)"\s*$', _line)
            if _m:
                return Path(_m.group(1))
    except OSError:
        pass
    return None


def resolve_memory_home() -> tuple[Path, str]:
    """-> (memory home, source label) via the portable 4-step chain (first hit wins).

    1. env AIDOG_MEMORY_HOME  2. local MEMORY_HOME  3. $HOME/.aidog/memories
    (only when it already exists)  4. legacy shim profile memories (only while
    deploy/upstream_shim.env exists) → final fallback: the post-swap default,
    which `sync` creates (mkdir parents) on fresh machines.
    Pure function of env/config/filesystem: no prints (import-safe).
    """
    v = os.environ.get("AIDOG_MEMORY_HOME")
    if isinstance(v, str) and v.strip():
        return Path(v.strip()), "env AIDOG_MEMORY_HOME"
    local = _local_kv("MEMORY_HOME")
    if local:
        return Path(local), "local MEMORY_HOME"
    default = Path(os.path.expanduser("~/.aidog/memories"))
    if default.is_dir():
        return default, "default ~/.aidog/memories"
    legacy = _legacy_profile_dir()
    if legacy is not None:
        return legacy / "memories", "legacy shim profile"
    return default, "default ~/.aidog/memories"


DST, DST_SOURCE = resolve_memory_home()
BUDGETS = {"MEMORY.md": 2200, "USER.md": 1375}


# ------------------------------------------------------- profile seam -----
# The curated slots live under the ACTIVE PROFILE's body root (car F), so
# a named profile has its OWN memory. This module resolves them through the
# SAME seam the rest of the product uses (lib/profile_paths.py), instead of
# the fixed <repo>/overlay/memory path it used to hard-code — that fixed
# path made a named profile read the REPOSITORY's curated slots, which is
# an isolation break (the profile's memory is built from another profile's
# files).
#
# WHY A LOADER AND NOT A PLAIN IMPORT: this script is executed from a
# COPIED deploy/ tree by the portability test (tests/memory_persist_p4.py
# copies deploy/*.py into a throwaway tree with no lib/ at all), so a plain
# `from lib import profile_paths` would raise there. The loader reads the
# file by path and degrades to None; only the default profile keeps the
# historical fixed-path behaviour. Loading by path also avoids a half-imported `lib`
# package from ANOTHER tree leaking into this one.


def profile_paths_module():
    """The profile seam (lib/profile_paths.py), or None when it is absent.

    Lazy and fail-soft, like memforget_module(): the deploy scripts stay
    import-safe and cheap, and a deploy tree without a kernel is a
    supported shape. None means "the profile seam is not available here"."""
    path = REPO / "lib" / "profile_paths.py"
    if not path.is_file():
        return None
    try:
        import importlib.util  # noqa: PLC0415
        spec = importlib.util.spec_from_file_location("aidog_profile_paths",
                                                      path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 — no seam, no crash
        return None


def curated_slots() -> tuple[Path, Path, str]:
    """(MEMORY slot, USER slot, source label) — the ACTIVE PROFILE's slots.

    The slots resolve through the ACTIVE PROFILE (the profile seam), so a
    named profile syncs ITS OWN curated memory and never the repository's.

    FALLBACK (and only this): when the profile seam cannot be imported —
    a copied deploy/ tree with no lib/ at all — the script keeps its
    historical behaviour and reads the fixed repository path
    <repo>/overlay/memory/*. The fallback is deliberately narrow: it
    applies ONLY to the default profile when the seam is unavailable.
    Any nonblank AIDOG_PROFILE requires a working seam, and invalid names
    are then refused by the seam itself."""
    pp = profile_paths_module()
    if pp is None:
        if os.environ.get("AIDOG_PROFILE", "").strip():
            raise ValueError("a named profile requires the profile resolver")
        return (REPO / "overlay" / "memory" / "MEMORY_curated.md",
                REPO / "overlay" / "memory" / "USER_curated.md",
                "fixed repository path (the profile seam is not importable "
                "here)")
    mem, usr = pp.memory_slots(REPO)
    return mem, usr, "the active profile's curated slots"


# ------------------------------------------------------- memory suppression --
# The tombstone ledger (mycore/memforget.py) is the product's record of the
# sources the owner suppressed. The portability paths write MEMORY bytes into
# the product home, so they must honour the ledger: a suppressed source must
# not come back through a sync or an import.
#
# D11 (a suppression never crosses stores) is kept: every file is filtered
# with the channel of the STORE it belongs to, and the store of a file is its
# name. The canonical store files are EVOLUTION_learned.md (learned),
# MEMORY_curated.md (curated memory) and USER_curated.md (curated user); the
# portability aliases name the SAME stores under their short names
# (MEMORY.md / MEMORY_full.md are the MEMORY store, USER.md / USER_full.md are
# the USER store). Everything else is the "other curated file" channel.


def memforget_module():
    """The tombstone-ledger module (mycore.memforget), or None.

    Lazy on purpose: the deploy scripts stay import-safe and cheap, and a
    copied deploy/ tree may have no kernel at all. None means "no
    suppression available here" — never a crash, never a silent skip."""
    root = str(REPO)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from mycore import memforget  # noqa: PLC0415 — sibling kernel package
        return memforget
    except Exception:  # noqa: BLE001 — the kernel may be absent or broken
        pass
    try:
        import importlib.util  # noqa: PLC0415
        path = REPO / "mycore" / "memforget.py"
        spec = importlib.util.spec_from_file_location("aidog_memforget", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # the dataclass decorator needs it
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 — no ledger, no filtering, no crash
        return None


def store_channel(path, module=None):
    """The admission channel of the store a memory file belongs to."""
    mf = module if module is not None else memforget_module()
    if mf is None:
        return None
    table = {
        "EVOLUTION_learned.md": mf.CHANNEL_LEARNED,
        "MEMORY_curated.md": mf.CHANNEL_CURATED_MEMORY,
        "USER_curated.md": mf.CHANNEL_CURATED_USER,
        "MEMORY.md": mf.CHANNEL_CURATED_MEMORY,
        "MEMORY_full.md": mf.CHANNEL_CURATED_MEMORY,
        "USER.md": mf.CHANNEL_CURATED_USER,
        "USER_full.md": mf.CHANNEL_CURATED_USER,
    }
    return table.get(Path(path).name, mf.CHANNEL_CURATED_OTHER)


def filter_store(path, text, tombstones):
    """(filtered text, entries suppressed) for one memory store file.

    With nothing suppressed the text comes back byte for byte — the normal
    state before anything is forgotten. A section is dropped when the ledger
    knows its source id, its content digest or its explicit marker."""
    mf = memforget_module()
    if mf is None or not tombstones or not text:
        return text, 0
    channel = store_channel(path, mf)
    filtered = mf.filter_text(text, channel, tombstones=tombstones)
    if filtered == text:
        return text, 0
    levels = (2, 2) if channel == mf.CHANNEL_LEARNED else (1, 6)
    _, before = mf.split_sections(text, *levels)
    _, after = mf.split_sections(filtered, *levels)
    return filtered, max(0, len(before) - len(after))


def copy_store(src, dst, tombstones):
    """Copy one memory store file into the product, filtered by the ledger.

    Nothing suppressed -> a plain byte-for-byte copy (metadata included).
    Something suppressed -> the FILTERED text is written; the source file is
    never edited (D7: derived stores are filtered, never edited).

    -> entries suppressed. An unreadable source raises, exactly like the
    plain copy it replaces."""
    source, target = Path(src), Path(dst)
    raw = source.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        shutil.copy2(source, target)  # not utf-8: copy verbatim, never guess
        return 0
    filtered, dropped = filter_store(source, text, tombstones)
    if not dropped:
        shutil.copy2(source, target)
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(filtered, encoding="utf-8")
    try:
        shutil.copystat(source, target)
    except OSError:
        pass
    return dropped


def load_suppression():
    """(tombstone set, note) for the active profile.

    An empty set means nothing was forgotten. None means the ledger is
    unavailable and the caller must withhold product memory writes."""
    mf = memforget_module()
    if mf is None:
        return None, ("suppression unavailable: the tombstone ledger could "
                      "not be loaded here — memory writes withheld")
    try:
        return mf.load_set(), ""
    except Exception:  # noqa: BLE001 — the caller reports and refuses the copy
        return None, ("suppression unavailable: the tombstone ledger could "
                      "not be read here — memory writes withheld")


def _missing_slot(path) -> int:
    """A curated slot that this profile does not have: report it, skip it.

    A named profile whose body was never seeded has no curated memory; the
    archive copy of the pi-side source still lands, and the honest line
    says which slot was not refreshed. Nothing is invented and nothing
    crashes."""
    print(f"curated slot not present for this profile: {Path(path).name} "
          "— the slot was not refreshed")
    return 0


def sync() -> int:
    if not str(SRC) or not (SRC / "MEMORY.md").is_file():
        print("memory source not configured — create deploy/memory_sources.local "
              "(see memory_sources.example)")
        return 1
    # The curated slots come from the ACTIVE PROFILE (the profile seam), so
    # a named profile syncs ITS OWN memory and never the repository's. An
    # invalid profile name is refused (fail-closed): it must never fall
    # through to another profile's memory.
    try:
        mem_slot, user_slot, slot_source = curated_slots()
    except Exception as e:  # noqa: BLE001 — type name only
        print(f"the active profile is not usable — refusing to sync "
              f"(profile error: {type(e).__name__})")
        return 1
    tombstones, note = load_suppression()
    if tombstones is None:
        print(note)
        return 1
    # write-path safety: a fresh machine has no home yet — create it (parents)
    # instead of failing; this is the ONLY place the product materializes DST.
    try:
        DST.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"cannot create memory home {DST}: {type(e).__name__}")
        return 1
    (DST / "ARCHIVE" / "daily").mkdir(parents=True, exist_ok=True)
    # Memory suppression: every PRODUCT-side copy is filtered through the
    # tombstone ledger before it is written, so a suppressed source cannot
    # come back through a sync. The external source home keeps its original
    # bytes — it is the owner's own copy, and it is never edited here.
    dropped = 0
    dropped += copy_store(SRC / "MEMORY.md", DST / "ARCHIVE" / "MEMORY_full.md",
                          tombstones)
    if (SRC / "daily").is_dir():
        for f in sorted((SRC / "daily").glob("*.md")):
            dropped += copy_store(f, DST / "ARCHIVE" / "daily" / f.name,
                                  tombstones)
    if (PERSONA / "profile.md").is_file():
        # persona is not a memory store: the ledger has no channel for it
        shutil.copy2(PERSONA / "profile.md", DST / "ARCHIVE" / "USER_full.md")
    for slot, target in ((mem_slot, DST / "MEMORY.md"),
                         (user_slot, DST / "USER.md")):
        if not Path(slot).is_file():
            _missing_slot(slot)
            continue
        dropped += copy_store(slot, target, tombstones)
    (DST / "SNAPSHOT_INFO.md").write_text(
        "# Snapshot provenance\n"
        f"- Source: pi-side project memory (authoritative, one-way snapshot)\n"
        f"- Top slots curated from {slot_source}; full text in ARCHIVE/\n"
        f"- Refresh: python3 {REPO}/deploy/aidog_memory.py sync\n"
        f"- Synced (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
    )
    print(f"curated slots: {slot_source}")
    print(f"suppression: {dropped} entr(ies) suppressed by the tombstone "
          "ledger (nothing deleted)")
    if note:
        print(f"note: {note}")
    print("sync ok")
    return status()


def status() -> int:
    rc = 0
    print(f"home: {DST} (source={DST_SOURCE})")
    for name, limit in BUDGETS.items():
        p = DST / name
        if not p.is_file():
            print(f"FAIL {name}: missing")
            rc = 1
            continue
        n = len(p.read_text())
        level = "ok" if n <= limit else "OVER"
        if n > limit:
            rc = 2
        print(f"{level:4} {name}: {n}/{limit} chars")
    arch = DST / "ARCHIVE" / "MEMORY_full.md"
    print(f"{'ok' if arch.is_file() else 'FAIL'}  ARCHIVE/MEMORY_full.md: "
          f"{arch.stat().st_size // 1024}KB" if arch.is_file() else "FAIL  ARCHIVE missing")
    daily = DST / "ARCHIVE" / "daily"
    n_daily = len(list(daily.glob('*.md'))) if daily.is_dir() else 0
    print(f"ok   ARCHIVE/daily: {n_daily} files")
    info = DST / "SNAPSHOT_INFO.md"
    if info.is_file():
        for line in info.read_text().splitlines():
            if line.startswith("- Synced"):
                print(line)
    return rc


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "sync":
        return sync()
    if cmd == "status":
        return status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
