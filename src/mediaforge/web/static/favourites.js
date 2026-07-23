// Favourites page logic — Modernized version.
//
// Security: every value that ends up in innerHTML goes through esc() (defined in app.js).
// Event delegation is used for user actions with data-* attributes.

let _allFavs = [];
let _autoSyncUrls = new Set();
let _favSearch = "";
let _favType = "all";
let _favProvider = "all";
let _favSort = "date-desc";
let _favGroup = "off";
let _favView = localStorage.getItem("mediaforge_fav_view") || "grid"; // 'grid' | 'list'
let _selectMode = false;
let _selectedUrls = new Set();

const _favListEl = () => document.getElementById("favouritesList");

const _PLAY_SVG =
  '<svg viewBox="0 0 24 24" fill="currentColor" style="width:14px;height:14px;"><polygon points="5 3 19 12 5 21 5 3"/></svg>';

const _SYNC_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>';

const _HEART_SVG =
  '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none" style="width:14px;height:14px;"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>';

const _EMPTY_HEART_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>';

function _renderSkeleton() {
  const container = _favListEl();
  if (!container) return;
  const cards = Array(6)
    .fill(
      `<div class="fav-skeleton-card">
        <div class="fav-skeleton-poster"></div>
        <div class="fav-skeleton-body">
          <div class="fav-skeleton-line w-70"></div>
          <div class="fav-skeleton-line w-40"></div>
        </div>
      </div>`,
    )
    .join("");
  container.innerHTML = `<div class="fav-skeleton-grid">${cards}</div>`;
}

async function loadFavourites(forceRefresh = false) {
  const container = _favListEl();
  const refreshBtn = document.getElementById("favRefreshBtn");
  if (refreshBtn) refreshBtn.classList.add("spin");

  _renderSkeleton();

  try {
    const [favsRes, syncRes] = await Promise.allSettled([
      fetch("/api/favourites"),
      fetch("/api/autosync"),
    ]);

    if (favsRes.status === "fulfilled" && favsRes.value.ok) {
      const data = await favsRes.value.json();
      _allFavs = data.favourites || [];
    } else {
      _allFavs = [];
    }

    if (syncRes.status === "fulfilled" && syncRes.value.ok) {
      const syncData = await syncRes.value.json();
      const items = syncData.items || syncData.autosync || [];
      _autoSyncUrls = new Set(items.map((i) => i.series_url).filter(Boolean));
    } else {
      _autoSyncUrls = new Set();
    }

    _populateProviderFilter();
    _renderStatPills();
    renderFavourites();
  } catch (e) {
    if (container) {
      container.innerHTML =
        '<div class="favourites-empty">' +
        esc(t("Fehler beim Laden der Favoriten.", "Error loading favourites.")) +
        "</div>";
    }
  } finally {
    if (refreshBtn) refreshBtn.classList.remove("spin");
  }
}

function _typeLabel(mediaType) {
  if (mediaType === "movie") return t("Film", "Movie");
  if (mediaType === "series") return t("Serie", "Series");
  return "";
}

function _populateProviderFilter() {
  const providerSelect = document.getElementById("favProvider");
  if (!providerSelect) return;

  const currentVal = providerSelect.value || "all";
  const providers = new Set();
  _allFavs.forEach((f) => {
    if (f.provider) providers.add(f.provider);
  });

  const sortedProviders = Array.from(providers).sort((a, b) => a.localeCompare(b));
  let html = `<option value="all">${esc(t("Alle Quellen", "All sources"))}</option>`;
  sortedProviders.forEach((p) => {
    html += `<option value="${esc(p)}">${esc(p)}</option>`;
  });

  providerSelect.innerHTML = html;
  if (providers.has(currentVal)) {
    providerSelect.value = currentVal;
    _favProvider = currentVal;
  } else {
    providerSelect.value = "all";
    _favProvider = "all";
  }
}

