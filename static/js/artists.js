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
    // Cancel any in-flight search so a late response can't repopulate results.
    _searchRequestSeq++;
    searchResults.innerHTML = "";
    return;
  }
  // Debounce: wait 400ms after typing stops before searching
  _searchTimeout = setTimeout(searchArtists, 400);
});

// --- Enabled sources cache (mirrors Settings toggles) ---
// Bandcamp is opt-in (default off); the others default on. Refreshed from
// /api/settings whenever the artists list renders and after settings save.
let _enabledSources = { musicbrainz: true, itunes: true, bandcamp: false, soundcloud: true };

async function refreshEnabledSources() {
  try {
    const s = await api("/api/settings");
    _enabledSources = {
      musicbrainz: s.source_musicbrainz !== "0",
      itunes: s.source_itunes !== "0",
      bandcamp: s.source_bandcamp === "1",
      soundcloud: s.source_soundcloud !== "0",
    };
  } catch {
    _enabledSources = { musicbrainz: true, itunes: true, bandcamp: false, soundcloud: true };
  }
  if (typeof syncSearchSourceMenu === "function") syncSearchSourceMenu();
  return _enabledSources;
}

function _srcEnabled(source) {
  return _enabledSources[source] !== false;
}

let _lastSearchResults = [];
let _searchRequestSeq = 0;   // drops responses from superseded searches
const SEARCH_PAGE_SIZE = 15;

function searchResultPills(a) {
  const pills = [];
  // Confirmation titles stay as hover tooltips; the visible ✓ MB marker is
  // rendered once to the right of the badges (see mbConfirmedTag below).
  const appleTitle = a._confirmed_apple ? ' title="Apple Music ID confirmed via MusicBrainz relationships"' : '';
  const scTitle = a._confirmed_sc ? ' title="SoundCloud profile confirmed via MusicBrainz relationships"' : '';
  const bcTitle = a._confirmed_bc ? ' title="Bandcamp profile confirmed via MusicBrainz relationships"' : '';
  if (a.source === 'soundcloud' && a.soundcloud_permalink) {
    if (_srcEnabled('soundcloud')) pills.push(`<a class="result-pill" href="https://soundcloud.com/${esc(a.soundcloud_permalink)}" target="_blank" rel="noopener"${scTitle}>${sourceIcon('soundcloud', 14)} SoundCloud ↗</a>`);
  } else {
    if (a.mbid && _srcEnabled('musicbrainz')) pills.push(`<a class="result-pill" href="https://musicbrainz.org/artist/${esc(a.mbid)}" target="_blank" rel="noopener">${sourceIcon('musicbrainz', 14)} MusicBrainz ↗</a>`);
    if (a.itunes_artist_id && _srcEnabled('itunes')) pills.push(`<a class="result-pill" href="https://music.apple.com/artist/${esc(String(a.name).toLowerCase().replace(/[^a-z0-9]/g, ''))}/${esc(a.itunes_artist_id)}" target="_blank" rel="noopener"${appleTitle}>${sourceIcon('itunes', 14)} Apple Music ↗</a>`);
    if (a.bandcamp_url && _srcEnabled('bandcamp')) pills.push(`<a class="result-pill" href="https://${esc(a.bandcamp_url)}" target="_blank" rel="noopener"${bcTitle}>${sourceIcon('bandcamp', 14)} Bandcamp ↗</a>`);
    if (a.soundcloud_permalink && _srcEnabled('soundcloud')) pills.push(`<a class="result-pill" href="https://soundcloud.com/${esc(a.soundcloud_permalink)}" target="_blank" rel="noopener"${scTitle}>${sourceIcon('soundcloud', 14)} SoundCloud ↗</a>`);
  }
  pills.push(mbConfirmedTag(a));
  if (a._link_name_only) {
    const service = a._link_service || "Service";
    pills.push(`<span class="link-match-tag" title="MusicBrainz has no ${esc(service)} link for this artist, so this is a name match from the ${esc(service)} page — check it before adding">${esc(service)} name match</span>`);
  }
  const inner = pills.join("");
  return inner ? `<div class="result-sources">${inner}</div>` : "";
}

