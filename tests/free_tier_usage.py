#!/usr/bin/env python3
"""Offline free-tier monitor contracts; no real credentials or network."""
from contextlib import redirect_stdout
from datetime import datetime, timezone, timedelta
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
spec = importlib.util.spec_from_file_location("free_tier_usage", REPO / "deploy/free_tier_usage.py")
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)
NOW = datetime(2026, 11, 17, 23, 59, 59, tzinfo=timezone.utc)


def fixture():
    return {
        "kvOperationsAdaptiveGroups": [
            {"dimensions": {"actionType": action}, "sum": {"requests": count}}
            for action, count in (("read", 11), ("write", 22), ("delete", 33), ("list", 44))],
        "workersInvocationsAdaptive": [{"sum": {"requests": 100}}],
        "pagesFunctionsInvocationsAdaptiveGroups": [{"sum": {"requests": 200}}],
        "d1AnalyticsAdaptiveGroups": [{"sum": {"rowsRead": 300, "rowsWritten": 400}}],
    }


def collect(data):
    return usage.snapshot("fake-token", "fake-account", NOW,
                          lambda name, *args: data[name])


class MonitorTests(unittest.TestCase):
    def test_categories_and_combined_budget(self):
        report, code = collect(fixture())
        self.assertEqual(code, 0)
        metrics = report["metrics"]
        for name, count in (("kv_reads", 11), ("kv_writes", 22),
                            ("kv_deletes", 33), ("kv_lists", 44),
                            ("workers_pages_requests", 300)):
            self.assertEqual(metrics[name]["used"], count)
        self.assertEqual(report["excluded"], ["r2"])
        self.assertNotIn("fake-token", json.dumps(report))
        self.assertEqual({name: value["daily_limit"] for name, value in metrics.items()},
                         usage.LIMITS)

    def test_utc_calendar_boundaries(self):
        self.assertEqual(usage.day_window(NOW),
                         ("2026-11-17T00:00:00Z", "2026-11-18T00:00:00Z"))
        east = datetime(2026, 12, 1, 0, 1, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(usage.day_window(east),
                         ("2026-11-30T00:00:00Z", "2026-12-01T00:00:00Z"))
        self.assertEqual(usage.day_window(NOW + timedelta(seconds=1))[0],
                         "2026-11-18T00:00:00Z")

    def test_dataset_failure_does_not_mask_other_datasets(self):
        data = fixture()
        seen = []
        def fetch(name, token, account, start, end):
            seen.append(name)
            self.assertEqual((start, end), usage.day_window(NOW))
            if name == "pagesFunctionsInvocationsAdaptiveGroups":
                raise RuntimeError("sensitive-response fake-token fake-account")
            return data[name]
        report, code = usage.snapshot("fake-token", "fake-account", NOW, fetch)
        self.assertEqual(code, 2)
        self.assertEqual(len(seen), 4)
        self.assertIsNone(report["metrics"]["workers_pages_requests"]["used"])
        self.assertEqual(report["metrics"]["d1_rows_read"]["used"], 300)
        self.assertEqual(report["metrics"]["kv_writes"]["used"], 22)
        self.assertNotIn("sensitive", json.dumps(report))

    def test_null_and_malformed_fields_are_unknown(self):
        for bad in (None, "1", -1, True, float("nan")):
            data = fixture()
            data["d1AnalyticsAdaptiveGroups"][0]["sum"]["rowsRead"] = bad
            report, code = collect(data)
            self.assertEqual(code, 2)
            self.assertIsNone(report["metrics"]["d1_rows_read"]["used"])
            self.assertEqual(report["metrics"]["d1_rows_written"]["used"], 400)

    def test_unknown_kv_category_does_not_report_false_zeros(self):
        data = fixture()
        data["kvOperationsAdaptiveGroups"] = [
            {"dimensions": {"actionType": "unknown"}, "sum": {"requests": 3}}]
        report, code = collect(data)
        self.assertEqual(code, 2)
        self.assertTrue(all(report["metrics"][key]["used"] is None
                            for key in ("kv_reads", "kv_writes", "kv_lists", "kv_deletes")))

    def test_empty_success_is_zero_missing_is_not(self):
        report, code = collect({name: [] for name in usage.DATASETS})
        self.assertEqual(code, 0)
        self.assertTrue(all(m["used"] == 0 for m in report["metrics"].values()))
        report, code = collect({name: None for name in usage.DATASETS})
        self.assertEqual(code, 2)
        self.assertTrue(all(m["used"] is None for m in report["metrics"].values()))

    def test_truncated_dataset_is_unknown(self):
        data = fixture()
        data["workersInvocationsAdaptive"] = [{"sum": {"requests": 1}}] * usage.ROW_LIMIT
        report, code = collect(data)
        self.assertEqual(code, 2)
        self.assertIsNone(report["metrics"]["workers_pages_requests"]["used"])
        self.assertEqual(report["metrics"]["kv_reads"]["used"], 11)

    def test_threshold_and_partial_precedence(self):
        data = fixture()
        data["workersInvocationsAdaptive"] = [{"sum": {"requests": 69800}}]
        report, code = collect(data)
        self.assertEqual(code, 1)
        self.assertIn("workers_pages_requests", report["local_warnings"])
        data["d1AnalyticsAdaptiveGroups"] = None
        report, code = collect(data)
        self.assertEqual(code, 2)
        self.assertIn("workers_pages_requests", report["local_warnings"])

    def test_each_daily_limit_threshold(self):
        for key, limit in usage.LIMITS.items():
            data = fixture()
            count = int(limit * usage.THRESHOLD)
            if key.startswith("kv_"):
                action = {"kv_reads": "read", "kv_writes": "write",
                          "kv_deletes": "delete", "kv_lists": "list"}[key]
                data["kvOperationsAdaptiveGroups"] = [
                    {"dimensions": {"actionType": action}, "sum": {"requests": count}}]
            elif key.startswith("d1_"):
                field = "rowsRead" if key == "d1_rows_read" else "rowsWritten"
                data["d1AnalyticsAdaptiveGroups"][0]["sum"][field] = count
            else:
                data["workersInvocationsAdaptive"] = [{"sum": {"requests": count}}]
                data["pagesFunctionsInvocationsAdaptiveGroups"] = []
            report, code = collect(data)
            self.assertEqual(code, 1, key)
            self.assertIn(key, report["local_warnings"])
            self.assertEqual(report["metrics"][key]["fraction"], 0.7)

    def test_query_is_read_only_and_rejects_graphql_errors(self):
        body = {"data": {"viewer": {"accounts": [{
            "workersInvocationsAdaptive": []}]}}}
        class Response(io.StringIO):
            pass
        with patch.object(usage.urllib.request, "urlopen", return_value=Response(json.dumps(body))) as open_mock:
            self.assertEqual(usage.query_dataset("workersInvocationsAdaptive", "token", "account",
                                                *usage.day_window(NOW)), [])
            request = open_mock.call_args.args[0]
            payload = json.loads(request.data)
            self.assertIn("datetime_geq: $start, datetime_lt: $end", payload["query"])
            self.assertNotIn("mutation", payload["query"])
            self.assertNotIn("token", payload["query"])
        with patch.object(usage.urllib.request, "urlopen", return_value=Response('{"errors":[{"message":"secret"}]}')):
            with self.assertRaisesRegex(ValueError, "Dataset unavailable"):
                usage.query_dataset("workersInvocationsAdaptive", "token", "account", *usage.day_window(NOW))

    def test_shared_secret_reader(self):
        from mycore import secrets_store
        with patch.dict(usage.os.environ, {}, clear=True), patch.object(
                secrets_store, "secrets_text", return_value="AIDOG_CF_API_TOKEN=test\nAIDOG_CF_ACCOUNT_ID=id\n") as reader:
            path = Path("/not-a-real-secret")
            self.assertEqual(usage.credentials(path), ("test", "id"))
            reader.assert_called_once_with(path)

    def test_cli_optional_runtime_output_and_no_default_write(self):
        report, code = collect(fixture())
        with tempfile.TemporaryDirectory() as tmp, patch.object(usage, "credentials", return_value=("", "")), patch.object(usage, "snapshot", return_value=(report, code)):
            output = Path(tmp) / "usage.json"
            stream = io.StringIO()
            with redirect_stdout(stream):
                self.assertEqual(usage.main([]), 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(usage.main(["--output", str(output)]), 0)
            self.assertEqual(json.loads(output.read_text()), report)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_output_failure_is_sanitized(self):
        report, code = collect(fixture())
        with patch.object(usage, "credentials", return_value=("", "")), patch.object(
                usage, "snapshot", return_value=(report, code)), patch.object(
                usage, "write_output", side_effect=OSError("sensitive-path")):
            stream = io.StringIO()
            with redirect_stdout(stream):
                self.assertEqual(usage.main(["--output", "unused"]), 2)
            self.assertEqual(json.loads(stream.getvalue())["output_status"], "unavailable")
            self.assertNotIn("sensitive-path", stream.getvalue())

    def test_missing_credentials_never_calls_network(self):
        def forbidden(*args):
            self.fail("Network must not be called without credentials")
        report, code = usage.snapshot("", "", NOW, forbidden)
        self.assertEqual(code, 2)
        self.assertTrue(all(m["used"] is None for m in report["metrics"].values()))


if __name__ == "__main__":
    unittest.main()
