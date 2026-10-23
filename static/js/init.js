// --- Init ---
restoreFeedFilters();     // apply saved feed filters before the first load
navigateTo(getRoute());   // sets the initial tab and loads its data
updateUnseenBadge();
loadSettings();
refreshEnabledSources();
updateResetFiltersBtn();
document.getElementById("filter-hidden").addEventListener("change", updateResetFiltersBtn);