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
// NOTE: the initial navigateTo(getRoute()) call lives at the bottom of the file
// (see "--- Init ---"). It used to run here, but loadReleases() reads module
// state (e.g. _releaseRequestSeq, filterUnseen) declared further down, so
// calling it this early threw a TDZ ReferenceError on every page load.

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

// --- Feed Tab ---
let activeArtistFilterId = "";   // selected artist ID (empty = all)
let activeTypeFilters = ["Album", "EP", "Single", "Other"];  // multi-select release types (empty = all)
const filterUnseen = document.getElementById("filter-unseen");
const releaseList = document.getElementById("release-list");
const unseenBadge = document.getElementById("unseen-badge");
const artistDropdownMenu = document.getElementById("artist-dropdown-menu");
const artistDropdownList = document.getElementById("artist-dropdown-list");
const artistFilterSearch = document.getElementById("artist-filter-search");
const filterArtistBtn = document.getElementById("filter-artist-btn");

filterUnseen.addEventListener("change", () => { updateResetFiltersBtn(); loadReleases(); });

// --- Artist Dropdown Logic ---
// Synchronous cache of artists for button text updates
let _artistsCache = [];

function _getArtistName(id) {
  const a = _artistsCache.find(x => String(x.id) === String(id));
  return a ? a.name : null;
}

function _updateCheckBtn() {
  if (!scanRunning) {
    if (activeArtistFilterId) {
      const name = _getArtistName(activeArtistFilterId);
      checkBtn.textContent = name ? `Check ${name}` : "Check All Artists";
    } else {
      checkBtn.textContent = "Check All Artists";
    }
  }
}

// Refresh the artists cache
async function _refreshArtistsCache() {
  try {
    _artistsCache = await api("/api/artists");
    _updateCheckBtn();
  } catch {
    // silent fail
  }
}

// Refresh cache periodically and after mutations (skip when tab hidden)
_refreshArtistsCache();
setInterval(() => { if (!document.hidden) _refreshArtistsCache(); }, 15000);

// --- Delegated event handling ---
// Generated markup used to embed JavaScript — and sometimes whole JSON
// snapshots — inside inline onclick="..." attributes. That is fragile (a quote
// or newline in a title/name could break out of the attribute) and pushes
// strings through the HTML parser. Instead, dynamic controls now declare what
// they do with data-action + data-* params, and a single document-level
// listener dispatches to the matching handler. Row objects that don't fit in
// attributes (search results) live in _rowData and are referenced by a key.
function dataAttrs(attrs) {
  let out = "";
  for (const key of Object.keys(attrs)) {
    const value = attrs[key];
    if (value === undefined || value === null) continue;
    out += ` data-${key}="${esc(value)}"`;
  }
  return out;
}

const _rowData = new Map();
let _rowDataSeq = 0;
function registerRow(obj) {
  const key = `r${++_rowDataSeq}`;
  _rowData.set(key, obj);
  return key;
}

const ACTIONS = {
  "select-artist": el => selectArtist(el.dataset.id),
  "select-tracked-artist": el => selectTrackedArtist(el.dataset.id),
  "goto-artists": () => { location.hash = "#/artists"; },
  "show-all-releases": () => {
    filterUnseen.checked = false;
    updateResetFiltersBtn();
    loadReleases();
  },
  "reset-filters": () => resetFilters(),
  "toggle-tracklist": el => toggleTracklist(el),
  "toggle-streaming": el => toggleStreaming(el),
  "hide-release": el => hideRelease(Number(el.dataset.id), el),
  "unhide-release": el => unhideRelease(Number(el.dataset.id), el),
  "mark-seen": el => markSeen(Number(el.dataset.id), el),
  "retry-tracklist": el => {
    const item = el.closest(".release-item");
    if (!item) return;
    delete tracklistCache[item.dataset.mbid];
    const section = item.querySelector(".tracklist-section");
    if (section) {
      section.classList.remove("expanded");
      setCardExpanded(item, "track", false);
    }
    toggleTracklist(item.querySelector(".release-card"));
  },
  "copy-tracklist": el => copyTracklist(el.dataset.mbid, el),
  "toggle-mb-links": el => toggleMbArtistLinks(el.dataset.mbid, el),
  "check-artist": el => checkSingleArtist(Number(el.dataset.id)),
  "edit-disambiguation": el => editDisambiguation(Number(el.dataset.id), el),
  "confirm-remove": el => confirmRemoveArtist(Number(el.dataset.id), el),
  "confirm-unlink": el => confirmUnlinkArtist(Number(el.dataset.id), el.dataset.source, el),
  "do-remove-artist": el => doRemoveArtist(Number(el.dataset.id)),
  "do-unlink-artist": el => doUnlinkArtist(Number(el.dataset.id), el.dataset.source),
  "load-artists": () => loadArtists(),
  "save-disambiguation": el => saveDisambiguation(Number(el.dataset.id)),
  "toggle-artist-menu": el => toggleArtistMenu(el),
  "open-link-source": el => openLinkSourceModal(Number(el.dataset.id), el.dataset.name, el.dataset.source),
  "add-artist": el => addArtist(_rowData.get(el.dataset.key), el),
  "link-source": el => linkSource(_rowData.get(el.dataset.key), el),
  "jump-to-artist": el => jumpToArtistFromSummary(el.dataset.artist),
  "remove-dedup-rule": el => removeDedupRule(Number(el.dataset.index)),
  "remove-explicit-map": el => removeExplicitMapRule(Number(el.dataset.index)),
  "search-link-source": () => searchLinkSource(),

  // Static UI from templates/index.html
  "close-side-panels": () => closeSidePanels(),
  "clear-logs": () => clearLogs(),
  "toggle-logs-panel": () => toggleLogsPanel(),
  "set-log-level": el => setLogLevel(el),
  "toggle-settings-panel": () => toggleSettingsPanel(),
  "run-dedup": () => runDedup(),
  "add-dedup-rule": () => addDedupRule(),
  "add-explicit-map-rule": () => addExplicitMapRule(),
  "toggle-artist-dropdown": () => toggleArtistDropdown(),
  "toggle-type": el => toggleType(el),
  "close-check-popup": () => closeCheckPopup(),
  "toggle-pause-check": () => togglePauseCheck(),
  "retry-failed": () => retryFailed(),
  "view-new-results": () => viewNewResults(),
  "toggle-search-source-menu": () => toggleSearchSourceMenu(),
  "toggle-tracked-dropdown": () => toggleTrackedDropdown(),
  "export-artists": () => exportArtists(),
  "close-link-source-modal": () => closeLinkSourceModal(),
};

// Change/input handlers for static controls (checkboxes, text inputs).
const CHANGE_ACTIONS = {
  "save-settings": () => saveSettings(true),
  "load-releases": () => loadReleases(),
  "update-search-sources": () => updateSearchSources(),
};
const INPUT_ACTIONS = {
  "load-logs": () => loadLogs(),
  "filter-artist-list": () => filterArtistList(),
  "filter-tracked-list": () => filterTrackedList(),
};

document.addEventListener("click", e => {
  // Plain navigation links (artist / source / view URLs) keep their default
  // behaviour and must not also trigger a surrounding card action.
  const link = e.target.closest("a[href]");
  if (link && !link.hasAttribute("data-action")) return;
  // Only the innermost action fires, so a button inside a card never also
  // toggles the card.
  const el = e.target.closest("[data-action]");
  if (!el) return;
  const action = ACTIONS[el.dataset.action];
  if (action) action(el, e);
});

// Keyboard activation for the card body (role="button"). Real <button>
// elements already raise a click on Enter/Space, so they are excluded here to
// avoid toggling twice.
document.addEventListener("keydown", e => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const el = e.target.closest('[data-action][role="button"]');
  if (!el) return;
  e.preventDefault();
  const action = ACTIONS[el.dataset.action];
  if (action) action(el, e);
});

document.addEventListener("change", e => {
  const el = e.target.closest("[data-change-action]");
  if (!el) return;
  const action = CHANGE_ACTIONS[el.dataset.changeAction];
  if (action) action(el, e);
});

document.addEventListener("input", e => {
  const el = e.target.closest("[data-input-action]");
  if (!el) return;
  const action = INPUT_ACTIONS[el.dataset.inputAction];
  if (action) action(el, e);
});

// <img> error fallbacks. `error` does not bubble, so this runs in the capture
// phase. data-cover-fallback carries the initials to swap in; data-hide-on-error
// just hides the broken image. Replaces the old inline onerror="..." handlers.
document.addEventListener("error", e => {
  const img = e.target;
  if (!img || img.tagName !== "IMG") return;
  if (img.hasAttribute("data-hide-on-error")) {
    img.style.display = "none";
    return;
  }
  const initials = img.getAttribute("data-cover-fallback");
  if (initials != null) {
    const fallback = document.createElement("div");
    fallback.className = "cover-fallback";
    fallback.setAttribute("aria-hidden", "true");
    fallback.textContent = initials;
    img.replaceWith(fallback);
  }
}, true);

// --- Toast helper ---
function showToast(message, actionLabel, actionFn, timeout = 5000) {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const el = document.createElement("div");
  el.className = "toast";
  const msg = document.createElement("span");
  msg.textContent = message;
  el.appendChild(msg);
  if (actionLabel && actionFn) {
    const btn = document.createElement("button");
    btn.textContent = actionLabel;
    btn.onclick = () => { actionFn(); el.remove(); };
    el.appendChild(btn);
  }
  container.appendChild(el);
  setTimeout(() => el.remove(), timeout);
}

function toggleArtistDropdown() {
  const hidden = artistDropdownMenu.classList.contains("hidden");
  if (hidden) {
    renderArtistDropdown();
    artistDropdownMenu.classList.remove("hidden");
    filterArtistBtn.setAttribute("aria-expanded", "true");
    artistFilterSearch.value = "";
    artistFilterSearch.focus();
    filterArtistBtn.style.borderColor = "#8c8c8c";
  } else {
    closeArtistDropdown();
  }
}

function closeArtistDropdown() {
  artistDropdownMenu.classList.add("hidden");
  filterArtistBtn.setAttribute("aria-expanded", "false");
  filterArtistBtn.style.borderColor = "";
  _dropdownFocusIdx = -1;
}

// Close dropdown when clicking outside / Esc
document.addEventListener("click", e => {
  const wrapper = document.getElementById("artist-filter-wrapper");
  if (wrapper && !wrapper.contains(e.target)) closeArtistDropdown();
  const trackedWrapper = document.getElementById("tracked-filter-wrapper");
  if (trackedWrapper && !trackedWrapper.contains(e.target)) closeTrackedDropdown();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    if (!artistDropdownMenu.classList.contains("hidden")) closeArtistDropdown();
    if (typeof trackedDropdownMenu !== "undefined" && trackedDropdownMenu && !trackedDropdownMenu.classList.contains("hidden")) closeTrackedDropdown();
    if (typeof linkSourceModal !== "undefined" && !linkSourceModal.classList.contains("hidden")) closeLinkSourceModal();
    if (!checkPopup.classList.contains("hidden") && !scanRunning) closeCheckPopup();
    if (!logsPanel.classList.contains("hidden") || !settingsPanel.classList.contains("hidden")) closeSidePanels();
    const srcMenu = document.getElementById("search-source-menu");
    if (srcMenu && !srcMenu.classList.contains("hidden")) closeSearchSourceMenu();
  }
  if (!artistDropdownMenu.classList.contains("hidden") && ["ArrowDown", "ArrowUp", "Enter"].includes(e.key)) {
    handleDropdownKey(e);
  }
  if (typeof trackedDropdownMenu !== "undefined" && trackedDropdownMenu && !trackedDropdownMenu.classList.contains("hidden") && ["ArrowDown", "ArrowUp", "Enter"].includes(e.key)) {
    handleTrackedDropdownKey(e);
  }
});

let _dropdownFocusIdx = -1;
let _dropdownItems = []; // {id, name}
let _filterDebounce = null;

function renderArtistDropdown() {
  // Use local cache (already refreshed periodically) — no fetch per keystroke
  const q = artistFilterSearch.value.trim().toLowerCase();
  const data = _artistsCache || [];
  const filtered = q ? data.filter(a => a.name.toLowerCase().includes(q)) : data;

  _dropdownItems = [{ id: "", name: "All Artists" }, ...filtered.map(a => ({ id: String(a.id), name: a.name }))];
  _dropdownFocusIdx = _dropdownItems.findIndex(x => x.id === (activeArtistFilterId || ""));
  if (_dropdownFocusIdx < 0) _dropdownFocusIdx = 0;

  const countEl = document.getElementById("artist-dropdown-count");
  if (countEl) countEl.textContent = filtered.length ? `${filtered.length} of ${data.length}` : (data.length ? "No matches" : "No artists tracked");

  let html = `<div class="dropdown-item ${!activeArtistFilterId ? 'active' : ''}" role="option" data-idx="0"${dataAttrs({ action: "select-artist", id: "" })}><em>All Artists</em></div>`;
  filtered.forEach((a, i) => {
    const activeClass = String(a.id) === String(activeArtistFilterId) ? " active" : "";
    html += `<div class="dropdown-item${activeClass}" role="option" data-idx="${i + 1}"${dataAttrs({ action: "select-artist", id: a.id })} title="${esc(a.name)}">${esc(a.name)}</div>`;
  });
  artistDropdownList.innerHTML = html || '<p class="status-msg">No artists tracked yet.</p>';
}

function handleDropdownKey(e) {
  e.preventDefault();
  if (e.key === "ArrowDown") _dropdownFocusIdx = Math.min(_dropdownFocusIdx + 1, _dropdownItems.length - 1);
  if (e.key === "ArrowUp") _dropdownFocusIdx = Math.max(_dropdownFocusIdx - 1, 0);
  if (e.key === "Enter" && _dropdownItems[_dropdownFocusIdx]) {
    selectArtist(_dropdownItems[_dropdownFocusIdx].id);
    return;
  }
  artistDropdownList.querySelectorAll(".dropdown-item").forEach(el => {
    el.classList.toggle("active", Number(el.dataset.idx) === _dropdownFocusIdx);
    if (Number(el.dataset.idx) === _dropdownFocusIdx) el.scrollIntoView({ block: "nearest" });
  });
}

