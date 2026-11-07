import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const repo = require('../repo.js');

// Fake fs: only statSync knows about files listed in `files` (absolute paths).
function fakeFs(files, { onWrite } = {}) {
  const written = new Map();
  return {
    written,
    statSync(p) {
      if (files.has(p)) return { isFile: () => true };
      const err = new Error('ENOENT: ' + p);
      err.code = 'ENOENT';
      throw err;
    },
    readFileSync(p) {
      if (written.has(p)) return written.get(p);
      const err = new Error('ENOENT: ' + p);
      err.code = 'ENOENT';
      throw err;
    },
    mkdirSync() {},
    writeSync() {},
    writeFileSync(p, data) {
      if (onWrite) onWrite(p, data);
      written.set(p, String(data));
    },
  };
}

const SRV = (dir) => path.join(dir, 'web', 'server.py');

test('priority 1: AIDOG_REPO env wins over everything', () => {
  const files = new Set([SRV('/env/repo'), SRV('/dev/repo'), SRV('/cwd/repo')]);
  const fsLike = fakeFs(files);
  const { repo: got } = repo.resolveRepo({
    env: { AIDOG_REPO: '/env/repo' },
    devDir: '/dev/repo',
    cwd: '/cwd/repo',
    cacheDir: '/cache',
    fsLike,
  });
  assert.equal(got, '/env/repo');
});

test('priority 2: dev-relative dir when it has web/server.py', () => {
  const files = new Set([SRV('/dev/repo'), SRV('/cwd/repo')]);
  const fsLike = fakeFs(files);
  const { repo: got } = repo.resolveRepo({
    env: {},
    devDir: '/dev/repo',
    cwd: '/cwd/repo',
    cacheDir: '/cache',
    fsLike,
  });
  assert.equal(got, '/dev/repo');
});

test('priority 3: cwd then cwd parent', () => {
  const fsLike = fakeFs(new Set([SRV('/cwd/repo')]));
  const { repo: got } = repo.resolveRepo({
    env: {},
    devDir: '/dev/repo', // invalid (no server.py)
    cwd: '/cwd/repo',
    cacheDir: '/cache',
    fsLike,
  });
  assert.equal(got, '/cwd/repo');

  const fsLike2 = fakeFs(new Set([SRV('/cwd')]));
  const got2 = repo.resolveRepo({
    env: {},
    devDir: '/dev/repo',
    cwd: '/cwd/repo', // /cwd/repo invalid, parent /cwd valid
    cacheDir: '/cache',
    fsLike: fsLike2,
  });
  assert.equal(got2.repo, '/cwd');
});

test('priority 4: userData cache last', () => {
  const fsLike = fakeFs(new Set([SRV('/cached/repo')]));
  fsLike.written.set('/cache/repo-cache.json', JSON.stringify({ repo: '/cached/repo' }));
  const { repo: got } = repo.resolveRepo({
    env: {},
    devDir: '/dev/repo',
    cwd: '/cwd/repo',
    cacheDir: '/cache',
    fsLike,
  });
  assert.equal(got, '/cached/repo');
});

test('no candidate: returns null (error.html path)', () => {
  const fsLike = fakeFs(new Set());
  const { repo: got, base } = repo.resolveRepo({
    env: {},
    devDir: '/dev/repo',
    cwd: '/cwd/repo',
    cacheDir: '/cache',
    fsLike,
  });
  assert.equal(got, null);
  assert.equal(base, 'http://127.0.0.1:19167');
});

test('base honors AIDOG_WEB_PORT for probe/spawn port', () => {
  assert.equal(repo.baseUrl({ AIDOG_WEB_PORT: '18790' }), 'http://127.0.0.1:18790');
  assert.equal(repo.baseUrl({}), 'http://127.0.0.1:19167');
  assert.equal(repo.baseUrl({ AIDOG_WEB_PORT: '' }), 'http://127.0.0.1:19167');
});

test('W4-01: hostile/corrupt AIDOG_WEB_PORT never reaches the URL', () => {
  // wrap-up audit W4-01: `19167@evil:443` would make baseUrl()
  // `http://127.0.0.1:19167@evil:443` (userinfo@host -> loadURL loads the
  // attacker origin). Every non-integer value falls through the SAME chain
  // as web/server.py _resolve_web_port: env -> pinned -> default.
  const inject = '19167@evil.example:443';
  assert.equal(repo.baseUrl({ AIDOG_WEB_PORT: inject }),
    'http://127.0.0.1:19167');                 // -> default
  assert.equal(repo.baseUrl({ AIDOG_WEB_PORT: inject }, '19200'),
    'http://127.0.0.1:19200');                 // -> pinned
  assert.equal(repo.baseUrl({ AIDOG_WEB_PORT: 'abc' }),
    'http://127.0.0.1:19167');
  assert.equal(repo.baseUrl({ AIDOG_WEB_PORT: '80x80' }),
    'http://127.0.0.1:19167');
  assert.equal(repo.baseUrl({ AIDOG_WEB_PORT: '99999' }),
    'http://127.0.0.1:19167');                 // out of range
  assert.equal(repo.baseUrl({ AIDOG_WEB_PORT: '-5' }),
    'http://127.0.0.1:19167');
  // valid integers still win over pinned (car A2 contract preserved)
  assert.equal(repo.baseUrl({ AIDOG_WEB_PORT: '20001' }, '19200'),
    'http://127.0.0.1:20001');
});

test('car A2 port chain: pinned port.local beats the default, env beats the file', () => {
  // env > pinned > default — the SAME order as web/server.py.
  assert.equal(repo.baseUrl({}, '19200'), 'http://127.0.0.1:19200');
  assert.equal(repo.baseUrl({ AIDOG_WEB_PORT: '18790' }, '19200'),
    'http://127.0.0.1:18790');
  assert.equal(repo.baseUrl({}, null), 'http://127.0.0.1:19167');
  // readPinnedPort validates the integer range and never throws.
  const fsLike = {
    readFileSync(p) {
      if (String(p).endsWith('port.local')) return ' 19201 \n';
      throw new Error('enoent');
    },
  };
  assert.equal(repo.readPinnedPort('/dev/repo', fsLike), '19201');
  assert.equal(repo.readPinnedPort('/dev/repo', {
    readFileSync() { return '99999'; },
  }), null);
  assert.equal(repo.readPinnedPort('/dev/repo', {
    readFileSync() { throw new Error('enoent'); },
  }), null);
});

test('successful resolve refreshes the cache file (best-effort)', () => {
  const fsLike = fakeFs(new Set([SRV('/dev/repo')]));
  const { repo: got } = repo.resolveRepo({
    env: {},
    devDir: '/dev/repo',
    cwd: '/cwd/repo',
    cacheDir: '/cache',
    fsLike,
  });
  assert.equal(got, '/dev/repo');
  assert.equal(
    fsLike.written.get('/cache/repo-cache.json'),
    JSON.stringify({ repo: '/dev/repo' })
  );
});
