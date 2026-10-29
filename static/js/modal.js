// --- Confirmation Modal ---
const confirmModal = document.getElementById("confirm-modal");
const confirmModalTitle = document.getElementById("confirm-modal-title");
const confirmModalMessage = document.getElementById("confirm-modal-message");
const confirmModalYes = document.getElementById("confirm-modal-yes");
const confirmModalCancel = document.getElementById("confirm-modal-cancel");
const confirmModalClose = document.getElementById("confirm-modal-close");

let _confirmCallback = null;
let _confirmReturnFocus = null;

function openConfirmModal({ title, message, confirmLabel = "Confirm", onConfirm, returnFocus = null }) {
  _confirmReturnFocus = returnFocus || document.activeElement;
  _confirmCallback = typeof onConfirm === "function" ? onConfirm : null;
  confirmModalTitle.textContent = title || "Confirm";
  confirmModalMessage.textContent = message || "";
  confirmModalYes.textContent = confirmLabel;
  confirmModal.classList.remove("hidden");
  confirmModalCancel.focus();
}

function closeConfirmModal() {
  confirmModal.classList.add("hidden");
  _confirmCallback = null;
  if (_confirmReturnFocus && _confirmReturnFocus.isConnected) {
    _confirmReturnFocus.focus();
  }
  _confirmReturnFocus = null;
}

confirmModalYes.addEventListener("click", () => {
  const callback = _confirmCallback;
  closeConfirmModal();
  if (callback) callback();
});
confirmModalCancel.addEventListener("click", closeConfirmModal);
confirmModalClose.addEventListener("click", closeConfirmModal);
confirmModal.addEventListener("click", e => {
  if (e.target === confirmModal) closeConfirmModal();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !confirmModal.classList.contains("hidden")) {
    closeConfirmModal();
  }
});

// --- Link Source Modal ---
const linkSourceModal = document.getElementById("link-source-modal");
const linkSourceTitle = document.getElementById("link-source-title");
const linkSourceInput = document.getElementById("link-source-input");
const linkSourceSearchBtn = document.getElementById("link-source-search-btn");
const linkSourceResults = document.getElementById("link-source-results");

let currentLinkArtistId = null;
let currentLinkSource = "";
let _linkSearchSeq = 0;   // drops responses from superseded link searches

linkSourceSearchBtn.addEventListener("click", () => searchLinkSource());
linkSourceInput.addEventListener("keydown", e => { if (e.key === "Enter") searchLinkSource(); });

