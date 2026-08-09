// --- Tab switching ---
const tabButtons = document.querySelectorAll(".tab-btn");
const tabContents = document.querySelectorAll(".tab-content");

tabButtons.forEach(btn => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.tab;
    if (!tab) return; // Skip buttons without data-tab (e.g., Logs)
    tabButtons.forEach(b => b.classList.remove("active"));
    tabContents.forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${tab}`).classList.add("active");
    if (tab === "feed") loadReleases();
    if (tab === "artists") loadArtists();
  });
});

// --- Feed Tab ---
let activeArtistFilterId = "";   // selected artist ID (empty = all)
let activeTypeFilter = "";       // selected release type
const filterUnseen = document.getElementById("filter-unseen");
const releaseList = document.getElementById("release-list");
const unseenBadge = document.getElementById("unseen-badge");
const artistDropdownMenu = document.getElementById("artist-dropdown-menu");
const artistDropdownList = document.getElementById("artist-dropdown-list");
const artistFilterSearch = document.getElementById("artist-filter-search");
const filterArtistBtn = document.getElementById("filter-artist-btn");

filterUnseen.addEventListener("change", loadReleases);

// --- Artist Dropdown Logic ---
function toggleArtistDropdown() {
  const hidden = artistDropdownMenu.classList.contains("hidden");
  if (hidden) {
    renderArtistDropdown();
    artistDropdownMenu.classList.remove("hidden");
    artistFilterSearch.value = "";
    filterArtistBtn.style.borderColor = "#8c8c8c";
  } else {
    closeArtistDropdown();
  }
}

function closeArtistDropdown() {
  artistDropdownMenu.classList.add("hidden");
  filterArtistBtn.style.borderColor = "";
}

// Close dropdown when clicking outside
document.addEventListener("click", e => {
  const wrapper = document.getElementById("artist-filter-wrapper");
  if (!wrapper.contains(e.target)) closeArtistDropdown();
});

function renderArtistDropdown(artists) {
  fetch("/api/artists")
    .then(r => r.json())
    .then(data => {
      const q = artistFilterSearch.value.trim().toLowerCase();
      const filtered = q ? data.filter(a => a.name.toLowerCase().includes(q)) : data;

      let html = `<div class="dropdown-item ${!activeArtistFilterId ? 'active' : ''}" onclick="selectArtist('')"><em>All Artists</em></div>`;
      filtered.forEach(a => {
        const activeClass = String(a.id) === activeArtistFilterId ? " active" : "";
        html += `<div class="dropdown-item${activeClass}" onclick="selectArtist(${a.id})">${esc(a.name)}</div>`;
      });
      artistDropdownList.innerHTML = html;
    });
}

function filterArtistList() {
  renderArtistDropdown();
}

function selectArtist(id) {
  activeArtistFilterId = String(id);
  if (id === "" || id === "0" || id === false) {
    filterArtistBtn.textContent = "All Artists";
  } else {
    // Find name for display
    fetch("/api/artists").then(r => r.json()).then(data => {
      const a = data.find(x => String(x.id) === String(id));
      filterArtistBtn.textContent = a ? esc(a.name) : "All Artists";
    });
  }
  closeArtistDropdown();
  loadReleases();
}

// --- Type Chip Logic ---
function selectType(chipEl) {
  document.querySelectorAll(".chip").forEach(c => c.classList.remove("active"));
  chipEl.classList.add("active");
  activeTypeFilter = chipEl.dataset.type;
  loadReleases();
}

async function loadReleases() {
  const params = new URLSearchParams();
  if (activeArtistFilterId) params.set("artist_id", activeArtistFilterId);
  if (activeTypeFilter) params.set("type", activeTypeFilter);
  if (filterUnseen.checked) params.set("unseen_only", "true");

  const resp = await fetch(`/api/releases?${params}`);
  const releases = await resp.json();

  if (releases.length === 0) {
    releaseList.innerHTML = '<p class="empty-state">No releases match your filters.</p>';
  } else {
    releaseList.innerHTML = releases.map(r => renderReleaseCard(r)).join("");
  }

  updateUnseenBadge();
}

function renderReleaseCard(r) {
  const hasTracklist = r.release_type !== "Single";
  const cardClick = hasTracklist ? `onclick="toggleTracklist(this)"` : "";
  const artistLink = r.artist_mbid
    ? `<a href="https://musicbrainz.org/artist/${esc(r.artist_mbid)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(r.artist_name)}</a>`
    : esc(r.artist_name);

  return `
    <div class="release-item ${r.notified === 0 ? "unseen" : ""}" data-id="${esc(r.id)}" data-mbid="${esc(r.mbid)}">
      <div class="release-card" ${cardClick}>
        <img class="release-cover"
             src="https://coverartarchive.org/release-group/${esc(r.mbid)}/front-250"
             alt="" loading="lazy"
             onerror="this.onerror=null;this.classList.add('no-cover');this.src='/static/icon.svg'">
        <div class="release-info">
          <div class="release-title">${esc(r.title)}</div>
          <div class="release-artist">${artistLink}</div>
          <div class="release-meta">
            ${esc(r.release_type)} · ${esc(r.release_date || "Unknown date")}
            ${hasTracklist ? '<span style="color:#666;font-size:0.75rem;margin-left:auto;">click for tracks ▾</span>' : ""}
          </div>
        </div>
        <div class="release-actions">
          <a href="${esc(r.mb_url || `https://musicbrainz.org/release-group/${r.mbid}`)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">View ↗</a>
          ${r.notified === 0 ? `<span class="release-badge" onclick="event.stopPropagation();markSeen(${r.id}, this)">NEW</span>` : ""}
        </div>
      </div>
      <div class="tracklist-section"></div>
    </div>
  `;
}

// --- Tracklist Toggle (integrated inline) ---
const tracklistCache = {};

function toggleTracklist(cardEl) {
  const item = cardEl.closest(".release-item");
  const section = item.querySelector(".tracklist-section");
  const releaseMbid = item.dataset.mbid;

  const expanded = section.classList.contains("expanded");

  if (expanded) {
    section.classList.remove("expanded");
    return;
  }

  section.classList.add("expanded");

  // Check cache
  if (tracklistCache[releaseMbid]) {
    renderTracklist(section, tracklistCache[releaseMbid]);
    return;
  }

  // Fetch tracks
  section.innerHTML = '<p class="tracklist-loading">Loading tracks...</p>';
  fetch(`/api/releases/${encodeURIComponent(releaseMbid)}/tracks`)
    .then(r => r.json())
    .then(data => {
      const tracks = data.tracks;
      tracklistCache[releaseMbid] = tracks;
      renderTracklist(section, tracks);
    })
    .catch(() => {
      section.innerHTML = '<p class="tracklist-error">Failed to load tracks.</p>';
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

  container.innerHTML = `
    <div class="tracklist-header">
      <span class="tracklist-title">Tracklist</span>
      <span class="tracklist-count">${tracks.length} tracks</span>
    </div>
    <div class="tracklist-list">
      ${tracks.map(t => `
        <div class="track-item">
          <span class="track-number">${esc(t.number)}</span>
          <span class="track-title-text">${esc(t.title)}${t.has_single ? ' <span class="single-badge" title="Also released as a single">SINGLE</span>' : ''}</span>
          <span class="track-length">${formatLength(t.length)}</span>
        </div>
      `).join("")}
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
        if (data.count > 0) { badge.textContent = data.count; badge.classList.remove("hidden"); }
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

async function searchArtists() {
  const query = searchInput.value.trim();
  if (!query) return;

  searchBtn.disabled = true;
  searchBtn.textContent = "Searching...";

  try {
    const resp = await fetch("/api/artists/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query })
    });
    const results = await resp.json();

    if (results.length === 0) {
      searchResults.innerHTML = '<p class="status-msg">No artists found.</p>';
    } else {
      searchResults.innerHTML = results.slice(0, 10).map(a => `
        <div class="search-result-item">
          <div class="result-info">
            <div class="result-name">${esc(a.name)}</div>
            <div class="result-detail">${[a.disambiguation, a.type, a.country].filter(Boolean).join(" · ")}</div>
          </div>
          <button class="add-btn" onclick='addArtist(${JSON.stringify(a).replace(/'/g, "&#39;")}, this)'>Add</button>
        </div>
      `).join("");
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

  try {
    const resp = await fetch("/api/artists", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mbid: artist.mbid,
        name: artist.name,
        disambiguation: artist.disambiguation
      })
    });
    const result = await resp.json();

    if (result.status === "already_exists") {
      btn.textContent = "Already added";
    } else {
      btn.textContent = `Added (${result.releases_imported} releases)`;
    }

    loadArtists();
    // Dropdown fetches fresh data on open, no need to update eagerly
  } catch {
    btn.textContent = "Error";
  }
}

