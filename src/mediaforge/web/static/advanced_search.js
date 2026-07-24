/* ===================================================================
   MediaForge — Advanced Search (TMDB Discover)

   Reworked July 2026. Previously ~1100 lines living inside the shared
   app.js, which every page of the app downloads and parses; this file is
   loaded by templates/advanced_search.html only.

   What changed, besides the move:
     * one module-scoped state object instead of ~15 globals on window,
     * event delegation instead of inline onclick="" (the tag/chip markup
       used to interpolate values straight into an onclick attribute),
     * generic multi-select / token-field helpers shared by all six of the
       autocomplete + checkbox filters (see .mf-* classes in forms.css),
     * numbered pagination clamped to TMDB's real 500-page window,
     * season counts fetched in ONE batched request per page instead of
       one request per card,
     * every user-facing string goes through t(de, en) — see base.html.

   Depends on app.js for the shared helpers openAniSearchModal(),
   addDownloadedBadgeForTmdb(), addSyncBadgeForTmdb(), loadAutoSyncJobs(),
   loadDownloadedFolders(), loadCineinfoSettings() and loadGeneralSettings();
   every call is guarded so a missing helper degrades instead of throwing.
   =================================================================== */

(function () {
  "use strict";

  // The page is only rendered when the "Advanced Search" feature is enabled,
  // and app.js is loaded on plenty of other pages — bail out immediately
  // unless this really is the Advanced Search.
  if (!document.getElementById("advFilterMenu")) return;

  // ── Constants ──────────────────────────────────────────────────────────
  var STORAGE_KEY = "advSearchState";
  var STORAGE_VERSION = 2;
  var FILTER_TTL_MS = 24 * 60 * 60 * 1000; // filters survive a day
  var RESULT_TTL_MS = 5 * 60 * 1000;       // cached results only 5 minutes
  var TMDB_PAGE_SIZE = 20;                 // items per upstream discover page
  var TMDB_MAX_PAGES = 500;                // TMDB's own hard limit
  var TMDB_MAX_RESULTS = TMDB_PAGE_SIZE * TMDB_MAX_PAGES;
  var MAX_CACHED_PAGES = 10;               // cap what we write to localStorage

  var TABS = ["basics", "quality", "streaming", "details"];

  // ── State ──────────────────────────────────────────────────────────────
  var S = {
    tab: "basics",
    type: "tv",
    genres: [],          // included genre ids (numbers)
    genresExcluded: [],  // excluded genre ids (numbers)
    keywords: [],        // [{ id, name }]
    incProviders: [],    // [{ provider_id, provider_name }]
    excProviders: [],
    networks: [],        // [{ id, name }]
    statuses: [],        // TMDB with_status codes, as strings
    region: "",
    originalLanguage: "",
    yearMin: "",
    yearMax: "",
    runtimeMin: "",
    runtimeMax: "",
    voteMin: 0,
    voteCountMin: "",
    sortBy: "popularity.desc",
    /* Result buffer. Sparse map of TMDB page number -> its 20 items, NOT a
       flat array: the pager can jump straight to page 400, and a flat buffer
       would have to walk every page in between (500 sequential requests for
       one click on "last page"). With the map we fetch only the one or two
       upstream pages a local grid page actually covers. */
    pages: {},
    total: 0,
    pageIndex: 0,
    paramsStr: null,
  };

  var allGenres = { tv: [], movie: [] };
  var allWatchProviders = [];
  var allNetworks = [];
  var inFlight = {};      // tmdb page number -> Promise, de-dupes concurrent fetches
  var searchSeq = 0;      // generation counter; stale responses are discarded
  var lastError = null;
  var lastPageSize = 0;

  // ── Small helpers ──────────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  var numberFormat = new Intl.NumberFormat(window.__LANG === "de" ? "de-DE" : "en-US");
  function fmtNumber(value) { return numberFormat.format(value || 0); }

  function callIfPresent(name) {
    var fn = window[name];
    if (typeof fn !== "function") return null;
    try {
      return fn.apply(null, Array.prototype.slice.call(arguments, 1));
    } catch (e) {
      console.debug("[AdvSearch] helper " + name + " failed", e);
      return null;
    }
  }

  /* Translate the error codes /api/tmdb/* returns. The backend deliberately
     sends a short English message plus a stable code instead of the raw
     upstream text (which carries the TMDB API key) — the UI localises it. */
  function translateError(payload) {
    var code = payload && payload.code;
    if (code === "no_api_key") return t("Kein TMDB API-Key hinterlegt. Bitte in den Integrationen eintragen.", "No TMDB API key configured. Add one under Integrations.");
    if (code === "tmdb_unauthorized") return t("TMDB hat den API-Key abgelehnt.", "TMDB rejected the API key.");
    if (code === "tmdb_rate_limited") return t("TMDB-Limit erreicht. Bitte kurz warten und erneut suchen.", "TMDB rate limit reached. Please wait a moment and search again.");
    if (code === "tmdb_timeout") return t("Zeitüberschreitung bei der TMDB-Anfrage.", "The TMDB request timed out.");
    if (code === "tmdb_not_found") return t("TMDB hat für diese Anfrage nichts gefunden.", "TMDB found nothing for this request.");
    if (code) return t("Die TMDB-Anfrage ist fehlgeschlagen.", "The TMDB request failed.");
    return t("Netzwerkfehler bei der Suche.", "Network error in search.");
  }

  // ═══════════════════════════════════════════════════════════════════════
  //  Persistence
  // ═══════════════════════════════════════════════════════════════════════

  /* Only the fields the grid actually renders are persisted. The old version
     stored raw TMDB objects (overview, backdrops, genre_ids, ...) for every
     loaded page, which pushed several hundred KB into localStorage. */
  function slimResult(r) {
    return {
      id: r.id,
      title: r.title,
      name: r.name,
      poster_path: r.poster_path,
      vote_average: r.vote_average,
      vote_count: r.vote_count,
      release_date: r.release_date,
      first_air_date: r.first_air_date,
      // Kept for the Details modal — the old page only ever showed a
      // truncated version of this in a :hover overlay.
      overview: r.overview,
      genre_ids: r.genre_ids,
      original_language: r.original_language,
    };
  }

  /* Keep the pages closest to the current view; a jump to page 400 must not
     push 400 pages' worth of posters into localStorage. */
  function pagesToPersist() {
    var numbers = Object.keys(S.pages).map(Number).filter(function (n) { return !isNaN(n); });
    if (numbers.length <= MAX_CACHED_PAGES) return numbers;
    var centre = Math.floor((S.pageIndex * getPageSize()) / TMDB_PAGE_SIZE) + 1;
    numbers.sort(function (a, b) { return Math.abs(a - centre) - Math.abs(b - centre); });
    return numbers.slice(0, MAX_CACHED_PAGES);
  }

  /* resultsFreshAt tracks when the buffer was actually (re)fetched, separate
     from the filter timestamp — otherwise merely opening the page rewrote
     savedAt and the 5-minute result TTL would never expire. */
  var resultsFreshAt = 0;

  function saveState() {
    try {
      var persisted = {};
      pagesToPersist().forEach(function (n) {
        persisted[n] = (S.pages[n] || []).map(slimResult);
      });
      var payload = {
        v: STORAGE_VERSION,
        savedAt: Date.now(),
        filters: {
          tab: S.tab,
          type: S.type,
          genres: S.genres,
          genresExcluded: S.genresExcluded,
          keywords: S.keywords,
          incProviders: S.incProviders,
          excProviders: S.excProviders,
          networks: S.networks,
          statuses: S.statuses,
          region: S.region,
          originalLanguage: S.originalLanguage,
          yearMin: S.yearMin,
          yearMax: S.yearMax,
          runtimeMin: S.runtimeMin,
          runtimeMax: S.runtimeMax,
          voteMin: S.voteMin,
          voteCountMin: S.voteCountMin,
          sortBy: S.sortBy,
        },
        results: {
          freshAt: resultsFreshAt,
          pages: persisted,
          total: S.total,
          pageIndex: S.pageIndex,
          paramsStr: S.paramsStr,
        },
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
    } catch (e) {
      // Quota exceeded / storage disabled — the page works fine without it.
      console.debug("[AdvSearch] state not persisted", e);
    }
  }

  function loadState() {
    var raw;
    try {
      raw = localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      return;
    }
    if (!raw) return;

    var saved;
    try {
      saved = JSON.parse(raw);
    } catch (e) {
      try { localStorage.removeItem(STORAGE_KEY); } catch (e2) {}
      return;
    }

    if (!saved || saved.v !== STORAGE_VERSION) {
      try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
      return;
    }

    var age = Date.now() - (saved.savedAt || 0);
    if (age > FILTER_TTL_MS) {
      try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
      return;
    }

    var f = saved.filters || {};
    S.tab = TABS.indexOf(f.tab) !== -1 ? f.tab : "basics";
    S.type = f.type === "movie" ? "movie" : "tv";
    S.genres = Array.isArray(f.genres) ? f.genres.map(Number).filter(function (n) { return !isNaN(n); }) : [];
    S.genresExcluded = Array.isArray(f.genresExcluded) ? f.genresExcluded.map(Number).filter(function (n) { return !isNaN(n); }) : [];
    S.keywords = Array.isArray(f.keywords) ? f.keywords : [];
    S.incProviders = Array.isArray(f.incProviders) ? f.incProviders : [];
    S.excProviders = Array.isArray(f.excProviders) ? f.excProviders : [];
    S.networks = Array.isArray(f.networks) ? f.networks : [];
    S.statuses = Array.isArray(f.statuses) ? f.statuses.map(String) : [];
    S.region = f.region || "";
    S.originalLanguage = f.originalLanguage || "";
    S.yearMin = f.yearMin || "";
    S.yearMax = f.yearMax || "";
    S.runtimeMin = f.runtimeMin || "";
    S.runtimeMax = f.runtimeMax || "";
    S.voteMin = parseFloat(f.voteMin) || 0;
    S.voteCountMin = f.voteCountMin || "";
    S.sortBy = f.sortBy || "popularity.desc";

    // Results are far more perishable than the filter selection, and their
    // age is measured from the last actual fetch, not from the last save.
    var r = saved.results || {};
    var resultAge = Date.now() - (r.freshAt || 0);
    if (resultAge <= RESULT_TTL_MS && r.pages && Object.keys(r.pages).length) {
      S.pages = {};
      Object.keys(r.pages).forEach(function (n) {
        if (Array.isArray(r.pages[n])) S.pages[n] = r.pages[n];
      });
      resultsFreshAt = r.freshAt || 0;
      S.total = r.total || 0;
      S.pageIndex = r.pageIndex || 0;
      S.paramsStr = r.paramsStr || null;
    }
  }

  // ═══════════════════════════════════════════════════════════════════════
  //  Generic controls
  // ═══════════════════════════════════════════════════════════════════════

  /* Multi-select dropdown (.mf-multiselect). One document-level listener
     closes whichever dropdown is open; each trigger only toggles its own. */
  function initMultiSelect(rootId, onChange) {
    var root = $(rootId);
    if (!root) return;
    var trigger = root.querySelector(".mf-multiselect-trigger");
    var dropdown = root.querySelector(".mf-multiselect-dropdown");
    if (!trigger || !dropdown) return;

    trigger.addEventListener("click", function (e) {
      e.stopPropagation();
      var willOpen = !root.classList.contains("is-open");
      document.querySelectorAll(".mf-multiselect.is-open").forEach(function (el) {
        el.classList.remove("is-open");
        var t2 = el.querySelector(".mf-multiselect-trigger");
        if (t2) t2.setAttribute("aria-expanded", "false");
      });
      root.classList.toggle("is-open", willOpen);
      trigger.setAttribute("aria-expanded", willOpen ? "true" : "false");
    });

    // Checkbox changes bubble; the <label> wrapper handles the click itself.
    dropdown.addEventListener("change", function (e) {
      if (e.target && e.target.matches('input[type="checkbox"]')) onChange();
    });
  }

  document.addEventListener("click", function (e) {
    document.querySelectorAll(".mf-multiselect.is-open").forEach(function (el) {
      if (el.contains(e.target)) return;
      el.classList.remove("is-open");
      var trigger = el.querySelector(".mf-multiselect-trigger");
      if (trigger) trigger.setAttribute("aria-expanded", "false");
    });
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    document.querySelectorAll(".mf-multiselect.is-open").forEach(function (el) {
      el.classList.remove("is-open");
      var trigger = el.querySelector(".mf-multiselect-trigger");
      if (trigger) trigger.setAttribute("aria-expanded", "false");
    });
    // Close through each token field's own handler so its suggestion buffer
    // is cleared too — otherwise a later Enter would pick an invisible entry.
    tokenFieldClosers.forEach(function (close) { close(); });
  });

  // Populated by initTokenField() so the global Escape handler above can reach
  // each field's close() without any of them being globally scoped.
  var tokenFieldClosers = [];

  /* Token field (.mf-token-field): an input with an async or in-memory
     suggestion source, arrow-key navigation, and removable tokens below.
     Used by Keywords, Include/Exclude Providers and Networks. */
  function initTokenField(config) {
    var input = $(config.inputId);
    var box = $(config.suggestionsId);
    if (!input || !box) return;

    var suggestions = [];
    var activeIndex = -1;
    var debounceTimer = null;

    function close() {
      box.classList.remove("is-open");
      input.setAttribute("aria-expanded", "false");
      suggestions = [];
      activeIndex = -1;
    }
    tokenFieldClosers.push(close);

    function renderSuggestions(items) {
      suggestions = items || [];
      activeIndex = -1;
      if (!suggestions.length) {
        box.innerHTML = '<div class="mf-token-suggestion" data-empty="1">' +
          esc(t("Keine Ergebnisse", "No results")) + "</div>";
        box.classList.add("is-open");
        input.setAttribute("aria-expanded", "true");
        return;
      }
      box.innerHTML = suggestions.map(function (item, idx) {
        return '<div class="mf-token-suggestion" role="option" data-idx="' + idx + '">' +
          esc(config.labelOf(item)) + "</div>";
      }).join("");
      box.classList.add("is-open");
      input.setAttribute("aria-expanded", "true");
    }

    function pick(item) {
      if (!item) return;
      config.onPick(item);
      input.value = "";
      close();
    }

    function query() {
      var q = input.value.trim();
      if (q.length < (config.minChars || 1)) { close(); return; }
      if (config.localSource) {
        renderSuggestions(config.localSource(q));
        return;
      }
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () {
        config.remoteSource(q).then(renderSuggestions).catch(function () { close(); });
      }, config.debounce || 300);
    }

    input.addEventListener("input", query);
    input.addEventListener("focus", function () { if (input.value.trim()) query(); });

    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!suggestions.length) return;
        e.preventDefault();
        activeIndex += e.key === "ArrowDown" ? 1 : -1;
        if (activeIndex < 0) activeIndex = suggestions.length - 1;
        if (activeIndex >= suggestions.length) activeIndex = 0;
        box.querySelectorAll(".mf-token-suggestion").forEach(function (el, idx) {
          el.classList.toggle("is-active", idx === activeIndex);
        });
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (suggestions.length) pick(suggestions[activeIndex >= 0 ? activeIndex : 0]);
      } else if (e.key === "Escape") {
        close();
      }
    });

    box.addEventListener("click", function (e) {
      var item = e.target.closest(".mf-token-suggestion");
      if (!item || item.dataset.empty) return;
      pick(suggestions[parseInt(item.dataset.idx, 10)]);
    });

    document.addEventListener("click", function (e) {
      if (!input.contains(e.target) && !box.contains(e.target)) close();
    });
  }

  /* Render a token list (.mf-token-list). Removal is delegated on the
     container, so nothing is interpolated into an event-handler attribute. */
  function renderTokens(containerId, items, labelOf, idOf, onRemove) {
    var container = $(containerId);
    if (!container) return;
    container.innerHTML = items.map(function (item) {
      return '<span class="mf-token"><span>' + esc(labelOf(item)) + "</span>" +
        '<button type="button" class="mf-token-remove" data-token-id="' + esc(idOf(item)) +
        '" aria-label="' + esc(t("Entfernen", "Remove")) + '">' +
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
        '<line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg></button></span>';
    }).join("");

    if (!container.dataset.bound) {
      container.dataset.bound = "1";
      container.addEventListener("click", function (e) {
        var btn = e.target.closest(".mf-token-remove");
        if (!btn) return;
        onRemove(btn.dataset.tokenId);
      });
    }
  }

  // ═══════════════════════════════════════════════════════════════════════
  //  Filter UI
  // ═══════════════════════════════════════════════════════════════════════

  function switchAdvTab(name) {
    if (TABS.indexOf(name) === -1) name = "basics";
    S.tab = name;
    document.querySelectorAll("#advFilterMenu .settings-tab[data-tab]").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.tab === name);
    });
    TABS.forEach(function (tab) {
      var panel = $("advPanel-" + tab);
      if (panel) panel.classList.toggle("active", tab === name);
    });
    saveState();
  }

  function applyTypeVisibility() {
    // with_status / with_networks only exist for series on TMDB.
    var isTv = S.type === "tv";
    document.querySelectorAll(".adv-tv-only").forEach(function (el) {
      el.hidden = !isTv;
    });
    if (!isTv) {
      S.statuses = [];
      S.networks = [];
      document.querySelectorAll(".adv-status-checkbox").forEach(function (cb) { cb.checked = false; });
      renderNetworks();
      updateStatusLabel();
    }
  }

  function renderGenres() {
    [
      { id: "genreSelectDropdown", selected: S.genres, cls: "adv-genre-checkbox" },
      { id: "genreExcludeDropdown", selected: S.genresExcluded, cls: "adv-genre-exclude-checkbox" },
    ].forEach(function (cfg) {
      var container = $(cfg.id);
      if (!container) return;
      var genres = allGenres[S.type] || [];
      if (!genres.length) {
        container.innerHTML = '<div class="mf-multiselect-empty">' +
          esc(t("Lädt…", "Loading…")) + "</div>";
        return;
      }
      container.innerHTML = genres.map(function (g) {
        var checked = cfg.selected.indexOf(g.id) !== -1 ? " checked" : "";
        return '<label class="mf-multiselect-item">' +
          '<input type="checkbox" class="chb-main ' + cfg.cls + '" value="' + esc(g.id) + '"' + checked + " />" +
          "<span>" + esc(g.name) + "</span></label>";
      }).join("");
    });
    updateGenreLabels();
  }

  function readCheckedValues(selector) {
    return Array.prototype.map.call(
      document.querySelectorAll(selector + ":checked"),
      function (cb) { return cb.value; }
    );
  }

  function genreNameById(id) {
    var list = allGenres[S.type] || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === id) return list[i].name;
    }
    return String(id);
  }

  function summariseSelection(names, noneLabel, manyLabel) {
    if (!names.length) return noneLabel;
    if (names.length <= 2) return names.join(", ");
    return names.length + " " + manyLabel;
  }

  function updateGenreLabels() {
    var incLabel = $("genreSelectLabel");
    if (incLabel) {
      incLabel.textContent = summariseSelection(
        S.genres.map(genreNameById),
        t("Alle Genres", "All Genres"),
        t("Genres ausgewählt", "genres selected")
      );
    }
    var excLabel = $("genreExcludeLabel");
    if (excLabel) {
      excLabel.textContent = summariseSelection(
        S.genresExcluded.map(genreNameById),
        t("Nichts ausgeschlossen", "Nothing excluded"),
        t("Genres ausgeschlossen", "genres excluded")
      );
    }
  }

  var STATUS_LABELS = {
    "0": function () { return t("Laufend", "Returning Series"); },
    "1": function () { return t("Geplant", "Planned"); },
    "2": function () { return t("In Produktion", "In Production"); },
    "3": function () { return t("Beendet", "Ended"); },
    "4": function () { return t("Abgesetzt", "Cancelled"); },
    "5": function () { return t("Pilot", "Pilot"); },
  };

  function statusName(code) {
    var fn = STATUS_LABELS[String(code)];
    return fn ? fn() : String(code);
  }

  function updateStatusLabel() {
    var label = $("statusLabel");
    if (!label) return;
    label.textContent = summariseSelection(
      S.statuses.map(statusName),
      t("Beliebiger Status", "Any status"),
      t("Status ausgewählt", "statuses selected")
    );
  }

  function renderKeywords() {
    renderTokens("selectedKeywords", S.keywords,
      function (k) { return k.name; },
      function (k) { return k.id; },
      function (id) {
        S.keywords = S.keywords.filter(function (k) { return String(k.id) !== String(id); });
        renderKeywords();
        afterFilterChange();
      });
  }

  function renderProviders(listKey) {
    var isInclude = listKey === "include";
    var items = isInclude ? S.incProviders : S.excProviders;
    renderTokens(isInclude ? "selectedIncludeProviders" : "selectedExcludeProviders", items,
      function (p) { return p.provider_name; },
      function (p) { return p.provider_id; },
      function (id) {
        if (isInclude) {
          S.incProviders = S.incProviders.filter(function (p) { return String(p.provider_id) !== String(id); });
        } else {
          S.excProviders = S.excProviders.filter(function (p) { return String(p.provider_id) !== String(id); });
        }
        renderProviders(listKey);
        afterFilterChange();
      });
  }

  function renderNetworks() {
    renderTokens("selectedNetworks", S.networks,
      function (n) { return n.name; },
      function (n) { return n.id; },
      function (id) {
        S.networks = S.networks.filter(function (n) { return String(n.id) !== String(id); });
        renderNetworks();
        afterFilterChange();
      });
  }

  // ── Active filter chips + per-group counters ───────────────────────────

  /* Each chip knows which state field it clears. Building the list here (and
     rendering from it) keeps "what is active" in exactly one place — the old
     version had chips for some filters and silently omitted others. */
  function buildChips() {
    var chips = [];

    chips.push({
      group: null,
      label: S.type === "tv" ? t("Typ: Serien", "Type: Series") : t("Typ: Filme", "Type: Movies"),
    });

    S.genres.forEach(function (id) {
      chips.push({
        group: "basics",
        label: t("Genre", "Genre") + ": " + genreNameById(id),
        clear: function () {
          S.genres = S.genres.filter(function (g) { return g !== id; });
          syncGenreCheckboxes();
        },
      });
    });

    S.genresExcluded.forEach(function (id) {
      chips.push({
        group: "basics",
        label: t("Ohne Genre", "Without genre") + ": " + genreNameById(id),
        clear: function () {
          S.genresExcluded = S.genresExcluded.filter(function (g) { return g !== id; });
          syncGenreCheckboxes();
        },
      });
    });

    if (S.sortBy !== "popularity.desc") {
      var sortSelect = $("sortBy");
      var sortLabel = "";
      if (sortSelect && sortSelect.selectedOptions && sortSelect.selectedOptions[0]) {
        sortLabel = sortSelect.selectedOptions[0].textContent.trim();
      }
      chips.push({
        group: "basics",
        label: t("Sortierung", "Sorting") + ": " + sortLabel,
        clear: function () {
          S.sortBy = "popularity.desc";
          if (sortSelect) sortSelect.value = S.sortBy;
        },
      });
    }

    if (S.voteMin > 0) {
      chips.push({
        group: "quality",
        label: "⭐ " + S.voteMin + "+",
        clear: function () {
          S.voteMin = 0;
          var slider = $("voteMin");
          if (slider) slider.value = "0";
          updateVoteLabel();
        },
      });
    }

    if (parseInt(S.voteCountMin, 10) > 0) {
      chips.push({
        group: "quality",
        label: t("Min. Stimmen", "Min votes") + ": " + fmtNumber(parseInt(S.voteCountMin, 10)),
        clear: function () {
          S.voteCountMin = "";
          var input = $("voteCountMin");
          if (input) input.value = "";
        },
      });
    }

    if (S.yearMin || S.yearMax) {
      chips.push({
        group: "quality",
        label: t("Jahr", "Year") + ": " + (S.yearMin || "…") + " – " + (S.yearMax || "…"),
        clear: function () {
          S.yearMin = "";
          S.yearMax = "";
          if ($("yearMin")) $("yearMin").value = "";
          if ($("yearMax")) $("yearMax").value = "";
        },
      });
    }

    if (S.runtimeMin || S.runtimeMax) {
      chips.push({
        group: "quality",
        label: t("Laufzeit", "Runtime") + ": " + (S.runtimeMin || "…") + " – " + (S.runtimeMax || "…") + " min",
        clear: function () {
          S.runtimeMin = "";
          S.runtimeMax = "";
          if ($("runtimeMin")) $("runtimeMin").value = "";
          if ($("runtimeMax")) $("runtimeMax").value = "";
        },
      });
    }

    if (S.region) {
      var regionSelect = $("watchRegion");
      var regionLabel = S.region;
      if (regionSelect && regionSelect.selectedOptions && regionSelect.selectedOptions[0]) {
        regionLabel = regionSelect.selectedOptions[0].textContent.trim();
      }
      chips.push({
        group: "streaming",
        label: t("Region", "Region") + ": " + regionLabel,
        clear: function () {
          S.region = "";
          if (regionSelect) regionSelect.value = "";
          loadWatchProviders();
        },
      });
    }

    S.incProviders.forEach(function (p) {
      chips.push({
        group: "streaming",
        label: "+ " + p.provider_name,
        clear: function () {
          S.incProviders = S.incProviders.filter(function (x) { return x.provider_id !== p.provider_id; });
          renderProviders("include");
        },
      });
    });

    S.excProviders.forEach(function (p) {
      chips.push({
        group: "streaming",
        label: "− " + p.provider_name,
        clear: function () {
          S.excProviders = S.excProviders.filter(function (x) { return x.provider_id !== p.provider_id; });
          renderProviders("exclude");
        },
      });
    });

    S.keywords.forEach(function (kw) {
      chips.push({
        group: "details",
        label: t("Schlagwort", "Keyword") + ": " + kw.name,
        clear: function () {
          S.keywords = S.keywords.filter(function (k) { return k.id !== kw.id; });
          renderKeywords();
        },
      });
    });

    if (S.originalLanguage) {
      var langSelect = $("originalLanguage");
      var langLabel = S.originalLanguage;
      if (langSelect && langSelect.selectedOptions && langSelect.selectedOptions[0]) {
        langLabel = langSelect.selectedOptions[0].textContent.trim();
      }
      chips.push({
        group: "details",
        label: t("Sprache", "Language") + ": " + langLabel,
        clear: function () {
          S.originalLanguage = "";
          if (langSelect) langSelect.value = "";
        },
      });
    }

    S.statuses.forEach(function (code) {
      chips.push({
        group: "details",
        label: t("Status", "Status") + ": " + statusName(code),
        clear: function () {
          S.statuses = S.statuses.filter(function (c) { return c !== code; });
          document.querySelectorAll(".adv-status-checkbox").forEach(function (cb) {
            cb.checked = S.statuses.indexOf(cb.value) !== -1;
          });
          updateStatusLabel();
        },
      });
    });

    S.networks.forEach(function (n) {
      chips.push({
        group: "details",
        label: t("Sender", "Network") + ": " + n.name,
        clear: function () {
          S.networks = S.networks.filter(function (x) { return x.id !== n.id; });
          renderNetworks();
        },
      });
    });

    return chips;
  }

  var currentChips = [];

  function renderChips() {
    currentChips = buildChips();
    var container = $("advActiveFilterTags");
    if (!container) return;

    container.innerHTML = currentChips.map(function (chip, idx) {
      if (!chip.clear) {
        return '<span class="mf-chip mf-chip-static"><span>' + esc(chip.label) + "</span></span>";
      }
      return '<span class="mf-chip"><span>' + esc(chip.label) + "</span>" +
        '<button type="button" class="mf-chip-remove" data-chip-idx="' + idx +
        '" aria-label="' + esc(t("Filter entfernen", "Remove filter")) + '">✕</button></span>';
    }).join("");

    // Per-group counters in the floating menu, so an active filter in a
    // collapsed group is still visible at a glance.
    var counts = { basics: 0, quality: 0, streaming: 0, details: 0 };
    currentChips.forEach(function (chip) {
      if (chip.group && counts.hasOwnProperty(chip.group)) counts[chip.group]++;
    });
    document.querySelectorAll(".adv-menu-count").forEach(function (badge) {
      var n = counts[badge.dataset.countFor] || 0;
      badge.textContent = n;
      badge.hidden = n === 0;
    });
  }

  function syncGenreCheckboxes() {
    document.querySelectorAll(".adv-genre-checkbox").forEach(function (cb) {
      cb.checked = S.genres.indexOf(parseInt(cb.value, 10)) !== -1;
    });
    document.querySelectorAll(".adv-genre-exclude-checkbox").forEach(function (cb) {
      cb.checked = S.genresExcluded.indexOf(parseInt(cb.value, 10)) !== -1;
    });
    updateGenreLabels();
  }

  /* Called after any filter mutation that did not come from a full search:
     refresh chips + counters and persist, but do not fire a request. */
  function afterFilterChange() {
    renderChips();
    saveState();
  }

  function updateVoteLabel() {
    var label = $("voteLabel");
    if (!label) return;
    label.textContent = S.voteMin > 0 ? S.voteMin + " – 10" : "0 – 10";
  }

  // ═══════════════════════════════════════════════════════════════════════
  //  Reference data
  // ═══════════════════════════════════════════════════════════════════════

  function loadGenres() {
    return fetch("/api/tmdb/genres")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.tv && d.movie) {
          var lang = window.__LANG === "en" ? "en" : "de";
          allGenres = {
            tv: d.tv[lang] || d.tv.de || [],
            movie: d.movie[lang] || d.movie.de || [],
          };
          renderGenres();
          renderChips();
        } else {
          showDropdownError("genreSelectDropdown", translateError(d));
          showDropdownError("genreExcludeDropdown", translateError(d));
        }
      })
      .catch(function () {
        showDropdownError("genreSelectDropdown", t("Netzwerkfehler.", "Network error."));
        showDropdownError("genreExcludeDropdown", t("Netzwerkfehler.", "Network error."));
      });
  }

  function showDropdownError(id, message) {
    var el = $(id);
    if (el) el.innerHTML = '<div class="mf-multiselect-empty" style="color:var(--error)">' + esc(message) + "</div>";
  }

  function loadWatchRegions() {
    var select = $("watchRegion");
    if (!select) return Promise.resolve();
    return fetch("/api/tmdb/watch_regions")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        (d.results || []).forEach(function (reg) {
          var opt = document.createElement("option");
          opt.value = reg.iso_3166_1;
          opt.textContent = reg.native_name || reg.english_name || reg.iso_3166_1;
          select.appendChild(opt);
        });
        if (S.region) select.value = S.region;
        // The chip label is read off the <option>; before this point the
        // select only held the placeholder.
        renderChips();
      })
      .catch(function (e) { console.debug("[AdvSearch] watch regions failed", e); });
  }

  function loadWatchProviders() {
    var params = new URLSearchParams({ type: S.type });
    if (S.region) params.append("watch_region", S.region);
    return fetch("/api/tmdb/watch_providers?" + params.toString())
      .then(function (r) { return r.json(); })
      .then(function (d) { allWatchProviders = d.results || []; })
      .catch(function () { allWatchProviders = []; });
  }

  function loadLanguages() {
    var select = $("originalLanguage");
    if (!select) return Promise.resolve();
    return fetch("/api/tmdb/languages")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        (d.results || []).forEach(function (lang) {
          var opt = document.createElement("option");
          opt.value = lang.iso_639_1;
          var native = lang.name && lang.name !== lang.english_name ? " (" + lang.name + ")" : "";
          opt.textContent = lang.english_name + native;
          select.appendChild(opt);
        });
        if (S.originalLanguage) select.value = S.originalLanguage;
        renderChips();
      })
      .catch(function (e) { console.debug("[AdvSearch] languages failed", e); });
  }

  function loadNetworks() {
    return fetch("/api/tmdb/networks")
      .then(function (r) { return r.json(); })
      .then(function (d) { allNetworks = d.results || []; })
      .catch(function () { allNetworks = []; });
  }

  // ═══════════════════════════════════════════════════════════════════════
  //  Search
  // ═══════════════════════════════════════════════════════════════════════

  function buildParams() {
    var params = new URLSearchParams();
    params.append("type", S.type);

    var sortBy = S.sortBy || "popularity.desc";
    if (S.type === "tv" && sortBy.indexOf("primary_release_date") === 0) {
      sortBy = sortBy.replace("primary_release_date", "first_air_date");
    } else if (S.type === "movie" && sortBy.indexOf("first_air_date") === 0) {
      sortBy = sortBy.replace("first_air_date", "primary_release_date");
    }
    params.append("sort_by", sortBy);

    if (S.voteMin > 0) params.append("vote_average.gte", String(S.voteMin));

    var voteCount = parseInt(S.voteCountMin, 10);
    if (!isNaN(voteCount) && voteCount > 0) params.append("vote_count.gte", String(voteCount));

    var dateKey = S.type === "tv" ? "first_air_date" : "primary_release_date";
    if (S.yearMin) params.append(dateKey + ".gte", S.yearMin + "-01-01");
    if (S.yearMax) params.append(dateKey + ".lte", S.yearMax + "-12-31");

    var runtimeMin = parseInt(S.runtimeMin, 10);
    if (!isNaN(runtimeMin) && runtimeMin > 0) params.append("with_runtime.gte", String(runtimeMin));
    var runtimeMax = parseInt(S.runtimeMax, 10);
    if (!isNaN(runtimeMax) && runtimeMax > 0) params.append("with_runtime.lte", String(runtimeMax));

    // Comma = AND on TMDB (must have all of them), pipe = OR.
    if (S.genres.length) params.append("with_genres", S.genres.join(","));
    if (S.genresExcluded.length) params.append("without_genres", S.genresExcluded.join(","));
    if (S.keywords.length) params.append("with_keywords", S.keywords.map(function (k) { return k.id; }).join(","));

    if (S.originalLanguage) params.append("with_original_language", S.originalLanguage);

    if (S.type === "tv") {
      if (S.statuses.length) params.append("with_status", S.statuses.join("|"));
      if (S.networks.length) params.append("with_networks", S.networks.map(function (n) { return n.id; }).join("|"));
    }

    if (S.region) params.append("watch_region", S.region);
    if (S.incProviders.length) {
      params.append("with_watch_providers", S.incProviders.map(function (p) { return p.provider_id; }).join("|"));
    }
    if (S.excProviders.length) {
      params.append("without_watch_providers", S.excProviders.map(function (p) { return p.provider_id; }).join("|"));
    }

    params.append("language", window.__LANG === "de" ? "de-DE" : "en-US");
    return params;
  }

  function setSearching(isSearching) {
    var btn = $("runSearchBtn");
    if (btn) btn.disabled = isSearching;
  }

  function skeletonMarkup(count) {
    var card = '<div class="adv-skeleton-card"><div class="adv-skeleton-poster"></div>' +
      '<div class="adv-skeleton-body"><div class="adv-skeleton-line w-70"></div>' +
      '<div class="adv-skeleton-line w-40"></div></div></div>';
    return new Array(count + 1).join(card);
  }

  function showSkeletons() {
    var grid = $("resultsGrid");
    if (grid) grid.innerHTML = skeletonMarkup(Math.max(8, getPageSize()));
  }

  function runSearch() {
    setSearching(true);
    lastError = null;

    // Bump the generation first: any response still in flight for the previous
    // query is discarded instead of being merged into the fresh buffer.
    var seq = ++searchSeq;
    inFlight = {};
    S.pages = {};
    S.total = 0;
    S.pageIndex = 0;

    // Refresh the "already downloaded" / "in auto-sync" badge sources in the
    // background — never block the search on them.
    callIfPresent("loadDownloadedFolders");
    callIfPresent("loadAutoSyncJobs");
    callIfPresent("loadCineinfoSettings");
    callIfPresent("loadGeneralSettings");

    S.paramsStr = buildParams().toString();
    renderChips();
    showSkeletons();

    var countSpan = $("resultsCount");
    if (countSpan) countSpan.textContent = t("Lädt…", "Loading…");
    var pagination = $("advPagination");
    if (pagination) pagination.hidden = true;

    // Page 1 first — only its total_results tells us which upstream pages the
    // first grid page actually spans.
    return fetchTmdbPage(1, seq)
      .then(function (ok) {
        if (seq !== searchSeq) return;
        if (!ok) { renderSearchError(); return; }
        return ensurePages(tmdbPagesFor(0), seq).then(function (allOk) {
          if (seq !== searchSeq) return;
          if (!allOk) { renderSearchError(); return; }
          renderPage();
          saveState();
        });
      })
      .catch(function (err) {
        console.error("[AdvSearch] search failed", err);
        if (seq === searchSeq) renderSearchError();
      })
      .finally(function () {
        if (seq === searchSeq) setSearching(false);
      });
  }

  function renderSearchError() {
    var grid = $("resultsGrid");
    var message = lastError || t("Netzwerkfehler bei der Suche.", "Network error in search.");
    if (grid) {
      grid.innerHTML = '<div class="adv-empty-state adv-empty-error">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:40px;height:40px;">' +
        '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>' +
        "<div>" + esc(message) + "</div></div>";
    }
    var countSpan = $("resultsCount");
    if (countSpan) countSpan.textContent = t("Fehler", "Error");
    var pagination = $("advPagination");
    if (pagination) pagination.hidden = true;
  }

  /* Which upstream pages does local grid page `index` need? Usually one, two
     when the grid page straddles a 20-item boundary — never more, which is
     the whole point of the sparse buffer. */
  function tmdbPagesFor(index) {
    var pageSize = getPageSize();
    var total = effectiveTotal();
    var start = index * pageSize;
    if (total && start >= total) return [];
    var end = (total ? Math.min(start + pageSize, total) : start + pageSize) - 1;
    var first = Math.floor(start / TMDB_PAGE_SIZE) + 1;
    var last = Math.min(Math.floor(end / TMDB_PAGE_SIZE) + 1, TMDB_MAX_PAGES);
    var pages = [];
    for (var p = first; p <= last; p++) pages.push(p);
    return pages;
  }

  function ensurePages(pageNumbers, seq) {
    var pending = pageNumbers
      .filter(function (n) { return n >= 1 && n <= TMDB_MAX_PAGES && !S.pages[n]; })
      .map(function (n) { return fetchTmdbPage(n, seq); });
    if (!pending.length) return Promise.resolve(true);
    return Promise.all(pending).then(function (results) {
      return results.every(Boolean);
    });
  }

  function fetchTmdbPage(page, seq) {
    if (!S.paramsStr) return Promise.resolve(false);
    if (S.pages[page]) return Promise.resolve(true);
    if (inFlight[page]) return inFlight[page];

    var params = new URLSearchParams(S.paramsStr);
    params.append("page", String(page));

    var controller = new AbortController();
    var timeoutId = setTimeout(function () { controller.abort(); }, 15000);
    // Hold on to the registry this request was filed in: runSearch() swaps
    // `inFlight` for a fresh object, and a late finally must not delete the
    // new query's entry for the same page number.
    var registry = inFlight;

    var request = fetch("/api/tmdb/discover?" + params.toString(), { signal: controller.signal })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        // A response that arrives after the filters changed belongs to a query
        // nobody is looking at any more — dropping it keeps the buffer honest.
        if (seq !== searchSeq) return false;
        if (d && d.error) {
          lastError = translateError(d);
          return false;
        }
        S.total = d.total_results || 0;
        S.pages[page] = d.results || [];
        resultsFreshAt = Date.now();
        var countSpan = $("resultsCount");
        if (countSpan) countSpan.textContent = describeRange();
        return true;
      })
      .catch(function (e) {
        console.error("[AdvSearch] discover page " + page + " failed", e);
        if (seq === searchSeq && !lastError) {
          lastError = e && e.name === "AbortError"
            ? t("Zeitüberschreitung bei der TMDB-Anfrage.", "The TMDB request timed out.")
            : t("Netzwerkfehler bei der Suche.", "Network error in search.");
        }
        return false;
      })
      .finally(function () {
        clearTimeout(timeoutId);
        if (registry[page] === request) delete registry[page];
      });

    registry[page] = request;
    return request;
  }

  // ── Results grid ───────────────────────────────────────────────────────

  function getGridColumns() {
    var grid = $("resultsGrid");
    if (!grid) return 1;
    var cols = window.getComputedStyle(grid).getPropertyValue("grid-template-columns").split(" ").length;
    return Math.max(1, cols);
  }

  function getPageSize() {
    // At least 10 items, but always complete rows for the current column count.
    var cols = getGridColumns();
    var rows = Math.max(2, Math.ceil(10 / cols));
    return cols * rows;
  }

  /* TMDB never serves past page 500, so anything beyond 10 000 results is
     unreachable — the old pager happily offered page 900 of 18 000 and then
     rendered an empty grid. */
  function effectiveTotal() {
    return Math.min(S.total, TMDB_MAX_RESULTS);
  }

  function maxPages() {
    return Math.max(1, Math.ceil(effectiveTotal() / getPageSize()));
  }

  /* Resolve a global result index against the sparse page buffer. */
  function itemAt(index) {
    var page = S.pages[Math.floor(index / TMDB_PAGE_SIZE) + 1];
    return page ? page[index % TMDB_PAGE_SIZE] : undefined;
  }

  function bufferedCount() {
    return Object.keys(S.pages).reduce(function (sum, n) {
      return sum + (S.pages[n] ? S.pages[n].length : 0);
    }, 0);
  }

  function describeRange() {
    var total = effectiveTotal();
    if (!total) return t("0 Ergebnisse", "0 results");
    var pageSize = getPageSize();
    var from = S.pageIndex * pageSize + 1;
    var to = Math.min(total, (S.pageIndex + 1) * pageSize);
    var suffix = S.total > TMDB_MAX_RESULTS
      ? " " + t("(TMDB liefert max. 10.000)", "(TMDB caps at 10,000)")
      : "";
    return t(
      fmtNumber(from) + "–" + fmtNumber(to) + " von " + fmtNumber(total) + suffix,
      fmtNumber(from) + "–" + fmtNumber(to) + " of " + fmtNumber(total) + suffix
    );
  }

  function renderPage() {
    var grid = $("resultsGrid");
    if (!grid) return;

    var pageSize = getPageSize();
    lastPageSize = pageSize;
    var start = S.pageIndex * pageSize;
    var pageResults = [];
    for (var i = start; i < start + pageSize; i++) {
      var item = itemAt(i);
      if (item) pageResults.push(item);
    }

    if (!pageResults.length) {
      grid.innerHTML = '<div class="adv-empty-state">' + esc(
        effectiveTotal() === 0
          ? t("Keine Ergebnisse gefunden.", "No results found.")
          : t("Keine Ergebnisse auf dieser Seite.", "No results on this page.")
      ) + "</div>";
      renderPagination();
      var countSpan0 = $("resultsCount");
      if (countSpan0) countSpan0.textContent = describeRange();
      return;
    }

    grid.innerHTML = pageResults.map(function (r, offset) {
      var title = r.title || r.name || "";
      var date = r.release_date || r.first_air_date || "";
      var year = date ? date.split("-")[0] : "";
      var rating = r.vote_average ? parseFloat(r.vote_average).toFixed(1) : null;

      var poster = r.poster_path
        ? '<img class="tmdb-card-poster" src="https://image.tmdb.org/t/p/w342' + esc(r.poster_path) +
          '" loading="lazy" alt="' + esc(title) + '" />'
        : '<div class="tmdb-card-poster tmdb-card-poster-empty">' + esc(t("Kein Bild", "No image")) + "</div>";

      var seasons = S.type === "tv"
        ? '<span class="tmdb-season-info" data-seasons-for="' + esc(r.id) + '">…</span>'
        : "<span></span>";

      // data-index is the global result index, so the Details modal can look
      // the full entry (overview, genres, …) back up without stuffing a whole
      // synopsis into a data attribute.
      return '<div class="tmdb-card" data-tmdb-id="' + esc(r.id) + '" data-title="' + esc(title) +
        '" data-poster="' + esc(r.poster_path || "") + '" data-index="' + (start + offset) +
        '" tabindex="0" role="button">' +
        '<div class="tmdb-card-poster-wrap">' +
        (rating && parseFloat(rating) > 0 ? '<div class="tmdb-card-rating">⭐ ' + esc(rating) + "</div>" : "") +
        poster +
        '<div class="tmdb-card-overlay"><div class="tmdb-card-actions">' +
        '<button type="button" class="tmdb-card-btn tmdb-card-btn-primary" data-action="search">' +
        '<svg viewBox="0 0 24 24" fill="currentColor" style="width:14px;height:14px;"><polygon points="5 3 19 12 5 21 5 3"/></svg>' +
        "<span>" + esc(t("Suchen", "Search")) + "</span></button>" +
        '<button type="button" class="tmdb-card-btn" data-action="details">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;">' +
        '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>' +
        "<span>" + esc(t("Details", "Details")) + "</span></button>" +
        "</div></div></div>" +
        '<div class="tmdb-card-info"><h4 class="tmdb-card-title" title="' + esc(title) + '">' + esc(title) + "</h4>" +
        '<div class="tmdb-card-meta"><span>' + esc(year) + "</span>" + seasons + "</div></div></div>";
    }).join("");

    // Badges are added per card by app.js's shared helpers.
    grid.querySelectorAll(".tmdb-card").forEach(function (card) {
      var title = card.dataset.title;
      var id = card.dataset.tmdbId;
      callIfPresent("addDownloadedBadgeForTmdb", card, title, id);
      callIfPresent("addSyncBadgeForTmdb", card, title);
    });

    if (S.type === "tv") loadSeasonCounts(pageResults);

    var countSpan = $("resultsCount");
    if (countSpan) countSpan.textContent = describeRange();

    renderPagination();
    prefetchNextPage();
  }

  /* One request for the whole page instead of one per card (see the
     /api/tmdb/tv_seasons docstring). */
  function loadSeasonCounts(pageResults) {
    var ids = pageResults.map(function (r) { return r.id; }).filter(Boolean);
    if (!ids.length) return;
    fetch("/api/tmdb/tv_seasons", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: ids }),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || d.error) { markUnknownSeasons(); return; }
        Object.keys(d).forEach(function (id) {
          // Ids are TMDB integers; strip anything else before building the
          // attribute selector rather than trusting the response shape.
          var safeId = String(id).replace(/[^0-9]/g, "");
          if (!safeId) return;
          var span = document.querySelector('.tmdb-season-info[data-seasons-for="' + safeId + '"]');
          if (!span) return;
          var n = d[id];
          span.textContent = n === 1
            ? t("1 Staffel", "1 Season")
            : n + " " + t("Staffeln", "Seasons");
          span.classList.add("is-loaded");
        });
        markUnknownSeasons();
      })
      .catch(markUnknownSeasons);
  }

  /* TMDB does not know the season count for every entry (and a single lookup
     can fail); say so instead of leaving the slot blank, same as before. */
  function markUnknownSeasons() {
    document.querySelectorAll(".tmdb-season-info:not(.is-loaded)").forEach(function (span) {
      span.textContent = t("Unbekannt", "Unknown");
      span.classList.add("is-loaded");
    });
  }

  function prefetchNextPage() {
    var pages = tmdbPagesFor(S.pageIndex + 1);
    if (!pages.length) return;
    if (pages.every(function (n) { return !!S.pages[n]; })) return;
    var seq = searchSeq;
    ensurePages(pages, seq).then(function (ok) {
      if (ok && seq === searchSeq) saveState();
    });
  }

  // ── Pagination ─────────────────────────────────────────────────────────

  /* Page-number list with ellipses: always first + last, a window of two
     around the current page. */
  function pageNumbers(current, total) {
    var pages = [];
    var push = function (n) { if (pages[pages.length - 1] !== n) pages.push(n); };
    for (var i = 1; i <= total; i++) {
      if (i === 1 || i === total || Math.abs(i - current) <= 2) {
        if (pages.length && i - pages[pages.length - 1] > 1) pages.push("…");
        push(i);
      }
    }
    return pages;
  }

  function renderPagination() {
    var container = $("advPagination");
    if (!container) return;

    var total = maxPages();
    if (!effectiveTotal() || total <= 1) {
      container.hidden = true;
      container.innerHTML = "";
      return;
    }
    container.hidden = false;

    var current = S.pageIndex + 1;
    var arrow = function (dir) {
      var points = dir === "prev" ? "15 18 9 12 15 6" : "9 18 15 12 9 6";
      return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="' + points + '"/></svg>';
    };

    var html = "";
    html += '<button type="button" class="mf-pagination-btn" data-page="1"' + (current === 1 ? " disabled" : "") +
      ' title="' + esc(t("Erste Seite", "First page")) + '">«</button>';
    html += '<button type="button" class="mf-pagination-btn" data-page="' + (current - 1) + '"' + (current === 1 ? " disabled" : "") +
      ' title="' + esc(t("Zurück", "Back")) + '">' + arrow("prev") + "</button>";

    pageNumbers(current, total).forEach(function (entry) {
      if (entry === "…") {
        html += '<span class="mf-pagination-ellipsis">…</span>';
        return;
      }
      html += '<button type="button" class="mf-pagination-page' + (entry === current ? " active" : "") +
        '" data-page="' + entry + '"' + (entry === current ? " disabled" : "") + ">" + entry + "</button>";
    });

    html += '<button type="button" class="mf-pagination-btn" data-page="' + (current + 1) + '"' + (current === total ? " disabled" : "") +
      ' title="' + esc(t("Weiter", "Next")) + '">' + arrow("next") + "</button>";
    html += '<button type="button" class="mf-pagination-btn" data-page="' + total + '"' + (current === total ? " disabled" : "") +
      ' title="' + esc(t("Letzte Seite", "Last page")) + '">»</button>';

    html += '<span class="mf-pagination-jump"><label for="advPageJump">' + esc(t("Seite", "Page")) + '</label>' +
      '<input type="text" id="advPageJump" inputmode="numeric" pattern="[0-9]*" value="' + current +
      '" aria-label="' + esc(t("Zu Seite springen", "Jump to page")) + '" />' +
      '<span>' + esc(t("von", "of")) + " " + total + "</span></span>";

    container.innerHTML = html;
  }

  function goToPage(pageNumber) {
    var total = maxPages();
    var target = Math.max(1, Math.min(parseInt(pageNumber, 10) || 1, total));
    var index = target - 1;
    if (index === S.pageIndex) return;

    S.pageIndex = index;
    var pages = tmdbPagesFor(index);
    var missing = pages.filter(function (n) { return !S.pages[n]; });

    if (!missing.length) {
      renderPage();
      saveState();
      scrollResultsIntoView();
      return;
    }

    // At most two upstream pages, whatever page number was jumped to.
    var seq = searchSeq;
    showSkeletons();
    setSearching(true);
    ensurePages(pages, seq)
      .then(function (ok) {
        if (seq !== searchSeq) return;
        if (!ok) { renderSearchError(); return; }
        renderPage();
        saveState();
        scrollResultsIntoView();
      })
      .finally(function () {
        if (seq === searchSeq) setSearching(false);
      });
  }

  // ═══════════════════════════════════════════════════════════════════════
  //  Details modal
  // ═══════════════════════════════════════════════════════════════════════

  /* The description used to be a CSS :hover overlay on the card — invisible
     on touch devices and clipped to whatever fitted the poster. It now opens
     on demand, with the full TMDB overview and the metadata around it. */
  function openDetailsModal(card) {
    var overlay = $("advDetailsOverlay");
    if (!overlay || !card) return;

    var item = itemAt(parseInt(card.dataset.index, 10));
    if (!item) return;

    var title = item.title || item.name || "";
    var date = item.release_date || item.first_air_date || "";
    var year = date ? date.split("-")[0] : "";

    $("advDetailsTitle").textContent = title;

    var poster = $("advDetailsPoster");
    poster.innerHTML = item.poster_path
      ? '<img src="https://image.tmdb.org/t/p/w342' + esc(item.poster_path) + '" alt="' + esc(title) + '" />'
      : '<div class="adv-details-poster-empty">' + esc(t("Kein Bild", "No image")) + "</div>";

    var meta = [];
    if (item.vote_average) {
      var votes = item.vote_count
        ? " (" + fmtNumber(item.vote_count) + " " + t("Stimmen", "votes") + ")"
        : "";
      meta.push("⭐ " + parseFloat(item.vote_average).toFixed(1) + votes);
    }
    if (year) meta.push(year);
    // The season count comes from the batch endpoint and is already on the card.
    var seasonSpan = card.querySelector(".tmdb-season-info");
    var seasonText = seasonSpan ? seasonSpan.textContent.trim() : "";
    if (seasonText && seasonText !== "…") meta.push(seasonText);
    if (item.original_language) {
      meta.push(t("Original", "Original") + ": " + item.original_language.toUpperCase());
    }
    $("advDetailsMeta").textContent = meta.join("  •  ");

    var genreNames = (item.genre_ids || []).map(genreNameById).filter(Boolean);
    $("advDetailsGenres").innerHTML = genreNames.map(function (name) {
      return '<span class="mf-chip mf-chip-static"><span>' + esc(name) + "</span></span>";
    }).join("");

    var overview = (item.overview || "").trim();
    var overviewEl = $("advDetailsOverview");
    overviewEl.textContent = overview || t("Keine Beschreibung verfügbar.", "No description available.");
    overviewEl.classList.toggle("is-empty", !overview);

    // Remember which card this modal belongs to, so "Search streams" can hand
    // the same title/poster on to the stream-search modal.
    overlay.dataset.forIndex = card.dataset.index;
    overlay.style.display = "flex";
    document.body.style.overflow = "hidden";
    var closeBtn = $("advDetailsClose");
    if (closeBtn) closeBtn.focus();
  }

  function closeDetailsModal() {
    var overlay = $("advDetailsOverlay");
    if (!overlay || overlay.style.display === "none") return;
    overlay.style.display = "none";
    // Only release the scroll lock if no other modal is open.
    var aniModal = document.getElementById("aniSearchModalOverlay");
    if (!aniModal || aniModal.style.display === "none" || !aniModal.style.display) {
      document.body.style.overflow = "";
    }
  }

  function openStreamSearch(card) {
    if (!card) return;
    if (typeof window.openAniSearchModal !== "function") {
      // Loud on purpose: silently doing nothing is what this whole button was
      // reported for once already.
      console.error("[AdvSearch] openAniSearchModal() is missing — is app.js loaded?");
      return;
    }
    window.openAniSearchModal(card.dataset.title, card.dataset.tmdbId, S.type, card.dataset.poster || null);
  }

  function scrollResultsIntoView() {
    var header = document.querySelector(".adv-results-header");
    if (header && typeof header.scrollIntoView === "function") {
      header.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  // ═══════════════════════════════════════════════════════════════════════
  //  Presets
  // ═══════════════════════════════════════════════════════════════════════

  function clearFilters() {
    S.genres = [];
    S.genresExcluded = [];
    S.keywords = [];
    S.incProviders = [];
    S.excProviders = [];
    S.networks = [];
    S.statuses = [];
    S.originalLanguage = "";
    S.yearMin = "";
    S.yearMax = "";
    S.runtimeMin = "";
    S.runtimeMax = "";
    S.voteMin = 0;
    S.voteCountMin = "";
    S.sortBy = "popularity.desc";
  }

  function writeFiltersToDom() {
    document.querySelectorAll("#mediaTypeFilter .mf-segmented-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.type === S.type);
    });
    if ($("sortBy")) $("sortBy").value = S.sortBy;
    if ($("yearMin")) $("yearMin").value = S.yearMin;
    if ($("yearMax")) $("yearMax").value = S.yearMax;
    if ($("runtimeMin")) $("runtimeMin").value = S.runtimeMin;
    if ($("runtimeMax")) $("runtimeMax").value = S.runtimeMax;
    if ($("voteMin")) $("voteMin").value = String(S.voteMin);
    if ($("voteCountMin")) $("voteCountMin").value = S.voteCountMin;
    if ($("watchRegion")) $("watchRegion").value = S.region;
    if ($("originalLanguage")) $("originalLanguage").value = S.originalLanguage;
    document.querySelectorAll(".adv-status-checkbox").forEach(function (cb) {
      cb.checked = S.statuses.indexOf(cb.value) !== -1;
    });
    updateVoteLabel();
    updateStatusLabel();
    syncGenreCheckboxes();
    renderKeywords();
    renderProviders("include");
    renderProviders("exclude");
    renderNetworks();
    applyTypeVisibility();
  }

  var PRESETS = {
    anime: function () {
      S.type = "tv";
      S.keywords = [{ id: 210024, name: "anime" }];
      S.sortBy = "vote_average.desc";
      S.voteMin = 7;
      S.voteCountMin = "200"; // without this, "best rated" is a list of 10.0/3-votes noise
    },
    popular_tv: function () {
      S.type = "tv";
      S.sortBy = "popularity.desc";
    },
    popular_movies: function () {
      S.type = "movie";
      S.sortBy = "popularity.desc";
      // Popular alone drags in a lot of poorly rated blockbusters — the
      // preset is meant to surface things actually worth watching.
      S.voteMin = 8;
    },
    new_this_year: function () {
      var year = String(new Date().getFullYear());
      S.type = "tv";
      S.yearMin = year;
      S.yearMax = year;
      S.sortBy = "primary_release_date.desc";
    },
    action: function () {
      S.type = "tv";
      S.sortBy = "popularity.desc";
      S.genres = [10759]; // Action & Adventure (TV)
    },
    hidden_gems: function () {
      S.type = "movie";
      S.sortBy = "vote_average.desc";
      S.voteMin = 7.5;
      S.voteCountMin = "300";
    },
  };

  function applyPreset(key) {
    var preset = PRESETS[key];
    if (!preset) return;
    clearFilters();
    preset();
    writeFiltersToDom();
    loadWatchProviders();
    renderGenres();
    runSearch();
  }

  // ═══════════════════════════════════════════════════════════════════════
  //  Wiring
  // ═══════════════════════════════════════════════════════════════════════

  function bindEvents() {
    // Floating side menu: filter groups + presets.
    var menu = $("advFilterMenu");
    menu.addEventListener("click", function (e) {
      var tab = e.target.closest(".settings-tab[data-tab]");
      if (tab) { switchAdvTab(tab.dataset.tab); return; }
      var preset = e.target.closest(".adv-preset-tab");
      if (preset) applyPreset(preset.dataset.preset);
    });

    // Media type.
    var typeGroup = $("mediaTypeFilter");
    if (typeGroup) {
      typeGroup.addEventListener("click", function (e) {
        var btn = e.target.closest(".mf-segmented-btn");
        if (!btn || btn.dataset.type === S.type) return;
        S.type = btn.dataset.type === "movie" ? "movie" : "tv";
        document.querySelectorAll("#mediaTypeFilter .mf-segmented-btn").forEach(function (b) {
          b.classList.toggle("active", b === btn);
        });
        // Genre ids differ between tv and movie, so a carried-over selection
        // would silently filter for the wrong thing.
        S.genres = [];
        S.genresExcluded = [];
        renderGenres();
        applyTypeVisibility();
        loadWatchProviders();
        afterFilterChange();
      });
    }

    initMultiSelect("genreSelect", function () {
      S.genres = readCheckedValues(".adv-genre-checkbox").map(Number);
      updateGenreLabels();
      afterFilterChange();
    });

    initMultiSelect("genreExcludeSelect", function () {
      S.genresExcluded = readCheckedValues(".adv-genre-exclude-checkbox").map(Number);
      updateGenreLabels();
      afterFilterChange();
    });

    initMultiSelect("statusSelect", function () {
      S.statuses = readCheckedValues(".adv-status-checkbox");
      updateStatusLabel();
      afterFilterChange();
    });

    // Plain inputs / selects.
    [
      ["sortBy", "sortBy", "change"],
      ["yearMin", "yearMin", "input"],
      ["yearMax", "yearMax", "input"],
      ["runtimeMin", "runtimeMin", "input"],
      ["runtimeMax", "runtimeMax", "input"],
      ["voteCountMin", "voteCountMin", "input"],
      ["originalLanguage", "originalLanguage", "change"],
    ].forEach(function (entry) {
      var el = $(entry[0]);
      if (!el) return;
      el.addEventListener(entry[2], function () {
        S[entry[1]] = el.value.trim();
        afterFilterChange();
      });
    });

    var voteSlider = $("voteMin");
    if (voteSlider) {
      voteSlider.addEventListener("input", function () {
        S.voteMin = parseFloat(voteSlider.value) || 0;
        updateVoteLabel();
      });
      voteSlider.addEventListener("change", afterFilterChange);
    }

    var regionSelect = $("watchRegion");
    if (regionSelect) {
      regionSelect.addEventListener("change", function () {
        S.region = regionSelect.value;
        loadWatchProviders();
        afterFilterChange();
      });
    }

    // Token fields.
    initTokenField({
      inputId: "keywordInput",
      suggestionsId: "keywordAutocomplete",
      minChars: 2,
      debounce: 350,
      labelOf: function (kw) { return kw.name; },
      remoteSource: function (q) {
        return fetch("/api/tmdb/keywords?q=" + encodeURIComponent(q))
          .then(function (r) {
            if (r.status === 404) return { results: [] }; // catalogue still downloading
            return r.json();
          })
          .then(function (d) { return d.results || []; });
      },
      onPick: function (kw) {
        if (S.keywords.some(function (k) { return k.id === kw.id; })) return;
        S.keywords.push({ id: kw.id, name: kw.name });
        renderKeywords();
        afterFilterChange();
      },
    });

    ["include", "exclude"].forEach(function (listKey) {
      initTokenField({
        inputId: listKey === "include" ? "includeProviderInput" : "excludeProviderInput",
        suggestionsId: listKey === "include" ? "includeProviderAutocomplete" : "excludeProviderAutocomplete",
        minChars: 1,
        labelOf: function (p) { return p.provider_name; },
        localSource: function (q) {
          var needle = q.toLowerCase();
          return allWatchProviders.filter(function (p) {
            return (p.provider_name || "").toLowerCase().indexOf(needle) !== -1;
          }).slice(0, 20);
        },
        onPick: function (p) {
          var list = listKey === "include" ? S.incProviders : S.excProviders;
          if (list.some(function (x) { return x.provider_id === p.provider_id; })) return;
          list.push({ provider_id: p.provider_id, provider_name: p.provider_name });
          renderProviders(listKey);
          afterFilterChange();
        },
      });
    });

    initTokenField({
      inputId: "networkInput",
      suggestionsId: "networkAutocomplete",
      minChars: 1,
      labelOf: function (n) { return n.name; },
      localSource: function (q) {
        var needle = q.toLowerCase();
        return allNetworks.filter(function (n) {
          return (n.name || "").toLowerCase().indexOf(needle) !== -1;
        }).slice(0, 20);
      },
      onPick: function (n) {
        if (S.networks.some(function (x) { return x.id === n.id; })) return;
        S.networks.push({ id: n.id, name: n.name });
        renderNetworks();
        afterFilterChange();
      },
    });

    // Chip removal.
    var chips = $("advActiveFilterTags");
    if (chips) {
      chips.addEventListener("click", function (e) {
        var btn = e.target.closest(".mf-chip-remove");
        if (!btn) return;
        var chip = currentChips[parseInt(btn.dataset.chipIdx, 10)];
        if (!chip || !chip.clear) return;
        chip.clear();
        renderChips();
        saveState();
        if (S.paramsStr) runSearch();
      });
    }

    // Search / reset.
    var runBtn = $("runSearchBtn");
    if (runBtn) runBtn.addEventListener("click", function () { runSearch(); });

    var resetBtn = $("resetFiltersBtn");
    if (resetBtn) {
      resetBtn.addEventListener("click", function () {
        clearFilters();
        S.type = "tv";
        S.region = "";
        // Bump the generation so a page still in flight cannot repopulate the
        // grid a moment after the user cleared it.
        searchSeq++;
        inFlight = {};
        S.pages = {};
        S.total = 0;
        S.pageIndex = 0;
        S.paramsStr = null;
        resultsFreshAt = 0;
        writeFiltersToDom();
        // The token inputs keep whatever was typed but never picked — clear
        // them too, or the next focus reopens a suggestion list for a filter
        // that is no longer set.
        ["keywordInput", "includeProviderInput", "excludeProviderInput", "networkInput"].forEach(function (id) {
          if ($(id)) $(id).value = "";
        });
        renderGenres();
        loadWatchProviders();
        renderChips();
        var grid = $("resultsGrid");
        if (grid) {
          grid.innerHTML = '<div class="adv-empty-state">' + esc(
            t("Filter zurückgesetzt. Bitte wähle deine Filter und klicke auf Suchen.",
              "Filters reset. Please select your filters and click Search.")
          ) + "</div>";
        }
        var countSpan = $("resultsCount");
        if (countSpan) countSpan.textContent = "";
        var pagination = $("advPagination");
        if (pagination) { pagination.hidden = true; pagination.innerHTML = ""; }
        try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
      });
    }

    // Pagination (delegated: the pager is re-rendered on every page change).
    var pager = $("advPagination");
    if (pager) {
      pager.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-page]");
        if (!btn || btn.disabled) return;
        goToPage(btn.dataset.page);
      });
      pager.addEventListener("keydown", function (e) {
        if (e.key !== "Enter" || e.target.id !== "advPageJump") return;
        e.preventDefault();
        goToPage(e.target.value);
      });
    }

    // Result cards (delegated — the grid is rebuilt on every render).
    var grid = $("resultsGrid");
    if (grid) {
      grid.addEventListener("click", function (e) {
        var card = e.target.closest(".tmdb-card");
        if (!card) return;
        var action = e.target.closest("[data-action]");
        if (action && action.dataset.action === "details") {
          e.stopPropagation();
          openDetailsModal(card);
          return;
        }
        openStreamSearch(card);
      });
      grid.addEventListener("keydown", function (e) {
        if (e.key !== "Enter" && e.key !== " ") return;
        var card = e.target.closest(".tmdb-card");
        if (!card) return;
        e.preventDefault();
        var action = e.target.closest("[data-action]");
        if (action && action.dataset.action === "details") openDetailsModal(card);
        else openStreamSearch(card);
      });
    }

    // Details modal: close button, backdrop, Escape, and "Search streams".
    var detailsOverlay = $("advDetailsOverlay");
    if (detailsOverlay) {
      detailsOverlay.addEventListener("click", function (e) {
        var modal = $("advDetailsModal");
        if (modal && !modal.contains(e.target)) closeDetailsModal();
      });
      var detailsClose = $("advDetailsClose");
      if (detailsClose) detailsClose.addEventListener("click", closeDetailsModal);

      var detailsSearch = $("advDetailsSearchBtn");
      if (detailsSearch) {
        detailsSearch.addEventListener("click", function () {
          var index = detailsOverlay.dataset.forIndex;
          var card = grid && grid.querySelector('.tmdb-card[data-index="' + String(index).replace(/[^0-9]/g, "") + '"]');
          closeDetailsModal();
          openStreamSearch(card);
        });
      }

      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") closeDetailsModal();
      });
    }

    // Re-flowing the grid changes how many items fit on a page — keep the
    // first visible item stable instead of jumping to a random offset.
    var resizeTimer = null;
    window.addEventListener("resize", function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        if (!bufferedCount()) return;
        var newSize = getPageSize();
        if (newSize === lastPageSize) return;
        var firstItem = S.pageIndex * (lastPageSize || newSize);
        S.pageIndex = Math.floor(firstItem / newSize);
        renderPage();
      }, 250);
    });
  }

  // ── Boot ───────────────────────────────────────────────────────────────
  function init() {
    loadState();
    bindEvents();
    switchAdvTab(S.tab);
    writeFiltersToDom();
    renderChips();

    loadGenres();
    loadWatchRegions();
    loadWatchProviders();
    loadLanguages();
    loadNetworks().then(renderNetworks);

    // Badge sources used by addDownloadedBadgeForTmdb / addSyncBadgeForTmdb.
    callIfPresent("loadDownloadedFolders");
    callIfPresent("loadAutoSyncJobs");
    callIfPresent("loadCineinfoSettings");
    callIfPresent("loadGeneralSettings");

    // Restored result set from a recent visit — show it without re-querying.
    // Only pages near the last position were persisted, so drop back to the
    // first buffered grid page if the stored index is no longer covered.
    if (S.paramsStr && bufferedCount()) {
      if (!itemAt(S.pageIndex * getPageSize())) {
        var lowest = Math.min.apply(null, Object.keys(S.pages).map(Number));
        S.pageIndex = Math.floor(((lowest - 1) * TMDB_PAGE_SIZE) / getPageSize());
      }
      renderPage();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