function openLinkSourceModal(artistId, artistName, source) {
  currentLinkArtistId = artistId;
  currentLinkSource = source;

  const label = source === "musicbrainz" ? "MusicBrainz"
    : source === "itunes" ? "Apple Music"
    : source === "bandcamp" ? "Bandcamp"
    : source === "soundcloud" ? "SoundCloud"
    : "Source";
  linkSourceTitle.textContent = `Link ${label} for ${artistName}`;
  document.getElementById("link-source-hint").textContent =
    source === "soundcloud"
      ? "Paste a SoundCloud URL or permalink, then Search."
      : source === "bandcamp"
        ? "Paste a Bandcamp artist URL (artist.bandcamp.com), then Search. Bandcamp has no name search."
        : source === "musicbrainz"
          ? `Showing matches for “${artistName}” — refine, or paste a MusicBrainz, Spotify, Tidal, Qobuz, Amazon Music or YouTube artist link, then Search.`
          : `Showing matches for “${artistName}” — refine or paste a link, then Search.`;

  // Always visible input, pre-filled with artist name (or guessed profile URL)
  if (source === "soundcloud") {
    const slug = artistName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    linkSourceInput.value = `https://soundcloud.com/${slug}`;
  } else if (source === "bandcamp") {
    const slug = artistName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    linkSourceInput.value = `https://${slug}.bandcamp.com`;
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
  _linkSearchSeq++;   // cancel any in-flight search
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
  const seq = ++_linkSearchSeq;
  linkSourceResults.innerHTML = '<p class="status-msg">Searching...</p>';

  try {
    const results = await api("/api/artists/search", {
      method: "POST",
      json: {
        query,
        source: currentLinkSource === "musicbrainz" ? "musicbrainz" :
                currentLinkSource === "itunes" ? "itunes" :
                currentLinkSource === "bandcamp" ? "bandcamp" :
                currentLinkSource === "soundcloud" ? "soundcloud" : ""
      },
      allowError: true,
    });
    if (seq !== _linkSearchSeq) return; // superseded or modal closed
    if (!Array.isArray(results)) {
      linkSourceResults.innerHTML = `<p class="status-msg error">Search failed. <button class="link-btn"${dataAttrs({ action: "search-link-source" })}>Retry</button></p>`;
      return;
    }

    if (results.length === 0) {
      linkSourceResults.innerHTML = `<p class="status-msg">${esc(noResultsMessage(query))}</p>`;
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
    if (seq !== _linkSearchSeq) return;
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
    } else if (currentLinkSource === "bandcamp" && artist.bandcamp_url) {
      sourceToLink = "bandcamp";
      idToLink = artist.bandcamp_url;
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
      } else if (artist.bandcamp_url) {
        sourceToLink = "bandcamp";
        idToLink = artist.bandcamp_url;
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
    if (sourceToLink === "bandcamp" && currentLinkArtistId != null) {
      payload.artist_id = currentLinkArtistId;
      if (artist.bandcamp_band_id) payload.bandcamp_band_id = artist.bandcamp_band_id;
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

// --- Album Art Download Modal ---
// The feed cover is a button; clicking it opens this dialog so the user can
// pick a resolution. The backend resolves the provider's larger variants
// (Apple, SoundCloud, Bandcamp, Cover Art Archive) and reports the real size
// it found, which is what the toast shows after a download.
const artworkModal = document.getElementById("artwork-modal");
const artworkPreview = document.getElementById("artwork-preview");
const artworkReleaseEl = document.getElementById("artwork-modal-release");
const artworkArtistEl = document.getElementById("artwork-modal-artist");
const artworkHint = document.getElementById("artwork-size-hint");
const artworkDownloadBtn = document.getElementById("artwork-download-btn");
const artworkCancelBtn = document.getElementById("artwork-cancel");
const artworkCloseBtn = document.getElementById("artwork-modal-close");
const ARTWORK_SIZE_KEY = "mrt_artwork_download_size";
const ARTWORK_DEFAULT_SIZE = 1000;

let _artworkTarget = null;
let _artworkReturnFocus = null;
let _artworkRequestSeq = 0;   // drops results from closed/superseded dialogs

function _artworkSizeInputs() {
  return artworkModal.querySelectorAll('input[name="artwork-size"]');
}

function _artworkSelectedSize() {
  const checked = artworkModal.querySelector('input[name="artwork-size"]:checked');
  return checked ? Number(checked.value) : ARTWORK_DEFAULT_SIZE;
}

function _artworkSetSelectedSize(size) {
  const value = String(size);
  let found = false;
  _artworkSizeInputs().forEach(input => {
    const match = input.value === value;
    input.checked = match;
    if (match) found = true;
  });
  if (!found) {
    _artworkSizeInputs().forEach(input => {
      input.checked = input.value === String(ARTWORK_DEFAULT_SIZE);
    });
  }
}

function _savedArtworkSize() {
  try {
    const raw = localStorage.getItem(ARTWORK_SIZE_KEY);
    if (raw !== null) {
      const value = Number(raw);
      if ([0, 500, 1000, 1500, 2000].includes(value)) return value;
    }
  } catch { /* storage unavailable — keep the default */ }
  return ARTWORK_DEFAULT_SIZE;
}

function openArtworkDownload(btn) {
  if (!btn) return;
  _artworkRequestSeq++;   // supersede any download still in flight
  _artworkTarget = {
    source: btn.dataset.source || "",
    mbid: btn.dataset.mbid || "",
    artworkUrl: btn.dataset.artworkUrl || "",
    artist: btn.dataset.artist || "",
    title: btn.dataset.title || "",
    preview: btn.dataset.coverSrc || "",
  };
  _artworkReturnFocus = btn;

  artworkPreview.style.display = "";
  artworkPreview.src = _artworkTarget.preview;
  artworkPreview.alt = _artworkTarget.title ? `Album art for ${_artworkTarget.title}` : "";
  artworkReleaseEl.textContent = _artworkTarget.title || "Untitled release";
  artworkArtistEl.textContent = _artworkTarget.artist || "";
  _artworkSetSelectedSize(_savedArtworkSize());
  artworkHint.textContent = "The closest available resolution is used.";
  artworkHint.classList.remove("error");
  artworkDownloadBtn.disabled = false;
  artworkDownloadBtn.textContent = "Download";
  artworkModal.classList.remove("hidden");
  artworkDownloadBtn.focus();
}

function closeArtworkDownload() {
  _artworkRequestSeq++;   // a closed dialog must not act on a late response
  artworkModal.classList.add("hidden");
  artworkPreview.removeAttribute("src");
  _artworkTarget = null;
  if (_artworkReturnFocus && _artworkReturnFocus.isConnected) {
    _artworkReturnFocus.focus();
  }
  _artworkReturnFocus = null;
}

function _artworkFilename(artist, title, width, height, contentType) {
  const extension = contentType === "image/png" ? "png"
    : contentType === "image/webp" ? "webp"
    : contentType === "image/gif" ? "gif"
    : "jpg";
  const base = [artist, title]
    .filter(part => part && part.trim())
    .join(" - ")
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 120) || "album-art";
  const dimensions = width && height ? `${width}x${height}` : "artwork";
  return `${base} (${dimensions}).${extension}`;
}

async function downloadArtwork() {
  const target = _artworkTarget;
  if (!target) return;
  const seq = ++_artworkRequestSeq;
  const size = _artworkSelectedSize();
  const params = new URLSearchParams();
  params.set("source", target.source);
  params.set("size", String(size));
  if (target.mbid) params.set("mbid", target.mbid);
  if (target.artworkUrl) params.set("artwork_url", target.artworkUrl);
  if (target.artist) params.set("artist", target.artist);
  if (target.title) params.set("title", target.title);

  const idleLabel = artworkDownloadBtn.textContent;
  artworkDownloadBtn.disabled = true;
  artworkDownloadBtn.textContent = "Downloading…";
  artworkHint.textContent = "Fetching the best available artwork…";
  artworkHint.classList.remove("error");

  try {
    const resp = await apiRaw(`/api/artwork/download?${params}`, { allowError: true });
    if (seq !== _artworkRequestSeq) return;
    if (!resp.ok) {
      let message = "No artwork available for this release.";
      try {
        const data = await resp.json();
        if (data && data.message) message = data.message;
      } catch { /* non-JSON error body */ }
      throw new Error(message);
    }

    const blob = await resp.blob();
    if (seq !== _artworkRequestSeq) return;
    const width = resp.headers.get("X-Image-Width") || "";
    const height = resp.headers.get("X-Image-Height") || "";
    const filename = _artworkFilename(
      target.artist,
      target.title,
      width,
      height,
      blob.type,
    );

    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);

    try { localStorage.setItem(ARTWORK_SIZE_KEY, String(size)); } catch { /* ignore */ }
    const actual = width && height ? ` (${width} × ${height})` : "";
    showToast(`Album art downloaded${actual}`);
    closeArtworkDownload();
  } catch (err) {
    if (seq !== _artworkRequestSeq) return;
    artworkHint.textContent = err && err.message
      ? err.message
      : "Could not download artwork.";
    artworkHint.classList.add("error");
    artworkDownloadBtn.disabled = false;
    artworkDownloadBtn.textContent = idleLabel || "Download";
  }
}

artworkDownloadBtn.addEventListener("click", downloadArtwork);
artworkCancelBtn.addEventListener("click", closeArtworkDownload);
artworkCloseBtn.addEventListener("click", closeArtworkDownload);
artworkModal.addEventListener("click", e => {
  if (e.target === artworkModal) closeArtworkDownload();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !artworkModal.classList.contains("hidden")) {
    closeArtworkDownload();
  }
});

