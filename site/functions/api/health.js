// Readiness for both D1-backed features, including a freshly migrated empty DB.
import { json } from './_ledger_lib.js';

export async function onRequestGet({ env }) {
  try {
    const row = await env.LEDGER.prepare(
      "SELECT EXISTS(SELECT 1 FROM ledger_daily LIMIT 1) AS has_report," +
      " (SELECT count FROM site_visits WHERE name = 'count') AS visits"
    ).first();
    if (row && (row.has_report === 0 || row.has_report === 1) &&
        (row.visits === null || (Number.isSafeInteger(row.visits) && row.visits >= 0))) {
      return json({ ok: true }, 200);
    }
  } catch {
    // Never disclose binding names, SQL or D1 errors to callers.
  }
  return json({ error: 'site storage unavailable' }, 503);
}
