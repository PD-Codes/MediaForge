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
let _autosyncDefaultPathId = "";
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
const filmoNewMoviesGrid = document.getElementById("filmoNewMoviesGrid");
const filmoPopularMoviesGrid = document.getElementById("filmoPopularMoviesGrid");
const nineanimeNewGrid = document.getElementById("nineanimeNewGrid");
const nineanimePopularGrid = document.getElementById("nineanimePopularGrid");
const aniwavesNewGrid = document.getElementById("aniwavesNewGrid");
const aniwavesPopularGrid = document.getElementById("aniwavesPopularGrid");
const hanimeNewGrid = document.getElementById("hanimeNewGrid");
const hanimeTrendingGrid = document.getElementById("hanimeTrendingGrid");

let currentSeasons = [];
let currentSeriesTitle = "";
let currentSeriesUrl = "";
let currentSeriesCoverUrl = "";
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
// Seerr request context for the currently-open modal, or null when the
// modal was opened normally (Discover/Home/search). Set by
// openSeriesFromSeerr() (called from seerr.js) and cleared in closeModal().
// See _updateSeerrModalActions() and the approve hook in
// _submitDownloadGroups() for how this is consumed.
let _seerrModalContext = null;
// Provider data per language label
let availableProviders = null;
let langSeparationEnabled = false;
// Language fallback groups ("group:<id>" + name + ordered languages), loaded
// with the settings in checkLangSeparation().
let languageGroups = [];
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
// {loose_title_key: folder}. Alternative names the library's folders answer to,
// resolved server-side from TMDB (web/library_aliases.py). Object rather than
// Map because it arrives as JSON and is only ever read by key.
let downloadedAliases = {};
// The folder list, pre-reduced to the two forms downloadedFolderFor() compares
// against. Built ONCE per library load instead of per title: the two passes
// used to run normalizeQuotes()+toLowerCase() and _looseTitleKey() on every
// folder for every title asked about, which is fine for the handful of cards a
// start page renders and catastrophic for the Catalogue page's thirteen
// thousand rows -- 13k x ~800 folders x two regex replaces measured at ~4.7
// seconds of solid main-thread work per pass, and the page runs two passes.
// With the folder side precomputed the same work is ~0.2s.
let _dlFoldersLower = [];   // normalizeQuotes(folder.toLowerCase())
let _dlFoldersLoose = [];   // _looseTitleKey(folder)
let mediascanTitles = new Set(); // normalised titles from Plex/Jellyfin as fallback
// The same titles reduced to letters and digits (_looseTitleKey). A second
// index rather than a loop, so the fallback stays O(1) per card.
let mediascanLooseTitles = new Set();
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
  // Cards that already have their payload only need to be redrawn -- that is
  // free, and it is what makes a card enriched before the settings arrived
  // pick up its hover drawer without a second round-trip.
  document.querySelectorAll('.browse-card').forEach(card => {
    if (card._mfTmdb) _applyTmdbToCard(card, card._mfTmdb);
  });
  document.querySelectorAll('[data-tmdb-title]').forEach(card => {
    if (card._mfTmdb) return;                      // already answered
    // Pills present but no drawer is exactly the state this pass exists for,
    // so .browse-tmdb-meta is no longer a reason to skip the card; the queue's
    // own dataset.tmdbQueued guard stops duplicate requests instead.
    if (card.dataset.tmdbQueued === "1" && _tmdbPending.size) return;
    const title = card.dataset.tmdbTitle;
    if (title) _queueTmdbEnrich(card, title); // use batched path
  });
}

// ---------------------------------------------------------------------------
// Batched TMDB enrichment — collects visible-card titles for 80 ms then
// fires ONE /api/tmdb/batch POST instead of N individual /api/tmdb/info GETs.
// This keeps the TMDB rate-limiter happy and stops the UI flooding the server.
// ---------------------------------------------------------------------------

// Keyed by "<title>::<card url>" rather than bare title -- two DIFFERENT
// catalogue entries (different provider, unrelated show) can share the
// exact same title string, and a bare-title key meant the second card
// silently inherited the first one's cached tmdb_id/genres/"already
// downloaded" badge (see _applyTmdbToCard) the moment they landed in the
// same 80 ms batch window, which is exactly what multi-provider search
// does. A card with no url (rare -- see cardKey()) still falls back to the
// bare title, same behaviour as before.
const _tmdbPending = new Map(); // key → { title, cards: [...] }
let _tmdbBatchTimer = null;

function _tmdbCardKey(card, title) {
  const url = card && card.dataset ? card.dataset.url : "";
  return url ? title + "::" + url : title;
}

// Answers we already have, by the same composite key _tmdbPending uses. Two
// jobs:
//  - a card rendered for a title/url another card already resolved is
//    enriched without a request at all;
//  - it is what mfPrewarmTmdb() fills (title-only keys, no card involved --
//    see its own comment), so a card that does not exist yet can still have
//    its data ready by the time it does.
// Unbounded on purpose within a page load: the payload is small, the page is
// one session, and evicting would only re-fetch something we asked for once.
const _tmdbMemo = new Map();    // key → payload
// Titles a prewarm is already in flight for, so two rows sharing a title do
// not both ask.
const _tmdbPrewarmed = new Set();

/** Resolve TMDB/CineInfo data for titles whose cards are NOT on the page yet.
 *
 *  The home feed fetches a reserve of cards beyond the number it shows (see
 *  routes/browse.py's pool), so hiding a source refills the row from cards the
 *  client already holds. Those cards render with no genres, no rating and no
 *  FSK until the IntersectionObserver notices them and a batch comes back --
 *  which is a visible flicker of empty metadata every time a filter is
 *  switched, on data that was sitting one request away the whole time.
 *
 *  This asks for it up front and remembers the answer, so a filter switch is
 *  a re-render rather than a round-trip. Deliberately fire-and-forget and
 *  low-priority: nothing waits for it, and a failure just means the old
 *  lazy path handles those cards as before.
 *
 *  /api/tmdb/batch caps a request at 25 titles, so this chunks -- and it runs
 *  the chunks one after another rather than all at once, because the point is
 *  to be finished before the user touches a filter, not to be first in the
 *  connection queue ahead of the poster images.
 */
window.mfPrewarmTmdb = async function (titles) {
  if (!Array.isArray(titles) || !titles.length) return;
  if (!cineinfoSettings) {
    try { await loadGeneralSettings(); } catch (e) { return; }
  }
  if (!cineinfoSettings || !cineinfoSettings.tmdb_api_key) return;
  const want = [];
  titles.forEach(function (t) {
    const title = String(t || "").trim();
    if (!title || _tmdbMemo.has(title) || _tmdbPrewarmed.has(title)) return;
    _tmdbPrewarmed.add(title);
    want.push(title);
  });
  if (!want.length) return;
  for (let i = 0; i < want.length; i += 25) {
    const chunk = want.slice(i, i + 25);
    try {
      const resp = await fetch("/api/tmdb/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ titles: chunk }),
        priority: "low",
      });
      if (!resp.ok) continue;
      const results = await resp.json();
      Object.keys(results || {}).forEach(function (title) {
        if (results[title]) _tmdbMemo.set(title, results[title]);
      });
    } catch (e) {
      // A failed chunk must not poison the titles in it -- drop them from the
      // in-flight set so the ordinary lazy path can still ask for them.
      chunk.forEach(function (title) { _tmdbPrewarmed.delete(title); });
    }
  }
};

async function _flushTmdbBatch() {
  _tmdbBatchTimer = null;
  if (!_tmdbPending.size) return;
  if (!cineinfoSettings) {
    // Settings still in flight: the queue is deliberately NOT cleared, so the
    // cards keep their place in it -- come back when we know whether there is
    // a key at all.
    loadGeneralSettings().then(() => {
      if (cineinfoSettings && cineinfoSettings.tmdb_api_key && _tmdbPending.size) {
        clearTimeout(_tmdbBatchTimer);
        _tmdbBatchTimer = setTimeout(_flushTmdbBatch, 0);
      }
    });
    return;
  }
  if (!cineinfoSettings.tmdb_api_key) return;
  const batch = [..._tmdbPending.entries()]; // [key, {title, cards}][]
  _tmdbPending.clear();
  // Request TITLES, deduped -- the lookup itself is title-based server-side
  // regardless of how many composite keys share that title (two providers
  // legitimately carrying the same real show should still cost one request
  // and get the one correct answer, just cached under their own keys below
  // rather than a shared one two UNRELATED titleAlike entries could collide
  // on).
  const titles = [...new Set(batch.map(([, entry]) => entry.title))];
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
    batch.forEach(([key, entry]) => {
      const tmdb = results[entry.title];
      if (!tmdb) return;
      // Remember it: the same title/url pair reappears constantly (a row
      // refilled after a filter switch, the same series in "New" and in
      // "Movies"), and asking again for something already answered is the
      // flicker this memo removes.
      _tmdbMemo.set(key, tmdb);
      entry.cards.forEach(card => _applyTmdbToCard(card, tmdb));
    });
  } catch (e) { /* best-effort */ }
}

function _queueTmdbEnrich(card, title) {
  const key = _tmdbCardKey(card, title);
  // Already answered once this page load -- paint it now. This is what makes a
  // row refilled after a filter switch appear complete instead of empty for
  // one batch interval, and it is also the payoff for mfPrewarmTmdb().
  const known = _tmdbMemo.get(key) || _tmdbMemo.get(title);
  if (known) {
    if (cineinfoSettings) _applyTmdbToCard(card, known);
    else loadGeneralSettings().then(() => _applyTmdbToCard(card, known));
    return;
  }
  if (!_tmdbPending.has(key)) _tmdbPending.set(key, { title: title, cards: [] });
  _tmdbPending.get(key).cards.push(card);
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
        // The title stays on the card on purpose. It used to be deleted here,
        // which meant a batch that never completed (settings still loading, a
        // failed request) left the card invisible to _reEnrichPendingCards --
        // the one thing that could have rescued it. dataset.tmdbQueued keeps
        // the queue from asking twice.
        card.dataset.tmdbQueued = "1";
        _queueTmdbEnrich(card, title);
      }
    });
  }, { rootMargin: '50px' })
  : null;

