console.log(">>> App JS Version: 1.6 - Unified App & Advanced Search <<<");
const searchInput = document.getElementById("searchInput");
const searchBtn = document.getElementById("searchBtn");
const searchSpinner = document.getElementById("searchSpinner");
const resultsDiv = document.getElementById("results");
const overlay = document.getElementById("overlay");
const languageSelect = document.getElementById("languageSelect");
const providerSelect = document.getElementById("providerSelect");
const seasonAccordion = document.getElementById("seasonAccordion");
const episodeSpinner = document.getElementById("episodeSpinner");
const selectAllCb = document.getElementById("selectAll");
const autoSyncConfigBtn = document.getElementById("autoSyncConfigBtn");
const autoSyncConfigLabel = document.getElementById("autoSyncConfigLabel");
let _currentSyncJob = null; // existing autosync job for the open series, or null
let _customPathsCache = [];
const statusBar = document.getElementById("statusBar");
const statusText = document.getElementById("statusText");
const downloadAllBtn = document.getElementById("downloadAllBtn");
const downloadSelectedBtn = document.getElementById("downloadSelectedBtn");
const browseDiv = document.getElementById("browse");
const newAnimesGrid = document.getElementById("newAnimesGrid");
const popularAnimesGrid = document.getElementById("popularAnimesGrid");
const newAnimesSection = document.getElementById("newAnimesSection");
const popularAnimesSection = document.getElementById("popularAnimesSection");
const newSeriesGrid = document.getElementById("newSeriesGrid");
const popularSeriesGrid = document.getElementById("popularSeriesGrid");
const newSeriesSection = document.getElementById("newSeriesSection");
const popularSeriesSection = document.getElementById("popularSeriesSection");
const newMoviesGrid = document.getElementById("newMoviesGrid");
const newMoviesSection = document.getElementById("newMoviesSection");
const megakinoNewMoviesGrid = document.getElementById("megakinoNewMoviesGrid");
const megakinoPopularMoviesGrid = document.getElementById("megakinoPopularMoviesGrid");
const megakinoNewSeriesGrid = document.getElementById("megakinoNewSeriesGrid");
const megakinoPopularSeriesGrid = document.getElementById("megakinoPopularSeriesGrid");
const hanimeNewGrid = document.getElementById("hanimeNewGrid");
const hanimeTrendingGrid = document.getElementById("hanimeTrendingGrid");

let currentSeasons = [];
let currentSeriesTitle = "";
let currentSeriesUrl = "";
// Bumped by every openSeries() call; each call captures its own value and
// checks it against this after every await before writing to the modal DOM.
// Without this, opening series B while series A's fetches (openSeries,
// buildAccordion, enrichModalWithTmdb — several independent async chains,
// none awaited by the others) are still in flight lets A's late-arriving
// response overwrite fields A wrote before B took over — a genuine bug seen
// in production (mixed titles/genres/episodes from two different series in
// one modal). Any continuation whose captured value no longer matches this
// counter belongs to a superseded openSeries() call and must bail out.
let _seriesLoadSeq = 0;
// Provider data per language label
let availableProviders = null;
let langSeparationEnabled = false;
// Static list of providers rendered into the template
const staticProviders = providerSelect ? Array.from(providerSelect.options).map((o) => o.value) : [];


// Site toggle state
let currentSite = "aniworld"; // kept for modal language detection via URL

let _upscaleModeCache = null;
let _upscaleModePromise = null;
function _loadUpscaleMode(force) {
  if (_upscaleModePromise && !force) return _upscaleModePromise;
  _upscaleModePromise = fetch("/api/upscale/settings")
    .then(r => r.json())
    .then(d => { _upscaleModeCache = (d.settings && d.settings.mode) || "disabled"; return _upscaleModeCache; })
    .catch(() => { _upscaleModeCache = "disabled"; return _upscaleModeCache; });
  return _upscaleModePromise;
}
// Preload once at startup so the cache is ready before any modal opens.
_loadUpscaleMode();

function _applyUpscaleCheckbox(url, mode, respectUserChoice) {
  const wrapper = document.getElementById("upscaleCheckWrapper");
  const check = document.getElementById("upscaleCheck");
  if (!wrapper || !check) return;
  if (!mode || mode === "disabled") {
    wrapper.style.display = "none";
    check.checked = false;
    return;
  }
  wrapper.style.display = "";
  // Never overwrite a box the user has already toggled since the modal opened.
  if (!respectUserChoice || !check.dataset.userTouched) {
    // Default: checked for aniworld.to, unchecked for others
    check.checked = (url || "").includes("aniworld.to");
  }
}

function _updateUpscaleCheckbox(url) {
  const wrapper = document.getElementById("upscaleCheckWrapper");
  const check = document.getElementById("upscaleCheck");
  if (!wrapper || !check) return;
  // Fresh modal open: forget the previous manual toggle, and (once) attach a
  // guard that records any future user interaction with the box.
  delete check.dataset.userTouched;
  if (!check._upscaleTouchBound) {
    check._upscaleTouchBound = true;
    check.addEventListener("change", () => { check.dataset.userTouched = "1"; });
  }
  if (_upscaleModeCache !== null) {
    // Cache ready -> configure synchronously, before the user can interact.
    // No async callback is left that could overwrite their later choice.
    _applyUpscaleCheckbox(url, _upscaleModeCache, false);
  } else {
    // Very first open before the preload resolved: apply once it lands, but do
    // not clobber the box if the user has already ticked it in the meantime.
    _loadUpscaleMode().then(mode => _applyUpscaleCheckbox(url, mode, true));
  }
}

// Downloaded folders cache
let downloadedFolders = [];

// MediaScan: TMDB/IMDB ID sets populated when source = mediascan
let mediascanTmdbIds = new Set();
let mediascanImdbIds = new Set();
let mediascanTitles = new Set(); // normalised titles from Plex/Jellyfin as fallback
let mediascanActive = false;  // true when source is plex/jellyfin (not folders)

// Auto-Sync URLs set (series_url -> job object)
let autoSyncUrlMap = {};

// CineInfo display settings (cached)
let cineinfoSettings = null;
let generalSettings = null;
let crunchyrollSettings = null;
let fernsehserienSettings = null;

let _generalSettingsPromise = null;
function loadGeneralSettings() {
  if (!_generalSettingsPromise) {
    _generalSettingsPromise = (async () => {
      try {
        const resp = await fetch("/api/settings");
        const data = await resp.json();
        generalSettings = data;
        cineinfoSettings = data.cineinfo || {};
        crunchyrollSettings = data.crunchyroll || {};
        fernsehserienSettings = data.fernsehserien || {};
        console.log("[General] Settings loaded (combined):", generalSettings);
        _reEnrichPendingCards();
        _reEnrichCrunchyrollCards();
        return generalSettings;
      } catch (e) {
        console.error("[General] Failed to load settings:", e);
        generalSettings = {};
        cineinfoSettings = {};
        crunchyrollSettings = {};
        fernsehserienSettings = {};
        return {};
      }
    })();
  }
  return _generalSettingsPromise;
}

function loadCineinfoSettings() {
  return loadGeneralSettings().then(() => cineinfoSettings);
}

function _reEnrichPendingCards() {
  if (!cineinfoSettings || !cineinfoSettings.tmdb_api_key) return;
  if (cineinfoSettings.show_providers === '0' &&
      cineinfoSettings.show_fsk === '0' &&
      cineinfoSettings.show_hover_rating !== '1' &&
      cineinfoSettings.show_hover_genres !== '1' &&
      cineinfoSettings.show_hover_fsk !== '1') return;
  document.querySelectorAll('[data-tmdb-title]').forEach(card => {
    const info = card.querySelector('.browse-info');
    if (info && info.querySelector('.browse-tmdb-meta')) return;
    const title = card.dataset.tmdbTitle;
    if (title) _queueTmdbEnrich(card, title); // use batched path
  });
}

// ---------------------------------------------------------------------------
// Batched TMDB enrichment — collects visible-card titles for 80 ms then
// fires ONE /api/tmdb/batch POST instead of N individual /api/tmdb/info GETs.
// This keeps the TMDB rate-limiter happy and stops the UI flooding the server.
// ---------------------------------------------------------------------------

const _tmdbPending = new Map(); // title → [card, ...]
let _tmdbBatchTimer = null;

async function _flushTmdbBatch() {
  _tmdbBatchTimer = null;
  if (!_tmdbPending.size) return;
  if (!cineinfoSettings || !cineinfoSettings.tmdb_api_key) return;
  const batch = [..._tmdbPending.entries()];
  _tmdbPending.clear();
  const titles = batch.map(([t]) => t);
  try {
    const resp = await fetch("/api/tmdb/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ titles }),
      // Deprioritize vs. poster images competing for the same connection pool
      // (Chrome/Edge; harmlessly ignored elsewhere) — CineInfo/pill data is
      // secondary to actually seeing the card art.
      priority: "low",
    });
    if (!resp.ok) return;
    const results = await resp.json();
    batch.forEach(([title, cards]) => {
      const tmdb = results[title];
      if (tmdb) cards.forEach(card => _applyTmdbToCard(card, tmdb));
    });
  } catch (e) { /* best-effort */ }
}

function _queueTmdbEnrich(card, title) {
  if (!_tmdbPending.has(title)) _tmdbPending.set(title, []);
  _tmdbPending.get(title).push(card);
  clearTimeout(_tmdbBatchTimer);
  _tmdbBatchTimer = setTimeout(_flushTmdbBatch, 80);
}

// IntersectionObserver with tighter margin — only cards near the viewport
// trigger, avoiding eager loading of the entire page at once.
const _tmdbObserver = ('IntersectionObserver' in window)
  ? new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const card = entry.target;
      const title = card.dataset.tmdbTitle;
      if (title) {
        _tmdbObserver.unobserve(card);
        delete card.dataset.tmdbTitle;
        _queueTmdbEnrich(card, title);
      }
    });
  }, { rootMargin: '50px' })
  : null;

function enrichCardWithTmdb(card, title) {
  if (!cineinfoSettings || !cineinfoSettings.tmdb_api_key) {
    // No TMDB: the TMDB pipeline never runs, but the Crunchyroll pill doesn't
    // need TMDB — trigger it on its own (lazy) path.
    _crEnrichCard(card, title);
    return;
  }
  if (_tmdbObserver) {
    card.dataset.tmdbTitle = title;
    _tmdbObserver.observe(card);
  } else {
    _queueTmdbEnrich(card, title);
  }
}

// ── Crunchyroll/Fernsehserien card enrichment (works without TMDB) ──
// When TMDB is configured, _applyTmdbToCard already runs the full chain via
// _cardProviderChain, so this path is only used when TMDB is off entirely —
// avoiding duplicate availability calls. Still follows the same
// Crunchyroll → Fernsehserien.de fallback order (TMDB is simply skipped here).
async function _crCheckCard(card, title) {
  const info = card.querySelector('.browse-info');
  if (!info) return;
  let meta = info.querySelector('.browse-tmdb-meta');
  if (!meta) {
    meta = document.createElement('div');
    meta.className = 'browse-tmdb-meta';
    info.appendChild(meta);
  }
  const crAdded = await _crProviderPill(title, meta, { small: true });
  if (!crAdded) await _enqueueFsLookup(title, meta, { small: true });
}

const _crObserver = ('IntersectionObserver' in window)
  ? new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const card = entry.target;
      const title = card.dataset.crTitle;
      if (title) {
        _crObserver.unobserve(card);
        delete card.dataset.crTitle;
        _crCheckCard(card, title);
      }
    });
  }, { rootMargin: '50px' })
  : null;

function _crEnrichCard(card, title) {
  if (!crunchyrollSettings || crunchyrollSettings.enabled !== '1') return;
  if (crunchyrollSettings.show_providers === '0') return;
  if (!title) return;
  if (_crObserver) {
    card.dataset.crTitle = title;
    _crObserver.observe(card);
  } else {
    _crCheckCard(card, title);
  }
}

// Re-scan browse cards once settings have loaded (TMDB-off case only — with
// TMDB on, _reEnrichPendingCards drives the pill via the TMDB pipeline).
function _reEnrichCrunchyrollCards() {
  if (!crunchyrollSettings || crunchyrollSettings.enabled !== '1') return;
  if (crunchyrollSettings.show_providers === '0') return;
  if (cineinfoSettings && cineinfoSettings.tmdb_api_key) return;
  document.querySelectorAll('.browse-card').forEach(card => {
    const info = card.querySelector('.browse-info');
    if (info && info.querySelector('.browse-tmdb-meta')) return;
    const title = card.dataset.title || "";
    if (title) _crEnrichCard(card, title);
  });
}

async function loadAutoSyncJobs() {
  try {
    const resp = await fetch("/api/autosync");
    const data = await resp.json();
    autoSyncUrlMap = {};
    (data.jobs || []).forEach(j => {
      const norm = (j.series_url || "").replace(/\/+$/, "").toLowerCase();
      autoSyncUrlMap[norm] = j;
    });
  } catch (e) { /* best-effort */ }
}


// Custom paths select
const customPathSelect = document.getElementById("customPathSelect");

async function loadCustomPaths() {
  if (!customPathSelect) return;
  try {
    const resp = await fetch("/api/custom-paths");
    const data = await resp.json();
    const paths = data.paths || [];
    _customPathsCache = paths;
    // Remove old custom options (keep "Default")
    while (customPathSelect.options.length > 1) customPathSelect.remove(1);
    if (paths.length) {
      paths.forEach(function (p) {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.name;
        customPathSelect.appendChild(opt);
      });
      customPathSelect.style.display = "";
    } else {
      customPathSelect.style.display = "none";
    }
  } catch (e) {
    /* best-effort */
  }
}

async function loadDownloadedFolders() {
  try {
    const resp = await fetch("/api/downloaded-folders");
    const data = await resp.json();

    if (data.source === "mediascan") {
      // MediaScan mode: ignore folder list, use TMDB/IMDB IDs instead
      downloadedFolders = [];
      mediascanActive = false; // will be set true after library fetch below
    } else {
      downloadedFolders = data.folders || [];
      mediascanActive = false;
    }
  } catch (e) {
    /* best-effort */
  }
  // Always try to load mediascan library (returns empty if disabled)
  try {
    const ms = await fetch("/api/mediascan/library");
    const md = await ms.json();
    if (md.enabled) {
      mediascanTmdbIds = new Set((md.tmdb_ids || []).map(id => String(id)));
      mediascanImdbIds = new Set((md.imdb_ids || []).map(id => String(id)));
      mediascanTitles = new Set((md.titles || []));
      mediascanActive = true;
      // Re-evaluate any already-rendered badges that have a tmdb data attribute
      _refreshMediascanBadges();
    } else {
      mediascanTmdbIds = new Set();
      mediascanImdbIds = new Set();
      mediascanTitles = new Set();
      mediascanActive = false;
    }
  } catch (e) {
    /* best-effort */
  }
}

function _refreshMediascanBadges() {
  // Re-check all visible cards that already have a tmdb_id data attribute
  // (set by _applyTmdbToCard after async TMDB load)
  document.querySelectorAll(".browse-card[data-tmdb-id], .card[data-tmdb-id], .tmdb-card[data-tmdb-id]").forEach(card => {
    const existing = card.querySelector(".downloaded-badge");
    if (existing) existing.remove();
    const tmdbId = card.dataset.tmdbId || "";
    const title = card.dataset.title || "";
    if (_isDownloadedByTmdb(tmdbId) || _isDownloadedByTitle(title)) {
      _attachDownloadedBadge(card);
    } else if (!mediascanActive && title) {
      if (isDownloaded(title)) _attachDownloadedBadge(card);
    }
  });
}

function renderSkeletons(grid, count = 10) {
  if (!document.body.classList.contains("skeleton-loader")) return;
  grid.innerHTML = "";
  for (let i = 0; i < count; i++) {
    const card = document.createElement("div");
    card.className = "browse-card skeleton";
    card.innerHTML = `
      <div style="width:100%; aspect-ratio:2/3; background:rgba(255,255,255,0.03)"></div>
      <div class="browse-info">
        <div style="height:14px; width:80%; background:rgba(255,255,255,0.03); border-radius:4px; margin-bottom:6px"></div>
        <div style="height:12px; width:60%; background:rgba(255,255,255,0.03); border-radius:4px"></div>
      </div>
    `;
    grid.appendChild(card);
  }
}

let stoLoadedAt = 0;
async function loadStoBrowse() {
  if (stoLoadedAt && Date.now() - stoLoadedAt < 3600000) return;
  stoLoadedAt = Date.now();
  renderSkeletons(newSeriesGrid);
  renderSkeletons(popularSeriesGrid);
  try {
    const [newResp, popResp] = await Promise.all([
      fetch("/api/new-series"),
      fetch("/api/popular-series"),
    ]);
    await Promise.all([loadDownloadedFolders(), loadAutoSyncJobs(), loadCineinfoSettings(), loadGeneralSettings()]);
    const newData = await newResp.json();
    const popData = await popResp.json();

    if (newData.results) renderBrowseCards(newSeriesGrid, newData.results);
    else newSeriesGrid.innerHTML = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;

    if (popData.results) renderBrowseCards(popularSeriesGrid, popData.results);
    else popularSeriesGrid.innerHTML = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
  } catch (e) {
    stoLoadedAt = 0;
    newSeriesGrid.innerHTML = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
    popularSeriesGrid.innerHTML = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
  }
}

let fpLoadedAt = 0;
async function loadFilmPalastBrowse() {
  if (fpLoadedAt && Date.now() - fpLoadedAt < 3600000) return;
  fpLoadedAt = Date.now();
  renderSkeletons(newMoviesGrid);
  try {
    const resp = await fetch("/api/new-movies");
    await Promise.all([loadDownloadedFolders(), loadAutoSyncJobs(), loadCineinfoSettings(), loadGeneralSettings()]);
    const data = await resp.json();

    if (data.results) renderBrowseCards(newMoviesGrid, data.results);
    else newMoviesGrid.innerHTML = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
  } catch (e) {
    fpLoadedAt = 0;
    newMoviesGrid.innerHTML = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
  }
}

let megakinoLoadedAt = 0;
async function loadMegakinoBrowse() {
  if (megakinoLoadedAt && Date.now() - megakinoLoadedAt < 3600000) return;
  megakinoLoadedAt = Date.now();
  const grids = [megakinoNewMoviesGrid, megakinoPopularMoviesGrid, megakinoNewSeriesGrid, megakinoPopularSeriesGrid];
  grids.forEach(g => { if (g) renderSkeletons(g); });
  const errHtml = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
  try {
    const [nmResp, pmResp, nsResp, psResp] = await Promise.all([
      fetch("/api/megakino/new-movies"),
      fetch("/api/megakino/popular-movies"),
      fetch("/api/megakino/new-series"),
      fetch("/api/megakino/popular-series"),
    ]);
    await Promise.all([loadDownloadedFolders(), loadAutoSyncJobs(), loadCineinfoSettings(), loadGeneralSettings()]);
    const data = await Promise.all([nmResp.json(), pmResp.json(), nsResp.json(), psResp.json()]);
    const targets = [
      [megakinoNewMoviesGrid, data[0]],
      [megakinoPopularMoviesGrid, data[1]],
      [megakinoNewSeriesGrid, data[2]],
      [megakinoPopularSeriesGrid, data[3]],
    ];
    targets.forEach(([grid, d]) => {
      if (!grid) return;
      if (d && d.results) renderBrowseCards(grid, d.results);
      else grid.innerHTML = errHtml;
    });
  } catch (e) {
    megakinoLoadedAt = 0;
    grids.forEach(g => { if (g) g.innerHTML = errHtml; });
  }
}

// "Zensiert"/"Unzensiert" are content-type filters applied to individual
// hanime items (both the New/Trending lists and the general title-search
// results mix censored and uncensored entries) — not separate sections, so
// this filters an item array rather than hiding a whole grid/section. Shared
// by loadHanimeBrowse() (home page New/Trending) and renderResultsBoth()
// (title search) so both respect the same setting identically.
function _filterHanimeCensorship(results) {
  const hnVis = (generalSettings && generalSettings.sources && generalSettings.sources.sections && generalSettings.sources.sections.hanime) || {};
  const showCensored = hnVis.censored !== "0";
  const showUncensored = hnVis.uncensored !== "0";
  return (results || []).filter((item) => {
    if (item.censored === "Censored" && !showCensored) return false;
    if (item.censored === "Uncensored" && !showUncensored) return false;
    return true; // items without censorship info are always kept
  });
}

let hanimeLoadedAt = 0;
async function loadHanimeBrowse() {
  if (hanimeLoadedAt && Date.now() - hanimeLoadedAt < 3600000) return;
  hanimeLoadedAt = Date.now();
  if (hanimeNewGrid) renderSkeletons(hanimeNewGrid);
  if (hanimeTrendingGrid) renderSkeletons(hanimeTrendingGrid);
  try {
    const [newResp, trendResp] = await Promise.all([
      fetch("/api/hanime/new"),
      fetch("/api/hanime/trending"),
    ]);
    await Promise.all([loadDownloadedFolders(), loadAutoSyncJobs(), loadCineinfoSettings(), loadGeneralSettings()]);
    const errHtml = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
    const newData = await newResp.json();
    const trendData = await trendResp.json();
    const newResults = _filterHanimeCensorship(newData.results);
    const trendResults = _filterHanimeCensorship(trendData.results);
    // skipTmdb: hanime is adult content, not in TMDB's database — CineInfo
    // (TMDB + Crunchyroll/Fernsehserien pills) doesn't apply here.
    if (hanimeNewGrid) (newData.results ? renderBrowseCards(hanimeNewGrid, newResults, { skipTmdb: true }) : (hanimeNewGrid.innerHTML = errHtml));
    if (hanimeTrendingGrid) (trendData.results ? renderBrowseCards(hanimeTrendingGrid, trendResults, { skipTmdb: true }) : (hanimeTrendingGrid.innerHTML = errHtml));
  } catch (e) {
    hanimeLoadedAt = 0;
    const errHtml = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
    if (hanimeNewGrid) hanimeNewGrid.innerHTML = errHtml;
    if (hanimeTrendingGrid) hanimeTrendingGrid.innerHTML = errHtml;
  }
}

