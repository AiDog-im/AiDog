#!/usr/bin/env python3
"""Read-only UTC-day Cloudflare usage; no external notifications.

Exit 0: healthy, 1: at least 70% used, 2: partial/unavailable. R2 is
intentionally excluded. Empty successful datasets mean zero; failed or
missing datasets mean null. Daily free-tier allowances are not billing data.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import urllib.request

REPO = Path(__file__).resolve().parent.parent
API = "https://api.cloudflare.com/client/v4/graphql"
THRESHOLD = 0.70
ROW_LIMIT = 10000
LIMITS = {
    "workers_pages_requests": 100000,
    "kv_reads": 100000,
    "kv_writes": 1000,
    "kv_deletes": 1000,
    "kv_lists": 1000,
    "d1_rows_read": 5000000,
    "d1_rows_written": 100000,
}
DATASETS = {
    "kvOperationsAdaptiveGroups": "dimensions { actionType } sum { requests }",
    "pagesFunctionsInvocationsAdaptiveGroups": "sum { requests }",
    "workersInvocationsAdaptive": "sum { requests }",
    "d1AnalyticsAdaptiveGroups": "sum { rowsRead rowsWritten }",
}


def credentials(path: Path) -> tuple[str, str]:
    """Read encrypted/plain stores through the shared reader only."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from mycore import secrets_store

    values = {}
    try:
        text = secrets_store.secrets_text(path)
    except Exception:
        text = ""
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in {"AIDOG_CF_API_TOKEN", "AIDOG_CF_ACCOUNT_ID"}:
            values[key] = value.strip().strip("\"'")
    return tuple(os.environ.get(key) or values.get(key, "") for key in
                 ("AIDOG_CF_API_TOKEN", "AIDOG_CF_ACCOUNT_ID"))


def day_window(now: datetime) -> tuple[str, str]:
    if now.tzinfo is None:
        raise ValueError("A timezone-aware datetime is required")
    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return tuple(dt.isoformat().replace("+00:00", "Z") for dt in
                 (start, start + timedelta(days=1)))


def query_dataset(dataset: str, token: str, account: str, start: str,
                  end: str) -> list[dict]:
    # Separate requests prevent an unsupported dataset from poisoning others.
    query = """query($account: String!, $start: Time!, $end: Time!) {
      viewer { accounts(filter: {accountTag: $account}) {
        %s(limit: %d, filter: {datetime_geq: $start, datetime_lt: $end}) { %s }
      } }
    }""" % (dataset, ROW_LIMIT, DATASETS[dataset])
    payload = {"query": query, "variables": {
        "account": account, "start": start, "end": end}}
    request = urllib.request.Request(API, data=json.dumps(payload).encode(), headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(request, timeout=25) as response:
        result = json.load(response)
    if result.get("errors"):
        raise ValueError("Dataset unavailable")
    accounts = result["data"]["viewer"]["accounts"]
    if not isinstance(accounts, list) or len(accounts) != 1:
        raise ValueError("Account unavailable")
    rows = accounts[0][dataset]
    if not isinstance(rows, list) or len(rows) >= ROW_LIMIT:
        raise ValueError("Missing or potentially truncated dataset")
    return rows


def sum_field(rows: list[dict] | None, field: str) -> int | float | None:
    if rows is None:
        return None
    total = 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("sum"), dict):
            return None
        value = row["sum"].get(field)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            return None
        total += value
    return total


def snapshot(token: str, account: str, now: datetime | None = None,
             fetch=None) -> tuple[dict, int]:
    now = now or datetime.now(timezone.utc)
    start, end = day_window(now)
    fetch = fetch or query_dataset
    datasets = {}
    states = {}
    for name in DATASETS:
        try:
            if not token or not account:
                raise ValueError("Credentials unavailable")
            rows = fetch(name, token, account, start, end)
            if not isinstance(rows, list) or len(rows) >= ROW_LIMIT:
                raise ValueError("Invalid dataset")
            datasets[name] = rows
            states[name] = "available"
        except Exception:
            # Never serialize exception messages, response bodies or credentials.
            datasets[name] = None
            states[name] = "unavailable"
    used = dict.fromkeys(LIMITS)
    workers = sum_field(datasets["workersInvocationsAdaptive"], "requests")
    pages = sum_field(datasets["pagesFunctionsInvocationsAdaptiveGroups"], "requests")
    if workers is not None and pages is not None:
        used["workers_pages_requests"] = workers + pages
    kv = datasets["kvOperationsAdaptiveGroups"]
    if kv is not None:
        try:
            buckets = {name: [] for name in ("read", "write", "delete", "list")}
            for row in kv:
                action = row["dimensions"]["actionType"].lower()
                buckets[action].append(row)
            for action, metric in (("read", "kv_reads"), ("write", "kv_writes"),
                                   ("delete", "kv_deletes"), ("list", "kv_lists")):
                used[metric] = sum_field(buckets[action], "requests")
        except (KeyError, TypeError, AttributeError):
            pass
    d1 = datasets["d1AnalyticsAdaptiveGroups"]
    used["d1_rows_read"] = sum_field(d1, "rowsRead")
    used["d1_rows_written"] = sum_field(d1, "rowsWritten")
    metrics = {}
    warnings = []
    for name, limit in LIMITS.items():
        value = used[name]
        high = value is not None and value >= limit * THRESHOLD
        metrics[name] = {"used": value, "daily_limit": limit,
                         "fraction": value / limit if value is not None else None,
                         "threshold_reached": high if value is not None else None}
        if high:
            warnings.append(name)
    partial = any(value is None for value in used.values())
    code = 2 if partial else 1 if warnings else 0
    return {"schema_version": 1, "period_start": start, "period_end": end,
            "observed_at": now.astimezone(timezone.utc).isoformat(),
            "threshold": THRESHOLD, "status": "partial" if partial else
            "threshold" if warnings else "healthy", "datasets": states,
            "metrics": metrics, "local_warnings": warnings,
            "excluded": ["r2"]}, code


def write_output(path: Path, text: str) -> None:
    """Atomically replace local runtime JSON, private to the current user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".usage-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secrets", type=Path, default=REPO / "secrets.env")
    parser.add_argument("--output", type=Path, help="Optional local runtime JSON path")
    args = parser.parse_args(argv)
    report, code = snapshot(*credentials(args.secrets))
    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        try:
            write_output(args.output, text)
        except OSError:
            report["output_status"] = "unavailable"
            text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
            code = 2
    print(text, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
