#!/usr/bin/env python3
"""Memory suppression across the portability paths — regression wall.

Offline (no network), stdlib only; every write is confined to a temp dir
under /tmp. Exit 0 = all assertions passed.

The tombstone ledger (mycore/memforget.py) filters admission, the read
paths and the console restores. The PORTABILITY paths are the remaining
re-introduction hole: `aidog import` writes the bundle's memory section
into the product memory home AND into the external source home, and
`aidog_memory.py sync` copies the curated slots and the ARCHIVE copy of the
external source into the product home. A suppressed source must not come
back through either path.

Contracts locked here:
  - `aidog import` filters the PRODUCT-side copy of the memory section
    through the ledger (local ∪ the set that travelled with the bundle);
    the EXTERNAL source home keeps the original bytes and the import says
    so plainly — no silent data loss, no silent re-introduction
  - the tombstone set travels with an exported bundle (manifest key
    "tombstones") and is merged into the local ledger on import (D6);
    the traveling rows carry ids, channels, scopes, digests and timestamps
    only — no content, no path, no credential
  - `aidog_memory.py sync` filters every product-side copy it writes (the
    curated slots and the ARCHIVE copy of the external source); the source
    itself is never edited (D7)
  - a suppression failure NEVER fails an import or a sync, and it is never
    silent: the output says what happened
  - the CLI shape, the exit codes and the existing messages are unchanged
  - nothing is deleted: a suppression removes a source from ACTIVE memory
    only, and the ledger never stores the source text

Every run uses a temporary profile, a temporary state root (the ledger
lives there) and a temporary HOME, so no production state is read or
written (D12: a restore must never touch the installation's own slots).
English output per repo i18n policy.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path("/tmp")  # hard boundary: no writes outside /tmp

SUPPRESSED = "the text the owner suppressed"
KEPT = "the text the owner kept"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail and not ok else ""))


# ------------------------------------------------------------------ helpers --

def load_memforget():
    """mycore/memforget.py loaded straight from its file.

    The package __init__ pulls the whole kernel in; this test wants the
    ledger module only. The repo root goes on sys.path so the module's own
    `from lib import profile_paths` resolves."""
    root = str(REPO)
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location(
        "aidog_memforget", REPO / "mycore" / "memforget.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # the dataclass decorator needs the entry
    spec.loader.exec_module(module)
    return module


def fake_repo(root: Path, with_kernel: bool = True) -> Path:
    """A throwaway repo tree under /tmp -> the path of its aidog CLI.

    The deploy scripts are copied verbatim; the suppression module and its
    profile-path seam are copied too, so the copied tree behaves like the
    real one (the ledger itself lives in the temporary state root either
    way). `with_kernel=False` builds the degraded shape: a deploy tree with
    no kernel at all — the product must degrade honestly, never crash."""
    (root / "deploy").mkdir(parents=True, exist_ok=True)
    for name in ("aidog_persist.py", "aidog_memory.py", "check_publish.py"):
        shutil.copy2(REPO / "deploy" / name, root / "deploy" / name)
    if with_kernel:
        (root / "mycore").mkdir(exist_ok=True)
        (root / "mycore" / "__init__.py").write_text("", encoding="utf-8")
        shutil.copy2(REPO / "mycore" / "memforget.py",
                     root / "mycore" / "memforget.py")
        (root / "lib").mkdir(exist_ok=True)
        shutil.copy2(REPO / "lib" / "profile_paths.py",
                     root / "lib" / "profile_paths.py")
    return root / "deploy" / "aidog_persist.py"


def write_source(root: Path) -> Path:
    """A pi-side memory source with one suppressed section and one kept."""
    src = root / "src"
    (src / "daily").mkdir(parents=True)
    (src / "MEMORY.md").write_text(
        "# MEMORY (pi-side authority)\n\n"
        f"## Secret note\n{SUPPRESSED}\n\n"
        f"## Kept note\n{KEPT}\n", encoding="utf-8")
    (src / "USER.md").write_text(
        "# USER\n\n## Kept user\nthe user text the owner kept\n", encoding="utf-8")
    (src / "daily" / "2026-11-22.md").write_text(
        "day log line\n", encoding="utf-8")
    return src


def write_overlay(root: Path) -> None:
    """The repo's curated slots (the product's own memory stores)."""
    ov = root / "overlay" / "memory"
    ov.mkdir(parents=True)
    (ov / "MEMORY_curated.md").write_text(
        "# MEMORY (curated)\n\n"
        f"## Secret note\n{SUPPRESSED}\n\n"
        "## Kept note\nthe curated text the owner kept\n", encoding="utf-8")
    (ov / "USER_curated.md").write_text(
        "# USER\n\n## Kept user\nthe curated user text\n", encoding="utf-8")


def env_for(state: Path, home: Path, extra: dict | None = None) -> dict:
    """A subprocess environment that can touch nothing outside /tmp."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("AIDOG_MEMORY_SRC", "AIDOG_MEMORY_PERSONA",
                        "AIDOG_MEMORY_HOME", "AIDOG_MC_STATE",
                        "AIDOG_PROFILE", "AIDOG_PROFILES_DIR")}
    env.update({
        "HOME": str(home),
        "AIDOG_MC_STATE": str(state),
        "AIDOG_PROFILE": "supptest",
        "AIDOG_PROFILES_DIR": str(home / "profiles"),
    })
    if extra:
        env.update(extra)
    return env