async function showBrowseSections() {
  browseDiv.style.display = "";
  let settings = {};
  try { settings = await loadGeneralSettings(); } catch (e) { settings = {}; }
  const sources = (settings && settings.sources) || {};
  _applySourceLayout(sources);

  const enabled = sources.enabled || {};
  if (enabled.aniworld !== "0") loadAniworldBrowse();
  if (enabled.sto !== "0") loadStoBrowse();
  if (enabled.filmpalast !== "0") loadFilmPalastBrowse();
  if (enabled.megakino !== "0") loadMegakinoBrowse();
  if (enabled.hanime === "1") loadHanimeBrowse();

  applyUptimeStatus();
}

// ── Source offline banner (only when UpTime monitoring is enabled) ──────────
let _uptimeBannerDismissed = false;
async function applyUptimeStatus() {
  if (!window.__UPTIME_ENABLED) return;
  const wrap = document.getElementById("sourceStatusBanner");
  if (!wrap || _uptimeBannerDismissed) return;
  let data;
  try {
    const resp = await fetch("/api/uptime/status");
    data = await resp.json();
  } catch (e) { return; }
  if (!data || !data.enabled || !Array.isArray(data.sources)) return;

  // Offline = tracked, enabled as a home source, and currently down.
  const offline = data.sources.filter(function (sc) {
    return sc.tracked && sc.enabled_source && sc.current_status === "down";
  });

  // Hide the offline provider blocks on the start page.
  offline.forEach(function (sc) {
    const block = browseDiv && browseDiv.querySelector('.browse-provider-block[data-provider="' + sc.id + '"]');
    if (block) block.style.display = "none";
  });

  if (!offline.length) { wrap.innerHTML = ""; return; }

  const names = offline.map(function (sc) { return sc.label; });
  const anyBlocked = offline.some(function (sc) { return sc.blocked; });
  const list = names.join(", ");
  const title = names.length === 1
    ? t("<b>" + escapeHtml(list) + "</b> ist gerade offline", "<b>" + escapeHtml(list) + "</b> is currently offline")
    : t("<b>" + escapeHtml(list) + "</b> sind gerade offline", "<b>" + escapeHtml(list) + "</b> are currently offline");
  const desc = anyBlocked
    ? t("Diese Quelle wurde ausgeblendet — eine Sperr-/ISP-Seite wurde erkannt. Prüfe deine DNS- und Netzwerkeinstellungen.",
        "This source was hidden — a block/ISP page was detected. Check your DNS and network settings.")
    : t("Ausgeblendet, weil nicht erreichbar. Prüfe deine DNS- und Netzwerkeinstellungen.",
        "Hidden because unreachable. Check your DNS and network settings.");

  wrap.innerHTML =
    '<div class="src-alert">' +
      '<span class="src-alert-ic">!</span>' +
      '<div class="src-alert-body">' +
        '<div class="src-alert-title">' + title + '</div>' +
        '<div class="src-alert-desc">' + desc + '</div>' +
      '</div>' +
      '<div class="src-alert-actions">' +
        '<a class="src-alert-btn primary" href="/settings#network">' + t("DNS-Test öffnen", "Open DNS test") + '</a>' +
        '<a class="src-alert-btn" href="/uptime">' + t("UpTime öffnen", "Open UpTime") + '</a>' +
      '</div>' +
      '<button class="src-alert-close" title="' + t("Ausblenden", "Dismiss") + '" onclick="dismissUptimeBanner()">×</button>' +
    '</div>';
}

function dismissUptimeBanner() {
  _uptimeBannerDismissed = true;
  const wrap = document.getElementById("sourceStatusBanner");
  if (wrap) wrap.innerHTML = "";
}

// Reorder provider blocks + their new/popular sections on the start page and
// hide disabled sources, based on the DB-backed source settings.
function _applySourceLayout(sources) {
  if (!browseDiv) return;
  const validProv = ["aniworld", "sto", "filmpalast", "megakino", "hanime"];
  let order = String((sources && sources.order) || "")
    .split(",").map(p => p.trim().toLowerCase()).filter(p => validProv.indexOf(p) !== -1);
  validProv.forEach(p => { if (order.indexOf(p) === -1) order.push(p); });
  const enabled = (sources && sources.enabled) || {};
  const sectionOrder = (sources && sources.section_order) || {};
  const sectionsVis = (sources && sources.sections) || {};

  order.forEach(prov => {
    const block = browseDiv.querySelector('.browse-provider-block[data-provider="' + prov + '"]');
    if (!block) return;
    browseDiv.appendChild(block); // reorder within #browse
    const so = String(sectionOrder[prov] || "")
      .split(",").map(x => x.trim().toLowerCase()).filter(Boolean);
    let anyVisible = false;
    const provVis = sectionsVis[prov] || {};
    so.forEach(secName => {
      const sec = block.querySelector('.browse-section[data-section="' + secName + '"]');
      if (!sec) return;
      block.appendChild(sec); // reorder new/popular within block
      const visible = provVis[secName] !== "0";
      sec.style.display = visible ? "" : "none";
      if (visible) anyVisible = true;
    });
    if (!so.length) anyVisible = true; // sources without configurable sections (e.g. FilmPalast)
    const disabled = enabled[prov] === "0";
    // Hide the whole block if the source is disabled or all its sections are hidden.
    block.style.display = (disabled || !anyVisible) ? "none" : "";
  });
}

function normalizeQuotes(s) {
  return s
    .replace(/[\u2018\u2019\u2032\u0060]/g, "'")
    .replace(/[\u201C\u201D\u201E]/g, '"');
}

function isDownloaded(title) {
  // Folder-based check (used when mediascan is inactive)
  if (!downloadedFolders.length || !title) return false;
  const clean = normalizeQuotes(
    unesc(title)
      .replace(/\s*\(.*$/, "")
      .replace(/[<>:"/\\|?*]/g, "") // : characters forbidden in folder names
      .trim()
      .toLowerCase(),
  );
  return downloadedFolders.some((f) =>
    normalizeQuotes(f.toLowerCase()).startsWith(clean),
  );
}

function _normalizeForMediascan(title) {
  if (!title) return "";
  return title
    .toLowerCase()
    .replace(/\s*\(\d{4}\)\s*$/, "")       // strip (2013)
    .replace(/\s*:?\s*season\s+\d+\s*$/i, "")
    .replace(/\s*:?\s*staffel\s+\d+\s*$/i, "")
    .replace(/\s*:?\s*part\s+\d+\s*$/i, "")
    .replace(/[^\w\s]/g, "")                  // strip punctuation
    .replace(/\s+/g, " ")
    .trim();
}

function _isDownloadedByTmdb(tmdbId) {
  if (!mediascanActive || !tmdbId) return false;
  return mediascanTmdbIds.has(String(tmdbId));
}

function _isDownloadedByTitle(title) {
  if (!mediascanActive || !title || !mediascanTitles.size) return false;
  const norm = _normalizeForMediascan(title);
  // O(1) Set lookup — prefix loop removed: normalization already strips
  // Season/Part suffixes on both sides, so exact match is sufficient.
  return norm ? mediascanTitles.has(norm) : false;
}

// Shared vertical stacking for every top-right corner pill (Vorhanden, Sync,
// and anime_seasons' own "Neu" badge -- see anime_seasons_view.js). Each
// stackable pill carries the "card-top-badge" marker class; a badge being
// attached counts how many are already on the card and picks its own "top"
// offset accordingly, so any combination/order of these three pills stacks
// cleanly without any one of them needing to know about the others by name.
// 27px = ~20px badge height + ~7px gap, same spacing the old hardcoded
// "hasVorhanden ? 34 : 7" constant used.
function _nextTopBadgeOffset(card) {
  return 7 + card.querySelectorAll(".card-top-badge").length * 27;
}

function _attachDownloadedBadge(card) {
  const badge = document.createElement("div");
  badge.className = "downloaded-badge card-top-badge";
  badge.textContent = "✓ " + t("Vorhanden", "Downloaded");
  badge.style.cssText = [
    "position:absolute", "top:" + _nextTopBadgeOffset(card) + "px", "right:7px",
    "background:var(--success)", "color:#fff",
    "font-size:0.65rem", "font-weight:700",
    "padding:2px 7px", "border-radius:99px",
    "line-height:1.5", "z-index:2", "pointer-events:none"
  ].join(";");
  card.style.position = "relative";
  card.appendChild(badge);
}

function addDownloadedBadge(card, title) {
  if (mediascanActive) {
    // Store title so _applyTmdbToCard can re-check via TMDB ID later.
    card.dataset.title = title || "";
    // Title-based check fires immediately — no TMDB load needed.
    if (_isDownloadedByTitle(title)) {
      _attachDownloadedBadge(card);
    }
    return;
  }
  if (isDownloaded(title)) _attachDownloadedBadge(card);
}

function addDownloadedBadgeForTmdb(card, title, tmdbId) {
  if (mediascanActive) {
    card.dataset.title = title || "";
    card.dataset.tmdbId = String(tmdbId || "");
    if (_isDownloadedByTmdb(tmdbId) || _isDownloadedByTitle(title)) {
      _attachDownloadedBadge(card);
    }
    return;
  }
  if (isDownloaded(title)) _attachDownloadedBadge(card);
}

// Same as addDownloadedBadge, but tries several title candidates in
// priority order instead of just one -- needed when the "canonical" title
// for a card (e.g. MyAnimeList's romaji/English title on the Anime Seasons
// page) isn't what downloaded folders/library entries are actually named
// after (a localized/German title, matching what AniWorld/S.to display).
// Only the FIRST candidate is stored as card.dataset.title for
// _applyTmdbToCard's later TMDB-id re-check, since that's the one most
// likely to match a TMDB-resolved display title too.
function addDownloadedBadgeMulti(card, titles) {
  const candidates = (titles || []).filter(Boolean);
  if (!candidates.length) return;
  if (mediascanActive) {
    card.dataset.title = candidates[0];
    if (candidates.some((title) => _isDownloadedByTitle(title))) {
      _attachDownloadedBadge(card);
    }
    return;
  }
  if (candidates.some((title) => isDownloaded(title))) _attachDownloadedBadge(card);
}

function _createSyncBadge(card) {
  const badge = document.createElement("div");
  badge.className = "sync-badge card-top-badge";
  badge.textContent = "⟳ Sync";
  badge.style.cssText = [
    "position:absolute", "top:" + _nextTopBadgeOffset(card) + "px", "right:7px",
    "background:var(--info)", "color:#fff",
    "font-size:0.6rem", "font-weight:700",
    "padding:2px 7px", "border-radius:99px",
    "line-height:1.6", "letter-spacing:.03em",
    "z-index:2", "pointer-events:none",
    "box-shadow:0 1px 6px rgba(59,130,246,.4)"
  ].join(";");
  card.style.position = "relative";
  card.appendChild(badge);
}

function addSyncBadge(card, url) {
  if (!url) return;
  const normUrl = url.replace(/\/+$/, "").toLowerCase();
  if (!autoSyncUrlMap[normUrl]) return;
  _createSyncBadge(card);
}

function addSyncBadgeForTmdb(card, title) {
  if (!title) return;
  const normTitle = _normalizeForMediascan(title);
  if (!normTitle) return;
  const hasMatchingJob = Object.values(autoSyncUrlMap).some(j => {
    const jobTitle = j.title || "";
    return _normalizeForMediascan(jobTitle) === normTitle;
  });
  if (!hasMatchingJob) return;
  _createSyncBadge(card);
}

// Same as addSyncBadgeForTmdb, but against several title candidates -- see
// addDownloadedBadgeMulti's comment for why (AutoSync jobs are also keyed by
// whatever title the job was created with, typically the localized/German
// one, not MyAnimeList's romaji/English title).
function addSyncBadgeForTmdbMulti(card, titles) {
  const candidates = (titles || []).map(_normalizeForMediascan).filter(Boolean);
  if (!candidates.length) return;
  const hasMatchingJob = Object.values(autoSyncUrlMap).some((j) => {
    const jobTitle = _normalizeForMediascan(j.title || "");
    return candidates.includes(jobTitle);
  });
  if (!hasMatchingJob) return;
  _createSyncBadge(card);
}

function refreshSyncBadges() {
  document.querySelectorAll(".browse-card, .card").forEach(card => {
    const img = card.querySelector("img[data-url]");
    if (!img) return;
    const url = img.getAttribute("data-url");
    const existing = card.querySelector(".sync-badge");
    if (existing) existing.remove();
    addSyncBadge(card, url);
  });
  document.querySelectorAll(".tmdb-card").forEach(card => {
    const title = card.dataset.title || "";
    const existing = card.querySelector(".sync-badge");
    if (existing) existing.remove();
    addSyncBadgeForTmdb(card, title);
  });
}

// Apply already-fetched TMDB data to a browse card synchronously (no network)
function _applyTmdbToCard(card, d) {
  _cardProviderChain(card, d);           // TMDB → Crunchyroll → Fernsehserien.de fallback pill
  if (!d || !d.found) return;
  const info = card.querySelector(".browse-info");
  if (!info) return;

  // Store TMDB ID on the card element so MediaScan badge matching can use it
  if (d.tmdb_id) {
    card.dataset.tmdbId = String(d.tmdb_id);
    // MediaScan mode: evaluate badge now that we have the TMDB ID
    if (mediascanActive) {
      const existing = card.querySelector(".downloaded-badge");
      if (!existing) {
        const cardTitle = card.dataset.title || "";
        if (_isDownloadedByTmdb(d.tmdb_id) || _isDownloadedByTitle(cardTitle)) {
          _attachDownloadedBadge(card);
        }
      }
    }
  }

  // Update genres if available
  if (d.genres && d.genres.length) {
    const genreEl = info.querySelector(".browse-genre");
    if (genreEl) {
      genreEl.textContent = d.genres.join(", ");
    }
  }

  if (!cineinfoSettings || !cineinfoSettings.tmdb_api_key) return;
  if (cineinfoSettings.show_providers === '0' &&
      cineinfoSettings.show_fsk === '0' &&
      cineinfoSettings.show_hover_rating !== '1' &&
      cineinfoSettings.show_hover_genres !== '1' &&
      cineinfoSettings.show_hover_fsk !== '1') return;

  let meta = info.querySelector(".browse-tmdb-meta");
  if (!meta) {
    meta = document.createElement("div");
    meta.className = "browse-tmdb-meta";
    info.appendChild(meta);
  }
  // Only clear the children — layout (flex/wrap/gap/margin) lives in the
  // .browse-tmdb-meta CSS rule now, not as an inline style set from JS, so
  // there's nothing stray left behind on an empty container to reset.
  meta.innerHTML = '';
  if (cineinfoSettings.show_providers !== '0' && d.providers && d.providers.length) {
    // Same small-pill styling as the Crunchyroll/Fernsehserien fallback pills
    // (see _makeProviderPill) so all three sources render identically.
    const pill = _makeProviderPill(d.providers[0], { small: true, title: d.providers.join(', ') });
    meta.appendChild(pill);
  }

  // Populate Browse Info Card
  let tmdb_voting = d.vote_average;
  let tmdb_genres = d.genres;
  let tmdb_fsk = d.fsk;
  renderBrowseHoverCards(card, tmdb_voting, tmdb_genres, tmdb_fsk);
}

// Single-card TMDB fetch — kept for the series modal and other one-off lookups.
// Browse cards use the batched _queueTmdbEnrich() path instead.
async function _doEnrichCard(card, title) {
  if (!cineinfoSettings || !cineinfoSettings.tmdb_api_key) return;
  if (cineinfoSettings.show_providers === '0' &&
      cineinfoSettings.show_fsk === '0' &&
      cineinfoSettings.show_hover_rating !== '1' &&
      cineinfoSettings.show_hover_genres !== '1' &&
      cineinfoSettings.show_hover_fsk !== '1') return;
  try {
    const resp = await fetch("/api/tmdb/info?title=" + encodeURIComponent(title).replace(/'/g, "%27"));
    _applyTmdbToCard(card, await resp.json());
  } catch (e) { /* best-effort */ }
}

function toggleSite() { /* no-op: both sites always shown */ }

function rebuildLanguageSelect(foundLangs = null) {
  const url = currentSeriesUrl || "";
  const isFilmPalast = url.includes("filmpalast.to");
  languageSelect.innerHTML = "";

  if (isFilmPalast) {
    // FilmPalast movies are always German-dubbed
    const opt = document.createElement("option");
    opt.value = "German Dub";
    opt.textContent = "German Dub";
    languageSelect.appendChild(opt);
    return;
  }

  if (url.includes("hanime.tv")) {
    // hanime: single Japanese audio track with burned-in subtitles.
    const opt = document.createElement("option");
    opt.value = "Japanese Dub";
    opt.textContent = t("Japanisch (Sub)", "Japanese (Sub)");
    languageSelect.appendChild(opt);
    return;
  }

  const isSto = url.includes("s.to") || url.includes("serienstream.to");
  const langs = isSto ? window.STO_LANGS || {} : window.ANIWORLD_LANGS || {};

  if (langSeparationEnabled) {
    const opt = document.createElement("option");
    opt.value = "All Languages";
    opt.textContent = "Alle Sprachen";
    languageSelect.appendChild(opt);
  }

  for (const [key, label] of Object.entries(langs)) {
    if (foundLangs && !foundLangs.has(label)) {
      continue;
    }
    const opt = document.createElement("option");
    opt.value = label;
    opt.textContent = label;
    languageSelect.appendChild(opt);
  }
}


if (searchInput) {
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
  });
  searchInput.addEventListener("input", () => {
    if (!searchInput.value.trim()) {
      if (resultsDiv) resultsDiv.innerHTML = "";
      showBrowseSections();
    }
  });
}
if (languageSelect) {
  languageSelect.addEventListener("change", updateProviderDropdown);
}

function _hanimeCensLabel(c) {
  const v = String(c || "").toLowerCase();
  if (v === "uncensored") return t("Unzensiert", "Uncensored");
  if (v === "censored") return t("Zensiert", "Censored");
  return c || "";
}

function renderBrowseCards(grid, items, opts) {
  opts = opts || {};
  grid.innerHTML = "";
  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "browse-card";
    card.dataset.url = item.url;
    card.onclick = () => openSeries(item.url);
    card.innerHTML =
      (item.censored ? `<div class="hanime-pill hanime-pill-${esc(String(item.censored).toLowerCase())}">${esc(_hanimeCensLabel(item.censored))}</div>` : ``) +
      `<img src="${esc(proxyImg(item.poster_url))}" alt="" onload="this.parentElement.classList.add('loaded')" onerror="this.parentElement.classList.add('loaded'); this.style.display='none'">` +
      `<div class="browse-info">` +
      `<div class="browse-title">${esc(item.title)}</div>` +
      `<div class="browse-genre">${esc(item.genre)}</div>` +
      `</div>`;
    addDownloadedBadge(card, item.title);
    addSyncBadge(card, item.url);
    grid.appendChild(card);
    // CineInfo (TMDB + Crunchyroll/Fernsehserien fallback pills) doesn't apply
    // here — hanime is adult content that isn't in TMDB's database, so this
    // would just be a wasted lookup (or, worse, a wrong match) on every card.
    // Genres/FSK hover info still applies — same overlay as everywhere else,
    // just fed from hanime's own data (tags + a hardcoded 18, since hanime is
    // inherently all-18+ content) instead of TMDB.
    if (opts.skipTmdb) {
      const hanimeTags = (item.tags && item.tags.length)
        ? item.tags
        : (item.genre ? item.genre.split(",").map(g => g.trim()).filter(Boolean) : []);
      renderBrowseHoverCards(card, null, hanimeTags, 18);
      return;
    }
    // If TMDB data came pre-loaded from the server cache → apply instantly (no fetch)
    if (item.tmdb) {
      if (item.tmdb.found) {
        // Defer one tick so cineinfoSettings is guaranteed loaded when aniworld tab is first
        setTimeout(() => _applyTmdbToCard(card, item.tmdb), 0);
      }
    } else {
      // Fall back to lazy loading via IntersectionObserver
      enrichCardWithTmdb(card, item.title);
    }
  });
}

function renderBrowseHoverCards(card, tmdb_voting, tmdb_genres, tmdb_fsk) {
  if (card.querySelector(".browse-hover-overlay")) return;

  const showRating = cineinfoSettings && cineinfoSettings.show_hover_rating === "1";
  const showGenres = cineinfoSettings && cineinfoSettings.show_hover_genres === "1";
  const showFSK = cineinfoSettings && cineinfoSettings.show_hover_fsk === "1";

  if (!showRating && !showGenres && !showFSK) return;

  let votingHtml = "";
  if (showRating && tmdb_voting) {
    const formattedVote = parseFloat(tmdb_voting).toFixed(1);
    votingHtml = `<span style="display: inline-flex; align-items: center; gap: 4px; font-size: 0.72rem; font-weight: 700; padding: 2px 8px 2px 6px; border-radius: 99px; border: 1px solid rgba(74, 222, 128, 0.4); background: rgba(0, 0, 0, 0.6); color: rgb(74, 222, 128); white-space: nowrap; cursor: default; letter-spacing: 0.01em; flex-shrink: 0; vertical-align: middle;"><svg width="10" height="10" viewBox="0 0 24 24" fill="#4ade80" style="flex-shrink:0"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"></path></svg>${formattedVote}</span>`;
  }

  let genresHtml = "";
  if (showGenres && tmdb_genres && tmdb_genres.length > 0) {
    const genreSpans = tmdb_genres.map(g => `<span>${esc(g)}</span>`).join("");
    genresHtml = `<div class="genres hover-genres">${genreSpans}</div>`;
  }

  let fskHtml = "";
  if (showFSK && tmdb_fsk) {
    const _fskPalette = {
      0: { bg: 'rgba(255,255,255,.07)', bc: 'rgba(255,255,255,.3)', c: '#d1d5db' },
      6: { bg: 'rgba(234,179,8,.12)', bc: 'rgba(234,179,8,.55)', c: '#fbbf24' },
      12: { bg: 'rgba(34,197,94,.12)', bc: 'rgba(34,197,94,.5)', c: '#4ade80' },
      16: { bg: 'rgba(59,130,246,.12)', bc: 'rgba(59,130,246,.5)', c: '#60a5fa' },
      18: { bg: 'rgba(239,68,68,.12)', bc: 'rgba(239,68,68,.5)', c: '#f87171' },
    };
    const fp = _fskPalette[tmdb_fsk] || { bg: 'rgba(148,163,184,.1)', bc: 'rgba(148,163,184,.35)', c: '#94a3b8' };
    fskHtml = `<span style="display: inline-flex; align-items: center; gap: 4px; font-size: 0.72rem; font-weight: 700; padding: 2px 8px 2px 6px; border-radius: 99px; border: 1px solid ${fp.bc}; background: ${fp.bg}; color: ${fp.c}; white-space: nowrap; cursor: default; letter-spacing: 0.01em; flex-shrink: 0; vertical-align: middle;">FSK ${tmdb_fsk}</span>`;
  }

  if (!votingHtml && !genresHtml && !fskHtml) return;

  const overlay = document.createElement("div");
  overlay.className = "browse-hover-overlay";
  overlay.innerHTML = `
    <div class="browse-hover-content">
      ${fskHtml}
      ${votingHtml}
      ${genresHtml}
    </div>
  `;
  card.appendChild(overlay);
}

let aniLoadedAt = 0;
async function loadAniworldBrowse() {
  if (aniLoadedAt && Date.now() - aniLoadedAt < 3600000) return;
  aniLoadedAt = Date.now();
  renderSkeletons(newAnimesGrid);
  renderSkeletons(popularAnimesGrid);
  try {
    const [newResp, popResp] = await Promise.all([
      fetch("/api/new-animes"),
      fetch("/api/popular-animes"),
    ]);
    await Promise.all([loadDownloadedFolders(), loadAutoSyncJobs(), loadCineinfoSettings(), loadGeneralSettings()]);
    const newData = await newResp.json();
    const popData = await popResp.json();

    if (newData.results) renderBrowseCards(newAnimesGrid, newData.results);
    else newAnimesGrid.innerHTML = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;

    if (popData.results) renderBrowseCards(popularAnimesGrid, popData.results);
    else popularAnimesGrid.innerHTML = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
  } catch (e) {
    aniLoadedAt = 0;
    newAnimesGrid.innerHTML = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
    popularAnimesGrid.innerHTML = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
  }
}
if (browseDiv) {
  showBrowseSections();
}

function initBrowseScrollButtons() {
  document.querySelectorAll(".browse-section").forEach(function (section) {
    const grid = section.querySelector(".browse-grid");
    const heading = section.querySelector(".browse-heading");
    if (!grid || !heading) return;

    const row = document.createElement("div");
    row.className = "browse-heading-row";
    heading.parentNode.insertBefore(row, heading);
    row.appendChild(heading);

    const btns = document.createElement("div");
    btns.className = "browse-scroll-btns";
    btns.innerHTML =
      '<button class="browse-scroll-btn" onclick="scrollBrowseGrid(this,-1)" aria-label="Zurück">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>' +
      '</button>' +
      '<button class="browse-scroll-btn" onclick="scrollBrowseGrid(this,1)" aria-label="Weiter">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>' +
      '</button>';
    row.appendChild(btns);
  });
}

function scrollBrowseGrid(btn, dir) {
  const grid = btn.closest(".browse-section").querySelector(".browse-grid");
  if (grid) grid.scrollBy({ left: dir * 460, behavior: "smooth" });
}

initBrowseScrollButtons();

async function doSearch() {
  const keyword = searchInput.value.trim().replace(/!+$/, "");
  if (!keyword) return;
  searchBtn.disabled = true;
  searchSpinner.style.display = "block";
  // Create a search grid with skeletons
  resultsDiv.innerHTML = "";
  const block = document.createElement("div");
  block.className = "browse-provider-block";
  const grid = document.createElement("div");
  grid.className = "results-poster-grid";
  block.appendChild(grid);
  resultsDiv.appendChild(block);
  renderSkeletons(grid, 12);

  browseDiv.style.display = "none";

  const searchSite = async (site) => {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000);
    try {
      const resp = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keyword, site }),
        signal: controller.signal
      });
      const data = await resp.json();
      return data.results || [];
    } catch (e) {
      return [];
    } finally {
      clearTimeout(timeoutId);
    }
  };

  try {
    let _srcSettings = {};
    try { _srcSettings = ((await loadGeneralSettings()) || {}).sources || {}; } catch (e) { _srcSettings = {}; }
    const _en = _srcSettings.enabled || {};
    const _hide = _srcSettings.hide_disabled_in_search === "1";
    const _active = (prov) => !(_hide && _en[prov] === "0");
    const _hanActive = _en.hanime === "1";
    const [aniResults, stoResults, fpResults, mkResults, hanResults] = await Promise.all([
      _active("aniworld") ? searchSite("aniworld").catch(() => []) : Promise.resolve([]),
      _active("sto") ? searchSite("sto").catch(() => []) : Promise.resolve([]),
      _active("filmpalast") ? searchSite("filmpalast").catch(() => []) : Promise.resolve([]),
      _active("megakino") ? searchSite("megakino").catch(() => []) : Promise.resolve([]),
      _hanActive ? searchSite("hanime").catch(() => []) : Promise.resolve([]),
    ]);
    renderResultsBoth(aniResults, stoResults, fpResults, mkResults, _filterHanimeCensorship(hanResults));
  } catch (e) {
    showToast(t("Suche fehlgeschlagen: ", "Search failed: " + e.message));
  } finally {
    searchBtn.disabled = false;
    searchSpinner.style.display = "none";
  }
}


