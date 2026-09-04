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
navigateTo(getRoute());

// --- Source icon helper ---
function sourceIcon(source, size = 16) {
  const icons = {
    musicbrainz: '/static/assets/musicbrainz.svg',
    itunes: '/static/assets/apple-music.svg',
    soundcloud: '/static/assets/soundcloud.svg'
  };
  const src = icons[source];
  if (!src) return '';
  return `<img src="${src}" alt="" class="source-icon-inline" style="width:${size}px;height:${size}px;">`;
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
    const resp = await fetch("/api/artists");
    _artistsCache = await resp.json();
    _updateCheckBtn();
  } catch {
    // silent fail
  }
}

// Refresh cache periodically and after mutations (skip when tab hidden)
_refreshArtistsCache();
setInterval(() => { if (!document.hidden) _refreshArtistsCache(); }, 15000);

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
  if (!wrapper.contains(e.target)) closeArtistDropdown();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    if (!artistDropdownMenu.classList.contains("hidden")) closeArtistDropdown();
    if (typeof linkSourceModal !== "undefined" && !linkSourceModal.classList.contains("hidden")) closeLinkSourceModal();
    if (!checkPopup.classList.contains("hidden") && !scanRunning) closeCheckPopup();
    if (!logsPanel.classList.contains("hidden") || !settingsPanel.classList.contains("hidden")) closeSidePanels();
    const srcMenu = document.getElementById("search-source-menu");
    if (srcMenu && !srcMenu.classList.contains("hidden")) closeSearchSourceMenu();
  }
  if (!artistDropdownMenu.classList.contains("hidden") && ["ArrowDown", "ArrowUp", "Enter"].includes(e.key)) {
    handleDropdownKey(e);
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

  let html = `<div class="dropdown-item ${!activeArtistFilterId ? 'active' : ''}" role="option" data-idx="0" onclick="selectArtist('')"><em>All Artists</em></div>`;
  filtered.forEach((a, i) => {
    const activeClass = String(a.id) === String(activeArtistFilterId) ? " active" : "";
    html += `<div class="dropdown-item${activeClass}" role="option" data-idx="${i + 1}" onclick="selectArtist(${a.id})" title="${esc(a.name)}">${esc(a.name)}</div>`;
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

async function loadReleases() {
  const params = new URLSearchParams();
  if (activeArtistFilterId) params.set("artist_id", activeArtistFilterId);
  // Send selected types as comma-separated; empty means "all"
  if (activeTypeFilters.length > 0 && activeTypeFilters.length < 4) {
    params.set("type", activeTypeFilters.join(","));
  }
  if (filterUnseen.checked) params.set("unseen_only", "true");
  if (document.getElementById("filter-hidden").checked) params.set("hidden_only", "true");

  const resp = await fetch(`/api/releases?${params}`);
  const releases = await resp.json();

  if (releases.length === 0) {
    const totalArtists = (_artistsCache || []).length;
    if (totalArtists === 0) {
      releaseList.innerHTML = `<div class="empty-wrap"><h3>No releases yet</h3><p>Add artists to start tracking.</p><button class="check-btn" onclick="location.hash='#/artists'">Find artists</button></div>`;
    } else if (filterUnseen.checked) {
      releaseList.innerHTML = `<div class="empty-wrap"><h3>You're all caught up</h3><p>No new releases match your filters.</p><button class="link-btn" onclick="filterUnseen.checked=false;updateResetFiltersBtn();loadReleases()">Show all releases</button></div>`;
    } else if (activeArtistFilterId || activeTypeFilters.length < 4) {
      releaseList.innerHTML = `<div class="empty-wrap"><h3>No matches</h3><p>Nothing matches the current filters.</p><button class="link-btn" onclick="resetFilters()">Reset filters</button></div>`;
    } else {
      releaseList.innerHTML = '<p class="empty-state">No releases yet. Check your artists for new releases.</p>';
    }
  } else {
    releaseList.innerHTML = releases.map(r => renderReleaseCard(r)).join("");
  }

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
    ? `<div class="release-card" onclick="toggleTracklist(this)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleTracklist(this);}" tabindex="0" role="button" aria-expanded="false">`
    : `<div class="release-card static">`;
  const artistLink = r.artist_mbid
    ? `<a href="https://musicbrainz.org/artist/${esc(r.artist_mbid)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(r.artist_name)}</a>`
    : esc(r.artist_name);

  // Determine source icon and view URL
  const source = r.source || "musicbrainz";
  let sourceBadge = sourceIcon(source, 32);

  // Build the correct "View" URL based on source
  let viewUrl;
  if (source === "soundcloud" && r.mbid) {
    // SoundCloud: link to the track page
    viewUrl = `https://soundcloud.com/${esc(r.mbid)}`;
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
  let coverUrl = null;
  if (source === 'soundcloud' && r.artwork_url) {
    coverUrl = r.artwork_url;
  } else if (source === 'itunes' && r.artwork_url) {
    coverUrl = r.artwork_url;
  } else if (source === 'musicbrainz') {
    coverUrl = `https://coverartarchive.org/release-group/${esc(r.mbid)}/front-250`;
  }
  const coverHtml = coverUrl
    ? `<img class="release-cover" src="${coverUrl}" alt="" loading="lazy" onerror="this.outerHTML='<div class=&quot;cover-fallback&quot; aria-hidden=&quot;true&quot;>${esc(initialsFor(r.artist_name))}</div>'">`
    : `<div class="cover-fallback" aria-hidden="true">${esc(initialsFor(r.artist_name))}</div>`;

    // Streaming links only exist for MusicBrainz-sourced releases.
  const hasStreaming = source === "musicbrainz";
  const streamBtn = hasStreaming
    ? `<button class="expand-btn" data-expanded="false" onclick="event.stopPropagation();toggleStreaming(this)" aria-expanded="false">Stream ▾</button>`
    : "";
  const trackBtn = hasTracklist
    ? `<button class="expand-btn" data-expanded="false" onclick="event.stopPropagation();toggleTracklist(this.closest('.release-item').querySelector('.release-card'))" aria-expanded="false">Tracklist ▾</button>`
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
                ? `<button class="expand-btn" onclick="event.stopPropagation();unhideRelease(${r.id}, this)" title="Restore to feed">Unhide</button>`
                : `<button class="hide-btn-inline" onclick="event.stopPropagation();hideRelease(${r.id}, this)" title="Hide from feed" aria-label="Hide ${esc(r.title)} from feed">
                <img src="/static/assets/hidden.svg" alt="">
              </button>`}
              ${trackBtn}
              ${streamBtn}
            </span>
          </div>
        </div>
        <div class="release-actions">
          <a class="release-source-link" href="${esc(viewUrl)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="View on ${source === 'musicbrainz' ? 'MusicBrainz' : source === 'itunes' ? 'Apple Music' : 'SoundCloud'}">
            ${sourceBadge}
          </a>
          ${r.notified === 0 ? `<button class="release-badge" onclick="event.stopPropagation();markSeen(${r.id}, this)" aria-label="Mark ${esc(r.title)} as seen">NEW</button>` : ""}
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
  fetch(`/api/releases/${encodeURIComponent(releaseMbid)}/tracks`)
    .then(r => r.json())
    .then(data => {
      const tracks = data.tracks;
      tracklistCache[releaseMbid] = tracks;
      renderTracklist(section, tracks);
    })
    .catch(() => {
      section.innerHTML = `<p class="tracklist-error">Failed to load tracks. <button class="link-btn" onclick="delete tracklistCache['${releaseMbid}'];toggleTracklist(this.closest('.release-item').querySelector('.release-card'));toggleTracklist(this.closest('.release-item').querySelector('.release-card'))">Retry</button></p>`;
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
  container.innerHTML = `
    <div class="tracklist-header">
      <span class="tracklist-title">Tracklist</span>
      <span><span class="tracklist-count">${tracks.length} tracks</span> <button class="copy-tracklist-btn" onclick="copyTracklist('${mbid}', this)" title="Copy track titles">Copy</button></span>
    </div>
    <div class="tracklist-list">
      ${tracks.map(t => {
        const feat = (t.credits && t.credits.length)
          ? `<span class="track-feat">feat. ${t.credits.map(c => esc(c.name)).join(", ")}</span>`
          : "";
        const fullTitle = t.title + ((t.credits && t.credits.length) ? ` (feat. ${t.credits.map(c => c.name).join(", ")})` : "");
        return `
        <div class="track-item" title="${esc(fullTitle)}">
          <span class="track-number">${esc(t.number)}</span>
          <span class="track-title-text">${esc(t.title)}${t.has_single ? ' <span class="single-badge" title="Also released as a single">SINGLE</span>' : ''}${feat}</span>
          <span class="track-length">${formatLength(t.length)}</span>
        </div>
      `; }).join("")}
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

  fetch(`/api/releases/${encodeURIComponent(releaseMbid)}/streaming`)
    .then(r => r.json())
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

function renderStreaming(container, streaming) {
  if (!streaming || streaming.length === 0) {
    container.innerHTML = '<p class="tracklist-empty">No streaming links found for this release.</p>';
    return;
  }

  container.innerHTML = `
    <div class="streaming-header">
      <span class="tracklist-title">Stream on</span>
      <span class="tracklist-count">${streaming.length} link${streaming.length > 1 ? "s" : ""}</span>
    </div>
    <div class="streaming-list">
      ${streaming.map(s => {
        const icon = STREAMING_ICONS[s.key];
        const iconHtml = icon
          ? `<img class="stream-icon" src="/static/assets/${icon}" alt="${esc(s.service)}" loading="lazy">`
          : "";
        return `
          <a class="stream-link stream-${esc(s.key)}" href="${esc(s.url)}" target="_blank" rel="noopener" title="${esc(s.service)}">
            ${iconHtml}
            <span class="stream-label">${esc(s.service)}</span>
          </a>
        `;
      }).join("")}
    </div>
  `;
}

// --- Mark Seen ---
async function markSeen(id, el) {
  await fetch(`/api/releases/${id}/seen`, { method: "POST" });
  const item = el.closest(".release-item");
  if (item) item.classList.remove("unseen");
  el.remove();
  updateUnseenBadge();
}

async function hideRelease(id, el) {
  const item = el.closest(".release-item");
  const title = item ? (item.querySelector(".release-title") || {}).textContent || "Release" : "Release";
  await fetch(`/api/releases/${id}/hide`, { method: "POST" });
  if (item) {
    const placeholder = document.createComment(`hidden-release-${id}`);
    item.replaceWith(placeholder);
    showToast(`Hidden “${title.trim().slice(0, 60)}”`, "Undo", async () => {
      await fetch(`/api/releases/${id}/unhide`, { method: "POST" });
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
  await fetch(`/api/releases/${id}/unhide`, { method: "POST" });
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
  await fetch("/api/releases/all_seen", { method: "POST" });
  updateUnseenBadge();
  loadReleases();
}

function updateUnseenBadge() {
  fetch("/api/unseen_count")
    .then(r => r.json())
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
    const resp = await fetch("/api/settings");
    const s = await resp.json();
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
  if (a.source === 'soundcloud' && a.soundcloud_permalink) {
    if (_srcEnabled('soundcloud')) pills.push(`<a class="result-pill" href="https://soundcloud.com/${esc(a.soundcloud_permalink)}" target="_blank" rel="noopener">${sourceIcon('soundcloud', 14)} SoundCloud ↗</a>`);
  } else {
    if (a.mbid && _srcEnabled('musicbrainz')) pills.push(`<a class="result-pill" href="https://musicbrainz.org/artist/${esc(a.mbid)}" target="_blank" rel="noopener">${sourceIcon('musicbrainz', 14)} MusicBrainz ↗</a>`);
    if (a.itunes_artist_id && _srcEnabled('itunes')) pills.push(`<a class="result-pill" href="https://music.apple.com/artist/${esc(String(a.name).toLowerCase().replace(/[^a-z0-9]/g, ''))}/${esc(a.itunes_artist_id)}" target="_blank" rel="noopener">${sourceIcon('itunes', 14)} Apple Music ↗</a>`);
    if (a.soundcloud_permalink && _srcEnabled('soundcloud')) pills.push(`<a class="result-pill" href="https://soundcloud.com/${esc(a.soundcloud_permalink)}" target="_blank" rel="noopener">${sourceIcon('soundcloud', 14)} SoundCloud ↗</a>`);
  }
  return pills.length ? `<div class="result-sources">${pills.join("")}</div>` : "";
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
      fetch(`/api/itunes/album-count?artist_id=${id}&country=${encodeURIComponent(a.country || 'us')}`)
        .then(r => r.json())
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

function renderSearchPage() {
  const moreWrap = document.getElementById("search-more-wrap");
  const visible = _lastSearchResults.slice(0, _searchVisibleCount);
  searchResults.innerHTML = `<p class="status-msg">Found ${_lastSearchResults.length} — showing ${visible.length}</p>` + visible.map(a => {
    const trackedPill = a.already_tracked ? '<span class="tracked-pill">TRACKED</span>' : '';
    let artistImage = a.artistImageUrl || '';
    if (!artistImage && a.itunes_artist_id) artistImage = '/static/assets/apple-music.svg';
    const imageHtml = artistImage
      ? `<img class="result-image ${a.itunes_artist_id && !a.artistImageUrl ? 'result-image-icon' : ''}" src="${esc(artistImage)}" alt="" onerror="this.style.display='none'">`
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
        <button class="add-btn" onclick='addArtist(${JSON.stringify(a).replace(/'/g, "&#39;")}, this)'>${a.already_tracked ? 'Link' : 'Add'}</button>
      </div>
    `;
  }).join("");
  if (moreWrap) moreWrap.classList.toggle("hidden", _lastSearchResults.length <= _searchVisibleCount);
  enhanceItunesCounts(visible, searchResults);
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
    const resp = await fetch("/api/artists/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, sources: _searchSources })
    });
    const results = await resp.json();
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

  for (const s of sourcesToAdd) {
    try {
      const resp = await fetch("/api/artists", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source: s.source,
          id: s.id,
          name: artist.name,
          disambiguation: artist.disambiguation
        })
      });
      const result = await resp.json();
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
  const resp = await fetch("/api/artists");
  const artists = await resp.json();
  await refreshEnabledSources();
  const countEl = document.getElementById("tracked-count");
  if (countEl) countEl.textContent = artists.length ? `(${artists.length})` : "";

  if (artists.length === 0) {
    artistListEl.innerHTML = '<div class="empty-wrap"><h3>No artists tracked yet</h3><p>Search above or paste a MusicBrainz / Apple Music / SoundCloud link.</p></div>';
  } else {
    artistListEl.innerHTML = artists.map(a => {
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
        sourceIcons += `<button class="source-icon-btn unlinked" onclick="openLinkSourceModal(${a.id}, '${esc(a.name)}', 'musicbrainz')" title="Link MusicBrainz for ${esc(a.name)}" aria-label="Link MusicBrainz for ${esc(a.name)}"><img src="/static/assets/musicbrainz.svg" alt=""></button>`;
      }
      }
      // iTunes
      if (itOn) {
      if (hasItunes) {
        sourceIcons += `<a class="source-icon-btn linked" href="https://music.apple.com/artist/${esc(a.name.toLowerCase().replace(/[^a-z0-9]/g, ''))}/${esc(a.itunes_artist_id)}" target="_blank" rel="noopener" title="Apple Music — linked, click to open" aria-label="Open ${esc(a.name)} on Apple Music"><img src="/static/assets/apple-music.svg" alt=""></a>`;
      } else {
        sourceIcons += `<button class="source-icon-btn unlinked" onclick="openLinkSourceModal(${a.id}, '${esc(a.name)}', 'itunes')" title="Link Apple Music for ${esc(a.name)}" aria-label="Link Apple Music for ${esc(a.name)}"><img src="/static/assets/apple-music.svg" alt=""></button>`;
      }
      }
      // SoundCloud
      if (scOn) {
      if (hasSc) {
        sourceIcons += `<a class="source-icon-btn linked" href="https://soundcloud.com/${esc(a.soundcloud_permalink)}" target="_blank" rel="noopener" title="SoundCloud — linked, click to open" aria-label="Open ${esc(a.name)} on SoundCloud"><img src="/static/assets/soundcloud.svg" alt=""></a>`;
      } else {
        sourceIcons += `<button class="source-icon-btn unlinked" onclick="openLinkSourceModal(${a.id}, '${esc(a.name)}', 'soundcloud')" title="Link SoundCloud for ${esc(a.name)}" aria-label="Link SoundCloud for ${esc(a.name)}"><img src="/static/assets/soundcloud.svg" alt=""></button>`;
      }
      }

      // Build dropdown menu items (link/unlink hidden for disabled sources)
      let dropdownItems = '';
      // MusicBrainz
      if (mbOn) {
      if (hasMb) {
        dropdownItems += `<button class="dropdown-item" onclick="event.stopPropagation();confirmUnlinkArtist(${a.id}, 'musicbrainz', this)"><img src="/static/assets/musicbrainz.svg" alt="" class="dropdown-icon"> Unlink MusicBrainz</button>`;
      } else {
        dropdownItems += `<button class="dropdown-item" onclick="openLinkSourceModal(${a.id}, '${esc(a.name)}', 'musicbrainz')"><img src="/static/assets/musicbrainz.svg" alt="" class="dropdown-icon"> Link MusicBrainz</button>`;
      }
      }
      // iTunes
      if (itOn) {
      if (hasItunes) {
        dropdownItems += `<button class="dropdown-item" onclick="event.stopPropagation();confirmUnlinkArtist(${a.id}, 'itunes', this)"><img src="/static/assets/apple-music.svg" alt="" class="dropdown-icon"> Unlink iTunes</button>`;
      } else {
        dropdownItems += `<button class="dropdown-item" onclick="openLinkSourceModal(${a.id}, '${esc(a.name)}', 'itunes')"><img src="/static/assets/apple-music.svg" alt="" class="dropdown-icon"> Link iTunes</button>`;
      }
      }
      // SoundCloud
      if (scOn) {
      if (hasSc) {
        dropdownItems += `<button class="dropdown-item" onclick="event.stopPropagation();confirmUnlinkArtist(${a.id}, 'soundcloud', this)"><img src="/static/assets/soundcloud.svg" alt="" class="dropdown-icon"> Unlink SoundCloud</button>`;
      } else {
        dropdownItems += `<button class="dropdown-item" onclick="openLinkSourceModal(${a.id}, '${esc(a.name)}', 'soundcloud')"><img src="/static/assets/soundcloud.svg" alt="" class="dropdown-icon"> Link SoundCloud</button>`;
      }
      }
      // Untrack (red, with confirmation)
      dropdownItems = `<button class="dropdown-item" onclick="checkSingleArtist(${a.id})">↻ Check this artist</button>` + dropdownItems;
      dropdownItems += `<button class="dropdown-item" onclick="event.stopPropagation();editDisambiguation(${a.id}, this)"><img src="/static/assets/edit.svg" alt="" class="dropdown-icon dropdown-icon-mono"> Edit note…</button>`;
      dropdownItems += `<button class="dropdown-item danger" onclick="event.stopPropagation();confirmRemoveArtist(${a.id}, this)">Untrack</button>`;
      
      return `
        <div class="artist-item">
          <div class="artist-identity" title="${esc(a.name)}${a.disambiguation ? ` — ${esc(a.disambiguation)}` : ""}">
            <span class="artist-name">${esc(a.name)}</span>
            ${a.disambiguation ? `<span class="artist-disambig"> — ${esc(a.disambiguation)}</span>` : ""}
          </div>
          <div class="artist-actions">
            <div class="artist-source-icons">${sourceIcons}</div>
            <div class="artist-manage-wrapper">
              <button class="manage-btn" onclick="toggleArtistMenu(this)" title="Manage artist">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
              </button>
              <div class="artist-dropdown hidden">${dropdownItems}</div>
            </div>
          </div>
        </div>
      `;
    }).join("");
  }
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
    d.innerHTML = `<div class="inline-confirm">Untrack “${esc(name)}”? <button class="confirm-yes" onclick="doRemoveArtist(${id})">Confirm</button> <button class="confirm-no" onclick="loadArtists()">Keep</button></div>`;
  });
}

