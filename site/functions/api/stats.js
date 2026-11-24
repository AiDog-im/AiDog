// Pages Function — GET /api/stats: public aggregates over the D1 usage
// ledger (D2 car). This is the endpoint the site's models chart reads.
//
//   GET /api/stats -> 200 {
//     updated, installs, reports, probe_reports,
//     totals {requests, tokens{in,out}},
//     today  {requests, tokens{in,out}, installs},
//     daily  [{day, requests, in, out, installs}, ...]  (last 7 UTC days),
//     by_model [{model, requests, in, out, share}],
//     by_scenario {chat, coding, research, voice, publishing}
//   }
//   D1 binding missing -> 503 {"error": "ledger not configured"}
//   D1 query failure   -> 503 {"error": "stats unavailable"} (honest, never
//                          a fabricated zero)
//
// Aggregates EXCLUDE canary rows (install_id starting with "_", reserved
// for verification probes); their count is surfaced as probe_reports only —
// an install_id is never echoed anywhere (privacy whitelist §5).
//
// Honest windows: clients report once per UTC day, so the windows offered
// are "today so far (UTC)", the last 7 UTC days and "all time". No rolling
// 24h claim is made anywhere — the data cannot support one.

import { json, SCENARIOS, REAL_ROW, PROBE_ROW } from "./_ledger_lib.js";

// One successful aggregate per D1 binding per warm isolate. Expiry is also
// bounded by UTC midnight because the today/week windows roll over then.
const snapshots = new WeakMap();
const ttlMs = 60000;

function utcDay(offsetDays) {
  const d = new Date(Date.now() - offsetDays * 86400000);
  return d.toISOString().slice(0, 10);
}

export async function onRequestGet({ env }) {
  const db = env.LEDGER;
  if (!db || typeof db.prepare !== "function") {
    return json({ error: "ledger not configured" }, 503);
  }
  const now = Date.now();
  const cached = snapshots.get(db);
  if (cached && cached.expires > now) return json(cached.body, 200);
  try {
    const totals = await db
      .prepare(
        "SELECT COUNT(*) AS reports, COUNT(DISTINCT install_id) AS installs," +
          " COALESCE(SUM(requests), 0) AS requests," +
          " COALESCE(SUM(tok_in), 0) AS tin," +
          " COALESCE(SUM(tok_out), 0) AS tout," +
          " MAX(updated_at) AS updated FROM ledger_daily WHERE " + REAL_ROW
      )
      .first();
    const probes = await db
      .prepare("SELECT COUNT(*) AS n FROM ledger_daily WHERE " + PROBE_ROW)
      .first();
    const today = await db
      .prepare(
        "SELECT COALESCE(SUM(requests), 0) AS requests," +
          " COALESCE(SUM(tok_in), 0) AS tin," +
          " COALESCE(SUM(tok_out), 0) AS tout," +
          " COUNT(DISTINCT install_id) AS installs FROM ledger_daily" +
          " WHERE day = ?1 AND " + REAL_ROW
      )
      .bind(utcDay(0))
      .first();
    const week = await db
      .prepare(
        "SELECT day, SUM(requests) AS requests, SUM(tok_in) AS tin," +
          " SUM(tok_out) AS tout, COUNT(DISTINCT install_id) AS installs" +
          " FROM ledger_daily WHERE day >= ?1 AND " + REAL_ROW +
          " GROUP BY day ORDER BY day ASC"
      )
      .bind(utcDay(6))
      .all();
    const scenRow = await db
      .prepare(
        "SELECT " +
          SCENARIOS.map(
            (s) => "COALESCE(SUM(json_extract(scenarios_json, '$." + s + "')), 0) AS " + s
          ).join(", ") +
          " FROM ledger_daily WHERE " + REAL_ROW
      )
      .first();
    // by_model: merge the per-day model JSON. Bounded read (recent 5000
    // rows); at current scale this covers everything, and the cap keeps the
    // function O(1)-ish forever. Documented in the design doc §2.
    const modelRows = await db
      .prepare(
        "SELECT models_json FROM ledger_daily WHERE " + REAL_ROW +
          " ORDER BY day DESC LIMIT 5000"
      )
      .all();
    const byModel = new Map();
    for (const row of modelRows.results || []) {
      let arr = [];
      try {
        arr = JSON.parse(row.models_json || "[]");
      } catch (err) {
        continue;
      }
      if (!Array.isArray(arr)) continue;
      for (const m of arr) {
        if (!m || typeof m.model !== "string") continue;
        const slot = byModel.get(m.model) || {
          model: m.model, requests: 0, in: 0, out: 0
        };
        slot.requests += Number(m.requests) || 0;
        slot.in += Number(m.in) || 0;
        slot.out += Number(m.out) || 0;
        byModel.set(m.model, slot);
      }
    }
    const models = [...byModel.values()].sort((a, b) => b.requests - a.requests);
    const denom = models.reduce((s, m) => s + m.requests, 0);
    for (const m of models) {
      m.share = denom > 0 ? Math.round((m.requests / denom) * 1000) / 1000 : 0;
    }
    const byScenario = {};
    for (const s of SCENARIOS) {
      byScenario[s] = (scenRow && Number(scenRow[s])) || 0;
    }
    const daily = (week.results || []).map((r) => ({
      day: r.day,
      requests: Number(r.requests) || 0,
      in: Number(r.tin) || 0,
      out: Number(r.tout) || 0,
      installs: Number(r.installs) || 0
    }));
    const body = {
        updated: (totals && totals.updated) || null,
        installs: (totals && Number(totals.installs)) || 0,
        reports: (totals && Number(totals.reports)) || 0,
        probe_reports: (probes && Number(probes.n)) || 0,
        totals: {
          requests: (totals && Number(totals.requests)) || 0,
          tokens: {
            in: (totals && Number(totals.tin)) || 0,
            out: (totals && Number(totals.tout)) || 0
          }
        },
        today: {
          requests: (today && Number(today.requests)) || 0,
          tokens: {
            in: (today && Number(today.tin)) || 0,
            out: (today && Number(today.tout)) || 0
          },
          installs: (today && Number(today.installs)) || 0
        },
        daily,
        by_model: models.slice(0, 12),
        by_scenario: byScenario
    };
    const nextMidnight = now - (now % 86400000) + 86400000;
    snapshots.set(db, { body, expires: Math.min(now + ttlMs, nextMidnight) });
    return json(body, 200);
  } catch (err) {
    return json({ error: "stats unavailable" }, 503);
  }
}