def run(script: Path, args: list[str], env: dict) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(script), *args],
                       capture_output=True, text=True, timeout=90, env=env)
    return p.returncode, p.stdout + p.stderr


def manifest_of(tarball: Path) -> dict:
    with tarfile.open(tarball) as tar:
        return json.loads(tar.extractfile("manifest.json").read())  # type: ignore[union-attr]


# -------------------------------------------------------------------- tests --

def test_export_carries_the_set(t: Path) -> tuple[Path, Path, Path]:
    """The exported bundle carries the tombstone set (D6).

    -> (script, tarball, export state root)"""
    mf = load_memforget()
    state = t / "state_export"
    state.mkdir(parents=True, exist_ok=True)
    result = mf.forget(mf.CHANNEL_CURATED_MEMORY, SUPPRESSED, state=state)
    check("the forget is recorded in the temporary ledger", result.get("ok") is True,
          str(result))
    rows = mf.load_rows(state=state)
    check("the ledger holds exactly one row", len(rows) == 1, str(rows))
    check("the ledger row carries no content", SUPPRESSED not in json.dumps(rows),
          json.dumps(rows))
    check("the ledger row carries only id/channel/scope/digest/ts",
          set(rows[0]) == {"id", "channel", "scope", "digest", "ts"},
          str(sorted(rows[0])))

    script = fake_repo(t / "repo_export")
    src = write_source(t)
    out = t / "out"
    env = env_for(state, t / "home_export")
    rc, so = run(script, ["export", "--src", str(src), "--out", str(out)], env)
    check("export exit 0", rc == 0, so[:400])
    tarballs = sorted(out.glob("*.tar.gz")) if out.is_dir() else []
    check("export wrote exactly one tarball", len(tarballs) == 1, str(tarballs))
    if not tarballs:
        return script, Path(""), state
    manifest = manifest_of(tarballs[0])
    check("the bundle carries the tombstone set", "tombstones" in manifest,
          str(sorted(manifest)))
    check("the traveling set carries no content",
          SUPPRESSED not in json.dumps(manifest), json.dumps(manifest)[:300])
    check("export says the set travels with the bundle",
          "tombstones" in so and "travel" in so, so[-300:])
    check("the existing export messages are unchanged",
          "export ok:" in so and "  files:" in so and "  sidecar:" in so, so[-300:])
    return script, tarballs[0], state


