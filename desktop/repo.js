'use strict';
// Repo resolution for the I'm AiDog desktop shell.
// Priority: AIDOG_REPO env -> dev-relative dir -> cwd and cwd parent ->
//           last-known cache in Electron userData -> null.
// Pure Node (no Electron imports) so node --test can cover the priority
// order with injected candidates and an injected fs-like object.

const path = require('path');

// Car A2 (port standard): same chain as web/server.py _resolve_web_port —
// AIDOG_WEB_PORT env > <repo>/web/port.local > 19167 product default.
const DEFAULT_PORT = '19167';

function portFromEnv(env, pinned) {
  const raw = env && env.AIDOG_WEB_PORT;
  if (raw && String(raw).trim()) {
    const v = String(raw).trim();
    // Wrap-up audit W4-01: only a plain integer port may reach URL
    // construction — a corrupt/hostile value (`19167@evil:443`) must fall
    // through exactly like web/server.py _resolve_web_port does, never
    // become `http://127.0.0.1:19167@evil:443` (userinfo@host injection).
    if (/^\d{1,5}$/.test(v) && Number(v) >= 1 && Number(v) <= 65535) return v;
  }
  if (pinned && String(pinned).trim()) return String(pinned).trim();
  return DEFAULT_PORT;
}

// Best-effort read of <repo>/web/port.local (one integer line). Any read or
// parse failure falls back to the default — never throws.
function readPinnedPort(repoDir, fsLike) {
  const fs = fsLike || require('fs');
  try {
    const raw = fs.readFileSync(path.join(repoDir, 'web', 'port.local'), 'utf8').trim();
    const port = Number(raw);
    return Number.isInteger(port) && port >= 1024 && port <= 65535 ? raw : null;
  } catch {
    return null;
  }
}

function baseUrl(env, pinned) {
  return `http://127.0.0.1:${portFromEnv(env, pinned)}`;
}

// A candidate repo is valid when <dir>/web/server.py exists.
function isRepo(dir, fsLike) {
  if (!dir) return false;
  try {
    return fsLike.statSync(path.join(dir, 'web', 'server.py')).isFile();
  } catch {
    return false;
  }
}

// Pure core: pick the first valid candidate from an ordered list.
// Returns that candidate, or null.
function pickCandidate(candidates, fsLike) {
  for (const c of candidates) {
    if (isRepo(c, fsLike)) return c;
  }
  return null;
}

// Cache file lives in Electron userData (best-effort read/write).
function readCache(cacheDir, fsLike) {
  try {
    const raw = fsLike.readFileSync(path.join(cacheDir, 'repo-cache.json'), 'utf8');
    const parsed = JSON.parse(raw);
    return typeof parsed.repo === 'string' ? parsed.repo : null;
  } catch {
    return null;
  }
}

function writeCache(cacheDir, repo, fsLike) {
  try {
    fsLike.mkdirSync(cacheDir, { recursive: true });
    fsLike.writeFileSync(
      path.join(cacheDir, 'repo-cache.json'),
      JSON.stringify({ repo }),
      'utf8'
    );
    return true;
  } catch {
    return false;
  }
}

// Ordered candidate list for a given environment.
// env: process.env-like; opts: { devDir, cwd, cacheDir, fsLike }
function candidates(env, opts) {
  const fsLike = opts.fsLike || require('fs');
  const list = [];
  if (env && env.AIDOG_REPO) list.push(env.AIDOG_REPO);
  if (opts.devDir) list.push(opts.devDir);
  if (opts.cwd) {
    list.push(opts.cwd);
    list.push(path.dirname(opts.cwd));
  }
  if (opts.cacheDir) {
    const cached = readCache(opts.cacheDir, fsLike);
    if (cached) list.push(cached);
  }
  // de-dupe preserving order
  const seen = new Set();
  return list.filter((c) => c && !seen.has(c) && seen.add(c));
}

/**
 * resolveRepo({ env, devDir, cwd, cacheDir, fsLike })
 * Returns { repo, base } or { repo: null, base }.
 */
function resolveRepo(opts) {
  const env = opts.env || {};
  const fsLike = opts.fsLike || require('fs');
  const repo = pickCandidate(candidates(env, opts), fsLike);
  // Car A2: base follows the resolved repo's port chain (env > port.local
  // > 19167) — computed AFTER repo resolution so a pinned port.local wins.
  const base = baseUrl(env, repo ? readPinnedPort(repo, fsLike) : null);
  if (repo && opts.cacheDir) writeCache(opts.cacheDir, repo, fsLike); // best-effort refresh
  return { repo, base };
}

module.exports = {
  DEFAULT_PORT,
  portFromEnv,
  readPinnedPort,
  baseUrl,
  isRepo,
  pickCandidate,
  candidates,
  readCache,
  writeCache,
  resolveRepo,
};
