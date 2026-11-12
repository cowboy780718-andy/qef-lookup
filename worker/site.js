/**
 * QEF Lookup - the site's own Worker.
 *
 * The website itself is static: every file under ./web is served by
 * Cloudflare directly and never reaches this code. Only a request that matches
 * no file arrives here, and exactly one such request is answered:
 *
 *     /pdf?u=<statement url>
 *
 * Why it exists: fund company servers do not send CORS headers, so a web page
 * is not allowed to read their PDFs to bundle them into a ZIP. This fetches a
 * statement on the page's behalf and hands it back from our own address.
 *
 * It replaces a separate "qef-pdf-proxy" Worker whose list of permitted hosts
 * was typed in by hand and had to be redeployed by hand. That list was never
 * updated after the first deploy, so every manager added later - BMO, National
 * Bank, Ninepoint, Sprott, Desjardins, Guardian, 731 statements - appeared on
 * the site and then failed inside every ZIP. Here the list is not typed at
 * all: it is web/data/hosts.json, which the crawler regenerates from the index
 * each night, and this Worker is redeployed with the site on every push.
 *
 * It is NOT a general proxy:
 *   - only hosts that the published index actually links to
 *   - only https, only paths ending in .pdf
 *   - the response must really be a PDF, or it is not passed on
 *   - other websites cannot use it from a browser
 */

const HOSTS_TTL_MS = 10 * 60 * 1000;
const MAX_BYTES = 40 * 1024 * 1024;
const UPSTREAM_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  Accept: "application/pdf,*/*",
  "X-Crawler": "QEFLookupBot/1.1 (+https://qef-lookup.cowboy780718.workers.dev)",
};

let hostsCache = null;
let hostsLoadedAt = 0;

/** The hosts the index links to. Read from the site's own files, cached briefly. */
async function allowedHosts(env, request) {
  if (hostsCache && Date.now() - hostsLoadedAt < HOSTS_TTL_MS) return hostsCache;
  try {
    const r = await env.ASSETS.fetch(new Request(new URL("/data/hosts.json", request.url)));
    if (r.ok) {
      const body = await r.json();
      hostsCache = new Set((body.hosts || []).map((h) => String(h).toLowerCase()));
      hostsLoadedAt = Date.now();
    }
  } catch {
    // Keep whatever list we had. An empty list refuses everything, which is
    // the safe way to fail.
  }
  return hostsCache || new Set();
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

function isPdf(bytes) {
  // "%PDF" somewhere in the first kilobyte: a few publishers put a byte-order
  // mark or a blank line ahead of the header.
  const head = new Uint8Array(bytes.slice(0, 1024));
  for (let i = 0; i + 3 < head.length; i++) {
    if (head[i] === 0x25 && head[i + 1] === 0x50 && head[i + 2] === 0x44 && head[i + 3] === 0x46) {
      return true;
    }
  }
  return false;
}

async function servePdf(request, env, ctx, url) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return json({ error: "method not allowed" }, 405);
  }
  // Browsers label where a request came from. Another website embedding this
  // address is "cross-site"; our own page is "same-origin". (Tools that send
  // no label at all, like curl, are let through - they are not the concern.)
  if (request.headers.get("Sec-Fetch-Site") === "cross-site") {
    return json({ error: "this endpoint only serves the QEF Lookup site" }, 403);
  }

  const raw = url.searchParams.get("u");
  if (!raw) return json({ error: "missing ?u=" }, 400);
  let target;
  try {
    target = new URL(raw);
  } catch {
    return json({ error: "malformed url" }, 400);
  }
  target.hash = "";                       // "#page=37" is for the reader, not the server
  if (target.protocol !== "https:") return json({ error: "https only" }, 400);
  if (target.username || target.password || target.port) {
    return json({ error: "plain https urls only" }, 400);
  }
  if (!target.pathname.toLowerCase().endsWith(".pdf")) {
    return json({ error: "only .pdf targets are served" }, 400);
  }
  const hosts = await allowedHosts(env, request);
  if (!hosts.has(target.hostname.toLowerCase())) {
    return json({ error: "host is not in the index", host: target.hostname }, 403);
  }

  const cache = typeof caches !== "undefined" ? caches.default : null;
  const cacheKey = new Request(target.toString(), { method: "GET" });
  if (cache) {
    const hit = await cache.match(cacheKey);
    if (hit) return hit;
  }

  let upstream;
  try {
    upstream = await fetch(target.toString(), { headers: UPSTREAM_HEADERS, redirect: "follow" });
  } catch (e) {
    return json({ error: "could not reach the manager's site", detail: String(e) }, 502);
  }
  if (!upstream.ok) {
    return json({ error: "the manager's site refused the request", status: upstream.status }, 502);
  }
  const declared = Number(upstream.headers.get("Content-Length") || 0);
  if (declared > MAX_BYTES) return json({ error: "document too large" }, 502);

  const body = await upstream.arrayBuffer();
  if (body.byteLength > MAX_BYTES) return json({ error: "document too large" }, 502);
  if (!isPdf(body)) {
    // An error page served with status 200. Passing it on would put a
    // web page inside the ZIP under a .pdf name.
    return json({ error: "the manager's site did not return a PDF" }, 502);
  }

  const resp = new Response(body, {
    status: 200,
    headers: {
      "Content-Type": "application/pdf",
      "Content-Length": String(body.byteLength),
      "Content-Disposition": "attachment",
      "X-Content-Type-Options": "nosniff",
      // A day, not a month: a reissued statement should not be served stale
      // for weeks.
      "Cache-Control": "public, max-age=86400",
    },
  });
  if (cache) ctx.waitUntil(cache.put(cacheKey, resp.clone()));
  return resp;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/pdf") return servePdf(request, env, ctx, url);
    if (url.pathname === "/pdf/health") {
      const hosts = await allowedHosts(env, request);
      return json({ ok: hosts.size > 0, hosts: hosts.size });
    }
    // Anything else that is not a file: let the asset server give its 404.
    return env.ASSETS.fetch(request);
  },
};
