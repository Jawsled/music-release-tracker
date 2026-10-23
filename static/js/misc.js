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