function doRemoveArtist(id) {
  fetch(`/api/artists/${id}`, { method: "DELETE" }).then(() => {
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
    d.innerHTML = `<div class="inline-confirm">Unlink ${sourceLabel}? <button class="confirm-yes" onclick="doUnlinkArtist(${id}, '${source}')">Confirm</button> <button class="confirm-no" onclick="loadArtists()">Keep</button></div>`;
  });
}

function doUnlinkArtist(id, source) {
  fetch(`/api/artists/${id}/unlink-${source}`, { method: "POST" }).then(() => {
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
        <button class="confirm-yes" onclick="saveDisambiguation(${id})">Save</button>
        <button class="confirm-no" onclick="loadArtists()">Cancel</button>
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
    const resp = await fetch(`/api/artists/${id}/disambiguation`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ disambiguation: value }),
    });
    const data = await resp.json();
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
  return `
    <h3>New Releases Found</h3>
    ${summary.map(s => `
      <div class="summary-artist">
        <button class="summary-artist-name" onclick="jumpToArtistFromSummary('${esc(s.artist)}')">${esc(s.artist)}</button>
        <ul>${s.new_releases.map(r => `<li>${esc(r)}</li>`).join("")}</ul>
      </div>
    `).join("")}
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
function esc(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// --- Export / Import Artists ---
async function exportArtists() {
  const resp = await fetch("/api/artists/export");
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
    const resp = await fetch("/api/artists/import", { method: "POST", body: form });
    if (!resp.ok) throw new Error(`Server returned ${resp.status}`);
    const result = await resp.json();

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
    const resp = await fetch("/api/logs?limit=200");
    const logs = await resp.json();
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
  const artistPart = log.artist ? `<span class="log-artist">[${esc(log.artist)}]</span>` : "";
  return `
    <div class="log-entry">
      <span class="log-timestamp">${esc(time)}</span>
      <span class="log-level ${esc(log.level)}">${esc(log.level)}</span>
      ${artistPart}
      <span class="log-message">${esc(log.message)}</span>
      ${log.detail ? `<span class="log-message"> — ${esc(log.detail)}</span>` : ""}
    </div>
  `;
}

async function clearLogs() {
  try {
    await fetch("/api/logs/clear", { method: "POST" });
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
    const resp = await fetch("/api/settings");
    const settings = await resp.json();
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
    const resp = await fetch("/api/settings/dedup-ignores");
    const data = await resp.json();
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
  listEl.innerHTML = _dedupRules.map((rule, i) => `
    <div class="dedup-card">
      <span class="dedup-text" title="${esc(rule)}"><span class="dedup-quote">“</span>${esc(rule)}<span class="dedup-quote">”</span></span>
      <button class="dedup-remove" onclick="removeDedupRule(${i})" aria-label="Remove ignore rule ${esc(rule)}" title="Remove">✕</button>
    </div>
  `).join("");
}

async function persistDedupRules(statusMsg) {
  const statusEl = document.getElementById("dedup-rules-status");
  try {
    const resp = await fetch("/api/settings/dedup-ignores", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ignores: _dedupRules }),
    });
    const data = await resp.json();
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
    const resp = await fetch("/api/settings/explicit-map");
    const data = await resp.json();
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
  listEl.innerHTML = _explicitMap.map((pair, i) => `
    <div class="dedup-card">
      <span class="dedup-text" title="${esc(pair[0])} → ${esc(pair[1])}"><span class="dedup-quote">“</span>${esc(pair[0])}<span class="dedup-quote">”</span> <span class="dedup-map-arrow">→</span> <span class="dedup-quote">“</span>${esc(pair[1])}<span class="dedup-quote">”</span></span>
      <button class="dedup-remove" onclick="removeExplicitMapRule(${i})" aria-label="Remove mapping ${esc(pair[0])}" title="Remove">✕</button>
    </div>
  `).join("");
}

async function persistExplicitMap(statusMsg) {
  const statusEl = document.getElementById("explicit-status");
  try {
    const resp = await fetch("/api/settings/explicit-map", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mappings: _explicitMap }),
    });
    const data = await resp.json();
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
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
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
    const resp = await fetch("/api/dedup", { method: "POST" });
    const data = await resp.json();
    status.textContent = `Hidden ${data.hidden} duplicate(s)`;
    status.className = "settings-status success";
    showToast(`Dedup hid ${data.hidden} duplicate(s)`, data.hidden ? "View feed" : null, data.hidden ? () => { location.hash = "#/feed"; loadReleases(); } : null);
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
    const resp = await fetch("/api/artists/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query,
        source: currentLinkSource === "musicbrainz" ? "musicbrainz" :
                currentLinkSource === "itunes" ? "itunes" :
                currentLinkSource === "soundcloud" ? "soundcloud" : ""
      })
    });
    const results = await resp.json();
    if (!Array.isArray(results)) {
      linkSourceResults.innerHTML = `<p class="status-msg error">Search failed. <button class="link-btn" onclick="searchLinkSource()">Retry</button></p>`;
      return;
    }

    if (results.length === 0) {
      linkSourceResults.innerHTML = '<p class="status-msg">No artists found. Check spelling or paste a direct link.</p>';
    } else {
      linkSourceResults.innerHTML = `<p class="status-msg">Found ${results.length} — showing ${Math.min(results.length, 10)}</p>` + results.slice(0, 10).map(a => {
        let artistImage = a.artistImageUrl || '';
        if (!artistImage && a.itunes_artist_id) artistImage = '/static/assets/apple-music.svg';
        const imageHtml = artistImage ? `<img class="result-image ${a.itunes_artist_id && !a.artistImageUrl ? 'result-image-icon' : ''}" src="${esc(artistImage)}" alt="" onerror="this.style.display='none'">` : '';
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
            <button class="add-btn" onclick='linkSource(${JSON.stringify(a).replace(/'/g, "&#39;")}, this)'>Link</button>
          </div>
        `;
      }).join("");
      enhanceItunesCounts(results.slice(0, 10), linkSourceResults);
    }
  } catch (err) {
    linkSourceResults.innerHTML = `<p class="status-msg error">Search failed: ${esc(err.message)} <button class="link-btn" onclick="searchLinkSource()">Retry</button></p>`;
  }
}

async function linkSource(artist, btn) {
  btn.disabled = true;
  btn.textContent = "Linking...";

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
      return;
    }

    const resp = await fetch("/api/artists", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source: sourceToLink,
        id: idToLink,
        name: artist.name,
        disambiguation: artist.disambiguation
      })
    });
    const result = await resp.json();

    if (result.status === "already_exists") {
      btn.textContent = "Already linked";
    } else if (result.status === "linked") {
      btn.textContent = `Linked (${result.releases_imported || 0} releases)`;
    }

    // Close modal and refresh artist list
    setTimeout(() => {
      closeLinkSourceModal();
      loadArtists();
    }, 1000);
  } catch {
    btn.textContent = "Error";
  }
}

// --- Init ---
loadReleases();
updateUnseenBadge();
loadSettings();
refreshEnabledSources();
updateResetFiltersBtn();
document.getElementById("filter-hidden").addEventListener("change", updateResetFiltersBtn);