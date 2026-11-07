'use strict';
// Console supervisor for the I'm AiDog desktop shell.
// Pure Node -- NO Electron imports. Probe GET {base}/api/health:
//   healthy  -> { mode: 'attached', base }          (never killed by us)
//   unhealthy -> spawn `python3 server.py` (cwd <repo>/web), poll health
//                up to ~10s -> { mode: 'spawned', base, child }
//   failure -> throws with a clear message.
// Wrap-up audit W4-02: a healthy probe is NOT enough -- a hostile local
// listener can steal the port and answer 200. Before the shell loads the
// console origin (whose localStorage holds WEB_TOKEN) it requires the
// listener to prove it knows WEB_TOKEN: GET /api/health?nonce=<hex> must
// come back with sig = HMAC-SHA256(WEB_TOKEN, nonce). The shell reads the
// token from the SAME file the console does (never echoed, never sent), a
// hostile listener cannot forge the sig, and a replay dies on the fresh
// nonce. Installations with no WEB_TOKEN yet (fresh provision) skip the
// check -- there is no token in localStorage to steal either.
// All collaborators are injectable (fetchImpl / spawnImpl / readToken /
// logger) so node --test can exercise every branch with fakes -- no real
// ports and no real secrets.env.

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { baseUrl, portFromEnv, readPinnedPort } = require('./repo');

const PROBE_TIMEOUT_MS = 1500;
const READY_TIMEOUT_MS = 10000;
const READY_INTERVAL_MS = 250;

function defaultLogger() {
  /* silent by default */
}

// Own-line WEB_TOKEN read from <repo>/secrets.env -- mirrors web/server.py
// _slot_value and lib/profile_paths.secrets_path (AIDOG_PROFILE ->
// <profiles>/<name>/secrets.env, invalid name fails closed to "no token"
// exactly like profile_paths.ProfileError refuses to resolve). Returns null
// when the file or the slot is absent/unreadable; the value never leaves
// this function except as the verifyConsole key.
function readWebToken(repo, env, fsLike) {
  try {
    const f = fsLike || fs;
    const e = env || {};
    const prof = String(e.AIDOG_PROFILE || '').trim();
    let file;
    if (prof) {
      if (!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(prof)) return null;
      const root = String(e.AIDOG_PROFILES_DIR || '').trim() ||
        path.join(repo, 'profiles');
      file = path.join(root, prof, 'secrets.env');
    } else {
      file = path.join(repo, 'secrets.env');
    }
    const m = /^WEB_TOKEN=(\S[^\r\n]*)$/m.exec(f.readFileSync(file, 'utf8'));
    return m ? m[1].trim() : null;
  } catch {
    return null;
  }
}