function filterArtistList() {
  clearTimeout(_filterDebounce);
  _filterDebounce = setTimeout(renderArtistDropdown, 150);
}

function selectArtist(id) {
  activeArtistFilterId = id ? String(id) : "";

  if (!activeArtistFilterId) {
    filterArtistBtn.textContent = "All Artists";
  } else {
    const name = _getArtistName(activeArtistFilterId);
    filterArtistBtn.textContent = name || "All Artists";
    filterArtistBtn.title = name || "";
  }
  _updateCheckBtn();
  updateResetFiltersBtn();
  closeArtistDropdown();
  loadReleases();
}

// --- Tracked Artists Dropdown Logic (Artists tab; filters the artist list) ---
let activeTrackedFilterId = "";   // selected tracked artist ID (empty = all)
let _trackedArtists = [];         // full tracked list cache for this tab
let _trackedFocusIdx = -1;
let _trackedItems = [];           // {id, name}
let _trackedDebounce = null;
const trackedDropdownMenu = document.getElementById("tracked-dropdown-menu");
const trackedDropdownList = document.getElementById("tracked-dropdown-list");
const trackedFilterSearch = document.getElementById("tracked-filter-search");
const filterTrackedBtn = document.getElementById("filter-tracked-btn");

function _trackedAllLabel() {
  return `All tracked artists (${_trackedArtists.length})`;
}

function _updateTrackedBtn() {
  if (!filterTrackedBtn) return;
  if (!activeTrackedFilterId) {
    filterTrackedBtn.textContent = _trackedAllLabel();
    filterTrackedBtn.title = "";
  } else {
    const a = _trackedArtists.find(x => String(x.id) === String(activeTrackedFilterId));
    filterTrackedBtn.textContent = a ? a.name : _trackedAllLabel();
    filterTrackedBtn.title = a ? a.name : "";
  }
}

function toggleTrackedDropdown() {
  const hidden = trackedDropdownMenu.classList.contains("hidden");
  if (hidden) {
    renderTrackedDropdown();
    trackedDropdownMenu.classList.remove("hidden");
    filterTrackedBtn.setAttribute("aria-expanded", "true");
    trackedFilterSearch.value = "";
    trackedFilterSearch.focus();
    filterTrackedBtn.style.borderColor = "#8c8c8c";
  } else {
    closeTrackedDropdown();
  }
}

function closeTrackedDropdown() {
  if (!trackedDropdownMenu) return;
  trackedDropdownMenu.classList.add("hidden");
  if (filterTrackedBtn) {
    filterTrackedBtn.setAttribute("aria-expanded", "false");
    filterTrackedBtn.style.borderColor = "";
  }
  _trackedFocusIdx = -1;
}

function renderTrackedDropdown() {
  const q = trackedFilterSearch.value.trim().toLowerCase();
  const data = _trackedArtists || [];
  const filtered = q ? data.filter(a => a.name.toLowerCase().includes(q)) : data;

  _trackedItems = [{ id: "", name: _trackedAllLabel() }, ...filtered.map(a => ({ id: String(a.id), name: a.name }))];
  _trackedFocusIdx = _trackedItems.findIndex(x => x.id === (activeTrackedFilterId || ""));
  if (_trackedFocusIdx < 0) _trackedFocusIdx = 0;

  const countEl = document.getElementById("tracked-dropdown-count");
  if (countEl) countEl.textContent = filtered.length ? `${filtered.length} of ${data.length}` : (data.length ? "No matches" : "No artists tracked");

  let html = `<div class="dropdown-item ${!activeTrackedFilterId ? 'active' : ''}" role="option" data-idx="0"${dataAttrs({ action: "select-tracked-artist", id: "" })}><em>${esc(_trackedAllLabel())}</em></div>`;
  filtered.forEach((a, i) => {
    const activeClass = String(a.id) === String(activeTrackedFilterId) ? " active" : "";
    html += `<div class="dropdown-item${activeClass}" role="option" data-idx="${i + 1}"${dataAttrs({ action: "select-tracked-artist", id: a.id })} title="${esc(a.name)}">${esc(a.name)}</div>`;
  });
  trackedDropdownList.innerHTML = html || '<p class="status-msg">No artists tracked yet.</p>';
}

function handleTrackedDropdownKey(e) {
  e.preventDefault();
  if (e.key === "ArrowDown") _trackedFocusIdx = Math.min(_trackedFocusIdx + 1, _trackedItems.length - 1);
  if (e.key === "ArrowUp") _trackedFocusIdx = Math.max(_trackedFocusIdx - 1, 0);
  if (e.key === "Enter" && _trackedItems[_trackedFocusIdx]) {
    selectTrackedArtist(_trackedItems[_trackedFocusIdx].id);
    return;
  }
  trackedDropdownList.querySelectorAll(".dropdown-item").forEach(el => {
    el.classList.toggle("active", Number(el.dataset.idx) === _trackedFocusIdx);
    if (Number(el.dataset.idx) === _trackedFocusIdx) el.scrollIntoView({ block: "nearest" });
  });
}

function filterTrackedList() {
  clearTimeout(_trackedDebounce);
  _trackedDebounce = setTimeout(renderTrackedDropdown, 150);
}

function selectTrackedArtist(id) {
  activeTrackedFilterId = id ? String(id) : "";
  _updateTrackedBtn();
  closeTrackedDropdown();
  renderTrackedArtists();
}

function renderTrackedArtists() {
  const visible = activeTrackedFilterId
    ? _trackedArtists.filter(a => String(a.id) === String(activeTrackedFilterId))
    : _trackedArtists;
  if (!visible.length) {
    artistListEl.innerHTML = activeTrackedFilterId
      ? '<div class="empty-wrap"><h3>Artist not found</h3><p>The selected artist is no longer tracked.</p></div>'
      : '<div class="empty-wrap"><h3>No artists tracked yet</h3><p>Search above or paste a MusicBrainz / Apple Music / SoundCloud link.</p></div>';
    return;
  }
  artistListEl.innerHTML = visible.map(renderTrackedArtistItem).join("");
}

// --- Tracked-artist MB links viewer ---
// On-demand panel showing the artist's MusicBrainz url-rels (streaming +
// confirmed platform links). Same backend endpoint as the search badges;
// results cached per MBID for the session.
const _mbArtistLinksCache = {};

async function toggleMbArtistLinks(mbid, btn) {
  const item = btn ? btn.closest('.artist-item') : null;
  if (!item) return;
  const next = item.nextElementSibling;
  if (next && next.classList && next.classList.contains('mb-links-panel')) {
    next.remove();
    return;
  }
  // Only one panel open at a time
  document.querySelectorAll('.mb-links-panel').forEach(p => p.remove());
  const panel = document.createElement('div');
  panel.className = 'mb-links-panel';
  panel.innerHTML = '<p class="status-msg">Loading links from MusicBrainz…</p>';
  item.after(panel);
  try {
    let d = _mbArtistLinksCache[mbid];
    if (!d) {
      d = await api(`/api/mb-artists/${encodeURIComponent(mbid)}/links`);
      _mbArtistLinksCache[mbid] = d;
    }
    const streaming = (d && d.streaming) || [];
    if (!streaming.length) {
      panel.innerHTML = '<p class="status-msg">No streaming links on this artist\u2019s MusicBrainz page.</p>';
      return;
    }
    panel.innerHTML = `<div class="result-sources">${streamingPillsHTML(streaming)}</div>`;
  } catch {
    panel.innerHTML = '<p class="status-msg error">Failed to load links.</p>';
  }
}

// --- Type Chip Logic (multi-select; empty = all) ---
function toggleType(chipEl) {
  const type = chipEl.dataset.type;
  const idx = activeTypeFilters.indexOf(type);
  if (idx === -1) {
    activeTypeFilters.push(type);
    chipEl.classList.add("active");
    chipEl.setAttribute("aria-pressed", "true");
  } else {
    activeTypeFilters.splice(idx, 1);
    chipEl.classList.remove("active");
    chipEl.setAttribute("aria-pressed", "false");
  }
  updateResetFiltersBtn();
  loadReleases();
}

function updateResetFiltersBtn() {
  const btn = document.getElementById("reset-filters-btn");
  if (!btn) return;
  const isDefault = !activeArtistFilterId && activeTypeFilters.length === 4 && !filterUnseen.checked && !document.getElementById("filter-hidden").checked;
  btn.classList.toggle("hidden", isDefault);
}

function resetFilters() {
  activeArtistFilterId = "";
  filterArtistBtn.textContent = "All Artists";
  filterArtistBtn.title = "";
  activeTypeFilters = ["Album", "EP", "Single", "Other"];
  document.querySelectorAll(".type-chips .chip").forEach(c => { c.classList.add("active"); c.setAttribute("aria-pressed", "true"); });
  filterUnseen.checked = false;
  document.getElementById("filter-hidden").checked = false;
  _updateCheckBtn();
  updateResetFiltersBtn();
  loadReleases();
}

// --- Feed paging state ---
// The feed loads one page at a time and appends more as the sentinel nears the
// viewport, instead of shipping/rendering the whole library at once.
const RELEASE_PAGE_SIZE = 60;
let _releaseCursor = "";        // opaque X-Next-Cursor from the last page
let _releaseTotal = 0;          // X-Total-Count for the active filters
let _releaseLoaded = 0;         // cards currently rendered
let _releaseLoadingMore = false;
let _releaseRequestSeq = 0;     // guards against stale responses after a filter change
let _releaseObserver = null;

function _releaseFilterParams() {
  const params = new URLSearchParams();
  if (activeArtistFilterId) params.set("artist_id", activeArtistFilterId);
  // Send selected types as comma-separated; empty means "all"
  if (activeTypeFilters.length > 0 && activeTypeFilters.length < 4) {
    params.set("type", activeTypeFilters.join(","));
  }
  if (filterUnseen.checked) params.set("unseen_only", "true");
  if (document.getElementById("filter-hidden").checked) params.set("hidden_only", "true");
  return params;
}

function _releaseEmptyStateHTML() {
  const totalArtists = (_artistsCache || []).length;
  if (totalArtists === 0) {
    return `<div class="empty-wrap"><h3>No releases yet</h3><p>Add artists to start tracking.</p><button class="check-btn"${dataAttrs({ action: "goto-artists" })}>Find artists</button></div>`;
  }
  if (filterUnseen.checked) {
    return `<div class="empty-wrap"><h3>You're all caught up</h3><p>No new releases match your filters.</p><button class="link-btn"${dataAttrs({ action: "show-all-releases" })}>Show all releases</button></div>`;
  }
  if (activeArtistFilterId || activeTypeFilters.length < 4) {
    return `<div class="empty-wrap"><h3>No matches</h3><p>Nothing matches the current filters.</p><button class="link-btn"${dataAttrs({ action: "reset-filters" })}>Reset filters</button></div>`;
  }
  return '<p class="empty-state">No releases yet. Check your artists for new releases.</p>';
}

function _renderReleaseCards(releases) {
  return releases.map(r => renderReleaseCard(r)).join("");
}

function _teardownReleaseObserver() {
  if (_releaseObserver) {
    _releaseObserver.disconnect();
    _releaseObserver = null;
  }
  const sentinel = document.getElementById("release-sentinel");
  if (sentinel) sentinel.remove();
}

function _setupReleaseObserver() {
  _teardownReleaseObserver();
  if (!_releaseCursor || _releaseLoaded >= _releaseTotal) return;

  const sentinel = document.createElement("div");
  sentinel.id = "release-sentinel";
  sentinel.className = "release-sentinel";
  sentinel.onclick = () => _loadMoreReleases();
  releaseList.appendChild(sentinel);

  if (typeof IntersectionObserver === "undefined") {
    // Fallback for very old browsers: an explicit click target.
    sentinel.textContent = "Load more";
    return;
  }

  _releaseObserver = new IntersectionObserver(entries => {
    if (entries.some(e => e.isIntersecting)) _loadMoreReleases();
  }, { rootMargin: "800px 0px" });
  _releaseObserver.observe(sentinel);
}

async function _loadMoreReleases() {
  if (_releaseLoadingMore || !_releaseCursor) return;
  _releaseLoadingMore = true;
  const seq = _releaseRequestSeq;

  const sentinel = document.getElementById("release-sentinel");
  if (sentinel) sentinel.textContent = "Loading more…";

  try {
    const params = _releaseFilterParams();
    params.set("limit", RELEASE_PAGE_SIZE);
    params.set("cursor", _releaseCursor);
    const resp = await apiRaw(`/api/releases?${params}`);
    if (seq !== _releaseRequestSeq) return; // filters changed mid-flight

    const releases = await resp.json();
    _releaseCursor = resp.headers.get("X-Next-Cursor") || "";
    if (!releases.length) { _teardownReleaseObserver(); return; }

    _releaseLoaded += releases.length;
    const cardsHtml = _renderReleaseCards(releases);
    const current = document.getElementById("release-sentinel");
    if (current) current.insertAdjacentHTML("beforebegin", cardsHtml);
    else releaseList.insertAdjacentHTML("beforeend", cardsHtml);

    if (!_releaseCursor || _releaseLoaded >= _releaseTotal) {
      _teardownReleaseObserver();
    } else if (_releaseObserver) {
      if (sentinel) sentinel.textContent = "";
    } else if (sentinel) {
      // No IntersectionObserver: keep the click target armed for the next page.
      sentinel.textContent = "Load more";
    }
  } catch {
    // The sentinel stays put (and is clickable) so the user can retry.
    if (sentinel) sentinel.textContent = "Couldn't load more — click to retry.";
  } finally {
    _releaseLoadingMore = false;
  }
}