function renderResults(results) {
  resultsDiv.innerHTML = "";
  if (!results.length) {
    resultsDiv.innerHTML =
      '<div style="width:100%;text-align:center;color:#888;padding:40px">Keine Ergebnisse gefunden.</div>';
    return;
  }
  results.forEach((r) => {
    const card = document.createElement("div");
    card.className = "card";
    card.onclick = () => openSeries(r.url);
    card.innerHTML = `<img src="" alt="" data-url="${esc(r.url)}"><div class="info"><div class="title">${esc(r.title)}</div></div>`;
    addDownloadedBadge(card, r.title);
    addSyncBadge(card, r.url);
    resultsDiv.appendChild(card);
    loadPoster(r.url, card.querySelector("img"));
  });
}

function renderResultsBoth(aniResults, stoResults, fpResults, mkResults, hanResults) {
  fpResults = fpResults || [];
  mkResults = mkResults || [];
  hanResults = hanResults || [];
  resultsDiv.innerHTML = "";
  if (!aniResults.length && !stoResults.length && !fpResults.length && !mkResults.length && !hanResults.length) {
    resultsDiv.innerHTML =
      '<div style="width:100%;text-align:center;color:#888;padding:40px">Keine Ergebnisse gefunden.</div>';
    return;
  }

  const sections = [
    { key: "aniworld", label: "AniWorld", cls: "browse-provider-aniworld", results: aniResults },
    { key: "sto", label: "SerienStream", cls: "browse-provider-sto", results: stoResults },
    { key: "filmpalast", label: "FilmPalast", cls: "browse-provider-filmpalast", results: fpResults },
    { key: "megakino", label: "MegaKino", cls: "browse-provider-megakino", results: mkResults },
    { key: "hanime", label: "hanime 18+", cls: "browse-provider-hanime", results: hanResults },
  ];
  try {
    const _ord = String(((generalSettings || {}).sources || {}).order || "")
      .split(",").map(x => x.trim().toLowerCase()).filter(Boolean);
    if (_ord.length) {
      sections.sort((a, b) => {
        const ia = _ord.indexOf(a.key), ib = _ord.indexOf(b.key);
        return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
      });
    }
  } catch (e) { }

  sections.forEach(function (sec) {
    if (!sec.results.length) return;

    const block = document.createElement("div");
    block.className = "browse-provider-block";

    const header = document.createElement("div");
    header.className = "browse-provider-header " + sec.cls;
    header.textContent = sec.label;
    block.appendChild(header);

    const grid = document.createElement("div");
    grid.className = "results-poster-grid";

    sec.results.forEach(function (r) {
      const card = document.createElement("div");
      card.className = "browse-card";
      card.dataset.url = r.url;
      card.onclick = () => openSeries(r.url);
      card.innerHTML =
        (r.censored ? '<div class="hanime-pill hanime-pill-' + esc(String(r.censored).toLowerCase()) + '">' + esc(_hanimeCensLabel(r.censored)) + '</div>' : '') +
        '<img src="" alt="" style="width:100%;aspect-ratio:2/3;object-fit:cover;background:var(--bg-elevated);display:block">' +
        '<div class="browse-info"><div class="browse-title">' + esc(r.title) + '</div><div class="browse-genre">' + esc(r.genre || '') + '</div></div>';
      addDownloadedBadge(card, r.title);
      addSyncBadge(card, r.url);
      grid.appendChild(card);
      loadPoster(r.url, card.querySelector("img"));
      // hanime is adult content and isn't in TMDB's database, so — same as
      // the dedicated hanime Browse tab (renderBrowseCards' skipTmdb option)
      // — skip the TMDB/Crunchyroll/Fernsehserien lookup chain entirely here
      // too; it would just be a wasted (or wrong-match) request per card.
      // Genre/FSK hover info still works, fed from hanime's own tags.
      if (sec.key === "hanime") {
        const hanimeTags = (r.tags && r.tags.length)
          ? r.tags
          : (r.genre ? r.genre.split(",").map(g => g.trim()).filter(Boolean) : []);
        renderBrowseHoverCards(card, null, hanimeTags, 18);
        return;
      }
      enrichCardWithTmdb(card, r.title);
    });

    block.appendChild(grid);
    resultsDiv.appendChild(block);
  });
}

/**
 * Marks cards in the overview/search results as "running" if they are currently being downloaded.
 * @param {string[]} runningUrls - List of series URLs currently active in the queue.
 */
window.updateRunningCards = function (runningUrls) {
  const cards = document.querySelectorAll(".browse-card, .result-card, .card");
  cards.forEach(card => {
    const url = card.dataset.url;
    if (url && runningUrls.includes(url)) {
      card.classList.add("running");
    } else {
      card.classList.remove("running");
    }
  });
};

async function loadPoster(url, imgEl) {
  try {
    const resp = await fetch("/api/series?url=" + encodeURIComponent(url));
    const data = await resp.json();
    if (data.poster_url) {
      imgEl.src = proxyImg(data.poster_url);
      imgEl.onload = () => {
        const card = imgEl.closest('.browse-card, .card');
        if (card) card.classList.add('loaded');
      };
      imgEl.onerror = () => {
        const card = imgEl.closest('.browse-card, .card');
        if (card) card.classList.add('loaded');
        imgEl.style.display = 'none';
      };
    } else {
      const card = imgEl.closest('.browse-card, .card');
      if (card) card.classList.add('loaded');
      imgEl.style.display = 'none';
    }
  } catch (e) {
    const card = imgEl.closest('.browse-card, .card');
    if (card) card.classList.add('loaded');
    imgEl.style.display = 'none';
  }
}

async function openSeries(url) {
  // Claim this load — see _seriesLoadSeq above for why this exists.
  const _mySeq = ++_seriesLoadSeq;
  if (!generalSettings || Object.keys(generalSettings || {}).length === 0) {
    await loadGeneralSettings();
  }
  if (!cineinfoSettings || Object.keys(cineinfoSettings || {}).length === 0) {
    await loadCineinfoSettings();
  }
  if (_mySeq !== _seriesLoadSeq) return; // superseded by a newer openSeries() call
  _currentSeriesUrl = url;
  overlay.style.display = "block";
  document.body.style.overflow = "hidden";
  const modal = document.getElementById("modal");
  const isSkeleton = document.body.classList.contains("skeleton-loader");

  document.getElementById("modalPoster").src = "";
  const _favBtn = document.getElementById("favouriteBtn");
  if (_favBtn) _favBtn.style.display = "none";

  if (isSkeleton) {
    modal.classList.add("skeleton");
    document.getElementById("modalPoster").style.opacity = "0";
    document.getElementById("modalTitle").innerHTML = '<div style="height:28px; width:60%; background:rgba(255,255,255,0.03); border-radius:6px; margin-bottom:8px"></div>';
    document.getElementById("modalGenres").innerHTML = '<div style="height:14px; width:40%; background:rgba(255,255,255,0.03); border-radius:4px"></div>';
    document.getElementById("modalYear").textContent = "";
    document.getElementById("modalDesc").innerHTML = '<div style="height:14px; width:100%; background:rgba(255,255,255,0.03); border-radius:4px; margin-bottom:6px"></div><div style="height:14px; width:80%; background:rgba(255,255,255,0.03); border-radius:4px"></div>';
  } else {
    modal.classList.remove("skeleton");
    document.getElementById("modalPoster").style.opacity = "";
    document.getElementById("modalTitle").textContent = "Lädt...";
    document.getElementById("modalGenres").textContent = "";
    document.getElementById("modalYear").textContent = "";
    document.getElementById("modalDesc").textContent = "";
  }
  const _tp = document.getElementById("tmdbProviders");
  if (_tp) { _tp.innerHTML = ""; _tp.style.display = "none"; }
  const _tfsk = document.getElementById("tmdbFsk");
  if (_tfsk) { _tfsk.textContent = ""; _tfsk.style.display = "none"; }
  const _mtS = document.getElementById("trailerSection");
  if (_mtS) {
    _mtS.style.display = "none";
    _mtS.querySelector('.season-header').classList.remove('expanded');
    _mtS.querySelector('.season-body').classList.remove('expanded');
    document.getElementById("modalTrailer").innerHTML = "";
  }
  const _mrS = document.getElementById("recommendationsSection");
  if (_mrS) {
    _mrS.style.display = "none";
    _mrS.querySelector('.season-header').classList.remove('expanded');
    _mrS.querySelector('.season-body').classList.remove('expanded');
    document.getElementById("modalRecommendations").innerHTML = "";
  }
  const modalMeta = document.querySelector('.modal-meta');
  if (modalMeta) modalMeta.classList.remove('loaded');

  seasonAccordion.innerHTML = "";
  const _lab = document.getElementById("langAvailBanner");
  if (_lab) {
    if (isSkeleton) {
      _lab.style.display = "block";
      _lab.innerHTML = "";
      _lab.className = "lang-avail-banner skeleton";
    } else {
      _lab.style.display = "none";
      _lab.innerHTML = "";
      _lab.className = "lang-avail-banner";
    }
  }
  statusBar.classList.remove("active");
  availableProviders = null;
  currentSeriesUrl = url;
  currentSeriesTitle = "";
  _updateUpscaleCheckbox(url);
  await checkLangSeparation();
  if (_mySeq !== _seriesLoadSeq) return; // superseded while awaiting settings
  rebuildLanguageSelect();
  resetProviderDropdown();
  loadCustomPaths();

  try {
    const [seriesResp, seasonsResp] = await Promise.all([
      fetch("/api/series?url=" + encodeURIComponent(url)),
      fetch("/api/seasons?url=" + encodeURIComponent(url)),
    ]);
    const seriesData = await seriesResp.json();
    const seasonsData = await seasonsResp.json();
    if (_mySeq !== _seriesLoadSeq) return; // a newer series was opened meanwhile — discard this response
    document.getElementById("modal").classList.remove("skeleton");
    document.getElementById("modalPoster").style.opacity = "";

    currentSeriesTitle = seriesData.title || t("Unbekannt", "Unknown");
    document.getElementById("modalTitle").textContent = currentSeriesTitle;
    if (seriesData.poster_url)
      document.getElementById("modalPoster").src = proxyImg(seriesData.poster_url);
    const _genresEl = document.getElementById("modalGenres");
    _genresEl.innerHTML = "";
    (seriesData.genres || []).forEach(g => {
      const sp = document.createElement("span");
      sp.textContent = g;
      _genresEl.appendChild(sp);
    });
    document.getElementById("modalYear").textContent =
      seriesData.release_year || "";
    document.getElementById("modalDesc").textContent =
      seriesData.description || "";

    if (modalMeta) modalMeta.classList.add('loaded');

    // CineInfo (TMDB + Crunchyroll/Fernsehserien pills) doesn't apply to
    // hanime — adult content isn't in TMDB's database, so this would just be
    // a wasted lookup (or a wrong match).
    if (!/hanime\.tv/i.test(url)) {
      enrichModalWithTmdb(currentSeriesTitle, seriesData.imdb_id || null, _mySeq);
    }

    currentSeasons = seasonsData.seasons || [];
    buildAccordion(currentSeasons, _mySeq);

    // For FilmPalast movies: populate provider dropdown from movie metadata
    const isMovie = !!seriesData.is_movie;
    const epHeading = document.getElementById("episodesHeading");
    if (epHeading) epHeading.style.display = isMovie ? "none" : "";
    if (isMovie && seriesData.available_providers && seriesData.available_providers.length) {
      availableProviders = { "German Dub": seriesData.available_providers };
      updateProviderDropdown();
    }

    // Hide auto-sync config for movies (not applicable)
    if (autoSyncConfigBtn) {
      autoSyncConfigBtn.style.display = isMovie ? "none" : "";
    }
    if (downloadAllLangsBtn) {
      // For movies, always hide; for series, defer to langSeparationEnabled setting
      if (isMovie) {
        downloadAllLangsBtn.style.display = "none";
      } else {
        downloadAllLangsBtn.style.display = langSeparationEnabled ? "" : "none";
      }
    }

    // Check if auto-sync exists for this series and reflect it on the button
    _currentSyncJob = null;
    _updateSyncConfigBtn();
    if (autoSyncConfigBtn && !isMovie) {
      try {
        const syncResp = await fetch(
          "/api/autosync/check?url=" + encodeURIComponent(url),
        );
        const syncData = await syncResp.json();
        if (_mySeq !== _seriesLoadSeq) return; // superseded while checking autosync
        if (syncData.exists && syncData.job) _currentSyncJob = syncData.job;
        _updateSyncConfigBtn();
      } catch (e) {
        /* ignore */
      }
    }

    // Check if this series is a favourite
    if (_mySeq !== _seriesLoadSeq) return; // superseded — don't touch the favourite button either
    _updateFavouriteBtn(url, seriesData.title, seriesData.poster_url || "");
  } catch (e) {
    document.getElementById("modal").classList.remove("skeleton");
    document.getElementById("modalPoster").style.opacity = "";
    showToast(t("Serie konnte nicht geladen werden: ", "Series could not be loaded: " + e.message));
  }
}

