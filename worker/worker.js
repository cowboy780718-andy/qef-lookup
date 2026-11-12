/**
 * QEF Lookup - the OLD stand-alone PDF proxy ("qef-pdf-proxy").
 *
 * SUPERSEDED by worker/site.js, which does the same job at the site's own
 * /pdf address, is deployed automatically with the site, and takes its list
 * of permitted hosts from the index instead of from the hand-typed list below.
 * The page only falls back to this Worker when /pdf is not available.
 *
 * It is kept so the fallback keeps working. The list below is only as current
 * as the last time someone deployed this file by hand - which is exactly the
 * problem site.js was written to remove - so once /pdf has been seen working
 * on the live site this Worker can simply be deleted from Cloudflare.
 *
 * Deploy (only if you want the fallback refreshed):  npx wrangler deploy
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
  "www.fidelity.ca",
  "www.renaissanceinvestments.ca",
  // Tier B
  "www.rbcgam.com",
  "www.manulifeim.com",
  "www.globalx.ca",
  "documents.purposeinvest.com",
  "www.dimensional.com",
  // Added after the first deploy - and never deployed, which is why these
  // failed in every ZIP until site.js took over.
  "bmo.bynder.com",
  "www.nbinvestments.ca",
  "www.ninepoint.com",
  "sprott.com",
  "www.fondsdesjardins.com",
  "www.guardiancapital.com",
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