function enrichCardWithTmdb(card, title) {
  // Not "no key" but "we do not know yet": search results render before
  // /api/settings answers, and taking the Crunchyroll-only branch here was
  // permanent -- the card never got a data-tmdb-title, so even the re-enrich
  // pass could not find it again.
  if (!cineinfoSettings) {
    loadGeneralSettings().then(() => enrichCardWithTmdb(card, title));
    return;
  }
  if (!cineinfoSettings.tmdb_api_key) {
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


// ── Modal description: clamped to three lines with a toggle ──────────────
// The paragraph used to be a scroll box (max-height + overflow-y), which put a
// second scroll context inside the modal and swallowed the wheel. Both labels
// of the toggle live in the template, so nothing here needs a translated
// string, and the button only appears when the text is really cut off.
function mfToggleDesc() {
  const desc = document.getElementById("modalDesc");
  const btn = document.getElementById("modalDescMore");
  if (!desc || !btn) return;
  const open = desc.classList.toggle("expanded");
  btn.setAttribute("aria-expanded", open ? "true" : "false");
}

function mfSyncDescClamp() {
  const desc = document.getElementById("modalDesc");
  const btn = document.getElementById("modalDescMore");
  if (!desc || !btn) return;
  desc.classList.remove("expanded");
  btn.setAttribute("aria-expanded", "false");
  // Measure after layout: called straight after textContent is set, the
  // element still reports the previous entry's height.
  requestAnimationFrame(() => {
    btn.style.display = (desc.scrollHeight > desc.clientHeight + 1) ? "" : "none";
  });
}

// Custom paths select
const customPathSelect = document.getElementById("customPathSelect");

// The select sits in a labelled field now (shared_modals.html), so hiding the
// select alone would leave its "Target folder" label behind on every install
// without custom paths. Falls back to the select for any other markup.
function _customPathFieldDisplay(value) {
  if (!customPathSelect) return;
  const field = customPathSelect.closest(".mf-fld") || customPathSelect;
  field.style.display = value;
}

async function loadCustomPaths() {
  if (!customPathSelect) return;
  try {
    const url = currentSeriesUrl ? "?url=" + encodeURIComponent(currentSeriesUrl) : "";
    const resp = await fetch("/api/custom-paths" + url);
    const data = await resp.json();
    const paths = data.paths || [];
    _customPathsCache = paths;
    // Which path a NEW Auto-Sync job opens on (Settings -> Auto-Sync).
    _autosyncDefaultPathId = String(data.autosync_default_path || "");
    // Remove old custom options (keep "Default")
    while (customPathSelect.options.length > 1) customPathSelect.remove(1);
    if (paths.length) {
      // Which path this site defaults to is resolved by the server
      // (db/paths.py's default_custom_path_for_url) -- the same answer every
      // other client gets, instead of a second copy of the CSV match here.
      paths.forEach(function (p) {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.name;
        customPathSelect.appendChild(opt);
      });
      customPathSelect.value = String(data.default_path_id || "");
      _customPathFieldDisplay("");
    } else {
      _customPathFieldDisplay("none");
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
    // {loose_title_key: folder} for every alternative name a folder answers
    // to. This is the only thing that can match a title spelled entirely
    // differently by another provider -- the string comparison below handles
    // punctuation and word order, not "Kyoukaisen-jou no Horizon" vs "Horizon
    // in the Middle of Nowhere". Empty until the background resolver has been
    // through the library (web/library_aliases.py), which is why the string
    // match stays in place rather than being replaced by this.
    downloadedAliases = data.aliases || {};
    // The folder list just changed, so the derived index has to follow it --
    // before any badge or library check can ask about it.
    _buildFolderIndex();
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
      // Second index, punctuation-insensitive. Same defect as the folder scan:
      // the media server's spelling of a title and the provider's spelling
      // differ by a colon or a dash often enough that an exact Set lookup
      // reports "not downloaded" for something plainly on the shelf. Built
      // once here rather than looped per card, so the per-card cost stays a
      // pair of O(1) lookups on libraries with tens of thousands of entries.
      mediascanLooseTitles = new Set(
        (md.titles || []).map(_looseTitleKey).filter(Boolean));
      mediascanActive = true;
      // Re-evaluate any already-rendered badges that have a tmdb data attribute
      _refreshMediascanBadges();
    } else {
      mediascanTmdbIds = new Set();
      mediascanImdbIds = new Set();
      mediascanTitles = new Set();
      mediascanLooseTitles = new Set();
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

// ── "Still loading" vs "failed" ────────────────────────────────────────────
// A browse list whose server-side cache is cold is scraped in the background;
// if that takes longer than the server's 10 s budget the route answers
// HTTP 202 with {"results": [], "pending": true} instead of a 502. That is not
// an error and must not be painted as one: the first visit after a restart
// would otherwise show "Error loading" for every source that happens to be
// slow that minute, and the hourly client-side guard (xLoadedAt) would then
// keep that wrong state on screen for a full hour.
//
// So: leave the skeletons up and ask again shortly. Bounded, because a source
// that never finishes must not poll forever -- after the last attempt whatever
// arrived (usually an empty row) simply stays.
const _PENDING_RETRY_MS = 4000;
const _PENDING_MAX_TRIES = 6;
const _pendingTries = {};

/**
 * @param {Array} dataList parsed JSON bodies of the responses just received
 * @param {string} tag     per-loader counter key
 * @param {Function} retry re-runs the loader (must clear its xLoadedAt guard)
 * @returns {boolean} true when a retry was scheduled -- the caller should
 *   return immediately and keep the skeletons in place.
 */
function retryIfPending(dataList, tag, retry) {
  if (!(dataList || []).some(function (d) { return d && d.pending; })) {
    _pendingTries[tag] = 0;
    return false;
  }
  const tries = (_pendingTries[tag] = (_pendingTries[tag] || 0) + 1);
  if (tries > _PENDING_MAX_TRIES) {
    _pendingTries[tag] = 0;
    return false;
  }
  setTimeout(retry, _PENDING_RETRY_MS);
  return true;
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
    if (retryIfPending([newData, popData], "sto", function () { stoLoadedAt = 0; loadStoBrowse(); })) return;

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
    if (retryIfPending([data], "filmpalast", function () { fpLoadedAt = 0; loadFilmPalastBrowse(); })) return;

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
    if (retryIfPending(data, "megakino", function () { megakinoLoadedAt = 0; loadMegakinoBrowse(); })) return;
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

let filmoLoadedAt = 0;
async function loadFilmoBrowse() {
  if (filmoLoadedAt && Date.now() - filmoLoadedAt < 3600000) return;
  filmoLoadedAt = Date.now();
  const grids = [filmoNewMoviesGrid, filmoPopularMoviesGrid];
  grids.forEach(g => { if (g) renderSkeletons(g); });
  const errHtml = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
  try {
    const [nmResp, pmResp] = await Promise.all([
      fetch("/api/filmo/new-movies"),
      fetch("/api/filmo/popular-movies"),
    ]);
    await Promise.all([loadDownloadedFolders(), loadAutoSyncJobs(), loadCineinfoSettings(), loadGeneralSettings()]);
    const data = await Promise.all([nmResp.json(), pmResp.json()]);
    if (retryIfPending(data, "filmo", function () { filmoLoadedAt = 0; loadFilmoBrowse(); })) return;
    const targets = [
      [filmoNewMoviesGrid, data[0]],
      [filmoPopularMoviesGrid, data[1]],
    ];
    targets.forEach(([grid, d]) => {
      if (!grid) return;
      if (d && d.results) renderBrowseCards(grid, d.results);
      else grid.innerHTML = errHtml;
    });
  } catch (e) {
    filmoLoadedAt = 0;
    grids.forEach(g => { if (g) g.innerHTML = errHtml; });
  }
}

let nineanimeLoadedAt = 0;
async function loadNineanimeBrowse() {
  if (nineanimeLoadedAt && Date.now() - nineanimeLoadedAt < 3600000) return;
  nineanimeLoadedAt = Date.now();
  if (nineanimeNewGrid) renderSkeletons(nineanimeNewGrid);
  if (nineanimePopularGrid) renderSkeletons(nineanimePopularGrid);
  const errHtml = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
  try {
    const [newResp, popResp] = await Promise.all([
      fetch("/api/nineanime/new"),
      fetch("/api/nineanime/popular"),
    ]);
    await Promise.all([loadDownloadedFolders(), loadAutoSyncJobs(), loadCineinfoSettings(), loadGeneralSettings()]);
    const newData = await newResp.json();
    const popData = await popResp.json();
    if (retryIfPending([newData, popData], "nineanime", function () { nineanimeLoadedAt = 0; loadNineanimeBrowse(); })) return;
    // Unlike hanime, 9anime's titles are mainstream anime that TMDB does
    // know about -- no skipTmdb here, so the same CineInfo enrichment
    // AniWorld/SerienStream cards get applies here too.
    if (nineanimeNewGrid) (newData.results ? renderBrowseCards(nineanimeNewGrid, newData.results) : (nineanimeNewGrid.innerHTML = errHtml));
    if (nineanimePopularGrid) (popData.results ? renderBrowseCards(nineanimePopularGrid, popData.results) : (nineanimePopularGrid.innerHTML = errHtml));
  } catch (e) {
    nineanimeLoadedAt = 0;
    if (nineanimeNewGrid) nineanimeNewGrid.innerHTML = errHtml;
    if (nineanimePopularGrid) nineanimePopularGrid.innerHTML = errHtml;
  }
}

let aniwavesLoadedAt = 0;
async function loadAniwavesBrowse() {
  if (aniwavesLoadedAt && Date.now() - aniwavesLoadedAt < 3600000) return;
  aniwavesLoadedAt = Date.now();
  if (aniwavesNewGrid) renderSkeletons(aniwavesNewGrid);
  if (aniwavesPopularGrid) renderSkeletons(aniwavesPopularGrid);
  const errHtml = `<div class="queue-empty" style="padding: 20px;">${t('Fehler beim Laden', 'Error loading')}</div>`;
  try {
    const [newResp, popResp] = await Promise.all([
      fetch("/api/aniwaves/new"),
      fetch("/api/aniwaves/popular"),
    ]);
    await Promise.all([loadDownloadedFolders(), loadAutoSyncJobs(), loadCineinfoSettings(), loadGeneralSettings()]);
    const newData = await newResp.json();
    const popData = await popResp.json();
    if (retryIfPending([newData, popData], "aniwaves", function () { aniwavesLoadedAt = 0; loadAniwavesBrowse(); })) return;
    // Same reasoning as loadNineanimeBrowse(): Aniwaves' catalogue is
    // mainstream anime TMDB knows about, so no skipTmdb here either.
    if (aniwavesNewGrid) (newData.results ? renderBrowseCards(aniwavesNewGrid, newData.results) : (aniwavesNewGrid.innerHTML = errHtml));
    if (aniwavesPopularGrid) (popData.results ? renderBrowseCards(aniwavesPopularGrid, popData.results) : (aniwavesPopularGrid.innerHTML = errHtml));
  } catch (e) {
    aniwavesLoadedAt = 0;
    if (aniwavesNewGrid) aniwavesNewGrid.innerHTML = errHtml;
    if (aniwavesPopularGrid) aniwavesPopularGrid.innerHTML = errHtml;
  }
}

// "Zensiert"/"Unzensiert" are content-type filters applied to individual
// hanime items (both the New/Trending lists and the general title-search
// results mix censored and uncensored entries) — not separate sections, so
// this filters an item array rather than hiding a whole grid/section. Shared
// by loadHanimeBrowse() (home page New/Trending) and buildSourceSection()
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
    if (retryIfPending([newData, trendData], "hanime", function () { hanimeLoadedAt = 0; loadHanimeBrowse(); })) return;
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

// ── Getting back out of a search ───────────────────────────────────────────
//
// Searching replaced the home page with the results and left no way back
// except reloading: the browse block and the feed were hidden and nothing
// ever un-hid them. Three ways out now, because people reach for different
// ones -- a button above the results, Escape, and the browser's own Back
// (which is the one most people try first, and which used to leave the page
// entirely).
let _searchScrollY = 0;

/** The bar above the results: where you are, and how to leave.
    Rendered into #searchHead, which lives OUTSIDE #results -- the render
    functions replace #results.innerHTML wholesale, so a header inside it
    disappeared the moment the first answer arrived. */
function renderSearchHeader(keyword) {
  const head = document.getElementById("searchHead");
  if (!head) return;
  head.innerHTML =
    '<button type="button" class="search-back" onclick="exitSearch()">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>' +
    "<span>" + escapeHtml(_homeText("search_back", "Zurück", "Back")) + "</span></button>" +
    '<span class="search-head-term">' +
    escapeHtml(_homeText("search_results_for", "Ergebnisse für", "Results for")) +
    " “" + escapeHtml(keyword) + "”</span>";
  head.hidden = false;
}

function enterSearchView(keyword) {
  _searchScrollY = window.scrollY || 0;
  document.body.classList.add("is-searching");
  // One entry, not one per keystroke-triggered search: replaceState while
  // already in a search, pushState only on the way in. Otherwise Back walks
  // through every search of the session before reaching the page.
  try {
    const url = "#search=" + encodeURIComponent(keyword);
    if (history.state && history.state.mfSearch) history.replaceState({ mfSearch: keyword }, "", url);
    else history.pushState({ mfSearch: keyword }, "", url);
  } catch (e) { /* file:// or a locked-down browser */ }
}

/** Put the home page back. Safe to call when no search is showing. */
function exitSearch(fromHistory) {
  if (!document.body.classList.contains("is-searching")) return;
  document.body.classList.remove("is-searching");
  if (resultsDiv) resultsDiv.innerHTML = "";
  const head = document.getElementById("searchHead");
  if (head) { head.hidden = true; head.innerHTML = ""; }
  if (searchSpinner) searchSpinner.style.display = "none";
  if (browseDiv) browseDiv.style.display = "";
  const feed = document.getElementById("homeFeed");
  if (feed) feed.style.display = "";
  // The rows are still rendered underneath -- this is not a reload, so the
  // scroll position people left is the one they get back.
  window.scrollTo(0, _searchScrollY);
  if (!fromHistory) {
    try { history.pushState({}, "", window.location.pathname); } catch (e) { /* ignore */ }
  }
}
window.exitSearch = exitSearch;

window.addEventListener("popstate", function () {
  // Back out of a search rather than off the page.
  exitSearch(true);
});

document.addEventListener("keydown", function (ev) {
  if (ev.key !== "Escape") return;
  if (!document.body.classList.contains("is-searching")) return;
  // Not while a modal or the suggestion list is open -- Escape belongs to
  // whatever is on top, and those close themselves first.
  if (document.querySelector(".directlink-overlay[style*='flex'], .queue-overlay[style*='flex']")) return;
  const suggest = document.getElementById("searchSuggest");
  if (suggest && !suggest.hidden) return;
  exitSearch();
});

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
  if (enabled.filmo !== "0") loadFilmoBrowse();
  if (enabled.nineanime === "1") loadNineanimeBrowse();
  if (enabled.aniwaves === "1") loadAniwavesBrowse();
  if (enabled.hanime === "1") loadHanimeBrowse();

  // Awaited: renderSourceChips() now waits for the source catalogue, and
  // applyUptimeStatus() marks chips that do not exist yet if it runs first.
  await renderSourceChips(sources);
  applyUptimeStatus();
}

// ── The source catalogue ───────────────────────────────────────────────────
// Which content sources exist is a *runtime* question, not a constant: a
// third-party module can register one (providers.register_provider +
// search.register_search_source) and it must then be searched, chipped and
// listed like any built-in. So the list comes from GET /api/search/sources
// and every consumer below derives from it, instead of the five ids each of
// them used to hardcode -- which is exactly why a module source used to be
// reachable by pasted URL but never actually asked a keyword.
//
// The hardcoded list survives only as _FALLBACK_SOURCES: if that one request
// fails, searching the built-ins is a far better outcome than searching
// nothing. An adult source is omitted server-side for an age-limited session,
// so anything in here is a source this session may see.
const _FALLBACK_SOURCES = [
  { id: "aniworld",   label: "AniWorld",     adult: false, thirdparty: false, css_class: "browse-provider-aniworld" },
  { id: "sto",        label: "SerienStream", adult: false, thirdparty: false, css_class: "browse-provider-sto" },
  { id: "filmpalast", label: "FilmPalast",   adult: false, thirdparty: false, css_class: "browse-provider-filmpalast" },
  { id: "megakino",   label: "MegaKino",     adult: false, thirdparty: false, css_class: "browse-provider-megakino" },
  { id: "filmo",      label: "filmo.to",     adult: false, thirdparty: false, css_class: "browse-provider-filmo" },
  { id: "nineanime",  label: "9anime",       adult: false, thirdparty: false, english_only: true, css_class: "browse-provider-nineanime" },
  { id: "aniwaves",   label: "Aniwaves",     adult: false, thirdparty: false, english_only: true, css_class: "browse-provider-aniwaves" },
  { id: "hanime",     label: "hanime 18+",   adult: true,  thirdparty: false, css_class: "browse-provider-hanime" },
];

let _searchSources = null;         // last resolved catalogue
let _searchSourcesPromise = null;  // in-flight request, so N callers = 1 fetch

/**
 * The source catalogue, fetched once per page load.
 * @param {boolean} [force] refetch instead of using the cached answer -- used
 *   after a module was installed/removed, where the list genuinely changed.
 * @returns {Promise<Array>} [{id,label,adult,thirdparty,enabled,css_class}]
 */
function loadSearchSources(force) {
  if (force) { _searchSources = null; _searchSourcesPromise = null; }
  if (_searchSources) return Promise.resolve(_searchSources);
  if (_searchSourcesPromise) return _searchSourcesPromise;
  _searchSourcesPromise = fetch("/api/search/sources")
    .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error(r.status)); })
    .then(function (data) {
      const list = (data && Array.isArray(data.sources) && data.sources.length)
        ? data.sources : _FALLBACK_SOURCES.slice();
      _searchSources = _sortSourcesByUserOrder(list, (data || {}).order);
      return _searchSources;
    })
    .catch(function () {
      _searchSourcesPromise = null;   // transient failure: allow a retry
      return _FALLBACK_SOURCES.slice();
    });
  return _searchSourcesPromise;
}
window.loadSearchSources = loadSearchSources;

/** Sort a catalogue by the user's saved order; unknown/new ids keep their
 *  position at the end rather than disappearing. */
function _sortSourcesByUserOrder(list, order) {
  const ord = Array.isArray(order)
    ? order
    : String(order || "").split(",").map(function (x) { return x.trim().toLowerCase(); }).filter(Boolean);
  if (!ord.length) return list;
  return list.slice().sort(function (a, b) {
    const ia = ord.indexOf(a.id), ib = ord.indexOf(b.id);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });
}

/** Is this source switched on? `enabled` from /api/settings wins (it is the
 *  live value the Sources tab writes), the catalogue's own flag is the
 *  fallback, and an adult source is opt-in either way. */
function _sourceIsOn(src, enabledMap) {
  const raw = (enabledMap || {})[src.id];
  if (raw !== undefined) return src.adult ? raw === "1" : raw !== "0";
  return !!src.enabled;
}

// Explicit exports for the pages that ask their own questions of the source
// catalogue -- static/seerr.js builds the Seerr search fan-out from these.
// A top-level function declaration already lands on window in a classic
// script, so this changes nothing at runtime; it is here so the three of them
// read as a public trio and nobody "cleans up" one into a const and quietly
// breaks another file.
window.loadGeneralSettings = loadGeneralSettings;
window._sortSourcesByUserOrder = _sortSourcesByUserOrder;
window._sourceIsOn = _sourceIsOn;

// ── "Who is being asked" chips under the search field ───────────────────────
// A search fans out to every *enabled* source, and the most common surprise is
// finding nothing because a source is switched off. So the chips state the
// answer instead of hiding it: enabled = normal, off = greyed with a label,
// down = red (set later by applyUptimeStatus, which is the only place that
// knows). Order follows the user's own source order.
async function renderSourceChips(sources) {
  const wrap = document.getElementById("homeSourceChips");
  if (!wrap) return;
  const enabled = (sources && sources.enabled) || {};
  let list = await loadSearchSources();
  // The Sources tab order is the more recent one when both are present.
  if (sources && sources.order) list = _sortSourcesByUserOrder(list, sources.order);

  wrap.innerHTML = list.map(function (src) {
    const on = _sourceIsOn(src, enabled);
    // "EN" marks a source with an English-only catalogue (see
    // web/source_policy.py's ENGLISH_ONLY_SOURCE_IDS). Informational only --
    // it is what explains why the source has no German audio and why it ships
    // switched off; it gates nothing, unlike the 18+ source.
    const enBadge = src.english_only
      ? '<span class="source-badge-en" title="' + escapeHtml(t("Nur englischsprachige Quelle", "English-only source")) + '">EN</span>'
      : "";
    return '<span class="home-chip' + (on ? "" : " is-off") + '" data-source="' + escapeHtml(src.id) + '">' +
      '<span class="home-chip-dot" style="background:' +
        (on ? "var(--success)" : "var(--text-muted)") + '"></span>' +
      escapeHtml(src.label) + enBadge +
      (on ? "" : " · " + t("aus", "off")) +
      "</span>";
  }).join("");
}

// Called from applyUptimeStatus(): a source that is enabled but unreachable is
// neither "on" nor "off" -- it is temporary and not the user's doing.
function markSourceChipsDown(ids) {
  // The new home page renders its own chip row (see home_feed.js) and takes
  // the same list from here, so /api/uptime/status is still polled once.
  if (typeof window.mfFeedMarkDown === "function") window.mfFeedMarkDown(ids);
  const wrap = document.getElementById("homeSourceChips");
  if (!wrap) return;
  ids.forEach(function (id) {
    const chip = wrap.querySelector('.home-chip[data-source="' + id + '"]');
    if (!chip || chip.classList.contains("is-off")) return;
    chip.classList.add("is-down");
    const dot = chip.querySelector(".home-chip-dot");
    if (dot) dot.style.background = "var(--error)";
    if (chip.textContent.indexOf("·") === -1) {
      chip.appendChild(document.createTextNode(" · " + t("offline", "offline")));
    }
  });
}

// ── One strip about the download that is running ────────────────────────────
// Fed by the queue poll that runs on every page anyway (queue.js), so this
// costs no extra request. Only visible while something is actually running --
// an always-present empty strip would just be furniture.
// The live status band under the search field.
//
// This used to say only "<title> — 64 %", which meant every other question a
// running instance raises ("did anything fail?", "is the encoder busy?", "am
// I about to run out of disk?") still cost a trip to the queue modal. It now
// carries all of them in one line and, as before, renders nothing at all when
// nothing is happening -- which is most of the time, and the reason it can
// afford to be this dense when it does appear.
window.renderHomeRunStrip = function (items, progress, paused) {
  const wrap = document.getElementById("homeRunStrip");
  if (!wrap) return;
  const list = Array.isArray(items) ? items : [];
  const running = list.find(function (i) { return i.status === "running"; });
  const failed = list.filter(function (i) {
    return i.status === "error" || i.status === "failed";
  });
  // Idle *and* nothing to report -> gone. A band that is always there is
  // furniture; one that appears only when it has news is a status band.
  if (!running && !failed.length) { wrap.style.display = "none"; wrap.innerHTML = ""; return; }

  const waiting = list.filter(function (i) { return i.status === "queued"; }).length;
  const p = progress || {};
  const pct = Math.max(0, Math.min(100, parseFloat(p.percent) || 0));
  const ep = running && running.current_episode
    ? " · " + _homeText("episode", "Folge", "Episode") + " " +
      escapeHtml(String(running.current_episode))
    : "";
  const facts = [];
  if (p.active && pct) facts.push(Math.round(pct) + " %");
  if (p.bandwidth) facts.push(escapeHtml(String(p.bandwidth)));
  if (p.eta) facts.push("ETA " + escapeHtml(String(p.eta)));
  if (p.phase && p.phase !== "download") facts.push(escapeHtml(String(p.phase)));
  if (waiting) facts.push(waiting + " " + t("in der Warteschlange", "in the queue"));

  let html = "";
  if (running) {
    html +=
      '<span class="home-run-text">' +
        '<span class="home-run-dot" aria-hidden="true"></span>' +
        '<span class="home-run-title">' + escapeHtml(running.title || "") + ep + "</span>" +
        '<span class="home-run-bar' + (paused ? " is-paused" : "") + '">' +
          '<span class="home-run-fill" style="width:' + pct + '%"></span></span>' +
        '<span class="home-run-sub">' +
          (facts.join(" · ") || _homeText("status_downloading", "Lädt…", "Downloading…")) +
        "</span>" +
      "</span>";
  }
  if (failed.length) {
    // Click-to-expand rather than the error text inline: a yt-dlp message is
    // three lines long and would push everything else off the band.
    html += '<button type="button" class="home-run-err" onclick="openQueueModal()" title="' +
      escapeHtml(failed.map(function (i) { return i.title || ""; }).join(", ")) + '">' +
      escapeHtml(_homeText("status_last_error", "Letzter Fehler", "Last error")) +
      " · " + failed.length + "</button>";
  }
  // Filled asynchronously by refreshHomeRunFacts() -- the queue poll that
  // feeds this function knows nothing about the encoder or the disk, and
  // making it wait for two more endpoints would slow down the part that
  // actually ticks every second.
  html += '<span class="home-run-facts" id="homeRunFacts"></span>';
  html += '<button type="button" class="btn btn-secondary btn-sm home-run-btn" onclick="openQueueModal()">' +
    escapeHtml(_homeText("status_open_queue", "Warteschlange", "Queue")) + "</button>";

  wrap.innerHTML = html;
  wrap.style.display = "flex";
  refreshHomeRunFacts();
};

// Encoder/upscaler load and free space, refreshed at most once a minute. Both
// come from the home-panel endpoints that already exist, so this adds no new
// server-side surface -- and both are allowed to be absent: /api/home-panel/
// storage is admin-only, and a normal account simply sees one fact fewer
// rather than an error.
let _homeFactsAt = 0;
let _homeFacts = "";
async function refreshHomeRunFacts() {
  const slot = document.getElementById("homeRunFacts");
  if (!slot) return;
  if (_homeFacts && Date.now() - _homeFactsAt < 60000) { slot.innerHTML = _homeFacts; return; }
  const parts = [];
  // The panels answer with {stats:[{label_key, value}]}, so the figures are
  // looked up by their key rather than by position -- the order of that list
  // is a presentation decision on the server and may change.
  function statValue(payload, key) {
    const stats = (payload && payload.stats) || [];
    for (let i = 0; i < stats.length; i++) {
      if (stats[i].label_key === key) return stats[i].value;
    }
    return "";
  }
  try {
    const sys = await (await fetch("/api/home-panel/system")).json();
    const busy = parseInt(statValue(sys, "hp_encoding"), 10) || 0;
    const up = parseInt(statValue(sys, "hp_upscaling"), 10) || 0;
    if (busy) parts.push(escapeHtml(_homeText("status_encoding", "{} im Encoding", "{} encoding")
      .replace("{}", String(busy))));
    if (up) parts.push(escapeHtml(_homeText("status_upscaling", "{} im Upscaling", "{} upscaling")
      .replace("{}", String(up))));
  } catch (e) { /* admin-only panel: one fact fewer, not an error */ }
  try {
    const st = await (await fetch("/api/home-panel/storage")).json();
    const fullest = statValue(st, "hp_fullest");
    if (fullest) {
      parts.push(escapeHtml(_homeText("status_fullest", "{} voll", "{} full")
        .replace("{}", String(fullest))));
    }
  } catch (e) { /* admin-only */ }
  _homeFacts = parts.join(" · ");
  _homeFactsAt = Date.now();
  slot.innerHTML = _homeFacts;
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
    // A kids account is refused this endpoint (403, see app.py's
    // _kids_blocked) — stop rather than parse an error body as a status.
    if (!resp.ok) return;
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

  markSourceChipsDown(offline.map(function (sc) { return sc.id; }));

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
  const validProv = ["aniworld", "sto", "filmpalast", "megakino", "filmo", "nineanime", "aniwaves", "hanime"];
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

// Punctuation-insensitive comparison key. The card title and the folder name
// do not come from the same place -- e.g. for hanime the card carries the
// catalogue's "name" while the folder is built from the video page's JSON-LD
// title -- so the two can differ by a "!", a dash or a colon and the strict
// comparison below then misses a title that IS on disk. Reducing both sides to
// letters and digits catches those without loosening what counts as a match:
// the whole (episode-suffix-stripped) title still has to be there.
function _looseTitleKey(s) {
  return normalizeQuotes(unesc(s || ""))
    .toLowerCase()
    .replace(/\s*\(.*$/, "")        // drop "(2013)" and everything after it
    .replace(/[^a-z0-9]+/g, "")      // punctuation, spaces, quotes -- all out
    .trim();
}

function isDownloaded(title) {
  return !!downloadedFolderFor(title);
}

/** Which folder holds *title*, or "" if none does.
 *
 *  Three passes, cheapest and most certain first:
 *   1. the alias index -- an exact answer, because the server resolved this
 *      folder against TMDB and recorded every name the show is known by. This
 *      is the only pass that can match across languages.
 *   2. a strict folder-name prefix match.
 *   3. a punctuation-insensitive, bidirectional match (_looseFolderHolds).
 *
 *  Passes 2 and 3 remain because pass 1 is only as complete as the background
 *  resolver has got, and is empty entirely on an instance with no TMDB key.
 */
function downloadedFolderFor(title) {
  if (!title) return "";
  // 1. Alias index. Checked before the folder list is even consulted: it costs
  // one lookup and, unlike the string passes, it cannot be wrong -- the server
  // only records aliases for a confident TMDB match.
  const aliasKey = _looseTitleKey(title);
  if (aliasKey && downloadedAliases[aliasKey]) return downloadedAliases[aliasKey];

  // Folder-based check (used when mediascan is inactive)
  if (!downloadedFolders.length) return "";
  // Built lazily so a caller that runs before loadDownloadedFolders() finished
  // (or after something assigned downloadedFolders directly) still gets an
  // index rather than a wrong answer.
  if (_dlFoldersLower.length !== downloadedFolders.length) _buildFolderIndex();
  const clean = normalizeQuotes(
    unesc(title)
      .replace(/\s*\(.*$/, "")
      .replace(/[<>:"/\\|?*]/g, "") // : characters forbidden in folder names
      .trim()
      .toLowerCase(),
  );
  // 2. Strict folder-name prefix. Against the PRE-REDUCED list: this used to
  // lower-case and normalise every folder again for every title it was asked
  // about (see _dlFoldersLower).
  for (let i = 0; i < _dlFoldersLower.length; i++) {
    if (_dlFoldersLower[i].startsWith(clean)) return downloadedFolders[i];
  }

  // 3. Punctuation-insensitive (see _looseTitleKey). Only ever adds matches the
  // strict pass above missed.
  if (!aliasKey) return "";
  for (let i = 0; i < _dlFoldersLoose.length; i++) {
    if (_looseFolderHolds(_dlFoldersLoose[i], aliasKey)) return downloadedFolders[i];
  }
  return "";
}

/** Reduce the folder list to the two forms the passes above compare against.
 *
 *  One pass over the folders instead of one pass per title. Called whenever
 *  the library index changes; the length check in downloadedFolderFor() is the
 *  safety net for anything that replaces `downloadedFolders` without saying so.
 */
function _buildFolderIndex() {
  _dlFoldersLower = downloadedFolders.map((f) => normalizeQuotes(String(f).toLowerCase()));
  _dlFoldersLoose = downloadedFolders.map(_looseTitleKey);
}

// Shortest folder key allowed to win a REVERSE match -- see _looseFolderHolds.
const LOOSE_MIN_REVERSE = 10;

/** Does a folder whose loose key is *folderKey* hold the title *titleKey*?
 *
 *  Both directions, because the folder on disk is named after whichever
 *  provider downloaded it FIRST and every other provider then compares its own
 *  spelling against that name. That asymmetry is why the badge appeared when
 *  you came from AniWorld and not when you came from a site that spells the
 *  same show "<title>: <romaji subtitle>".
 *
 *  The reverse direction needs a length floor: without it a "Naruto" folder
 *  would claim "Naruto Shippuden" and "One Piece" would claim "One Piece Film
 *  Red". A wrong "already downloaded" is the more expensive mistake -- it stops
 *  a download the user asked for -- so short keys only match forwards.
 *
 *  The Python twin is titles_match() in models/common/common.py; the card badge
 *  and the modal's episode ticks answer the same question and must not
 *  disagree. */
function _looseFolderHolds(folderKey, titleKey) {
  if (!folderKey || !titleKey) return false;
  if (folderKey.startsWith(titleKey)) return true;
  return folderKey.length >= LOOSE_MIN_REVERSE && titleKey.startsWith(folderKey);
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
  if (norm && mediascanTitles.has(norm)) return true;
  // ...except that _normalizeForMediascan keeps spaces and underscores, so
  // "Kaguya-sama: Love is War" and "Kaguya sama Love is War" still miss each
  // other. The loose index (letters and digits only) closes that gap without
  // giving up the O(1) lookup. Still exact, not prefix: a media server holds
  // one entry per show, so there is no "Season 2 folder" case to catch here.
  const loose = _looseTitleKey(title);
  return loose ? mediascanLooseTitles.has(loose) : false;
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
  // hanime cards carry a rotated corner flag (.hanime-pill) across the
  // top-left corner whose tip reaches into the top-right area of a 140px
  // card. Start the badge stack below it there, so "Downloaded"/"Sync" no
  // longer land on top of the flag.
  const base = card.querySelector(".hanime-pill") ? 32 : 7;
  return base + card.querySelectorAll(".card-top-badge").length * 27;
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

/* ── The two badges, as questions ──────────────────────────────────────────
   The same answers addDownloadedBadgeMulti() and addSyncBadge() act on, but
   returned instead of attached, so a caller can DECIDE something with them
   rather than only decorate a card that already exists.

   Exported because the home feed's status filter ("hide what I already have")
   has to agree with the badge on the card, card for card. Re-implementing
   either check there would be a second opinion that drifts the first time the
   alias index, the mediascan path or the loose title key changes -- and all
   three have changed before. On the registry for the same reason a module may
   render browse cards at all: a module row is filtered by the same dropdown.

   Both take a card ITEM (the {title, url, ...} shape /api/home-feed and the
   browse endpoints return), not a DOM node. */
function mfCardInLibrary(item) {
  if (!item) return false;
  // hanime cards carry the episode title while the folder is named after the
  // franchise -- both candidates, exactly like addDownloadedBadgeMulti().
  const candidates = [item.series_title, item.title].filter(Boolean);
  if (!candidates.length) return false;
  if (mediascanActive) {
    if (candidates.some((title) => _isDownloadedByTitle(title))) return true;
    // The badge gets a second chance from the TMDB id once CineInfo has
    // enriched the card (see _applyTmdbToCard); when the id is already on the
    // item, take it here too rather than answering "no" to a card that is
    // about to grow the badge.
    return item.tmdb && item.tmdb.tmdb_id
      ? _isDownloadedByTmdb(item.tmdb.tmdb_id) : false;
  }
  return candidates.some((title) => isDownloaded(title));
}

function mfCardOnAutoSync(item) {
  if (!item) return false;
  const url = (item.url || "").replace(/\/+$/, "").toLowerCase();
  if (url && autoSyncUrlMap[url]) return true;
  // A job created from the other site of a merged card points at the other
  // url, so the title is the fallback -- same pairing addSyncBadgeForTmdb()
  // uses when a card has no provider url at all.
  const candidates = [item.series_title, item.title]
    .filter(Boolean).map(_normalizeForMediascan).filter(Boolean);
  if (!candidates.length) return false;
  return Object.values(autoSyncUrlMap).some((job) =>
    candidates.indexOf(_normalizeForMediascan(job.title || "")) !== -1);
}

window.mfCardInLibrary = mfCardInLibrary;
window.mfCardOnAutoSync = mfCardOnAutoSync;

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
  // Every branch below reads cineinfoSettings; called before /api/settings
  // answered, the card silently ended up with no pills and no hover drawer and
  // nothing ever retried it. Re-run once the settings are in.
  if (!cineinfoSettings) {
    loadGeneralSettings().then(() => _applyTmdbToCard(card, d));
    return;
  }
  // Keep the payload on the card so a settings change (or a later re-enrich
  // pass) can redraw from memory instead of asking TMDB again.
  if (d) card._mfTmdb = d;
  if (!d || !d.found) {
    // No TMDB data at all — the chain still runs (Crunchyroll, Fernsehserien,
    // module pills can all still know this title).
    _cardProviderChain(card, d);
    return;
  }
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

  if (!cineinfoSettings || !cineinfoSettings.tmdb_api_key) {
    _cardProviderChain(card, d);
    return;
  }
  if (cineinfoSettings.show_providers === '0' &&
      cineinfoSettings.show_fsk === '0' &&
      cineinfoSettings.show_hover_rating !== '1' &&
      cineinfoSettings.show_hover_genres !== '1' &&
      cineinfoSettings.show_hover_fsk !== '1') return;

  const meta = _ensureCardMeta(card);
  // Clear the children — layout (flex/wrap/gap/margin) lives in the
  // .browse-tmdb-meta CSS rule now, not as an inline style set from JS, so
  // there's nothing stray left behind on an empty container to reset.
  // Except .browse-src-pill: home_feed.js puts its "found via MegaKino +1"
  // badge in this same row (see addSourcePills) so it lines up with the
  // streaming-service pill instead of fighting a fixed page corner for
  // space — and re-enrichment (settings changed, a later fallback pill)
  // running through here must not wipe it out from under that unrelated
  // code path just because it happens to share a container.
  if (meta) {
    Array.from(meta.children).forEach((el) => {
      if (!el.classList.contains('browse-src-pill')) el.remove();
    });
  }
  // Pills are rendered by the chain (which honours the configured CineInfo
  // provider order, TMDB included) — run it only AFTER the container is
  // cleared, or its pills would be wiped again by the line above.
  _cardProviderChain(card, d, meta);

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

// Repopulating a <select> makes the browser fall back to its FIRST option,
// which is a property of the site's language order, not of what the user
// asked for. Sites whose list does not happen to start with the configured
// default (filmo.to lists English first) therefore opened the modal on the
// wrong language. Apply the instance default (Settings -> Auto-Sync ->
// Default Language, `sync_language`) whenever the previous selection is not
// among the new options -- a manual choice that survives the rebuild is kept.
function rebuildLanguageSelect(foundLangs = null) {
  _rebuildLanguageSelect(foundLangs);
  // The multi-language checkbox list is mirrored from the options this just
  // wrote, so it has to follow every rebuild -- including the several early
  // returns inside, which is why this is a wrapper and not a trailing call.
  syncLangMultiFromSelect();
}

function _rebuildLanguageSelect(foundLangs = null) {
  const prev = languageSelect ? languageSelect.value : "";
  rebuildLanguageSelectOptions(foundLangs);
  if (!languageSelect || !languageSelect.options.length) return;
  const has = (v) => Array.from(languageSelect.options).some((o) => o.value === v);
  if (has(prev)) return;
  if (defaultSyncLanguage && has(defaultSyncLanguage)) {
    languageSelect.value = defaultSyncLanguage;
    syncLangAvailPills();
  }
}

function rebuildLanguageSelectOptions(foundLangs = null) {
  const url = currentSeriesUrl || "";
  // FilmPalast and MegaKino movies are both German-dub-only (see
  // seerrUpdateLangDropdown() in seerr.js, which treats these two sites the
  // same way -- kept in sync here so the Seerr modal doesn't regress now
  // that it uses this shared implementation).
  const isFilmPalast = url.includes("filmpalast.to") || url.includes("megakino");
  languageSelect.innerHTML = "";

  if (isFilmPalast) {
    // FilmPalast/MegaKino movies are always German-dubbed
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
  const isAniworld = url.includes("aniworld.to");

  // Any OTHER site: its languages are whatever the backend reported for THIS
  // title, not AniWorld's fixed set. Falling through to ANIWORLD_LANGS is what
  // made every source added after AniWorld/s.to look broken -- the dropdown
  // offered "German Dub" for an English-only 9anime episode (and never offered
  // filmo.to's own language labels at all), so the provider lookup, which is
  // keyed by exactly these strings, found nothing and the modal said
  // "No source available" for a title that plays fine.
  //
  // foundLangs comes from /api/episodes; availableProviders from
  // /api/providers. Either is a real answer about this title; ANIWORLD_LANGS
  // is a guess that is only right for one site.
  if (!isSto && !isAniworld) {
    const dynamic = [];
    const add = (l) => { if (l && !dynamic.includes(l)) dynamic.push(l); };
    if (foundLangs && foundLangs.size) foundLangs.forEach(add);
    if (!dynamic.length && availableProviders) Object.keys(availableProviders).forEach(add);
    if (dynamic.length) {
      dynamic.forEach((l) => {
        const opt = document.createElement("option");
        opt.value = l;
        opt.textContent = l;
        languageSelect.appendChild(opt);
      });
      syncLangAvailPills();
      return;
    }
    // Nothing known yet (the first call happens before either fetch has
    // returned): leave the dropdown empty rather than filling it with another
    // site's languages. The later call, with foundLangs, fills it in.
    //
    // The `return` is the point of that sentence and was missing: without it
    // execution fell through to ANIWORLD_LANGS below and a module's modal was
    // handed AniWorld's five language labels. The provider lookup is keyed by
    // exactly these strings, so every one of them then resolved to "no source
    // available" -- an empty Hoster dropdown under a full-looking Language one,
    // which is the shape the module bug report describes.
    syncLangAvailPills();
    return;
  }

  const langs = isSto ? window.STO_LANGS || {} : window.ANIWORLD_LANGS || {};

  if (langSeparationEnabled) {
    const opt = document.createElement("option");
    opt.value = "All Languages";
        opt.textContent = t("Alle Sprachen", "All Languages");
    languageSelect.appendChild(opt);
  }

  const siteLangs = new Set(Object.values(langs));
  for (const [key, label] of Object.entries(langs)) {
    if (foundLangs && !foundLangs.has(label)) {
      continue;
    }
    const opt = document.createElement("option");
    opt.value = label;
    opt.textContent = label;
    languageSelect.appendChild(opt);
  }

  // Fallback groups (settings → Downloads). Offered only with language
  // separation on — without per-language folders a group cannot tell which
  // language an existing file is in, so the backend rejects it too. A group
  // shows up as long as this series can serve at least one of its languages:
  // the queue worker picks the first available one per episode, so partial
  // overlap is still exactly what the user wants.
  const usable = !langSeparationEnabled ? [] : (languageGroups || []).filter((g) =>
    (g.languages || []).some((l) => siteLangs.has(l) && (!foundLangs || foundLangs.has(l))),
  );
  if (usable.length) {
    const optgroup = document.createElement("optgroup");
    optgroup.label = t("Sprachgruppen", "Language groups");
    usable.forEach((g) => {
      const opt = document.createElement("option");
      opt.value = "group:" + g.id;
      opt.textContent = g.name;
      opt.title = (g.languages || []).join(" → ");
      optgroup.appendChild(opt);
    });
    languageSelect.appendChild(optgroup);
  }
  // Rebuilding the options can land on a different value than the one the
  // pills were rendered against (checkLangSeparation() calls this after the
  // banner already exists). Without this the select and its pill twin show
  // two different answers until the next manual change.
  syncLangAvailPills();
}

// The ordered languages behind a dropdown value: one entry for a plain
// language, the whole chain for a "group:<id>" selection.
function languageChainFor(value) {
  if (!String(value || "").startsWith("group:")) return value ? [value] : [];
  const group = (languageGroups || []).find((g) => "group:" + g.id === value);
  return group ? (group.languages || []).slice() : [];
}

// ── Multi-language download ──────────────────────────────────────────────────
// One file, several audio tracks. The <select> above stays the single source
// of truth for WHICH languages this title offers -- everything below only
// mirrors its options into a checkbox list, so the per-site special cases in
// rebuildLanguageSelectOptions() (filmpalast, hanime, module-provided labels)
// keep working untouched.
//
// Order matters and the DOM does not carry it: the FIRST language the user
// ticks is the primary one, and the primary decides the target folder and the
// file name (the others only add a track to its file, see the queue route).
// mf_multiselect reports checked boxes in document order, so the click order is
// tracked here instead.
let langMultiActive = false;
let langMultiOrder = [];

const langMultiToggle = document.getElementById("langMultiToggle");
const langMultiRoot = document.getElementById("langMultiSelect");
const langMultiHint = document.getElementById("langMultiHint");

// A language group ("take the first of these that exists") and a multi
// selection ("take all of these") are different questions, and "All Languages"
// is already its own fan-out. None of them can be a track in someone else's
// file, so they are left out of the checkbox list.
function langMultiEligibleOptions() {
  if (!languageSelect) return [];
  return Array.from(languageSelect.options).filter(
    (o) => o.value && !o.value.startsWith("group:") && o.value !== "All Languages",
  );
}

/** Rebuild the checkbox list from the current <select> options. */
function syncLangMultiFromSelect() {
  if (!langMultiRoot || !langMultiToggle) return;
  const opts = langMultiEligibleOptions();
  // Nothing to combine: a single-language title (filmpalast, hanime) must not
  // offer a mode that cannot do anything. Not tied to dl_audio_track_merge --
  // choosing several languages is itself the instruction to merge them, and
  // the queue worker forces it for exactly those items.
  const usable = opts.length > 1;
  langMultiToggle.hidden = !usable;
  if (!usable && langMultiActive) setLangMultiActive(false);

  const dropdown = langMultiRoot.querySelector(".mf-multiselect-dropdown");
  if (!dropdown) return;
  const stillThere = new Set(opts.map((o) => o.value));
  langMultiOrder = langMultiOrder.filter((v) => stillThere.has(v));

  dropdown.innerHTML = "";
  opts.forEach((o) => {
    const label = document.createElement("label");
    label.className = "mf-multiselect-item";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "chb-main";
    box.value = o.value;
    box.checked = langMultiOrder.includes(o.value);
    const span = document.createElement("span");
    span.textContent = o.textContent;
    // Flagged in the list, not only in the hint below: by the time the hint
    // explains it the box is already ticked.
    if (isBurnedSubLang(o.value)) {
      const mark = document.createElement("em");
      mark.className = "mf-multiselect-note";
      mark.textContent = t(" (Videospur)", " (video track)");
      span.appendChild(mark);
      label.title = t(
        "Untertitel sind ins Bild gebrannt: ergibt eine zusätzliche Videospur, keine Tonspur.",
        "Subtitles are burned into the picture: this adds a second video stream, not an audio track.",
      );
    }
    label.appendChild(box);
    label.appendChild(span);
    dropdown.appendChild(label);
  });
  if (window.mfMultiSelect) window.mfMultiSelect.refresh(langMultiRoot);
  renderLangMultiHint();
}

/** Whether this language's subtitles are burned into the picture.
 *
 * Those cannot ride along as an audio track -- what makes them that language
 * is the image -- so the download fetches a second VIDEO stream instead. The
 * list comes from the server (languages.burned_subtitle_labels via
 * shared_modals.html); an unknown label, e.g. one a module invented, counts as
 * not burned in, which only means no warning rather than a wrong one.
 */
function isBurnedSubLang(lang) {
  return (window.MF_BURNED_SUB_LANGS || []).includes(lang);
}

/** "Primary: X — Y as an extra track", plus the video-stream caveat. */
function renderLangMultiHint() {
  if (!langMultiHint) return;
  if (!langMultiActive || !langMultiOrder.length) {
    langMultiHint.hidden = true;
    langMultiHint.textContent = "";
    return;
  }
  const primary = langMultiOrder[0];
  const extras = langMultiOrder.slice(1);
  let msg = t("Hauptsprache: ", "Primary language: ") + primary;
  if (extras.length) {
    msg += t(" — zusätzlich: ", " — plus: ") + extras.join(", ");
  }

  // Sub languages among the EXTRAS are the ones worth warning about: as the
  // primary, a burned-in language is simply the file, which is what the user
  // asked for. Named individually, because a mixed selection makes "some of
  // these behave differently" useless on its own.
  const burned = extras.filter(isBurnedSubLang);
  if (burned.length) {
    msg += t(
      " · Achtung: bei " + burned.join(", ") + " sind die Untertitel ins Bild "
        + "gebrannt — das ergibt eine zusätzliche Videospur statt einer Tonspur "
        + "und vergrößert die Datei entsprechend.",
      " · Note: " + burned.join(", ") + " has its subtitles burned into the "
        + "picture — that adds a second video stream rather than an audio "
        + "track, and grows the file accordingly.",
    );
  }
  langMultiHint.textContent = msg;
  langMultiHint.hidden = false;
}

function setLangMultiActive(on) {
  langMultiActive = !!on;
  if (langMultiToggle) langMultiToggle.setAttribute("aria-pressed", String(langMultiActive));
  if (languageSelect) languageSelect.hidden = langMultiActive;
  if (langMultiRoot) langMultiRoot.hidden = !langMultiActive;
  if (langMultiActive) {
    // Carry the single selection over so switching modes never silently drops
    // what the user had already picked -- and it becomes the primary, which is
    // the answer they would have got in single mode anyway.
    const cur = languageSelect ? languageSelect.value : "";
    if (cur && !cur.startsWith("group:") && cur !== "All Languages" && !langMultiOrder.length) {
      langMultiOrder = [cur];
    }
    syncLangMultiFromSelect();
  } else if (langMultiOrder.length && languageSelect) {
    // Back to one language: keep the primary, so the provider dropdown and the
    // availability pills below carry on describing what is actually selected.
    languageSelect.value = langMultiOrder[0];
    languageSelect.dispatchEvent(new Event("change"));
  }
  renderLangMultiHint();
}

/** Ordered languages for a download: the ticked ones, or the single select. */
function selectedLanguages() {
  if (langMultiActive && langMultiOrder.length) return langMultiOrder.slice();
  return languageSelect && languageSelect.value ? [languageSelect.value] : [];
}

if (langMultiToggle) {
  langMultiToggle.addEventListener("click", () => setLangMultiActive(!langMultiActive));
}

if (langMultiRoot) {
  langMultiRoot.addEventListener("mf-multiselect-change", (e) => {
    const picked = (e.detail && e.detail.values) || [];
    // Append newly ticked in click order, drop unticked, keep the rest as-is —
    // this is the only place the primary is decided.
    langMultiOrder = langMultiOrder.filter((v) => picked.includes(v));
    picked.forEach((v) => {
      if (!langMultiOrder.includes(v)) langMultiOrder.push(v);
    });
    // The hoster list is per language: follow the primary.
    if (langMultiOrder.length && languageSelect) {
      languageSelect.value = langMultiOrder[0];
      languageSelect.dispatchEvent(new Event("change"));
    }
    renderLangMultiHint();
  });
}

// Display name for a dropdown value — a group's name instead of "group:<id>".
function languageLabelFor(value) {
  if (!String(value || "").startsWith("group:")) return value || "";
  const group = (languageGroups || []).find((g) => "group:" + g.id === value);
  return group ? group.name : value;
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
  // The availability pills mirror this select, so a change made in the
  // dropdown has to move the highlight too -- otherwise the two controls show
  // different answers to the same question.
  languageSelect.addEventListener("change", () => syncLangAvailPills());
}

/** Move the pill highlight to whatever the language select currently holds. */
function syncLangAvailPills() {
  const banner = document.getElementById("langAvailBanner");
  if (!banner || !languageSelect) return;
  banner.querySelectorAll(".lang-avail-pill[data-lang]").forEach(function (p) {
    const on = p.dataset.lang === languageSelect.value;
    p.classList.toggle("is-active", on);
    p.setAttribute("aria-pressed", String(on));
  });
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
    // hanime listing cards carry the per-episode title ("Foo 2") while the
    // download folder is named after the franchise ("Foo") — pass both so the
    // "Vorhanden" check matches either. Other providers only ever have .title.
    addDownloadedBadgeMulti(card, [item.series_title, item.title]);
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
      // Wait for the settings instead of deferring one tick and hoping: a tick
      // is not enough while /api/settings is still in flight, and every path
      // inside _applyTmdbToCard reads cineinfoSettings. Resolved settings make
      // this a microtask, so nothing is slower in the common case.
      // `found: false` is passed through on purpose -- the provider chain
      // (Crunchyroll, Fernsehserien, module pills) still has something to say
      // about a title TMDB does not know, and used to be skipped entirely.
      loadGeneralSettings().then(() => _applyTmdbToCard(card, item.tmdb));
    } else {
      // Fall back to lazy loading via IntersectionObserver
      enrichCardWithTmdb(card, item.title);
    }
  });
}

function renderBrowseHoverCards(card, tmdb_voting, tmdb_genres, tmdb_fsk) {
  // The drawer used to be built exactly once ("if it exists, return"), which
  // made it a snapshot of whatever was known at first paint. CineInfo enriches
  // LATER (batched /api/tmdb/batch after the IntersectionObserver fires), so
  // on a cold TMDB cache the first call had nothing to show, bailed at the
  // "no data" guard below -- and the second call, with the real data, was
  // blocked by that early return. The only way to see the drawer was a reload,
  // by which time the server cache answered inline. It now UPDATES.
  //
  // Data is merged, not replaced: several paths call this for the same card
  // (provider chain, re-enrich after settings arrive) and some of them only
  // know part of it. A later call without a rating must not throw away a
  // rating that is already on screen.
  const prev = card._mfHoverData || {};
  const data = {
    rating: (tmdb_voting !== undefined && tmdb_voting !== null && tmdb_voting !== "")
      ? tmdb_voting : prev.rating,
    genres: (tmdb_genres && tmdb_genres.length) ? tmdb_genres : prev.genres,
    fsk: (tmdb_fsk !== undefined && tmdb_fsk !== null && tmdb_fsk !== "")
      ? tmdb_fsk : prev.fsk,
  };
  card._mfHoverData = data;
  tmdb_voting = data.rating;
  tmdb_genres = data.genres;
  tmdb_fsk = data.fsk;

  // "Not loaded yet" is not "switched off". The hanime branch of
  // renderBrowseCards() calls this synchronously, before /api/settings has
  // answered -- treating that as "all three off" would delete a drawer that is
  // about to be correct. Come back when we actually know.
  if (!cineinfoSettings) {
    loadGeneralSettings().then(() =>
      renderBrowseHoverCards(card, tmdb_voting, tmdb_genres, tmdb_fsk));
    return;
  }

  const showRating = cineinfoSettings.show_hover_rating === "1";
  const showGenres = cineinfoSettings.show_hover_genres === "1";
  const showFSK = cineinfoSettings.show_hover_fsk === "1";

  // Nothing switched on in Settings -> CineInfo: no drawer at all, and an
  // existing one goes away. The three hover switches are the gate for this
  // whole feature -- it must never render "specially" because data happens to
  // be there while the user has the option off.
  if (!showRating && !showGenres && !showFSK) {
    const stale = card.querySelector(".browse-hover-overlay");
    if (stale) stale.remove();
    return;
  }

  // Colour, size and shape all live in cards.css (.browse-hover-pill and
  // friends) instead of inline styles: the drawer these pills sit in is a
  // shared component that third-party modules render too, and inline styles
  // can neither be themed nor overridden by them.
  let votingHtml = "";
  if (showRating && tmdb_voting) {
    const formattedVote = parseFloat(tmdb_voting).toFixed(1);
    votingHtml = `<span class="browse-hover-pill browse-hover-pill--rating"><svg width="10" height="10" viewBox="0 0 24 24" fill="#4ade80" aria-hidden="true"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"></path></svg>${esc(formattedVote)}</span>`;
  }

  let genresHtml = "";
  if (showGenres && tmdb_genres && tmdb_genres.length > 0) {
    const genreSpans = tmdb_genres.map(g => `<span>${esc(g)}</span>`).join("");
    genresHtml = `<div class="genres hover-genres">${genreSpans}</div>`;
  }

  let fskHtml = "";
  if (showFSK && tmdb_fsk) {
    // Whitelist rather than interpolation: tmdb_fsk comes from an API
    // response and ends up inside a class attribute — anything unexpected
    // falls back to the neutral pill instead of reaching the markup.
    const _fskSteps = [0, 6, 12, 16, 18];
    const _step = Number(tmdb_fsk);
    const fskCls = _fskSteps.indexOf(_step) !== -1 ? ` browse-fsk-${_step}` : "";
    fskHtml = `<span class="browse-hover-pill browse-hover-pill--fsk${fskCls}">FSK ${esc(tmdb_fsk)}</span>`;
  }

  // Still nothing to say. An enrichment that has not answered yet must keep
  // whatever is already on the card (waiting for the next call), but an
  // EMPTY drawer has to go: since the badges became a bar at the poster's
  // bottom edge, a content-less .browse-hover-content still paints its
  // background and its accent border -- and on touch it rests permanently
  // open. That is the empty hover strip on rows whose source carries no TMDB
  // metadata (aniworld "latest" and every other module-fed row).
  if (!votingHtml && !genresHtml && !fskHtml) {
    const stale = card.querySelector(".browse-hover-overlay");
    const staleContent = stale && stale.querySelector(".browse-hover-content");
    if (stale && (!staleContent || !staleContent.textContent.trim())) stale.remove();
    return;
  }

  const inner = fskHtml + votingHtml + genresHtml;
  let overlay = card.querySelector(".browse-hover-overlay");
  if (overlay) {
    const content = overlay.querySelector(".browse-hover-content");
    // Signature check: an update while the pointer is inside the card would
    // otherwise rebuild the drawer's DOM on every poll and make it flicker
    // mid-transition. Same markup in, nothing touched.
    if (content && content.dataset.mfSig !== inner) {
      content.dataset.mfSig = inner;
      content.innerHTML = inner;
    }
    return;
  }

  overlay = document.createElement("div");
  overlay.className = "browse-hover-overlay";
  // The drawer element itself is created once and only its contents change
  // afterwards, so the CSS transition in cards.css never restarts.
  overlay.innerHTML = `
    <div class="browse-hover-content" data-mf-sig="${esc(inner)}">
      ${fskHtml}
      ${votingHtml}
      ${genresHtml}
    </div>
  `;
  const content = overlay.querySelector(".browse-hover-content");
  if (content) content.dataset.mfSig = inner;
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
    if (retryIfPending([newData, popData], "aniworld", function () { aniLoadedAt = 0; loadAniworldBrowse(); })) return;

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

function updateBrowseScrollBtns(section) {
  // A row that fits on screen (or was thinned out by a content filter) has
  // nothing to scroll -- grey the arrows out instead of leaving buttons that
  // visibly do nothing when clicked.
  const grid = section.querySelector(".browse-grid");
  const btns = section.querySelectorAll(".browse-scroll-btn");
  if (!grid || btns.length < 2) return;
  const max = grid.scrollWidth - grid.clientWidth;
  const atStart = grid.scrollLeft <= 1;
  const atEnd = grid.scrollLeft >= max - 1;
  btns[0].disabled = max <= 1 || atStart;
  btns[1].disabled = max <= 1 || atEnd;
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
      '<button class="browse-scroll-btn" onclick="scrollBrowseGrid(this,-1)" aria-label="' + t("Zurück", "Back") + '">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>' +
      '</button>' +
      '<button class="browse-scroll-btn" onclick="scrollBrowseGrid(this,1)" aria-label="' + t("Weiter", "Next") + '">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>' +
      '</button>';
    row.appendChild(btns);

    const refresh = function () { updateBrowseScrollBtns(section); };
    grid.addEventListener("scroll", refresh, { passive: true });
    // Cards arrive asynchronously and the row width changes with the window,
    // so the state has to be recomputed on both.
    if (window.ResizeObserver) new ResizeObserver(refresh).observe(grid);
    if (window.MutationObserver) new MutationObserver(refresh).observe(grid, { childList: true });
    window.addEventListener("resize", refresh);
    refresh();
  });
}

function scrollBrowseGrid(btn, dir) {
  const section = btn.closest(".browse-section");
  const grid = section && section.querySelector(".browse-grid");
  if (grid) grid.scrollBy({ left: dir * 460, behavior: "smooth" });
}

initBrowseScrollButtons();

// ── Recent searches ─────────────────────────────────────────────────────────
// The search field is the whole point of the home page and used to be the
// dumbest thing on it: no history, no shortcut, no way back to what you looked
// up yesterday. The list is per browser on purpose -- it is typing history,
// not a setting, and it never leaves the device.
const _RECENT_KEY = "mf-recent-searches";
const _RECENT_MAX = 8;

function _homeText(key, de, en) {
  const map = window.__HOME_I18N || {};
  return map[key] || t(de, en);
}

function _recentSearches() {
  try {
    const raw = JSON.parse(localStorage.getItem(_RECENT_KEY) || "[]");
    return Array.isArray(raw) ? raw.filter(function (x) { return typeof x === "string"; }) : [];
  } catch (e) { return []; }
}

function _saveRecentSearches(list) {
  try { localStorage.setItem(_RECENT_KEY, JSON.stringify(list.slice(0, _RECENT_MAX))); }
  catch (e) { /* private mode */ }
}

function pushRecentSearch(keyword) {
  const kw = String(keyword || "").trim();
  if (!kw) return;
  const list = _recentSearches().filter(function (x) { return x.toLowerCase() !== kw.toLowerCase(); });
  list.unshift(kw);
  _saveRecentSearches(list);
}

function _suggestBox() { return document.getElementById("searchSuggest"); }

function closeSearchSuggest() {
  const box = _suggestBox();
  if (!box) return;
  box.hidden = true;
  box.innerHTML = "";
  const input = document.getElementById("searchInput");
  if (input) input.setAttribute("aria-expanded", "false");
}

// Suggestions fetched from /api/home/suggest for the current query. Kept
// outside renderSearchSuggest() so the dropdown can repaint instantly from
// the local history while the server answer is still on its way -- a preview
// that only appears once the network replies is not a preview.
let _suggestGroups = [];
let _suggestQuery = "";
let _suggestTimer = null;

const _SUGGEST_GROUP_LABELS = {
  library: ["suggest_library", "In deiner Mediathek", "In your library"],
  watchlist: ["suggest_watchlist", "Auf deiner Merkliste", "On your watchlist"],
};

/** Ask the server what it already knows about *typed*.
    Debounced and query-checked: answers that arrive after the user has typed
    on are dropped rather than replacing a newer list. */
function _fetchSuggest(typed) {
  clearTimeout(_suggestTimer);
  if (typed.length < 2) { _suggestGroups = []; _suggestQuery = ""; return; }
  _suggestTimer = setTimeout(async function () {
    try {
      const resp = await fetch("/api/home/suggest?q=" + encodeURIComponent(typed));
      const data = await resp.json();
      const input = document.getElementById("searchInput");
      const still = input && input.value.trim().toLowerCase();
      if (still !== typed) return;          // the user has moved on
      _suggestGroups = Array.isArray(data.groups) ? data.groups : [];
      _suggestQuery = typed;
      renderSearchSuggest(true);
    } catch (e) {
      _suggestGroups = [];
    }
  }, 180);
}

function renderSearchSuggest(skipFetch) {
  const box = _suggestBox();
  const input = document.getElementById("searchInput");
  if (!box || !input) return;
  const typed = input.value.trim().toLowerCase();
  if (!skipFetch) {
    if (typed !== _suggestQuery) { _suggestGroups = []; }
    _fetchSuggest(typed);
  }
  const list = _recentSearches().filter(function (x) {
    return !typed || x.toLowerCase().indexOf(typed) !== -1;
  });
  const groups = typed === _suggestQuery ? _suggestGroups : [];
  if (!list.length && !groups.length) { closeSearchSuggest(); return; }

  let html = "";
  // Server groups first: "the thing you are looking for is already on disk"
  // is a more useful answer than "you searched for this before".
  groups.forEach(function (group) {
    const meta = _SUGGEST_GROUP_LABELS[group.key];
    if (!meta || !(group.items || []).length) return;
    html += '<div class="home-suggest-head">' +
      escapeHtml(_homeText(meta[0], meta[1], meta[2])) + "</div>";
    group.items.forEach(function (item) {
      html += '<div class="home-suggest-item" role="option" tabindex="-1"' +
        ' data-kw="' + escapeHtml(item.title || "") + '"' +
        (item.href ? ' data-href="' + escapeHtml(item.href) + '"' : "") +
        (item.url ? ' data-url="' + escapeHtml(item.url) + '"' : "") + ">" +
        '<span class="home-suggest-dot" data-group="' + escapeHtml(group.key) + '"></span>' +
        "<span>" + escapeHtml(item.title || "") + "</span>" +
        (item.sub ? '<small class="home-suggest-sub">' + escapeHtml(item.sub) + "</small>" : "") +
        "</div>";
    });
  });

  if (list.length) {
    html += '<div class="home-suggest-head">' +
      escapeHtml(_homeText("recent_searches", "Zuletzt gesucht", "Recent searches")) +
      '<button type="button" class="home-suggest-clear" data-clear="1">' +
      escapeHtml(_homeText("clear_history", "Verlauf löschen", "Clear history")) + "</button></div>" +
      list.map(function (kw) {
        return '<div class="home-suggest-item" role="option" tabindex="-1" data-kw="' + escapeHtml(kw) + '">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>' +
          '<span>' + escapeHtml(kw) + "</span>" +
          '<button type="button" class="home-suggest-del" data-del="' + escapeHtml(kw) + '" aria-label="' +
          escapeHtml(_homeText("remove_entry", "Entfernen", "Remove")) + '">&times;</button></div>';
      }).join("");
  }

  box.innerHTML = html;
  box.hidden = false;
  input.setAttribute("aria-expanded", "true");
}

/** Act on one suggestion. A watchlist entry carries a real series URL, so it
    opens that series directly instead of starting a search for its own
    name; everything else falls back to the ordinary search. */
function _applySuggestion(item) {
  const input = document.getElementById("searchInput");
  closeSearchSuggest();
  if (item.dataset.href) { window.location.href = item.dataset.href; return; }
  if (item.dataset.url && typeof openSeries === "function") {
    openSeries(item.dataset.url);
    return;
  }
  if (input) input.value = item.dataset.kw || "";
  if (typeof doSearch === "function") doSearch();
}

// The search field carries autofocus (the home page is a search page), and a
// focus event is a focus event -- so the history panel used to be open before
// the user had done anything at all, covering the first row of the feed. Only
// a focus the user actually caused may open it: the very first pointer or key
// event on the page arms it, which the browser's own autofocus never fires.
let _suggestArmed = false;
["pointerdown", "keydown", "touchstart"].forEach(function (evt) {
  document.addEventListener(evt, function () { _suggestArmed = true; },
    { capture: true, once: true, passive: true });
});

function initSearchSuggest() {
  const input = document.getElementById("searchInput");
  const box = _suggestBox();
  if (!input || !box) return;

  input.addEventListener("focus", function () {
    if (_suggestArmed) renderSearchSuggest();
  });
  input.addEventListener("input", function () { renderSearchSuggest(); });
  input.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { closeSearchSuggest(); return; }
    if (box.hidden) return;
    const items = Array.prototype.slice.call(box.querySelectorAll(".home-suggest-item"));
    if (!items.length) return;
    const current = items.indexOf(box.querySelector(".home-suggest-item.is-active"));
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      const next = ev.key === "ArrowDown"
        ? (current + 1) % items.length
        : (current <= 0 ? items.length - 1 : current - 1);
      items.forEach(function (el) { el.classList.remove("is-active"); });
      items[next].classList.add("is-active");
      // Keeps the highlighted row inside the (scrollable) dropdown.
      items[next].scrollIntoView({ block: "nearest" });
      return;
    }
    // Enter only takes the highlighted entry -- without a highlight it must
    // still start the search for exactly what was typed.
    if (ev.key === "Enter" && current >= 0) {
      ev.preventDefault();
      _applySuggestion(items[current]);
    }
  });
  box.addEventListener("mousedown", function (ev) {
    // mousedown, not click: the input's blur would close the box first.
    const del = ev.target.closest("[data-del]");
    if (del) {
      ev.preventDefault();
      _saveRecentSearches(_recentSearches().filter(function (x) { return x !== del.dataset.del; }));
      renderSearchSuggest();
      return;
    }
    if (ev.target.closest("[data-clear]")) {
      ev.preventDefault();
      _saveRecentSearches([]);
      closeSearchSuggest();
      return;
    }
    const item = ev.target.closest(".home-suggest-item");
    if (!item) return;
    ev.preventDefault();
    _applySuggestion(item);
  });
  document.addEventListener("click", function (ev) {
    if (!ev.target.closest(".home-search-field")) closeSearchSuggest();
  });

  // "/" jumps to the search field, the way every search-first page does it.
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "/" || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const el = document.activeElement;
    const tag = el && el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (el && el.isContentEditable)) return;
    if (document.querySelector(".queue-overlay[style*='display: flex'], .queue-overlay[style*='display:flex']")) return;
    ev.preventDefault();
    input.focus();
    input.select();
  });
}

