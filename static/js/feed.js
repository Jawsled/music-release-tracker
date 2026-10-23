// --- Feed Tab ---
const ALL_RELEASE_TYPES = ["Album", "EP", "Single", "Other"];
let activeArtistFilterId = "";   // selected artist ID (empty = all)
let activeTypeFilters = [...ALL_RELEASE_TYPES];  // multi-select release types (empty = all)
const filterUnseen = document.getElementById("filter-unseen");
const releaseList = document.getElementById("release-list");
const unseenBadge = document.getElementById("unseen-badge");
const artistDropdownMenu = document.getElementById("artist-dropdown-menu");
const artistDropdownList = document.getElementById("artist-dropdown-list");
const artistFilterSearch = document.getElementById("artist-filter-search");
const filterArtistBtn = document.getElementById("filter-artist-btn");

filterUnseen.addEventListener("change", () => { updateResetFiltersBtn(); loadReleases(); });

// --- Feed filter persistence ---
// Filters survive a reload (same localStorage pattern as the search-source and
// chime preferences). State is written from loadReleases(), the single funnel
// every filter change goes through, and restored once before the first load.
const FEED_FILTERS_KEY = "mrt_feed_filters";

function saveFeedFilters() {
  try {
    let artistName = "";
    if (activeArtistFilterId) {
      artistName = _getArtistName(activeArtistFilterId) || "";
      if (!artistName) {
        const prev = JSON.parse(localStorage.getItem(FEED_FILTERS_KEY) || "null");
        if (prev && String(prev.artistId) === String(activeArtistFilterId)) {
          artistName = prev.artistName || "";
        }
      }
    }
    const hiddenEl = document.getElementById("filter-hidden");
    localStorage.setItem(FEED_FILTERS_KEY, JSON.stringify({
      artistId: activeArtistFilterId || "",
      artistName,
      types: activeTypeFilters,
      unseen: !!filterUnseen.checked,
      hidden: !!(hiddenEl && hiddenEl.checked),
    }));
  } catch { /* storage unavailable — filters just won't persist */ }
}