async function loadArtists() {
  const resp = await fetch("/api/artists");
  const artists = await resp.json();

  if (artists.length === 0) {
    artistListEl.innerHTML = '<p class="empty-state">No artists tracked yet.</p>';
  } else {
    artistListEl.innerHTML = artists.map(a => `
      <div class="artist-item">
        <div>
          <span class="artist-name">${esc(a.name)}</span>
          ${a.disambiguation ? `<span class="artist-disambig"> — ${esc(a.disambiguation)}</span>` : ""}
          ${a.mbid ? `<a class="mb-link mb-artist-link" href="https://musicbrainz.org/artist/${esc(a.mbid)}" target="_blank" rel="noopener">MB ↗</a>` : ""}
        </div>
        <button class="remove-btn" onclick="removeArtist(${a.id}, this)">Remove</button>
      </div>
    `).join("");
  }
}

async function removeArtist(id, btn) {
  btn.disabled = true;
  await fetch(`/api/artists/${id}`, { method: "DELETE" });
  loadArtists();
  // Dropdown will refresh itself when opened next time
  loadReleases();
}

// --- Check Now Popup ---
const checkBtn = document.getElementById("check-btn");
const checkPopup = document.getElementById("check-popup");
const popupTitle = document.getElementById("popup-title");
const checkProgressBar = document.getElementById("check-progress-bar");
const checkProgressText = document.getElementById("check-progress-text");
const summaryBody = document.getElementById("summary-body");
const pauseBtn = document.getElementById("pause-btn");