def test_import_filters_the_product_copy(t: Path, script: Path, tarball: Path) -> None:
    """The import filters the product-side copy; the source home keeps its
    original bytes; the traveling set is merged into the local ledger."""
    mf = load_memforget()
    state = t / "state_import"          # another machine: its own ledger
    state.mkdir(parents=True, exist_ok=True)
    dest = t / "dest_profile"
    source_home = t / "source_home"
    persona_home = t / "persona_home"
    env = env_for(state, t / "home_import")
    rc, so = run(script, ["import", str(tarball), "--dest-profile", str(dest),
                           "--source-home", str(source_home),
                           "--persona-home", str(persona_home)], env)
    check("import exit 0", rc == 0, so[:600])
    check("the import keeps its existing messages",
          "import ok:" in so and "restored:" in so and "source home:" in so,
          so[-500:])

    product = dest / "memories" / "MEMORY.md"
    check("the product-side copy exists", product.is_file(), str(product))
    body = product.read_text(encoding="utf-8") if product.is_file() else ""
    check("the suppressed source does NOT come back", SUPPRESSED not in body,
          body[:200])
    check("the kept source is restored", KEPT in body, body[:200])
    check("the product-side copy keeps its other sections",
          "Kept note" in body, body[:200])

    source_copy = source_home / "MEMORY.md"
    check("the external source home keeps the ORIGINAL bytes",
          source_copy.is_file() and SUPPRESSED in source_copy.read_text(encoding="utf-8"),
          "the source home was filtered — it must not be")
    check("the import says the source home keeps the original bytes",
          "ORIGINAL bytes" in so and "only the" in so, so[-500:])
    check("the import reports how many entries were suppressed",
          "suppression:" in so and "1 entr(ies) suppressed" in so, so[-500:])
    check("the import reports that nothing was deleted",
          "nothing deleted" in so, so[-500:])

    merged = mf.load_rows(state=state)
    check("the traveling set is merged into the local ledger", len(merged) == 1,
          str(merged))
    check("the merged row carries no content",
          merged and SUPPRESSED not in json.dumps(merged), json.dumps(merged)[:200])


def test_sync_filters_every_product_copy(t: Path) -> None:
    """`aidog_memory.py sync` filters every product-side copy it writes."""
    mf = load_memforget()
    state = t / "state_sync"
    state.mkdir(parents=True, exist_ok=True)
    mf.forget(mf.CHANNEL_CURATED_MEMORY, SUPPRESSED, state=state)

    repo = t / "repo_sync"
    fake_repo(repo)
    write_overlay(repo)
    src = t / "sync_src"
    src.mkdir()
    (src / "MEMORY.md").write_text(
        "# MEMORY (pi-side authority)\n\n"
        f"## Secret note\n{SUPPRESSED}\n\n"
        f"## Kept note\n{KEPT}\n", encoding="utf-8")
    dst = t / "sync_dst"
    env = env_for(state, t / "home_sync",
                  {"AIDOG_MEMORY_SRC": str(src), "AIDOG_MEMORY_HOME": str(dst)})
    rc, so = run(repo / "deploy" / "aidog_memory.py", ["sync"], env)
    check("sync exit 0", rc == 0, so[:500])
    check("sync keeps its existing message", "sync ok" in so, so[-300:])
    check("sync reports the suppressed count",
          "suppression:" in so and "entr(ies) suppressed" in so, so[-500:])
    check("sync reports that nothing was deleted", "nothing deleted" in so, so[-500:])

    archive = dst / "ARCHIVE" / "MEMORY_full.md"
    check("the ARCHIVE copy of the external source is filtered",
          archive.is_file() and SUPPRESSED not in archive.read_text(encoding="utf-8"),
          archive.read_text(encoding="utf-8")[:200] if archive.is_file() else "missing")
    check("the ARCHIVE copy keeps its other sections",
          archive.is_file() and KEPT in archive.read_text(encoding="utf-8"))
    curated = dst / "MEMORY.md"
    check("the curated slot copy is filtered",
          curated.is_file() and SUPPRESSED not in curated.read_text(encoding="utf-8"),
          curated.read_text(encoding="utf-8")[:200] if curated.is_file() else "missing")
    check("the external source keeps its original bytes",
          SUPPRESSED in (src / "MEMORY.md").read_text(encoding="utf-8"),
          "the source file must never be edited")
    check("sync writes no tombstone file into the memory home",
          not (dst / "memforget").exists())