async function loadReleases() {
  // New load: invalidate any in-flight page request and reset paging.
  const seq = ++_releaseRequestSeq;
  _releaseCursor = "";
  _releaseTotal = 0;
  _releaseLoaded = 0;
  _teardownReleaseObserver();

  const params = _releaseFilterParams();
  params.set("limit", RELEASE_PAGE_SIZE);

  let releases;
  try {
    const resp = await apiRaw(`/api/releases?${params}`);
    if (seq !== _releaseRequestSeq) return;
    releases = await resp.json();
    _releaseCursor = resp.headers.get("X-Next-Cursor") || "";
    const total = parseInt(resp.headers.get("X-Total-Count") || "", 10);
    _releaseTotal = Number.isFinite(total) ? total : releases.length;
  } catch {
    if (seq !== _releaseRequestSeq) return;
    releaseList.innerHTML = '<p class="empty-state">Failed to load releases.</p>';
    return;
  }
  if (seq !== _releaseRequestSeq) return;

  if (!releases.length) {
    releaseList.innerHTML = _releaseEmptyStateHTML();
    updateUnseenBadge();
    return;
  }

  _releaseLoaded = releases.length;
  releaseList.innerHTML = _renderReleaseCards(releases);
  _setupReleaseObserver();
  updateUnseenBadge();
}

// --- Credited artists (MusicBrainz artist-credit) ---
function parseCredits(raw) {
  // Credits are stored as a JSON string in SQLite; tolerate already-parsed arrays
  if (!raw) return [];
  if (Array.isArray(raw)) return raw;
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function initialsFor(name) {
  if (!name) return "?";
  const words = name.trim().split(/\s+/).slice(0, 2);
  return words.map(w => (w[0] || "").toUpperCase()).join("") || "?";
}

function renderReleaseCard(r) {
  const hasTracklist = r.release_type !== "Single";
  const expandable = hasTracklist;
  // When expandable we inject cardClass via wrapper below; build opening tag accordingly
  const openTag = expandable
    ? `<div class="release-card"${dataAttrs({ action: "toggle-tracklist" })} tabindex="0" role="button" aria-expanded="false">`
    : `<div class="release-card static">`;
  const artistLink = r.artist_mbid
    ? `<a href="https://musicbrainz.org/artist/${esc(r.artist_mbid)}" target="_blank" rel="noopener">${esc(r.artist_name)}</a>`
    : esc(r.artist_name);

  // Determine source icon and view URL
  const source = r.source || "musicbrainz";
  let sourceBadge = sourceIcon(source, 32);

  // Build the correct "View" URL based on source
  let viewUrl;
  if (source === "soundcloud") {
    // SoundCloud: the stored permalink/set URL. Singles keep only their
    // numeric track id in mbid (it is the uniqueness key), and
    // soundcloud.com/{artist}/{numeric_id} does not resolve — so without a
    // stored URL the honest fallback is a search for the release.
    if (r.mb_url) {
      viewUrl = r.mb_url;
    } else {
      viewUrl = `https://soundcloud.com/search?q=${encodeURIComponent(`${r.artist_name} ${r.title}`)}`;
    }
  } else if (source === "itunes" && r.itunes_collection_id) {
    // iTunes: link to Apple Music album page
    viewUrl = `https://music.apple.com/us/album/${esc(r.title.toLowerCase().replace(/[^a-z0-9]/g, ''))}/${r.itunes_collection_id}`;
  } else if (source === "itunes" && r.mb_url) {
    viewUrl = r.mb_url;
  } else {
    // MusicBrainz default
    viewUrl = r.mb_url || `https://musicbrainz.org/release-group/${r.mbid}`;
  }

  // Determine cover art URL (null => initials fallback instead of broken icon)
  // MusicBrainz covers go through the local disk-cached proxy: CAA is slow and
  // hundreds of release groups have no art, which the proxy remembers so the
  // browser never re-requests them.
  let coverUrl = null;
  if (source === 'soundcloud' && r.artwork_url) {
    coverUrl = r.artwork_url;
  } else if (source === 'itunes' && r.artwork_url) {
    coverUrl = r.artwork_url;
  } else if (source === 'musicbrainz' && r.mbid) {
    coverUrl = `/api/covers/${encodeURIComponent(r.mbid)}`;
  }
  const coverHtml = coverUrl
    ? `<img class="release-cover" src="${coverUrl}" alt="" width="64" height="64" decoding="async" loading="lazy" data-cover-fallback="${esc(initialsFor(r.artist_name))}">`
    : `<div class="cover-fallback" aria-hidden="true">${esc(initialsFor(r.artist_name))}</div>`;

    // Streaming links only exist for MusicBrainz-sourced releases.
  const hasStreaming = source === "musicbrainz";
  const streamBtn = hasStreaming
    ? `<button class="expand-btn" data-expanded="false"${dataAttrs({ action: "toggle-streaming" })} aria-expanded="false">Stream ▾</button>`
    : "";
  const trackBtn = hasTracklist
    ? `<button class="expand-btn" data-expanded="false"${dataAttrs({ action: "toggle-tracklist" })} aria-expanded="false">Tracklist ▾</button>`
    : "";

  // Credited artists (e.g. featured guests / collabs), excluding the tracked artist
  const creditNames = parseCredits(r.credits).map(c => esc(c.name)).join(", ");
  const creditsHtml = creditNames ? `<div class="release-credits">with ${creditNames}</div>` : "";

  return `
    <div class="release-item ${r.notified === 0 ? "unseen" : ""} ${r.is_visible === 0 ? "is-hidden" : ""}" data-id="${esc(r.id)}" data-mbid="${esc(r.mbid)}" data-source="${esc(source)}">
      ${openTag}
        ${coverHtml}
        <div class="release-info">
          <div class="release-title">${esc(r.title)}</div>
          <div class="release-artist">${artistLink}</div>
          ${creditsHtml}
          <div class="release-meta">
            ${esc(r.release_type)} · ${esc(r.release_date || "Unknown date")}
            <span class="hint-group">
              ${r.is_visible === 0
                ? `<button class="expand-btn"${dataAttrs({ action: "unhide-release", id: r.id })} title="Restore to feed">Unhide</button>`
                : `<button class="hide-btn-inline"${dataAttrs({ action: "hide-release", id: r.id })} title="Hide from feed" aria-label="Hide ${esc(r.title)} from feed">
                <img src="/static/assets/hidden.svg" alt="">
              </button>`}
              ${trackBtn}
              ${streamBtn}
            </span>
          </div>
        </div>
        <div class="release-actions">
          <a class="release-source-link" href="${esc(viewUrl)}" target="_blank" rel="noopener" title="View on ${source === 'musicbrainz' ? 'MusicBrainz' : source === 'itunes' ? 'Apple Music' : 'SoundCloud'}">
            ${sourceBadge}
          </a>
          ${r.notified === 0 ? `<button class="release-badge"${dataAttrs({ action: "mark-seen", id: r.id })} aria-label="Mark ${esc(r.title)} as seen">NEW</button>` : ""}
        </div>
      </div>
      <div class="tracklist-section"></div>
      ${hasStreaming ? '<div class="streaming-section"></div>' : ''}
    </div>
  `;
}

// --- Tracklist Toggle (integrated inline) ---
const tracklistCache = {};
const streamingCache = {};

function setCardExpanded(item, kind, isExpanded) {
  if (!item) return;
  const card = item.querySelector(".release-card");
  if (card) card.setAttribute("aria-expanded", isExpanded ? "true" : "false");
  item.classList.toggle("expanded", isExpanded);
  item.querySelectorAll(".expand-btn").forEach(b => {
    const isTrack = b.textContent.includes("Tracklist");
    const isStream = b.textContent.includes("Stream");
    if ((kind === "track" && isTrack) || (kind === "stream" && isStream)) {
      b.setAttribute("aria-expanded", isExpanded ? "true" : "false");
      b.setAttribute("data-expanded", isExpanded ? "true" : "false");
      const label = isTrack ? "Tracklist" : "Stream";
      b.textContent = `${label} ${isExpanded ? "▴" : "▾"}`;
    }
  });
}

function toggleTracklist(cardEl) {
  const item = cardEl.closest(".release-item");
  const section = item.querySelector(".tracklist-section");
  const releaseMbid = item.dataset.mbid;

  const expanded = section.classList.contains("expanded");

  if (expanded) {
    section.classList.remove("expanded");
    setCardExpanded(item, "track", false);
    return;
  }

  section.classList.add("expanded");
  setCardExpanded(item, "track", true);

  // Check cache
  if (tracklistCache[releaseMbid]) {
    renderTracklist(section, tracklistCache[releaseMbid]);
    return;
  }

  // Fetch tracks
  section.innerHTML = '<div class="tracklist-list"><div class="track-skeleton"><span style="width:2rem"></span><span style="flex:1"></span></div><div class="track-skeleton"><span style="width:2rem"></span><span style="flex:1"></span></div><div class="track-skeleton"><span style="width:2rem"></span><span style="flex:1"></span></div></div>';
  api(`/api/releases/${encodeURIComponent(releaseMbid)}/tracks`)
    .then(data => {
      const tracks = data.tracks;
      tracklistCache[releaseMbid] = tracks;
      renderTracklist(section, tracks);
    })
    .catch(() => {
      section.innerHTML = `<p class="tracklist-error">Failed to load tracks. <button class="link-btn"${dataAttrs({ action: "retry-tracklist" })}>Retry</button></p>`;
    });
}

function renderTracklist(container, tracks) {
  if (!tracks || tracks.length === 0) {
    container.innerHTML = '<p class="tracklist-empty">No tracks found.</p>';
    return;
  }

  function formatLength(ms) {
    if (!ms) return "";
    const sec = Math.floor(ms / 1000);
    const min = Math.floor(sec / 60);
    const s = sec % 60;
    return `${min}:${s.toString().padStart(2, "0")}`;
  }

  const item = container.closest(".release-item");
  const mbid = item ? item.dataset.mbid : "";
  container.innerHTML = html`
    <div class="tracklist-header">
      <span class="tracklist-title">Tracklist</span>
      <span><span class="tracklist-count">${tracks.length} tracks</span> <button class="copy-tracklist-btn"${raw(dataAttrs({ action: "copy-tracklist", mbid: mbid }))} title="Copy track titles">Copy</button></span>
    </div>
    <div class="tracklist-list">
      ${tracks.map(t => {
        const creditNames = (t.credits && t.credits.length)
          ? t.credits.map(c => c.name).join(", ")
          : "";
        const feat = creditNames
          ? html`<span class="track-feat">feat. ${creditNames}</span>`
          : "";
        const fullTitle = t.title + (creditNames ? ` (feat. ${creditNames})` : "");
        return html`
        <div class="track-item" title="${fullTitle}">
          <span class="track-number">${t.number}</span>
          <span class="track-title-text">${t.title}${t.has_single ? raw(' <span class="single-badge" title="Also released as a single">SINGLE</span>') : ''}${feat}</span>
          <span class="track-length">${formatLength(t.length)}</span>
        </div>
      `; })}
    </div>
  `;
}

function copyTracklist(mbid, btn) {
  const tracks = tracklistCache[mbid] || [];
  const text = tracks.map(t => `${t.number}. ${t.title}`).join("\n");
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = "Copied ✓";
    setTimeout(() => { btn.textContent = orig; }, 1500);
  }).catch(() => showToast("Copy failed"));
}

// --- Streaming URLs Toggle (dedicated section, separate from tracklist) ---
function toggleStreaming(cardEl) {
  const item = cardEl.closest(".release-item");
  if (!item) return;
  const section = item.querySelector(".streaming-section");
  if (!section) return;

  // Streaming links only exist for MusicBrainz-sourced releases.
  const source = item.dataset.source || "musicbrainz";
  if (source !== "musicbrainz") {
    section.classList.remove("expanded");
    return;
  }

  const releaseMbid = item.dataset.mbid;
  const expanded = section.classList.contains("expanded");

  if (expanded) {
    section.classList.remove("expanded");
    setCardExpanded(item, "stream", false);
    return;
  }

  // Check cache first
  if (streamingCache[releaseMbid]) {
    renderStreaming(section, streamingCache[releaseMbid]);
    section.classList.add("expanded");
    setCardExpanded(item, "stream", true);
    return;
  }

  section.classList.add("expanded");
  setCardExpanded(item, "stream", true);
  section.innerHTML = '<p class="tracklist-loading">Loading streaming links...</p>';

  api(`/api/releases/${encodeURIComponent(releaseMbid)}/streaming`)
    .then(data => {
      const streaming = data.streaming || [];
      streamingCache[releaseMbid] = streaming;
      renderStreaming(section, streaming);
    })
    .catch(() => {
      section.innerHTML = '<p class="tracklist-error">Failed to load streaming links.</p>';
    });
}

// --- Streaming service icon mapping ---
const STREAMING_ICONS = {
  spotify: "spotify.svg",
  apple_music: "apple-music.svg",
  amazon_music: "amazon-music.svg",
  deezer: "deezer.svg",
  tidal: "tidal.svg",
  soundcloud: "soundcloud.svg",
  qobuz: "qobuz.svg",
  bandcamp: "bandcamp.svg",
  youtube: "youtube.svg",
};

// Shared pill markup for streaming/external platform links (feed stream
// section + tracked-artist MB links panel). Same look as the search-result
// source badges: brand icon in original colors + label + ↗.
function streamingPillsHTML(streaming) {
  return html`${(streaming || []).map(s => {
    const icon = (typeof STREAMING_ICONS !== 'undefined' && STREAMING_ICONS[s.key])
      ? html`<img src="/static/assets/${STREAMING_ICONS[s.key]}" alt="" class="source-icon-inline" style="width:14px;height:14px;margin-left:0;">`
      : '';
    return html`<a class="result-pill" href="${s.url}" target="_blank" rel="noopener" title="${s.service}">${icon}${s.service} ↗</a>`;
  })}`;
}

