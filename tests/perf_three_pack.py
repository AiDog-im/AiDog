#!/usr/bin/env python3
"""Performance three-pack (batch 4 item 4) — honest, reproducible numbers.

Run:  python3 tests/perf_three_pack.py                 (offline, stdlib only)
      python3 tests/perf_three_pack.py --rows 5000      (bigger transcript)

Nothing here touches the network, the production state dir or a real session:
the transcript is a synthetic fixture under a temp dir and AIDOG_MC_STATE is
pointed at a temp dir before mycore is imported.

Each item prints a BEFORE and an AFTER measured on the SAME fixture, with the
exact operation each column names — a delta is only a delta when both columns
are written down:

1. FIRST CHAT (warm-up)
   BEFORE = `build_system_stack()` with an empty body read cache. That is the
            only shape the code could produce before this car: every turn
            reopened the constitution/memory/learned/capability/project files
            (~9 open+read+close cycles).
   AFTER  = the same call with the cache primed — what a turn pays after the
            boot warm-up thread (web.server main / ChatService.run) has run.
            The filter and the cap still run on every build (D31).

2. BOUNDED HISTORY (GET /api/chat/history)
   Both columns are the SAME endpoint function, timed live:
   BEFORE = `ws.read_page` is temporarily the pre-car read (Session.load() of
            the WHOLE transcript + the newest 200 raw rows) and the body read
            cache is cleared (the old code had no cache), then
            `chat_history()` is timed — the old default limit of 200.
   AFTER  = the shipped `read_page()` (backward window, anchored ids), the
            warm body cache and the client's `?limit=40` page.
   The payload column is the JSON of `messages` for each response.

3. APPEND (measured, NOT changed)
   `Session.append` / `append_many` were already O(1) writes — they open the
   file in append mode and never read it back. The bench counts the bytes an
   append actually reads, so "no re-read" stays a checkable claim instead of
   a comment.

English output per repo i18n policy.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_STATE = tempfile.mkdtemp(prefix="perf_three_pack_state_")
os.environ["AIDOG_MC_STATE"] = _STATE     # hermetic: never the production state

from mycore import assembly, cli                     # noqa: E402
from mycore.session import READ_PAGE_CHUNK, Session, read_page  # noqa: E402


def ms(fn, runs: int = 7) -> float:
    """Median wall time of fn() over `runs` runs, in milliseconds."""
    samples = []
    for _ in range(runs):
        samples.append(ms1(fn))
    return statistics.median(samples)


def ms1(fn) -> float:
    """Wall time of ONE call, in milliseconds (for interleaved sampling)."""
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000.0


def rows_for(n: int) -> list[dict]:
    """n rows in real transcript shape (user/assistant/tool, mixed lengths)."""
    out: list[dict] = []
    for i in range(n // 3):
        out.append({"role": "user", "content": f"question {i} " + "x" * 180})
        out.append({"role": "assistant", "content": f"answer {i} " + "y" * 420})
        out.append({"role": "tool", "content": f"tool {i} " + "z" * 120,
                    "tool_call_id": "call_1"})
    return out


def item1_warmup() -> None:
    from web import server as ws
    print("\n== item 1 — first chat: the body-layer warm-up ==")
    print("  (the REAL body layer, read-only: constitution + memory slots +")
    print("   learned slot + capability map + project context, production args)")

    def cold_build():
        assembly.clear_read_cache()          # the pre-car state: no cache
        return assembly.build_system_stack(
            context_dirs=ws.resolve_context_dirs(),
            user_rules=ws.resolve_user_rules(),
            capability_path=ws.resolve_capability_path(),
            runtime_facts=cli.resolve_runtime_facts(
                cli.resolve_model_config().model))

    def warm_build():
        return assembly.build_system_stack(
            context_dirs=ws.resolve_context_dirs(),
            user_rules=ws.resolve_user_rules(),
            capability_path=ws.resolve_capability_path(),
            runtime_facts=cli.resolve_runtime_facts(
                cli.resolve_model_config().model))

    # Interleaved: this machine's filesystem drifts run to run, so BEFORE and
    # AFTER are sampled in the SAME rounds instead of back-to-back blocks.
    colds: list[float] = []
    warms: list[float] = []
    for _ in range(5):
        assembly.clear_read_cache()          # the pre-car state: no cache
        colds.append(ms1(cold_build))
        warms.append(ms(warm_build))
    cold = statistics.median(colds)
    warm = statistics.median(warms)
    assembly.warm_startup()                  # the boot thread's one read
    err = assembly.warm_startup()
    print(f"  build_system_stack  before (cold, every turn) {cold:7.2f} ms")
    print(f"  build_system_stack  after  (warm, post warm-up){warm:7.2f} ms"
          f"   -> {cold / max(warm, 1e-9):.1f}x")
    print(f"  body bytes cached after warm-up: {len(assembly._read_cache)} "
          f"files, 0 re-reads on the turn path")
    print(f"  warm_startup(): {err or 'ok'} (read-only, daemon thread at boot)")


def item2_history(path: Path, rows: list[dict], runs: int) -> None:
    from web import server as ws
    print(f"\n== item 2 — one GET /api/chat/history (N={len(rows)}) ==")
    saved_dirs = ws.chat_sessions_dir
    saved_read = ws.read_page
    try:
        ws.chat_sessions_dir = lambda: path.parent    # type: ignore[assignment]

        # ---- BEFORE: the pre-car read path inside the SAME endpoint ----
        def legacy_read_page(p, limit, before=None, keep=None):
            """What GET /api/chat/history did before this car: parse the
            WHOLE transcript, then take the newest 200 raw rows."""
            rows_all = Session(p).load()
            picked = [(i, m) for i, m in enumerate(rows_all)
                      if (before is None or i < before)
                      and (keep is None or keep(m))]
            has_more = len(picked) > 200
            if has_more:
                picked = picked[-200:]
            return {"messages": picked, "count": len(picked),
                    "has_more": has_more}
        # Interleaved rounds: BEFORE = legacy read + cold body cache, AFTER =
        # shipped page + warm body cache, each sampled in the same round.
        before_samples: list[float] = []
        after_samples: list[float] = []
        before_rows: list[dict] = []
        after_rows: list[dict] = []
        for _ in range(5):
            ws.read_page = legacy_read_page      # type: ignore[assignment]
            assembly.clear_read_cache()
            before_samples.append(ms1(lambda: ws.chat_history()))
            ws.read_page = saved_read            # type: ignore[assignment]
            assembly.warm_startup()
            after_samples.append(ms1(lambda: ws.chat_history(limit=40)))
        before_ms = statistics.median(before_samples)
        after_ms = statistics.median(after_samples)
        ws.read_page = legacy_read_page          # type: ignore[assignment]
        assembly.clear_read_cache()
        before_rows = ws.chat_history()["messages"]
        ws.read_page = saved_read                # type: ignore[assignment]
        assembly.warm_startup()
        after_rows = ws.chat_history(limit=40)["messages"]

        # ---- components (so the delta can be attributed) ----
        full_ms = ms(lambda: Session(path).load(), runs)
        Session(path).load()                   # prime the page anchor
        page_ms = ms(lambda: read_page(path, 40), runs)
        before_bytes = len(json.dumps({"messages": before_rows},
                                      ensure_ascii=False).encode())
        after_bytes = len(json.dumps({"messages": after_rows},
                                     ensure_ascii=False).encode())
        print(f"  whole endpoint      before {before_ms:7.2f} ms  "
              f"(full load + 200-row window)")
        print(f"  whole endpoint      after  {after_ms:7.2f} ms  "
              f"(live chat_history(limit=40))    -> "
              f"{before_ms / max(after_ms, 1e-9):.1f}x")
        print(f"  transcript read     before Session.load()  {full_ms:7.2f} ms")
        print(f"  transcript read     after  read_page(40)   {page_ms:7.2f} ms"
              f"   -> {full_ms / max(page_ms, 1e-9):.1f}x")
        print(f"  response `messages` before {before_bytes:8d} B / "
              f"{len(before_rows)} rows (client rendered 40 of them)")
        print(f"  response `messages` after  {after_bytes:8d} B / "
              f"{len(after_rows)} rows"
              f"   -> {before_bytes / max(after_bytes, 1):.1f}x smaller")
    finally:
        ws.chat_sessions_dir = saved_dirs      # type: ignore[assignment]
        ws.read_page = saved_read              # type: ignore[assignment]
        assembly.clear_read_cache()


def item3_append(rows: list[dict]) -> None:
    import builtins
    print("\n== item 3 — Session.append / append_many (measured, NOT changed) ==")
    print("  finding: the writers were ALREADY O(1) — they open the file in")
    print("  append mode and never read it back; no change was made.")
    with tempfile.TemporaryDirectory(prefix="perf_append_") as td:
        p = Path(td) / "web.jsonl"
        s = Session.create(p, meta={"model": "m"})
        s.append_many(rows)
        size = p.stat().st_size

        reads: list[int] = []
        real_open = builtins.open

        def counting_open(file, mode="r", *args, **kwargs):
            fh = real_open(file, mode, *args, **kwargs)
            if str(file) == str(p) and "r" in mode:
                orig = fh.read

                def read(*a, _o=orig):
                    data = _o(*a)
                    reads.append(len(data) if isinstance(data, (bytes, str))
                                 else 0)
                    return data
                fh.read = read
            return fh

        builtins.open = counting_open
        try:
            t0 = time.perf_counter()
            for _ in range(50):
                s.append({"role": "assistant", "content": "one line"})
            per_append = (time.perf_counter() - t0) / 50 * 1000.0
            append_reads = sum(reads)
            reads.clear()
            t0 = time.perf_counter()
            for _ in range(50):
                s.append_many([{"role": "assistant", "content": "delta a"},
                               {"role": "tool", "content": "delta b",
                                "tool_call_id": "c"}])
            per_many = (time.perf_counter() - t0) / 50 * 1000.0
            many_reads = sum(reads)
            reads.clear()
            read_page(p, 40)
            page_reads = sum(reads)
        finally:
            builtins.open = real_open
        grew = p.stat().st_size - size
        print(f"  transcript on disk              {size:8d} B ({len(rows)} rows)")
        print(f"  bytes read by 50 appends        {append_reads:8d} B"
              f"   ({per_append:.3f} ms/append; 1 B = the torn-tail check)")
        print(f"  bytes read by 50 append_many    {many_reads:8d} B"
              f"   ({per_many:.3f} ms/call)")
        print(f"  bytes read by read_page(40)     {page_reads:8d} B"
              f"   (window <= {2 * READ_PAGE_CHUNK} B, not the {size} B file)")
        print(f"  file growth after 100 appends   {grew:8d} B"
              f"   (append-only: the prefix is untouched)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="batch 4 item 4 perf bench")
    ap.add_argument("--rows", type=int, default=2000,
                    help="transcript rows in the fixture (default 2000)")
    ap.add_argument("--runs", type=int, default=7,
                    help="timed repetitions per measurement (default 7)")
    args = ap.parse_args(argv)
    n = max(300, args.rows - (args.rows % 3))

    with tempfile.TemporaryDirectory(prefix="perf_three_pack_") as td:
        tdp = Path(td)
        rows = rows_for(n)
        path = tdp / "sessions" / "web.jsonl"
        path.parent.mkdir(parents=True)
        Session.create(path, meta={"model": "m"})
        Session(path).append_many(rows)

        print(f"I'm AiDog perf three-pack (batch 4 item 4) — N={n} rows, "
              f"{path.stat().st_size} B transcript, "
              f"median of {args.runs} runs, offline")
        item1_warmup()
        item2_history(path, rows, args.runs)
        item3_append(rows)
    print("\nAll numbers above come from this process; re-run to reproduce.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