initSearchSuggest();

/**
 * "Still searching" text for a fan-out search, or "" when nothing is left.
 * The search asks every enabled source with its own request and paints each
 * answer as it lands, so between the first and the last answer the page looks
 * finished while it is not. Exported because the Seerr page fans out over the
 * same /api/search endpoint and needs the same sentence (static/seerr.js).
 * @param {number} pending sources that have not answered yet
 */
function mfSearchPendingText(pending) {
  if (pending <= 0) return "";
  if (pending === 1) {
    return t("Suche läuft … noch 1 Quelle", "Still searching … 1 source to go");
  }
  return t("Suche läuft … noch " + pending + " Quellen",
           "Still searching … " + pending + " sources to go");
}
window.mfSearchPendingText = mfSearchPendingText;

async function doSearch() {
  const keyword = searchInput.value.trim().replace(/!+$/, "");
  if (!keyword) return;
  pushRecentSearch(keyword);
  if (typeof closeSearchSuggest === "function") closeSearchSuggest();
  searchBtn.disabled = true;
  searchSpinner.style.display = "block";
  // Create a search grid with skeletons
  resultsDiv.innerHTML = "";
  renderSearchHeader(keyword);
  const block = document.createElement("div");
  block.className = "browse-provider-block";
  const grid = document.createElement("div");
  grid.className = "results-poster-grid";
  block.appendChild(grid);
  resultsDiv.appendChild(block);
  renderSkeletons(grid, 12);

  // Either layout may be on the page (see index.html's new_home switch), and
  // on the new home page #browse does not exist at all.
  if (browseDiv) browseDiv.style.display = "none";
  const _homeFeed = document.getElementById("homeFeed");
  if (_homeFeed) _homeFeed.style.display = "none";
  enterSearchView(keyword);

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
    // Every source that exists right now, built-in or module-registered, in
    // the user's own order -- see loadSearchSources(). An adult source is
    // always opt-in; everything else is only skipped when the user asked for
    // disabled sources to be left out of search ("hide_disabled_in_search").
    const _asked = _sortSourcesByUserOrder(await loadSearchSources(), _srcSettings.order)
      .filter(function (src) {
        // searchable === false: provider-only module source (URL resolution,
        // no search function). Listed on the Sources tab, never queried here.
        if (src.searchable === false) return false;
        const on = _sourceIsOn(src, _en);
        return src.adult ? on : (on || !_hide);
      });

    // One request per source (that was always the case), but the results are
    // painted the moment THAT source answers instead of when the slowest one
    // does: awaiting Promise.all meant a site that needed 12 s held back the
    // one that answered in 200 ms, and a dead source held everything until
    // searchSite()'s 15 s timeout fired.
    //
    // The slots are created up front, in the user's source order, so a source
    // that answers late still lands in its own place rather than wherever the
    // network happened to put it. An empty slot is removed again.
    resultsDiv.innerHTML = "";
    const _slots = _asked.map(function (src) {
      const slot = document.createElement("div");
      slot.className = "browse-provider-block";
      const skel = document.createElement("div");
      skel.className = "results-poster-grid";
      slot.appendChild(skel);
      renderSkeletons(skel, 6);   // no-op unless the user has skeletons on
      resultsDiv.appendChild(slot);
      return slot;
    });

    // The spinner stays up until the LAST source has answered and counts down
    // as they do -- otherwise the results of the fastest source look like the
    // whole answer while four sites are still being scraped. searchSite()
    // resolves on error and on its own 15 s timeout too, so the count always
    // reaches zero and the finally below always hides it.
    let _pending = _asked.length;
    searchSpinner.textContent = mfSearchPendingText(_pending);

    let _anyResults = false;
    await Promise.all(_asked.map(function (src, i) {
      return searchSite(src.id).catch(() => []).then(function (results) {
        _pending--;
        searchSpinner.textContent = mfSearchPendingText(_pending);
        // The censorship filter is a property of the hanime source's own
        // metadata (r.censored), so it applies to whichever source reports
        // it, not to a hardcoded site id.
        const block = buildSourceSection({
          source: src,
          results: _filterHanimeCensorship(results) || [],
        });
        if (!block) { _slots[i].remove(); return; }
        _slots[i].replaceWith(block);
        _anyResults = true;
      });
    }));

    // Only once every source has had its say -- an empty state shown while
    // requests are still running would be wrong for the whole time it takes
    // the next one to answer.
    if (!_anyResults) {
      resultsDiv.innerHTML =
        '<div style="width:100%;text-align:center;color:#888;padding:40px">' +
        escapeHtml(t("Keine Ergebnisse gefunden.", "No results found.")) + "</div>";
    }
  } catch (e) {
    // The message belonged to both languages, not just to the English one --
    // the German toast used to end after the colon.
    showToast(t("Suche fehlgeschlagen: ", "Search failed: ") + e.message);
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

/**
 * One source's result block, ready to be dropped into #results.
 * doSearch() appends these one at a time, the moment each source answers,
 * instead of building the whole result list once every source has replied.
 * Replaces the old five-positional-argument renderResultsBoth(), which could
 * not represent a sixth source at all.
 * @param {{source: Object, results: Array}} sec
 * @returns {HTMLElement|null} null when that source had no hits.
 */
function buildSourceSection(sec) {
  sec.results = sec.results || [];
  if (!sec.results.length) return null;

  const block = document.createElement("div");
  block.className = "browse-provider-block";

  const header = document.createElement("div");
  // A module source has no colour of its own in the shipped CSS, so
  // source_policy hands it the neutral browse-provider-thirdparty class
  // rather than a class name that does not exist.
  header.className = "browse-provider-header " +
    (sec.source.css_class || "browse-provider-thirdparty");
  // textContent, not innerHTML: the label comes from a module.
  header.textContent = sec.source.label || sec.source.id;
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
    // Same franchise-vs-episode title split as renderBrowseCards (hanime).
    // Skipped for an adult source: "already downloaded"/"already syncing"
    // are questions about the LIBRARY, matched purely by title text -- an
    // adult tube site's own search can and does return results tagged with
    // the same title as something unrelated in the library (a clip literally
    // named after a show, a clickbait tag, ...), and badging that result
    // "Vorhanden" reads as "this IS your library item", which it is not.
    // Built-in hanime already made the same call for the same reason (see
    // its own opts.skipTmdb branch just below); this brings a module-
    // registered adult source (register_search_source(..., adult=True)) up
    // to the same standard instead of only covering the one built-in.
    if (!sec.source.adult) {
      addDownloadedBadgeMulti(card, [r.series_title, r.title]);
      addSyncBadge(card, r.url);
    }
    grid.appendChild(card);
    loadPoster(r.url, card.querySelector("img"));
    // hanime is adult content and isn't in TMDB's database, so — same as
    // the dedicated hanime Browse tab (renderBrowseCards' skipTmdb option)
    // — skip the TMDB/Crunchyroll/Fernsehserien lookup chain entirely here
    // too; it would just be a wasted (or wrong-match) request per card.
    // Genre/FSK hover info still works, fed from hanime's own tags.
    // Keyed off "is this an adult source", not off the literal id, so a
    // module's 18+ source is treated the same way.
    if (sec.source.adult) {
      const hanimeTags = (r.tags && r.tags.length)
        ? r.tags
        : (r.genre ? r.genre.split(",").map(g => g.trim()).filter(Boolean) : []);
      renderBrowseHoverCards(card, null, hanimeTags, 18);
      return;
    }
    enrichCardWithTmdb(card, r.title);
  });

  block.appendChild(grid);
  return block;
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
  // Drop the previous title's backdrop before the next one loads -- otherwise
  // the modal opens showing the last series' artwork behind the new title.
  _mfSetBackdrop("");
  const _favBtn = document.getElementById("favouriteBtn");
  if (_favBtn) _favBtn.style.display = "none";

  if (isSkeleton) {
    modal.classList.add("skeleton");
    document.getElementById("modalPoster").style.opacity = "0";
    document.getElementById("modalTitle").innerHTML = '<div style="height:28px; width:60%; background:rgba(255,255,255,0.03); border-radius:6px; margin-bottom:8px;"></div>';
    document.getElementById("modalGenres").innerHTML = '<div style="height:14px; width:40%; background:rgba(255,255,255,0.03); border-radius:4px"></div>';
    document.getElementById("modalYear").textContent = "";
    document.getElementById("modalDesc").innerHTML = '<div style="height:14px; width:100%; background:rgba(255,255,255,0.03); border-radius:4px; margin-bottom:6px"></div><div style="height:14px; width:80%; background:rgba(255,255,255,0.03); border-radius:4px"></div>';
  } else {
    modal.classList.remove("skeleton");
    document.getElementById("modalPoster").style.opacity = "";
    document.getElementById("modalTitle").textContent = t("Lädt...", "Loading...");
    document.getElementById("modalGenres").textContent = "";
    document.getElementById("modalYear").textContent = "";
    document.getElementById("modalDesc").textContent = "";
    mfSyncDescClamp();
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
  // Cleared HERE, not in buildAccordion(): that runs only after /api/series
  // and /api/seasons have both come back, so the previous title's "Already
  // stored in ..." row stayed on screen for the first seconds of the next one
  // -- pointing at a folder that has nothing to do with what is being opened.
  // Everything else the modal carries over is reset in this same block for
  // exactly that reason. No skeleton variant: a title with nothing on disk
  // shows no row at all, so a placeholder would promise a line that may never
  // arrive.
  const _libLoc = document.getElementById("libLocation");
  if (_libLoc) {
    _libLoc.style.display = "none";
    _libLoc.innerHTML = "";
  }
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

    // The server may have resolved an episode URL to its series page (a
    // 9anime "Recently Updated" card links to the newest episode, not to the
    // series). Adopt the canonical URL, otherwise every follow-up call --
    // episodes, download, favourite, Auto-Sync -- would still be keyed to that
    // one episode.
    if (seriesData.url && seriesData.url !== currentSeriesUrl) {
      currentSeriesUrl = seriesData.url;
      _updateUpscaleCheckbox(currentSeriesUrl);
    }

    currentSeriesTitle = seriesData.title || t("Unbekannt", "Unknown");
    currentSeriesCoverUrl = seriesData.poster_url || "";
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
    mfSyncDescClamp();

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
      // "German Dub" is only correct for the German-only movie sites. filmo.to
      // carries several languages per movie, each with its own hoster list, so
      // labelling the whole list German made the provider lookup miss for
      // every language the dropdown actually offered -- "No source available"
      // on a movie whose sources were right there. Ask /api/providers instead,
      // which answers per language for exactly this reason.
      const _germanOnlyMovieSite = url.includes("filmpalast.to") || url.includes("megakino");
      if (_germanOnlyMovieSite) {
        availableProviders = { "German Dub": seriesData.available_providers };
        updateProviderDropdown();
      } else {
        fetchProviders(currentSeriesUrl || url);
      }
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
    _updateFavouriteBtn(url, seriesData.title, seriesData.poster_url || "", isMovie);
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

  // Belt and braces: openSeries() already cleared this the moment the modal
  // was opened. This covers a rebuild that does not go through openSeries.
  const _locEl = document.getElementById("libLocation");
  if (_locEl) {
    _locEl.style.display = "none";
    _locEl.innerHTML = "";
  }

  // Fetch all seasons' episodes in parallel
  const fetches = seasons.map((s, i) =>
    fetch("/api/episodes?url=" + encodeURIComponent(s.url))
      .then((r) => r.json())
      .then((data) => ({
        index: i,
        episodes: data.episodes || [],
        locations: data.locations || [],
      }))
      .catch(() => ({ index: i, episodes: [], locations: [] })),
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

      const langFlagMap = LANG_FLAG_SRC;

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

    // Where the existing files live (header line + per-season deviations)
    renderLibraryLocations(results);

    // Fetch providers from first episode (updates dynamically with checked availability)
    if (firstProviderUrl) {
      fetchProviders(firstProviderUrl);
    }
  });
}

