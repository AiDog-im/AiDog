import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { validateReport } from '../site/functions/api/_ledger_lib.js';
import { onRequestPost } from '../site/functions/api/ledger.js';
import { onRequestGet as stats } from '../site/functions/api/stats.js';
import { onRequestGet as visits } from '../site/functions/api/visits.js';

import { onRequestGet as health } from '../site/functions/api/health.js';

const spec = JSON.parse(readFileSync(new URL('../site/openapi.json', import.meta.url)));
const example = spec.paths['/api/ledger'].post.requestBody.content['application/json'].examples.minimal.value;
Date.now = () => Date.parse(example.day + 'T12:00:00Z');
assert.equal(validateReport(example).ok, true);
const fixtures = [];
async function capture(path, method, response, expected) {
  assert.equal(response.status, expected);
  const mime = response.headers.get('content-type').split(';', 1)[0];
  const body = mime === 'application/json' ? await response.json() : await response.text();
  fixtures.push({ path, method, status: response.status, mime, body });
  return body;
}
const fakeDb = { prepare(sql) {
  return { bind() { return this; }, async first() {
    if (sql.startsWith('INSERT INTO site_visits')) return 1;
    assert.match(sql, /^SELECT/);
    return {};
  }, async all() { assert.match(sql, /^SELECT/); return { results: [] }; }, async run() {
    assert.match(sql, /^INSERT INTO ledger_daily/);
  } };
} };
const healthDb = { prepare(sql) {
  assert.match(sql, /^SELECT .*ledger_daily.*site_visits/);
  return { first: async () => ({ has_report: 0, visits: null }) };
} };
const broken = { prepare() { throw new Error('fixture'); } };
const report = (doc, db) => onRequestPost({ env: { LEDGER: db }, request: new Request('https://fixture.invalid/api/ledger', { method: 'POST', body: JSON.stringify(doc) }) });
await capture('/api/ledger', 'post', await report(example, fakeDb), 200);
assert.deepEqual(await capture('/api/ledger', 'post', await report({ ...example, tokens: null }, fakeDb), 400), { error: 'bad tokens' });
await capture('/api/ledger', 'post', await report(example, broken), 500);
await capture('/api/ledger', 'post', await report(example, undefined), 503);
assert.equal((await capture('/api/stats', 'get', await stats({ env: { LEDGER: fakeDb } }), 200)).updated, null);
await capture('/api/stats', 'get', await stats({ env: { LEDGER: broken } }), 503);
for (const [suffix, db, status] of [['', fakeDb, 200], ['?format=text', fakeDb, 200], ['', undefined, 503]]) {
  await capture('/api/visits', 'get', await visits({ env: { LEDGER: db }, request: new Request('https://fixture.invalid/api/visits' + suffix) }), status);
}
for (const [db, status] of [[healthDb, 200], [undefined, 503], [broken, 503]]) {
  await capture('/api/health', 'get', await health({ env: { LEDGER: db } }), status);
}
console.log(JSON.stringify(fixtures));