function restoreFeedFilters() {
  let saved;
  try { saved = JSON.parse(localStorage.getItem(FEED_FILTERS_KEY) || "null"); }
  catch { return; }
  if (!saved || typeof saved !== "object") return;

  if (Array.isArray(saved.types)) {
    activeTypeFilters = saved.types.filter(t => ALL_RELEASE_TYPES.includes(t));
  }
  const hiddenEl = document.getElementById("filter-hidden");
  if (hiddenEl) hiddenEl.checked = !!saved.hidden;
  filterUnseen.checked = !!saved.unseen;

  if (saved.artistId) {
    activeArtistFilterId = String(saved.artistId);
    filterArtistBtn.textContent = saved.artistName || "All Artists";
    filterArtistBtn.title = saved.artistName || "";
  }

  document.querySelectorAll(".type-chips .chip").forEach(c => {
    const on = activeTypeFilters.includes(c.dataset.type);
    c.classList.toggle("active", on);
    c.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

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
    // A restored/selected artist filter must point at a real artist. Refresh
    // its label once names are known, and drop it if that artist was untracked
    // in another tab or a previous session.
    if (activeArtistFilterId) {
      const match = _artistsCache.find(a => String(a.id) === String(activeArtistFilterId));
      if (match) {
        filterArtistBtn.textContent = match.name;
        filterArtistBtn.title = match.name;
      } else if (_artistsCache.length) {
        activeArtistFilterId = "";
        filterArtistBtn.textContent = "All Artists";
        filterArtistBtn.title = "";
        updateResetFiltersBtn();
        saveFeedFilters();
        loadReleases();   // the stale filter was hiding the real feed
      }
    }
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
// Objects already registered keep their key across re-renders (e.g. "Show
// more"), so the map can no longer grow once per render. Each new search
// starts from a clean slate.
let _rowKeys = new WeakMap();
function registerRow(obj) {
  let key = _rowKeys.get(obj);
  if (key) return key;
  key = `r${++_rowDataSeq}`;
  _rowKeys.set(obj, key);
  _rowData.set(key, obj);
  return key;
}
function resetRowData() {
  _rowData.clear();
  _rowKeys = new WeakMap();
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
  "jump-to-today": () => _jumpToToday(),
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
  // The Today divider is injected once, immediately before the first release
  // that is not in the future. Feed order is newest-first, so that is exactly
  // the future/past boundary. Pages append in order, so the same rule holds for
  // the initial render and for every later page.
  let html = "";
  for (const r of releases) {
    if (!_todayMarkerInserted && _dateSegment(r.release_date) <= 0) {
      html += _todayDividerHTML();
      _todayMarkerInserted = true;
    }
    html += renderReleaseCard(r);
  }
  return html;
}

// --- Timeline anchors ---
// release_date is a calendar date (YYYY-MM-DD, sometimes YYYY-MM or YYYY), not
// an instant, so "today" means the viewer's local calendar day. Build it from
// local getters (toISOString() would flip days around UTC midnight) and compare
// ISO strings, which already sort correctly at every precision.
let _feedToday = _localTodayISO();
let _todayMarkerInserted = false;

function _localTodayISO() {
  const now = new Date();
  const pad = n => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

// Split an ISO-ish release date, remembering whether it names a day, a month or
// only a year. Empty/unknown values fall through as null.
function _parseReleaseDate(value) {
  const match = /^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$/.exec(value || "");
  if (!match) return null;
  const year = Number(match[1]);
  const month = match[2] ? Number(match[2]) : null;
  const day = match[3] ? Number(match[3]) : null;
  return { year, month, day, precision: day ? "day" : month ? "month" : "year" };
}

// -1 = past, 0 = today, 1 = upcoming. Plain string comparison also puts partial
// dates in a sensible bucket ("2026-11" is upcoming, "2026" is already past).
function _dateSegment(value) {
  if (!value) return -1;
  if (value === _feedToday) return 0;
  return value > _feedToday ? 1 : -1;
}

function _formatReleaseDate(value) {
  const parsed = _parseReleaseDate(value);
  if (!parsed) return "Unknown date";
  if (parsed.precision === "year") return String(parsed.year);
  const options = parsed.precision === "month"
    ? { month: "long", year: "numeric" }
    : { month: "short", day: "numeric", year: "numeric" };
  // UTC getter + explicit timeZone stops date-only values from rendering as
  // the previous day in timezones behind UTC.
  return new Date(Date.UTC(parsed.year, (parsed.month || 1) - 1, parsed.day || 1))
    .toLocaleDateString(undefined, { ...options, timeZone: "UTC" });
}

// Short "where am I" label. Day maths uses UTC midnights so a DST week is still
// exactly 7 days, and distant dates stay quiet instead of adding noise.
function _relativeDateLabel(value) {
  const parsed = _parseReleaseDate(value);
  const today = _parseReleaseDate(_feedToday);
  if (!parsed || !today) return "";

  if (parsed.precision === "day") {
    const then = Date.UTC(parsed.year, parsed.month - 1, parsed.day);
    const now = Date.UTC(today.year, today.month - 1, today.day);
    const days = Math.round((then - now) / 86400000);
    if (days === 0) return "today";
    if (days === 1) return "tomorrow";
    if (days === -1) return "yesterday";
    if (days > 1) {
      if (days <= 60) return `in ${days} days`;
      // Far-out announcements still need an orientation hint; old rows don't.
      const months = Math.round(days / 30.44);
      return months < 24 ? `in ${months} months` : `in ${Math.round(months / 12)} years`;
    }
    return days >= -60 ? `${Math.abs(days)} days ago` : "";
  }

  if (parsed.precision === "month") {
    const months = (parsed.year * 12 + parsed.month) - (today.year * 12 + today.month);
    if (months === 0) return "this month";
    if (months === 1) return "next month";
    if (months === -1) return "last month";
    if (months > 1) return months <= 12 ? `in ${months} months` : "";
    return months >= -12 ? `${Math.abs(months)} months ago` : "";
  }

  const years = parsed.year - today.year;
  if (years === 0) return "this year";
  if (years === 1) return "next year";
  return years > 1 ? `in ${years} years` : "";
}

function _todayDividerHTML() {
  const today = _parseReleaseDate(_feedToday);
  const label = new Date(today.year, today.month - 1, today.day);
  const text = label.toLocaleDateString(undefined, {
    weekday: "long", month: "short", day: "numeric", year: "numeric",
  });
  return `<div class="feed-today" id="feed-today" role="separator" aria-label="Today, ${esc(text)}">
      <span class="feed-today-line" aria-hidden="true"></span>
      <span class="feed-today-label">Today · ${esc(text)}</span>
      <span class="feed-today-line" aria-hidden="true"></span>
    </div>`;
}

// --- Today jump pill ---
// A floating shortcut back to the divider, shown only while the divider is
// off-screen (or not rendered yet because the first page is all upcoming), so
// it never competes with the feed itself.
let _todayPillFrame = 0;

// The app header is sticky (`.app-header`), so it covers the top of the
// viewport. Anything we scroll to must land below its bottom edge, and the
// "is the divider visible?" test has to use that same line.
function _feedViewportTop() {
  const header = document.querySelector(".app-header");
  return Math.max(0, header ? header.getBoundingClientRect().bottom : 0);
}

function _updateTodayPill() {
  const pill = document.getElementById("today-pill");
  if (!pill) return;
  const onFeed = document.getElementById("tab-feed")?.classList.contains("active");
  if (!onFeed || !releaseList.querySelector(".release-item")) {
    pill.classList.add("hidden");
    return;
  }

  const marker = document.getElementById("feed-today");
  if (!marker) {
    // More upcoming releases than one page: today is further down, not drawn.
    pill.classList.toggle("hidden", !_releaseCursor);
    pill.textContent = "↓ Jump to today";
    return;
  }

  const topLimit = _feedViewportTop();
  const rect = marker.getBoundingClientRect();
  if (rect.top >= topLimit && rect.bottom <= window.innerHeight) {
    pill.classList.add("hidden");
    return;
  }
  pill.textContent = rect.top > topLimit ? "↓ Jump to today" : "↑ Back to today";
  pill.classList.remove("hidden");
}

function _scheduleTodayPillUpdate() {
  if (_todayPillFrame) return;
  _todayPillFrame = requestAnimationFrame(() => {
    _todayPillFrame = 0;
    _updateTodayPill();
  });
}

function _scrollToToday() {
  const marker = document.getElementById("feed-today");
  if (!marker) return;
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  // Offset by the sticky header instead of using scrollIntoView(), which would
  // align the divider with the top of the viewport and hide it behind it.
  const top = window.scrollY + marker.getBoundingClientRect().top - _feedViewportTop() - 12;
  window.scrollTo({ top: Math.max(0, top), behavior: reduceMotion ? "auto" : "smooth" });
  _updateTodayPill();
}

async function _jumpToToday() {
  if (document.getElementById("feed-today")) {
    _scrollToToday();
    return;
  }

  // The boundary lives on a later page: page forward until it renders (or the
  // feed runs out). Bounded so a bad cursor can't spin forever.
  const pill = document.getElementById("today-pill");
  if (pill) {
    pill.disabled = true;
    pill.textContent = "Finding today…";
  }
  try {
    for (let pages = 0; pages < 20 && !document.getElementById("feed-today") && _releaseCursor; pages++) {
      // Let an observer-triggered page finish first instead of bailing out.
      while (_releaseLoadingMore) await new Promise(resolve => setTimeout(resolve, 50));
      const before = _releaseLoaded;
      await _loadMoreReleases();
      if (_releaseLoaded === before) break; // failed or superseded by a filter change
    }
  } finally {
    if (pill) pill.disabled = false;
    _scrollToToday();   // also restores the pill label
  }
}

// The divider and relative labels are computed at render time. When the local
// day changes (tab left open overnight, or the window regains focus) rebuild
// against the new "today" so the boundary doesn't go stale.
function _refreshIfDayChanged() {
  const today = _localTodayISO();
  if (today === _feedToday) return;
  _feedToday = today;
  loadReleases();
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) _refreshIfDayChanged();
});
setInterval(() => { if (!document.hidden) _refreshIfDayChanged(); }, 60000);
window.addEventListener("scroll", _scheduleTodayPillUpdate, { passive: true });
window.addEventListener("resize", _scheduleTodayPillUpdate);

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
    _scheduleTodayPillUpdate();
  }
}