function buildAccordion(seasons, _seq) {
  seasonAccordion.innerHTML = "";
  episodeSpinner.style.display = "block";
  selectAllCb.checked = false;

  // Fetch all seasons' episodes in parallel
  const fetches = seasons.map((s, i) =>
    fetch("/api/episodes?url=" + encodeURIComponent(s.url))
      .then((r) => r.json())
      .then((data) => ({ index: i, episodes: data.episodes || [] }))
      .catch(() => ({ index: i, episodes: [] })),
  );

  Promise.all(fetches).then((results) => {
    // openSeries() may have moved on to a different series while these
    // per-season episode fetches were in flight — see _seriesLoadSeq.
    if (_seq !== undefined && _seq !== _seriesLoadSeq) return;
    episodeSpinner.style.display = "none";
    let firstProviderUrl = null;

    results.sort((a, b) => a.index - b.index);

    // Find all languages actually present in the episodes
    const foundLangs = new Set();
    results.forEach(({ episodes }) => {
      episodes.forEach((ep) => {
        if (ep.languages) {
          ep.languages.forEach((l) => foundLangs.add(l));
        }
      });
    });

    if (foundLangs.size > 0) {
      const prevVal = languageSelect.value;
      rebuildLanguageSelect(foundLangs);
      if (Array.from(languageSelect.options).some(o => o.value === prevVal)) {
        languageSelect.value = prevVal;
      }
    }

    // Per-episode language lookup (keyed by URL) so the download flow can detect
    // episodes that lack the chosen language and offer a per-episode fallback.
    window._epLangMap = {};
    results.forEach(({ index, episodes }) => {
      const season = seasons[index];
      episodes.forEach((ep) => {
        window._epLangMap[ep.url] = {
          languages: ep.languages || [],
          epNum: ep.episode_number,
          seasonNumber: season ? season.season_number : null,
          isMovie: !!(season && (season.is_single_movie || season.are_movies)),
          title: ep.title_en || ep.title_de || "",
        };
      });
    });

    results.forEach(({ index, episodes }) => {
      const season = seasons[index];
      const section = document.createElement("div");
      section.className = "season-section";
      section.dataset.seasonIndex = index;

      const label = season.is_single_movie
        ? t("Film", "Movie")
        : season.are_movies
          ? `${t("Filme", "Movies")} (${episodes.length} ${t("Episoden", "Episodes")})`
          : `${t("Staffel", "Season")} ${season.season_number} (${episodes.length} ${t("Episoden", "Episodes")})`;

      const isSingleMovie = !!season.is_single_movie;

      // Header — hidden for single movies (no season concept)
      const header = document.createElement("div");
      if (isSingleMovie) {
        header.className = "season-header season-header-movie expanded";
        header.style.display = "none";
      } else {
        const allDownloaded =
          episodes.length > 0 && episodes.every((ep) => ep.downloaded);
        const seasonDlIcon = allDownloaded
          ? t('<span class="season-downloaded" title="Alle Episoden heruntergeladen">&#10003;</span>', '<span class="season-downloaded" title="All episodes downloaded">&#10003;</span>')
          : "";
        header.className = "season-header";
        header.innerHTML =
          `<div class="season-label"><span class="season-arrow">&#9654;</span> ${esc(label)}${seasonDlIcon}</div>` +
          `<label class="season-all-label" onclick="event.stopPropagation()"><input type="checkbox" class="chb-main" onchange="toggleSeasonAll(this, ${index})"> Alle</label>`;
        header.addEventListener("click", () => toggleSeason(index));
      }

      // Body
      const body = document.createElement("div");
      body.className = "season-body" + (isSingleMovie ? " expanded" : "");
      body.id = "seasonBody-" + index;

      // Language flags
      const langFlagMap = {
        "German Dub": "/static/flags/german.svg",
        "English Dub": "/static/flags/english.svg",
        "German Sub": "/static/flags/japanese-germanSub.svg",
        "English Sub": "/static/flags/japanese-englishSub.svg",
        "English Dub (German Sub)": "/static/flags/english-germanSub.svg",
      };

      episodes.forEach((ep) => {
        const div = document.createElement("div");
        div.className = "episode-item";
        div.style.cursor = "pointer";
        div.addEventListener("click", (event) => {
          if (event.target.tagName.toLowerCase() !== "input") {
            const checkbox = div.querySelector('input[type="checkbox"]');
            if (checkbox) {
              checkbox.checked = !checkbox.checked;
            }
          }
        });
        const title = ep.title_en || ep.title_de || "";
        const dlIcon = ep.downloaded
          ? '<span class="ep-downloaded" title="Downloaded">&#10003;</span>'
          : "";

        let langsHtml = "";
        if (ep.languages && ep.languages.length) {
          const pills = ep.languages.map((l) => {
            const src = langFlagMap[l];
            if (!src) return "";
            return `<img class="ep-lang-flag" src="${src}" title="${esc(l)}" alt="${esc(l)}">`;
          }).join("");
          langsHtml = `<span class="ep-langs">${pills}</span>`;
        }

        const epNumHtml = isSingleMovie ? "" : `<span class="ep-num">E${ep.episode_number}</span>`;
        const cb = `<input type="checkbox" class="chb-main" value="${esc(ep.url)}" data-season="${index}"${isSingleMovie ? " checked" : ""}>`;
        const streamBtn = `<button type="button" class="ep-stream-btn" title="${esc(t('Stream starten','Start stream'))}" aria-label="${esc(t('Stream starten','Start stream'))}"><svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg></button>`;
        div.innerHTML = `${cb}${epNumHtml}${dlIcon}<span class="ep-title">${esc(title)}</span>${langsHtml}${streamBtn}`;
        const _sBtn = div.querySelector(".ep-stream-btn");
        if (_sBtn) {
          _sBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            streamEpisode(ep.url, title, ep.languages || []);
          });
        }
        body.appendChild(div);
      });

      if (!firstProviderUrl && episodes.length) {
        firstProviderUrl = episodes[0].url;
      }

      section.appendChild(header);
      section.appendChild(body);
      seasonAccordion.appendChild(section);
    });

    // Language availability banner
    renderLangAvailBanner(results);

    // Fetch providers from first episode (updates dynamically with checked availability)
    if (firstProviderUrl) {
      fetchProviders(firstProviderUrl);
    }
  });
}

function renderLangAvailBanner(results) {
  const banner = document.getElementById("langAvailBanner");
  if (!banner) return;
  banner.classList.remove("skeleton");
  // FilmPalast movies don't need a language availability banner
  if ((currentSeriesUrl || "").includes("filmpalast.to")) {
    banner.style.display = "none";
    return;
  }
  // hanime has exactly one language (Japanese Dub, burned-in subs — see
  // HANIME_LANGUAGE in models/hanime_tv/episode.py), so a Ger./Eng.
  // Dub/Sub availability breakdown is meaningless there.
  if ((currentSeriesUrl || "").includes("hanime.tv")) {
    banner.style.display = "none";
    return;
  }

  const isSto = (currentSeriesUrl || "").includes("s.to") || (currentSeriesUrl || "").includes("serienstream.to");
  const LANG_ORDER = ["German Dub", "English Sub", "German Sub", "English Dub"];
  if (isSto) {
    LANG_ORDER.push("English Dub (German Sub)");
  }
  const LANG_SHORT = {
    "German Dub": "Ger. Dub",
    "English Sub": "Eng. Sub",
    "German Sub": "Ger. Sub",
    "English Dub": "Eng. Dub",
    "English Dub (German Sub)": "Eng. Dub (Ger. Sub)",
  };

  // Count episodes per language and total
  const counts = {};
  let total = 0;
  results.forEach(({ episodes }) => {
    episodes.forEach((ep) => {
      total++;
      if (ep.languages) {
        ep.languages.forEach((l) => {
          counts[l] = (counts[l] || 0) + 1;
        });
      }
    });
  });

  if (total === 0) { banner.style.display = "none"; return; }

  const pills = LANG_ORDER.map((lang) => {
    const n = counts[lang] || 0;
    const pct = Math.round((n / total) * 100);
    const full = n === total;
    const none = n === 0;
    const cls = full ? "lang-avail-pill lang-avail-full"
      : none ? "lang-avail-pill lang-avail-none"
        : "lang-avail-pill lang-avail-partial";
    return `<span class="${cls}" title="${lang}">${LANG_SHORT[lang]}: ${n}&thinsp;/&thinsp;${total}</span>`;
  }).join("");

  banner.innerHTML = pills;
  banner.style.display = "flex";
}

function toggleSeason(index) {
  const section = seasonAccordion.querySelector(
    `[data-season-index="${index}"]`,
  );
  if (!section) return;
  const header = section.querySelector(".season-header");
  const body = section.querySelector(".season-body");
  header.classList.toggle("expanded");
  body.classList.toggle("expanded");
}

function toggleSeasonAll(checkbox, seasonIndex) {
  const body = document.getElementById("seasonBody-" + seasonIndex);
  if (!body) return;
  body
    .querySelectorAll("input[type=checkbox]")
    .forEach((cb) => (cb.checked = checkbox.checked));
  syncSelectAll();
}

function toggleSelectAll() {
  const checked = selectAllCb.checked;
  seasonAccordion
    .querySelectorAll("input[type=checkbox]")
    .forEach((cb) => (cb.checked = checked));
}

function syncSelectAll() {
  const all = seasonAccordion.querySelectorAll(
    ".episode-item input[type=checkbox]",
  );
  const checked = seasonAccordion.querySelectorAll(
    ".episode-item input[type=checkbox]:checked",
  );
  selectAllCb.checked = all.length > 0 && all.length === checked.length;
}

function getAllEpisodeUrls() {
  return Array.from(
    seasonAccordion.querySelectorAll(".episode-item input[type=checkbox]"),
  ).map((cb) => cb.value);
}

function getSelectedEpisodeUrls() {
  return Array.from(
    seasonAccordion.querySelectorAll(
      ".episode-item input[type=checkbox]:checked",
    ),
  ).map((cb) => cb.value);
}

async function fetchProviders(episodeUrl) {
  try {
    const resp = await fetch(
      "/api/providers?url=" + encodeURIComponent(episodeUrl),
    );
    const data = await resp.json();
    if (data.providers) {
      availableProviders = data.providers;
      updateProviderDropdown();
    }
  } catch (e) {
    // If provider fetch fails, keep the static list
  }
}

function resetProviderDropdown() {
  providerSelect.innerHTML = "";
  staticProviders.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = p;
    providerSelect.appendChild(opt);
  });
  selectDefaultProvider();
}

function updateProviderDropdown() {
  if (!availableProviders) return;

  const lang = languageSelect.value;
  const providers = availableProviders[lang];

  providerSelect.innerHTML = "";
  if (providers && providers.length) {
    providers.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      providerSelect.appendChild(opt);
    });
  } else {
    // The backend already checked (extractor support +, for movies, live
    // availability) and came back empty for this language — don't fall back
    // to the unfiltered static list, that would just offer sources we know
    // are dead. (A fetch that never happened / failed is handled above by
    // the "if (!availableProviders) return;" guard, which leaves whatever
    // was already rendered — usually the static list — untouched.)
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = t("Keine Quelle verfügbar", "No Source available");
    opt.disabled = true;
    providerSelect.appendChild(opt);
  }
  selectDefaultProvider();
}

function selectDefaultProvider() {
  for (const opt of providerSelect.options) {
    if (opt.value === "VOE") {
      providerSelect.value = "VOE";
      return;
    }
  }
}

// ── VeeV Availability Check ──────────────────────────────────────────────────

function showVeevCheck() {
  const overlay = document.getElementById("veevCheckOverlay");
  if (!overlay) return;
  // Move to <body> to escape any stacking contexts created by parent elements
  if (overlay.parentNode !== document.body) document.body.appendChild(overlay);
  overlay.style.cssText = "display:flex;position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:99999;align-items:center;justify-content:center;backdrop-filter:blur(4px)";
  const spinnerWrap = document.getElementById("veevCheckSpinnerWrap");
  if (spinnerWrap) spinnerWrap.style.display = "flex";
  const textEl = document.getElementById("veevCheckText");
  if (textEl) { textEl.style.display = ""; textEl.textContent = "Es wird überprüft ob der ausgewählte Inhalt auf Veev verfügbar ist"; }
  const errEl = document.getElementById("veevCheckError");
  if (errEl) { errEl.style.display = "none"; errEl.textContent = ""; }
  const closeBtn = document.getElementById("veevCheckCloseBtn");
  if (closeBtn) closeBtn.style.display = "none";
}

function closeVeevCheck() {
  const overlay = document.getElementById("veevCheckOverlay");
  if (!overlay) return;
  overlay.style.display = "none";
}

/**
 * Prüft ob eine Veev-Episode tatsächlich abrufbar ist.
 * Gibt true zurück wenn verfügbar, false wenn nicht (Fehler wird im Modal angezeigt).
 */
async function veevCheckAvailability(episodeUrl) {
  showVeevCheck();
  try {
    const resp = await fetch("/api/veev/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ episode_url: episodeUrl }),
    });
    const data = await resp.json();
    if (data.available) {
      closeVeevCheck();
      return true;
    }
    // Show error state
    document.getElementById("veevCheckSpinnerWrap").style.display = "none";
    document.getElementById("veevCheckText").style.display = "none";
    const errEl = document.getElementById("veevCheckError");
    errEl.textContent = data.error || t("Dieser Film ist auf Veev momentan nicht verfügbar.", "This movie is currently not available on Veev.");
    errEl.style.display = "block";
    document.getElementById("veevCheckCloseBtn").style.display = "inline-block";
    return false;
  } catch (e) {
    document.getElementById("veevCheckSpinnerWrap").style.display = "none";
    document.getElementById("veevCheckText").style.display = "none";
    const errEl = document.getElementById("veevCheckError");
    errEl.textContent = t("Fehler bei der Verfügbarkeitsprüfung: ", "Error checking availability: " + e.message);
    errEl.style.display = "block";
    document.getElementById("veevCheckCloseBtn").style.display = "inline-block";
    return false;
  }
}

// Stream a single episode directly from its provider (no download).
async function streamEpisode(episodeUrl, title, langOptions) {
  if (typeof openStreamSource !== "function") {
    showToast(t("Player wird geladen…", "Player loading…"));
    return;
  }
  const language = languageSelect ? languageSelect.value : "German Dub";
  const provider = providerSelect ? providerSelect.value : "VOE";
  if (!provider) {
    showToast(t("Keine Quelle verfügbar", "No Source available"));
    return;
  }
  // Available languages: this episode's, else fall back to the page selector.
  let langs = (langOptions && langOptions.length) ? langOptions.slice() : [];
  if (!langs.length && languageSelect) {
    langs = Array.from(languageSelect.options).map((o) => o.value);
  }
  // Available providers from the page's provider selector.
  let providers = providerSelect ? Array.from(providerSelect.options).map((o) => o.value) : [];
  // Look up this user's saved position for the episode (keyed by URL).
  let startPos = 0;
  try {
    const r = await fetch("/api/progress/get?path=" + encodeURIComponent(episodeUrl));
    if (r.ok) {
      const p = await r.json();
      if (p && p.percent > 3 && !p.watched) startPos = p.position || 0;
    }
  } catch (e) { /* resume is best-effort */ }
  openStreamSource(episodeUrl, title, provider, language, startPos, langs, providers);
}

async function startDownload(all) {
  const episodes = all ? getAllEpisodeUrls() : getSelectedEpisodeUrls();
  if (!episodes.length) {
    showToast(all ? t("Keine Episoden verfügbar.", "No episodes available.") : t("Keine Episoden ausgewählt.", "No episodes selected."));
    return;
  }

  const language = languageSelect.value;
  const provider = providerSelect.value;
  if (!provider) {
    showToast(t("Keine Quelle verfügbar", "No Source available"));
    return;
  }

  // Detect selected episodes that do not offer the chosen language. This is a
  // manual-download safeguard only — it never runs for Auto-Sync.
  const map = window._epLangMap || {};
  const matched = [];
  const mismatched = [];
  episodes.forEach((url) => {
    const info = map[url];
    if (info && Array.isArray(info.languages) && info.languages.length && !info.languages.includes(language)) {
      mismatched.push(url);
    } else {
      matched.push(url);
    }
  });

  if (mismatched.length) {
    openLangMismatchModal(matched, mismatched, language, provider);
    return;
  }

  await _submitDownloadGroups([{ episodes, language, provider }]);
}

// Queue one or more {episodes, language, provider} groups in sequence.
async function _submitDownloadGroups(groups) {
  groups = (groups || []).filter((g) => g.episodes && g.episodes.length);
  if (!groups.length) {
    showToast(t("Keine Episoden ausgewählt.", "No episodes selected."));
    return;
  }

  // VeeV availability check (once) if any group uses a Veev provider.
  for (const g of groups) {
    if (g.provider && g.provider.toLowerCase().replace(/\s+(hd|hq)$/i, "") === "veev") {
      const ok = await veevCheckAvailability(g.episodes[0]);
      if (!ok) return; // modal stays open with the error
      break;
    }
  }

  downloadAllBtn.disabled = true;
  downloadSelectedBtn.disabled = true;
  const upscaleCheck = document.getElementById("upscaleCheck");
  const upscale = !!(upscaleCheck && upscaleCheck.closest("#upscaleCheckWrapper") && upscaleCheck.closest("#upscaleCheckWrapper").style.display !== "none" && upscaleCheck.checked);
  let ok = 0;
  let lastErr = "";
  try {
    for (const g of groups) {
      const dlBody = {
        episodes: g.episodes,
        language: g.language,
        provider: g.provider,
        title: currentSeriesTitle,
        series_url: currentSeriesUrl,
        upscale,
      };
      if (customPathSelect && customPathSelect.value) {
        dlBody.custom_path_id = parseInt(customPathSelect.value);
      }
      const resp = await fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(dlBody),
      });
      const data = await resp.json();
      if (data.error) lastErr = data.error;
      else ok++;
    }
    if (ok) {
      showToast(t("Zur Download-Warteschlange hinzugefügt", "Added to download queue"));
      if (typeof loadQueue === "function") loadQueue();
    } else if (lastErr) {
      showToast(lastErr);
    }
  } catch (e) {
    showToast(t("Download-Anfrage fehlgeschlagen: ", "Download request failed: ") + e.message);
  } finally {
    downloadAllBtn.disabled = false;
    downloadSelectedBtn.disabled = false;
  }
}

// ── Language mismatch modal (manual download) ────────────────────────────────

function openLangMismatchModal(matched, mismatched, language, provider) {
  const overlayEl = document.getElementById("langMismatchOverlay");
  const listEl = document.getElementById("langMismatchList");
  if (!overlayEl || !listEl) {
    // Fallback: just queue the episodes that do offer the chosen language.
    _submitDownloadGroups([{ episodes: matched, language, provider }]);
    return;
  }

  window._langMismatchCtx = { matched, mismatched, language, provider };

  const titleEl = document.getElementById("langMismatchTitle");
  if (titleEl) titleEl.textContent = t("Sprache nicht verfügbar", "Language not available");
  const introEl = document.getElementById("langMismatchIntro");
  if (introEl) {
    introEl.textContent = t(
      `Für die gewählte Sprache „${language}" sind ${mismatched.length} ausgewählte Episode(n) nicht verfügbar. Wähle pro Episode eine andere Sprache oder überspringe sie.`,
      `The selected language "${language}" is not available for ${mismatched.length} selected episode(s). Pick another language per episode or skip it.`
    );
  }
  const cancelBtn = document.getElementById("langMismatchCancel");
  if (cancelBtn) cancelBtn.textContent = t("Abbrechen", "Cancel");
  const confirmBtn = document.getElementById("langMismatchConfirm");
  if (confirmBtn) confirmBtn.textContent = t("Bestätigen", "Confirm");

  const map = window._epLangMap || {};
  const skipLabel = t("Nicht hinzufügen", "Do not add");
  let html = "";
  mismatched.forEach((url) => {
    const info = map[url] || {};
    const langs = info.languages || [];
    const label = info.isMovie
      ? (info.title || t("Film", "Movie"))
      : `${t("S", "S")}${info.seasonNumber != null ? info.seasonNumber : "?"} E${info.epNum != null ? info.epNum : "?"}${info.title ? " · " + info.title : ""}`;
    let opts = `<option value="__skip__">${esc(skipLabel)}</option>`;
    langs.forEach((l) => { opts += `<option value="${esc(l)}">${esc(l)}</option>`; });
    html += `<div class="lang-mismatch-row">
      <span class="lmm-ep" title="${esc(label)}">${esc(label)}</span>
      <select class="lmm-select" data-url="${esc(url)}">${opts}</select>
    </div>`;
  });
  listEl.innerHTML = html;
  overlayEl.style.display = "flex";
}

function closeLangMismatchModal() {
  const o = document.getElementById("langMismatchOverlay");
  if (o) o.style.display = "none";
}

async function confirmLangMismatch() {
  const ctx = window._langMismatchCtx || {};
  const groups = [];
  if (ctx.matched && ctx.matched.length) {
    groups.push({ episodes: ctx.matched, language: ctx.language, provider: ctx.provider });
  }

  // Group the mismatched episodes by the alternative language the user picked.
  const byLang = {};
  document.querySelectorAll("#langMismatchList .lmm-select").forEach((sel) => {
    const val = sel.value;
    if (val === "__skip__") return;
    (byLang[val] = byLang[val] || []).push(sel.dataset.url);
  });
  Object.entries(byLang).forEach(([lang, eps]) => {
    // Prefer a provider that actually serves this language; fall back to the
    // originally selected provider, then VOE.
    let prov = ctx.provider;
    if (availableProviders && availableProviders[lang] && availableProviders[lang].length) {
      prov = availableProviders[lang].includes("VOE") ? "VOE" : availableProviders[lang][0];
    }
    groups.push({ episodes: eps, language: lang, provider: prov });
  });

  closeLangMismatchModal();
  await _submitDownloadGroups(groups);
}

function closeModal() {
  overlay.style.display = "none";
  document.body.style.overflow = "";
  _currentSyncJob = null;
}
function closeModalOutside(e) {
  if (e.target === overlay) closeModal();
}

// Auto-Sync configuration (opens the shared filter dialog)
function _updateSyncConfigBtn() {
  if (!autoSyncConfigLabel) return;
  if (_currentSyncJob) {
    let txt = t("Auto-Sync bearbeiten", "Edit Auto-Sync");
    const sum =
      window.AutosyncFilter && _currentSyncJob.episode_filter
        ? window.AutosyncFilter.summarize(_currentSyncJob.episode_filter)
        : "";
    if (sum) txt += " · " + sum;
    autoSyncConfigLabel.textContent = txt;
    if (autoSyncConfigBtn) autoSyncConfigBtn.classList.add("btn-primary");
  } else {
    autoSyncConfigLabel.textContent = t("Auto-Sync einrichten", "Set up Auto-Sync");
    if (autoSyncConfigBtn) autoSyncConfigBtn.classList.remove("btn-primary");
  }
}

