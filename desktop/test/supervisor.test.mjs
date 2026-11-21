import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { ensureConsole, stopSpawned, verifyConsole, readWebToken } = require('../supervisor.js');
const crypto = require('node:crypto');

// ---- fakes -------------------------------------------------------------

// fetch fake: healthy only after `healthyAfter` total calls (>=1 means first
// call is unhealthy). Call count shared via the returned handle.
function makeFetch({ healthyAfter = 1, ok = true } = {}) {
  const state = { calls: 0 };
  const fn = async () => {
    state.calls += 1;
    return { ok: state.calls >= healthyAfter && ok, status: 200 };
  };
  return { fn, state };
}

function makeChild() {
  const child = new EventEmitter();
  child.killed = false;
  child.kill = () => {
    child.killed = true;
    return true;
  };
  return child;
}

function makeSpawn() {
  const state = { calls: [] };
  const fn = (cmd, args, opts) => {
    const child = makeChild();
    state.calls.push({ cmd, args, opts, child });
    return child;
  };
  return { fn, state };
}

const silent = () => {};
const BASE = 'http://127.0.0.1:18790';

// ---- attach ------------------------------------------------------------

test('probe healthy -> attached (no spawn)', async () => {
  const fetchFake = makeFetch({ healthyAfter: 1 });
  const spawnFake = makeSpawn();
  const res = await ensureConsole({
    repo: '/some/repo',
    base: BASE,
    fetchImpl: fetchFake.fn,
    spawnImpl: spawnFake.fn,
    logger: silent,
  });
  assert.equal(res.mode, 'attached');
  assert.equal(res.base, BASE);
  assert.equal(spawnFake.state.calls.length, 0);
  assert.equal(res.child, undefined);
});

test('probe healthy hits GET {base}/api/health', async () => {
  const seen = [];
  const fetchFake = makeFetch({ healthyAfter: 1 });
  const spy = async (url, init) => {
    seen.push({ url, method: init && init.method });
    return fetchFake.fn(url, init);
  };
  await ensureConsole({
    repo: '/some/repo',
    base: BASE,
    fetchImpl: spy,
    spawnImpl: makeSpawn().fn,
    logger: silent,
  });
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, `${BASE}/api/health`);
  assert.equal(seen[0].method, 'GET');
});

// ---- win32 attach-only (car J) ----------------------------------------

test('win32: healthy console still attaches; down console fails fast, never spawns', async () => {
  // healthy -> the platform gate must not shadow the attach path
  const up = makeFetch({ healthyAfter: 1 });
  const upSpawn = makeSpawn();
  const attached = await ensureConsole({
    repo: '/some/repo',
    base: BASE,
    platform: 'win32',
    fetchImpl: up.fn,
    spawnImpl: upSpawn.fn,
    logger: silent,
  });
  assert.equal(attached.mode, 'attached');
  assert.equal(upSpawn.state.calls.length, 0);

  // down -> honest attach-only error before any spawn or 10s poll
  const down = makeFetch({ healthyAfter: Infinity });
  const downSpawn = makeSpawn();
  await assert.rejects(
    () => ensureConsole({
      repo: '/some/repo',
      base: BASE,
      platform: 'win32',
      fetchImpl: down.fn,
      spawnImpl: downSpawn.fn,
      logger: silent,
    }),
    /only attaches/
  );
  assert.equal(downSpawn.state.calls.length, 0);
  assert.equal(down.state.calls, 1); // one probe, then the honest throw
});

// ---- spawn -------------------------------------------------------------

test('probe unhealthy -> spawn python3 server.py in {repo}/web, then healthy', async () => {
  const fetchFake = makeFetch({ healthyAfter: 4 }); // 3 failures then OK
  const spawnFake = makeSpawn();
  const res = await ensureConsole({
    repo: '/some/repo',
    base: BASE,
    platform: 'linux',           // car J: spawn legs are platform-explicit
    env: { AIDOG_WEB_PORT: '18790' },
    fetchImpl: fetchFake.fn,
    spawnImpl: spawnFake.fn,
    logger: silent,
    readyIntervalMs: 5,
  });
  assert.equal(res.mode, 'spawned');
  assert.equal(res.base, BASE);
  assert.equal(spawnFake.state.calls.length, 1);
  const call = spawnFake.state.calls[0];
  assert.equal(call.cmd, 'python3');
  assert.deepEqual(call.args, ['server.py']);
  assert.equal(call.opts.cwd, require('node:path').join('/some/repo', 'web'));
  assert.equal(call.opts.env.AIDOG_WEB_PORT, '18790');
  assert.equal(res.child, call.child);
  assert.equal(fetchFake.state.calls, 4);
});

test('base port derives from AIDOG_WEB_PORT when no base override', async () => {
  const fetchFake = makeFetch({ healthyAfter: 1 });
  const seen = [];
  const spy = async (url, init) => {
    seen.push(url);
    return fetchFake.fn(url, init);
  };
  const res = await ensureConsole({
    repo: '/some/repo',
    env: { AIDOG_WEB_PORT: '18791' },
    fetchImpl: spy,
    spawnImpl: makeSpawn().fn,
    logger: silent,
  });
  assert.equal(res.base, 'http://127.0.0.1:18791');
  assert.equal(seen[0], 'http://127.0.0.1:18791/api/health');
});

// ---- timeout -----------------------------------------------------------

