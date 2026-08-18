/**
 * QEF Lookup - PDF fetch proxy (Cloudflare Worker, free tier)
 *
 * Why this exists: fund company servers do not send CORS headers, so the
 * browser cannot fetch their PDFs directly to bundle them into a ZIP. This
 * Worker fetches on the page's behalf and re-serves with CORS.
 *
 * It is NOT a general proxy. Requests are refused unless the target host is on
 * ALLOWED_HOSTS below, so this cannot be turned into an open relay if someone
 * finds the URL. Add a host here when you add a fund family to sources.yaml.
 *
 * Deploy:  npx wrangler deploy
 * Free tier: 100,000 requests/day.
 */

const ALLOWED_HOSTS = [
  // Tier A
  "www.mackenzieinvestments.com",
  "www.td.com",
  "www.vanguard.ca",
  "fund-docs.vanguard.com",
  "russellinvestments.com",
  "funds.cifinancial.com",
  "www.mawer.com",
  "az-prd-mawer-com-cms-bda9ehd8a2fqgdgn.a02.azurefd.net",
  "www.cibc.com",
  "www.blackrock.com",
  "www.capitalgroup.com",
  "www.fidelity.ca",
  "iaclarington.com",
  "www.iaclarington.com",
  "www.renaissanceinvestments.ca",
  // Tier B
  "www.rbcgam.com",
  "bmogam.com",
  "bmogamhub.com",
  "www.manulifeim.com",
  "www.globalx.ca",
  "www.purposeinvest.com",
  "documents.purposeinvest.com",
  "www.sunlifeglobalinvestments.com",
  "www.pimco.com",
  "www.dimensional.com",
];

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Max-Age": "86400",
};

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ error: "method not allowed" }, 405);
    }

    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return json({ ok: true, hosts: ALLOWED_HOSTS.length });
    }

    const target = url.searchParams.get("u");
    if (!target) return json({ error: "missing ?u=" }, 400);

    let t;
    try {
      t = new URL(target);
    } catch {
      return json({ error: "malformed url" }, 400);
    }
    if (t.protocol !== "https:") {
      return json({ error: "https only" }, 400);
    }
    if (!ALLOWED_HOSTS.includes(t.hostname)) {
      return json({ error: "host not allowlisted", host: t.hostname }, 403);
    }
    if (!t.pathname.toLowerCase().endsWith(".pdf")) {
      return json({ error: "only .pdf targets are proxied" }, 400);
    }

    // Cache aggressively: these documents are immutable once published.
    const cache = caches.default;
    const cacheKey = new Request(t.toString(), { method: "GET" });
    let hit = await cache.match(cacheKey);
    if (hit) return withCors(hit);

    const upstream = await fetch(t.toString(), {
      headers: {
        "User-Agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        Accept: "application/pdf,*/*",
        "X-Crawler": "QEFLookup/1.0 (+https://github.com/qef-lookup)",
      },
      cf: { cacheTtl: 86400 * 30, cacheEverything: true },
    });

    if (!upstream.ok) {
      return json({ error: "upstream error", status: upstream.status }, 502);
    }

    const body = await upstream.arrayBuffer();
    const resp = new Response(body, {
      status: 200,
      headers: {
        "Content-Type": "application/pdf",
        "Content-Length": String(body.byteLength),
        "Cache-Control": "public, max-age=2592000",
        ...CORS,
      },
    });
    await cache.put(cacheKey, resp.clone());
    return resp;
  },
};

function withCors(resp) {
  const h = new Headers(resp.headers);
  for (const [k, v] of Object.entries(CORS)) h.set(k, v);
  return new Response(resp.body, { status: resp.status, headers: h });
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}