function openAutoSyncConfig() {
  if (!window.AutosyncFilter || !currentSeriesUrl) return;
  const _key = (currentSeriesUrl || "").replace(/\/+$/, "").toLowerCase();
  window.AutosyncFilter.openCreate({
    seriesUrl: currentSeriesUrl,
    title: currentSeriesTitle,
    customPaths: _customPathsCache,
    languages: languageSelect
      ? Array.from(languageSelect.options)
          .map((o) => o.value)
          .filter((v) => v && v !== "All Languages")
      : null,
    currentLanguage: languageSelect ? languageSelect.value : null,
    currentProvider: providerSelect ? providerSelect.value : null,
    langSepEnabled: langSeparationEnabled,
    existing: _currentSyncJob,
    onSaved: async (res) => {
      if (res && res.removed) {
        _currentSyncJob = null;
        if (typeof autoSyncUrlMap === "object") delete autoSyncUrlMap[_key];
      } else {
        try {
          const r = await fetch(
            "/api/autosync/check?url=" + encodeURIComponent(currentSeriesUrl),
          );
          const d = await r.json();
          _currentSyncJob = d.exists && d.job ? d.job : null;
        } catch (e) {
          /* ignore */
        }
        if (_currentSyncJob && typeof autoSyncUrlMap === "object")
          autoSyncUrlMap[_key] = { series_url: currentSeriesUrl };
      }
      _updateSyncConfigBtn();
      if (typeof refreshSyncBadges === "function") refreshSyncBadges();
    },
  });
}

// Provider → branded color map
const _providerColors = {
  'Netflix': '#E50914',
  'Netflix basic with Ads': '#E50914',
  'Netflix Standard with Ads': '#E50914',
  'Amazon Prime Video': '#00A8E0',
  'Amazon Channel': '#00A8E0',
  'Amazon Prime': '#00A8E0',
  'Disney+': '#0063E5',
  'Disney Plus': '#0063E5',
  'Apple TV+': '#555',
  'Apple TV Plus': '#555',
  'Sky': '#003C8F',
  'WOW': '#00B4D8',
  'RTL+': '#FF6900',
  'Joyn': '#00C896',
  'Paramount+': '#0064FF',
  'Max': '#5822B7',
  'HBO Max': '#5822B7',
  'Crunchyroll': '#F47521',
  'MUBI': '#C2410C',
  'Hulu': '#1CE783',
  'MagentaTV': '#E20074',
  'ARD Mediathek': '#003D5B',
  'ZDFmediathek': '#008CD2',
};

function getProviderColor(name) {
  // 1. Exakter Match (für die Performance und genaue Treffer)
  if (_providerColors[name]) {
    return _providerColors[name];
  }

  // 2. Teilstring-Match (sucht nach "Amazon Channel" im Namen)
  const exactKey = Object.keys(_providerColors).find(key => name.includes(key));

  // Wenn was gefunden wurde, nimm die Farbe, ansonsten Fallback (z.B. grau)
  return exactKey ? _providerColors[exactKey] : '#888';
}

// Builds a provider pill. Every caller — TMDB's own card/modal badge, the
// Crunchyroll pill and the Fernsehserien.de pill — goes through this single
// function so all three always look and behave identically, regardless of
// which provider source actually supplied the name. `opts.small` is the card
// variant (compact size, truncates with an ellipsis instead of wrapping);
// `opts.title` sets a hover tooltip (e.g. the full, un-truncated provider list).
function _makeProviderPill(name, opts) {
  opts = opts || {};
  const pill = document.createElement('span');
  pill.className = 'tmdb-provider-pill';
  const color = getProviderColor(name);
  // Always apply full pill style inline – not reliant on cached CSS
  pill.style.cssText = [
    'display:inline-flex',
    'align-items:center',
    'gap:6px',
    'font-size:' + (opts.small ? '0.7rem' : '0.75rem'),
    'font-weight:600',
    'padding:' + (opts.small ? '2px 8px 2px 6px' : '4px 12px 4px 8px'),
    'border-radius:99px',
    'border:1.5px solid ' + (color ? color + '60' : 'rgba(148,163,184,.35)'),
    'background:var(--bg-elevated,#1a1a28)',
    'color:' + (color || 'var(--text-secondary,#9191b0)'),
    'white-space:nowrap',
    'line-height:1.4',
    'cursor:default',
  ].concat(opts.small ? ['max-width:100%', 'overflow:hidden'] : []).join(';');
  if (opts.title) pill.title = opts.title;
  if (color) {
    const dot = document.createElement('span');
    dot.style.cssText = 'width:7px;height:7px;border-radius:50%;background:' + color + ';flex-shrink:0;display:inline-block';
    pill.appendChild(dot);
  }
  const label = document.createElement('span');
  label.textContent = name;
  if (opts.small) {
    label.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0';
  }
  pill.appendChild(label);
  return pill;
}

// Add a "Crunchyroll" provider pill to a container if the title is on
// Crunchyroll. Gated on the frontend flag so no request fires when the
// integration is off. Works for fresh simulcasts TMDB doesn't list yet.
// Returns true iff a pill was actually inserted — callers use this to decide
// whether to fall through to the next provider in the resolution chain
// (TMDB → Crunchyroll → Fernsehserien.de, see _cardProviderChain / _crCheckCard
// / enrichModalWithTmdb).
async function _crProviderPill(title, containerEl, opts) {
  opts = opts || {};
  if (!crunchyrollSettings || crunchyrollSettings.enabled !== '1') return false;
  if (crunchyrollSettings.show_providers === '0') return false;
  if (!title || !containerEl) return false;
  try {
    const resp = await fetch('/api/crunchyroll/availability?title=' +
      encodeURIComponent(title).replace(/'/g, "%27"), { priority: "low" });
    const cd = await resp.json();
    if (!cd || !cd.available) return false;
    const already = Array.from(containerEl.querySelectorAll('span')).some(
      el => /crunchyroll/i.test(el.textContent || ''));
    if (already) return true;
    // Layout (flex/wrap/gap/margin) comes from CSS on the container
    // (.browse-tmdb-meta or .tmdb-providers) — no inline style set here, so
    // there's nothing stray left behind if this container ends up empty.
    const pill = _makeProviderPill('Crunchyroll', { small: !!opts.small, title: opts.small ? 'Crunchyroll' : undefined });
    containerEl.insertBefore(pill, containerEl.firstChild);
    return true;
  } catch (e) { /* silent */ return false; }
}

// ─── Fernsehserien.de lookup queue ──────────────────────────────────────
// fernsehserien_service.py rate-limits itself to ~1 request/1.5s through a
// single shared scraper instance, on purpose — it's a small, independently
// run site, and hammering it risks getting the scraper IP-blocked (see that
// file's docstring). If every card on a season/browse grid fires its own
// fetch() the moment it needs an FS check, they all just pile up behind that
// same server-side sleep at once — wasting browser connection slots and
// making pills pop in in a random, bursty order instead of steadily.
//
// _enqueueFsLookup funnels every FS lookup (grid cards AND the detail modal)
// through one client-side FIFO queue drained by a single consumer loop, so
// exactly one FS request is ever in flight, matching the server's own
// pacing. As soon as one title's result comes back (pill shown or not), the
// very next queued title starts immediately — no dead air, and pills appear
// steadily in the order titles became ready rather than all-at-once-then-wait.
const _fsQueue = [];
let _fsQueueRunning = false;

function _enqueueFsLookup(title, containerEl, opts) {
  return new Promise((resolve) => {
    _fsQueue.push({ title, containerEl, opts, resolve });
    _runFsQueue();
  });
}

async function _runFsQueue() {
  if (_fsQueueRunning) return;
  _fsQueueRunning = true;
  while (_fsQueue.length) {
    const job = _fsQueue.shift();
    let added = false;
    try {
      added = await _fsProviderPill(job.title, job.containerEl, job.opts);
    } catch (e) { /* _fsProviderPill already fails silently on its own */ }
    job.resolve(added);
  }
  _fsQueueRunning = false;
}

// Add a "Fernsehserien" provider pill naming the German streaming premiere
// provider fernsehserien.de reports for a title. This is the last link in the
// provider resolution chain (TMDB → Crunchyroll → Fernsehserien.de) — callers
// only reach it once both TMDB and Crunchyroll came up empty, which keeps
// request volume against this self-rate-limited, unofficial scraper low even
// though it's now wired into card hover too (see _cardProviderChain).
// Gated on the frontend flag so no request fires when the integration is off.
// Fails silently — a miss just means no pill. Returns true iff a pill was
// actually inserted. Not called directly by pill callers below — see
// _enqueueFsLookup above, which all of them go through instead so every FS
// request across the whole page shares one queue.
async function _fsProviderPill(title, containerEl, opts) {
  opts = opts || {};
  if (!fernsehserienSettings || fernsehserienSettings.enabled !== '1') return false;
  if (fernsehserienSettings.show_providers === '0') return false;
  if (!title || !containerEl) return false;
  try {
    const resp = await fetch('/api/fernsehserien/availability?title=' +
      encodeURIComponent(title).replace(/'/g, "%27"), { priority: "low" });
    const fd = await resp.json();
    if (!fd || !fd.available || !fd.provider) return false;
    const already = Array.from(containerEl.querySelectorAll('span')).some(
      el => el.textContent === fd.provider);
    if (already) return true;
    // Layout (flex/wrap/gap/margin) comes from CSS on the container
    // (.browse-tmdb-meta or .tmdb-providers) — no inline style set here, so
    // there's nothing stray left behind if this container ends up empty.
    const pill = _makeProviderPill(fd.provider, { small: !!opts.small, title: opts.small ? fd.provider : undefined });
    containerEl.insertBefore(pill, containerEl.firstChild);
    return true;
  } catch (e) { /* silent */ return false; }
}

// Card-level wrapper: skips when TMDB already shows Crunchyroll, otherwise adds
// the pill (creating the meta container if the card had no TMDB data at all).
// Returns true iff a pill is present afterwards (already-there or newly added)
// so _cardProviderChain knows whether to fall through to Fernsehserien.de.
async function _crCardPill(card, d) {
  if (!crunchyrollSettings || crunchyrollSettings.enabled !== '1') return false;
  if (crunchyrollSettings.show_providers === '0') return false;
  if (d && d.providers && d.providers.some(pp => /crunchyroll/i.test(pp))) return true;
  // Prefer the canonical TMDB title — it lines up with Crunchyroll's catalog
  // better than the raw site title.
  const title = (d && d.title) || card.dataset.title || card.dataset.tmdbTitle || "";
  if (!title) return false;
  const info = card.querySelector('.browse-info');
  if (!info) return false;
  let meta = info.querySelector('.browse-tmdb-meta');
  if (!meta) {
    meta = document.createElement('div');
    meta.className = 'browse-tmdb-meta';
    info.appendChild(meta);
  }
  return _crProviderPill(title, meta, { small: true });
}

// Card-level Fernsehserien wrapper, mirroring _crCardPill. Only ever called
// as the last step of _cardProviderChain (TMDB and Crunchyroll both empty),
// so this does not add extra load against the scraper for cards that are
// already covered by TMDB or Crunchyroll.
async function _fsCardPill(card, d) {
  if (!fernsehserienSettings || fernsehserienSettings.enabled !== '1') return false;
  if (fernsehserienSettings.show_providers === '0') return false;
  const title = (d && d.title) || card.dataset.title || card.dataset.tmdbTitle || "";
  if (!title) return false;
  const info = card.querySelector('.browse-info');
  if (!info) return false;
  let meta = info.querySelector('.browse-tmdb-meta');
  if (!meta) {
    meta = document.createElement('div');
    meta.className = 'browse-tmdb-meta';
    info.appendChild(meta);
  }
  return _enqueueFsLookup(title, meta, { small: true });
}

// ─── Registered extension provider pills ────────────────────────────────
// A thirdparty integration can add its own entry to the provider-pill
// fallback chain by calling the global registerProviderPill(name,
// resolverFn) from a small JS file it registers via
// register_thirdparty(provider_pill_script=...) (see
// web/thirdparties/registry.py) — that file is included as a <script> on
// every page (see base.html) while the integration is enabled, and just
// needs to call registerProviderPill() once at load time. resolverFn
// receives (title, imdbId) and must return (or resolve to) either
// null/undefined/false (no pill for this title) or {name, tooltip?}
// describing the pill to render via _makeProviderPill.
//
// window._providerPillResolvers is defined as an empty array as early as
// possible in base.html — before this file or any thirdparty script has
// necessarily loaded — so registerProviderPill() is always safe to call
// regardless of script order; the `||` below is just a defensive fallback
// in case app.js somehow loads first.
window._providerPillResolvers = window._providerPillResolvers || [];
window.registerProviderPill = window.registerProviderPill || function (name, resolverFn) {
  window._providerPillResolvers.push({ name: name, resolverFn: resolverFn });
};

// Last link in the provider resolution chain (TMDB → Crunchyroll →
// Fernsehserien.de → registered extensions), mirroring _crProviderPill's
// exact signature/contract: (title, containerEl, opts) -> Promise<boolean>.
// Tries each registered resolver in registration order and stops at the
// first one that returns a pill. A resolver that throws or returns garbage
// is treated as "no pill" — one broken extension resolver never blocks
// another, or the CR/FS pills that already ran before this.
async function _extensionProviderPill(title, containerEl, opts) {
  opts = opts || {};
  if (!title || !containerEl || !window._providerPillResolvers || !window._providerPillResolvers.length) return false;
  for (const entry of window._providerPillResolvers) {
    try {
      const result = await entry.resolverFn(title, opts.imdbId);
      if (!result || !result.name) continue;
      const already = Array.from(containerEl.querySelectorAll('span')).some(
        el => el.textContent === result.name);
      if (already) return true;
      const pill = _makeProviderPill(result.name, {
        small: !!opts.small,
        title: opts.small ? (result.tooltip || result.name) : result.tooltip,
      });
      containerEl.insertBefore(pill, containerEl.firstChild);
      return true;
    } catch (e) { /* one broken resolver shouldn't block the rest */ }
  }
  return false;
}

// Card-level wrapper, mirroring _crCardPill/_fsCardPill — only reached once
// TMDB, Crunchyroll and Fernsehserien.de all came up empty for this card.
async function _extensionCardPill(card, d) {
  const title = (d && d.title) || card.dataset.title || card.dataset.tmdbTitle || "";
  if (!title) return false;
  const info = card.querySelector('.browse-info');
  if (!info) return false;
  let meta = info.querySelector('.browse-tmdb-meta');
  if (!meta) {
    meta = document.createElement('div');
    meta.className = 'browse-tmdb-meta';
    info.appendChild(meta);
  }
  return _extensionProviderPill(title, meta, { small: true, imdbId: d && d.imdb_id });
}

// Provider resolution order for browse cards: TMDB → Crunchyroll →
// Fernsehserien.de → registered extensions. TMDB's own provider badge is
// rendered separately in _applyTmdbToCard; this only decides whether the
// fallback pill (CR, then FS, then extensions) is worth trying at all —
// skipped entirely once TMDB already has providers.
async function _cardProviderChain(card, d) {
  const tmdbHasProviders = !!(d && d.found && d.providers && d.providers.length);
  if (tmdbHasProviders) return;
  const crAdded = await _crCardPill(card, d);
  if (crAdded) return;
  const fsAdded = await _fsCardPill(card, d);
  if (fsAdded) return;
  await _extensionCardPill(card, d);
}

async function enrichModalWithTmdb(title, imdbId, _seq) {
  const provEl = document.getElementById('tmdbProviders');
  if (!provEl) return;
  // openSeries() may already have moved on to a different series by the time
  // any of the awaits below resolve — see _seriesLoadSeq. Bail rather than
  // write stale-series data (genres, rating, FSK, trailer, recommendations,
  // provider pills) into the now-current modal.
  const _stale = () => _seq !== undefined && _seq !== _seriesLoadSeq;
  if (!cineinfoSettings || !cineinfoSettings.tmdb_api_key) {
    // TMDB is off entirely — start the chain at Crunchyroll.
    const crAdded = await _crProviderPill(title, provEl);
    if (_stale()) return;
    if (crAdded) return;
    const fsAdded = await _enqueueFsLookup(title, provEl);
    if (_stale()) return;
    if (!fsAdded) await _extensionProviderPill(title, provEl, { imdbId });
    return;
  }
  try {
    let tmdbUrl = '/api/tmdb/info?title=' + encodeURIComponent(title).replace(/'/g, "%27");
    if (imdbId) tmdbUrl += '&imdb_id=' + encodeURIComponent(imdbId).replace(/'/g, "%27");
    const resp = await fetch(tmdbUrl);
    const d = await resp.json();
    if (_stale()) return;
    console.log("[CineInfo] Full Modal Data for", title, ":", d);
    console.log("[CineInfo] Settings Debug - General:", generalSettings, "CineInfo:", cineinfoSettings);
    const sTrailer = cineinfoSettings?.show_trailer ?? "1";
    const sRecs = cineinfoSettings?.show_recommendations ?? "1";
    console.log("[CineInfo] Final Checks - show_trailer:", sTrailer, "show_recs:", sRecs);
    if (!d.found) {
      // No TMDB data at all — start the chain at Crunchyroll.
      const crAdded = await _crProviderPill(title, provEl);
      if (_stale()) return;
      if (crAdded) return;
      const fsAdded = await _enqueueFsLookup(title, provEl);
      if (_stale()) return;
      if (!fsAdded) await _extensionProviderPill(title, provEl, { imdbId });
      return;
    }
    const tmdbHasProviders = !!(d.providers && d.providers.length);
    if (cineinfoSettings.show_providers !== '0' && tmdbHasProviders) {
      provEl.innerHTML = '';
      provEl.style.cssText = [
        'display:flex',
        'flex-wrap:wrap',
        'gap:5px',
        'margin:4px 0 16px',
        'max-height:74px',
        'overflow:hidden',
        'position:relative',
      ].join(';');
      const MAX_SHOW = 6;
      const visible = d.providers.slice(0, MAX_SHOW);
      const rest = d.providers.length - MAX_SHOW;
      visible.forEach(p => provEl.appendChild(_makeProviderPill(p)));
      if (rest > 0) {
        const more = document.createElement('span');
        more.textContent = '+' + rest + '\u00a0mehr';
        more.style.cssText = [
          'display:inline-flex',
          'align-items:center',
          'font-size:0.72rem',
          'font-weight:600',
          'padding:4px 10px',
          'border-radius:99px',
          'border:1.5px solid rgba(148,163,184,.3)',
          'background:var(--bg-elevated,#1a1a28)',
          'color:var(--text-muted,#55556a)',
          'white-space:nowrap',
          'cursor:default',
        ].join(';');
        provEl.appendChild(more);
      }
    }
    // Provider resolution order: TMDB → Crunchyroll → Fernsehserien.de. Only
    // fall through to CR/FS when TMDB itself has no provider data for this
    // title (not just when the display toggle is off) — avoids a redundant
    // or conflicting pill next to TMDB's own list.
    if (!tmdbHasProviders) {
      const crAdded = await _crProviderPill((d && d.title) || title, provEl);
      if (_stale()) return;
      if (!crAdded) {
        const fsAdded = await _enqueueFsLookup((d && d.title) || title, provEl);
        if (_stale()) return;
        if (!fsAdded) await _extensionProviderPill((d && d.title) || title, provEl, { imdbId: imdbId || (d && d.imdb_id) });
        if (_stale()) return;
      }
    }
    // TMDB Genres — ersetze die Seiten-Genres wenn aktiviert
    if (cineinfoSettings.show_genres === '1' && d.genres && d.genres.length) {
      const genresEl = document.getElementById('modalGenres');
      if (genresEl) {
        genresEl.innerHTML = '';
        d.genres.forEach(g => {
          const sp = document.createElement('span');
          sp.textContent = g;
          genresEl.appendChild(sp);
        });
      }
    }
    // Bewertung neben dem Titel
    if (cineinfoSettings.show_rating === '1' && d.vote_average) {
      // Badge lives INSIDE the h2 so it sits inline next to the title text
      const titleEl = document.getElementById('modalTitle');
      if (titleEl) {
        // Make h2 a flex row so title text + badge align on one line
        titleEl.style.cssText = [
          'display:flex',
          'align-items:center',
          'flex-wrap:wrap',
          'gap:8px',
          'margin:0 0 4px',
        ].join(';');
        // Remove old badge if modal was reopened
        const old = titleEl.querySelector('#tmdbRating');
        if (old) old.remove();
        const score = d.vote_average.toFixed(1);
        const col = d.vote_average >= 7 ? '#4ade80' : d.vote_average >= 5 ? '#fbbf24' : '#f87171';
        const brd = d.vote_average >= 7 ? 'rgba(74,222,128,.4)' : d.vote_average >= 5 ? 'rgba(251,191,36,.4)' : 'rgba(248,113,113,.4)';
        const ratingEl = document.createElement('span');
        ratingEl.id = 'tmdbRating';
        ratingEl.style.cssText = [
          'display:inline-flex',
          'align-items:center',
          'gap:4px',
          'font-size:0.72rem',
          'font-weight:700',
          'padding:2px 8px 2px 6px',
          'border-radius:99px',
          'border:1px solid ' + brd,
          'background:rgba(0,0,0,.22)',
          'color:' + col,
          'white-space:nowrap',
          'cursor:default',
          'letter-spacing:.01em',
          'flex-shrink:0',
          'vertical-align:middle',
        ].join(';');
        ratingEl.innerHTML =
          '<svg width="10" height="10" viewBox="0 0 24 24" fill="' + col + '" style="flex-shrink:0">' +
          '<path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>' +
          '</svg>' + score;
        titleEl.appendChild(ratingEl);
      }
    }
    // FSK unterhalb des Covers
    if (cineinfoSettings.show_fsk !== '0' && d.fsk) {
      const fskEl = document.getElementById('tmdbFsk');
      if (fskEl) {
        const fskNum = parseInt(d.fsk, 10);
        const _fskPalette = {
          0: { bg: 'rgba(255,255,255,.07)', bc: 'rgba(255,255,255,.3)', c: '#d1d5db' },
          6: { bg: 'rgba(234,179,8,.12)', bc: 'rgba(234,179,8,.55)', c: '#fbbf24' },
          12: { bg: 'rgba(34,197,94,.12)', bc: 'rgba(34,197,94,.5)', c: '#4ade80' },
          16: { bg: 'rgba(59,130,246,.12)', bc: 'rgba(59,130,246,.5)', c: '#60a5fa' },
          18: { bg: 'rgba(239,68,68,.12)', bc: 'rgba(239,68,68,.5)', c: '#f87171' },
        };
        const fp = _fskPalette[fskNum] || { bg: 'rgba(148,163,184,.1)', bc: 'rgba(148,163,184,.35)', c: '#94a3b8' };
        fskEl.textContent = 'FSK\u00a0' + d.fsk;
        fskEl.style.cssText = [
          'display:block',
          'font-size:0.75rem',
          'font-weight:700',
          'padding:3px 10px',
          'border-radius:99px',
          'border:1px solid ' + fp.bc,
          'background:' + fp.bg,
          'color:' + fp.c,
          'text-align:center',
          'white-space:nowrap',
          'letter-spacing:.02em',
          'width:100%',
          'box-sizing:border-box',
        ].join(';');
      }

    }
    // Trailer
    const trailerEl = document.getElementById('modalTrailer');
    const trailerSection = document.getElementById('trailerSection');
    if (trailerEl && trailerSection) {
      const showT = (cineinfoSettings?.show_trailer !== '0');
      if (showT && d.trailer_key) {
        trailerEl.innerHTML = `<iframe src="https://www.youtube.com/embed/${d.trailer_key}" allowfullscreen></iframe>`;
        trailerSection.style.display = 'block';
      } else {
        trailerEl.innerHTML = '';
        trailerSection.style.display = 'none';
      }
    }
    // Recommendations
    const recEl = document.getElementById('modalRecommendations');
    const recSection = document.getElementById('recommendationsSection');
    if (recEl && recSection) {
      const showR = (cineinfoSettings?.show_recommendations !== '0');
      if (showR && d.recommendations && d.recommendations.length) {
        recSection.style.display = 'block';
        let html = '<div class="recommendations-grid">';
        d.recommendations.forEach(r => {
          const poster = r.poster_path ? `https://image.tmdb.org/t/p/w185${r.poster_path}` : '';
          html += `
            <div class="rec-card" data-title="${esc(r.title)}" onclick="searchForTitle(this.dataset.title)">
              <img class="rec-poster" src="${proxyImg(poster)}" alt="">
              <div class="rec-title" title="${esc(r.title)}">${esc(r.title)}</div>
            </div>
          `;
        });
        html += '</div>';
        recEl.innerHTML = html;
      } else {
        recSection.style.display = 'none';
      }
    }
  } catch (e) { /* best-effort */ }
}

function searchForTitle(title) {
  closeModal();
  const sIn = document.getElementById("searchInput");
  if (sIn) {
    sIn.value = title;
    doSearch();
  } else {
    saveAdvSearchState();
    window.location.href = "/?q=" + encodeURIComponent(title);
  }
}

function showToast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.style.display = "";
  t.classList.remove("show");
  void t.offsetWidth; // reflow so transition fires even on repeated calls
  t.classList.add("show");
  clearTimeout(t._hideTimer);
  t._hideTimer = setTimeout(() => t.classList.remove("show"), 4000);
}

function unesc(s) {
  const d = document.createElement("textarea");
  d.innerHTML = s || "";
  return d.value;
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = unesc(s);
  return d.innerHTML;
}

const downloadAllLangsBtn = document.getElementById("downloadAllLangsBtn");
let defaultSyncLanguage = "German Dub";

async function checkLangSeparation() {
  try {
    const resp = await fetch("/api/settings");
    const data = await resp.json();
    langSeparationEnabled = data.lang_separation === "1";
    if (data.sync_language) {
      defaultSyncLanguage = data.sync_language;
    }
    if (downloadAllLangsBtn) {
      downloadAllLangsBtn.style.display = langSeparationEnabled ? "" : "none";
    }
  } catch (e) {
    /* ignore */
  }
}

async function startDownloadAllLangs() {
  const episodes = getAllEpisodeUrls();
  if (!episodes.length) {
    showToast(t("Keine Episoden verfügbar.", "No episodes available."));
    return;
  }
  if (!availableProviders) {
    showToast(t("Anbieter-Daten noch nicht geladen.", "Provider data not yet loaded."));
    return;
  }

  // VeeV availability check — startDownloadAllLangs uses provider from availableProviders loop,
  // but for FilmPalast movies VeeV is a single provider, so check before queuing anything.
  {
    const allProviders = availableProviders ? Object.values(availableProviders).flat() : [];
    const hasVeev = allProviders.some(p => p.toLowerCase().replace(/\s+(hd|hq)$/i, "") === "veev");
    if (hasVeev) {
      const ok = await veevCheckAvailability(episodes[0]);
      if (!ok) return;
    }
  }

  downloadAllLangsBtn.disabled = true;
  downloadAllBtn.disabled = true;
  downloadSelectedBtn.disabled = true;

  const upscaleCheck = document.getElementById("upscaleCheck");
  let queued = 0;
  try {
    for (const [lang, providers] of Object.entries(availableProviders)) {
      if (!providers.length) continue;
      const provider = providers.includes("VOE") ? "VOE" : providers[0];
      const dlBody = {
        episodes,
        language: lang,
        provider,
        title: currentSeriesTitle,
        series_url: currentSeriesUrl,
        upscale: !!(upscaleCheck && upscaleCheck.closest("#upscaleCheckWrapper") && upscaleCheck.closest("#upscaleCheckWrapper").style.display !== "none" && upscaleCheck.checked),
      };
      if (customPathSelect && customPathSelect.value) {
        dlBody.custom_path_id = parseInt(customPathSelect.value);
      }
      const resp = await fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(dlBody),
      });
      const data = await resp.json();
      if (!data.error) queued++;
    }
    showToast(queued + t(" Sprache(n) zur Warteschlange hinzugefügt", " Language(s) added to download queue"));
    if (typeof loadQueue === "function") loadQueue();
  } catch (e) {
    showToast(t("Downloads konnten nicht zur Warteschlange hinzugefügt werden: " + e.message, "Downloads could not be added to the download queue: " + e.message));
  } finally {
    downloadAllLangsBtn.disabled = false;
    downloadAllBtn.disabled = false;
    downloadSelectedBtn.disabled = false;
  }
}

// ===== Favourites =====

let _currentFavUrl = "";
let _currentFavTitle = "";
let _currentFavPoster = "";

async function _updateFavouriteBtn(url, title, posterUrl) {
  _currentFavUrl = url;
  _currentFavTitle = title;
  _currentFavPoster = posterUrl;
  const btn = document.getElementById("favouriteBtn");
  if (!btn) return;
  try {
    const resp = await fetch("/api/favourites/check?series_url=" + encodeURIComponent(url).replace(/'/g, "%27"));
    const data = await resp.json();
    btn.textContent = data.is_favourite ? "♥" : "♡";
    btn.style.color = data.is_favourite ? "var(--accent, #e05a5a)" : "var(--text-secondary)";
    btn.dataset.isFav = data.is_favourite ? "1" : "0";
  } catch (e) { /* ignore */ }
}

async function toggleFavourite() {
  const btn = document.getElementById("favouriteBtn");
  if (!btn || !_currentFavUrl) return;
  const isFav = btn.dataset.isFav === "1";
  try {
    if (isFav) {
      await fetch("/api/favourites", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ series_url: _currentFavUrl }),
      });
      btn.textContent = "♡";
      btn.style.color = "var(--text-secondary)";
      btn.dataset.isFav = "0";
      showToast("Aus Favoriten entfernt");
    } else {
      await fetch("/api/favourites", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          series_url: _currentFavUrl,
          title: _currentFavTitle,
          poster_url: _currentFavPoster,
        }),
      });
      btn.textContent = "♥";
      btn.style.color = "var(--accent, #e05a5a)";
      btn.dataset.isFav = "1";
      showToast(t("Zu Favoriten hinzugefügt ♥", "Added to favorites ♥"));
    }
  } catch (e) {
    showToast(t("Fehler: " + e.message, "Error: " + e.message));
  }
}