function _renderStatPills() {
  const pillsEl = document.getElementById("favSummaryPills");
  if (!pillsEl) return;

  if (!_allFavs.length) {
    pillsEl.innerHTML = "";
    return;
  }

  const seriesCount = _allFavs.filter((f) => f.media_type === "series").length;
  const movieCount = _allFavs.filter((f) => f.media_type === "movie").length;
  const providers = new Set(_allFavs.map((f) => f.provider).filter(Boolean));

  pillsEl.innerHTML = `
    <div class="fav-summary-pill">
      <span>${esc(t("Gesamt", "Total"))}:</span> <b>${_allFavs.length}</b>
    </div>
    ${
      seriesCount > 0
        ? `<div class="fav-summary-pill fav-summary-pill--series">
             <span>${esc(t("Serien", "Series"))}:</span> <b>${seriesCount}</b>
           </div>`
        : ""
    }
    ${
      movieCount > 0
        ? `<div class="fav-summary-pill fav-summary-pill--movie">
             <span>${esc(t("Filme", "Movies"))}:</span> <b>${movieCount}</b>
           </div>`
        : ""
    }
    ${
      providers.size > 0
        ? `<div class="fav-summary-pill fav-summary-pill--source">
             <span>${esc(t("Quellen", "Sources"))}:</span> <b>${providers.size}</b>
           </div>`
        : ""
    }
  `;
}

function _groupValue(f) {
  if (_favGroup === "provider") return f.provider || t("Unbekannte Quelle", "Unknown source");
  if (_favGroup === "type") return _typeLabel(f.media_type) || t("Unbekannt", "Unknown");
  if (_favGroup === "language") return f.language || t("Keine Sprache", "No language");
  return "";
}

function _filteredFavs() {
  let list = _allFavs;

  if (_favSearch) {
    list = list.filter((f) => (f.title || "").toLowerCase().includes(_favSearch));
  }

  if (_favType !== "all") {
    list = list.filter((f) => (f.media_type || "series") === _favType);
  }

  if (_favProvider !== "all") {
    list = list.filter((f) => f.provider === _favProvider);
  }

  const [key, dir] = _favSort.split("-");
  const mult = dir === "desc" ? -1 : 1;
  return [...list].sort((a, b) => {
    if (key === "name") return mult * (a.title || "").localeCompare(b.title || "");
    const av = a.created_at || "";
    const bv = b.created_at || "";
    return mult * (av < bv ? -1 : av > bv ? 1 : 0);
  });
}

function _favCard(f) {
  const url = esc(f.series_url || "");
  const title = esc(f.title || "");
  const poster = esc(proxyImg(f.poster_url || ""));
  const date = f.created_at ? esc(String(f.created_at).slice(0, 10)) : "";
  const isMovie = f.media_type === "movie";
  const isAutoSynced = _autoSyncUrls.has(f.series_url);
  const isSelected = _selectedUrls.has(f.series_url);

  let topBadges = "";
  const typeLbl = _typeLabel(f.media_type);
  if (typeLbl) topBadges += `<span class="fav-badge fav-badge-type">${esc(typeLbl)}</span>`;
  if (isAutoSynced)
    topBadges += `<span class="fav-badge fav-badge-autosync" title="${esc(
      t("Auto-Sync aktiv", "Auto-Sync active"),
    )}">✓ Auto-Sync</span>`;

  const syncBtn = !isMovie
    ? `<button type="button" class="btn btn-sm ${
        isAutoSynced ? "btn-secondary" : "btn-ghost"
      } fav-btn fav-btn-icon" data-action="autosync" data-url="${url}" title="${esc(
        isAutoSynced ? t("Auto-Synced", "Auto-Synced") : t("Zu Auto-Sync hinzufügen", "Add to Auto-Sync"),
      )}">${_SYNC_SVG}</button>`
    : "";

  const checkbox = _selectMode
    ? `<input type="checkbox" class="fav-checkbox fav-poster-checkbox" data-url="${url}" ${
        isSelected ? "checked" : ""
      } />`
    : "";

  return `
    <div class="fav-grid-card ${_selectMode ? "selectable" : ""} ${
    isSelected ? "selected" : ""
  }" data-url="${url}">
      ${checkbox}
      <div class="fav-poster-container">
        <button type="button" class="fav-poster-heart" data-action="remove" data-url="${url}" title="${esc(
    t("Aus Favoriten entfernen", "Remove from favourites"),
  )}">${_HEART_SVG}</button>
        ${topBadges ? `<div class="fav-poster-badges">${topBadges}</div>` : ""}
        <img class="fav-poster-img" src="${poster}" alt="${title}"
             onerror="this.style.display='none'" loading="lazy" />
        <div class="fav-poster-overlay" data-action="open" data-url="${url}">
          <div class="fav-poster-play-btn">${_PLAY_SVG}<span>${esc(t("Öffnen", "Open"))}</span></div>
        </div>
      </div>
      <div class="fav-card-body">
        <div class="fav-card-title" title="${title}">${title}</div>
        <div class="fav-card-meta-row">
          ${f.provider ? `<span class="fav-badge">${esc(f.provider)}</span>` : ""}
          ${date ? `<span class="fav-card-date">${date}</span>` : ""}
        </div>
        <div class="fav-card-actions">
          <button type="button" class="btn btn-sm btn-primary fav-btn fav-btn-full" data-action="open" data-url="${url}">${esc(
            t("Öffnen", "Open"),
          )}</button>
          ${syncBtn}
          <button type="button" class="btn btn-sm btn-danger fav-btn fav-btn-icon" data-action="remove" data-url="${url}" title="${esc(
            t("Entfernen", "Remove"),
          )}">✕</button>
        </div>
      </div>
    </div>`;
}

