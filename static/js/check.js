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