function renderStreaming(container, streaming) {
  if (!streaming || streaming.length === 0) {
    container.innerHTML = '<p class="tracklist-empty">No streaming links found for this release.</p>';
    return;
  }

  container.innerHTML = html`
    <div class="streaming-header">
      <span class="tracklist-title">Stream on</span>
      <span class="tracklist-count">${streaming.length} link${streaming.length > 1 ? "s" : ""}</span>
    </div>
    <div class="streaming-list">
      ${streamingPillsHTML(streaming)}
    </div>
  `;
}

// --- Mark Seen ---
async function markSeen(id, el) {
  await apiRaw(`/api/releases/${id}/seen`, { method: "POST", allowError: true });
  const item = el.closest(".release-item");
  if (item) item.classList.remove("unseen");
  el.remove();
  updateUnseenBadge();
}

async function hideRelease(id, el) {
  const item = el.closest(".release-item");
  const title = item ? (item.querySelector(".release-title") || {}).textContent || "Release" : "Release";
  await apiRaw(`/api/releases/${id}/hide`, { method: "POST", allowError: true });
  if (item) {
    const placeholder = document.createComment(`hidden-release-${id}`);
    item.replaceWith(placeholder);
    showToast(`Hidden “${title.trim().slice(0, 60)}”`, "Undo", async () => {
      await apiRaw(`/api/releases/${id}/unhide`, { method: "POST", allowError: true });
      placeholder.replaceWith(item);
      item.style.opacity = "0";
      requestAnimationFrame(() => { item.style.transition = "opacity 0.3s"; item.style.opacity = "1"; });
      updateUnseenBadge();
    });
    setTimeout(() => { if (placeholder.parentNode) placeholder.remove(); }, 6000);
  }
  updateUnseenBadge();
}

async function unhideRelease(id, el) {
  await apiRaw(`/api/releases/${id}/unhide`, { method: "POST", allowError: true });
  const item = el.closest(".release-item");
  if (item) {
    item.style.transition = "opacity 0.3s";
    item.style.opacity = "0";
    setTimeout(() => { loadReleases(); }, 250);
    showToast("Restored to feed");
  } else {
    loadReleases();
  }
  updateUnseenBadge();
}

async function markAllReleasesSeen() {
  await apiRaw("/api/releases/all_seen", { method: "POST", allowError: true });
  updateUnseenBadge();
  loadReleases();
}

function updateUnseenBadge() {
  api("/api/unseen_count")
    .then(data => {
      const badge = document.getElementById("unseen-badge");
      if (badge) {
        if (data.count > 0) { badge.textContent = data.count; badge.classList.remove("hidden"); badge.title = `${data.count} unseen release(s)`; }
        else { badge.classList.add("hidden"); }
      }
    });
}

// --- Artists Tab ---
const searchInput = document.getElementById("artist-search-input");
const searchBtn = document.getElementById("artist-search-btn");
const searchResults = document.getElementById("search-results");
const artistListEl = document.getElementById("artist-list");

searchBtn.addEventListener("click", searchArtists);
searchInput.addEventListener("keydown", e => { if (e.key === "Enter") searchArtists(); });

// Reactive search: clear results when input is empty, debounce search when typing
let _searchTimeout = null;
searchInput.addEventListener("input", () => {
  const query = searchInput.value.trim();
  clearTimeout(_searchTimeout);
  if (!query) {
    searchResults.innerHTML = "";
    return;
  }
  // Debounce: wait 400ms after typing stops before searching
  _searchTimeout = setTimeout(searchArtists, 400);
});

// --- Enabled sources cache (mirrors Settings toggles) ---
// Default: everything enabled. Refreshed from /api/settings whenever the
// artists list renders and after settings are saved.
let _enabledSources = { musicbrainz: true, itunes: true, soundcloud: true };

async function refreshEnabledSources() {
  try {
    const s = await api("/api/settings");
    _enabledSources = {
      musicbrainz: s.source_musicbrainz !== "0",
      itunes: s.source_itunes !== "0",
      soundcloud: s.source_soundcloud !== "0",
    };
  } catch {
    _enabledSources = { musicbrainz: true, itunes: true, soundcloud: true };
  }
  if (typeof syncSearchSourceMenu === "function") syncSearchSourceMenu();
  return _enabledSources;
}

function _srcEnabled(source) {
  return _enabledSources[source] !== false;
}

let _lastSearchResults = [];
const SEARCH_PAGE_SIZE = 15;

function searchResultPills(a) {
  const pills = [];
  // Confirmation titles stay as hover tooltips; the visible ✓ MB marker is
  // rendered once to the right of the badges (see mbConfirmedTag below).
  const appleTitle = a._confirmed_apple ? ' title="Apple Music ID confirmed via MusicBrainz relationships"' : '';
  const scTitle = a._confirmed_sc ? ' title="SoundCloud profile confirmed via MusicBrainz relationships"' : '';
  if (a.source === 'soundcloud' && a.soundcloud_permalink) {
    if (_srcEnabled('soundcloud')) pills.push(`<a class="result-pill" href="https://soundcloud.com/${esc(a.soundcloud_permalink)}" target="_blank" rel="noopener"${scTitle}>${sourceIcon('soundcloud', 14)} SoundCloud ↗</a>`);
  } else {
    if (a.mbid && _srcEnabled('musicbrainz')) pills.push(`<a class="result-pill" href="https://musicbrainz.org/artist/${esc(a.mbid)}" target="_blank" rel="noopener">${sourceIcon('musicbrainz', 14)} MusicBrainz ↗</a>`);
    if (a.itunes_artist_id && _srcEnabled('itunes')) pills.push(`<a class="result-pill" href="https://music.apple.com/artist/${esc(String(a.name).toLowerCase().replace(/[^a-z0-9]/g, ''))}/${esc(a.itunes_artist_id)}" target="_blank" rel="noopener"${appleTitle}>${sourceIcon('itunes', 14)} Apple Music ↗</a>`);
    if (a.soundcloud_permalink && _srcEnabled('soundcloud')) pills.push(`<a class="result-pill" href="https://soundcloud.com/${esc(a.soundcloud_permalink)}" target="_blank" rel="noopener"${scTitle}>${sourceIcon('soundcloud', 14)} SoundCloud ↗</a>`);
  }
  pills.push(mbConfirmedTag(a));
  const inner = pills.join("");
  return inner ? `<div class="result-sources">${inner}</div>` : "";
}

// Visible marker placed to the right of the source badges when MB URL
// relationships confirmed the Apple Music / SoundCloud links.
function mbConfirmedTag(a) {
  const bits = [];
  if (a._confirmed_apple) bits.push("Apple Music");
  if (a._confirmed_sc) bits.push("SoundCloud");
  if (!bits.length) return "";
  return html`<span class="mb-confirmed-tag" title="${bits.join(" + ")} link${bits.length > 1 ? "s" : ""} confirmed via this artist's MusicBrainz page">✓ MB</span>`;
}

function formatFollowers(n) {
  if (n == null || isNaN(Number(n))) return "";
  n = Number(n);
  if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M followers";
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "K followers";
  return n + (n === 1 ? " follower" : " followers");
}

function resultDetailLine(a) {
  const parts = [a.disambiguation, a.type, a.country].filter(Boolean);
  const followers = formatFollowers(a.followers_count);
  if (followers) parts.push(followers);
  if (a._albums) parts.push(a._albums);
  return parts.join(" · ");
}

// Album counts arrive after the rows render (one lookup per artist, paced
// server-side) and patch the detail lines in place — search stays instant.
const _albumCountCache = {};
const _albumCountPending = new Set();

function enhanceItunesCounts(list, containerEl) {
  if (!containerEl || !list) return;
  const rows = containerEl.querySelectorAll('.search-result-item');
  list.forEach((a, i) => {
    const id = a.itunes_artist_id;
    if (!id) return;
    const key = `${id}:${a.country || 'us'}`;
    const apply = () => {
      const row = rows[i];
      if (!row) return;
      const detail = row.querySelector('.result-detail');
      if (detail) {
        const line = resultDetailLine(a);
        detail.textContent = line;
        detail.title = line;
      }
    };
    if (Object.hasOwn(_albumCountCache, key)) {
      if (_albumCountCache[key]) { a._albums = _albumCountCache[key]; apply(); }
    } else if (!_albumCountPending.has(key)) {
      _albumCountPending.add(key);
      api(`/api/itunes/album-count?artist_id=${id}&country=${encodeURIComponent(a.country || 'us')}`)
        .then(d => {
          const label = (d.albums == null) ? null
            : d.albums === 1 ? "1 album"
            : (d.capped ? `${d.albums}+ albums` : `${d.albums} albums`);
          _albumCountCache[key] = label;
          if (label) { a._albums = label; apply(); }
        })
        .catch(() => { _albumCountCache[key] = null; })
        .finally(() => _albumCountPending.delete(key));
    }
  });
}

// --- MusicBrainz-confirmed artist links (Apple Music ID + SC permalink) ---
// MB artist url-rels are ground truth: when present they override the fuzzy
// name-based merge from iTunes text search / SC slug probing. Fetched lazily
// per visible MB row so search stays instant (MB serves ~1 req/sec).
const _mbLinksCache = {};
const _mbLinksPending = new Set();

function _applyMbLinksToEntry(a, d) {
  if (!d) return false;
  let changed = false;
  if (d.itunes_artist_id) {
    if (String(a.itunes_artist_id || "") !== String(d.itunes_artist_id)) {
      a.itunes_artist_id = d.itunes_artist_id;
      changed = true;
    }
    if (!a._confirmed_apple) { a._confirmed_apple = true; changed = true; }
  }
  if (d.soundcloud_permalink) {
    if ((a.soundcloud_permalink || "").toLowerCase() !== String(d.soundcloud_permalink).toLowerCase()) {
      a.soundcloud_permalink = d.soundcloud_permalink;
      changed = true;
    }
    if (!a._confirmed_sc) { a._confirmed_sc = true; changed = true; }
  }
  if (d.streaming && !a._mb_streaming) a._mb_streaming = d.streaming;
  return changed;
}

function _patchSearchRowPills(containerEl, index, a) {
  const rows = containerEl ? containerEl.querySelectorAll('.search-result-item') : null;
  const row = rows ? rows[index] : null;
  if (!row || !row.isConnected) return;
  const srcWrap = row.querySelector('.result-text');
  if (srcWrap) {
    const oldSources = srcWrap.querySelector('.result-sources');
    const fresh = document.createElement('div');
    fresh.innerHTML = searchResultPills(a);
    const freshSources = fresh.firstElementChild;
    if (oldSources && freshSources) oldSources.replaceWith(freshSources);
    else if (freshSources) srcWrap.appendChild(freshSources);
  }
  // The Add button references the live row object via _rowData (see the
  // delegated handler), so a re-patch is no longer needed to keep it current:
  // _applyMbLinksToEntry mutates the very object the button points at.
}

function enrichSearchRowsWithMbLinks(list, containerEl) {
  if (!containerEl || !list) return;
  list.forEach((a, i) => {
    const mbid = a.mbid;
    if (!mbid) return;
    // Backend already confirmed this row's links during search (it skipped
    // the redundant platform searches) — nothing to fetch.
    if (a._mb_enriched) return;
    if (Object.hasOwn(_mbLinksCache, mbid)) {
      if (_applyMbLinksToEntry(a, _mbLinksCache[mbid])) _patchSearchRowPills(containerEl, i, a);
      return;
    }
    if (_mbLinksPending.has(mbid)) return;
    _mbLinksPending.add(mbid);
    api(`/api/mb-artists/${encodeURIComponent(mbid)}/links`)
      .then(d => {
        _mbLinksCache[mbid] = d;
        // Upgrade every entry sharing this MBID (visible now or via paging),
        // then patch the rows of this render in place.
        for (const entry of _lastSearchResults) {
          if (entry.mbid === mbid) _applyMbLinksToEntry(entry, d);
        }
        list.forEach((entry, j) => {
          if (entry.mbid === mbid) _patchSearchRowPills(containerEl, j, entry);
        });
      })
      .catch(() => { /* keep name-merged badges on failure */ })
      .finally(() => _mbLinksPending.delete(mbid));
  });
}

function renderSearchPage() {
  const moreWrap = document.getElementById("search-more-wrap");
  const visible = _lastSearchResults.slice(0, _searchVisibleCount);
  searchResults.innerHTML = `<p class="status-msg">Found ${_lastSearchResults.length} — showing ${visible.length}</p>` + visible.map(a => {
    const trackedPill = a.already_tracked ? '<span class="tracked-pill">TRACKED</span>' : '';
    let artistImage = a.artistImageUrl || '';
    if (!artistImage && a.itunes_artist_id) artistImage = '/static/assets/apple-music.svg';
    const imageHtml = artistImage
      ? `<img class="result-image ${a.itunes_artist_id && !a.artistImageUrl ? 'result-image-icon' : ''}" src="${esc(artistImage)}" alt="" data-hide-on-error">`
      : '';
    return `
      <div class="search-result-item">
        <div class="result-info">
          ${imageHtml}
          <div class="result-text">
            <div class="result-name">${esc(a.name)}${trackedPill}</div>
            <div class="result-detail" title="${esc(resultDetailLine(a))}">${esc(resultDetailLine(a))}</div>
            ${searchResultPills(a)}
          </div>
        </div>
        <button class="add-btn"${dataAttrs({ action: "add-artist", key: registerRow(a) })}>${a.already_tracked ? 'Link' : 'Add'}</button>
      </div>
    `;
  }).join("");
  if (moreWrap) moreWrap.classList.toggle("hidden", _lastSearchResults.length <= _searchVisibleCount);
  enhanceItunesCounts(visible, searchResults);
  enrichSearchRowsWithMbLinks(visible, searchResults);
}

let _searchVisibleCount = SEARCH_PAGE_SIZE;
const _moreBtn = document.getElementById("search-more-btn");
if (_moreBtn) _moreBtn.addEventListener("click", () => { _searchVisibleCount += SEARCH_PAGE_SIZE; renderSearchPage(); });