function _favListCard(f) {
  const url = esc(f.series_url || "");
  const title = esc(f.title || "");
  const poster = esc(proxyImg(f.poster_url || ""));
  const date = f.created_at ? esc(String(f.created_at).slice(0, 10)) : "";
  const isMovie = f.media_type === "movie";
  const isAutoSynced = _autoSyncUrls.has(f.series_url);
  const isSelected = _selectedUrls.has(f.series_url);

  let badges = "";
  const typeLbl = _typeLabel(f.media_type);
  if (typeLbl) badges += `<span class="fav-badge fav-badge-type">${esc(typeLbl)}</span>`;
  if (f.provider) badges += `<span class="fav-badge">${esc(f.provider)}</span>`;
  if (f.language) badges += `<span class="fav-badge">${esc(f.language)}</span>`;
  if (isAutoSynced)
    badges += `<span class="fav-badge fav-badge-autosync">✓ Auto-Sync</span>`;

  const checkbox = _selectMode
    ? `<div class="fav-checkbox-wrap">
         <input type="checkbox" class="fav-checkbox" data-url="${url}" ${
           isSelected ? "checked" : ""
         } />
       </div>`
    : "";

  return `
    <div class="fav-list-row ${_selectMode ? "selectable" : ""} ${
    isSelected ? "selected" : ""
  }" data-url="${url}">
      ${checkbox}
      <div class="fav-list-poster-wrap" data-action="open" data-url="${url}" style="cursor:pointer;">
        <img class="fav-list-poster" src="${poster}" alt="${title}" loading="lazy" onerror="this.style.display='none'" />
      </div>
      <div class="fav-list-main">
        <div class="fav-list-title" title="${title}">${title}</div>
        <div class="fav-list-meta">
          ${badges ? badges : ""}
          ${date ? `<span class="fav-card-date">${date}</span>` : ""}
        </div>
      </div>
      <div class="fav-list-actions">
        <button type="button" class="btn btn-sm btn-primary fav-btn" data-action="open" data-url="${url}">${esc(
          t("Öffnen", "Open"),
        )}</button>
        ${
          !isMovie
            ? `<button type="button" class="btn btn-sm ${
                isAutoSynced ? "btn-secondary" : "btn-ghost"
              } fav-btn" data-action="autosync" data-url="${url}" title="${esc(
                t("Auto-Sync", "Auto-Sync"),
              )}">${_SYNC_SVG}</button>`
            : ""
        }
        <button type="button" class="btn btn-sm btn-danger fav-btn" data-action="remove" data-url="${url}" title="${esc(
          t("Entfernen", "Remove"),
        )}">✕</button>
      </div>
    </div>`;
}

function _emptyState(container) {
  container.innerHTML = `
    <div class="favourites-empty">
      ${_EMPTY_HEART_SVG}
      <span class="favourites-empty-title">${esc(
        t("Keine Favoriten gespeichert.", "No favourites saved."),
      )}</span>
      <span class="favourites-empty-text">${esc(
        t("Füge Serien oder Filme über die Suche hinzu.", "Add series or movies via the search page."),
      )}</span>
      <a href="/" class="btn btn-sm btn-primary" style="margin-top:8px;">${esc(
        t("Zur Suche", "Go to search"),
      )}</a>
    </div>`;
}

function renderFavourites() {
  const container = _favListEl();
  if (!container) return;

  const controls = document.getElementById("favControls");
  const countEl = document.getElementById("favCount");
  const clearBtn = document.getElementById("favSearchClear");

  if (clearBtn) {
    clearBtn.style.display = _favSearch ? "block" : "none";
  }

  if (!_allFavs.length) {
    if (controls) controls.style.display = "none";
    if (countEl) countEl.textContent = "";
    _emptyState(container);
    return;
  }
  if (controls) controls.style.display = "";

  const list = _filteredFavs();

  if (countEl) {
    countEl.textContent =
      list.length < _allFavs.length
        ? `${list.length} ${t("von", "of")} ${_allFavs.length} ${t("Favoriten", "favourites")}`
        : `${_allFavs.length} ${t("Favoriten", "favourites")}`;
  }

  if (!list.length) {
    container.innerHTML = `
      <div class="favourites-empty">
        ${_EMPTY_HEART_SVG}
        <span class="favourites-empty-title">${esc(
          t("Keine Treffer für deine Filter.", "No matches for your filters."),
        )}</span>
        <button type="button" class="btn btn-sm btn-ghost" onclick="_resetFilters()">${esc(
          t("Filter zurücksetzen", "Reset filters"),
        )}</button>
      </div>`;
    return;
  }

  const renderCardFn = _favView === "list" ? _favListCard : _favCard;
  const containerClass = _favView === "list" ? "fav-list-mode" : "fav-grid";

  if (_favGroup === "off") {
    container.innerHTML = `<div class="${containerClass}">${list
      .map(renderCardFn)
      .join("")}</div>`;
  } else {
    const groups = {};
    list.forEach((f) => {
      const k = _groupValue(f);
      (groups[k] = groups[k] || []).push(f);
    });
    const keys = Object.keys(groups).sort((a, b) => a.localeCompare(b));
    container.innerHTML = keys
      .map(
        (k) => `
        <div class="fav-group">
          <div class="fav-group-header">
            <span class="fav-group-name">${esc(k)}</span>
            <span class="fav-group-count">${groups[k].length}</span>
          </div>
          <div class="${containerClass}">${groups[k].map(renderCardFn).join("")}</div>
        </div>`,
      )
      .join("");
  }

  _updateBulkBar();
}

function _resetFilters() {
  _favSearch = "";
  _favType = "all";
  _favProvider = "all";

  const searchInput = document.getElementById("favSearch");
  if (searchInput) searchInput.value = "";

  const providerSelect = document.getElementById("favProvider");
  if (providerSelect) providerSelect.value = "all";

  const typeBtns = document.querySelectorAll("#favTypeSegmented .fav-segmented-btn");
  typeBtns.forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.type === "all");
  });

  renderFavourites();
}

async function _removeFavourite(seriesUrl, btn) {
  if (btn) btn.disabled = true;
  try {
    await fetch("/api/favourites", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ series_url: seriesUrl }),
    });
    _allFavs = _allFavs.filter((f) => f.series_url !== seriesUrl);
    _selectedUrls.delete(seriesUrl);
    _renderStatPills();
    renderFavourites();
    if (window.showToast) showToast(t("Aus Favoriten entfernt", "Removed from favourites"));
  } catch (e) {
    if (btn) btn.disabled = false;
    if (window.showToast) showToast(t("Fehler: ", "Error: ") + e.message);
  }
}

async function _removeBulkFavourites() {
  const urls = Array.from(_selectedUrls);
  if (!urls.length) return;

  if (
    !confirm(
      t(
        `${urls.length} Favoriten wirklich entfernen?`,
        `Really remove ${urls.length} favourites?`,
      ),
    )
  )
    return;

  try {
    await fetch("/api/favourites", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls }),
    });
    _allFavs = _allFavs.filter((f) => !_selectedUrls.has(f.series_url));
    _selectedUrls.clear();
    _renderStatPills();
    renderFavourites();
    if (window.showToast)
      showToast(t("Ausgewählte Favoriten entfernt", "Removed selected favourites"));
  } catch (e) {
    if (window.showToast) showToast(t("Fehler beim Löschen: ", "Error deleting: ") + e.message);
  }
}

function _updateBulkBar() {
  const bulkBar = document.getElementById("favBulkBar");
  const bulkCount = document.getElementById("favBulkCount");
  if (!bulkBar || !bulkCount) return;

  if (_selectMode) {
    bulkBar.style.display = "flex";
    bulkCount.textContent = _selectedUrls.size;
  } else {
    bulkBar.style.display = "none";
  }
}

function _toggleSelectMode() {
  _selectMode = !_selectMode;
  if (!_selectMode) _selectedUrls.clear();

  const toggleBtn = document.getElementById("favSelectToggleBtn");
  if (toggleBtn) toggleBtn.classList.toggle("active", _selectMode);

  renderFavourites();
}

function _onFavListClick(e) {
  const checkbox = e.target.closest(".fav-checkbox");
  if (checkbox) {
    const url = checkbox.dataset.url;
    if (checkbox.checked) {
      _selectedUrls.add(url);
    } else {
      _selectedUrls.delete(url);
    }
    _updateBulkBar();
    return;
  }

  const btn = e.target.closest("[data-action]");
  if (!btn) return;

  const url = btn.dataset.url || "";
  const action = btn.dataset.action;

  if (action === "open") {
    window.location.href = "/?open=" + encodeURIComponent(url);
  } else if (action === "autosync") {
    window.location.href = "/?open=" + encodeURIComponent(url) + "&autosync=1";
  } else if (action === "remove") {
    _removeFavourite(url, btn);
  }
}

function _bindFavControls() {
  // Search Input
  const search = document.getElementById("favSearch");
  if (search) {
    search.addEventListener("input", () => {
      _favSearch = (search.value || "").trim().toLowerCase();
      renderFavourites();
    });
  }

  // Clear Search Button
  const clearBtn = document.getElementById("favSearchClear");
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      if (search) search.value = "";
      _favSearch = "";
      renderFavourites();
    });
  }

  // Type Segmented Filter
  const typeGroup = document.getElementById("favTypeSegmented");
  if (typeGroup) {
    typeGroup.addEventListener("click", (e) => {
      const btn = e.target.closest(".fav-segmented-btn");
      if (!btn) return;
      typeGroup
        .querySelectorAll(".fav-segmented-btn")
        .forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      _favType = btn.dataset.type || "all";
      renderFavourites();
    });
  }

  // Provider Select Filter
  const providerSelect = document.getElementById("favProvider");
  if (providerSelect) {
    providerSelect.addEventListener("change", () => {
      _favProvider = providerSelect.value;
      renderFavourites();
    });
  }

  // Sort Select
  const sort = document.getElementById("favSort");
  if (sort) {
    sort.addEventListener("change", () => {
      _favSort = sort.value;
      renderFavourites();
    });
  }

  // Group Select
  const group = document.getElementById("favGroup");
  if (group) {
    group.addEventListener("change", () => {
      _favGroup = group.value;
      renderFavourites();
    });
  }

  // View Mode Grid/List
  const gridBtn = document.getElementById("favViewGrid");
  const listBtn = document.getElementById("favViewList");
  if (gridBtn && listBtn) {
    if (_favView === "list") {
      gridBtn.classList.remove("active");
      listBtn.classList.add("active");
    } else {
      gridBtn.classList.add("active");
      listBtn.classList.remove("active");
    }

    gridBtn.addEventListener("click", () => {
      _favView = "grid";
      localStorage.setItem("mediaforge_fav_view", "grid");
      gridBtn.classList.add("active");
      listBtn.classList.remove("active");
      renderFavourites();
    });

    listBtn.addEventListener("click", () => {
      _favView = "list";
      localStorage.setItem("mediaforge_fav_view", "list");
      listBtn.classList.add("active");
      gridBtn.classList.remove("active");
      renderFavourites();
    });
  }

  // Select Mode Toggle
  const selectToggleBtn = document.getElementById("favSelectToggleBtn");
  if (selectToggleBtn) {
    selectToggleBtn.addEventListener("click", _toggleSelectMode);
  }

  // Bulk Actions
  const bulkSelectAll = document.getElementById("favBulkSelectAll");
  if (bulkSelectAll) {
    bulkSelectAll.addEventListener("click", () => {
      const list = _filteredFavs();
      list.forEach((f) => _selectedUrls.add(f.series_url));
      renderFavourites();
    });
  }

  const bulkDeselectAll = document.getElementById("favBulkDeselectAll");
  if (bulkDeselectAll) {
    bulkDeselectAll.addEventListener("click", () => {
      _selectedUrls.clear();
      renderFavourites();
    });
  }

  const bulkRemoveBtn = document.getElementById("favBulkRemoveBtn");
  if (bulkRemoveBtn) {
    bulkRemoveBtn.addEventListener("click", _removeBulkFavourites);
  }

  const bulkAutoSyncBtn = document.getElementById("favBulkAutoSyncBtn");
  if (bulkAutoSyncBtn) {
    bulkAutoSyncBtn.addEventListener("click", () => {
      const urls = Array.from(_selectedUrls);
      if (!urls.length) return;
      window.location.href = "/?open=" + encodeURIComponent(urls[0]) + "&autosync=1";
    });
  }

  // Delegated Click Handler
  const list = _favListEl();
  if (list) list.addEventListener("click", _onFavListClick);
}

_bindFavControls();
loadFavourites();
