// --- Hash router ---
const navLinks = document.querySelectorAll(".nav-link");
const tabContents = document.querySelectorAll(".tab-content");

function navigateTo(route) {
  if (!route) route = "feed";
  tabContents.forEach(c => c.classList.remove("active"));
  navLinks.forEach(l => l.classList.remove("active"));
  const section = document.getElementById(`tab-${route}`);
  if (section) section.classList.add("active");
  const link = document.querySelector(`.nav-link[data-route="${route}"]`);
  if (link) link.classList.add("active");
  if (route === "feed") loadReleases();
  if (route === "artists") loadArtists();
}

function getRoute() {
  const hash = location.hash.replace("#/", "").split("?")[0];
  return ["feed", "artists"].includes(hash) ? hash : "feed";
}

window.addEventListener("hashchange", () => navigateTo(getRoute()));
// NOTE: the initial navigateTo(getRoute()) call lives in init.js, which is
// loaded last. It used to run here, but loadReleases() reads module state
// (e.g. _releaseRequestSeq, filterUnseen) declared in feed.js, so calling it
// this early threw a TDZ ReferenceError on every page load.

// --- HTML helpers ---
// esc() escapes any value for interpolation into HTML (text or attribute
// context). It is a pure string function (no placeholder element) and unlike
// the old version escapes quotes too, so values dropped into attributes can't
// break out of them.
function esc(value) {
  if (value == null) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Marks a string as trusted HTML so `html` won't re-escape it. It stringifies
// like a plain string, so a raw()/html() result can still be assigned to
// innerHTML or dropped into an ordinary template literal. A plain constructor
// (not a class) so it is hoisted and can be referenced from top-level code
// that runs earlier in the file.
function SafeHtml(markup) { this.markup = String(markup); }
SafeHtml.prototype.toString = function () { return this.markup; };
function raw(markup) { return new SafeHtml(markup); }

function _renderValue(value) {
  if (value instanceof SafeHtml) return value.markup;
  if (Array.isArray(value)) return value.map(_renderValue).join("");
  return esc(value);
}

// Tagged template that escapes every interpolated value by default — the safe
// way to build markup from data. Splice in already-safe fragments (built with
// `html` or wrapped in raw()) and they pass through verbatim.
function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) {
    out += _renderValue(values[i]);
    out += strings[i + 1];
  }
  return raw(out);
}

// --- HTTP helpers ---
// Thin wrapper over fetch: JSON in/out, and HTTP errors become exceptions
// (pass allowError to inspect a non-2xx response instead of throwing).
async function apiRaw(path, { method = "GET", json, body, headers, allowError = false } = {}) {
  const init = { method, headers: { ...headers } };
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (body !== undefined) {
    init.body = body;
  }
  const resp = await fetch(path, init);
  if (!resp.ok && !allowError) {
    const err = new Error(`${method} ${path} → ${resp.status} ${resp.statusText}`);
    err.status = resp.status;
    err.response = resp;
    throw err;
  }
  return resp;
}

async function api(path, opts) {
  const resp = await apiRaw(path, opts);
  const text = await resp.text();
  return text ? JSON.parse(text) : null;
}

// --- Source icon helper ---
function sourceIcon(source, size = 16) {
  const icons = {
    musicbrainz: '/static/assets/musicbrainz.svg',
    itunes: '/static/assets/apple-music.svg',
    soundcloud: '/static/assets/soundcloud.svg'
  };
  const src = icons[source];
  if (!src) return raw('');
  return raw(`<img src="${src}" alt="" class="source-icon-inline" style="width:${size}px;height:${size}px;">`);
}

