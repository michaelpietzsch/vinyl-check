"use strict";
// Translations for the web interface.
//
// The server delivers the catalog of the chosen language before this script runs (i18n/catalog.js sets
// window.VC_I18N), so T() works everywhere, also at the top level of app.js. Catalogs live in
// static/locales/<lang>/<namespace>.json; a key is "<namespace>.<key>". Templates use named placeholders,
// "{n}" or with fixed decimals "{db:.1f}"; plural forms are objects {"one": …, "other": …} chosen by the
// parameter n. Missing keys fall back to English, then to the key itself. See docs/translating.md.

const I18N = (() => {
  const data = window.VC_I18N || { lang: "en", languages: { en: "English" }, catalog: {}, fallback: {} };
  const lang = data.lang;
  const plural = new Intl.PluralRules(lang);
  const missing = new Set();
  const numFmt = new Map();

  // Fixed number of decimals in the reader's convention; "–" for missing values.
  function num(v, d = 1) {
    if (v === null || v === undefined || v === "" || Number.isNaN(Number(v))) return "–";
    if (!numFmt.has(d)) numFmt.set(d, new Intl.NumberFormat(lang, { minimumFractionDigits: d, maximumFractionDigits: d, useGrouping: false }));
    return numFmt.get(d).format(Number(v));
  }

  function lookup(key) {
    if (key in data.catalog) return data.catalog[key];
    if (key in data.fallback) return data.fallback[key];
    if (!missing.has(key)) { missing.add(key); console.warn("i18n: missing key", key); }
    return key;
  }

  // T("records.count", { n: 3 }) → "3 records"
  function t(key, params = {}) {
    let tpl = lookup(key);
    if (tpl && typeof tpl === "object") tpl = tpl[plural.select(Number(params.n ?? 0))] ?? tpl.other ?? key;
    return String(tpl).replace(/\{(\w+)(?::\.(\d)f)?\}/g, (m, name, d) => {
      if (!(name in params)) return m;
      const v = params[name];
      if (d !== undefined) return num(v, +d);
      return typeof v === "number" ? num(v, Number.isInteger(v) ? 0 : 1) : String(v ?? "");
    });
  }

  const has = (key) => key in data.catalog || key in data.fallback;

  // Date and time of a Unix timestamp (seconds).
  const date = (ts, opts = { dateStyle: "short", timeStyle: "short" }) => new Date(ts * 1000).toLocaleString(lang, opts);

  // Static markup: data-i18n (text), data-i18n-html (markup from the catalog), data-i18n-title,
  // data-i18n-placeholder, data-i18n-aria-label.
  function apply(root = document) {
    root.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
    root.querySelectorAll("[data-i18n-html]").forEach((el) => { el.innerHTML = t(el.dataset.i18nHtml); });
    for (const attr of ["title", "placeholder", "aria-label"]) {
      const ds = "i18n" + attr.replace(/(^|-)(\w)/g, (m, s, c) => c.toUpperCase());
      root.querySelectorAll(`[data-i18n-${attr}]`).forEach((el) => el.setAttribute(attr, t(el.dataset[ds])));
    }
  }

  // Switching the language stores it in a cookie (the server reads it for API texts and PDF reports) and reloads.
  function setLang(code) {
    document.cookie = `vc_lang=${encodeURIComponent(code)}; path=/; max-age=31536000; SameSite=Lax`;
    location.reload();
  }

  document.documentElement.lang = lang;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => apply());
  else apply();

  return { lang, languages: data.languages, t, has, num, date, apply, setLang };
})();
// Global shorthand. Not "t": app.js uses t for times everywhere.
const T = I18N.t;
