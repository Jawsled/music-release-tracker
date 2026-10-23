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