def test_the_local_ledger_alone_is_enough(t: Path) -> None:
    """A bundle WITHOUT a traveling set is still filtered by the local ledger.

    That is the common case for bundles exported before the set travelled:
    the local ledger alone must keep a suppressed source out."""
    mf = load_memforget()
    state = t / "state_local"
    state.mkdir(parents=True, exist_ok=True)
    mf.forget(mf.CHANNEL_CURATED_MEMORY, SUPPRESSED, state=state)
    script = fake_repo(t / "repo_local")
    src = write_source(t / "local")
    out = t / "out_local"
    # export with an EMPTY ledger -> the bundle carries no set
    empty_state = t / "state_empty"
    empty_state.mkdir(parents=True, exist_ok=True)
    rc, so = run(script, ["export", "--src", str(src), "--out", str(out)],
                 env_for(empty_state, t / "home_local"))
    check("export exit 0 (no set to travel)", rc == 0, so[:300])
    tarballs = sorted(out.glob("*.tar.gz"))
    if not tarballs:
        check("export wrote a tarball", False, str(tarballs))
        return
    manifest = manifest_of(tarballs[0])
    check("a bundle without a set carries no tombstone key",
          "tombstones" not in manifest, str(sorted(manifest)))

    dest = t / "dest_local"
    rc, so = run(script, ["import", str(tarballs[0]), "--dest-profile", str(dest),
                           "--source-home", str(t / "source_local"),
                           "--persona-home", str(t / "persona_local")],
                 env_for(state, t / "home_local2"))
    check("import exit 0", rc == 0, so[:400])
    body = (dest / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    check("the local ledger alone keeps the source out", SUPPRESSED not in body,
          body[:200])
    check("the kept source is restored", KEPT in body, body[:200])
    check("the import still reports the suppressed count",
          "1 entr(ies) suppressed" in so, so[-400:])


def test_the_same_machine_import_is_idempotent(t: Path, tarball: Path) -> None:
    """A second import on the machine that already holds the set stays clean."""
    mf = load_memforget()
    state = t / "state_export"          # the state that holds the row
    script = fake_repo(t / "repo_export")
    dest = t / "dest_same"
    rc, so = run(script, ["import", str(tarball), "--dest-profile", str(dest),
                           "--source-home", str(t / "source_same"),
                           "--persona-home", str(t / "persona_same")],
                 env_for(state, t / "home_same"))
    check("import exit 0 on the machine that holds the set", rc == 0, so[:400])
    body = (dest / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    check("the source stays out", SUPPRESSED not in body, body[:200])
    check("the traveling set is not duplicated in the ledger",
          len(mf.load_rows(state=state)) == 1,
          str(mf.load_rows(state=state)))
    check("the import does not report a merge error",
          "could not be merged" not in so, so[-400:])


def test_a_broken_ledger_never_fails_the_import(t: Path, tarball: Path) -> None:
    """A ledger that cannot be written must not fail the import."""
    """A ledger that cannot be written must not fail the import."""
    script = fake_repo(t / "repo_broken")
    broken = t / "broken_state"
    broken.write_text("this is a file, not a state root\n", encoding="utf-8")
    dest = t / "dest_broken"
    source_home = t / "source_broken"
    env = env_for(broken, t / "home_broken")
    rc, so = run(script, ["import", str(tarball), "--dest-profile", str(dest),
                           "--source-home", str(source_home),
                           "--persona-home", str(t / "persona_broken")], env)
    check("import exit 0 even when the ledger cannot be written", rc == 0,
          so[:600])
    check("the import restored the memory files anyway",
          (dest / "memories" / "MEMORY.md").is_file())
    check("the import says the merge could not be persisted",
          "could not be merged" in so, so[-500:])
    check("the import keeps its exit-0 contract",
          "import ok:" in so, so[-500:])


def test_a_missing_ledger_module_degrades_honestly(t: Path, tarball: Path) -> None:
    """A deploy tree without the suppression module degrades, never crashes."""
    script = fake_repo(t / "repo_nokernel", with_kernel=False)
    state = t / "state_nokernel"
    state.mkdir(parents=True, exist_ok=True)
    dest = t / "dest_nokernel"
    env = env_for(state, t / "home_nokernel")
    rc, so = run(script, ["import", str(tarball), "--dest-profile", str(dest),
                           "--source-home", str(t / "source_nokernel"),
                           "--persona-home", str(t / "persona_nokernel")], env)
    check("import exit 0 without the suppression module", rc == 0, so[:600])
    check("the import says the suppression was unavailable",
          "suppression unavailable" in so, so[-500:])
    check("the import still restored the files",
          (dest / "memories" / "MEMORY.md").is_file())
    check("nothing was silently filtered",
          "nothing was filtered" in so, so[-500:])


def test_the_product_tree_is_untouched(before: dict) -> None:
    """No production state was written by this suite (D12).

    The machine's own machine-config file and the active profile's ledger
    are snapshotted before the suite and compared afterwards: a portability
    test must never touch the installation's own memory."""
    after = _production_snapshot()
    for key, value in before.items():
        check(f"production state unchanged: {key}", after.get(key) == value,
              f"{value!r} -> {after.get(key)!r}")
    local = REPO / "deploy" / "memory_sources.local"
    check("the suite wrote nothing into the repo tree",
          local.is_file() == (before.get("deploy/memory_sources.local") is not None),
          "the machine config appeared or vanished")


def _production_snapshot() -> dict:
    """(path -> (mtime_ns, size, sha256)) of the machine files this suite must
    not touch: the repo's machine config and the active profile's ledger."""
    import hashlib

    out: dict = {}
    local = REPO / "deploy" / "memory_sources.local"
    try:
        raw = local.read_bytes()
        info = local.stat()
        out["deploy/memory_sources.local"] = (
            info.st_mtime_ns, info.st_size, hashlib.sha256(raw).hexdigest())
    except OSError:
        out["deploy/memory_sources.local"] = None
    try:
        root = str(REPO)
        if root not in sys.path:
            sys.path.insert(0, root)
        from lib import profile_paths as pp
        ledger = pp.state_dir(REPO) / "memforget" / "tombstones.jsonl"
        raw = ledger.read_bytes()
        info = ledger.stat()
        out["profile ledger"] = (
            info.st_mtime_ns, info.st_size, hashlib.sha256(raw).hexdigest())
    except Exception:  # noqa: BLE001 — a missing ledger is a valid state
        out["profile ledger"] = None
    return out


def main() -> int:
    before = _production_snapshot()
    with tempfile.TemporaryDirectory(prefix="aidog_suppress_", dir=str(TMP)) as td:
        t = Path(td)
        script, tarball, _state = test_export_carries_the_set(t)
        if not tarball.is_file():
            print("export failed — aborting the remaining checks")
            return 1
        test_import_filters_the_product_copy(t, script, tarball)
        test_the_local_ledger_alone_is_enough(t)
        test_the_same_machine_import_is_idempotent(t, tarball)
        test_sync_filters_every_product_copy(t)
        test_a_broken_ledger_never_fails_the_import(t, tarball)
        test_a_missing_ledger_module_degrades_honestly(t, tarball)
    test_the_product_tree_is_untouched(before)

    print(f"\n{len(PASS)} pass · {len(FAIL)} fail")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
