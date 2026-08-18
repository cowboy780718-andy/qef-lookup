/**
 * Site configuration.
 *
 * PDF_PROXY  Cloudflare Worker that re-serves fund company PDFs with CORS
 *            headers, so the page can bundle them into a ZIP. Without it the
 *            site still works fully - "Open in tabs" and the per-statement
 *            download links go straight to the manager's site - but ZIP
 *            bundling is unavailable, because browsers block cross-origin
 *            reads of those PDFs.
 *
 *            Deploy worker/worker.js, then paste its URL here, e.g.
 *              "https://qef-pdf-proxy.<your-subdomain>.workers.dev"
 */
window.QEF_CONFIG = {
  PDF_PROXY: "",
  INDEX_URL: "data/index.json",
  MAX_PARALLEL_DOWNLOADS: 4,
};
