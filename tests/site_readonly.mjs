// Real handler SQL against an isolated SQLite database with writes disabled.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { onRequestGet as health } from '../site/functions/api/health.js';
import { onRequestGet as stats } from '../site/functions/api/stats.js';

const db = new DatabaseSync(':memory:');
db.exec(readFileSync(new URL('../deploy/d1_migrations/0001_init.sql', import.meta.url), 'utf8'));
let now = Date.parse('2026-11-24T23:59:30Z');
Date.now = () => now;
let queries = 0;
const context = { env: { LEDGER: { prepare(sql) {
  queries++;
  const stmt = db.prepare(sql);
  let params = {};
  return {
    bind(value) { params = { 1: value }; return this; },
    first() { return stmt.get(params); },
    all() { return { results: stmt.all(params) }; }
  };
} } } };
db.exec('PRAGMA query_only=ON');
assert.equal((await health(context)).status, 200);
db.exec(`PRAGMA query_only=OFF;
  INSERT INTO ledger_daily(install_id,day,requests,updated_at) VALUES
  ('real','2026-11-24',3,'2026-11-24T12:00:00Z'), ('_probe','2026-11-24',11,'');
  PRAGMA query_only=ON`);
const first = await (await stats(context)).json();
assert.equal(first.totals.requests, 3);
assert.equal(first.today.requests, 3);
assert.equal(first.probe_reports, 1);
assert.equal(first.updated, '2026-11-24T12:00:00Z');
const baseline = queries;
assert.deepEqual(await (await stats(context)).json(), first);
assert.equal(queries, baseline);
now += 30000;
assert.equal((await (await stats(context)).json()).today.requests, 0);
assert.equal(queries, baseline + 6);
db.exec('PRAGMA query_only=OFF; DROP TABLE ledger_daily; PRAGMA query_only=ON');
assert.equal((await health(context)).status, 503);
now += 60000;
assert.equal((await stats(context)).status, 503);
db.exec('PRAGMA query_only=OFF');
db.exec(readFileSync(new URL('../deploy/d1_migrations/0001_init.sql', import.meta.url), 'utf8'));
db.exec('PRAGMA query_only=ON');
assert.equal((await (await stats(context)).json()).reports, 0);
db.exec('PRAGMA query_only=OFF; DROP TABLE site_visits; PRAGMA query_only=ON');
assert.equal((await health(context)).status, 503);
console.log('PASS read-only health, canary exclusion, cache hit, midnight and TTL expiry, unavailable and immediate recovery');
