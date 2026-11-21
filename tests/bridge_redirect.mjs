import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = (await readFile(new URL('../deploy/bridge/worker.js', import.meta.url), 'utf8'))
  .replace('const ALLOW = new Set([', 'const ALLOW = new Set(["allowed.example",');
const worker = (await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)).default;
const original = globalThis.fetch;
let calls = [];
try {
  globalThis.fetch = async (url, options) => {
    calls.push(url);
    assert.equal(options.redirect, 'manual');
    return new Response(null, {status: 302, headers: {location: 'https://blocked.example/private'}});
  };
  let response = await worker.fetch(new Request('https://bridge.example/https://allowed.example/start'));
  assert.equal(response.status, 403);
  assert.equal(calls.length, 1);
  calls = [];
  globalThis.fetch = async (url) => {
    calls.push(url);
    if (calls.length === 1) return new Response(null, {status: 302, headers: {location: '/ok'}});
    return new Response('success');
  };
  response = await worker.fetch(new Request('https://bridge.example/https://allowed.example/start'));
  assert.equal(await response.text(), 'success');
  assert.equal(calls[1], 'https://allowed.example/ok');
  calls = [];
  response = await worker.fetch(new Request('https://bridge.example/https://user:password@allowed.example/'));
  assert.equal(response.status, 403);
  assert.equal(calls.length, 0);
  console.log('PASS: off-list redirects blocked, relative redirects allowed, URL credentials refused');
} finally {
  globalThis.fetch = original;
}
