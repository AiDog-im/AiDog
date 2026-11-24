#!/usr/bin/env python3
"""Offline regression checks for post-deploy purge and the public beacon."""
import contextlib
import io
import json
import os
import shutil
import tempfile
from pathlib import Path
import subprocess
import unittest
import urllib.error
from unittest.mock import Mock, patch
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "deploy/deploy_site.sh").read_text()
PURGE = SCRIPT.split("<<'PYPURGE'\n", 1)[1].split("\nPYPURGE", 1)[0]


class BeaconParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.beacons = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src") == "https://static.cloudflareinsights.com/beacon.min.js":
            self.beacons.append(attrs)


class DeploySiteTests(unittest.TestCase):
    def run_purge(self, responses):
        opener = Mock()
        values = []
        for response in responses:
            if isinstance(response, Exception):
                values.append(response)
            else:
                handle = Mock()
                handle.status = 200
                handle.read.return_value = response
                context = Mock()
                context.__enter__ = Mock(return_value=handle)
                context.__exit__ = Mock(return_value=False)
                values.append(context)
        opener.open.side_effect = values
        output = io.StringIO()
        with patch.dict("os.environ", {"AIDOG_CF_API_TOKEN": "secret-test-only", "AIDOG_CF_ZONE_ID": "zone-test"}), patch("urllib.request.build_opener", return_value=opener), patch("time.sleep") as sleep, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as stopped:
                exec(compile(PURGE, "deploy_site.sh:purge", "exec"), {})
        self.assertNotIn("secret-test-only", output.getvalue())
        self.assertNotIn("sensitive-response", output.getvalue())
        return stopped.exception.code, opener, sleep, output.getvalue()

    def test_semantic_success(self):
        code, opener, sleep, output = self.run_purge([b'{"success":true,"errors":[],"result":{"id":"purge-id"}}'])
        self.assertEqual(code, 0)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertTrue(request.full_url.endswith("/zones/zone-test/purge_cache"))
        self.assertEqual(json.loads(request.data), {"purge_everything": True})
        self.assertIn("confirmed", output)
        sleep.assert_not_called()

    def test_fail_closed(self):
        for response in [b'{"success":false,"errors":["sensitive-response"]}', b'{"success":true}', b'{"success":true,"errors":[],"result":{}}', b'{"success":"true","errors":[],"result":{"id":"x"}}', b'{"success":true,"errors":["sensitive-response"],"result":{"id":"x"}}', b'[]', b'sensitive-response']:
            with self.subTest(response=response):
                code, opener, sleep, _ = self.run_purge([response])
                self.assertEqual(code, 1)
                self.assertEqual(opener.open.call_count, 1)
                sleep.assert_not_called()

    def test_retryable_failures_recover(self):
        for error in [urllib.error.URLError("sensitive-response"), urllib.error.HTTPError("url", 429, "sensitive-response", {}, None), urllib.error.HTTPError("url", 503, "sensitive-response", {}, None)]:
            with self.subTest(error=type(error).__name__):
                code, opener, sleep, _ = self.run_purge([error, b'{"success":true,"errors":[],"result":{"id":"x"}}'])
                self.assertEqual(code, 0)
                self.assertEqual(opener.open.call_count, 2)
                sleep.assert_called_once_with(1)

    def test_retries_are_bounded(self):
        code, opener, sleep, _ = self.run_purge([urllib.error.URLError("sensitive-response")] * 3)
        self.assertEqual(code, 1)
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_permission_failure_is_not_retried(self):
        code, opener, sleep, _ = self.run_purge([urllib.error.HTTPError("url", 403, "sensitive-response", {}, None)])
        self.assertEqual(code, 1)
        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_check_only_skips_write_branch(self):
        # Execute the actual outer write guard, replacing its body with a marker.
        start = SCRIPT.index('if [ "$CHECK_ONLY" -eq 0 ]; then\n')
        end = SCRIPT.index('\nfi\n\n# ---- 4) verify public endpoints', start)
        branch = SCRIPT[start:end + 3]
        self.assertIn('cf_purge_cache || die "post-deploy cache purge failed"', branch)
        self.assertGreater(branch.index("cf_purge_cache ||"), branch.index("wrangler pages deploy"))
        guard = branch.split("\n", 1)[0]
        checked = subprocess.run(["bash", "-c", 'CHECK_ONLY=1\n' + guard + '\nprintf unexpected-write\nfi'], capture_output=True, text=True, check=True)
        self.assertEqual(checked.stdout, "")

    def test_check_runs_without_credentials_or_mutating_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'deploy').mkdir()
            shutil.copy(ROOT / 'deploy/deploy_site.sh', root / 'deploy')
            for name in ('site', 'mycore', 'VERSION'):
                (root / name).symlink_to(ROOT / name)
            # Every curl call resolves to a local file or synthetic response.
            curl = root / 'curl'
            curl.write_text('''#!/bin/bash
set -eu
[[ "$*" != *" -X "* && "$*" != *" -d "* ]]
out=""
while (($# > 1)); do
  case "$1" in
    -o) out="$2"; shift 2;;
    -w|--noproxy|--max-time) shift 2;;
    -q|-s) shift;;
    *) exit 90;;
  esac
done
url="$1"
echo "$url" >> calls
case "$url" in
  https://www.aidog.im/|https://aidog.im/) cp site/index.html "$out";;
  https://aidog.im/models) cp site/models.html "$out";;
  https://aidog.im/api/health) echo '{"ok":true}' > "$out";;
  https://aidog.im/api/stats) echo '{"totals":{"requests":0},"installs":0,"reports":0,"daily":[],"by_model":[],"by_scenario":{},"probe_reports":0}' > "$out";;
  https://aidog.im/api/*) exit 91;;
  https://aidog.im/data/gh.json) echo '{"fetched_at":"2026-11-24T00:00:00Z","stars":0,"forks":0,"watchers":0,"discussions":null,"version":null,"notes_teaser":null}' > "$out";;
  https://aidog.im/*) cp "site/${url#https://aidog.im/}" "$out";;
  *) exit 92;;
esac
if [[ "$url" == "$BAD_URL" ]]; then echo '{}' > "$out"; echo "$BAD_STATUS"; else echo 200; fi
''')
            curl.chmod(0o700)
            for endpoint, status in [('', ''), ('health', '503'), ('stats', '503'), ('health', '200'), ('stats', '200')]:
                with self.subTest(endpoint=endpoint, status=status):
                    log = root / 'calls'
                    log.write_text('')
                    result = subprocess.run(['bash', 'deploy/deploy_site.sh', '--check'], cwd=root,
                        env={'PATH': tmp + os.pathsep + os.defpath, 'TMPDIR': tmp,
                             'BAD_URL': 'https://aidog.im/api/' + endpoint, 'BAD_STATUS': status},
                        capture_output=True, text=True)
                    self.assertEqual(result.returncode, int(bool(endpoint)), result.stderr)
                    self.assertEqual('ALL VERIFIED' in result.stdout, not endpoint)
                    self.assertEqual([s for s in log.read_text().splitlines() if '/api/' in s],
                                     ['https://aidog.im/api/stats', 'https://aidog.im/api/health'])

    def test_all_four_pages_have_one_deferred_beacon(self):
        pages = sorted((ROOT / "site").glob("*.html"))
        self.assertEqual(len(pages), 4)
        for page in pages:
            with self.subTest(page=page.name):
                parser = BeaconParser()
                parser.feed(page.read_text())
                self.assertEqual(len(parser.beacons), 1)
                beacon = parser.beacons[0]
                self.assertIn("defer", beacon)
                self.assertEqual(json.loads(beacon["data-cf-beacon"]), {"token": "7ffe3f32bb65471e8685ac2cb88051cd"})
                self.assertIn("assets/style.css?v=20261117-1", page.read_text())


if __name__ == "__main__":
    unittest.main()
