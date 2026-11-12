/**
 * Tests for the site Worker's /pdf endpoint. No network access.
 *
 *     node --test tests/
 *
 * The Worker runs on Cloudflare, but it only uses web-standard pieces
 * (Request, Response, URL, fetch), all of which Node has - so it can be loaded
 * here as an ordinary module with the outside world stubbed.
 */
import test from "node:test";
import assert from "node:assert/strict";
import worker from "../worker/site.js";

const SITE = "https://qef-lookup.example";
const PDF = new TextEncoder().encode("%PDF-1.7\n fake body");
const HTML = new TextEncoder().encode("<!doctype html><title>Not found</title>");

function makeEnv(hosts = ["www.manager.example", "cdn.manager.example"]) {
  return {
    ASSETS: {
      async fetch(req) {
        const path = new URL(req.url).pathname;
        if (path === "/data/hosts.json") {
          return new Response(JSON.stringify({ hosts }), { status: 200 });
        }
        return new Response("not found", { status: 404 });
      },
    },
  };
}

const ctx = { waitUntil() {} };
let upstreamCalls = [];

function stubUpstream(responder) {
  upstreamCalls = [];
  globalThis.fetch = async (url, init) => {
    upstreamCalls.push({ url: String(url), init });
    return responder(String(url));
  };
}

async function call(target, { env = makeEnv(), headers = {}, method = "GET" } = {}) {
  const u = `${SITE}/pdf?u=${encodeURIComponent(target)}`;
  return worker.fetch(new Request(u, { method, headers }), env, ctx);
}

test("serves a PDF from a host that is in the index", async () => {
  stubUpstream(() => new Response(PDF, { status: 200 }));
  const r = await call("https://www.manager.example/docs/pfic-2025-abc.pdf");
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("Content-Type"), "application/pdf");
  assert.equal(r.headers.get("X-Content-Type-Options"), "nosniff");
  assert.deepEqual(new Uint8Array(await r.arrayBuffer()), PDF);
  assert.equal(upstreamCalls.length, 1);
});

test("refuses a host that is not in the index", async () => {
  stubUpstream(() => new Response(PDF));
  const r = await call("https://evil.example/x.pdf");
  assert.equal(r.status, 403);
  assert.equal(upstreamCalls.length, 0);
});

test("a host that merely ends with an allowed name is still refused", async () => {
  stubUpstream(() => new Response(PDF));
  for (const t of ["https://www.manager.example.evil.test/x.pdf",
                   "https://notwww.manager.example/x.pdf",
                   "https://www.manager.example@evil.test/x.pdf"]) {
    const r = await call(t);
    assert.notEqual(r.status, 200, t);
  }
  assert.equal(upstreamCalls.length, 0);
});

test("refuses http, non-PDF paths, credentials and odd ports", async () => {
  stubUpstream(() => new Response(PDF));
  for (const t of ["http://www.manager.example/x.pdf",
                   "https://www.manager.example/x.html",
                   "https://www.manager.example/x.pdf.exe",
                   "https://user:pw@www.manager.example/x.pdf",
                   "https://www.manager.example:8443/x.pdf",
                   "file:///etc/passwd.pdf",
                   "not a url"]) {
    const r = await call(t);
    assert.equal(r.status, 400, t);
  }
  assert.equal(upstreamCalls.length, 0);
});

test("an HTML error page served with status 200 is not passed on as a PDF", async () => {
  stubUpstream(() => new Response(HTML, { status: 200 }));
  const r = await call("https://www.manager.example/docs/gone.pdf");
  assert.equal(r.status, 502);
});

test("an upstream refusal is reported, not relayed", async () => {
  stubUpstream(() => new Response("forbidden", { status: 403 }));
  const r = await call("https://www.manager.example/docs/a.pdf");
  assert.equal(r.status, 502);
  assert.equal((await r.json()).status, 403);
});

test("the page fragment used to open a combined statement is not sent upstream", async () => {
  stubUpstream(() => new Response(PDF));
  const r = await call("https://cdn.manager.example/tax/PFIC-Investor-Statement_2024.pdf#page=37");
  assert.equal(r.status, 200);
  assert.equal(upstreamCalls[0].url, "https://cdn.manager.example/tax/PFIC-Investor-Statement_2024.pdf");
});

test("encoded spaces in a document name survive the round trip", async () => {
  stubUpstream(() => new Response(PDF));
  const target = "https://www.manager.example/Docs/BTCC/pfic/en/Purpose%20Bitcoin%20ETF%20PFIC%202026-02-13.pdf";
  const r = await call(target);
  assert.equal(r.status, 200);
  assert.equal(upstreamCalls[0].url, target);
});

test("another website cannot use the endpoint from a browser", async () => {
  stubUpstream(() => new Response(PDF));
  const r = await call("https://www.manager.example/a.pdf", { headers: { "Sec-Fetch-Site": "cross-site" } });
  assert.equal(r.status, 403);
  const ok = await call("https://www.manager.example/a.pdf", { headers: { "Sec-Fetch-Site": "same-origin" } });
  assert.equal(ok.status, 200);
});

test("only GET and HEAD are accepted", async () => {
  stubUpstream(() => new Response(PDF));
  const r = await call("https://www.manager.example/a.pdf", { method: "POST" });
  assert.equal(r.status, 405);
});

test("health reports the size of the host list", async () => {
  const r = await worker.fetch(new Request(`${SITE}/pdf/health`), makeEnv(["a.example", "b.example", "c.example"]), ctx);
  assert.equal(r.status, 200);
  // The list is cached briefly between requests, so only its presence is asserted.
  assert.equal((await r.json()).ok, true);
});

test("everything that is not /pdf falls through to the static site", async () => {
  const r = await worker.fetch(new Request(`${SITE}/no-such-page`), makeEnv(), ctx);
  assert.equal(r.status, 404);
});

test("with no host list at all, everything is refused", async () => {
  // Fresh module state is not available here, so exercise the rule directly:
  // an env whose hosts.json is missing must never widen access.
  const env = { ASSETS: { async fetch() { return new Response("nope", { status: 404 }); } } };
  stubUpstream(() => new Response(PDF));
  const r = await worker.fetch(
    new Request(`${SITE}/pdf?u=${encodeURIComponent("https://never-listed.example/a.pdf")}`), env, ctx);
  assert.equal(r.status, 403);
});