// --- Search platform filter (which sources the Artists search queries) ---
const SEARCH_SOURCES_ALL = ["musicbrainz", "itunes", "soundcloud"];
const SEARCH_SOURCE_LABELS = { musicbrainz: "MB", itunes: "Apple", soundcloud: "SC" };
let _searchSources = [...SEARCH_SOURCES_ALL];
try {
  const saved = JSON.parse(localStorage.getItem("mrt_search_sources") || "null");
  if (Array.isArray(saved)) {
    const valid = saved.filter(s => SEARCH_SOURCES_ALL.includes(s));
    if (valid.length) _searchSources = valid;
  }
} catch { /* keep defaults */ }

function toggleSearchSourceMenu() {
  const menu = document.getElementById("search-source-menu");
  const btn = document.getElementById("search-source-btn");
  const hidden = menu.classList.contains("hidden");
  syncSearchSourceMenu();
  menu.classList.toggle("hidden", !hidden);
  btn.setAttribute("aria-expanded", hidden ? "true" : "false");
}

function closeSearchSourceMenu() {
  document.getElementById("search-source-menu").classList.add("hidden");
  document.getElementById("search-source-btn").setAttribute("aria-expanded", "false");
}

function syncSearchSourceMenu() {
  const menu = document.getElementById("search-source-menu");
  if (!menu) return;
  menu.querySelectorAll('input[data-source]').forEach(cb => {
    const s = cb.dataset.source;
    const on = _srcEnabled(s);
    cb.disabled = !on;
    cb.checked = on && _searchSources.includes(s);
    cb.closest('.dropdown-check').classList.toggle('disabled', !on);
    cb.closest('.dropdown-check').title = on ? '' : 'Disabled in Settings';
  });
  updateSearchSourceBtn();
}

function updateSearchSourceBtn() {
  const btn = document.getElementById("search-source-btn");
  if (!btn) return;
  const active = _searchSources.filter(s => _srcEnabled(s));
  const show = active.length ? active : _searchSources;
  const full = show.map(s => ({ musicbrainz: "MusicBrainz", itunes: "Apple Music", soundcloud: "SoundCloud" }[s])).join(", ");
  btn.textContent = (show.length === SEARCH_SOURCES_ALL.length ? "All" : show.map(s => SEARCH_SOURCE_LABELS[s]).join("+")) + " ▾";
  btn.title = `Search: ${full}. Click to choose platforms.`;
}

function updateSearchSources() {
  const sel = [...document.querySelectorAll('#search-source-menu input[data-source]:checked')]
    .map(cb => cb.dataset.source);
  if (!sel.length) {
    syncSearchSourceMenu(); // keep at least one platform selected
    return;
  }
  _searchSources = sel;
  try { localStorage.setItem("mrt_search_sources", JSON.stringify(sel)); } catch { /* ignore */ }
  updateSearchSourceBtn();
}

document.addEventListener("click", e => {
  const wrapper = document.getElementById("search-source-wrapper");
  if (wrapper && !wrapper.contains(e.target)) closeSearchSourceMenu();
});

async function searchArtists() {
  const query = searchInput.value.trim();
  if (!query) return;

  searchBtn.disabled = true;
  searchBtn.textContent = "Searching...";
  searchResults.innerHTML = '<p class="status-msg">Searching…</p>';

  try {
    const results = await api("/api/artists/search", {
      method: "POST",
      json: { query, sources: _searchSources },
      allowError: true,
    });
    if (!Array.isArray(results)) {
      searchResults.innerHTML = `<p class="status-msg error">Search failed: ${esc(typeof results === 'string' ? results : JSON.stringify(results))}</p>`;
      return;
    }

        if (results.length === 0) {
      _lastSearchResults = [];
      searchResults.innerHTML = '<p class="status-msg">No artists found. Check spelling or paste a direct link.</p>';
      document.getElementById("search-more-wrap").classList.add("hidden");
    } else {
      _lastSearchResults = results;
      _searchVisibleCount = SEARCH_PAGE_SIZE;
      renderSearchPage();
    }
  } catch (err) {
    searchResults.innerHTML = `<p class="status-msg error">Search failed: ${esc(err.message)}</p>`;
  } finally {
    searchBtn.disabled = false;
    searchBtn.textContent = "Search";
  }
}

async function addArtist(artist, btn) {
  btn.disabled = true;
  btn.textContent = "Adding...";

  // Determine which sources to add based on available IDs.
  // Merged rows can carry IDs for several platforms — link them all
  // sequentially (first creates the artist, the rest link by name).
  const sourcesToAdd = [];
  if (artist.mbid) {
    sourcesToAdd.push({ source: "musicbrainz", id: artist.mbid });
  }
  if (artist.itunes_artist_id) {
    sourcesToAdd.push({ source: "itunes", id: String(artist.itunes_artist_id) });
  }
  if (artist.soundcloud_permalink) {
    sourcesToAdd.push({ source: "soundcloud", id: artist.soundcloud_permalink });
  }

  // Default to the original source if neither is set (legacy)
  if (sourcesToAdd.length === 0) {
    sourcesToAdd.push({ source: artist.source, id: artist.mbid });
  }

  let totalReleases = 0;
  let lastStatus = "";
  let failedSources = 0;
  let conflictDetail = "";

  for (const s of sourcesToAdd) {
    try {
      const result = await api("/api/artists", {
        method: "POST",
        json: {
          source: s.source,
          id: s.id,
          name: artist.name,
          disambiguation: artist.disambiguation
        }
      });
      if (result.status === "conflict") {
        // That platform ID is already tracked under a different artist —
        // same "already taken" outcome as the link-source modal. Skip only
        // this source, keep the rest, and report who owns it.
        failedSources += 1;
        lastStatus = "conflict";
        const owner = result.conflicting_artist ? result.conflicting_artist.name : "";
        conflictDetail = owner ? ` (${s.source} already linked to “${owner}”)` : ` (${s.source} already linked elsewhere)`;
        showToast(`“${artist.name}”${conflictDetail}`);
        continue;
      }
      lastStatus = result.status;
      totalReleases += result.releases_imported || 0;
    } catch {
      // One platform failing must not block the others
      failedSources += 1;
    }
  }

  if (!lastStatus) {
    btn.textContent = "Error — retry";
    btn.disabled = false;
    showToast(`Could not add “${artist.name}” — check connection and retry`);
    return;
  }

  if (lastStatus === "conflict" && !totalReleases) {
    btn.textContent = "Already linked elsewhere — retry";
    btn.disabled = false;
    showToast(`Could not add “${artist.name}”${conflictDetail}`);
    loadArtists();
    _refreshArtistsCache();
    return;
  }

  if (lastStatus === "already_exists" && !failedSources) {
    btn.textContent = "Already added";
    showToast(`“${artist.name}” is already tracked`);
  } else if (lastStatus === "linked" && !failedSources) {
    btn.textContent = `Linked (${totalReleases} releases)`;
    showToast(`Linked “${artist.name}” — ${totalReleases} releases`, "View feed", () => { location.hash = "#/feed"; });
  } else if (!failedSources) {
    btn.textContent = `Added (${totalReleases} releases)`;
    showToast(`Added “${artist.name}” — ${totalReleases} releases`, "View feed", () => { location.hash = "#/feed"; });
  } else {
    btn.textContent = `Partial (${totalReleases} releases) — retry`;
    btn.disabled = false;
    showToast(`“${artist.name}” partly added — ${failedSources} source(s) failed, click to retry`);
  }

  loadArtists();
  _refreshArtistsCache();
}

async function loadArtists() {
  const artists = await api("/api/artists");
  await refreshEnabledSources();
  _trackedArtists = artists;
  if (activeTrackedFilterId && !_trackedArtists.some(a => String(a.id) === String(activeTrackedFilterId))) {
    activeTrackedFilterId = "";
  }
  _updateTrackedBtn();
  renderTrackedArtists();
}

function renderTrackedArtistItem(a) {
      const hasMb = !!a.mbid;
      const hasItunes = !!a.itunes_artist_id;
      const hasSc = !!a.soundcloud_permalink;
      const mbOn = _srcEnabled('musicbrainz');
      const itOn = _srcEnabled('itunes');
      const scOn = _srcEnabled('soundcloud');

      // Build compact icon-only source buttons (32px targets, + badge when unlinked).
      // Sources disabled in Settings are hidden entirely.
      let sourceIcons = '';
      // MusicBrainz
      if (mbOn) {
      if (hasMb) {
        sourceIcons += `<a class="source-icon-btn linked" href="https://musicbrainz.org/artist/${esc(a.mbid)}" target="_blank" rel="noopener" title="MusicBrainz — linked, click to open" aria-label="Open ${esc(a.name)} on MusicBrainz"><img src="/static/assets/musicbrainz.svg" alt=""></a>`;
      } else {
        sourceIcons += `<button class="source-icon-btn unlinked"${dataAttrs({ action: "open-link-source", id: a.id, source: "musicbrainz", name: a.name })} title="Link MusicBrainz for ${esc(a.name)}" aria-label="Link MusicBrainz for ${esc(a.name)}"><img src="/static/assets/musicbrainz.svg" alt=""></button>`;
      }
      }
      // iTunes
      if (itOn) {
      if (hasItunes) {
        sourceIcons += `<a class="source-icon-btn linked" href="https://music.apple.com/artist/${esc(a.name.toLowerCase().replace(/[^a-z0-9]/g, ''))}/${esc(a.itunes_artist_id)}" target="_blank" rel="noopener" title="Apple Music — linked, click to open" aria-label="Open ${esc(a.name)} on Apple Music"><img src="/static/assets/apple-music.svg" alt=""></a>`;
      } else {
        sourceIcons += `<button class="source-icon-btn unlinked"${dataAttrs({ action: "open-link-source", id: a.id, source: "itunes", name: a.name })} title="Link Apple Music for ${esc(a.name)}" aria-label="Link Apple Music for ${esc(a.name)}"><img src="/static/assets/apple-music.svg" alt=""></button>`;
      }
      }
      // SoundCloud
      if (scOn) {
      if (hasSc) {
        sourceIcons += `<a class="source-icon-btn linked" href="https://soundcloud.com/${esc(a.soundcloud_permalink)}" target="_blank" rel="noopener" title="SoundCloud — linked, click to open" aria-label="Open ${esc(a.name)} on SoundCloud"><img src="/static/assets/soundcloud.svg" alt=""></a>`;
      } else {
        sourceIcons += `<button class="source-icon-btn unlinked"${dataAttrs({ action: "open-link-source", id: a.id, source: "soundcloud", name: a.name })} title="Link SoundCloud for ${esc(a.name)}" aria-label="Link SoundCloud for ${esc(a.name)}"><img src="/static/assets/soundcloud.svg" alt=""></button>`;
      }
      }
      // MB external links (on-demand viewer for the artist's url-rels)
      if (hasMb) {
        sourceIcons += `<button class="source-icon-btn mb-links-btn"${dataAttrs({ action: "toggle-mb-links", mbid: a.mbid })} title="View external links from this artist's MusicBrainz page" aria-label="View external links for ${esc(a.name)}"><svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#aaa" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg></button>`;
      }

      // Build dropdown menu items (link/unlink hidden for disabled sources)
      let dropdownItems = '';
      // MusicBrainz
      if (mbOn) {
      if (hasMb) {
        dropdownItems += `<button class="dropdown-item"${dataAttrs({ action: "confirm-unlink", id: a.id, source: "musicbrainz" })}><img src="/static/assets/musicbrainz.svg" alt="" class="dropdown-icon"> Unlink MusicBrainz</button>`;
      } else {
        dropdownItems += `<button class="dropdown-item"${dataAttrs({ action: "open-link-source", id: a.id, source: "musicbrainz", name: a.name })}><img src="/static/assets/musicbrainz.svg" alt="" class="dropdown-icon"> Link MusicBrainz</button>`;
      }
      }
      // iTunes
      if (itOn) {
      if (hasItunes) {
        dropdownItems += `<button class="dropdown-item"${dataAttrs({ action: "confirm-unlink", id: a.id, source: "itunes" })}><img src="/static/assets/apple-music.svg" alt="" class="dropdown-icon"> Unlink iTunes</button>`;
      } else {
        dropdownItems += `<button class="dropdown-item"${dataAttrs({ action: "open-link-source", id: a.id, source: "itunes", name: a.name })}><img src="/static/assets/apple-music.svg" alt="" class="dropdown-icon"> Link iTunes</button>`;
      }
      }
      // SoundCloud
      if (scOn) {
      if (hasSc) {
        dropdownItems += `<button class="dropdown-item"${dataAttrs({ action: "confirm-unlink", id: a.id, source: "soundcloud" })}><img src="/static/assets/soundcloud.svg" alt="" class="dropdown-icon"> Unlink SoundCloud</button>`;
      } else {
        dropdownItems += `<button class="dropdown-item"${dataAttrs({ action: "open-link-source", id: a.id, source: "soundcloud", name: a.name })}><img src="/static/assets/soundcloud.svg" alt="" class="dropdown-icon"> Link SoundCloud</button>`;
      }
      }
      // Untrack (red, with confirmation)
      dropdownItems = `<button class="dropdown-item"${dataAttrs({ action: "check-artist", id: a.id })}>↻ Check this artist</button>` + dropdownItems;
      dropdownItems += `<button class="dropdown-item"${dataAttrs({ action: "edit-disambiguation", id: a.id })}><img src="/static/assets/edit.svg" alt="" class="dropdown-icon dropdown-icon-mono"> Edit note…</button>`;
      dropdownItems += `<button class="dropdown-item danger"${dataAttrs({ action: "confirm-remove", id: a.id })}>Untrack</button>`;
      
      return `
        <div class="artist-item">
          <div class="artist-identity" title="${esc(a.name)}${a.disambiguation ? ` — ${esc(a.disambiguation)}` : ""}">
            <span class="artist-name">${esc(a.name)}</span>
            ${a.disambiguation ? `<span class="artist-disambig"> — ${esc(a.disambiguation)}</span>` : ""}
          </div>
          <div class="artist-actions">
            <div class="artist-source-icons">${sourceIcons}</div>
            <div class="artist-manage-wrapper">
              <button class="manage-btn"${dataAttrs({ action: "toggle-artist-menu" })} title="Manage artist">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
              </button>
              <div class="artist-dropdown hidden">${dropdownItems}</div>
            </div>
          </div>
        </div>
      `;
}

