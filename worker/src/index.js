// RunWise API — Cloudflare Worker
// ----------------------------------------------------------------
// Endpoints
//   GET  /api/snapshot         -> latest combined Oura + Garmin payload (KV)
//   GET  /api/oura/fresh       -> live Oura readiness+sleep (last 7d)
//   POST /api/sync             -> push payload from local sync script (Bearer auth)
//   GET  /api/health           -> liveness probe
//
// Bindings (wrangler.toml + secrets)
//   OURA_TOKEN   secret  Personal Access Token from cloud.ouraring.com
//   SYNC_TOKEN   secret  shared secret used by the local Garmin sync script
//   RUNWISE      KV      key-value namespace
//
// CORS: open to GET/POST from any origin (the data is read-only and the only
// write path is gated by SYNC_TOKEN).

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
  "Access-Control-Max-Age": "86400",
};

const json = (data, init = {}) =>
  new Response(JSON.stringify(data), {
    status: init.status || 200,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": init.cache || "no-store",
      ...CORS,
      ...(init.headers || {}),
    },
  });

const err = (status, message) => json({ error: message }, { status });

// ---------- Oura ----------
async function ouraFetch(env, path, params) {
  const url = new URL(`https://api.ouraring.com/v2/usercollection/${path}`);
  for (const [k, v] of Object.entries(params || {})) url.searchParams.set(k, v);
  const r = await fetch(url, {
    headers: { Authorization: `Bearer ${env.OURA_TOKEN}` },
  });
  if (!r.ok) throw new Error(`Oura ${path} ${r.status}`);
  return r.json();
}

async function freshOura(env) {
  const today = new Date().toISOString().slice(0, 10);
  const start = new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10);
  const [readiness, sleep] = await Promise.all([
    ouraFetch(env, "daily_readiness", { start_date: start, end_date: today }),
    ouraFetch(env, "daily_sleep", { start_date: start, end_date: today }),
  ]);
  return {
    readiness: readiness.data || [],
    sleep: sleep.data || [],
    fetched_at: new Date().toISOString(),
  };
}

// ---------- Snapshot ----------
async function getSnapshot(env) {
  const stored = await env.RUNWISE.get("snapshot", "json");
  // Always try a fresh Oura pull (cheap), keep stored Garmin (only the local
  // sync script can refresh that).
  let oura = stored?.oura;
  try {
    oura = await freshOura(env);
  } catch (e) {
    // fall back to stored if fresh pull fails
  }
  return {
    oura,
    garmin: stored?.garmin || null,
    garmin_synced_at: stored?.garmin_synced_at || null,
    served_at: new Date().toISOString(),
  };
}

// ---------- Routes ----------
export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "");

    try {
      if (path === "/api/health") {
        return json({ ok: true, ts: new Date().toISOString() });
      }

      if (path === "/api/snapshot" && request.method === "GET") {
        const snap = await getSnapshot(env);
        return json(snap, { cache: "public, max-age=60" });
      }

      if (path === "/api/oura/fresh" && request.method === "GET") {
        const data = await freshOura(env);
        return json(data, { cache: "public, max-age=300" });
      }

      if (path === "/api/sync" && request.method === "POST") {
        const auth = request.headers.get("Authorization") || "";
        const token = auth.replace(/^Bearer\s+/i, "");
        if (!env.SYNC_TOKEN || token !== env.SYNC_TOKEN) {
          return err(401, "unauthorized");
        }
        const body = await request.json();
        const existing = (await env.RUNWISE.get("snapshot", "json")) || {};
        const next = {
          ...existing,
          ...(body.oura ? { oura: body.oura } : {}),
          ...(body.garmin
            ? { garmin: body.garmin, garmin_synced_at: new Date().toISOString() }
            : {}),
        };
        await env.RUNWISE.put("snapshot", JSON.stringify(next));
        return json({ ok: true, stored: Object.keys(next) });
      }

      return err(404, "not found");
    } catch (e) {
      return err(500, e.message || "internal error");
    }
  },
};