// Visible marker placed to the right of the source badges when MB URL
// relationships confirmed the Apple Music / SoundCloud links.
function mbConfirmedTag(a) {
  const bits = [];
  if (a._confirmed_apple) bits.push("Apple Music");
  if (a._confirmed_bc) bits.push("Bandcamp");
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
  if (d.bandcamp_url) {
    if ((a.bandcamp_url || "").toLowerCase() !== String(d.bandcamp_url).toLowerCase()) {
      a.bandcamp_url = d.bandcamp_url;
      changed = true;
    }
    if (!a._confirmed_bc) { a._confirmed_bc = true; changed = true; }
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
const SEARCH_SOURCES_ALL = ["musicbrainz", "itunes", "bandcamp", "soundcloud"];
const SEARCH_SOURCE_LABELS = { musicbrainz: "MB", itunes: "Apple", bandcamp: "BC", soundcloud: "SC" };
let _searchSources = [...SEARCH_SOURCES_ALL];
try {
  const saved = JSON.parse(localStorage.getItem("mrt_search_sources") || "null");
  if (Array.isArray(saved)) {
    const valid = saved.filter(s => SEARCH_SOURCES_ALL.includes(s));
    // Selections saved before Bandcamp existed that included every old
    // platform were effectively "All"; include Bandcamp for those users.
    const oldAll = ["musicbrainz", "itunes", "soundcloud"];
    if (valid.length && !valid.includes("bandcamp") && oldAll.every(s => valid.includes(s))) {
      valid.push("bandcamp");
    }
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
  const enabledAll = SEARCH_SOURCES_ALL.filter(s => _srcEnabled(s));
  const full = show.map(s => ({ musicbrainz: "MusicBrainz", itunes: "Apple Music", bandcamp: "Bandcamp", soundcloud: "SoundCloud" }[s])).join(", ");
  btn.textContent = (enabledAll.length && show.length === enabledAll.length ? "All" : show.map(s => SEARCH_SOURCE_LABELS[s]).join("+")) + " ▾";
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

function noResultsMessage(query) {
  if (/^spotify:(?:album|track|playlist|episode|show):/i.test(query) ||
      /(?:open\.|play\.)?spotify\.com\/(?:intl-[A-Za-z-]+\/)?(?:album|track|playlist|episode|show)\//i.test(query)) {
    return "Paste a Spotify artist link (open.spotify.com/artist/…) — album and track links aren't supported here.";
  }
  const services = [
    [/^spotify:|spotify\.(?:com|link)|spoti\.fi/i, "Spotify"],
    [/tidal\.com/i, "Tidal"],
    [/qobuz\.com/i, "Qobuz"],
    [/music\.amazon\./i, "Amazon Music"],
    [/(?:music\.)?youtube\.com|youtu\.be/i, "YouTube"],
  ];
  for (const [re, label] of services) {
    if (re.test(query)) {
      return `Couldn't resolve that ${label} link to a MusicBrainz artist. Try searching by name, or paste the artist page link instead.`;
    }
  }
  return "No artists found. Check spelling or paste a direct link.";
}

async function searchArtists() {
  const query = searchInput.value.trim();
  if (!query) return;
  // Debounced typing can leave an older request in flight. Tag each search so
  // a slow earlier response can never overwrite a newer one.
  const seq = ++_searchRequestSeq;

  searchBtn.disabled = true;
  searchBtn.textContent = "Searching...";
  searchResults.innerHTML = '<p class="status-msg">Searching…</p>';

  try {
    const results = await api("/api/artists/search", {
      method: "POST",
      json: { query, sources: _searchSources },
      allowError: true,
    });
    if (seq !== _searchRequestSeq) return; // superseded by a newer search
    if (!Array.isArray(results)) {
      searchResults.innerHTML = `<p class="status-msg error">Search failed: ${esc(typeof results === 'string' ? results : JSON.stringify(results))}</p>`;
      return;
    }

    resetRowData();
    if (results.length === 0) {
      _lastSearchResults = [];
      searchResults.innerHTML = `<p class="status-msg">${esc(noResultsMessage(query))}</p>`;
      document.getElementById("search-more-wrap").classList.add("hidden");
    } else {
      _lastSearchResults = results;
      _searchVisibleCount = SEARCH_PAGE_SIZE;
      renderSearchPage();
    }
  } catch (err) {
    if (seq !== _searchRequestSeq) return;
    searchResults.innerHTML = `<p class="status-msg error">Search failed: ${esc(err.message)}</p>`;
  } finally {
    // Only the latest search owns the button state.
    if (seq === _searchRequestSeq) {
      searchBtn.disabled = false;
      searchBtn.textContent = "Search";
    }
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
  if (artist.bandcamp_url && _srcEnabled("bandcamp")) {
    sourcesToAdd.push({ source: "bandcamp", id: artist.bandcamp_url, bandcamp_band_id: artist.bandcamp_band_id || null });
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
          disambiguation: artist.disambiguation,
          bandcamp_band_id: s.bandcamp_band_id || null
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
      const hasBc = !!a.bandcamp_url;
      const hasSc = !!a.soundcloud_permalink;
      const mbOn = _srcEnabled('musicbrainz');
      const itOn = _srcEnabled('itunes');
      const bcOn = _srcEnabled('bandcamp');
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
      // Bandcamp
      if (bcOn) {
      if (hasBc) {
        sourceIcons += `<a class="source-icon-btn linked" href="https://${esc(a.bandcamp_url)}" target="_blank" rel="noopener" title="Bandcamp — linked, click to open" aria-label="Open ${esc(a.name)} on Bandcamp"><img src="/static/assets/bandcamp.svg" alt=""></a>`;
      } else {
        sourceIcons += `<button class="source-icon-btn unlinked"${dataAttrs({ action: "open-link-source", id: a.id, source: "bandcamp", name: a.name })} title="Link Bandcamp for ${esc(a.name)}" aria-label="Link Bandcamp for ${esc(a.name)}"><img src="/static/assets/bandcamp.svg" alt=""></button>`;
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
      // Bandcamp
      if (bcOn) {
      if (hasBc) {
        dropdownItems += `<button class="dropdown-item"${dataAttrs({ action: "confirm-unlink", id: a.id, source: "bandcamp" })}><img src="/static/assets/bandcamp.svg" alt="" class="dropdown-icon"> Unlink Bandcamp</button>`;
      } else {
        dropdownItems += `<button class="dropdown-item"${dataAttrs({ action: "open-link-source", id: a.id, source: "bandcamp", name: a.name })}><img src="/static/assets/bandcamp.svg" alt="" class="dropdown-icon"> Link Bandcamp</button>`;
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

// Close dropdown when clicking outside. Actions that re-render the menu
// (e.g. Edit note) remove the clicked node from the DOM, so use the event
// path captured at dispatch instead of the now-detached target.
document.addEventListener('click', e => {
  const path = typeof e.composedPath === 'function' ? e.composedPath() : [];
  const inside = path.some(n => n && n.classList && n.classList.contains('artist-manage-wrapper'))
    || (e.target.closest && e.target.closest('.artist-manage-wrapper'));
  if (!inside) _closeArtistMenus();
});

function _artistName(id) {
  const a = (_artistsCache || []).find(x => String(x.id) === String(id));
  return a ? a.name : "this artist";
}

function _artistMenuOf(btn) {
  // The dropdown belonging to the clicked item. Falls back to any open
  // dropdown so callers never silently do nothing.
  const own = btn && btn.closest ? btn.closest('.artist-dropdown') : null;
  if (own) return [own];
  return [...document.querySelectorAll('.artist-dropdown:not(.hidden)')];
}

function _artistManageButton(btn) {
  const wrapper = btn && btn.closest ? btn.closest('.artist-manage-wrapper') : null;
  return wrapper ? wrapper.querySelector('.manage-btn') : null;
}

function _closeArtistMenus() {
  document.querySelectorAll('.artist-dropdown:not(.hidden)').forEach(d => d.classList.add('hidden'));
}

function confirmRemoveArtist(id, btn) {
  const name = _artistName(id);
  const returnFocus = _artistManageButton(btn);
  _closeArtistMenus();
  openConfirmModal({
    title: "Untrack artist?",
    message: `Untrack “${name}”? All of its releases will be removed from the feed.`,
    confirmLabel: "Untrack",
    returnFocus,
    onConfirm: () => doRemoveArtist(id),
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
  const sourceLabel = source === 'musicbrainz' ? 'MusicBrainz'
    : source === 'itunes' ? 'Apple Music'
    : source === 'bandcamp' ? 'Bandcamp'
    : 'SoundCloud';
  const name = _artistName(id);
  const returnFocus = _artistManageButton(btn);
  _closeArtistMenus();
  openConfirmModal({
    title: `Unlink ${sourceLabel}?`,
    message: `Unlink ${sourceLabel} from “${name}”? Its ${sourceLabel} releases will be removed.`,
    confirmLabel: "Unlink",
    returnFocus,
    onConfirm: () => doUnlinkArtist(id, source),
  });
}

function doUnlinkArtist(id, source) {
  // MusicBrainz is the one source whose API slug is not its UI name.
  const slug = source === "musicbrainz" ? "mb" : source;
  apiRaw(`/api/artists/${id}/unlink-${slug}`, { method: "POST" }).then(() => {
    showToast("Source unlinked");
    loadArtists();
    _refreshArtistsCache();
    loadReleases();
  }).catch(() => {
    showToast("Could not unlink source — check Logs");
    loadArtists();
    _refreshArtistsCache();
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

