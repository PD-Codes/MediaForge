/* ===================================================================
   MediaForge — Home feed (the opt-in "new home page")

   Settings → General → "Use the new home page" swaps the classic home
   discovery block (one section per provider, two to four poster rows each,
   eleven rows in total) for this one: rows grouped by *question* instead of
   by source.

     New this week   — every enabled source, interleaved
     Popular right now — same
     Movies          — FilmPalast + MegaKino only

   Each poster carries a small label naming where it came from, because the
   row no longer does. Two chip groups above the rows filter sources and
   media type; the choice is remembered per browser (localStorage), not per
   account -- it is a viewing preference, not a setting.

   Everything below deliberately reuses app.js: renderBrowseCards() (so the
   "already downloaded" badge, the Auto-Sync badge, TMDB/CineInfo enrichment
   and the click-through all behave exactly as on the classic home page),
   renderSkeletons(), renderSourceChips() and applyUptimeStatus(). The only
   thing this file owns is *which items end up in which row*.
   =================================================================== */

(function () {
  const feed = document.getElementById("homeFeed");
  if (!feed) return;                       // classic home page — nothing to do

  const SOURCE_LABELS = {
    aniworld: "AniWorld",
    sto: "SerienStream",
    filmpalast: "FilmPalast",
    megakino: "MegaKino",
    hanime: "hanime",
  };
  const SOURCE_ORDER = ["aniworld", "sto", "filmpalast", "megakino", "hanime"];

  // [source, row, type, endpoint]. "adult" is its own type so the 18+ chip can
  // switch hanime off without touching the source chips.
  const ENDPOINTS = [
    ["aniworld", "new", "series", "/api/new-animes"],
    ["aniworld", "popular", "series", "/api/popular-animes"],
    ["sto", "new", "series", "/api/new-series"],
    ["sto", "popular", "series", "/api/popular-series"],
    ["filmpalast", "new", "movies", "/api/new-movies"],
    ["megakino", "new", "movies", "/api/megakino/new-movies"],
    ["megakino", "popular", "movies", "/api/megakino/popular-movies"],
    ["megakino", "new", "series", "/api/megakino/new-series"],
    ["megakino", "popular", "series", "/api/megakino/popular-series"],
    ["hanime", "new", "adult", "/api/hanime/new"],
    ["hanime", "popular", "adult", "/api/hanime/trending"],
  ];

  const ROW_GRIDS = { new: "feedNewGrid", popular: "feedPopularGrid", movies: "feedMoviesGrid" };
  const ROW_MAX = 30;                      // per row, after interleaving
  const FILTER_KEY = "homeFeedFilters";

  let sourcesSetting = {};
  let enabledSources = [];                 // ids, in the user's own order
  let items = [];                          // every fetched item, tagged
  let filters = { sources: {}, types: { series: true, movies: true, adult: false } };

  // ---------------------------------------------------------------- filters
  function loadFilters() {
    try {
      const raw = JSON.parse(localStorage.getItem(FILTER_KEY) || "{}");
      if (raw && raw.sources) filters.sources = raw.sources;
      if (raw && raw.types) Object.assign(filters.types, raw.types);
    } catch (e) { /* first visit, or a browser that refuses storage */ }
  }

  function saveFilters() {
    try { localStorage.setItem(FILTER_KEY, JSON.stringify(filters)); } catch (e) { }
  }

  /** A source is shown unless it was explicitly switched off in the chips. */
  function sourceOn(id) { return filters.sources[id] !== false; }
  function allSourcesOn() { return enabledSources.every(sourceOn); }

  function renderFilters() {
    const wrap = document.getElementById("feedFilters");
    if (!wrap) return;
    const chip = (on, label, kind, value) =>
      '<button type="button" class="feed-chip' + (on ? " is-on" : "") + '"' +
      ' data-kind="' + kind + '" data-value="' + escapeHtml(value) + '"' +
      ' aria-pressed="' + (on ? "true" : "false") + '">' + escapeHtml(label) + "</button>";

    let html = '<span class="feed-chip-label">' + escapeHtml(t("Quellen", "Sources")) + "</span>";
    html += chip(allSourcesOn(), t("Alle", "All"), "all", "all");
    enabledSources.forEach(function (id) {
      if (id === "hanime") return;         // reached through the 18+ chip instead
      html += chip(sourceOn(id), SOURCE_LABELS[id] || id, "source", id);
    });
    html += '<span class="feed-chip-label feed-chip-label--split">' + escapeHtml(t("Art", "Type")) + "</span>";
    html += chip(filters.types.series, t("Serien", "Series"), "type", "series");
    html += chip(filters.types.movies, t("Filme", "Movies"), "type", "movies");
    if (enabledSources.indexOf("hanime") !== -1) {
      html += chip(filters.types.adult, "18+", "type", "adult");
    }
    wrap.innerHTML = html;
  }

  feed.addEventListener("click", function (ev) {
    const btn = ev.target.closest(".feed-chip");
    if (!btn) return;
    const kind = btn.dataset.kind;
    if (kind === "all") {
      const turnOn = !allSourcesOn();
      enabledSources.forEach(function (id) { filters.sources[id] = turnOn ? true : (id === enabledSources[0]); });
    } else if (kind === "source") {
      const id = btn.dataset.value;
      const next = !sourceOn(id);
      // Never leave every source off — that is an empty page, not a filter.
      if (!next && enabledSources.filter(function (s) { return s !== id && sourceOn(s); }).length === 0) return;
      filters.sources[id] = next;
    } else if (kind === "type") {
      const key = btn.dataset.value;
      const next = !filters.types[key];
      const others = Object.keys(filters.types).filter(function (k) { return k !== key && filters.types[k]; });
      if (!next && others.length === 0) return;
      filters.types[key] = next;
    } else {
      return;
    }
    saveFilters();
    renderFilters();
    renderRows();
  });

  // ---------------------------------------------------------------- rows
  /** Round-robin over the sources so a row never opens with 20 AniWorld cards. */
  function interleave(list) {
    const buckets = enabledSources.map(function (id) {
      return list.filter(function (i) { return i._src === id; });
    });
    const out = [];
    for (let n = 0; out.length < ROW_MAX; n++) {
      let took = false;
      for (let b = 0; b < buckets.length; b++) {
        if (buckets[b][n] !== undefined) { out.push(buckets[b][n]); took = true; }
        if (out.length >= ROW_MAX) break;
      }
      if (!took) break;
    }
    return out;
  }

  function normTitle(s) {
    return String(s || "").toLowerCase().replace(/[\s._:!?,'"()\-]+/g, "");
  }

  /** The same title from two sources becomes one card that names both. */
  function dedupe(list) {
    const seen = {};
    const out = [];
    list.forEach(function (item) {
      const key = normTitle(item.title) + "|" + item._type;
      if (seen[key]) {
        const first = seen[key];
        if (first._also.indexOf(item._src) === -1 && item._src !== first._src) first._also.push(item._src);
        return;
      }
      item._also = [];
      seen[key] = item;
      out.push(item);
    });
    return out;
  }

  function rowItems(row) {
    return interleave(dedupe(items.filter(function (i) {
      return i._row === row && sourceOn(i._src) && filters.types[i._type];
    })));
  }

  /** The origin label the row no longer carries. Added after renderBrowseCards()
      so app.js stays the single owner of what a browse card looks like. */
  function addSourcePills(grid, list) {
    if (grid.children.length !== list.length) return;
    for (let i = 0; i < list.length; i++) {
      const item = list[i];
      const label = SOURCE_LABELS[item._src] || item._src;
      const extra = (item._also && item._also.length) ? " +" + item._also.length : "";
      const pill = document.createElement("span");
      pill.className = "browse-src-pill";
      pill.textContent = label + extra;
      if (extra) {
        pill.title = [item._src].concat(item._also).map(function (s) {
          return SOURCE_LABELS[s] || s;
        }).join(" · ");
      }
      grid.children[i].appendChild(pill);
    }
  }

  function renderRows() {
    Object.keys(ROW_GRIDS).forEach(function (row) {
      const grid = document.getElementById(ROW_GRIDS[row]);
      const section = grid && grid.closest(".browse-section");
      if (!grid) return;
      const list = rowItems(row);
      if (!list.length) {
        // An empty row is hidden rather than shown empty — with filters on,
        // three "nothing here" blocks say less than two filled rows.
        if (section) section.style.display = "none";
        grid.innerHTML = "";
        return;
      }
      if (section) section.style.display = "";
      renderBrowseCards(grid, list, { skipTmdb: list.every(function (i) { return i._type === "adult"; }) });
      addSourcePills(grid, list);
    });
    const anyVisible = Object.keys(ROW_GRIDS).some(function (row) {
      const g = document.getElementById(ROW_GRIDS[row]);
      const s = g && g.closest(".browse-section");
      return s && s.style.display !== "none";
    });
    const empty = document.getElementById("feedEmpty");
    if (empty) empty.style.display = anyVisible ? "none" : "";
  }

  // ---------------------------------------------------------------- loading
  let loadedAt = 0;

  async function load() {
    if (loadedAt && Date.now() - loadedAt < 3600000) return;
    loadedAt = Date.now();

    let settings = {};
    try { settings = await loadGeneralSettings(); } catch (e) { settings = {}; }
    sourcesSetting = (settings && settings.sources) || {};
    const on = sourcesSetting.enabled || {};
    const order = String(sourcesSetting.order || "").split(",")
      .map(function (s) { return s.trim().toLowerCase(); })
      .filter(function (s) { return SOURCE_ORDER.indexOf(s) !== -1; });
    SOURCE_ORDER.forEach(function (s) { if (order.indexOf(s) === -1) order.push(s); });
    // hanime is opt-in ("1"), every other source is opt-out ("0").
    enabledSources = order.filter(function (id) {
      return id === "hanime" ? on[id] === "1" : on[id] !== "0";
    });

    renderSourceChips(sourcesSetting);
    applyUptimeStatus();
    loadFilters();
    renderFilters();

    Object.keys(ROW_GRIDS).forEach(function (row) {
      const grid = document.getElementById(ROW_GRIDS[row]);
      if (grid) renderSkeletons(grid, 12);
    });

    // Badges need their own data before the cards are built, exactly as
    // loadAniworldBrowse() does it on the classic page.
    await Promise.all([loadDownloadedFolders(), loadAutoSyncJobs()]);

    const wanted = ENDPOINTS.filter(function (e) { return enabledSources.indexOf(e[0]) !== -1; });
    const results = await Promise.all(wanted.map(async function (e) {
      try {
        const resp = await fetch(e[3]);
        const data = await resp.json();
        return (data && data.results) || [];
      } catch (err) {
        return [];                        // one dead source must not empty the page
      }
    }));

    items = [];
    results.forEach(function (list, n) {
      const [src, row, type] = wanted[n];
      list.forEach(function (item) {
        items.push(Object.assign({}, item, { _src: src, _row: row, _type: type }));
        // Movies also feed their own row, so "Movies" is complete whether a
        // title showed up under new or under popular.
        if (type === "movies" && row !== "movies") {
          items.push(Object.assign({}, item, { _src: src, _row: "movies", _type: type }));
        }
      });
    });

    if (!items.length) loadedAt = 0;       // let a reload try again
    renderRows();
  }

  window.reloadHomeFeed = function () { loadedAt = 0; load(); };
  load();
})();