async function loadReleases() {
  // Any filter change funnels through here, so this is the one place that has
  // to persist the current filter state.
  saveFeedFilters();
  // New load: invalidate any in-flight page request and reset paging.
  const seq = ++_releaseRequestSeq;
  _releaseCursor = "";
  _releaseTotal = 0;
  _releaseLoaded = 0;
  _todayMarkerInserted = false;
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
    _updateTodayPill();
    return;
  }

  _releaseLoaded = releases.length;
  releaseList.innerHTML = _renderReleaseCards(releases);
  _setupReleaseObserver();
  updateUnseenBadge();
  _updateTodayPill();
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

function sourceLabel(source) {
  if (source === "musicbrainz") return "MusicBrainz";
  if (source === "itunes") return "Apple Music";
  if (source === "soundcloud") return "SoundCloud";
  return source || "";
}

// Hidden rows carry hidden_reason (and, for dedup hides, the surviving copy
// or owning album) so the Hidden view can explain why a release is set aside.
function hiddenReasonNote(r) {
  if (r.is_visible !== 0) return "";

  let label;
  if (r.hidden_reason === "manual") {
    label = "Hidden manually";
  } else if (r.hidden_reason === "dedup_title") {
    label = r.duplicate_title
      ? `Duplicate of “${esc(r.duplicate_title)}”`
      : "Duplicate release";
  } else if (r.hidden_reason === "dedup_track") {
    label = r.duplicate_title
      ? `Album/EP track from “${esc(r.duplicate_title)}”`
      : "Album/EP track";
  } else {
    label = "Hidden (reason not recorded)";
  }

  if (r.hidden_reason === "dedup_title" || r.hidden_reason === "dedup_track") {
    const details = [
      r.duplicate_release_type,
      r.duplicate_release_date,
      sourceLabel(r.duplicate_source),
    ]
      .filter(Boolean)
      .map(value => esc(value));
    if (r.duplicate_is_visible === 0) details.push("target hidden");
    if (details.length) {
      label += ` <span class="release-hidden-detail">(${details.join(" · ")})</span>`;
    }
  }

  return `<div class="release-hidden-note">${label}</div>`;
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
  const hiddenNote = hiddenReasonNote(r);

  // Date line: human-readable date plus a short relative hint ("in 7 days",
  // "today") so the feed reads as a timeline instead of a wall of ISO strings.
  const dateSegment = _dateSegment(r.release_date);
  const dateLabel = _formatReleaseDate(r.release_date);
  const relativeLabel = _relativeDateLabel(r.release_date);
  const relativeHtml = relativeLabel
    ? ` <span class="release-relative ${dateSegment > 0 ? "is-upcoming" : dateSegment === 0 ? "is-today" : "is-past"}">${esc(relativeLabel)}</span>`
    : "";

  return `
    <div class="release-item ${r.notified === 0 ? "unseen" : ""} ${r.is_visible === 0 ? "is-hidden" : ""}" data-id="${esc(r.id)}" data-mbid="${esc(r.mbid)}" data-source="${esc(source)}">
      ${openTag}
        ${coverHtml}
        <div class="release-info">
          <div class="release-title">${esc(r.title)}</div>
          <div class="release-artist">${artistLink}</div>
          ${creditsHtml}
          ${hiddenNote}
          <div class="release-meta">
            ${esc(r.release_type)} · ${esc(dateLabel)}${relativeHtml}
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
    })
    .catch(() => { /* badge is non-critical */ });
}

