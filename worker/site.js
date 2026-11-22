/**
 * QEF Lookup - the site's own Worker.
 *
 * The website itself is static: every file under ./web is served by
 * Cloudflare directly and never reaches this code. Only a request that matches
 * no file arrives here, and exactly one such request is answered:
 *
 *     /pdf?u=<statement url>
 *
 * and one more, the Contact form (see serveContact, below):
 *
 *     /contact          POST name, email, message -> emailed to the site owner
 *     /contact/health   whether the form is set up
 *
 * Why /pdf exists: fund company servers do not send CORS headers, so a web page
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
 *   - only https, and only paths ending in .pdf (bar the few hosts the index
 *     marks as publishing statements without that suffix)
 *   - the response must really be a PDF, or it is not passed on
 *   - from the Internet Archive, only a copy of a file from one of those
 *     same hosts (earlier years a manager has since taken down)
 *   - other websites cannot use it from a browser
 */

const HOSTS_TTL_MS = 10 * 60 * 1000;
const ARCHIVE_HOST = "web.archive.org";
const MAX_BYTES = 40 * 1024 * 1024;
const UPSTREAM_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  Accept: "application/pdf,*/*",
  "X-Crawler": "QEFLookupBot/1.1 (+https://qef-lookup.cowboy780718.workers.dev)",
};

let hostsCache = null;
let anyPathHosts = new Set();
let hostsLoadedAt = 0;