/* One flag per language label, used by BOTH the availability pills and the
   per-episode flag row. It used to be declared inside buildAccordion's season
   loop, which meant the pills could only have gotten a second copy -- and two
   copies of a mapping like this drift the first time a language is added. */
const LANG_FLAG_SRC = {
  "German Dub": "/static/flags/german.svg",
  "English Dub": "/static/flags/english.svg",
  "German Sub": "/static/flags/japanese-germanSub.svg",
  "English Sub": "/static/flags/japanese-englishSub.svg",
  "English Dub (German Sub)": "/static/flags/english-germanSub.svg",
};

/** Identity of one storage location, for grouping across seasons. */
function _locKey(loc) {
  return (loc.root_label || "") + " " + (loc.folder || "");
}

/** "Anime NAS › Naruto (2002)" — the label every account gets to see.
    root_label is empty for the global download folder, which has no name of
    its own, so it borrows the one the Target-folder select already uses. */
function _locLabel(loc) {
  const root = loc.root_label || t("Standard", "Default");
  return loc.folder ? root + " › " + loc.folder : root;
}

/**
 * Show WHERE the episodes that already exist are stored.
 *
 * Two levels, because one line is right almost always and wrong occasionally:
 *
 *  - The line under the three selects names the location holding most of the
 *    series. That is the answer in the normal case (one folder, one series).
 *  - A season whose files sit somewhere ELSE gets its own small marker in the
 *    season header. Printing the same path on all twelve season rows would be
 *    noise; printing it on the one row that deviates is the information.
 *
 * The absolute path is only in the payload for admins (see search.py's
 * _build_locations), so the tooltip simply has nothing to show for everyone
 * else -- no separate client-side check needed.
 */
