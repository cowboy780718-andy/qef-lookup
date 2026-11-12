/**
 * Site configuration.
 *
 * PDF_PROXY  The OLD stand-alone download helper, kept only as a fallback.
 *            Statements are normally fetched through the site's own /pdf
 *            address (worker/site.js), which needs no setting here. This one
 *            is used only when /pdf is not there - for example when the page
 *            is opened from a plain local web server during development.
 */
window.QEF_CONFIG = {
  PDF_PROXY: "https://qef-pdf-proxy.cowboy780718.workers.dev",
  INDEX_URL: "data/index.json",
  MAX_PARALLEL_DOWNLOADS: 4,
};