// Challenge-response against {base}/api/health?nonce=<hex> (see header).
// Resolves true when the listener proved knowledge of WEB_TOKEN, when no
// token is configured (nothing to protect), or when no repo is known (the
// file cannot be located -- production always passes a resolved repo).
async function verifyConsole({ base, repo, env, fetchImpl, readToken, logger }) {
  let token = null;
  if (readToken) {
    token = readToken(repo, env);
  } else if (repo) {
    token = readWebToken(repo, env);
  }
  if (!token) return true; // fresh install: no token -> no sig expected
  const nonce = crypto.randomBytes(16).toString('hex');
  try {
    const res = await fetchImpl(`${base}/api/health?nonce=${nonce}`, {
      method: 'GET',
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    if (!res || !res.ok) return false;
    const body = await res.json();
    const expect = crypto.createHmac('sha256', token).update(nonce).digest('hex');
    const got = body && typeof body.sig === 'string' ? body.sig : '';
    const ok = got.length === expect.length &&
      crypto.timingSafeEqual(Buffer.from(got), Buffer.from(expect));
    if (!ok && logger) logger(`console at ${base} failed the health signature check -- refusing to attach`);
    return ok;
  } catch {
    return false;
  }
}

// Probe + verify in one verdict (both attach and spawn-poll use THIS).
async function healthy(base, opts) {
  if (!(await probeHealth(base, opts.fetchImpl))) return false;
  return verifyConsole({
    base,
    repo: opts.repo,
    env: opts.env,
    fetchImpl: opts.fetchImpl,
    readToken: opts.readToken,
    logger: opts.logger,
  });
}

// Probe {base}/api/health. Resolves true only on a 2xx JSON-ish answer.
async function probeHealth(base, fetchImpl) {
  try {
    const res = await fetchImpl(`${base}/api/health`, {
      method: 'GET',
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    return !!(res && res.ok);
  } catch {
    return false;
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/**
 * ensureConsole({ repo, base, fetchImpl, spawnImpl, logger, readyTimeoutMs, readyIntervalMs })
 * - base: optional override; default derived from AIDOG_WEB_PORT of process.env.
 * Returns { mode, base, child? } -- child only when mode === 'spawned'.
 */
async function ensureConsole(opts = {}) {
  const env = opts.env || process.env;
  const fetchImpl = opts.fetchImpl || ((...a) => fetch(...a));
  const spawnImpl = opts.spawnImpl || require('child_process').spawn;
  const logger = opts.logger || defaultLogger;
  // Car A2: the shell follows the SAME port chain as the server (env >
  // <repo>/web/port.local > 19167), so an operator who pinned the port in
  // the settings card gets an attach/spawn on that exact port.
  const pinned = opts.repo ? readPinnedPort(opts.repo) : null;
  const base = opts.base || baseUrl(env, pinned);
  const repo = opts.repo;
  const readyTimeoutMs = opts.readyTimeoutMs ?? READY_TIMEOUT_MS;
  const readyIntervalMs = opts.readyIntervalMs ?? READY_INTERVAL_MS;

  if (await healthy(base, { repo, env, fetchImpl, readToken: opts.readToken, logger })) {
    logger(`console healthy at ${base} -- attaching`);
    return { mode: 'attached', base };
  }

  if (!repo) {
    throw new Error(
      `Local console at ${base} is not reachable and no install directory is known to start one.`
    );
  }

  // Car J (owner #5): the Windows shell is attach-only by design — there is
  // no python3/tmux stack for it to spawn, so fail fast with the honest
  // instruction instead of waiting out a spawn that cannot exist.
  const platform = opts.platform || process.platform;
  if (platform === 'win32') {
    throw new Error(
      `Local console at ${base} is not running. The Windows shell only attaches: ` +
      `start the console where it runs (WSL2: bash deploy/boot_services.sh) ` +
      `or point AIDOG_WEB_PORT at a running one, then reopen.`
    );
  }

  logger(`console unhealthy at ${base} -- spawning python3 server.py in ${repo}/web`);
  const child = spawnImpl('python3', ['server.py'], {
    cwd: require('path').join(repo, 'web'),
    env: { ...env, AIDOG_WEB_PORT: portFromEnv(env, pinned) },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  child.on?.('error', (err) => logger(`spawn error: ${err.message}`));

  const deadline = Date.now() + readyTimeoutMs;
  for (;;) {
    if (await healthy(base, { repo, env, fetchImpl, readToken: opts.readToken, logger })) {
      return { mode: 'spawned', base, child };
    }
    if (Date.now() >= deadline) {
      try {
        child.kill();
      } catch {
        /* best effort */
      }
      throw new Error(
        `Spawned console in ${repo}/web did not become healthy at ${base} within ${readyTimeoutMs}ms.`
      );
    }
    await sleep(readyIntervalMs);
  }
}

/**
 * stopSpawned(state) -- kills ONLY a child this shell spawned.
 * `state` is the object returned by ensureConsole; attached mode (no child)
 * is a no-op by design: never touch a pre-existing server.
 */
function stopSpawned(state) {
  if (!state || state.mode !== 'spawned' || !state.child) return false;
  try {
    state.child.kill();
    return true;
  } catch {
    return false;
  }
}

module.exports = { ensureConsole, stopSpawned, probeHealth, verifyConsole, readWebToken, PROBE_TIMEOUT_MS, READY_TIMEOUT_MS };