// --- Artist dropdown menu ---
function toggleArtistMenu(btn) {
  const wrapper = btn.closest('.artist-manage-wrapper');
  const dropdown = wrapper.querySelector('.artist-dropdown');
  const wasHidden = dropdown.classList.contains('hidden');
  
  // Close all other dropdowns first
  document.querySelectorAll('.artist-dropdown:not(.hidden)').forEach(d => d.classList.add('hidden'));
  
  if (wasHidden) {
    dropdown.classList.remove('hidden');
  }
}

// Close dropdown when clicking outside
document.addEventListener('click', e => {
  if (!e.target.closest('.artist-manage-wrapper')) {
    document.querySelectorAll('.artist-dropdown:not(.hidden)').forEach(d => d.classList.add('hidden'));
  }
});

function _artistMenuOf(btn) {
  // The dropdown belonging to the clicked item. Falls back to any open
  // dropdown so callers never silently do nothing.
  const own = btn && btn.closest ? btn.closest('.artist-dropdown') : null;
  if (own) return [own];
  return [...document.querySelectorAll('.artist-dropdown:not(.hidden)')];
}

function _artistName(id) {
  const a = (_artistsCache || []).find(x => String(x.id) === String(id));
  return a ? a.name : "this artist";
}

function confirmRemoveArtist(id, btn) {
  const name = _artistName(id);
  _artistMenuOf(btn).forEach(d => {
    d.classList.remove('hidden');
    d.innerHTML = `<div class="inline-confirm">Untrack “${esc(name)}”? <button class="confirm-yes"${dataAttrs({ action: "do-remove-artist", id: id })}>Confirm</button> <button class="confirm-no"${dataAttrs({ action: "load-artists" })}>Keep</button></div>`;
  });
}

function doRemoveArtist(id) {
  apiRaw(`/api/artists/${id}`, { method: "DELETE", allowError: true }).then(() => {
    showToast("Artist untracked");
    loadArtists();
    _refreshArtistsCache();
    loadReleases();
  });
}

function confirmUnlinkArtist(id, source, btn) {
  const sourceLabel = source === 'musicbrainz' ? 'MusicBrainz' : source === 'itunes' ? 'Apple Music' : 'SoundCloud';
  _artistMenuOf(btn).forEach(d => {
    d.classList.remove('hidden');
    d.innerHTML = `<div class="inline-confirm">Unlink ${sourceLabel}? <button class="confirm-yes"${dataAttrs({ action: "do-unlink-artist", id: id, source: source })}>Confirm</button> <button class="confirm-no"${dataAttrs({ action: "load-artists" })}>Keep</button></div>`;
  });
}

function doUnlinkArtist(id, source) {
  apiRaw(`/api/artists/${id}/unlink-${source}`, { method: "POST", allowError: true }).then(() => {
    showToast("Source unlinked");
    loadArtists();
    _refreshArtistsCache();
    loadReleases();
  });
}

function checkSingleArtist(id) {
  document.querySelectorAll('.artist-dropdown:not(.hidden)').forEach(d => d.classList.add('hidden'));
  const a = (_artistsCache || []).find(x => String(x.id) === String(id));
  activeArtistFilterId = String(id);
  filterArtistBtn.textContent = a ? a.name : "All Artists";
  updateResetFiltersBtn();
  if (location.hash !== "#/feed") location.hash = "#/feed";
  openCheckPopup();
}

function editDisambiguation(id, btn) {
  const a = (_artistsCache || []).find(x => String(x.id) === String(id));
  const current = a ? (a.disambiguation || "") : "";
  const safe = esc(current).replace(/"/g, "&quot;");
  _artistMenuOf(btn).forEach(d => {
    d.classList.remove('hidden');
    d.innerHTML = `<div class="inline-edit">
      <input type="text" id="disambig-edit-input" value="${safe}" placeholder="Add a note..." maxlength="200" aria-label="Artist note">
      <div class="edit-btn-row">
        <button class="confirm-yes"${dataAttrs({ action: "save-disambiguation", id: id })}>Save</button>
        <button class="confirm-no"${dataAttrs({ action: "load-artists" })}>Cancel</button>
      </div>
    </div>`;
    const input = d.querySelector('#disambig-edit-input');
    input.focus();
    input.select();
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') saveDisambiguation(id);
      if (e.key === 'Escape') loadArtists();
    });
  });
}

async function saveDisambiguation(id) {
  const input = document.getElementById('disambig-edit-input');
  const value = input ? input.value : "";
  try {
    const data = await api(`/api/artists/${id}/disambiguation`, {
      method: "POST",
      json: { disambiguation: value },
    });
    if (data.status !== "ok") throw new Error(data.message || "save failed");
    showToast(value.trim() ? "Note saved" : "Note cleared");
  } catch {
    showToast("Could not save note");
  }
  loadArtists();
  _refreshArtistsCache();
}

// --- Check Now Popup ---
const checkBtn = document.getElementById("check-btn");
const checkPopup = document.getElementById("check-popup");
const popupTitle = document.getElementById("popup-title");
const checkProgressBar = document.getElementById("check-progress-bar");
const checkProgressText = document.getElementById("check-progress-text");
const checkProgressDetail = document.getElementById("check-progress-detail");
const summaryBody = document.getElementById("summary-body");
const summaryActions = document.getElementById("summary-actions");
const pauseBtn = document.getElementById("pause-btn");
let lastSummary = [];

function parseProgressMessage(msg) {
  // Server: "Checking 2 source(s) (MB, ITUNES) - artist 7 of 24: Name..."
  const m = /artist\s+(\d+)\s+of\s+(\d+):\s*(.*)/i.exec(msg || "");
  if (m) return { counter: `Artist ${m[1]} of ${m[2]}`, detail: m[3] || "" };
  return { counter: "", detail: msg || "" };
}

let scanRunning = false;
let scanPaused = false;
let currentSkip = 0; // How many artists have been fully checked (for resume)
let activeSource = null; // Current EventSource connection
let partialSummary = []; // Accumulated results across pause/resume cycles
let scanStartTime = 0;
let scanArtistFilter = "";
let lastFailed = [];

function retryFailed() {
  if (!lastFailed.length || scanRunning) return;
  // Re-run full check; server will quickly pass clean artists, failed ones retried
  runCheck(0);
}

document.getElementById("markallasseen").addEventListener("click", markAllReleasesSeen);
checkBtn.addEventListener("click", toggleCheckPopup);

// Click outside to close — only when idle (not scanning)
document.addEventListener("click", e => {
  const visible = !checkPopup.classList.contains("hidden");
  if (visible && !scanRunning) {
    if (!checkBtn.contains(e.target) && !checkPopup.contains(e.target)) {
      window.closeCheckPopup();
    }
  }
});

function toggleCheckPopup() {
  const hidden = checkPopup.classList.contains("hidden");

  if (hidden) {
    openCheckPopup();
  } else {
    closeCheckPopup(); // Minimize — scan keeps running in background
  }
}

function openCheckPopup() {
  const filtersEl = document.querySelector(".filters");
  const scrollY = window.scrollY || window.pageYOffset;
  const btnRect = checkBtn.getBoundingClientRect();
  const filterRect = filtersEl.getBoundingClientRect();

  // Position below the button, right-aligned to avoid obscuring left-side UI
  const top = btnRect.bottom - filterRect.top + scrollY + 8;

  checkPopup.style.top = `${top}px`;
  checkPopup.style.right = "0";
  checkPopup.style.left = "auto";
  checkPopup.style.transform = "none";
  checkPopup.classList.remove("hidden");

  // Start scan if not already running (and not paused mid-scan)
  if (!scanRunning && !scanPaused) runCheck();
}

function closeCheckPopup() {
  checkPopup.classList.add("hidden");
}

window.closeCheckPopup = closeCheckPopup;

// Pause: closes SSE so server stops processing, keeps state for resume
window.togglePauseCheck = function () {
  if (activeSource && !scanPaused) {
    // PAUSE
    scanPaused = true;
    activeSource.close();
    activeSource = null;

    pauseBtn.textContent = "▶ Resume";
    pauseBtn.classList.add("resuming");
    popupTitle.textContent = `Paused at artist ${currentSkip}`;
  } else if (scanPaused) {
    // RESUME — reconnect with skip param to continue where we left off
    scanPaused = false;

    pauseBtn.textContent = "⏸ Pause";
    pauseBtn.classList.remove("resuming");
    runCheck(currentSkip);
  }
};

function runCheck(skip = 0) {
  if (activeSource) activeSource.close(); // Close any lingering connection

  scanRunning = true;
  currentSkip = skip;
  scanStartTime = Date.now();
  // Snapshot filter at scan start so mid-scan filter changes don't corrupt resume
  scanArtistFilter = activeArtistFilterId || "";
  lastFailed = [];

  // Build URL with artist_id if a specific artist is selected
  let url = `/api/check?skip=${skip}`;
  if (scanArtistFilter) {
    url += `&artist_id=${scanArtistFilter}`;
    const name = _getArtistName(scanArtistFilter);
    checkBtn.innerHTML = `<span class="check-spinner" aria-hidden="true"></span> Checking ${esc(name || "")}…`;
  } else {
    checkBtn.innerHTML = `<span class="check-spinner" aria-hidden="true"></span> Checking…`;
  }

  popupTitle.textContent = `Starting check…`;

  summaryBody.innerHTML = "";
  summaryBody.classList.add("hidden");
  const failedBody = document.getElementById("failed-body");
  if (failedBody) { failedBody.innerHTML = ""; failedBody.classList.add("hidden"); }
  if (summaryActions) summaryActions.classList.add("hidden");
  if (checkProgressDetail) checkProgressDetail.textContent = "";
  checkProgressBar.style.width = "0%";
  checkProgressBar.classList.remove("done");

  pauseBtn.classList.remove("hidden");
  pauseBtn.textContent = "⏸ Pause";
  pauseBtn.classList.remove("resuming");

  const source = new EventSource(url);
  activeSource = source;

  // Compute total artists for progress bar (fetch once)
  let totalArtists = null;

  source.onmessage = event => {
    const data = JSON.parse(event.data);

    if (data.type === "progress") {
      if (data.total && !totalArtists) totalArtists = data.total - skip; // remaining artists
      // `completed` counts finished artists (drives the bar); `resume` is the
      // contiguous checkpoint pause/resume restarts from. Fall back to the
      // legacy `current` field if an older server omits them.
      const done = typeof data.completed === "number" ? data.completed : data.current;
      if (typeof data.resume === "number") currentSkip = data.resume;
      else if (typeof data.current === "number") currentSkip = data.current;
      if (typeof done === "number" && totalArtists) {
        const pct = Math.round(((skip + done) / (skip + totalArtists)) * 100);
        checkProgressBar.style.width = pct + "%";

        // Live percentage on button too
        checkBtn.textContent = `${pct}%`;
        const elapsed = Math.round((Date.now() - scanStartTime) / 1000);
        popupTitle.textContent = `Checking… ${pct}% · ${elapsed}s`;
      }
      const parsed = parseProgressMessage(data.message);
      if (checkProgressDetail) {
        checkProgressDetail.textContent = parsed.counter;
        checkProgressDetail.title = parsed.counter;
      }
      checkProgressText.textContent = parsed.detail || data.message;
      checkProgressText.title = parsed.detail || data.message;
      checkProgressText.className = "check-progress-text";
    } else if (data.type === "warning") {
      // Per-artist failure (e.g. timeout / rate limit after retries).
      // Show it, but let the scan continue.
      checkProgressText.textContent = data.message;
      checkProgressText.className = "check-progress-text error";
    } else if (data.type === "error") {
      scanRunning = false;
      source.close();
      activeSource = null;
      pauseBtn.classList.add("hidden");
      _updateCheckBtn();
      checkProgressText.textContent = data.message;
      checkProgressText.className = "check-progress-text error";
    } else if (data.type === "done") {
      scanRunning = false;
      source.close();
      activeSource = null;
      pauseBtn.classList.add("hidden");
      playChime();

      // Merge partial results from this run with earlier runs
      const mergedSummary = [...partialSummary];
      for (const s of data.summary) {
        const existing = mergedSummary.find(m => m.artist === s.artist);
        if (existing) {
          existing.new_releases.push(...s.new_releases);
        } else {
          mergedSummary.push(s);
        }
      }

      // Update button text to reflect current filter state
      _updateCheckBtn();
      
      popupTitle.textContent = "Done!";
      checkProgressBar.style.width = "100%";
      checkProgressBar.classList.add("done");

      const totalNew = mergedSummary.reduce((sum, s) => sum + s.new_releases.length, 0);
      const failedNames = Array.isArray(data.failed) ? data.failed : [];
      lastFailed = failedNames;
      const failedCount = failedNames.length;
      const elapsed = Math.round((Date.now() - scanStartTime) / 1000);
      checkProgressText.textContent =
        `Done in ${elapsed}s — ${totalNew} new release(s).` +
        (failedCount > 0 ? ` ${failedCount} failed.` : "");
      checkProgressText.className = "check-progress-text" + (failedCount > 0 ? " error" : " done");
      const failedBody = document.getElementById("failed-body");
      if (failedBody) {
        if (failedCount > 0) {
          failedBody.innerHTML = `Could not check: ${failedNames.map(n => esc(n)).join(", ")}`;
          failedBody.classList.remove("hidden");
        } else {
          failedBody.innerHTML = "";
          failedBody.classList.add("hidden");
        }
      }
      const retryBtn = document.getElementById("retry-failed-btn");
      if (retryBtn) retryBtn.style.display = failedCount > 0 ? "" : "none";

      if (mergedSummary.length > 0) {
        lastSummary = mergedSummary;
        summaryBody.innerHTML = renderSummary(mergedSummary);
        summaryBody.classList.remove("hidden");
        if (summaryActions) summaryActions.classList.remove("hidden");
      } else {
        lastSummary = [];
        if (summaryActions) summaryActions.classList.add("hidden");
      }

      // Reset state for next scan
      partialSummary = [];
      currentSkip = 0;

      updateUnseenBadge();
      loadReleases();
    }
  };

  source.onerror = () => {
    if (!scanPaused) { // Don't error out when we intentionally closed it (pause)
      scanRunning = false;
      activeSource = null;
      pauseBtn.classList.add("hidden");
      _updateCheckBtn();
      popupTitle.textContent = "Error";
      checkProgressText.textContent = "Connection lost.";
      checkProgressText.className = "check-progress-text error";
    }
  };
}