function renderLibraryLocations(results) {
  const el = document.getElementById("libLocation");
  if (!el) return;

  // Merge the per-season lists into one location -> total episodes map.
  const totals = new Map();
  (results || []).forEach(({ locations }) => {
    (locations || []).forEach((loc) => {
      const key = _locKey(loc);
      const prev = totals.get(key);
      if (prev) prev.episodes += loc.episodes || 0;
      else totals.set(key, Object.assign({}, loc));
    });
  });

  if (totals.size === 0) {
    el.style.display = "none";
    el.innerHTML = "";
    return;
  }

  const ranked = Array.from(totals.values()).sort((a, b) => b.episodes - a.episodes);
  const primary = ranked[0];
  const others = ranked.length - 1;

  const icon =
    '<svg class="lib-loc-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"' +
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';

  // title= carries the absolute path when the server sent one (admins only).
  const titleAttr = primary.path ? ' title="' + esc(primary.path) + '"' : "";
  let html =
    icon +
    '<span class="lib-loc-lead">' + esc(t("Bereits vorhanden in", "Already stored in")) + "</span>" +
    '<span class="lib-loc-name"' + titleAttr + ">" + esc(_locLabel(primary)) + "</span>" +
    '<span class="lib-loc-count">' + primary.episodes + " " +
    esc(primary.episodes === 1 ? t("Episode", "episode") : t("Episoden", "episodes")) +
    "</span>";

  if (others > 0) {
    // The rest go into the tooltip rather than onto the line: a split library
    // is worth flagging, but not worth three lines above the episode list.
    const rest = ranked.slice(1)
      .map((l) => _locLabel(l) + " (" + l.episodes + ")")
      .join("\n");
    html +=
      '<span class="lib-loc-more" title="' + esc(rest) + '">+' + others + " " +
      esc(others === 1 ? t("weiterer Ordner", "more folder") : t("weitere Ordner", "more folders")) +
      "</span>";
  }

  el.innerHTML = html;
  el.style.display = "flex";

  // Per-season marker, only where a season deviates from the primary location.
  const primaryKey = _locKey(primary);
  (results || []).forEach(({ index, locations }) => {
    if (!locations || !locations.length) return;
    const top = locations.slice().sort((a, b) => b.episodes - a.episodes)[0];
    if (_locKey(top) === primaryKey) return;
    const section = seasonAccordion.querySelector(
      '.season-section[data-season-index="' + index + '"] .season-label',
    );
    if (!section) return;
    const chip = document.createElement("span");
    chip.className = "season-loc";
    chip.textContent = _locLabel(top);
    if (top.path) chip.title = top.path;
    section.appendChild(chip);
  });
}

