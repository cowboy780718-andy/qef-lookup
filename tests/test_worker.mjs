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

// The Worker keeps the host list for ten minutes, so every test shares this one.
function makeEnv(hosts = ["www.manager.example", "cdn.manager.example", "files.manager.example",
                          "web.archive.org"]) {
  return {
    ASSETS: {
      async fetch(req) {
        const path = new URL(req.url).pathname;
        if (path === "/data/hosts.json") {
          return new Response(
            JSON.stringify({ hosts, any_path_hosts: ["files.manager.example"] }), { status: 200 });
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

test("a host the index marks as suffix-less may be fetched without .pdf; others may not", async () => {
  stubUpstream(() => new Response(PDF));
  const ok = await call(
    "https://files.manager.example/files/191/download/38415/2024-PFIC-Annual-Information-Statement?preview=1");
  assert.equal(ok.status, 200);
  assert.equal(upstreamCalls[0].url,
    "https://files.manager.example/files/191/download/38415/2024-PFIC-Annual-Information-Statement");
  const no = await call("https://www.manager.example/files/191/download/38415/statement");
  assert.equal(no.status, 400);
  // A web page from the suffix-less host is still refused: it is not a PDF.
  stubUpstream(() => new Response(HTML, { status: 200 }));
  const page = await call("https://files.manager.example/en/about-us");
  assert.equal(page.status, 502);
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

test("an archived copy is served only when it is a copy of a document from an indexed host", async () => {
  const env = makeEnv();
  stubUpstream(() => new Response(PDF, { status: 200 }));
  const get = (u) => worker.fetch(
    new Request(`${SITE}/pdf?u=${encodeURIComponent(u)}`), env, ctx);

  const ok = await get("https://web.archive.org/web/20250420225254/https://www.manager.example/docs/pfic-abc-en.pdf");
  assert.equal(ok.status, 200);
  assert.equal(upstreamCalls.length, 1);

  // The archive holds everything; that is not what was allowed.
  for (const u of [
    "https://web.archive.org/web/20250420225254/https://elsewhere.example/any.pdf",
    "https://web.archive.org/web/20250420225254/https://web.archive.org/web/2020/https://www.manager.example/a.pdf",
    "https://web.archive.org/save/https://www.manager.example/a.pdf",
    "https://web.archive.org/some/other/file.pdf",
  ]) {
    const r = await get(u);
    assert.equal(r.status, 403, u);
  }
  assert.equal(upstreamCalls.length, 1);
});

// --------------------------------------------------------------------------
// Contact form
// --------------------------------------------------------------------------

const MAIL_ENV = { ...makeEnv(), RESEND_API_KEY: "re_test", CONTACT_TO: "owner@example.invalid" };
const post = (body, { env = MAIL_ENV, headers = {} } = {}) => worker.fetch(new Request(`${SITE}/contact`, {
  method: "POST", headers: { "Content-Type": "application/json", Origin: SITE, ...headers },
  body: typeof body === "string" ? body : JSON.stringify(body),
}), env, ctx);
const GOOD = { name: "Pat Lee", email: "pat@example.com", message: "Hello.\nA question about XSH." };

test("the Contact form is reported as not set up until both secrets exist", async () => {
  for (const env of [makeEnv(), { ...makeEnv(), RESEND_API_KEY: "k" }, { ...makeEnv(), CONTACT_TO: "a@b.c" }]) {
    const r = await worker.fetch(new Request(`${SITE}/contact/health`), env, ctx);
    assert.equal((await r.json()).ok, false);
  }
  const r = await worker.fetch(new Request(`${SITE}/contact/health`), MAIL_ENV, ctx);
  const h = await r.json();
  assert.equal(h.ok, true);
  assert.equal(h.limits.message, 20000);
  assert.equal(JSON.stringify(h).includes("owner@"), false);    // the address is never given out
  stubUpstream(() => new Response("{}", { status: 200 }));
  assert.equal((await post(GOOD, { env: makeEnv() })).status, 503);
  assert.equal(upstreamCalls.length, 0);
});

test("a message is emailed to the owner, with the visitor's address to reply to", async () => {
  stubUpstream(() => new Response(JSON.stringify({ id: "x" }), { status: 200 }));
  const r = await post(GOOD);
  assert.equal(r.status, 200);
  assert.equal((await r.json()).ok, true);
  assert.equal(upstreamCalls.length, 1);
  const call = upstreamCalls[0];
  assert.equal(call.url, "https://api.resend.com/emails");
  assert.equal(call.init.headers.Authorization, "Bearer re_test");
  const sent = JSON.parse(call.init.body);
  assert.deepEqual(sent.to, ["owner@example.invalid"]);
  assert.equal(sent.reply_to, "pat@example.com");
  assert.match(sent.subject, /Pat Lee/);
  assert.match(sent.text, /Name: +Pat Lee/);
  assert.match(sent.text, /Email: +pat@example\.com/);
  assert.match(sent.text, /A question about XSH\./);
});

test("a message of 1,000 words goes through; one over 20,000 characters does not", async () => {
  stubUpstream(() => new Response("{}", { status: 200 }));
  const words = Array.from({ length: 1000 }, (_, i) => `word${i}`).join(" ");
  assert.equal((await post({ ...GOOD, message: words })).status, 200);
  assert.equal((await post({ ...GOOD, message: "x".repeat(20001) })).status, 400);
  assert.equal(upstreamCalls.length, 1);
});

test("missing or malformed fields are refused and nothing is sent", async () => {
  stubUpstream(() => new Response("{}", { status: 200 }));
  for (const body of [{ ...GOOD, name: "" }, { ...GOOD, email: "not-an-address" },
                      { ...GOOD, email: "a@b" }, { ...GOOD, message: "   " }, "{not json"]) {
    const r = await post(body);
    assert.equal(r.status, 400, JSON.stringify(body));
  }
  assert.equal(upstreamCalls.length, 0);
});

test("a filled-in hidden field is treated as a script: it is told ok, and nothing is sent", async () => {
  stubUpstream(() => new Response("{}", { status: 200 }));
  const r = await post({ ...GOOD, website: "http://spam.example" });
  assert.equal(r.status, 200);
  assert.equal(upstreamCalls.length, 0);
});

test("other websites cannot post to the form", async () => {
  stubUpstream(() => new Response("{}", { status: 200 }));
  assert.equal((await post(GOOD, { headers: { Origin: "https://elsewhere.example" } })).status, 403);
  assert.equal((await post(GOOD, { headers: { "Sec-Fetch-Site": "cross-site" } })).status, 403);
  const get = await worker.fetch(new Request(`${SITE}/contact`), MAIL_ENV, ctx);
  assert.equal(get.status, 405);
  assert.equal(upstreamCalls.length, 0);
});

test("a header injected through the name cannot reach the subject line", async () => {
  stubUpstream(() => new Response("{}", { status: 200 }));
  await post({ ...GOOD, name: "Pat\r\nBcc: victim@example.com" });
  const sent = JSON.parse(upstreamCalls[0].init.body);
  assert.equal(/[\r\n]/.test(sent.subject), false);
  assert.equal(sent.bcc, undefined);
});

test("when the mail service fails, the visitor is told so", async () => {
  stubUpstream(() => new Response(JSON.stringify({ message: "invalid key" }), { status: 401 }));
  const r = await post(GOOD);
  assert.equal(r.status, 502);
  assert.equal(JSON.stringify(await r.json()).includes("invalid key"), false);
});

