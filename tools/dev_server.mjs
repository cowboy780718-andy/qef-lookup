/**
 * Local stand-in for Cloudflare, for trying the site on this computer.
 *
 *     node tools/dev_server.mjs            # then open http://localhost:8099
 *
 * It behaves the way the live site does: files under ./web are served as they
 * are, and any address that is not a file is handed to worker/site.js - so the
 * ZIP download's /pdf address works locally too, running the very same code
 * that runs in production. Nothing is installed and nothing is deployed.
 */
import http from "node:http";
import { readFile, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import worker from "../worker/site.js";

const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "web");
const PORT = Number(process.env.PORT || 8099);
const TYPES = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
  ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
};

/** The file a URL path refers to, or null. Never outside ./web. */
async function assetFor(pathname) {
  let rel = decodeURIComponent(pathname);
  if (rel.endsWith("/")) rel += "index.html";
  const full = path.resolve(WEB, "." + rel);
  if (full !== WEB && !full.startsWith(WEB + path.sep)) return null;
  try {
    if ((await stat(full)).isFile()) return full;
  } catch { /* not a file */ }
  return null;
}

async function assetResponse(request) {
  const file = await assetFor(new URL(request.url).pathname);
  if (!file) return new Response("Not found", { status: 404 });
  return new Response(await readFile(file), {
    status: 200,
    headers: {
      "Content-Type": TYPES[path.extname(file).toLowerCase()] || "application/octet-stream",
      "Cache-Control": "no-store",
    },
  });
}

const env = { ASSETS: { fetch: assetResponse } };
const ctx = { waitUntil(p) { Promise.resolve(p).catch(() => {}); } };

http.createServer(async (req, res) => {
  try {
    const url = `http://${req.headers.host}${req.url}`;
    const headers = new Headers();
    for (const [k, v] of Object.entries(req.headers)) {
      if (typeof v === "string") headers.set(k, v);
    }
    const request = new Request(url, { method: req.method, headers });
    // Same order as Cloudflare: a matching file wins, the Worker gets the rest.
    const isFile = await assetFor(new URL(url).pathname);
    const response = isFile ? await assetResponse(request) : await worker.fetch(request, env, ctx);
    res.writeHead(response.status, Object.fromEntries(response.headers));
    res.end(Buffer.from(await response.arrayBuffer()));
  } catch (e) {
    res.writeHead(500, { "Content-Type": "text/plain" });
    res.end(String(e && e.stack || e));
  }
}).listen(PORT, "127.0.0.1", () => {
  console.log(`QEF Lookup dev server: http://localhost:${PORT}`);
});
