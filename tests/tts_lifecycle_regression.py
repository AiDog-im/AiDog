#!/usr/bin/env python3
"""Offline TTS lifecycle regression tests; no provider calls or credential reads.

Run: python3 tests/tts_lifecycle_regression.py
Node.js is required for the isolated browser-function VM tests.
"""
from __future__ import annotations

import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
import urllib.error
from unittest.mock import MagicMock, patch
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("tts_regression_server", ROOT / "web/server.py")
server = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {}, clear=True):
    spec.loader.exec_module(server)

AUDIO = b"ID3 offline test audio"
LOCAL = {"media": {"tts_engine": "byok", "tts_base_url": "http://127.0.0.1:9/v1"}}


def response(data=AUDIO, content_type="audio/mpeg"):
    result = MagicMock()
    result.__enter__.return_value = result
    result.headers = {"Content-Type": content_type}
    result.read.return_value = data
    return result


class TTSLifecycle(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.slot = self.enterContext(patch.object(server, "read_env_slot", return_value=None))
        # Default denial also protects every accidentally added transport call.
        self.transport = self.enterContext(patch.object(
            server.urllib.request, "urlopen", side_effect=AssertionError("unexpected upstream call")))
        self.enterContext(patch.object(server, "_byok_say_lock", threading.Lock()))
        self.enterContext(patch.object(server, "_byok_say_retry_at", 0.0))
        self.enterContext(patch.object(server.time, "monotonic", return_value=1000.0))

    def assert_unlocked(self):
        self.assertTrue(server._byok_say_lock.acquire(blocking=False))
        server._byok_say_lock.release()

    def test_chat_endpoint_alone_never_calls_upstream(self):
        settings = {"mycore": {"base_url": "http://127.0.0.1:9/v1"}}
        self.assertEqual(server._tts_byok_cfg(settings), ("", False, ""))
        with self.assertRaisesRegex(RuntimeError, "no base URL"):
            server._synth_say_byok("hello", settings)
        self.transport.assert_not_called()
        self.slot.assert_not_called()
        self.assert_unlocked()

    def test_model_key_is_not_inherited(self):
        self.slot.side_effect = lambda name: "dummy-model-key" if name == "MODEL_API_KEY" else None
        with patch.dict(os.environ, {"MODEL_API_KEY": "dummy-env-model-key"}):
            settings = {"media": {"tts_base_url": "https://tts.invalid/v1"}}
            self.assertFalse(server._tts_byok_cfg(settings)[1])
            with self.assertRaisesRegex(RuntimeError, "no credential"):
                server._synth_say_byok("hello", settings)
        self.assertTrue(all(call.args == ("TTS_API_KEY",) for call in self.slot.call_args_list))
        self.transport.assert_not_called()

    def test_dedicated_env_url_and_key(self):
        self.slot.return_value = "dummy-tts-key"
        with patch.dict(os.environ, {"AIDOG_TTS_URL": "https://tts.invalid/v1/"}):
            self.transport.side_effect = None
            self.transport.return_value = response()
            self.assertEqual(server._synth_say_byok("hello", LOCAL), (AUDIO, "audio/mpeg"))
        req = self.transport.call_args.args[0]
        self.assertEqual(req.full_url, "https://tts.invalid/v1/audio/speech")
        self.assertEqual(req.get_header("Authorization"), "Bearer dummy-tts-key")
        self.assert_unlocked()

    def test_success_local_keyless_audio_and_request(self):
        self.transport.side_effect = None
        upstream = response(content_type="audio/mpeg; charset=binary")
        self.transport.return_value = upstream
        self.assertEqual(server._synth_say_byok("hello", LOCAL), (AUDIO, "audio/mpeg"))
        req = self.transport.call_args.args[0]
        self.assertEqual(req.full_url, "http://127.0.0.1:9/v1/audio/speech")
        self.assertEqual(req.method, "POST")
        self.assertIsNone(req.get_header("Authorization"))
        self.assertEqual(json.loads(req.data)["input"], "hello")
        self.assertEqual(self.transport.call_args.kwargs["timeout"], server.SAY_TIMEOUT)
        upstream.read.assert_called_once_with(server.BYOK_MAX_AUDIO + 1)
        self.assertEqual(server._byok_say_retry_at, 0)
        self.assert_unlocked()

    def test_429_retry_after_cooldown_then_recovery(self):
        error = urllib.error.HTTPError("https://tts.invalid", 429, "busy",
                                       {"Retry-After": "120"}, io.BytesIO(b"ignored"))
        self.transport.side_effect = error
        with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
            server._synth_say_byok("hello", LOCAL)
        self.assertEqual(server._byok_say_retry_at, 1120)
        self.assertTrue(error.fp.closed)
        with self.assertRaisesRegex(RuntimeError, "cooling down"):
            server._synth_say_byok("next", LOCAL)
        self.transport.assert_called_once()
        self.assert_unlocked()
        server.time.monotonic.return_value = 1121
        self.transport.side_effect = None
        self.transport.return_value = response()
        self.assertEqual(server._synth_say_byok("recovered", LOCAL)[0], AUDIO)
        self.assertEqual(self.transport.call_count, 2)
        self.assert_unlocked()

    def test_singleflight_rejects_second_concurrent_request(self):
        entered, release = threading.Event(), threading.Event()
        results, failures = [], []

        def upstream(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test release timed out")
            return response()

        def worker():
            try:
                results.append(server._synth_say_byok("first", LOCAL))
            except BaseException as exc:
                failures.append(exc)

        self.transport.side_effect = upstream
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                server._synth_say_byok("second", LOCAL)
            self.transport.assert_called_once()
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, [(AUDIO, "audio/mpeg")])
        self.assert_unlocked()

    def test_timeout_has_no_retry_and_releases_lock(self):
        self.transport.side_effect = TimeoutError("offline timeout")
        with self.assertRaisesRegex(RuntimeError, "unreachable: TimeoutError"):
            server._synth_say_byok("hello", LOCAL)
        self.assertEqual(server._byok_say_retry_at, 1030)
        with self.assertRaisesRegex(RuntimeError, "cooling down"):
            server._synth_say_byok("again", LOCAL)
        self.transport.assert_called_once()
        self.assert_unlocked()

    def test_invalid_audio_cools_down_and_releases_lock(self):
        for data, content_type in ((b"", "audio/mpeg"), (b"{}", "application/json"),
                                   (b"x" * (server.BYOK_MAX_AUDIO + 1), "audio/mpeg")):
            with self.subTest(content_type=content_type, length=len(data)):
                server._byok_say_retry_at = 0
                self.transport.reset_mock()
                self.transport.side_effect = None
                self.transport.return_value = response(data, content_type)
                with self.assertRaisesRegex(RuntimeError, "invalid audio"):
                    server._synth_say_byok("hello", LOCAL)
                self.assertEqual(server._byok_say_retry_at, 1030)
                with self.assertRaisesRegex(RuntimeError, "cooling down"):
                    server._synth_say_byok("again", LOCAL)
                self.transport.assert_called_once()
                self.assert_unlocked()

    def post_say(self):
        """Use http.client so mocking urllib never intercepts the test client."""
        class QuietHandler(server.Handler):
            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler) as httpd:
            thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            client = http.client.HTTPConnection(*httpd.server_address, timeout=3)
            try:
                client.request("POST", "/api/say", json.dumps({"text": "hello", "engine": "byok"}),
                               {"Content-Type": "application/json"})
                res = client.getresponse()
                return res.status, res.getheader("Content-Type"), res.read()
            finally:
                client.close()
                httpd.shutdown()
                thread.join(3)

    def test_handler_no_config_503_zero_upstream(self):
        with patch.object(server, "load_settings", return_value={"mycore": {"base_url": "http://127.0.0.1:9/v1"}}), \
                patch.object(server, "_say_rate_ok", return_value=True):
            status, ctype, body = self.post_say()
        self.assertEqual(status, 503)
        self.assertIn("no base URL", json.loads(body)["error"])
        self.transport.assert_not_called()

    def test_handler_success_serves_audio(self):
        self.transport.side_effect = None
        self.transport.return_value = response()
        with patch.object(server, "load_settings", return_value=LOCAL), \
                patch.object(server, "_say_rate_ok", return_value=True):
            status, ctype, body = self.post_say()
        self.assertEqual((status, ctype, body), (200, "audio/mpeg", AUDIO))
        self.transport.assert_called_once()
        self.assert_unlocked()

    def test_handler_upstream_429_maps_503_and_cools_down(self):
        self.transport.side_effect = urllib.error.HTTPError(
            "https://tts.invalid", 429, "busy", {"Retry-After": "90"}, io.BytesIO())
        with patch.object(server, "load_settings", return_value=LOCAL), \
                patch.object(server, "_say_rate_ok", return_value=True):
            self.assertEqual(self.post_say()[0], 503)
            status, _, body = self.post_say()
        self.assertEqual(status, 503)
        self.assertIn("cooling down", json.loads(body)["error"])
        self.transport.assert_called_once()
        self.assert_unlocked()