/** The hosts the index links to. Read from the site's own files, cached briefly. */
async function allowedHosts(env, request) {
  if (hostsCache && Date.now() - hostsLoadedAt < HOSTS_TTL_MS) return hostsCache;
  try {
    const r = await env.ASSETS.fetch(new Request(new URL("/data/hosts.json", request.url)));
    if (r.ok) {
      const body = await r.json();
      hostsCache = new Set((body.hosts || []).map((h) => String(h).toLowerCase()));
      anyPathHosts = new Set((body.any_path_hosts || []).map((h) => String(h).toLowerCase()));
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
  const hosts = await allowedHosts(env, request);
  const host = target.hostname.toLowerCase();
  // A few managers serve statements from addresses with no ".pdf" on the end;
  // the index says which. For everyone else the suffix is required. (What
  // comes back is checked to be a PDF in every case, further down.)
  const suffixless = anyPathHosts.has(host);
  if (!suffixless && !target.pathname.toLowerCase().endsWith(".pdf")) {
    return json({ error: "only .pdf targets are served" }, 400);
  }
  if (!hosts.has(host)) {
    return json({ error: "host is not in the index", host: target.hostname }, 403);
  }
  if (host === ARCHIVE_HOST) {
    // The archive holds a copy of everything. Being allowed to fetch a
    // manager's earlier statements from it must not mean fetching anything
    // else it has: the copy has to be of a file from a host in the index.
    const m = /^\/web\/\d{14}(?:id_)?\/https?:\/\/([^\/:?#]+)\//i.exec(target.pathname);
    if (!m || m[1].toLowerCase() === ARCHIVE_HOST || !hosts.has(m[1].toLowerCase())) {
      return json({ error: "not an archived copy of a document in the index" }, 403);
    }
  }
  if (suffixless) target.search = "";     // "?preview=1" and the like

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

// --------------------------------------------------------------------------
// Contact form
// --------------------------------------------------------------------------
//
// A visitor writes to the site owner without the owner's address ever
// appearing on the site or in this repository (which is public). The address
// and the key of the mail service live in Cloudflare as Worker secrets:
//
//     CONTACT_TO       where messages go
//     RESEND_API_KEY   key for resend.com, which sends them
//     CONTACT_FROM     optional; without a domain of your own, Resend sends
//                      from onboarding@resend.dev, to your own address only
//
// Until both of the first two are set, /contact/health says so and the page
// does not show the Contact button at all.
//
// Nothing is stored. The message is passed to Resend and the answer passed
// back; the visitor's address goes in Reply-To, so answering is one click.

export const CONTACT_LIMITS = { name: 200, email: 254, message: 20000 };
const CONTACT_GAP_S = 30;            // one message per visitor per half minute
const RESEND_URL = "https://api.resend.com/emails";
const EMAIL_RE = /^[^\s@<>(),;:"]+@[^\s@<>(),;:"]+\.[^\s@<>(),;:"]{2,}$/;

const contactReady = (env) => Boolean(env.RESEND_API_KEY && env.CONTACT_TO);

async function serveContact(request, env, ctx) {
  if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
  // Only our own page may post here: browsers label a post from another site.
  const origin = request.headers.get("Origin");
  if (request.headers.get("Sec-Fetch-Site") === "cross-site" ||
      (origin && origin !== new URL(request.url).origin)) {
    return json({ error: "this form only accepts messages from the QEF Lookup site" }, 403);
  }
  if (!contactReady(env)) return json({ error: "the contact form is not set up yet" }, 503);

  let body;
  try {
    const raw = await request.text();
    if (raw.length > 64 * 1024) return json({ error: "message too long" }, 413);
    body = JSON.parse(raw);
  } catch {
    return json({ error: "malformed request" }, 400);
  }
  const name = String(body?.name ?? "").trim();
  const email = String(body?.email ?? "").trim();
  const message = String(body?.message ?? "").replace(/\r\n/g, "\n").trim();

  // A field a person cannot see. Something that fills it in is a script:
  // tell it all went well, and send nothing.
  if (String(body?.website ?? "").trim()) return json({ ok: true });

  if (!name || name.length > CONTACT_LIMITS.name) {
    return json({ error: `Please give a name (up to ${CONTACT_LIMITS.name} characters).` }, 400);
  }
  if (email.length > CONTACT_LIMITS.email || !EMAIL_RE.test(email)) {
    return json({ error: "Please give an email address you can be reached at." }, 400);
  }
  if (!message) return json({ error: "Please write a message." }, 400);
  if (message.length > CONTACT_LIMITS.message) {
    return json({ error: `The message is over ${CONTACT_LIMITS.message.toLocaleString("en")} characters.` }, 400);
  }

  // One message per visitor every half minute: enough for a person, and
  // a cap on anyone trying to flood the owner's inbox.
  const ip = request.headers.get("CF-Connecting-IP") || "";
  const cache = typeof caches !== "undefined" ? caches.default : null;
  const gateKey = ip && new Request(`https://contact-gate.invalid/${encodeURIComponent(ip)}`);
  if (cache && gateKey && await cache.match(gateKey)) {
    return json({ error: "Please wait a moment before sending another message." }, 429);
  }

  const oneLine = (s) => s.replace(/[\r\n\t]+/g, " ").slice(0, 120);
  const sent = new Date().toISOString().replace("T", " ").slice(0, 16) + " UTC";
  const text = [
    `Name:    ${name}`,
    `Email:   ${email}`,
    `Sent:    ${sent}`,
    "",
    message,
    "",
    "--",
    "Sent from the Contact form on QEF Statement Lookup. Reply to this email to answer.",
  ].join("\n");

  if (env.CONTACT_DRY_RUN) {           // the local dev server: nothing leaves the machine
    console.log(`[contact, not sent]\n${text}`);
    return json({ ok: true });
  }

  let upstream;
  try {
    upstream = await fetch(RESEND_URL, {
      method: "POST",
      headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        from: env.CONTACT_FROM || "QEF Statement Lookup <onboarding@resend.dev>",
        to: [env.CONTACT_TO],
        reply_to: email,
        subject: `QEF Lookup - message from ${oneLine(name)}`,
        text,
      }),
    });
  } catch {
    return json({ error: "The message could not be sent. Please try again later." }, 502);
  }
  if (!upstream.ok) {
    // The visitor needs to know it failed, not why; the owner can see the
    // reason in Resend's own log.
    return json({ error: "The message could not be sent. Please try again later." }, 502);
  }
  if (cache && gateKey) {
    ctx.waitUntil(cache.put(gateKey, new Response("1", {
      headers: { "Cache-Control": `max-age=${CONTACT_GAP_S}` } })));
  }
  return json({ ok: true });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/pdf") return servePdf(request, env, ctx, url);
    if (url.pathname === "/contact") return serveContact(request, env, ctx);
    if (url.pathname === "/contact/health") {
      return json({ ok: contactReady(env), limits: CONTACT_LIMITS });
    }
    if (url.pathname === "/pdf/health") {
      const hosts = await allowedHosts(env, request);
      return json({ ok: hosts.size > 0, hosts: hosts.size });
    }
    // Anything else that is not a file: let the asset server give its 404.
    return env.ASSETS.fetch(request);
  },
};