function renderLangAvailBanner(results) {
  const banner = document.getElementById("langAvailBanner");
  if (!banner) return;
  banner.classList.remove("skeleton");
  // FilmPalast/MegaKino movies don't need a language availability banner
  // (German-dub-only, see rebuildLanguageSelect() above)
  if ((currentSeriesUrl || "").includes("filmpalast.to") || (currentSeriesUrl || "").includes("megakino")) {
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

  // Languages the series does not have at all are folded into one counter.
  // Five equally loud pills of which three read "0 / 279" spend most of the
  // row saying there is nothing to say; the ones that DO exist are the answer
  // to "which language can I actually take".
  const available = LANG_ORDER.filter((lang) => (counts[lang] || 0) > 0);
  const missing = LANG_ORDER.filter((lang) => !(counts[lang] || 0));

  // Nothing available at all does NOT mean "this series has no languages" --
  // it means the provider sent no language flags for it (ep.languages absent).
  // The old row said "0 / 279" four times, which was useless but honest; a
  // bare "4 unavailable" would be an assertion, and a wrong one. Say nothing.
  if (available.length === 0) { banner.style.display = "none"; return; }

  const selected = languageSelect ? languageSelect.value : "";

  const pills = available.map((lang) => {
    const n = counts[lang];
    const pct = Math.round((n / total) * 100);
    const full = n === total;
    const state = full ? "lang-avail-full" : "lang-avail-partial";
    // Pressed state, not just a highlight: the pill IS the language selector's
    // twin, so it has to say which one is active to a screen reader too.
    const isActive = lang === selected;
    const flag = LANG_FLAG_SRC[lang]
      ? `<img class="lang-avail-flag" src="${LANG_FLAG_SRC[lang]}" alt="" aria-hidden="true">`
      : "";
    const hint = full
      ? t("Alle Episoden verfügbar", "Every episode available")
      : `${total - n} ${t("Episoden fehlen", "episodes missing")}`;
    return (
      `<button type="button" class="lang-avail-pill ${state}${isActive ? " is-active" : ""}"` +
      ` style="--avail:${pct}%" data-lang="${esc(lang)}" aria-pressed="${isActive}"` +
      ` title="${esc(lang + " — " + hint)}">` +
      flag +
      `<span class="lang-avail-name">${esc(LANG_SHORT[lang])}</span>` +
      `<span class="lang-avail-num">${n}&thinsp;/&thinsp;${total}</span>` +
      "</button>"
    );
  }).join("");

  const missingChip = missing.length
    ? `<span class="lang-avail-missing" title="${esc(missing.map((l) => LANG_SHORT[l]).join("\n"))}">` +
      `${missing.length} ${esc(t("nicht verfügbar", "unavailable"))}</span>`
    : "";

  banner.innerHTML = pills + missingChip;
  banner.style.display = "flex";
}

/* Clicking a pill picks that language. Delegated on the banner because the
   pills are rebuilt on every season load -- per-pill listeners would have to
   be re-attached each time, and the ones from the previous series would keep
   a dead closure alive. `change` is dispatched by hand: assigning .value does
   not fire it, and updateProviderDropdown() hangs off that event. */
document.addEventListener("click", function (e) {
  const pill = e.target.closest && e.target.closest(".lang-avail-pill[data-lang]");
  if (!pill) return;
  const banner = document.getElementById("langAvailBanner");
  if (!banner || !banner.contains(pill)) return;
  const lang = pill.dataset.lang;
  if (!languageSelect || languageSelect.value === lang) return;
  // A language with no <option> cannot be selected -- the pill would look like
  // it did nothing. Only pills for languages that exist are rendered, so this
  // is a guard, not an expected path.
  if (!Array.from(languageSelect.options).some((o) => o.value === lang)) return;
  languageSelect.value = lang;
  // syncLangAvailPills() runs off this event, so the highlight follows.
  languageSelect.dispatchEvent(new Event("change"));
});

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

// Note the emptiness check on data.providers below. `{}` is truthy, so an
// answer with no hosters at all used to be accepted as a valid provider
// matrix: availableProviders became {}, which is not null, so
// updateProviderDropdown() ran, found no key for any language and left a
// single disabled "No source available" entry in the Hoster dropdown.
// Treating "empty" as "no answer" keeps the server-rendered static hoster
// list in place instead -- usable, and the real cause (a module whose
// provider_data the backend could not read) stays visible in the log rather
// than showing up as a dead dropdown.
async function fetchProviders(episodeUrl) {
  try {
    const resp = await fetch(
      "/api/providers?url=" + encodeURIComponent(episodeUrl),
    );
    const data = await resp.json();
    if (data.providers && Object.keys(data.providers).length) {
      availableProviders = data.providers;
      // For a site whose languages are not one of the two hardcoded sets,
      // this answer IS the language list (see rebuildLanguageSelect). Rebuild
      // before touching the provider dropdown, so the selected language is one
      // the providers are actually keyed by -- otherwise the very next lookup
      // misses and reports "No source available".
      const _u = currentSeriesUrl || "";
      const _hardcoded = _u.includes("aniworld.to") || _u.includes("s.to") ||
        _u.includes("serienstream.to") || _u.includes("filmpalast.to") ||
        _u.includes("megakino") || _u.includes("hanime.tv");
      if (!_hardcoded && languageSelect && !languageSelect.options.length) {
        rebuildLanguageSelect();
      }
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
  // A fallback group has no hoster list of its own: offer every hoster that
  // serves any of its languages, in chain order, so the preferred language's
  // hosters come first. Which language an episode ends up in is decided by the
  // queue worker, and it falls back to another hoster anyway if the picked one
  // doesn't serve that episode.
  let providers = availableProviders[lang];
  if (!providers && String(lang || "").startsWith("group:")) {
    providers = [];
    languageChainFor(lang).forEach((l) => {
      (availableProviders[l] || []).forEach((p) => {
        if (!providers.includes(p)) providers.push(p);
      });
    });
  }

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
  if (textEl) { textEl.style.display = ""; textEl.textContent = t("Es wird überprüft, ob der ausgewählte Inhalt auf Veev verfügbar ist",
                             "Checking whether the selected content is available on Veev"); }
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
  let language = languageSelect ? languageSelect.value : "German Dub";
  const provider = providerSelect ? providerSelect.value : "VOE";
  if (!provider) {
    showToast(t("Keine Quelle verfügbar", "No Source available"));
    return;
  }
  // Available languages: this episode's, else fall back to the page selector
  // (fallback groups are not languages the player can request).
  let langs = (langOptions && langOptions.length) ? langOptions.slice() : [];
  if (!langs.length && languageSelect) {
    langs = Array.from(languageSelect.options)
      .map((o) => o.value)
      .filter((v) => !String(v).startsWith("group:"));
  }
  // Streaming happens now, for one episode: resolve a fallback group to the
  // first of its languages this episode actually offers.
  if (String(language).startsWith("group:")) {
    const chain = languageChainFor(language);
    language = chain.find((l) => !langs.length || langs.includes(l)) || chain[0] || "German Dub";
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
  // The full {language: [hoster]} matrix powers the player's source picker;
  // `providers` alone only covers the language that happens to be selected
  // on this page right now.
  openStreamSource(episodeUrl, title, provider, language, startPos, langs, providers,
                   availableProviders || null);
}

// The player asks the page what comes next (see player.js::_resolveNext).
// Episode rows carry their URL in the checkbox value, so the next one is the
// next row in the same season body.
window.mfPlayerResolveNext = function (current) {
  if (!current || !current.url) return null;
  const boxes = Array.from(document.querySelectorAll("#seasonAccordion .episode-item input[type=checkbox][value]"));
  const i = boxes.findIndex((b) => b.value === current.url);
  if (i < 0 || i + 1 >= boxes.length) return null;
  const next = boxes[i + 1];
  // Stay inside the same season: the last episode of season 1 does not
  // roll into season 2 on its own.
  if (next.closest(".season-body") !== boxes[i].closest(".season-body")) return null;
  const row = next.closest(".episode-item");
  const titleEl = row ? row.querySelector(".ep-title") : null;
  return {
    url: next.value,
    title: titleEl ? titleEl.textContent : "",
    language: current.language,
    provider: current.provider,
  };
};

async function startDownload(all) {
  const episodes = all ? getAllEpisodeUrls() : getSelectedEpisodeUrls();
  if (!episodes.length) {
    showToast(all ? t("Keine Episoden verfügbar.", "No episodes available.") : t("Keine Episoden ausgewählt.", "No episodes selected."));
    return;
  }

  // In multi mode the FIRST ticked language is the primary; the rest ride
  // along as extra audio tracks in the same file. Everything below (mismatch
  // check, provider) deliberately keeps looking at the primary only: an extra
  // language the site does not offer costs one track, not the download.
  const languages = selectedLanguages();
  const language = languages[0] || "";
  if (!language) {
    showToast(t("Keine Sprache ausgewählt.", "No language selected."));
    return;
  }
  const provider = providerSelect.value;
  if (!provider) {
    showToast(t("Keine Quelle verfügbar", "No Source available"));
    return;
  }

  // Detect selected episodes that do not offer the chosen language. This is a
  // manual-download safeguard only — it never runs for Auto-Sync. With a
  // fallback group selected, an episode only counts as mismatched when it
  // offers none of the group's languages: falling back is the point, so a
  // missing first choice is not a problem to warn about.
  const map = window._epLangMap || {};
  const wanted = languageChainFor(language);
  const matched = [];
  const mismatched = [];
  episodes.forEach((url) => {
    const info = map[url];
    if (info && Array.isArray(info.languages) && info.languages.length &&
        !wanted.some((l) => info.languages.includes(l))) {
      mismatched.push(url);
    } else {
      matched.push(url);
    }
  });

  if (mismatched.length) {
    openLangMismatchModal(matched, mismatched, language, provider, languages);
    return;
  }

  await _submitDownloadGroups([{ episodes, language, provider, languages }]);
}

// Queue one or more {episodes, language, provider} groups in sequence.
// `languages` is optional and only set by the multi-language mode.
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

  // Seerr integration: approve the pending request before queuing the
  // download, mirroring the original seerr.js flow (best-effort -- the
  // download proceeds even if the approve call fails, see
  // _approveSeerrRequestIfPending()).
  if (_seerrModalContext && _seerrModalContext.isPending && _seerrModalContext.reqId) {
    await _approveSeerrRequestIfPending(_seerrModalContext);
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
      if (Array.isArray(g.languages) && g.languages.length > 1) {
        dlBody.languages = g.languages;
      }
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
      // Seerr integration: the original seerr.js flow closed the modal and
      // refreshed the request grid after a successful download so the
      // card's status pill updates right away -- keep that behavior here.
      if (_seerrModalContext) {
        closeModal();
        if (typeof seerrLoad === "function") seerrLoad();
      }
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

function openLangMismatchModal(matched, mismatched, language, provider, languages) {
  const overlayEl = document.getElementById("langMismatchOverlay");
  const listEl = document.getElementById("langMismatchList");
  if (!overlayEl || !listEl) {
    // Fallback: just queue the episodes that do offer the chosen language.
    _submitDownloadGroups([{ episodes: matched, language, provider, languages }]);
    return;
  }

  window._langMismatchCtx = { matched, mismatched, language, provider, languages };

  const titleEl = document.getElementById("langMismatchTitle");
  if (titleEl) titleEl.textContent = t("Sprache nicht verfügbar", "Language not available");
  const introEl = document.getElementById("langMismatchIntro");
  if (introEl) {
    // With a fallback group the episodes listed here offer none of its
    // languages, so the wording has to name the group, not one language.
    const label = languageLabelFor(language);
    introEl.textContent = String(language || "").startsWith("group:")
      ? t(
        `Für keine Sprache der Gruppe „${label}" sind ${mismatched.length} ausgewählte Episode(n) verfügbar. Wähle pro Episode eine andere Sprache oder überspringe sie.`,
        `${mismatched.length} selected episode(s) are available in none of the languages of the group "${label}". Pick another language per episode or skip it.`
      )
      : t(
        `Für die gewählte Sprache „${label}" sind ${mismatched.length} ausgewählte Episode(n) nicht verfügbar. Wähle pro Episode eine andere Sprache oder überspringe sie.`,
        `The selected language "${label}" is not available for ${mismatched.length} selected episode(s). Pick another language per episode or skip it.`
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
    // Only the episodes that DO offer the primary language keep the extra
    // track selection. The ones rerouted to a different language below become
    // ordinary single-language jobs: their new language is a replacement for
    // the primary, not a track to add to a file that was never created.
    groups.push({
      episodes: ctx.matched,
      language: ctx.language,
      provider: ctx.provider,
      languages: ctx.languages,
    });
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
  // Drop the multi-language selection with the modal: the next title has its
  // own set of languages, and carrying three ticks over into it would queue
  // tracks nobody asked for.
  langMultiOrder = [];
  setLangMultiActive(false);
  if (_seerrModalContext) {
    _seerrModalContext = null;
    _updateSeerrModalActions();
  }
}
function closeModalOutside(e) {
  if (e.target === overlay) closeModal();
}

// ---------------------------------------------------------------
// Seerr integration -- lets seerr.js open the standard modal for a
// series/movie tied to a Seerr request, so Discover/Home and the Seerr
// requests page share one modal implementation (see project memory
// mediaforge-seerr-modal-unification.md for background).
// ---------------------------------------------------------------

window.openSeriesFromSeerr = function (url, reqId, isPending, isMovie) {
  _seerrModalContext = { reqId: reqId || null, isPending: !!isPending, isMovie: !!isMovie };
  openSeries(url);
  _updateSeerrModalActions();
};

// Relabels the standard download buttons ("Approve & Download" instead of
// "Download" while the request is still pending) and shows/hides the
// Decline button injected into shared_modals.html's modal-actions bar.
// Called both when a Seerr-context modal opens and when it closes (to
// restore the normal Discover/Home labels for the next open).
function _updateSeerrModalActions() {
  const declineBtn = document.getElementById("modalDeclineBtn");
  if (!_seerrModalContext) {
    if (downloadSelectedBtn) downloadSelectedBtn.textContent = t("Ausgewählte herunterladen", "Download selected");
    if (downloadAllBtn) downloadAllBtn.textContent = t("Alle herunterladen", "Download all");
    if (declineBtn) declineBtn.style.display = "none";
    return;
  }
  const ctx = _seerrModalContext;
  if (downloadSelectedBtn) {
    downloadSelectedBtn.textContent = ctx.isPending
      ? t("Annehmen & Herunterladen", "Approve & Download")
      : t("Herunterladen", "Download");
  }
  if (downloadAllBtn) {
    downloadAllBtn.textContent = ctx.isPending
      ? t("Annehmen & alle herunterladen", "Approve & download all")
      : t("Alle herunterladen", "Download all");
  }
  if (declineBtn) {
    declineBtn.style.display = (ctx.reqId && typeof seerrCanDecline !== "undefined" && seerrCanDecline) ? "" : "none";
  }
}

// Best-effort: approves the Seerr request, warns via toast on failure but
// never blocks the download (matches the original seerrStartDownload()
// behavior in seerr.js before the modal unification).
async function _approveSeerrRequestIfPending(ctx) {
  try {
    const resp = await fetch(`/api/seerr/requests/${ctx.reqId}/approve`, { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      console.warn("Seerr approve failed:", resp.status, err);
      showToast("⚠ " + t("Seerr-Genehmigung fehlgeschlagen: ", "Seerr approval failed: ") + (err.error || resp.status));
    } else {
      ctx.isPending = false; // don't re-approve if the user downloads again from the same modal session
    }
  } catch (e) {
    console.warn("Seerr approve error:", e);
  }
}

// Wired to the Decline button injected into modal-actions by
// _updateSeerrModalActions(); delegates to seerr.js's existing decline flow.
function _declineSeerrFromModal() {
  if (!_seerrModalContext || !_seerrModalContext.reqId) return;
  if (typeof seerrDeclineRequest === "function") seerrDeclineRequest(_seerrModalContext.reqId);
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
    coverUrl: currentSeriesCoverUrl,
    customPaths: _customPathsCache,
    defaultCustomPathId: _autosyncDefaultPathId,
    languages: languageSelect
      ? Array.from(languageSelect.options)
          .map((o) => o.value)
          .filter((v) => v && v !== "All Languages")
      : null,
    currentLanguage: languageSelect ? languageSelect.value : null,
    currentProvider: providerSelect ? providerSelect.value : null,
    langSepEnabled: langSeparationEnabled,
    languageGroups: languageGroups,
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
    // The card variant stays on one line and truncates; the modal variant
    // wraps instead. A name like "Netflix Standard with Ads" is otherwise
    // either cut off or -- because a nowrap pill raises the row's min-content
    // width -- wide enough to push the modal past its max-width.
    'white-space:' + (opts.small ? 'nowrap' : 'normal'),
    'max-width:100%',
    'line-height:1.4',
    'cursor:default',
  ].concat(opts.small ? ['overflow:hidden'] : ['overflow-wrap:anywhere']).join(';');
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
async function _crCardPill(card, d, metaEl) {
  if (!crunchyrollSettings || crunchyrollSettings.enabled !== '1') return false;
  if (crunchyrollSettings.show_providers === '0') return false;
  if (d && d.providers && d.providers.some(pp => /crunchyroll/i.test(pp))) return true;
  // Prefer the canonical TMDB title — it lines up with Crunchyroll's catalog
  // better than the raw site title.
  const title = (d && d.title) || card.dataset.title || card.dataset.tmdbTitle || "";
  if (!title) return false;
  const meta = metaEl || _ensureCardMeta(card);
  if (!meta) return false;
  return _crProviderPill(title, meta, { small: true });
}

// Card-level Fernsehserien wrapper, mirroring _crCardPill. Only ever called
// as the last step of _cardProviderChain (TMDB and Crunchyroll both empty),
// so this does not add extra load against the scraper for cards that are
// already covered by TMDB or Crunchyroll.
async function _fsCardPill(card, d, metaEl) {
  if (!fernsehserienSettings || fernsehserienSettings.enabled !== '1') return false;
  if (fernsehserienSettings.show_providers === '0') return false;
  const title = (d && d.title) || card.dataset.title || card.dataset.tmdbTitle || "";
  if (!title) return false;
  const meta = metaEl || _ensureCardMeta(card);
  if (!meta) return false;
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
  // opts.only restricts this to ONE registered resolver (by its name) — that's
  // how the configurable CineInfo order addresses a single module's pill; with
  // no `only`, every resolver is tried in registration order (legacy behaviour).
  const entries = opts.only
    ? window._providerPillResolvers.filter(e => e.name === opts.only)
    : window._providerPillResolvers;
  for (const entry of entries) {
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

// Card-level wrapper, mirroring _crCardPill/_fsCardPill. `only` names a single
// registered resolver, so the chain can place each module's pill at exactly the
// position the user configured.
async function _extensionCardPill(card, d, metaEl, only) {
  const title = (d && d.title) || card.dataset.title || card.dataset.tmdbTitle || "";
  if (!title) return false;
  const meta = metaEl || _ensureCardMeta(card);
  if (!meta) return false;
  return _extensionProviderPill(title, meta, { small: true, imdbId: d && d.imdb_id, only: only });
}

// ─── CineInfo provider order ────────────────────────────────────────────
// Which source gets to put its pill on a card/modal first is user-configurable
// (Integrations → CineInfo → "Provider order"): a comma-separated list of
// source ids, stored as cineinfo_provider_order and served with the CineInfo
// settings. Built-in ids are "tmdb", "crunchyroll" and "fernsehserien"; a
// module's pill (registered via registerProviderPill(), see below) is
// addressed as "ext:<its registered name>".
//
// The order is a *preference*, not a whitelist: any source the saved order
// doesn't mention (a module installed after the order was saved, say) is still
// tried, appended after the configured ones — so a new module's pill shows up
// on its own, and nothing silently disappears.
const _PILL_SOURCES_DEFAULT = ["tmdb", "crunchyroll", "fernsehserien"];

function _registeredPillIds() {
  return (window._providerPillResolvers || []).map(e => "ext:" + e.name);
}

function _pillSources() {
  const known = _PILL_SOURCES_DEFAULT.concat(_registeredPillIds());
  const raw = (cineinfoSettings && cineinfoSettings.provider_order) || "";
  const configured = raw.split(",").map(s => s.trim()).filter(s => s && known.includes(s));
  return configured.concat(known.filter(id => !configured.includes(id)));
}

// TMDB's own pill on a browse card: the top provider, with the full list as a
// tooltip. Same _makeProviderPill styling as every other source, so all pills
// look identical no matter which source supplied the name.
function _tmdbCardPill(meta, d) {
  if (!meta || !d || !d.found) return false;
  if (!cineinfoSettings || cineinfoSettings.show_providers === '0') return false;
  if (!d.providers || !d.providers.length) return false;
  if (meta.querySelector('.tmdb-provider-pill')) return true;
  meta.appendChild(_makeProviderPill(d.providers[0], { small: true, title: d.providers.join(', ') }));
  return true;
}

// Provider resolution chain for browse cards. Walks _pillSources() and stops
// at the first source that actually produced a pill for this title.
async function _cardProviderChain(card, d, metaEl) {
  const meta = metaEl || _ensureCardMeta(card);
  if (!meta) return;
  for (const id of _pillSources()) {
    let added = false;
    if (id === "tmdb") {
      added = _tmdbCardPill(meta, d);
    } else if (id === "crunchyroll") {
      added = await _crCardPill(card, d, meta);
    } else if (id === "fernsehserien") {
      added = await _fsCardPill(card, d, meta);
    } else if (id.startsWith("ext:")) {
      added = await _extensionCardPill(card, d, meta, id.slice(4));
    }
    if (added) return;
  }
}

// The .browse-tmdb-meta container a card's pills live in, created on demand.
function _ensureCardMeta(card) {
  if (!card) return null;
  const info = card.querySelector('.browse-info');
  if (!info) return null;
  let meta = info.querySelector('.browse-tmdb-meta');
  if (!meta) {
    meta = document.createElement('div');
    meta.className = 'browse-tmdb-meta';
    info.appendChild(meta);
  }
  return meta;
}

// TMDB's provider block in the detail modal: every provider as a pill, in a
// box that scrolls once the list outgrows it. Returns true iff it rendered
// anything.
//
// This used to cap the list at six and append a "+N more" chip, because the
// box clipped at a fixed height and the rest was simply unreachable. Now that
// the box scrolls, a cap would only hide providers behind a chip that says
// nothing about them -- so every provider is rendered and the user scrolls.
// (That chip was also a hardcoded German string, untranslated.)
function _tmdbModalPills(provEl, d) {
  if (!provEl || !d || !d.found) return false;
  if (!cineinfoSettings || cineinfoSettings.show_providers === '0') return false;
  if (!d.providers || !d.providers.length) return false;

  provEl.innerHTML = '';
  provEl.style.cssText = [
    'display:flex',
    'flex-wrap:wrap',
    'align-content:flex-start',
    'gap:5px',
    'margin:4px 0 16px',
    // Roughly three rows of pills. Past that the block starts crowding out the
    // rest of the modal, which is what the height limit is here for.
    'max-height:104px',
    'overflow-y:auto',
    // Keeps a flick at the end of the list from scrolling the modal behind it.
    'overscroll-behavior:contain',
    'position:relative',
  ].join(';');
  // The scrollbar itself is hidden via the .tmdb-providers rule in
  // queue.css -- ::-webkit-scrollbar has no inline-style equivalent.
  d.providers.forEach(p => provEl.appendChild(_makeProviderPill(p)));
  return true;
}

// Detail-modal counterpart of _cardProviderChain: same configured order, same
// "first source that has something wins" rule. `staleFn` lets the caller abort
// when the user has already opened a different series while we were awaiting.
async function _modalProviderChain(title, provEl, d, imdbId, staleFn) {
  const stale = staleFn || (() => false);
  for (const id of _pillSources()) {
    let added = false;
    if (id === "tmdb") {
      added = _tmdbModalPills(provEl, d);
    } else if (id === "crunchyroll") {
      added = await _crProviderPill((d && d.title) || title, provEl);
    } else if (id === "fernsehserien") {
      added = await _enqueueFsLookup((d && d.title) || title, provEl);
    } else if (id.startsWith("ext:")) {
      added = await _extensionProviderPill((d && d.title) || title, provEl, {
        imdbId: imdbId || (d && d.imdb_id),
        only: id.slice(4),
      });
    }
    if (stale()) return;
    if (added) return;
  }
}

// ── Modal backdrop ───────────────────────────────────────────────────────
// TMDB already ships the image path inside `raw_details` (see
// web/tmdb_cache.py), so this costs no extra lookup -- and it goes through the
// image proxy like every other remote image, which is what keeps the browser
// from talking to image.tmdb.org directly.
// The picture reaches down to the bottom of the header text, so its height is
// whatever that text happens to need -- which CSS cannot express: the header
// grows when the genre chips and provider pills arrive from TMDB, when the
// user expands "show more", and on every resize. So measure it, publish the
// result as --mf-backdrop-h (modals.css reads it), and keep watching.
let _mfBackdropRO = null;

function _mfSyncBackdropHeight() {
  const modal = document.getElementById("modal");
  const header = modal && modal.querySelector(".modal-header");
  if (!modal || !header || !modal.classList.contains("has-backdrop")) return;
  const top = modal.getBoundingClientRect().top;
  const bottom = header.getBoundingClientRect().bottom;
  // A closed modal measures 0 -- leave the CSS fallback in place rather than
  // writing a 0px height that would hide the picture on the next open.
  if (bottom <= top) return;
  // +10px so the gradient's final fade lands just below the last line of text
  // instead of cutting through it.
  modal.style.setProperty("--mf-backdrop-h", Math.round(bottom - top + 10) + "px");
}

function _mfWatchBackdropHeight(on) {
  const modal = document.getElementById("modal");
  const header = modal && modal.querySelector(".modal-header");
  if (_mfBackdropRO) {
    _mfBackdropRO.disconnect();
    _mfBackdropRO = null;
  }
  if (!on || !header) return;
  if (typeof ResizeObserver === "function") {
    _mfBackdropRO = new ResizeObserver(_mfSyncBackdropHeight);
    _mfBackdropRO.observe(header);
  }
  _mfSyncBackdropHeight();
}

function _mfSetBackdrop(path) {
  const el = document.getElementById("modalBackdrop");
  const modal = document.getElementById("modal");
  if (!el) return;
  if (!path) {
    el.style.backgroundImage = "";
    el.classList.remove("is-on");
    if (modal) {
      modal.classList.remove("has-backdrop");
      // Drop the measured height too: a later title whose header is shorter
      // would otherwise inherit this one's picture height for a frame.
      modal.style.removeProperty("--mf-backdrop-h");
    }
    _mfWatchBackdropHeight(false);
    return;
  }
  const url = proxyImg("https://image.tmdb.org/t/p/w780" + path);
  // Preload: switching the class before the image is decoded shows an empty
  // band first and then pops the picture in.
  const img = new Image();
  img.onload = function () {
    el.style.backgroundImage = 'url("' + url.replace(/"/g, "%22") + '")';
    el.classList.add("is-on");
    if (modal) modal.classList.add("has-backdrop");
    _mfWatchBackdropHeight(true);
  };
  img.src = url;
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
    // TMDB is off entirely — the chain simply skips it and runs the rest of
    // the configured order (Crunchyroll, Fernsehserien.de, module pills).
    await _modalProviderChain(title, provEl, null, imdbId, _stale);
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
      // No TMDB data at all — the chain skips TMDB and runs the rest of the
      // configured order (Crunchyroll, Fernsehserien.de, module pills).
      await _modalProviderChain(title, provEl, null, imdbId, _stale);
      return;
    }
    // Provider resolution follows the configured CineInfo order (TMDB,
    // Crunchyroll, Fernsehserien.de and any module-registered pill, see
    // _pillSources()): the first source that actually has something for this
    // title renders its pill(s), the rest are skipped — so there is never a
    // redundant or conflicting second pill next to TMDB's own list.
    await _modalProviderChain(title, provEl, d, imdbId, _stale);
    if (_stale()) return;
    // Backdrop as the modal's header image. Off by option, off without an
    // image -- and `!== '0'` rather than `=== '1'` because this one defaults
    // to on, like the trailer and the recommendations.
    if ((cineinfoSettings.show_backdrop ?? '1') !== '0') {
      const _bd = (d.raw_details && d.raw_details.backdrop_path) || "";
      if (_bd) _mfSetBackdrop(_bd);
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
          'background:rgba(0,0,0,.45)',
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
      // The key comes from the TMDB response and goes straight into an
      // attribute, so it is validated against YouTube's id charset rather than
      // merely escaped -- anything else is not a video id anyway.
      const trailerKey = /^[A-Za-z0-9_-]{5,20}$/.test(String(d.trailer_key || ''))
        ? String(d.trailer_key) : '';
      if (showT && trailerKey) {
        trailerEl.innerHTML = `<iframe src="https://www.youtube.com/embed/${trailerKey}" allowfullscreen></iframe>`;
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

// HTML-escape a value for interpolation into markup — including into an
// ATTRIBUTE, which is what most callers here do (title="${esc(x)}",
// data-url="${esc(x)}").
//
// It used to escape via div.textContent -> innerHTML, which escapes & and < but
// NOT quotes: `title="${esc(x)}"` with x = '" onmouseover=alert(1) x="' broke
// straight out of the attribute. It is now the same escaping as escapeHtml()
// (which does cover " and '), so the two can't disagree — and a module copying
// the shorter name out of the core, which is exactly what happened, gets the
// safe one.
//
// unesc() first: values reaching this are often already HTML-entity-encoded by
// the source site's markup ("Tom &amp; Jerry"), and decoding before re-escaping
// is what keeps them from rendering as literal entities.
function esc(s) {
  return escapeHtml(unesc(s));
}

const downloadAllLangsBtn = document.getElementById("downloadAllLangsBtn");
let defaultSyncLanguage = "German Dub";

async function checkLangSeparation() {
  try {
    const resp = await fetch("/api/settings");
    const data = await resp.json();
    langSeparationEnabled = data.lang_separation === "1";
    // Language fallback groups are offered alongside the plain languages in
    // every language dropdown (see rebuildLanguageSelect).
    languageGroups = data.language_groups || [];
    if (languageSelect && languageSelect.options.length) rebuildLanguageSelect();
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
let _currentFavIsMovie = false;

async function _updateFavouriteBtn(url, title, posterUrl, isMovie) {
  _currentFavUrl = url;
  _currentFavTitle = title;
  _currentFavPoster = posterUrl;
  _currentFavIsMovie = !!isMovie;
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
      // Send metadata so the favourites page can badge & group by type,
      // source and language. media_type is derived from is_movie; language is
      // the currently selected language in the detail modal (best-effort).
      const _favLang =
        typeof languageSelect !== "undefined" && languageSelect
          ? languageSelect.value
          : null;
      await fetch("/api/favourites", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          series_url: _currentFavUrl,
          title: _currentFavTitle,
          poster_url: _currentFavPoster,
          media_type: _currentFavIsMovie ? "movie" : "series",
          language: _favLang,
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

// Ask the backend what the pasted link actually is BEFORE probing it.
// The site lookup runs server-side (/api/direct-link/classify) against the
// very same URL patterns the rest of the app uses (mediaforge/providers.py),
// so it covers every supported site — including FilmPalast, which the old
// hard-coded frontend regexes silently missed — and every mirror domain
// (serienstream.to, a bare origin IP, ...), which are normalized back to the
// canonical host. Only a link that is NOT one of our sites falls through to
// the generic yt-dlp probe (which itself first tries the supported hosters in
// the configured provider order, see models/direct_link/probe.py).
async function submitDirectLink() {
  const input = document.getElementById("directLinkInput");
  const error = document.getElementById("directLinkError");
  let url = input.value.trim();

  // Normalize: strip trailing slash
  url = url.replace(/\/+$/, "");

  error.textContent = "";
  error.style.display = "none";

  if (!url) return;

  if (!/^https?:\/\//i.test(url)) {
    error.textContent = t("Bitte eine gültige URL eingeben.", "Please enter a valid URL.");
    error.style.display = "block";
    input.focus();
    return;
  }

  let cls = null;
  try {
    const resp = await fetch("/api/direct-link/classify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    cls = await resp.json();
  } catch (e) {
    cls = null;  // backend unreachable — fall through to the generic probe
  }

  if (cls && cls.kind === "site") {
    // hanime is an adult source: a direct link must not bypass the 18+ gate.
    if (cls.source === "hanime") {
      const _hanOn = ((((generalSettings || {}).sources || {}).enabled || {}).hanime === "1");
      if (!_hanOn) {
        error.textContent = t("hanime ist deaktiviert. Bitte zuerst in den Einstellungen aktivieren (18+).", "hanime is disabled. Please enable it in Settings first (18+).");
        error.style.display = "block";
        input.focus();
        return;
      }
    }
    closeDirectLinkModal();
    openSeries(cls.series_url || url);
    return;
  }

  // Not one of the known scraper sites -- try it as a generic direct link
  // (a supported hoster embed page, or a raw .m3u8 HLS master playlist).
  // MediaForge fetches the available quality variants first so the user can
  // pick one, instead of just guessing "best" (see GitHub issue #8).
  startDirectLinkProbe((cls && cls.url) || url);
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
  // When set (e.g. from the favourites page's "Add to Auto-Sync" action), open
  // the Auto-Sync config right after the series modal finishes loading, so the
  // whole job-creation flow stays in the canonical detail-modal UI.
  const wantAutosync = params.get("autosync") === "1";

  if (openUrl || searchQuery) {
    // Remove query param from browser history without reload
    const cleanUrl = window.location.pathname;
    window.history.replaceState({}, "", cleanUrl);

    const action = async () => {
      if (openUrl) {
        await openSeries(decodeURIComponent(openUrl));
        if (wantAutosync && typeof openAutoSyncConfig === "function") {
          // Only meaningful for series; openAutoSyncConfig no-ops without a
          // series URL / when the filter module is unavailable.
          openAutoSyncConfig();
        }
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

// ── Advanced Search ─────────────────────────────────────────────────────────
// Moved out of this file in July 2026: the ~1100 lines that drove the TMDB
// Discover page now live in static/advanced_search.js, which only
// templates/advanced_search.html loads. They used to be parsed on every page
// of the app. The AniWorld search modal below stays here — it is opened from
// the index page and the Seerr page too, not only from the Advanced Search.

// ---- AniWorld Search Modal Logic ----

function openAniSearchModal(title, tmdbId, type, posterPath, presetLocalizedTitle) {
  const modal = document.getElementById('aniSearchModalOverlay');
  if (!modal) return;
  modal.style.display = 'flex';
  document.body.style.overflow = 'hidden';

  const cleanTitle = title.trim().replace(/!+$/, "");
  document.getElementById('aniSearchTitle').textContent = t(`Suche nach "${cleanTitle}"...`, `Searching for "${cleanTitle}"...`);
  const _sp = document.getElementById('aniSearchSpinner');
  // Back to the neutral placeholder on every open: runAniSearch() replaces it
  // with the source list it resolves, and without this reset the SECOND open
  // would still be showing the first search's list while the new one is being
  // resolved.
  if (_sp) {
    if (_sp.dataset.defaultText) _sp.textContent = _sp.dataset.defaultText;
    _sp.style.display = 'block';
  }
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
    // Every source that exists right now -- built-in or module-registered --
    // in the user's own order, exactly like the main search box (doSearch()).
    // This used to be four hardcoded ids (aniworld, sto, filmpalast, megakino),
    // which meant this modal could not find a title on filmo, 9anime, Aniwaves
    // or on ANY source a module registers: a module could add a provider to the
    // app and its own titles would still be unreachable from the one dialog
    // that starts a download. It also asked disabled sources, so switching a
    // source off in Settings changed nothing here.
    //
    // Adult sources stay opt-in (an 18+ result appearing in a lookup nobody
    // asked for is not a missing feature), and everything else follows the
    // same "hide_disabled_in_search" preference the main search honours.
    let _srcSettings = {};
    try { _srcSettings = ((await loadGeneralSettings()) || {}).sources || {}; } catch (e) { _srcSettings = {}; }
    const _en = _srcSettings.enabled || {};
    const _hide = _srcSettings.hide_disabled_in_search === "1";
    const _sources = _sortSourcesByUserOrder(await loadSearchSources(), _srcSettings.order)
      .filter(function (src) {
        // searchable === false: provider-only module source (URL resolution,
        // no search function). Listed on the Sources tab, never queried here.
        if (src.searchable === false) return false;
        const on = _sourceIsOn(src, _en);
        return src.adult ? on : (on || !_hide);
      });

    // Say which sources are being asked, now that we know. The template's
    // placeholder is source-agnostic (it used to name three sites and went
    // stale the moment this became dynamic), and "searching AniWorld,
    // SerienStream, filmo.to" is the answer to the only question a user has
    // while a spinner runs.
    const _spinner = document.getElementById('aniSearchSpinner');
    if (_spinner && _sources.length) {
      const _names = _sources.map(s => s.label || s.id).join(", ");
      _spinner.textContent = t("Suche Streams auf ", "Searching streams on ") + _names + "…";
    }

    let allPromises = [];
    searchTitles.forEach(kw => {
      _sources.forEach(src => allPromises.push(searchSite(src.id, kw)));
    });

    let displayTitle = primaryCleaned;
    if (localizedCleaned && localizedCleaned.toLowerCase() !== primaryCleaned.toLowerCase()) {
      displayTitle += ` / ${localizedCleaned}`;
    }
    if (enCleaned && enCleaned.toLowerCase() !== primaryCleaned.toLowerCase() && enCleaned.toLowerCase() !== localizedCleaned.toLowerCase()) {
      displayTitle += ` / ${enCleaned}`;
    }
    document.getElementById('aniSearchTitle').textContent = t(`Ergebnisse für "${displayTitle}"`, `Results for "${displayTitle}"`);

    const normalizeForCompare = (str) => {
      return str.toLowerCase()
                .replace(/’/g, "'")
                .replace(/[–—―]/g, "-")
                .replace(/\s*-\s*/g, "-") // remove spaces around hyphens first
                .replace(/-/g, " ")       // treat hyphens as spaces
                .replace(/\s+/g, " ")
                .trim();
    };

    // HARD FILTER: Only keep results where title contains ANY of our keywords, or keyword contains title (apostrophe-insensitive & hyphen-insensitive)
    const titleMatches = (r) => {
      if (!r.title) return false;
      const tNorm = normalizeForCompare(r.title);
      return searchTitles.some(kw => {
        const kNorm = normalizeForCompare(kw);
        if (tNorm.includes(kNorm) || kNorm.includes(tNorm)) return true;
        const tNoApos = tNorm.replace(/'/g, "");
        const kNoApos = kNorm.replace(/'/g, "");
        return tNoApos.includes(kNoApos) || kNoApos.includes(tNoApos);
      });
    };

    const seenUrls = new Set();
    let painted = 0;
    const appendResult = (r) => {
      if (!r || !r.url || seenUrls.has(r.url)) return;   // dedup across sources AND title variants
      seenUrls.add(r.url);
      if (!titleMatches(r)) return;
      // The loading skeleton stays up until the first real hit lands.
      if (!painted++) grid.innerHTML = '';

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
    };

    // Every answer is painted as it arrives instead of after Promise.all.
    // This modal fans out over every source AND every title variant, so it
    // waits on the slowest of N×M requests -- up to searchSite()'s 15 s
    // timeout for a dead site, during which hits that were already in hand
    // stayed invisible.
    await Promise.all(allPromises.map(p =>
      p.catch(() => []).then(arr => (arr || []).forEach(appendResult))
    ));

    document.getElementById('aniSearchSpinner').style.display = 'none';

    // Only after every source has answered -- an empty state while requests
    // are still running would claim "nothing found" too early.
    if (!painted) {
      // Names the sources that were ACTUALLY asked rather than the three that
      // used to be hardcoded here. The lookup now fans out over every enabled
      // source (see above), so a fixed list of names was both wrong and the
      // one thing that could make a user think a source they enabled was
      // never queried.
      const _asked = _sources.map(s => s.label || s.id).join(", ");
      grid.innerHTML = `<div class="adv-empty-state">${t("Keine exakten Treffer für " + escapeHtml(displayTitle) + " auf " + escapeHtml(_asked) + " gefunden.", "No exact matches for " + escapeHtml(displayTitle) + " found on " + escapeHtml(_asked) + ".")}</div>`;
    }

  } catch (e) {
    document.getElementById('aniSearchSpinner').style.display = 'none';
    grid.innerHTML = `<div class="adv-empty-state" style="color:var(--error)">${t("Fehler bei der Suche.", "Error during search.")}</div>`;
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
  // Delegates to the shared escaper (static/mf_escape.js). The local version
  // missed '>' and returned '' for every falsy value, so an episode number 0
  // or a size of 0 MB vanished from the output.
  return window.mfEscape(unsafe);
}