window.openSeriesModal = function (url, title) {
  openSeries(url);
};

// Pre-load autosync map on page start so search results also get badges
loadAutoSyncJobs();
loadCineinfoSettings();
loadGeneralSettings();

// Auto-search if ?q= is in the query string (e.g. from Seerr page)
(function () {
  const params = new URLSearchParams(window.location.search);
  const q = params.get("q");
  if (q && searchInput) {
    window.history.replaceState({}, "", window.location.pathname);
    searchInput.value = q;
    doSearch();
  }
})();

// ── Direkt-Link Modal ────────────────────────────────────────────────────────

function openDirectLinkModal() {
  const overlay = document.getElementById("directLinkOverlay");
  const input = document.getElementById("directLinkInput");
  const error = document.getElementById("directLinkError");
  error.textContent = "";
  error.style.display = "none";
  input.value = "";
  overlay.style.display = "block";
  document.body.style.overflow = "hidden";
  setTimeout(() => input.focus(), 50);
}

function closeDirectLinkModal() {
  document.getElementById("directLinkOverlay").style.display = "none";
  document.body.style.overflow = "";
}

function closeDLModalOutside(event) {
  if (event.target === document.getElementById("directLinkOverlay")) {
    closeDirectLinkModal();
  }
}

function submitDirectLink() {
  const input = document.getElementById("directLinkInput");
  const error = document.getElementById("directLinkError");
  let url = input.value.trim();

  // Normalize: strip trailing slash
  url = url.replace(/\/+$/, "");

  error.textContent = "";
  error.style.display = "none";

  if (!url) return;

  const isSto = /s\.to\/serie\/[^\/]+/.test(url);
  const isAniworld = /aniworld\.to\/anime\/stream\/[^\/]+/.test(url);
  const isMegakino = /megakino[^/]*\/watch\/[^/]+\/[a-f0-9]{24}/i.test(url);
  const isHanime = /hanime\.tv\/videos\/hentai\/[^/?#]+/.test(url);
  const isKnownSite = isSto || isAniworld || isMegakino || isHanime;

  // hanime is an adult source: a direct link must not bypass the 18+ gate.
  if (isHanime) {
    const _hanOn = ((((generalSettings || {}).sources || {}).enabled || {}).hanime === "1");
    if (!_hanOn) {
      error.textContent = t("hanime ist deaktiviert. Bitte zuerst in den Einstellungen aktivieren (18+).", "hanime is disabled. Please enable it in Settings first (18+).");
      error.style.display = "block";
      input.focus();
      return;
    }
  }

  if (isKnownSite) {
    // Extract base series URL (strip staffel/episode sub-paths)
    let seriesUrl = url;
    if (isSto) {
      const m = url.match(/(https?:\/\/[^/]*s\.to\/serie\/[^\/]+)/);
      if (m) seriesUrl = m[1];
    } else if (isAniworld) {
      const m = url.match(/(https?:\/\/[^/]*aniworld\.to\/anime\/stream\/[^\/]+)/);
      if (m) seriesUrl = m[1];
    } else if (isMegakino) {
      // megakino: the post URL itself is the series/movie; drop any ?episode=N
      seriesUrl = url.split("?")[0];
    } else if (isHanime) {
      // hanime: strip any ?ep=N / query -> canonical franchise (series) URL
      seriesUrl = url.split("?")[0].split("#")[0];
    }

    closeDirectLinkModal();
    openSeries(seriesUrl);
    return;
  }

  // Not one of the known scraper sites -- try it as a generic yt-dlp direct
  // link (e.g. a raw .m3u8 HLS master playlist). MediaForge fetches the
  // available quality variants first so the user can pick one, instead of
  // just guessing "best" (see GitHub issue #8).
  if (!/^https?:\/\//i.test(url)) {
    error.textContent = t("Bitte eine gültige URL eingeben.", "Please enter a valid URL.");
    error.style.display = "block";
    input.focus();
    return;
  }

  startDirectLinkProbe(url);
}

document.addEventListener("DOMContentLoaded", () => {
  const dlInput = document.getElementById("directLinkInput");
  if (dlInput) {
    dlInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitDirectLink();
      if (e.key === "Escape") closeDirectLinkModal();
    });
  }
  const dlNameInput = document.getElementById("dlFinalizeName");
  if (dlNameInput) {
    dlNameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitDirectLinkDownload();
      if (e.key === "Escape") closeDirectLinkFinalizeModal();
    });
  }
});

// ── Ende Direkt-Link Modal ───────────────────────────────────────────────────

// ── Direct Link: format-picker + finalize modals (yt-dlp probe, issue #8) ───

let _dlProbeUrl = "";
let _dlProbeTitle = "";
let _dlProbeProvider = null;
let _dlSelectedFormat = "bestvideo+bestaudio/best";

async function startDirectLinkProbe(url) {
  _dlProbeUrl = url;
  _dlProbeTitle = "";
  _dlProbeProvider = null;
  _dlSelectedFormat = "bestvideo+bestaudio/best";
  closeDirectLinkModal();
  openDirectLinkFormatModal();

  const listEl = document.getElementById("dlFormatList");
  const spinnerEl = document.getElementById("dlFormatSpinner");
  const errorEl = document.getElementById("dlFormatError");
  const continueBtn = document.getElementById("dlFormatContinueBtn");
  listEl.innerHTML = "";
  errorEl.textContent = "";
  errorEl.style.display = "none";
  continueBtn.disabled = true;
  spinnerEl.style.display = "flex";

  try {
    const resp = await fetch("/api/direct-link/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await resp.json();
    spinnerEl.style.display = "none";
    if (data.error) {
      errorEl.textContent = t("Konnte diesen Link nicht analysieren: ", "Could not analyze this link: ") + data.error;
      errorEl.style.display = "block";
      return;
    }
    renderDirectLinkFormats(data);
  } catch (e) {
    spinnerEl.style.display = "none";
    errorEl.textContent = t("Konnte diesen Link nicht analysieren: ", "Could not analyze this link: ") + e.message;
    errorEl.style.display = "block";
  }
}

function renderDirectLinkFormats(data) {
  const listEl = document.getElementById("dlFormatList");
  const errorEl = document.getElementById("dlFormatError");
  const continueBtn = document.getElementById("dlFormatContinueBtn");
  const formats = data.formats || [];
  _dlProbeTitle = data.title || "";
  _dlProbeProvider = data.provider || null;
  listEl.innerHTML = "";

  formats.forEach((f, idx) => {
    const row = document.createElement("label");
    row.className = "dl-format-row";

    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "dlFormatChoice";
    radio.value = f.selector;
    radio.checked = idx === 0;
    if (idx === 0) _dlSelectedFormat = f.selector;
    radio.addEventListener("change", () => { _dlSelectedFormat = f.selector; });

    const labelSpan = document.createElement("span");
    if (f.best) {
      labelSpan.textContent = t("Automatisch (beste Qualität)", "Automatic (best quality)");
    } else {
      let txt = f.height ? `${f.height}p` : t("Unbekannte Qualität", "Unknown quality");
      if (f.filesize_mb) {
        txt += f.filesize_mb >= 1024
          ? ` (${(f.filesize_mb / 1024).toFixed(1)} GB)`
          : ` (${f.filesize_mb} MB)`;
      }
      labelSpan.textContent = txt;
    }

    row.appendChild(radio);
    row.appendChild(labelSpan);
    listEl.appendChild(row);
  });

  continueBtn.disabled = formats.length === 0;
  if (!formats.length) {
    errorEl.textContent = t("Keine Streams gefunden.", "No streams found.");
    errorEl.style.display = "block";
  }
}

function openDirectLinkFormatModal() {
  document.getElementById("dlFormatOverlay").style.display = "block";
  document.body.style.overflow = "hidden";
}

function closeDirectLinkFormatModal() {
  document.getElementById("dlFormatOverlay").style.display = "none";
  document.body.style.overflow = "";
}

function closeDLFormatModalOutside(event) {
  if (event.target === document.getElementById("dlFormatOverlay")) {
    closeDirectLinkFormatModal();
  }
}

function confirmDirectLinkFormat() {
  closeDirectLinkFormatModal();
  openDirectLinkFinalizeModal();
}

function openDirectLinkFinalizeModal() {
  const nameInput = document.getElementById("dlFinalizeName");
  const errorEl = document.getElementById("dlFinalizeError");
  errorEl.textContent = "";
  errorEl.style.display = "none";
  nameInput.value = _dlProbeTitle || "";
  loadDirectLinkPaths();
  document.getElementById("dlFinalizeOverlay").style.display = "block";
  document.body.style.overflow = "hidden";
  setTimeout(() => nameInput.focus(), 50);
}

function closeDirectLinkFinalizeModal() {
  document.getElementById("dlFinalizeOverlay").style.display = "none";
  document.body.style.overflow = "";
}

function closeDLFinalizeModalOutside(event) {
  if (event.target === document.getElementById("dlFinalizeOverlay")) {
    closeDirectLinkFinalizeModal();
  }
}

async function loadDirectLinkPaths() {
  const select = document.getElementById("dlFinalizePathSelect");
  if (!select) return;
  try {
    const resp = await fetch("/api/custom-paths");
    const data = await resp.json();
    const paths = data.paths || [];
    while (select.options.length > 1) select.remove(1);
    paths.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name;
      select.appendChild(opt);
    });
  } catch (e) {
    /* best-effort */
  }
}

async function submitDirectLinkDownload() {
  const nameInput = document.getElementById("dlFinalizeName");
  const pathSelect = document.getElementById("dlFinalizePathSelect");
  const errorEl = document.getElementById("dlFinalizeError");
  const btn = document.getElementById("dlFinalizeDownloadBtn");
  const title = nameInput.value.trim() || t("Direkter Download", "Direct Download");

  errorEl.textContent = "";
  errorEl.style.display = "none";
  btn.disabled = true;
  try {
    const body = {
      url: _dlProbeUrl,
      title,
      format_id: _dlSelectedFormat || "bestvideo+bestaudio/best",
    };
    if (_dlProbeProvider) body.provider = _dlProbeProvider;
    if (pathSelect && pathSelect.value) body.custom_path_id = parseInt(pathSelect.value);

    const resp = await fetch("/api/direct-link/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (data.error) {
      errorEl.textContent = data.error;
      errorEl.style.display = "block";
      return;
    }
    closeDirectLinkFinalizeModal();
    showToast(t("Zur Download-Warteschlange hinzugefügt", "Added to download queue"));
    if (typeof loadQueue === "function") loadQueue();
  } catch (e) {
    errorEl.textContent = t("Download-Anfrage fehlgeschlagen: ", "Download request failed: ") + e.message;
    errorEl.style.display = "block";
  } finally {
    btn.disabled = false;
  }
}

// ── Ende Direct-Link Format-/Finalize-Modals ────────────────────────────────

// Auto-open modal if ?open=<encoded-url> is in the query string (e.g. from Favourites page)
// Or trigger search if ?q=<search> is present
(function () {
  const params = new URLSearchParams(window.location.search);
  const openUrl = params.get("open");
  const searchQuery = params.get("q");

  if (openUrl || searchQuery) {
    // Remove query param from browser history without reload
    const cleanUrl = window.location.pathname;
    window.history.replaceState({}, "", cleanUrl);

    const action = () => {
      if (openUrl) {
        openSeries(decodeURIComponent(openUrl));
      } else if (searchQuery) {
        const input = document.getElementById("searchInput");
        if (input) {
          input.value = decodeURIComponent(searchQuery);
          doSearch();
        }
      }
    };

    // Wait for DOM to be ready
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", action);
    } else {
      action();
    }
  }
})();

// ── Erweiterte Suche / Advanced Search ──────────────────────────────────────────────────────────

let currentType = 'tv';
let allGenres = { tv: [], movie: [] };
let selectedKeywords = []; // Array of { id, name }
let selectedIncludeProviders = []; // Array of { provider_id, provider_name }
let selectedExcludeProviders = []; // Array of { provider_id, provider_name }
let allWatchProviders = [];         // cached provider list for current type/region
let selectedWatchRegion = '';       // ISO 3166-1 code, e.g. "DE"

// Pagination & Buffer State
let allLoadedResults = [];
let totalTmdbResults = 0;
let currentTmdbPage = 0;
let currentPageIndex = 0; // 0-indexed local grid page
let lastSearchParams = null;
let isFetchingTmdb = false;
let activeFetchPromise = null;

function saveAdvSearchState() {
  const filtersContainer = document.getElementById('advFiltersContainer');
  if (!filtersContainer) return;
  const checkedGenres = Array.from(document.querySelectorAll('.genre-checkbox:checked')).map(cb => cb.value);
  const isMenuOpen = filtersContainer.classList.contains('is-open');
  const state = {
    currentType,
    checkedGenres,
    selectedKeywords,
    selectedIncludeProviders,
    selectedExcludeProviders,
    selectedWatchRegion,
    isMenuOpen,
    yearMin: document.getElementById('yearMin')?.value || '',
    yearMax: document.getElementById('yearMax')?.value || '',
    voteMin: document.getElementById('voteMin')?.value || '0',
    sortBy: document.getElementById('sortBy')?.value || 'popularity.desc',
    allLoadedResults,
    totalTmdbResults,
    currentTmdbPage,
    currentPageIndex,
    lastSearchParamsStr: lastSearchParams ? lastSearchParams.toString() : null,
    timestamp: Date.now()
  };
  localStorage.setItem('advSearchState', JSON.stringify(state));
}