// --- Completion chime ---
// Tiny WAV (~13KB) played when a check finishes. Toggle persists in localStorage.
const chimeAudio = new Audio("/static/assets/chime.wav");
chimeAudio.preload = "auto";
chimeAudio.volume = 0.25; // gentle notification level, not a jumpscare
let soundEnabled = localStorage.getItem("mrt_sound_enabled") !== "0"; // default: on

const soundToggle = document.getElementById("sound-enabled");
soundToggle.checked = soundEnabled;
soundToggle.addEventListener("change", () => {
  soundEnabled = soundToggle.checked;
  localStorage.setItem("mrt_sound_enabled", soundEnabled ? "1" : "0");
});

function playChime() {
  if (!soundEnabled) return;
  try {
    chimeAudio.currentTime = 0;
    chimeAudio.play().catch(() => {}); // ignore autoplay-policy rejections
  } catch { /* ignore */ }
}

function renderSummary(summary) {
  return html`
    <h3>New Releases Found</h3>
    ${summary.map(s => html`
      <div class="summary-artist">
        <button class="summary-artist-name"${raw(dataAttrs({ action: "jump-to-artist", artist: s.artist }))}>${s.artist}</button>
        <ul>${s.new_releases.map(r => html`<li>${r}</li>`)}</ul>
      </div>
    `)}
  `;
}

function jumpToArtistFromSummary(artistName) {
  const match = (_artistsCache || []).find(a => a.name === artistName);
  if (match) {
    closeCheckPopup();
    selectArtist(String(match.id));
    if (location.hash !== "#/feed") location.hash = "#/feed";
  }
}

function viewNewResults() {
  closeCheckPopup();
  filterUnseen.checked = true;
  document.getElementById("filter-hidden").checked = false;
  updateResetFiltersBtn();
  if (location.hash !== "#/feed") location.hash = "#/feed";
  loadReleases();
}

// --- Utilities ---
// (esc / raw / html are defined near the top of the file with the helpers.)

// --- Export / Import Artists ---
async function exportArtists() {
  const resp = await apiRaw("/api/artists/export");
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "music-release-tracker-artists.json";
  a.click();
  URL.revokeObjectURL(url);
  showToast("Artists exported");
}

document.getElementById("import-btn").addEventListener("click", () => {
  document.getElementById("import-file-input").click();
});

document.getElementById("import-file-input").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  const statusEl = document.getElementById("import-status");
  if (!file) return;
  const importBtn = document.getElementById("import-btn");
  importBtn.disabled = true;

  const form = new FormData();
  form.append("file", file);

  try {
    const result = await api("/api/artists/import", { method: "POST", body: form });

    if (result.status === "error") {
      statusEl.className = "import-status error";
      statusEl.textContent = `Import failed: ${result.message}`;
      statusEl.classList.remove("hidden");
    } else {
      const errs = (result.errors || []).slice(0, 5).map(e => `<li>${esc(e)}</li>`).join("");
      statusEl.className = "import-status";
      statusEl.innerHTML = `Import complete — added ${result.added}, skipped ${result.skipped}.`
        + (errs ? `<ul>${errs}</ul>` : "")
        + ((result.errors || []).length > 5 ? `<div>+${result.errors.length - 5} more errors</div>` : "");
      statusEl.classList.remove("hidden");
      showToast(`Import: +${result.added} artists`);
      loadArtists();
      _refreshArtistsCache();
    }
  } catch (err) {
    statusEl.className = "import-status error";
    statusEl.textContent = `Import failed: ${err.message}`;
    statusEl.classList.remove("hidden");
  } finally {
    importBtn.disabled = false;
  }

  // Reset file input so the same file can be re-imported
  event.target.value = "";
});

// --- Logs Panel ---
const logsPanel = document.getElementById("logs-panel");
const logsContainer = document.getElementById("logs-container");
const panelScrim = document.getElementById("panel-scrim");
let logsAutoScroll = true;
let logsRefreshTimer = null;
const LOGS_REFRESH_INTERVAL = 2000; // 2 seconds
let activeLogLevel = "ALL";
let _lastFocusedEl = null;

function updateScrim() {
  const anyOpen = !logsPanel.classList.contains("hidden") || !settingsPanel.classList.contains("hidden");
  panelScrim.classList.toggle("hidden", !anyOpen);
}

function closeSidePanels() {
  logsPanel.classList.add("hidden");
  settingsPanel.classList.add("hidden");
  stopLogsRefresh();
  updateScrim();
  if (_lastFocusedEl) _lastFocusedEl.focus();
}
window.closeSidePanels = closeSidePanels;

document.getElementById("logs-btn").addEventListener("click", toggleLogsPanel);

function toggleLogsPanel() {
  const hidden = logsPanel.classList.contains("hidden");
  if (hidden) {
    _lastFocusedEl = document.activeElement;
    settingsPanel.classList.add("hidden");
    logsPanel.classList.remove("hidden");
    updateScrim();
    startLogsRefresh();
    logsPanel.querySelector(".popup-close-btn").focus();
  } else {
    logsPanel.classList.add("hidden");
    stopLogsRefresh();
    updateScrim();
  }
}

function setLogLevel(btn) {
  activeLogLevel = btn.dataset.logLevel;
  document.querySelectorAll(".log-filters .chip").forEach(c => c.classList.remove("active"));
  btn.classList.add("active");
  loadLogs();
}

function startLogsRefresh() {
  stopLogsRefresh(); // Clear any existing timer
  loadLogs(); // Load immediately
  logsRefreshTimer = setInterval(() => {
    if (document.hidden) return;
    if (document.getElementById("logs-pause").checked) return;
    loadLogs();
  }, LOGS_REFRESH_INTERVAL);
}

function stopLogsRefresh() {
  if (logsRefreshTimer) {
    clearInterval(logsRefreshTimer);
    logsRefreshTimer = null;
  }
}

async function loadLogs() {
  try {
    const logs = await api("/api/logs?limit=200");
    const q = (document.getElementById("logs-search").value || "").toLowerCase();
    const filtered = logs.filter(l =>
      (activeLogLevel === "ALL" || l.level === activeLogLevel || (activeLogLevel === "WARN" && l.level === "WARNING")) &&
      (!q || `${l.message} ${l.artist || ""} ${l.detail || ""}`.toLowerCase().includes(q))
    );
    const newHtml = filtered.map(l => renderLogEntry(l)).join("") || '<p class="status-msg">No logs match.</p>';
    // Only update if content changed to avoid unnecessary DOM work
    if (logsContainer.dataset.html !== newHtml) {
      logsContainer.dataset.html = newHtml;
      logsContainer.innerHTML = newHtml;
      if (logsAutoScroll) {
        logsContainer.scrollTop = logsContainer.scrollHeight;
      }
    }
  } catch {
    // silent fail
  }
}

function renderLogEntry(log) {
  const parts = log.timestamp.split("T");
  const time = parts[1] ? parts[1].slice(0, 8) : "";
  const artistPart = log.artist ? html`<span class="log-artist">[${log.artist}]</span>` : "";
  return html`
    <div class="log-entry">
      <span class="log-timestamp">${time}</span>
      <span class="log-level ${log.level}">${log.level}</span>
      ${artistPart}
      <span class="log-message">${log.message}</span>
      ${log.detail ? html`<span class="log-message"> — ${log.detail}</span>` : ""}
    </div>
  `;
}

async function clearLogs() {
  try {
    await apiRaw("/api/logs/clear", { method: "POST", allowError: true });
    logsContainer.innerHTML = "";
  } catch {
    // silent fail
  }
}

// Auto-scroll when user scrolls to bottom
logsContainer.addEventListener("scroll", () => {
  const atBottom = logsContainer.scrollHeight - logsContainer.scrollTop - logsContainer.clientHeight < 50;
  logsAutoScroll = atBottom;
});

// --- Settings Panel ---
const settingsPanel = document.getElementById("settings-panel");
const settingsBtn = document.getElementById("settings-btn");
const settingsStatus = document.getElementById("settings-status");

settingsBtn.addEventListener("click", toggleSettingsPanel);

function toggleSettingsPanel() {
  const hidden = settingsPanel.classList.contains("hidden");
  if (hidden) {
    _lastFocusedEl = document.activeElement;
    logsPanel.classList.add("hidden");
    stopLogsRefresh();
    settingsPanel.classList.remove("hidden");
    updateScrim();
    loadSettings();
    settingsPanel.querySelector(".popup-close-btn").focus();
  } else {
    settingsPanel.classList.add("hidden");
    updateScrim();
  }
}

// Close settings when clicking outside
document.addEventListener("click", e => {
  if (!settingsPanel.classList.contains("hidden")) {
    if (!settingsPanel.contains(e.target) && !settingsBtn.contains(e.target)) {
      settingsPanel.classList.add("hidden");
      updateScrim();
    }
  }
});

async function loadSettings() {
  try {
    const settings = await api("/api/settings");
    // Default: all sources enabled, dedup off (first run has no settings)
    document.getElementById("source-musicbrainz").checked = settings.source_musicbrainz !== "0";
    document.getElementById("source-itunes").checked = settings.source_itunes !== "0";
    document.getElementById("source-soundcloud").checked = settings.source_soundcloud !== "0";
    document.getElementById("setting-startup-refresh").checked = settings.startup_refresh === "1";
    document.getElementById("setting-startup-dedup").checked = settings.startup_dedup === "1";
  } catch {
    // On error, default to all sources enabled
    document.getElementById("source-musicbrainz").checked = true;
    document.getElementById("source-itunes").checked = true;
    document.getElementById("source-soundcloud").checked = true;
    document.getElementById("setting-startup-refresh").checked = false;
    document.getElementById("setting-startup-dedup").checked = false;
  }
  loadDedupRules();
  loadExplicitMap();
}

// --- Title ignore rules (dedup) ---
let _dedupRules = [];

async function loadDedupRules() {
  const listEl = document.getElementById("dedup-list");
  try {
    const data = await api("/api/settings/dedup-ignores");
    _dedupRules = Array.isArray(data.ignores) ? data.ignores : [];
  } catch {
    _dedupRules = [];
  }
  renderDedupList();
}

function renderDedupList() {
  const listEl = document.getElementById("dedup-list");
  if (!listEl) return;
  if (!_dedupRules.length) {
    listEl.innerHTML = '<p class="dedup-empty">No rules — every title difference counts as a different release.</p>';
    return;
  }
  listEl.innerHTML = html`${_dedupRules.map((rule, i) => html`
    <div class="dedup-card">
      <span class="dedup-text" title="${rule}"><span class="dedup-quote">“</span>${rule}<span class="dedup-quote">”</span></span>
      <button class="dedup-remove"${raw(dataAttrs({ action: "remove-dedup-rule", index: i }))} aria-label="Remove ignore rule ${rule}" title="Remove">✕</button>
    </div>
  `)}`;
}

async function persistDedupRules(statusMsg) {
  const statusEl = document.getElementById("dedup-rules-status");
  try {
    const data = await api("/api/settings/dedup-ignores", {
      method: "PUT",
      json: { ignores: _dedupRules },
    });
    if (data.status !== "ok") throw new Error(data.message || "save failed");
    _dedupRules = data.ignores;
    renderDedupList();
    if (statusEl) {
      statusEl.textContent = statusMsg || "Saved ✓ — run Dedup to apply";
      statusEl.className = "settings-status success";
      clearTimeout(persistDedupRules._t);
      persistDedupRules._t = setTimeout(() => { statusEl.textContent = ""; statusEl.className = "settings-status"; }, 2500);
    }
  } catch (err) {
    if (statusEl) {
      statusEl.textContent = "Error saving rules";
      statusEl.className = "settings-status error";
    }
  }
}

function addDedupRule() {
  const input = document.getElementById("dedup-input");
  const raw = input.value || "";
  if (!raw.trim()) return;
  // Preserve leading space (' - Single' needs it); drop trailing whitespace only
  const value = raw.replace(/\s+$/, "");
  if (_dedupRules.some(r => r.trim().toLowerCase() === value.trim().toLowerCase())) {
    input.select();
    return;
  }
  _dedupRules.push(value);
  input.value = "";
  input.focus();
  persistDedupRules("Added ✓ — run Dedup to apply");
}

function removeDedupRule(idx) {
  _dedupRules.splice(idx, 1);
  persistDedupRules("Removed ✓ — run Dedup to apply");
}

document.getElementById("dedup-input").addEventListener("keydown", e => {
  if (e.key === "Enter") addDedupRule();
});

// --- Explicit word mappings (censored -> clean) ---
let _explicitMap = [];

async function loadExplicitMap() {
  try {
    const data = await api("/api/settings/explicit-map");
    _explicitMap = Array.isArray(data.mappings) ? data.mappings : [];
  } catch {
    _explicitMap = [];
  }
  renderExplicitMap();
}