class BrowserVoice(unittest.TestCase):
    def test_isolated_node_vm(self):
        self.assertIsNotNone(shutil.which("node"), "Node.js is required for browser regressions")
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
function fn(name) {
  const re = new RegExp('^(?:async )?function ' + name + '\\([^]*?^}', 'm');
  const match = source.match(re);
  assert(match, 'missing function ' + name);
  return match[0];
}
const nodes = {};
const context = {
  settings: {sound: false}, voiceReady: false, voiceEngine: {available:true},
  voiceBusy:false, voiceLastText:'', voiceLastAt:0, voiceError:'',
  params: new URLSearchParams(), dismissedLevel:'', unsavedMode:false,
  window:{speechSynthesis:true},
  document:{documentElement:{lang:'en'}, getElementById(id) {
    return nodes[id] ||= {hidden:true, innerHTML:''};
  }},
  SpeechSynthesisUtterance: function(text) {this.text=text;},
  speechSynthesis:{getVoices:()=>[],cancel(){},speak(u){u.onend();}},
  voiceVolume:()=>0.8, renderVoice(){}, refreshVoiceCap:async()=>{},
  t:(_, fallback)=>fallback, beep(){}, reasonLi:s=>s, closeUnsavedAlert(){},
  vsState(){}, Date,
  fetch:async()=>{throw Error('unexpected fetch');},
};
vm.createContext(context);
vm.runInContext(['speakLocal','speak','armVoice','applyAlert','vsSpeakReply'].map(fn).join('\n'), context);
(async()=>{
  let fetches=0, plays=0;
  context.fetch=async()=>{fetches++; return {ok:true,arrayBuffer:async()=>new ArrayBuffer(0)};};
  context.playBuf=async()=>{plays++;};
  // No local voice means automatic speech fails quietly without any server call.
  assert.equal(await context.speak('automatic', {onDeviceOnly:true}),false);
  assert.equal(fetches,0); assert.equal(context.voiceBusy,false);
  context.speechSynthesis.getVoices=()=>[{localService:false,lang:'en-GB'}];
  assert.equal(await context.speak('remote browser voice', {onDeviceOnly:true}),false);
  assert.equal(fetches,0);
  context.speechSynthesis.getVoices=()=>[{localService:true,lang:'en-US'}];
  assert.equal(await context.speak('local browser voice', {onDeviceOnly:true}),true);
  assert.equal(fetches,0);
  assert.equal(await context.speak('explicit speech'),true);
  assert.equal(fetches,1); assert.equal(plays,1);
  // Exercise the actual automatic callers with a spy on their options.
  const calls=[];
  context.speak=async(...args)=>{calls.push(args);return true;};
  await context.armVoice(); assert.equal(calls.length,0);
  context.settings.sound=true;
  await context.armVoice(); assert.equal(calls.length,1);
  assert.equal(calls[0][1].onDeviceOnly,true);
  calls.length=0;
  context.settings.sound=false;
  context.applyAlert({level:'urgent',reasons:['test']}); assert.equal(calls.length,0);
  nodes.alertLayer.hidden=true; context.settings.sound=true;
  context.applyAlert({level:'urgent',reasons:['test']}); assert.equal(calls.length,1);
  assert.equal(calls[0][1].onDeviceOnly,true);
  context.applyAlert({level:'urgent',reasons:['test']}); assert.equal(calls.length,1);
  // The microphone reply must obey sound and retain explicit engine routing.
  calls.length=0; context.settings.sound=false;
  await context.vsSpeakReply('mic muted'); assert.equal(calls.length,0);
  context.settings.sound=true;
  await context.vsSpeakReply('mic audible'); assert.equal(calls.length,1);
  assert.equal(calls[0].length,1);
  // The escalation block belongs to a large renderer: pin its automatic route.
  assert.match(source, /if \(voiceReady && settings\.sound && !voiceBusy\)[^]*?speak\(line, \{ onDeviceOnly: true \}\)/);
  console.log('PASS browser voice lifecycle');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(["node", "-e", script, str(ROOT / "web/static/app.js")],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS browser voice lifecycle", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