let scanRunning = false;
let scanPaused = false;
let currentSkip = 0; // How many artists have been fully checked (for resume)
let activeSource = null; // Current EventSource connection
let partialSummary = []; // Accumulated results across pause/resume cycles

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
    popupTitle.textContent = `Paused at ${currentSkip}%`;
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

  checkBtn.textContent = "Checking...";
  popupTitle.textContent = `Starting check…`;

  summaryBody.innerHTML = "";
  summaryBody.classList.add("hidden");

  pauseBtn.classList.remove("hidden");
  pauseBtn.textContent = "⏸ Pause";
  pauseBtn.classList.remove("resuming");

  const source = new EventSource(`/api/check?skip=${skip}`);
  activeSource = source;

  // Compute total artists for progress bar (fetch once)
  let totalArtists = null;

  source.onmessage = event => {
    const data = JSON.parse(event.data);

    if (data.type === "progress") {
      if (!totalArtists) totalArtists = data.total - skip; // remaining artists
      const pct = Math.round((data.current / (skip + totalArtists)) * 100);
      checkProgressBar.style.width = pct + "%";
      checkProgressText.textContent = data.message;
      checkProgressText.className = "check-progress-text";
      popupTitle.textContent = `Checking… ${pct}%`;

      // Live percentage on button too
      checkBtn.textContent = `${pct}%`;
    } else if (data.type === "error") {
      scanRunning = false;
      source.close();
      activeSource = null;
      pauseBtn.classList.add("hidden");
      checkBtn.textContent = "Check Now";
      checkProgressText.textContent = data.message;
      checkProgressText.className = "check-progress-text error";
    } else if (data.type === "done") {
      scanRunning = false;
      source.close();
      activeSource = null;
      pauseBtn.classList.add("hidden");

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

      checkBtn.textContent = "Check Now";
      popupTitle.textContent = "Done!";
      checkProgressBar.style.width = "100%";
      checkProgressBar.classList.add("done");

      const totalNew = mergedSummary.reduce((sum, s) => sum + s.new_releases.length, 0);
      checkProgressText.textContent = `Done! Found ${totalNew} new release(s).`;
      checkProgressText.className = "check-progress-text done";

      if (mergedSummary.length > 0) {
        summaryBody.innerHTML = renderSummary(mergedSummary);
        summaryBody.classList.remove("hidden");
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
      checkBtn.textContent = "Check Now";
      popupTitle.textContent = "Error";
      checkProgressText.textContent = "Connection lost.";
      checkProgressText.className = "check-progress-text error";
    }
  };
}

function renderSummary(summary) {
  return `
    <h3>New Releases Found</h3>
    ${summary.map(s => `
      <div class="summary-artist">
        <h4>${esc(s.artist)}</h4>
        <ul>${s.new_releases.map(r => `<li>${esc(r)}</li>`).join("")}</ul>
      </div>
    `).join("")}
  `;
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
}

document.getElementById("import-btn").addEventListener("click", () => {
  document.getElementById("import-file-input").click();
});

document.getElementById("import-file-input").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;

  const form = new FormData();
  form.append("file", file);

  try {
    const resp = await fetch("/api/artists/import", { method: "POST", body: form });
    if (!resp.ok) {
      throw new Error(`Server returned ${resp.status}`);
    }
    const result = await resp.json();

    if (result.status === "error") {
      alert(`Import failed: ${result.message}`);
    } else {
      let msg = `Import complete.\nAdded: ${result.added}\nSkipped (already exists): ${result.skipped}`;
      if (result.errors && result.errors.length > 0) {
        msg += `\nErrors:\n${result.errors.join("\n")}`;
      }
      alert(msg);

      loadArtists();
    }
  } catch (err) {
    alert(`Import failed: ${err.message}`);
  }

  // Reset file input so the same file can be re-imported
  event.target.value = "";
});

// --- Logs Panel ---
const logsPanel = document.getElementById("logs-panel");
const logsContainer = document.getElementById("logs-container");
let logsAutoScroll = true;
let logsRefreshTimer = null;
const LOGS_REFRESH_INTERVAL = 2000; // 2 seconds

document.getElementById("logs-btn").addEventListener("click", toggleLogsPanel);

function toggleLogsPanel() {
  const hidden = logsPanel.classList.contains("hidden");
  if (hidden) {
    logsPanel.classList.remove("hidden");
    startLogsRefresh();
  } else {
    logsPanel.classList.add("hidden");
    stopLogsRefresh();
  }
}

function startLogsRefresh() {
  stopLogsRefresh(); // Clear any existing timer
  loadLogs(); // Load immediately
  logsRefreshTimer = setInterval(loadLogs, LOGS_REFRESH_INTERVAL);
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
    const newHtml = logs.map(l => renderLogEntry(l)).join("");
    // Only update if content changed to avoid unnecessary DOM work
    if (logsContainer.innerHTML !== newHtml) {
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

// --- Init ---
loadReleases();
updateUnseenBadge();