function renderExplicitMap() {
  const listEl = document.getElementById("explicit-list");
  if (!listEl) return;
  if (!_explicitMap.length) {
    listEl.innerHTML = '<p class="dedup-empty">No mappings — censored and explicit titles compare as different releases.</p>';
    return;
  }
  listEl.innerHTML = html`${_explicitMap.map((pair, i) => html`
    <div class="dedup-card">
      <span class="dedup-text" title="${pair[0]} → ${pair[1]}"><span class="dedup-quote">“</span>${pair[0]}<span class="dedup-quote">”</span> <span class="dedup-map-arrow">→</span> <span class="dedup-quote">“</span>${pair[1]}<span class="dedup-quote">”</span></span>
      <button class="dedup-remove"${raw(dataAttrs({ action: "remove-explicit-map", index: i }))} aria-label="Remove mapping ${pair[0]}" title="Remove">✕</button>
    </div>
  `)}`;
}

async function persistExplicitMap(statusMsg) {
  const statusEl = document.getElementById("explicit-status");
  try {
    const data = await api("/api/settings/explicit-map", {
      method: "PUT",
      json: { mappings: _explicitMap },
    });
    if (data.status !== "ok") throw new Error(data.message || "save failed");
    _explicitMap = data.mappings;
    renderExplicitMap();
    if (statusEl) {
      statusEl.textContent = statusMsg || "Saved ✓ — run Dedup to apply";
      statusEl.className = "settings-status success";
      clearTimeout(persistExplicitMap._t);
      persistExplicitMap._t = setTimeout(() => { statusEl.textContent = ""; statusEl.className = "settings-status"; }, 2500);
    }
  } catch (err) {
    if (statusEl) {
      statusEl.textContent = "Error saving mappings";
      statusEl.className = "settings-status error";
    }
  }
}

function addExplicitMapRule() {
  const censoredEl = document.getElementById("explicit-censored-input");
  const cleanEl = document.getElementById("explicit-clean-input");
  const censored = (censoredEl.value || "").trim();
  const clean = (cleanEl.value || "").trim();
  const statusEl = document.getElementById("explicit-status");
  if (!censored || !clean) return;
  if (!censored.includes("*")) {
    if (statusEl) {
      statusEl.textContent = "Censored form needs a * wildcard";
      statusEl.className = "settings-status error";
    }
    censoredEl.focus();
    return;
  }
  if (clean.includes("*")) {
    if (statusEl) {
      statusEl.textContent = "Clean word must not contain *";
      statusEl.className = "settings-status error";
    }
    cleanEl.focus();
    return;
  }
  if (_explicitMap.some(p => p[0].toLowerCase() === censored.toLowerCase())) {
    censoredEl.select();
    return;
  }
  _explicitMap.push([censored, clean]);
  censoredEl.value = "";
  cleanEl.value = "";
  censoredEl.focus();
  persistExplicitMap("Added ✓ — run Dedup to apply");
}

function removeExplicitMapRule(idx) {
  _explicitMap.splice(idx, 1);
  persistExplicitMap("Removed ✓ — run Dedup to apply");
}

document.getElementById("explicit-censored-input").addEventListener("keydown", e => {
  if (e.key === "Enter") addExplicitMapRule();
});
document.getElementById("explicit-clean-input").addEventListener("keydown", e => {
  if (e.key === "Enter") addExplicitMapRule();
});

async function saveSettings(auto = false) {
  const settings = {
    source_musicbrainz: document.getElementById("source-musicbrainz").checked ? "1" : "0",
    source_itunes: document.getElementById("source-itunes").checked ? "1" : "0",
    source_soundcloud: document.getElementById("source-soundcloud").checked ? "1" : "0",
    startup_refresh: document.getElementById("setting-startup-refresh").checked ? "1" : "0",
    startup_dedup: document.getElementById("setting-startup-dedup").checked ? "1" : "0",
  };

  try {
    await apiRaw("/api/settings", { method: "POST", json: settings });
    settingsStatus.textContent = auto ? "Saved ✓" : "Saved";
    settingsStatus.className = "settings-status success";
    clearTimeout(saveSettings._t);
    saveSettings._t = setTimeout(() => { settingsStatus.textContent = ""; settingsStatus.className = "settings-status"; }, 2000);
    // Source toggles affect the Artists tab immediately — refresh icons/pills
    await refreshEnabledSources();
    if (typeof loadArtists === "function") loadArtists();
    if (_lastSearchResults.length && typeof renderSearchPage === "function") renderSearchPage();
  } catch (err) {
    settingsStatus.textContent = "Error saving";
    settingsStatus.className = "settings-status error";
  }
}

async function runDedup() {
  const status = document.getElementById("dedup-status");
  status.textContent = "Running...";
  status.className = "settings-status";
  try {
    const data = await api("/api/dedup", { method: "POST" });
    const hidden = data.hidden || 0;
    const restored = data.restored || 0;
    const totalHidden = data.total_hidden || 0;
    let detail = `Hidden ${hidden} duplicate(s) this run · ${totalHidden} total hidden`;
    if (restored) detail += ` · restored ${restored}`;
    status.textContent = detail;
    status.className = "settings-status success";
    showToast(`Dedup: ${detail}`, hidden ? "View feed" : null, hidden ? () => { location.hash = "#/feed"; loadReleases(); } : null);
    loadReleases();
    setTimeout(() => { status.textContent = ""; status.className = "settings-status"; }, 3000);
  } catch (err) {
    status.textContent = "Error";
    status.className = "settings-status error";
  }
}

// --- Link Source Modal ---
const linkSourceModal = document.getElementById("link-source-modal");
const linkSourceTitle = document.getElementById("link-source-title");
const linkSourceInput = document.getElementById("link-source-input");
const linkSourceSearchBtn = document.getElementById("link-source-search-btn");
const linkSourceResults = document.getElementById("link-source-results");

let currentLinkArtistId = null;
let currentLinkSource = "";

linkSourceSearchBtn.addEventListener("click", () => searchLinkSource());
linkSourceInput.addEventListener("keydown", e => { if (e.key === "Enter") searchLinkSource(); });

function openLinkSourceModal(artistId, artistName, source) {
  currentLinkArtistId = artistId;
  currentLinkSource = source;

  const label = source === "musicbrainz" ? "MusicBrainz" : source === "itunes" ? "Apple Music" : source === "soundcloud" ? "SoundCloud" : "Source";
  linkSourceTitle.textContent = `Link ${label} for ${artistName}`;
  document.getElementById("link-source-hint").textContent =
    source === "soundcloud"
      ? "Paste a SoundCloud URL or permalink, then Search."
      : `Showing matches for “${artistName}” — refine or paste a link, then Search.`;

  // Always visible input, pre-filled with artist name (or guessed SC URL)
  if (source === "soundcloud") {
    const slug = artistName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    linkSourceInput.value = `https://soundcloud.com/${slug}`;
  } else {
    linkSourceInput.value = artistName;
  }
  linkSourceInput.placeholder = 'Artist name or link...';
  linkSourceResults.innerHTML = '<p class="status-msg">Searching...</p>';
  linkSourceModal.classList.remove("hidden");
  linkSourceInput.focus();
  linkSourceInput.select();
  searchLinkSource(linkSourceInput.value.trim());
}

function closeLinkSourceModal() {
  linkSourceModal.classList.add("hidden");
  currentLinkArtistId = null;
  currentLinkSource = "";
}

// Close modal when clicking outside
linkSourceModal.addEventListener("click", (e) => {
  if (e.target === linkSourceModal) {
    closeLinkSourceModal();
  }
});

async function searchLinkSource(query) {
  query = query || linkSourceInput.value.trim();
  if (!query) return;
  linkSourceResults.innerHTML = '<p class="status-msg">Searching...</p>';

  try {
    const results = await api("/api/artists/search", {
      method: "POST",
      json: {
        query,
        source: currentLinkSource === "musicbrainz" ? "musicbrainz" :
                currentLinkSource === "itunes" ? "itunes" :
                currentLinkSource === "soundcloud" ? "soundcloud" : ""
      },
      allowError: true,
    });
    if (!Array.isArray(results)) {
      linkSourceResults.innerHTML = `<p class="status-msg error">Search failed. <button class="link-btn"${dataAttrs({ action: "search-link-source" })}>Retry</button></p>`;
      return;
    }

    if (results.length === 0) {
      linkSourceResults.innerHTML = '<p class="status-msg">No artists found. Check spelling or paste a direct link.</p>';
    } else {
      linkSourceResults.innerHTML = `<p class="status-msg">Found ${results.length} — showing ${Math.min(results.length, 10)}</p>` + results.slice(0, 10).map(a => {
        let artistImage = a.artistImageUrl || '';
        if (!artistImage && a.itunes_artist_id) artistImage = '/static/assets/apple-music.svg';
        const imageHtml = artistImage ? `<img class="result-image ${a.itunes_artist_id && !a.artistImageUrl ? 'result-image-icon' : ''}" src="${esc(artistImage)}" alt="" data-hide-on-error">` : '';
        return `
          <div class="search-result-item">
            <div class="result-info">
              ${imageHtml}
              <div class="result-text">
                <div class="result-name">${esc(a.name)}</div>
                <div class="result-detail" title="${esc(resultDetailLine(a))}">${esc(resultDetailLine(a))}</div>
                ${searchResultPills(a)}
              </div>
            </div>
            <button class="add-btn"${dataAttrs({ action: "link-source", key: registerRow(a) })}>Link</button>
          </div>
        `;
      }).join("");
      enhanceItunesCounts(results.slice(0, 10), linkSourceResults);
    }
  } catch (err) {
    linkSourceResults.innerHTML = `<p class="status-msg error">Search failed: ${esc(err.message)} <button class="link-btn"${dataAttrs({ action: "search-link-source" })}>Retry</button></p>`;
  }
}

async function linkSource(artist, btn, force = false) {
  btn.disabled = true;
  btn.textContent = force ? "Replacing..." : "Linking...";

  try {
    // Determine which source to link
    let sourceToLink = null;
    let idToLink = null;

    if (currentLinkSource === "musicbrainz" && artist.mbid) {
      sourceToLink = "musicbrainz";
      idToLink = artist.mbid;
    } else if (currentLinkSource === "itunes" && artist.itunes_artist_id) {
      sourceToLink = "itunes";
      idToLink = String(artist.itunes_artist_id);
    } else if (currentLinkSource === "soundcloud" && artist.soundcloud_permalink) {
      sourceToLink = "soundcloud";
      idToLink = artist.soundcloud_permalink;
    } else if (currentLinkSource === "both") {
      // Link both if available
      if (artist.mbid) {
        sourceToLink = "musicbrainz";
        idToLink = artist.mbid;
      } else if (artist.itunes_artist_id) {
        sourceToLink = "itunes";
        idToLink = String(artist.itunes_artist_id);
      } else if (artist.soundcloud_permalink) {
        sourceToLink = "soundcloud";
        idToLink = artist.soundcloud_permalink;
      }
    }

    if (!sourceToLink || !idToLink) {
      btn.textContent = "No valid source";
      btn.disabled = false;
      return;
    }

    const payload = {
      source: sourceToLink,
      id: idToLink,
      name: artist.name,
      disambiguation: artist.disambiguation
    };
    // Explicit link target: the tracked row the modal was opened from.
    // The backend links onto it directly instead of name-matching, so a
    // SoundCloud display name ("Avicii Official") never spawns a new entry
    // next to the tracked name ("Avicii").
    if (sourceToLink === "soundcloud" && currentLinkArtistId != null) {
      payload.artist_id = currentLinkArtistId;
      if (force) payload.force = true;
    }

    const result = await api("/api/artists", {
      method: "POST",
      json: payload
    });

    if (result.status === "conflict") {
      const owner = (result.conflicting_artist && result.conflicting_artist.name)
        || (result.message || "").replace(/^already linked to\s*/i, "")
        || "another artist";
      showLinkConflict(btn, artist, `already linked to ${owner}`);
      return;
    }

    if (result.status === "already_exists") {
      btn.textContent = "Already linked";
    } else if (result.status === "linked") {
      btn.textContent = `Linked (${result.releases_imported || 0} releases)`;
    } else if (result.status === "error") {
      btn.textContent = result.message || "Error";
      btn.disabled = false;
      return;
    }

    // Close modal and refresh artist list
    setTimeout(() => {
      closeLinkSourceModal();
      loadArtists();
    }, 1000);
  } catch {
    btn.textContent = "Error";
    btn.disabled = false;
  }
}

function showLinkConflict(btn, artist, message) {
  btn.textContent = "Conflict";
  const item = btn.closest(".search-result-item") || btn.parentElement;
  if (item && !item.querySelector(".link-conflict-box")) {
    const box = document.createElement("div");
    box.className = "link-conflict-box";
    const safeMsg = typeof esc === "function" ? esc(message) : message;
    box.innerHTML = `<span class="link-conflict-msg">${safeMsg}. Replace + unlink from the other entry, or cancel?</span> `;
    const replaceBtn = document.createElement("button");
    replaceBtn.className = "add-btn";
    replaceBtn.textContent = "Replace";
    replaceBtn.onclick = (e) => {
      e.stopPropagation();
      box.remove();
      linkSource(artist, btn, true);
    };
    const cancelBtn = document.createElement("button");
    cancelBtn.className = "link-btn";
    cancelBtn.textContent = "Cancel";
    cancelBtn.onclick = (e) => {
      e.stopPropagation();
      box.remove();
      btn.disabled = false;
      btn.textContent = "Link";
    };
    box.appendChild(replaceBtn);
    box.appendChild(document.createTextNode(" "));
    box.appendChild(cancelBtn);
    item.appendChild(box);
  }
}

// --- Init ---
navigateTo(getRoute());   // sets the initial tab and loads its data
updateUnseenBadge();
loadSettings();
refreshEnabledSources();
updateResetFiltersBtn();
document.getElementById("filter-hidden").addEventListener("change", updateResetFiltersBtn);