test('spawn never becomes healthy -> throws, child killed', async () => {
  const fetchFake = makeFetch({ healthyAfter: Number.MAX_SAFE_INTEGER });
  const spawnFake = makeSpawn();
  await assert.rejects(
    () =>
      ensureConsole({
        repo: '/some/repo',
        base: BASE,
        platform: 'linux',       // car J: spawn legs are platform-explicit
        fetchImpl: fetchFake.fn,
        spawnImpl: spawnFake.fn,
        logger: silent,
        readyTimeoutMs: 30,
        readyIntervalMs: 5,
      }),
    /did not become healthy/
  );
  assert.equal(spawnFake.state.calls.length, 1);
  assert.equal(spawnFake.state.calls[0].child.killed, true);
});

test('unhealthy + no repo -> throws clear message, nothing spawned', async () => {
  const spawnFake = makeSpawn();
  await assert.rejects(
    () =>
      ensureConsole({
        repo: null,
        base: BASE,
        fetchImpl: makeFetch({ healthyAfter: Number.MAX_SAFE_INTEGER }).fn,
        spawnImpl: spawnFake.fn,
        logger: silent,
      }),
    /no install directory/
  );
  assert.equal(spawnFake.state.calls.length, 0);
});

// ---- stopSpawned -------------------------------------------------------

test('stopSpawned kills only a spawned child', async () => {
  const res = await ensureConsole({
    repo: '/some/repo',
    base: BASE,
    platform: 'linux',           // car J: spawn legs are platform-explicit
    fetchImpl: makeFetch({ healthyAfter: 2 }).fn,
    spawnImpl: makeSpawn().fn,
    logger: silent,
    readyIntervalMs: 5,
  });
  assert.equal(res.mode, 'spawned');
  assert.equal(stopSpawned(res), true);
  assert.equal(res.child.killed, true);
});

test('stopSpawned in attached mode is a no-op', async () => {
  const res = await ensureConsole({
    repo: '/some/repo',
    base: BASE,
    fetchImpl: makeFetch({ healthyAfter: 1 }).fn,
    spawnImpl: makeSpawn().fn,
    logger: silent,
  });
  assert.equal(res.mode, 'attached');
  assert.equal(stopSpawned(res), false);
  assert.equal(stopSpawned(null), false);
  assert.equal(stopSpawned({ mode: 'attached', base: BASE }), false);
});

// ---- W4-02: health signature verification -------------------------------

// fetch fake that answers BOTH plain health and ?nonce= health: the nonce
// leg returns `sig` computed with `token` (a correct listener) or a wrong
// one (a hostile listener that stole the port).
function makeSignedFetch({ token = 'test-token', good = true } = {}) {
  const seen = [];
  const fn = async (url, init) => {
    seen.push(String(url));
    const u = new URL(String(url));
    const nonce = u.searchParams.get('nonce');
    let body = { status: 'ok', version: '0.0.0', ts: 1 };
    if (nonce) {
      const key = good ? token : 'attacker-guess';
      body.sig = crypto.createHmac('sha256', key).update(nonce).digest('hex');
    }
    return { ok: true, status: 200, json: async () => body };
  };
  return { fn, seen };
}

test('W4-02: valid health signature verifies -> console accepted', async () => {
  const f = makeSignedFetch({ token: 'sekrit', good: true });
  const ok = await verifyConsole({
    base: BASE, repo: '/r', fetchImpl: f.fn,
    readToken: () => 'sekrit', logger: silent,
  });
  assert.equal(ok, true);
  assert.equal(f.seen.length, 1);
  assert.ok(f.seen[0].includes('nonce='), f.seen[0]);
});

test('W4-02: hostile listener (wrong sig) is refused', async () => {
  const f = makeSignedFetch({ token: 'sekrit', good: false });
  const ok = await verifyConsole({
    base: BASE, repo: '/r', fetchImpl: f.fn,
    readToken: () => 'sekrit', logger: silent,
  });
  assert.equal(ok, false);
});

test('W4-02: listener that omits the sig entirely is refused', async () => {
  const fetchFake = async () => ({
    ok: true, status: 200, json: async () => ({ status: 'ok' }),
  });
  const ok = await verifyConsole({
    base: BASE, repo: '/r', fetchImpl: fetchFake,
    readToken: () => 'sekrit', logger: silent,
  });
  assert.equal(ok, false);
});

test('W4-02: no WEB_TOKEN configured (fresh install) skips the check', async () => {
  let called = false;
  const fetchFake = async () => { called = true; throw new Error('must not fetch'); };
  const ok = await verifyConsole({
    base: BASE, repo: '/r', fetchImpl: fetchFake,
    readToken: () => null, logger: silent,
  });
  assert.equal(ok, true);
  assert.equal(called, false);
});

test('W4-02: readWebToken parses the own-line slot (profile-aware)', () => {
  const fsFake = {
    readFileSync(p) {
      if (String(p).endsWith('profiles/beta/secrets.env'))
        return 'A=x\nWEB_TOKEN=abc123\nZ=y\n';
      if (String(p).endsWith('secrets.env')) return 'WEB_TOKEN=base-token\n';
      throw new Error('enoent');
    },
  };
  assert.equal(readWebToken('/r', {}, fsFake), 'base-token');
  assert.equal(readWebToken('/r', { AIDOG_PROFILE: 'beta' }, fsFake), 'abc123');
  // invalid profile name fails closed -> null (never another profile's brain)
  assert.equal(readWebToken('/r', { AIDOG_PROFILE: '../evil' }, fsFake), null);
  // missing file -> null (fresh install)
  const none = { readFileSync() { throw new Error('enoent'); } };
  assert.equal(readWebToken('/r', {}, none), null);
});