function loadAdvSearchState() {
  const stateStr = localStorage.getItem('advSearchState');
  if (!stateStr) return;
  try {
    const state = JSON.parse(stateStr);
    const ageMs = Date.now() - state.timestamp;
    if (ageMs > 5 * 60 * 1000) { // 5 minutes cache expiration, cause there could be a switch...
      localStorage.removeItem('advSearchState');
      return;
    }

    currentType = state.currentType || 'tv';
    selectedKeywords = state.selectedKeywords || [];
    selectedIncludeProviders = state.selectedIncludeProviders || [];
    selectedExcludeProviders = state.selectedExcludeProviders || [];
    selectedWatchRegion = state.selectedWatchRegion || '';
    allLoadedResults = state.allLoadedResults || [];
    totalTmdbResults = state.totalTmdbResults || 0;
    currentTmdbPage = state.currentTmdbPage || 0;
    currentPageIndex = state.currentPageIndex || 0;
    lastSearchParams = state.lastSearchParamsStr ? new URLSearchParams(state.lastSearchParamsStr) : null;

    // Restore DOM inputs
    const mediaTV = document.querySelector('#mediaTypeFilter [data-type="tv"]');
    const mediaMovie = document.querySelector('#mediaTypeFilter [data-type="movie"]');
    if (mediaTV && mediaMovie) {
      mediaTV.classList.toggle('active', currentType === 'tv');
      mediaMovie.classList.toggle('active', currentType === 'movie');
    }

    const yearMinEl = document.getElementById('yearMin');
    if (yearMinEl) yearMinEl.value = state.yearMin || '';

    const yearMaxEl = document.getElementById('yearMax');
    if (yearMaxEl) yearMaxEl.value = state.yearMax || '';

    const voteMinEl = document.getElementById('voteMin');
    if (voteMinEl) {
      voteMinEl.value = state.voteMin || '0';
      const voteLabel = document.getElementById("voteLabel");
      if (voteLabel) {
        const val = parseInt(state.voteMin, 10);
        if (val === 0) voteLabel.textContent = "0 - 10 (Alle)";
        else voteLabel.textContent = `${val} - 10`;
      }
    }

    const sortByEl = document.getElementById('sortBy');
    if (sortByEl) {
      let savedSortBy = state.sortBy || 'popularity.desc';
      if (savedSortBy.startsWith('first_air_date')) {
        savedSortBy = savedSortBy.replace('first_air_date', 'primary_release_date');
      }
      sortByEl.value = savedSortBy;
    }

    const filtersContainer = document.getElementById('advFiltersContainer');
    if (filtersContainer && state.isMenuOpen) {
      filtersContainer.classList.add('is-open');
    }

    renderSelectedKeywords();
    renderSelectedProviders('include');
    renderSelectedProviders('exclude');

    const watchRegionEl = document.getElementById('watchRegion');
    if (watchRegionEl) watchRegionEl.value = selectedWatchRegion;

    window.checkedGenresToRestore = state.checkedGenres || [];

    // Restore results UI
    if (lastSearchParams !== null) {
      if (totalTmdbResults > 0) {
        document.getElementById('resultsCount').textContent = `${totalTmdbResults} Ergebnisse`;
      } else {
        document.getElementById('resultsCount').textContent = `0 Ergebnisse`;
      }
      renderLocalPage();
    }
  } catch (e) {
    console.error("Error loading advanced search state:", e);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  if (document.getElementById('advFiltersContainer')) {
    initUI();
    loadAdvSearchState();
    loadGenres();
    loadWatchRegions();
    loadWatchProviders();
    Promise.all([
      loadDownloadedFolders(),
      loadAutoSyncJobs(),
      loadCineinfoSettings(),
      loadGeneralSettings()
    ]).catch(e => console.error("Error pre-loading data for badges:", e));
  }
});

function initUI() {
  // Toggle Filters Panel
  const toggleBtn = document.getElementById('advFiltersToggle');
  const filtersContainer = document.getElementById('advFiltersContainer');
  if (toggleBtn && filtersContainer) {
    toggleBtn.addEventListener('click', () => {
      filtersContainer.classList.toggle('is-open');
      saveAdvSearchState();
    });
  }

  // Media Type Filter
  document.querySelectorAll('#mediaTypeFilter .filter-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('#mediaTypeFilter .filter-btn').forEach(b => b.classList.remove('active'));
      e.target.classList.add('active');
      currentType = e.target.dataset.type;

      // Reset genres on type change
      document.querySelectorAll('.genre-checkbox').forEach(cb => cb.checked = false);
      updateGenreLabel();
      renderGenres();
      loadWatchProviders(); // provider list differs between tv & movie
      saveAdvSearchState();
    });
  });

  // Custom Select for Genres
  const genreSelect = document.getElementById('genreSelect');
  const genreSelectTrigger = document.getElementById('genreSelectTrigger');
  if (genreSelectTrigger) {
    genreSelectTrigger.addEventListener('click', (e) => {
      e.stopPropagation();
      genreSelect.classList.toggle('is-open');
    });
  }
  document.addEventListener('click', (e) => {
    if (genreSelect && !genreSelect.contains(e.target)) {
      genreSelect.classList.remove('is-open');
    }
  });

  // Vote range label
  const voteMin = document.getElementById("voteMin");
  const voteLabel = document.getElementById("voteLabel");
  if (voteMin && voteLabel) {
    voteMin.addEventListener('input', () => {
      const val = parseInt(voteMin.value, 10);
      voteLabel.textContent = `${val} - 10`;
    });
  }

  // Search button
  const runBtn = document.getElementById("runSearchBtn");
  if (runBtn) {
    runBtn.addEventListener('click', () => {
      // Start fresh search
      allLoadedResults = [];
      currentTmdbPage = 0;
      currentPageIndex = 0;
      totalTmdbResults = 0;

      runSearch();
    });
  }

  // Reset button
  const resetBtn = document.getElementById("resetFiltersBtn");
  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      document.querySelectorAll('.genre-checkbox').forEach(cb => cb.checked = false);
      updateGenreLabel();
      selectedKeywords = [];
      renderSelectedKeywords();
      selectedIncludeProviders = [];
      selectedExcludeProviders = [];
      renderSelectedProviders('include');
      renderSelectedProviders('exclude');
      selectedWatchRegion = '';
      const watchRegionReset = document.getElementById('watchRegion');
      if (watchRegionReset) watchRegionReset.value = '';
      document.getElementById('yearMin').value = '';
      document.getElementById('yearMax').value = '';
      document.getElementById('voteMin').value = '0';
      document.getElementById('voteLabel').textContent = '0 - 10';
      document.getElementById('sortBy').value = 'popularity.desc';

      currentType = 'tv';
      const mediaTV = document.querySelector('#mediaTypeFilter [data-type="tv"]');
      const mediaMovie = document.querySelector('#mediaTypeFilter [data-type="movie"]');
      if (mediaTV && mediaMovie) {
        mediaTV.classList.add('active');
        mediaMovie.classList.remove('active');
      }
      renderGenres();

      document.getElementById('resultsGrid').innerHTML = '<div class="adv-empty-state">Filter zurückgesetzt. Bitte wähle deine Filter und klicke auf Suchen.</div>';
      document.getElementById('resultsCount').textContent = '';
      document.getElementById('keywordInput').value = '';
      const incProvInput = document.getElementById('includeProviderInput');
      if (incProvInput) incProvInput.value = '';
      const excProvInput = document.getElementById('excludeProviderInput');
      if (excProvInput) excProvInput.value = '';
      loadWatchProviders(); // currentType reset to 'tv'
      document.getElementById('advPagination').style.display = 'none';
      lastSearchParams = null;
      localStorage.removeItem('advSearchState');
    });
  }

  // Pagination buttons
  const prevBtn = document.getElementById("prevPageBtn");
  const nextBtn = document.getElementById("nextPageBtn");
  if (prevBtn) prevBtn.addEventListener('click', goToPrevPage);
  if (nextBtn) nextBtn.addEventListener('click', goToNextPage);

  // Add auto-saving listeners to input controls
  const yearMin = document.getElementById("yearMin");
  const yearMax = document.getElementById("yearMax");
  const voteMinInput = document.getElementById("voteMin");
  const sortByInput = document.getElementById("sortBy");

  if (yearMin) yearMin.addEventListener('change', saveAdvSearchState);
  if (yearMax) yearMax.addEventListener('change', saveAdvSearchState);
  if (voteMinInput) voteMinInput.addEventListener('change', saveAdvSearchState);
  if (sortByInput) sortByInput.addEventListener('change', saveAdvSearchState);

  setupKeywordAutocomplete();
  setupProviderAutocomplete('include');
  setupProviderAutocomplete('exclude');

  const watchRegionSelect = document.getElementById('watchRegion');
  if (watchRegionSelect) {
    watchRegionSelect.addEventListener('change', () => {
      selectedWatchRegion = watchRegionSelect.value;
      loadWatchProviders(); // available providers depend on the region
      saveAdvSearchState();
    });
  }
}

async function loadGenres() {
  try {
    const r = await fetch("/api/tmdb/genres");
    const d = await r.json();
    if (d.tv && d.movie) {
      const lang = window.__LANG === 'en' ? 'en' : 'de';
      allGenres = {
        tv:    (d.tv[lang]    || d.tv['de']    || d.tv),
        movie: (d.movie[lang] || d.movie['de'] || d.movie),
      };
      renderGenres();
    } else {
      const dd = document.getElementById('genreSelectDropdown');
      if (dd) dd.innerHTML = `<div style="color:var(--error);padding:0.5rem;">${t('API Key Fehler.', 'API key error.')}</div>`;
    }
  } catch (e) {
    const dd = document.getElementById('genreSelectDropdown');
    if (dd) dd.innerHTML = `<div style="color:var(--error);padding:0.5rem;">${t('Netzwerkfehler.', 'Network error.')}</div>`;
  }
}

function renderGenres() {
  const container = document.getElementById('genreSelectDropdown');
  if (!container) return;
  const genres = allGenres[currentType] || [];

  if (genres.length === 0) {
    container.innerHTML = `<div class="skeleton-loader" style="height: 40px; width: 100%; border-radius: 6px;"></div>`;
    return;
  }

  const currentlyChecked = Array.from(document.querySelectorAll('.genre-checkbox:checked')).map(cb => parseInt(cb.value, 10));

  container.innerHTML = '';
  genres.forEach(g => {
    const isChecked = currentlyChecked.includes(g.id) || (window.checkedGenresToRestore && window.checkedGenresToRestore.map(id => parseInt(id, 10)).includes(g.id));
    const cbId = `genre_${g.id}`;

    const item = document.createElement('label');
    item.className = 'custom-select-item';
    item.setAttribute('for', cbId);

    item.innerHTML = `
      <input type="checkbox" id="${cbId} " class="genre-checkbox chb-main" value="${g.id}" ${isChecked ? 'checked' : ''} />
      <span>${escapeHtml(g.name)}</span>
    `;

    item.querySelector('input').addEventListener('change', () => {
      updateGenreLabel();
      saveAdvSearchState();
    });
    container.appendChild(item);
  });

  window.checkedGenresToRestore = null;
  updateGenreLabel();
}

function updateGenreLabel() {
  const label = document.getElementById('genreSelectLabel');
  if (!label) return;

  const checkedBoxes = Array.from(document.querySelectorAll('.genre-checkbox:checked'));
  if (checkedBoxes.length === 0) {
    label.textContent = t("Alle Genres", "All Genres");
  } else if (checkedBoxes.length <= 2) {
    const names = checkedBoxes.map(cb => cb.nextElementSibling.textContent);
    label.textContent = names.join(', ');
  } else {
    label.textContent = `${checkedBoxes.length} ${t("Genres ausgewählt", "genres selected")}`;
  }
}

function setupKeywordAutocomplete() {
  const input = document.getElementById('keywordInput');
  const autocomplete = document.getElementById('keywordAutocomplete');
  let debounceTimer = null;
  let currentSuggestions = [];

  if (!input || !autocomplete) return;

  document.addEventListener('click', (e) => {
    if (!input.contains(e.target) && !autocomplete.contains(e.target)) {
      autocomplete.style.display = 'none';
      currentSuggestions = [];
    }
  });

  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    const q = input.value.trim();
    if (q.length < 2) {
      autocomplete.style.display = 'none';
      currentSuggestions = [];
      return;
    }

    debounceTimer = setTimeout(async () => {
      try {
        const r = await fetch(`/api/tmdb/keywords?q=${encodeURIComponent(q)}`);
        if (r.status === 404) {
          autocomplete.innerHTML = `<div class="kw-item" style="color:var(--text-muted)">Datenbank wird noch geladen...</div>`;
          autocomplete.style.display = 'block';
          currentSuggestions = [];
          return;
        }
        const d = await r.json();

        if (d.results && d.results.length > 0) {
          currentSuggestions = d.results;
          autocomplete.innerHTML = '';
          d.results.forEach(kw => {
            const item = document.createElement('div');
            item.className = 'kw-item';
            item.textContent = kw.name;
            item.addEventListener('click', () => {
              addKeyword(kw);
              input.value = '';
              autocomplete.style.display = 'none';
              currentSuggestions = [];
            });
            autocomplete.appendChild(item);
          });
          autocomplete.style.display = 'block';
        } else {
          autocomplete.innerHTML = `<div class="kw-item" style="color:var(--text-muted)">Keine Ergebnisse</div>`;
          autocomplete.style.display = 'block';
          currentSuggestions = [];
        }
      } catch (e) {
        currentSuggestions = [];
      }
    }, 350);
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (autocomplete.style.display !== 'none' && currentSuggestions.length > 0) {
        addKeyword(currentSuggestions[0]);
        input.value = '';
        autocomplete.style.display = 'none';
        currentSuggestions = [];
      }
    }
  });

  input.addEventListener('focus', () => {
    if (autocomplete.children.length > 0 && input.value.trim().length >= 2) {
      autocomplete.style.display = 'block';
    }
  });
}

function addKeyword(kw) {
  if (selectedKeywords.find(k => k.id === kw.id)) return;
  selectedKeywords.push(kw);
  renderSelectedKeywords();
  saveAdvSearchState();
}

function removeKeyword(id) {
  selectedKeywords = selectedKeywords.filter(k => k.id !== id);
  renderSelectedKeywords();
  saveAdvSearchState();
}

function renderSelectedKeywords() {
  const container = document.getElementById('selectedKeywords');
  if (!container) return;
  container.innerHTML = '';
  selectedKeywords.forEach(kw => {
    const tag = document.createElement('div');
    tag.className = 'kw-tag';
    tag.innerHTML = `
      <span>${escapeHtml(kw.name)}</span>
      <span class="kw-tag-remove" onclick="removeKeyword(${kw.id})">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </span>
    `;
    container.appendChild(tag);
  });
}

// ── Watch Providers (Include / Exclude) ──────────────────────────────────────

async function loadWatchRegions() {
  const select = document.getElementById('watchRegion');
  if (!select) return;
  try {
    const r = await fetch('/api/tmdb/watch_regions');
    const d = await r.json();
    const regions = d.results || [];
    regions.forEach(reg => {
      const opt = document.createElement('option');
      opt.value = reg.iso_3166_1;
      opt.textContent = reg.native_name || reg.english_name || reg.iso_3166_1;
      select.appendChild(opt);
    });
    // Re-apply restored selection now that options exist
    if (selectedWatchRegion) select.value = selectedWatchRegion;
  } catch (e) {
    console.error("Error loading watch regions:", e);
  }
}

async function loadWatchProviders() {
  try {
    const params = new URLSearchParams({ type: currentType });
    if (selectedWatchRegion) params.append('watch_region', selectedWatchRegion);
    const r = await fetch(`/api/tmdb/watch_providers?${params.toString()}`);
    const d = await r.json();
    allWatchProviders = d.results || [];
  } catch (e) {
    console.error("Error loading watch providers:", e);
    allWatchProviders = [];
  }
}

function setupProviderAutocomplete(listKey) {
  const inputId = listKey === 'include' ? 'includeProviderInput' : 'excludeProviderInput';
  const acId    = listKey === 'include' ? 'includeProviderAutocomplete' : 'excludeProviderAutocomplete';
  const input = document.getElementById(inputId);
  const autocomplete = document.getElementById(acId);
  if (!input || !autocomplete) return;

  let currentSuggestions = [];

  document.addEventListener('click', (e) => {
    if (!input.contains(e.target) && !autocomplete.contains(e.target)) {
      autocomplete.style.display = 'none';
      currentSuggestions = [];
    }
  });

  function renderSuggestions() {
    const q = input.value.trim().toLowerCase();
    if (q.length < 1) {
      autocomplete.style.display = 'none';
      currentSuggestions = [];
      return;
    }
    const matches = allWatchProviders
      .filter(p => (p.provider_name || '').toLowerCase().includes(q))
      .slice(0, 20);
    currentSuggestions = matches;

    if (matches.length === 0) {
      autocomplete.innerHTML = `<div class="kw-item" style="color:var(--text-muted)">${t('Keine Ergebnisse', 'No results')}</div>`;
      autocomplete.style.display = 'block';
      return;
    }
    autocomplete.innerHTML = '';
    matches.forEach(p => {
      const item = document.createElement('div');
      item.className = 'kw-item';
      item.textContent = p.provider_name;
      item.addEventListener('click', () => {
        addProvider(listKey, p);
        input.value = '';
        autocomplete.style.display = 'none';
        currentSuggestions = [];
      });
      autocomplete.appendChild(item);
    });
    autocomplete.style.display = 'block';
  }

  input.addEventListener('input', renderSuggestions);
  input.addEventListener('focus', () => { if (input.value.trim()) renderSuggestions(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (currentSuggestions.length > 0) {
        addProvider(listKey, currentSuggestions[0]);
        input.value = '';
        autocomplete.style.display = 'none';
        currentSuggestions = [];
      }
    }
  });
}

function addProvider(listKey, p) {
  const arr = listKey === 'include' ? selectedIncludeProviders : selectedExcludeProviders;
  if (arr.find(x => x.provider_id === p.provider_id)) return;
  arr.push({ provider_id: p.provider_id, provider_name: p.provider_name });
  renderSelectedProviders(listKey);
  saveAdvSearchState();
}

function removeProvider(listKey, id) {
  if (listKey === 'include') {
    selectedIncludeProviders = selectedIncludeProviders.filter(x => x.provider_id !== id);
  } else {
    selectedExcludeProviders = selectedExcludeProviders.filter(x => x.provider_id !== id);
  }
  renderSelectedProviders(listKey);
  saveAdvSearchState();
}

function renderSelectedProviders(listKey) {
  const containerId = listKey === 'include' ? 'selectedIncludeProviders' : 'selectedExcludeProviders';
  const arr = listKey === 'include' ? selectedIncludeProviders : selectedExcludeProviders;
  const container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = '';
  arr.forEach(p => {
    const tag = document.createElement('div');
    tag.className = 'kw-tag';
    tag.innerHTML = `
      <span>${escapeHtml(p.provider_name)}</span>
      <span class="kw-tag-remove" onclick="removeProvider('${listKey}', ${p.provider_id})">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </span>
    `;
    container.appendChild(tag);
  });
}

function getGridColumns() {
  const grid = document.getElementById("resultsGrid");
  if (!grid) return 1;
  const cols = window.getComputedStyle(grid).getPropertyValue('grid-template-columns').split(' ').length;
  return Math.max(1, cols);
}

function getPageSize() {
  // 10 items, but fill up complete rows... I mean cmon...
  const cols = getGridColumns();
  const rows = Math.max(2, Math.ceil(10 / cols)); // At least 2 rows
  return cols * rows;
}

