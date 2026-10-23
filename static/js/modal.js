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