async function runSearch() {
  await Promise.all([loadDownloadedFolders(), loadAutoSyncJobs(), loadCineinfoSettings(), loadGeneralSettings()]).catch(() => {});

  //search params
  lastSearchParams = new URLSearchParams();
  lastSearchParams.append("type", currentType);

  const voteMin = parseInt(document.getElementById("voteMin").value, 10);
  if (voteMin > 0) {
    lastSearchParams.append("vote_average.gte", voteMin);
  }

  const yearMin = document.getElementById("yearMin").value;
  if (yearMin) {
    if (currentType === 'tv') lastSearchParams.append("first_air_date.gte", `${yearMin}-01-01`);
    else lastSearchParams.append("primary_release_date.gte", `${yearMin}-01-01`);
  }

  const yearMax = document.getElementById("yearMax").value;
  if (yearMax) {
    if (currentType === 'tv') lastSearchParams.append("first_air_date.lte", `${yearMax}-12-31`);
    else lastSearchParams.append("primary_release_date.lte", `${yearMax}-12-31`);
  }

  const checkedGenres = Array.from(document.querySelectorAll('.genre-checkbox:checked')).map(cb => cb.value);
  if (checkedGenres.length > 0) {
    lastSearchParams.append("with_genres", checkedGenres.join(','));
  }

  if (selectedKeywords.length > 0) {
    lastSearchParams.append("with_keywords", selectedKeywords.map(k => k.id).join(','));
  }

  if (selectedWatchRegion) {
    lastSearchParams.append("watch_region", selectedWatchRegion);
  }
  // TMDB OR-joins providers with a pipe ("|")
  if (selectedIncludeProviders.length > 0) {
    lastSearchParams.append("with_watch_providers", selectedIncludeProviders.map(p => p.provider_id).join('|'));
  }
  if (selectedExcludeProviders.length > 0) {
    lastSearchParams.append("without_watch_providers", selectedExcludeProviders.map(p => p.provider_id).join('|'));
  }

  let sortBy = document.getElementById('sortBy').value || "popularity.desc";
  if (currentType === 'tv') {
    if (sortBy.startsWith("primary_release_date")) {
      sortBy = sortBy.replace("primary_release_date", "first_air_date");
    }
  } else {
    if (sortBy.startsWith("first_air_date")) {
      sortBy = sortBy.replace("first_air_date", "primary_release_date");
    }
  }
  lastSearchParams.append("sort_by", sortBy);

  lastSearchParams.append("language", window.__LANG === 'de' ? 'de-DE' : 'en-US');
  lastSearchParams.append("include_adult", "false");

  // Show loading
  const grid = document.getElementById("resultsGrid");
  const countSpan = document.getElementById("resultsCount");
  grid.innerHTML = '<div class="skeleton-loader" style="grid-column: 1/-1; height: 300px; border-radius: 12px;"></div>';
  countSpan.textContent = t("Lädt...", "Loading...");
  document.getElementById('advPagination').style.display = 'none';

  // Fetch first page
  let success = await fetchNextTmdbPage();
  if (success) {
    while (allLoadedResults.length < getPageSize() && allLoadedResults.length < totalTmdbResults) {
      const success = await fetchNextTmdbPage();
      if (!success) break;
    }
    renderLocalPage();
    saveAdvSearchState();
  } else {
    grid.innerHTML = `<div class="adv-empty-state" style="color:var(--error)">${t("Netzwerkfehler bei der Suche.", "Network error in search.")}</div>`;
    countSpan.textContent = t("Fehler", "Error");
  }
}

async function fetchNextTmdbPage() {
  if (!lastSearchParams) return false;
  if (activeFetchPromise) {
    return activeFetchPromise;
  }

  // Prevent fetching if we already reached total results
  if (currentTmdbPage > 0 && allLoadedResults.length >= totalTmdbResults) return true;

  isFetchingTmdb = true;
  const btn = document.getElementById("runSearchBtn");
  if (btn) btn.disabled = true;

  activeFetchPromise = (async () => {
    const searchParamsToUse = new URLSearchParams(lastSearchParams);
    const nextPage = currentTmdbPage + 1;
    searchParamsToUse.append("page", nextPage);

    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 15000);
      const r = await fetch(`/api/tmdb/discover?${searchParamsToUse.toString()}`, { signal: controller.signal });
      clearTimeout(timeoutId);

      const d = await r.json();
      if (d.error) throw new Error(d.error);

      totalTmdbResults = d.total_results || 0;
      const countSpan = document.getElementById("resultsCount");
      if (countSpan) countSpan.textContent = t(`${totalTmdbResults} Ergebnisse`, `${totalTmdbResults} results`);

      allLoadedResults.push(...(d.results || []));
      currentTmdbPage = nextPage;

      return true;
    } catch (e) {
      console.error("Error fetching TMDB page:", e);
      return false;
    } finally {
      isFetchingTmdb = false;
      if (btn) btn.disabled = false;
      activeFetchPromise = null;
    }
  })();

  return activeFetchPromise;
}

async function goToNextPage() {
  const pageSize = getPageSize();
  const maxPages = Math.ceil(totalTmdbResults / pageSize);
  if (currentPageIndex + 1 >= maxPages) return;

  currentPageIndex++;

  // Check if we have enough items loaded
  const requiredItems = (currentPageIndex + 1) * pageSize;
  if (allLoadedResults.length < requiredItems && allLoadedResults.length < totalTmdbResults) {
    const grid = document.getElementById("resultsGrid");
    grid.innerHTML = '<div class="skeleton-loader" style="grid-column: 1/-1; height: 300px; border-radius: 12px;"></div>';

    while (allLoadedResults.length < requiredItems && allLoadedResults.length < totalTmdbResults) {
      const success = await fetchNextTmdbPage();
      if (!success) break;
    }
  }

  renderLocalPage();
  saveAdvSearchState();
}

function goToPrevPage() {
  if (currentPageIndex > 0) {
    currentPageIndex--;
    renderLocalPage();
    saveAdvSearchState();
  }
}

function updatePaginationUI() {
  const pag = document.getElementById('advPagination');
  if (totalTmdbResults === 0) {
    pag.style.display = 'none';
    return;
  }
  pag.style.display = 'flex';

  const pageSize = getPageSize();
  const maxPages = Math.max(1, Math.ceil(totalTmdbResults / pageSize));

  document.getElementById('pageIndicator').textContent = `Seite ${currentPageIndex + 1} / ${maxPages}`;

  document.getElementById("prevPageBtn").disabled = (currentPageIndex === 0);
  document.getElementById("nextPageBtn").disabled = (currentPageIndex + 1 >= maxPages);
}

function renderLocalPage() {
  const grid = document.getElementById("resultsGrid");
  grid.innerHTML = '';

  const pageSize = getPageSize();
  const startIndex = currentPageIndex * pageSize;
  const pageResults = allLoadedResults.slice(startIndex, startIndex + pageSize);

  if (pageResults.length === 0) {
    if (totalTmdbResults === 0) {
      grid.innerHTML = `<div class="adv-empty-state">Keine Ergebnisse gefunden.</div>`;
    } else {
      grid.innerHTML = `<div class="adv-empty-state">Keine Ergebnisse auf dieser Seite.</div>`;
    }
    updatePaginationUI();
    return;
  }

  pageResults.forEach(r => {
    const title = r.title || r.name;
    const year = r.release_date ? r.release_date.split('-')[0] : (r.first_air_date ? r.first_air_date.split('-')[0] : '');

    const card = document.createElement('div');
    card.className = 'tmdb-card';
    card.dataset.tmdbId = r.id;
    card.dataset.title = title || "";

    card.onclick = () => {
      openAniSearchModal(title, r.id, currentType, r.poster_path);
    };

    let posterHtml = `<div class="tmdb-card-poster" style="display:flex;align-items:center;justify-content:center;color:var(--text-muted)">Kein Bild</div>`;
    if (r.poster_path) {
      const url = `https://image.tmdb.org/t/p/w342${r.poster_path}`;
      posterHtml = `<img class="tmdb-card-poster" src="${url}" loading="lazy" alt="Poster" />`;
    }

    const rating = r.vote_average ? parseFloat(r.vote_average).toFixed(1) : '-';

    const seasonsHtml = (currentType === 'tv') ? `<span class="tmdb-season-info" style="opacity: 0.5;">...</span>` : '';

    card.innerHTML = `
      ${posterHtml}
      <div class="tmdb-card-info">
        <h4 class="tmdb-card-title" title="${escapeHtml(title)}">${escapeHtml(title)}</h4>
        <div class="tmdb-card-meta">
          <span class="tmdb-rating">
            <svg viewBox="0 0 24 24"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"></path></svg>
            ${rating}
          </span>
          ${year ? `<span>• ${year}</span>` : ''}
        </div>
        <div class="tmdb-card-meta">
          ${seasonsHtml ? `<span>• ${seasonsHtml}</span>` : ''}
        </div>
        <div class="tmdb-card-desc">${escapeHtml(r.overview || t('Keine Beschreibung verfügbar.', 'No description available.'))}</div>
        <div class="tmdb-card-desc-hover">
          <h4 class="tmdb-card-title-hover">${escapeHtml(title)}</h4>
          <div class="tmdb-card-desc-text-hover">${escapeHtml(r.overview || t('Keine Beschreibung verfügbar.', 'No description available.'))}</div>
        </div>
      </div>
    `;

    grid.appendChild(card);

    addDownloadedBadgeForTmdb(card, title, r.id);
    addSyncBadgeForTmdb(card, title);

    if (currentType === 'tv') {
      fetchSeasonsInfo(r.id, card);
    }
  });

  updatePaginationUI();

  // Pre-fetch next TMDB page immediately if we might need it soon
  ensureBuffer();
}

async function ensureBuffer() {
  const pageSize = getPageSize();
  const requiredNextItems = (currentPageIndex + 2) * pageSize;

  let didFetch = false;
  while (allLoadedResults.length < requiredNextItems && allLoadedResults.length < totalTmdbResults && !isFetchingTmdb) {
    const success = await fetchNextTmdbPage();
    if (!success) break;
    didFetch = true;
  }
  if (didFetch) {
    saveAdvSearchState();
  }
}

async function fetchSeasonsInfo(id, cardElement) {
  try {
    const r = await fetch(`/api/tmdb/details?id=${id}&type=tv`);
    const d = await r.json();
    const infoSpan = cardElement.querySelector('.tmdb-season-info');
    if (infoSpan) {
      if (d.number_of_seasons !== undefined) {
        infoSpan.textContent = `${d.number_of_seasons} ${t('Staffel', 'Season')}${window.__LANG === 'de' && d.number_of_seasons !== 1 ? 'n' : ''}`;
        infoSpan.style.opacity = '1';
      } else {
        infoSpan.textContent = t('Unbekannt', 'Unknown');
        infoSpan.style.opacity = '1';
      }
    }
  } catch (e) {
    const infoSpan = cardElement.querySelector('.tmdb-season-info');
    if (infoSpan) infoSpan.textContent = '';
  }
}

// ---- AniWorld Search Modal Logic ----

function openAniSearchModal(title, tmdbId, type, posterPath, presetLocalizedTitle) {
  const modal = document.getElementById('aniSearchModalOverlay');
  if (!modal) return;
  modal.style.display = 'flex';
  document.body.style.overflow = 'hidden';

  const cleanTitle = title.trim().replace(/!+$/, "");
  document.getElementById('aniSearchTitle').textContent = `Suche nach "${cleanTitle}"...`;
  document.getElementById('aniSearchSpinner').style.display = 'block';
  document.getElementById('aniSearchResults').innerHTML = '';

  // presetLocalizedTitle: an already-known localized (e.g. German) title,
  // used as an extra search variant WITHOUT needing MediaForge's own TMDB
  // integration configured — e.g. Anime Seasons passes item.title_localized
  // here, which the self-hosted jikan-rest instance's own TMDB translator
  // already resolved server-side. See runAniSearch().
  runAniSearch(cleanTitle, tmdbId, type, posterPath, presetLocalizedTitle);
}

function closeAniSearchModal() {
  const modal = document.getElementById('aniSearchModalOverlay');
  if (modal) {
    modal.style.display = 'none';
    document.body.style.overflow = '';
  }
}

function closeAniSearchModalOutside(event) {
  const modalContent = document.getElementById('aniSearchModal');
  if (modalContent && !modalContent.contains(event.target)) {
    closeAniSearchModal();
  }
}

window.openAniSearchModal = openAniSearchModal;
window.closeAniSearchModal = closeAniSearchModal;
window.closeAniSearchModalOutside = closeAniSearchModalOutside;

async function runAniSearch(primaryTitle, tmdbId, type, posterPath, presetLocalizedTitle) {
  await Promise.all([loadDownloadedFolders(), loadAutoSyncJobs(), loadCineinfoSettings(), loadGeneralSettings()]).catch(() => {});

  const grid = document.getElementById('aniSearchResults');
  grid.innerHTML = '<div class="skeleton-loader" style="grid-column: 1/-1; height: 150px; border-radius: 12px;"></div>';

  // Helper to clean title
  const cleanTitleForSearch = (str) => {
    return str
      .trim()
      .replace(/!+$/, "")
      .replace("‼", "!!")
      .replace(/[–—―]/g, "-")
      .replace(/\s*-\s*/g, "-") // removes any spaces around a hyphen, e.g. "word - word" -> "word-word"
      .replace(/\s+/g, " ")
      .trim();
  };

  // Helper to generate apostrophe variants
  const getApostropheVariants = (str) => {
    const cleaned = cleanTitleForSearch(str);
    if (!cleaned) return [];
    if (cleaned.includes("'") || cleaned.includes("’")) {
      const straight = cleaned.replace(/’/g, "'");
      const curly = cleaned.replace(/'/g, "’");
      const none = cleaned.replace(/['’]/g, "");
      const variants = [straight];
      if (curly !== straight) variants.push(curly);
      if (none !== straight && none !== curly) variants.push(none);
      return variants;
    }
    return [cleaned];
  };

  const primaryCleaned = cleanTitleForSearch(primaryTitle);
  const primaryVariants = getApostropheVariants(primaryTitle);
  let searchTitles = [...primaryVariants];
  let enCleaned = "";
  let localizedCleaned = "";

  // Seed the localized variant from a preset (e.g. Anime Seasons passing
  // jikan-rest's own title_localized) BEFORE the TMDB lookup below — this
  // way German search results work even when MediaForge's own TMDB
  // integration isn't configured, since the self-hosted Jikan instance
  // already resolved it server-side. The TMDB lookup further below still
  // runs and can add its own (usually identical) variant on top; dedup
  // against searchTitles prevents that from showing as a true duplicate.
  if (presetLocalizedTitle) {
    const presetCleaned = cleanTitleForSearch(presetLocalizedTitle);
    if (presetCleaned && presetCleaned.toLowerCase() !== primaryCleaned.toLowerCase()) {
      localizedCleaned = presetCleaned;
      getApostropheVariants(presetLocalizedTitle).forEach(variant => {
        if (!searchTitles.some(t => t.toLowerCase() === variant.toLowerCase())) {
          searchTitles.push(variant);
        }
      });
    }
  }

  console.log("Primary search titles:", searchTitles);

  try {
    if (tmdbId && type) {
      const detailRes = await fetch(`/api/tmdb/details?id=${tmdbId}&type=${type}`);
      const detailData = await detailRes.json();

      // /api/tmdb/details asks TMDB for language=de (or "en" only if the
      // UI itself is set to English — see routes/search.py's
      // api_tmdb_details), so detailData.name/title IS already the
      // localized (typically German) title — add it as its own search
      // variant. This matters for callers whose primaryTitle is NOT
      // already German, e.g. Anime Seasons: its primary title comes
      // straight from MyAnimeList/Jikan in English/Romaji, so without
      // this AniWorld/S.to (German sites, usually listing the German
      // name) would never be searched under the name they actually use.
      const localizedName = detailData.name || detailData.title || "";
      if (localizedName) {
        localizedCleaned = cleanTitleForSearch(localizedName);
        if (localizedCleaned.toLowerCase() !== primaryCleaned.toLowerCase()) {
          const localizedVariants = getApostropheVariants(localizedName);
          console.log("Localized search titles:", localizedVariants);
          localizedVariants.forEach(variant => {
            if (!searchTitles.some(t => t.toLowerCase() === variant.toLowerCase())) {
              searchTitles.push(variant);
            }
          });
        } else {
          localizedCleaned = ""; // same as primary — nothing extra to show/search
        }
      }

      if (detailData.translations && detailData.translations.translations) {
        const enTrans = detailData.translations.translations.find(t => t.iso_639_1 === 'en');
        if (enTrans && enTrans.data && enTrans.data.name) {
          enCleaned = cleanTitleForSearch(enTrans.data.name);
          const enVariants = getApostropheVariants(enTrans.data.name);
          console.log("English search titles:", enVariants);
          enVariants.forEach(variant => {
            if (!searchTitles.some(t => t.toLowerCase() === variant.toLowerCase())) {
              searchTitles.push(variant);
            }
          });
        }
      }
    }
  } catch (e) {
    // Ignore translation fetch error
  }

  const searchSite = async (site, kw) => {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000);
    try {
      const resp = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keyword: kw, site }),
        signal: controller.signal
      });
      const data = await resp.json();
      return data.results || [];
    } catch (e) {
      return [];
    } finally {
      clearTimeout(timeoutId);
    }
  };

  try {
    let allPromises = [];
    searchTitles.forEach(kw => {
      allPromises.push(searchSite("aniworld", kw));
      allPromises.push(searchSite("sto", kw));
      allPromises.push(searchSite("filmpalast", kw));
      allPromises.push(searchSite("megakino", kw));
    });

    const resultsArrays = await Promise.all(allPromises.map(p => p.catch(() => [])));

    document.getElementById('aniSearchSpinner').style.display = 'none';

    let displayTitle = primaryCleaned;
    if (localizedCleaned && localizedCleaned.toLowerCase() !== primaryCleaned.toLowerCase()) {
      displayTitle += ` / ${localizedCleaned}`;
    }
    if (enCleaned && enCleaned.toLowerCase() !== primaryCleaned.toLowerCase() && enCleaned.toLowerCase() !== localizedCleaned.toLowerCase()) {
      displayTitle += ` / ${enCleaned}`;
    }
    document.getElementById('aniSearchTitle').textContent = `Ergebnisse für "${displayTitle}"`;
    grid.innerHTML = '';

    let allResults = [];
    resultsArrays.forEach(arr => {
      allResults = allResults.concat(arr);
    });

    // Deduplicate by URL
    const seenUrls = new Set();
    allResults = allResults.filter(r => {
      if (seenUrls.has(r.url)) return false;
      seenUrls.add(r.url);
      return true;
    });

    // HARD FILTER: Only keep results where title contains ANY of our keywords, or keyword contains title (apostrophe-insensitive & hyphen-insensitive)
    allResults = allResults.filter(r => {
      if (!r.title) return false;
      
      const normalizeForCompare = (str) => {
        return str.toLowerCase()
                  .replace(/’/g, "'")
                  .replace(/[–—―]/g, "-")
                  .replace(/\s*-\s*/g, "-") // remove spaces around hyphens first
                  .replace(/-/g, " ")       // treat hyphens as spaces
                  .replace(/\s+/g, " ")
                  .trim();
      };

      const tNorm = normalizeForCompare(r.title);

      return searchTitles.some(kw => {
        const kNorm = normalizeForCompare(kw);
        
        if (tNorm.includes(kNorm) || kNorm.includes(tNorm)) return true;
        
        const tNoApos = tNorm.replace(/'/g, "");
        const kNoApos = kNorm.replace(/'/g, "");
        return tNoApos.includes(kNoApos) || kNoApos.includes(tNoApos);
      });
    });

    if (allResults.length === 0) {
      grid.innerHTML = `<div class="adv-empty-state">${t("Keine exakten Treffer für " + escapeHtml(displayTitle) + " auf AniWorld, SerienStream oder FilmPalast gefunden.", "No exact matches for " + escapeHtml(displayTitle) + " found on AniWorld, SerienStream or FilmPalast.")}</div>`;
      return;
    }

    allResults.forEach(r => {
      const card = document.createElement('div');
      card.className = 'browse-card';
      card.style.cursor = 'pointer';

      // Determine provider styling
      let provClass = '';
      if (r.url.includes('aniworld.to')) provClass = 'prov-ani';
      if (r.url.includes('s.to') || r.url.includes('serienstream.to')) provClass = 'prov-sto';
      if (r.url.includes('filmpalast.to')) provClass = 'prov-fp';
      if (r.url.includes('megakino')) provClass = 'prov-mk';

      card.innerHTML = `
        <img src="" loading="lazy" alt="Cover" style="width:100%;aspect-ratio:2/3;object-fit:cover;background:var(--bg-elevated);display:block" />
        <div class="browse-info">
          <div class="browse-title" title="${escapeHtml(r.title)}">${escapeHtml(r.title)}</div>
          <div class="browse-provider ${provClass}">${provClass === 'prov-ani' ? 'AniWorld' : provClass === 'prov-sto' ? 'S.to' : provClass === 'prov-mk' ? 'MegaKino' : 'FilmPalast'}</div>
        </div>
      `;

      card.onclick = () => {
        closeAniSearchModal();
        openSeries(r.url);
      };

      addDownloadedBadge(card, r.title);
      addSyncBadge(card, r.url);

      grid.appendChild(card);

      // Always fetch poster from the source site (like the normal search does)
      advLoadPoster(r.url, card.querySelector('img'));
    });

  } catch (e) {
    document.getElementById('aniSearchSpinner').style.display = 'none';
    grid.innerHTML = `<div class="adv-empty-state" style="color:var(--error)">Fehler bei der Suche.</div>`;
  }
}

async function advLoadPoster(url, imgEl) {
  try {
    const resp = await fetch("/api/series?url=" + encodeURIComponent(url));
    const data = await resp.json();
    if (data.poster_url) {
      imgEl.src = (typeof proxyImg === 'function' ? proxyImg(data.poster_url) : data.poster_url);
      imgEl.onload = () => {
        const card = imgEl.closest('.browse-card');
        if (card) card.classList.add('loaded');
      };
      imgEl.onerror = () => {
        const card = imgEl.closest('.browse-card');
        if (card) card.classList.add('loaded');
        imgEl.style.display = 'none';
      };
    } else {
      const card = imgEl.closest('.browse-card');
      if (card) card.classList.add('loaded');
      imgEl.style.display = 'none';
    }
  } catch (e) {
    const card = imgEl.closest('.browse-card');
    if (card) card.classList.add('loaded');
    imgEl.style.display = 'none';
  }
}

function escapeHtml(unsafe) {
  if (!unsafe) return '';
  return (unsafe + '').replace(/[&<"']/g, function (m) {
    switch (m) {
      case '&': return '&amp;';
      case '<': return '&lt;';
      case '"': return '&quot;';
      case "'": return '&#039;';
    }
  });
}

