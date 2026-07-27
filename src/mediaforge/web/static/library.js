// ============================================================
// Mediathek — File Explorer
// ============================================================
// Render architecture (2026-07 modernization): every title, across every
// location/lang-folder, is flattened into one list of
// {title, cpId, cpLabel, langFolder} items and painted as a single
// mf-poster-grid (or list) instead of nested location/lang-folder shells.
// Clicking a card/row expands its detail INLINE, directly below it in DOM
// order (grid-column: 1 / -1), instead of opening a panel elsewhere on the
// page. Only one item is open at a time. Season/episode rendering, the
// kebab menu system, and every mutating action (rename/delete/move/
// upscale/auto-sync) are unchanged from the previous implementation —
// only how titles are grouped and painted changed.

var libLangSep       = false;
var libLocations     = [];
var libAllTargets    = [];
var libScanPollTimer = null;
var libIdlePollTimer = null;  // slow background poll to catch watcher-triggered rescans
var libLastUpdated   = 0;     // scanned_at timestamp of the last full render
var libSearchQuery   = "";    // current search filter
var _libSearchTimer  = null;  // debounce timer for search input
var libSortKey       = "name"; // "name" | "size" | "episodes"
var libSortAsc       = true;   // ascending = true
var libFilterMode    = "all";  // "all" | "series" | "movies"
var libViewMode      = "grid"; // "grid" | "list"

// Pagination — purely client-side: the whole filtered/sorted item list is
// already in memory (libFlattenTitles/libFilterTitles/libSortTitles), so
// paging is just a slice, no extra network round-trip. Mirrors the
// .mf-pagination usage pattern from history.js (numbered pager + a
// 10/20/50/100 "results per page" <select>, persisted in localStorage).
var LIB_PER_PAGE_OPTIONS = [10, 20, 50, 100];
function _libInitialPerPage() {
  try {
    var saved = parseInt(localStorage.getItem("mf-lib-perpage"), 10);
    if (LIB_PER_PAGE_OPTIONS.indexOf(saved) !== -1) return saved;
  } catch (e) { /* private mode */ }
  return 20;
}
var libPerPage = _libInitialPerPage();
var libPage    = 0; // 0-based

// Single-open accordion state — which flattened item (by stable key) is
// currently expanded, and which of its seasons are expanded. Survives
// re-renders triggered by the idle poll / watcher so a background refresh
// never silently collapses what the user has open.
var _libOpenKey     = null;        // _libTitleKey() of the open item, or null
var _libOpenSeasons = new Set();   // "sN" keys open within the current item

// ---- Boot ----

async function libLoad(forceRefresh) {
  if (forceRefresh) {
    var refreshResp = await fetch("/api/library/refresh", { method: "POST" });
    var refreshData = await refreshResp.json();
  }
  await libFetch();
}

async function libFetch() {
  try {
    var resp = await fetch("/api/library");
    var data = await resp.json();
    libLangSep   = !!data.lang_sep;
    libLocations = data.locations || [];
    libAllTargets = libLocations.map(function(loc) {
      return { label: loc.label, custom_path_id: loc.custom_path_id };
    });

    // Track when we last rendered so the idle poll can detect watcher updates
    libLastUpdated = data.last_updated || 0;

    libRender(libLocations);
    libUpdateWatcherStatus(data.watcher || {});
    libUpdateTotalSize(libLocations);

    if (data.is_scanning) {
      libShowScanBadge(true);
      if (!libScanPollTimer) {
        libScanPollTimer = window.mfPoll(libPollScan, 2500);
      }
    } else {
      libShowScanBadge(false);
      if (libScanPollTimer) {
        window.mfPollStop(libScanPollTimer);
        libScanPollTimer = null;
      }
      libUpdateTimestamp();
    }

    // Start idle poll if not already running
    if (!libIdlePollTimer) {
      libIdlePollTimer = window.mfPoll(libIdlePoll, 8000);
    }
  } catch (e) {
    var gridEl = document.getElementById("libGridView");
    var listEl = document.getElementById("libListView");
    var emptyEl = document.getElementById("libEmptyState");
    if (gridEl) gridEl.hidden = true;
    if (listEl) listEl.hidden = true;
    if (emptyEl) {
      emptyEl.hidden = false;
      emptyEl.innerHTML = '<p>' + t("Bibliothek konnte nicht geladen werden.", "Could not load the library.") + '</p>';
    }
  }
}

// Cheap background check: only reads a tiny status object from DB (no disk scan).
// Re-renders only when the watcher has updated the cache since last render.
async function libIdlePoll() {
  // Skip if a scan poll is already running (it will handle the update)
  if (libScanPollTimer) return;
  try {
    var resp = await fetch("/api/library/status");
    var status = await resp.json();
    if (status.is_scanning) {
      // Watcher just triggered a scan — hand off to scan poller
      libShowScanBadge(true);
      if (!libScanPollTimer) {
        libScanPollTimer = window.mfPoll(libPollScan, 2500);
      }
    } else if (status.last_updated > libLastUpdated) {
      // Cache was updated since our last render — fetch and re-render
      await libFetch();
    }
  } catch (e) { /* ignore network errors */ }
}

// Poll only while a scan is running — stops itself when done
async function libPollScan() {
  try {
    var resp = await fetch("/api/library");
    var data = await resp.json();
    libUpdateWatcherStatus(data.watcher || {});
    if (!data.is_scanning) {
      libLangSep   = !!data.lang_sep;
      libLocations = data.locations || [];
      libAllTargets = libLocations.map(function(loc) {
        return { label: loc.label, custom_path_id: loc.custom_path_id };
      });
      libLastUpdated = data.last_updated || 0;
      libRender(libLocations);
      libUpdateTotalSize(libLocations);
      libShowScanBadge(false);
      window.mfPollStop(libScanPollTimer);
      libScanPollTimer = null;
      libUpdateTimestamp();
    }
  } catch (e) {}
}

function libUpdateTimestamp() {
  var el = document.getElementById("libLastScanned");
  if (el) el.textContent = t("Aktualisiert: ", "Updated: ") + new Date().toLocaleTimeString(window.__LANG === 'de' ? 'de-DE' : 'en-US', { hour: "2-digit", minute: "2-digit" });
}

function libShowScanBadge(visible) {
  var badge = document.getElementById("libScanBadge");
  var btn   = document.getElementById("libRefreshBtn");
  if (badge) {
    badge.style.display = visible ? "inline-flex" : "none";
  }
  if (btn) {
    btn.disabled = visible;
    btn.classList.toggle("spin", visible);
  }
}

function libUpdateWatcherStatus(watcher) {
  var dot   = document.getElementById("libWatcherDot");
  var label = document.getElementById("libWatcherLabel");
  var tip   = document.getElementById("libWatcherTip");
  if (!dot || !label) return;

  if (!watcher.available) {
    dot.className   = "lib-watcher-dot lib-watcher-off";
    label.textContent = t("Watcher inaktiv", "Watcher inactive");
    if (tip) tip.title = "watchdog nicht installiert (pip install watchdog)";
    return;
  }
  if (watcher.active) {
    dot.className   = "lib-watcher-dot lib-watcher-on";
    label.textContent = t("Watcher aktiv", "Watcher active");
    if (tip && watcher.watched && watcher.watched.length) {
      tip.title = "Überwacht: " + watcher.watched.map(function(w){ return w.path; }).join(", ");
    }
  } else {
    dot.className   = "lib-watcher-dot lib-watcher-starting";
    label.textContent = "Watcher startet…";
  }
}

// ---- Total size ----

function libUpdateTotalSize(locations) {
  var total = 0, totalEps = 0, totalMovies = 0, totalSeries = 0;
  locations.forEach(function(loc) {
    var titles = [];
    if (loc.lang_folders) {
      loc.lang_folders.forEach(function(lf) { titles = titles.concat(lf.titles || []); });
    } else if (loc.titles) {
      titles = titles.concat(loc.titles);
    }
    titles.forEach(function(t) {
      total += t.total_size || 0;
      if (t.is_movie) totalMovies++;
      else { totalSeries++; totalEps += t.total_episodes || 0; }
    });
  });

  var pillsEl = document.getElementById("libSummaryPills");
  if (pillsEl) {
    var parts = [];
    if (totalSeries > 0) parts.push('<span class="lib-summary-pill"><b>' + totalSeries + '</b> ' + t('Serien', 'Series') + '</span>');
    if (totalEps > 0)    parts.push('<span class="lib-summary-pill"><b>' + totalEps + '</b> ' + t('Episoden', 'Episodes') + '</span>');
    if (totalMovies > 0) parts.push('<span class="lib-summary-pill lib-summary-pill--film"><b>' + totalMovies + '</b> ' + (window.__LANG === 'de' ? 'Film' + (totalMovies !== 1 ? 'e' : '') : (totalMovies !== 1 ? 'Movies' : 'Movie')) + '</span>');
    if (total > 0)       parts.push('<span class="lib-summary-pill"><b>' + libFmtSize(total) + '</b> ' + t('gesamt', 'total') + '</span>');
    pillsEl.innerHTML = parts.join("");
  }
}

// ---- Sort / Filter ----

function libSetSort(key) {
  if (libSortKey === key) {
    libSortAsc = !libSortAsc; // toggle direction on second click
  } else {
    libSortKey = key;
    libSortAsc = key === "name"; // name defaults A→Z, others default big→small
  }
  // Update button active state + direction arrows
  ["name", "size", "episodes"].forEach(function(k) {
    var btn = document.getElementById("libSort-" + k);
    var dir = document.getElementById("libSortDir-" + k);
    if (!btn) return;
    btn.classList.toggle("active", k === libSortKey);
    if (dir) dir.textContent = (k === libSortKey) ? (libSortAsc ? "↑" : "↓") : "";
    if (k === libSortKey) {
      btn.title = (k === "name")
        ? (libSortAsc ? "A–Z (klicken für Z–A)" : "Z–A (klicken für A–Z)")
        : (libSortAsc ? t("Aufsteigend", "Ascending") : t("Absteigend", "Descending"));
    }
  });
  libPage = 0; // sort order changed — start back on page 1
  libRender(libLocations);
}

function libSetFilter(mode) {
  libFilterMode = mode;
  ["all", "series", "movies"].forEach(function(k) {
    var btn = document.getElementById("libFilter-" + k);
    if (btn) btn.classList.toggle("active", k === mode);
  });
  libPage = 0; // result set changed — start back on page 1
  libRender(libLocations);
}

// Filter/sort operate on flattened {title, cpId, cpLabel, langFolder} items.
function libFilterTitles(items) {
  if (libFilterMode === "all") return items;
  return items.filter(function(it) {
    return libFilterMode === "movies" ? !!it.title.is_movie : !it.title.is_movie;
  });
}

function libSortTitles(items) {
  return items.slice().sort(function(a, b) {
    var v;
    if (libSortKey === "size") v = (a.title.total_size || 0) - (b.title.total_size || 0);
    else if (libSortKey === "episodes") v = (a.title.total_episodes || 0) - (b.title.total_episodes || 0);
    else v = (a.title.folder || "").localeCompare(b.title.folder || "", "de", { sensitivity: "base" });
    return libSortAsc ? v : -v;
  });
}

// ---- Search ----

function libOnSearch(value) {
  if (_libSearchTimer) clearTimeout(_libSearchTimer);
  _libSearchTimer = setTimeout(function() {
    libSearchQuery = value.trim();
    var clearBtn = document.getElementById("libSearchClear");
    if (clearBtn) clearBtn.hidden = !libSearchQuery;
    libPage = 0; // result set changed — start back on page 1
    requestAnimationFrame(function() { libRender(libLocations); });
  }, 200);
}

function libClearSearch() {
  var input = document.getElementById("libSearchInput");
  if (input) { input.value = ""; input.focus(); }
  libSearchQuery = "";
  var clearBtn = document.getElementById("libSearchClear");
  if (clearBtn) clearBtn.hidden = true;
  libPage = 0; // result set changed — start back on page 1
  libRender(libLocations);
}

function libSetView(mode) {
  if (mode !== "grid" && mode !== "list") return;
  libViewMode = mode;
  var gBtn = document.getElementById("libViewGrid");
  var lBtn = document.getElementById("libViewList");
  if (gBtn) { gBtn.classList.toggle("active", mode === "grid"); gBtn.setAttribute("aria-pressed", mode === "grid"); }
  if (lBtn) { lBtn.classList.toggle("active", mode === "list"); lBtn.setAttribute("aria-pressed", mode === "list"); }
  libRender(libLocations);
}

// ---- Flatten ----
// Every title across every location/lang-folder becomes one flat item, so
// the whole library paints as a single poster grid / list regardless of
// how many volumes (custom paths) or language folders it spans.

var _libLazy = {};          // bodyId → {title, cpId, langFolder, pfx, ...} for the lazy detail fill
var _libUpscaleTitles = {}; // upscaleKey → title object (read by libUpscaleTitle)

function libFlattenTitles(locations) {
  var items = [];
  locations.forEach(function(loc) {
    if (libLangSep && loc.lang_folders) {
      loc.lang_folders.forEach(function(lf) {
        (lf.titles || []).forEach(function(title) {
          items.push({ title: title, cpId: loc.custom_path_id, cpLabel: loc.label, langFolder: lf.name });
        });
      });
    } else if (loc.titles) {
      loc.titles.forEach(function(title) {
        items.push({ title: title, cpId: loc.custom_path_id, cpLabel: loc.label, langFolder: null });
      });
    }
  });
  return items;
}

function _libTitleKey(p) {
  return (p.title.folder || "") + "|" +
         (p.cpId !== null && p.cpId !== undefined ? p.cpId : "default") + "|" +
         (p.langFolder || "");
}

function _libExpandEl(el) {
  if (!el) return;
  el.classList.add("lib-expanded");
  var header = el.previousElementSibling;
  if (header) {
    var arrow = header.querySelector(".lib-arrow");
    if (arrow) arrow.classList.add("lib-arrow-open");
  }
}

function libTitleMatchesQuery(title, q) {
  // Match against the folder name or any episode/movie file name.
  if ((title.folder || "").toLowerCase().includes(q)) return true;
  if (title.seasons) {
    var seasonKeys = Object.keys(title.seasons);
    for (var si = 0; si < seasonKeys.length; si++) {
      var eps = title.seasons[seasonKeys[si]] || [];
      for (var fi = 0; fi < eps.length; fi++) {
        if ((eps[fi].file || "").toLowerCase().includes(q)) return true;
      }
    }
  }
  return false;
}

// ---- Render ----

function libRender(locations) {
  if (libSearchQuery) { libRenderSearchResults(libSearchQuery); return; }
  var items = libSortTitles(libFilterTitles(libFlattenTitles(locations)));
  libPaintItems(items);
}

function libRenderSearchResults(query) {
  var q = query.toLowerCase();
  var matches = libFlattenTitles(libLocations).filter(function(it) {
    return libTitleMatchesQuery(it.title, q);
  });
  libPaintItems(matches);
}

// Re-paint using the data already in memory (no network round-trip) —
// used when opening/closing a card or switching filter/sort/view locally
// would otherwise require duplicating the fetch-then-render flow.
function libRepaint() {
  libRender(libLocations);
}

function libPaintItems(items) {
  var gridEl  = document.getElementById("libGridView");
  var listEl  = document.getElementById("libListView");
  var emptyEl = document.getElementById("libEmptyState");
  if (!gridEl || !listEl) return;

  if (!items.length) {
    gridEl.hidden = true;
    listEl.hidden = true;
    if (emptyEl) emptyEl.hidden = false;
    libRenderPagination(0);
    return;
  }
  if (emptyEl) emptyEl.hidden = true;

  var isGrid = libViewMode === "grid";
  gridEl.hidden = !isGrid;
  listEl.hidden = isGrid;
  var target = isGrid ? gridEl : listEl;

  // Slice to the current page. Clamp first in case a filter/sort/search
  // change (or a background refresh) shrank the list out from under the
  // previously selected page.
  var totalP = libTotalPages(items.length);
  if (libPage >= totalP) libPage = totalP - 1;
  if (libPage < 0) libPage = 0;
  var pageStart = libPage * libPerPage;
  var pageItems = items.slice(pageStart, pageStart + libPerPage);

  _libLazy = {};
  var html = [];
  var openItem = null, openPfx = null;
  pageItems.forEach(function(it, idx) {
    var pfx = "libCard" + idx;
    _libLazy[pfx + "Body"] = { title: it.title, li: 0, langFolder: it.langFolder, lfi: null, ti: 0, cpId: it.cpId, pfx: pfx };
    html.push(isGrid ? libRenderCard(it, pfx) : libRenderRow(it, pfx));
    if (_libOpenKey && _libTitleKey(it) === _libOpenKey) {
      html.push(libRenderDetailShell(it, pfx));
      openItem = it;
      openPfx = pfx;
    }
  });
  target.innerHTML = html.join("");

  if (openItem) {
    var bodyEl = document.getElementById(openPfx + "Body");
    if (bodyEl) {
      libRenderDetailBody(bodyEl, openItem, openPfx);
      _libOpenSeasons.forEach(function(sKey) {
        var sBody = document.getElementById(openPfx + "_" + sKey + "Body");
        if (sBody) _libExpandEl(sBody);
      });
    }
  }
  libRenderPagination(items.length);
  _libScheduleProgressFlush();
}

// ---- Pagination ----

function libTotalPages(total) {
  return Math.max(1, Math.ceil(total / libPerPage));
}

function libPageNumbers(current, total) {
  if (total <= 7) return Array.from({ length: total }, function (_, i) { return i + 1; });
  var out = [1];
  var from = Math.max(2, current - 1), to = Math.min(total - 1, current + 1);
  if (from > 2) out.push("…");
  for (var i = from; i <= to; i++) out.push(i);
  if (to < total - 1) out.push("…");
  out.push(total);
  return out;
}

// Rebuilds the pager + "Showing X–Y of Z" text + per-page <select> every
// time it's called (cheap — the pager is a handful of buttons), same
// convention as history.js's renderPagination(). Clicks are delegated via
// data-page since the whole pager is replaced on every page change.
function libRenderPagination(totalItems) {
  var row    = document.getElementById("libPaginationRow");
  var host   = document.getElementById("libPagination");
  var cnt    = document.getElementById("libPageCount");
  var perSel = document.getElementById("libPerPageSelect");

  if (perSel && perSel.value !== String(libPerPage)) perSel.value = String(libPerPage);
  if (row) row.hidden = totalItems === 0;

  if (cnt) {
    var from = totalItems ? libPage * libPerPage + 1 : 0;
    var to = Math.min(totalItems, (libPage + 1) * libPerPage);
    cnt.textContent = t("Zeige " + from + "–" + to + " von " + totalItems,
      "Showing " + from + "–" + to + " of " + totalItems);
  }
  if (!host) return;

  var totalP = libTotalPages(totalItems);
  var current = libPage + 1;
  if (totalP <= 1) { host.innerHTML = ""; return; }

  var btn = function (page, label, disabled, title) {
    return '<button type="button" class="mf-pagination-btn" data-page="' + page + '"' +
      (disabled ? " disabled" : "") + ' title="' + libEscAttr(title) + '">' + label + "</button>";
  };
  var html = '<div class="mf-pagination">';
  html += btn(1, "&laquo;", current === 1, t("Erste Seite", "First page"));
  html += btn(current - 1, "&lsaquo;", current === 1, t("Zurück", "Back"));
  libPageNumbers(current, totalP).forEach(function (entry) {
    if (entry === "…") { html += '<span class="mf-pagination-ellipsis">…</span>'; return; }
    html += '<button type="button" class="mf-pagination-page' + (entry === current ? " active" : "") +
      '" data-page="' + entry + '"' + (entry === current ? " disabled" : "") + ">" + entry + "</button>";
  });
  html += btn(current + 1, "&rsaquo;", current === totalP, t("Weiter", "Next"));
  html += btn(totalP, "&raquo;", current === totalP, t("Letzte Seite", "Last page"));
  html += "</div>";
  host.innerHTML = html;
  host.querySelectorAll("[data-page]").forEach(function (b) {
    b.addEventListener("click", function () { libGoToPage(parseInt(b.getAttribute("data-page"), 10) - 1); });
  });
}

function libGoToPage(n) {
  n = Math.max(0, n);
  if (n === libPage) return;
  libPage = n;
  libRepaint();
  var toolbar = document.getElementById("libToolbar");
  if (toolbar && toolbar.scrollIntoView) toolbar.scrollIntoView({ behavior: "smooth", block: "start" });
}

function libSetPerPage(value) {
  var n = parseInt(value, 10);
  if (LIB_PER_PAGE_OPTIONS.indexOf(n) === -1) n = 20;
  libPerPage = n;
  try { localStorage.setItem("mf-lib-perpage", String(n)); } catch (e) { /* private mode */ }
  libPage = 0;
  libRepaint();
}

// ---- Shared card/row/detail markup helpers ----

function typePillHtml(isMovie) {
  return '<span class="mf-type-pill' + (isMovie ? ' mf-type-pill--movie' : ' mf-type-pill--series') + '">' +
    (isMovie ? t('Film', 'Movie') : t('Serie', 'Series')) + '</span>';
}

function volTagHtml(cpLabel) {
  if (!cpLabel) return '';
  return '<span class="mf-vol-tag" title="' + libEscAttr(cpLabel) + '">' +
    '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg>' +
    '<span>' + libEsc(cpLabel) + '</span></span>';
}

// "Neu" flag: title was written to disk within the last N days.
// Backed by title.added_at (Unix seconds), populated server-side in
// _lib_scan_base() from the newest st_mtime among the title's files.
var LIB_NEW_DAYS = 14;
function isNewTitle(title) {
  if (!title.added_at) return false;
  return (Date.now() / 1000 - title.added_at) < (LIB_NEW_DAYS * 86400);
}

function _libFauxArt(name) {
  var hash = 0;
  for (var i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  var hue1 = hash % 360, hue2 = (hue1 + 48) % 360;
  var style = 'background:linear-gradient(155deg,hsl(' + hue1 + ',55%,22%),hsl(' + hue2 + ',55%,14%))';
  return '<div class="lib-fauxart" style="' + style + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg></div>';
}

// Primary/first file of a title — used for the card-level "Details" menu
// item (movies) and the eager card-level watch-progress prefetch.
function _libFirstFile(title) {
  if (title.seasons && title.seasons.movies && title.seasons.movies.length) return title.seasons.movies[0];
  return null;
}

// Queue an eager, card-level progress lookup for a movie's primary file so
// the "watched" badge / progress bar can show up without the user having
// to expand the card first. Series intentionally do NOT do this — their
// episodes are only fetched on first expand to keep large libraries
// (thousands of episodes) fast to paint.
function _libQueueCardProgress(it, pfx) {
  if (!it.title.is_movie) return;
  var f = _libFirstFile(it.title);
  if (!f || !f.path) return;
  _libPendingProgress.push({ rowId: pfx, path: f.path, kind: 'card' });
}

function _libCardMenuKey(it, pfx) {
  var upscaleKey = "usc_" + pfx;
  _libUpscaleTitles[upscaleKey] = it.title;
  var firstFile = it.title.is_movie ? _libFirstFile(it.title) : null;
  return libRegMenuCtx({
    type: it.title.is_movie ? 'movie' : 'title',
    folder: it.title.folder, cpId: it.cpId, lf: it.langFolder,
    pfx: pfx, upscaleKey: upscaleKey, isMovie: !!it.title.is_movie,
    epPath: it.title.is_movie ? (firstFile ? firstFile.path : '') : undefined
  });
}

function libRenderCard(it, pfx) {
  var title = it.title;
  var isOpen = _libOpenKey === _libTitleKey(it);
  var menuKey = _libCardMenuKey(it, pfx);
  _libQueueCardProgress(it, pfx);

  var h = [];
  h.push('<div class="mf-poster-card' + (isOpen ? ' is-open' : '') + '" id="' + pfx + '">');
  h.push('<div class="mf-poster-art" onclick="libOpenTitle(\'' + pfx + '\')">');
  h.push(_libFauxArt(title.folder));
  if (isNewTitle(title)) h.push('<span class="mf-poster-flag mf-poster-flag--new">' + t('Neu', 'New') + '</span>');
  h.push('<div class="mf-poster-scrim">');
  h.push('<div class="mf-poster-meta">' + typePillHtml(title.is_movie) + volTagHtml(it.cpLabel) + '</div>');
  h.push('<p class="mf-poster-title" id="' + pfx + 'Name">' + libEsc(title.folder) + '</p>');
  h.push('</div>'); // scrim
  h.push('<div class="mf-poster-progress"><div class="mf-poster-progress-fill" style="width:0%"></div></div>');
  h.push('</div>'); // art
  h.push('<div class="mf-poster-foot">');
  h.push('<span class="lib-badge">' + title.total_episodes + (title.is_movie ? ' Film' : ' Ep.') + '</span>');
  h.push('<span class="lib-badge lib-badge-size">' + libFmtSize(title.total_size) + '</span>');
  h.push('<button class="mf-icon-btn lib-kebab-btn mf-poster-kebab" data-libkey="' + menuKey + '" onclick="event.stopPropagation();libOpenMenu(this)" title="' + t('Mehr Optionen', 'More Options') + '"><svg viewBox="0 0 6 24"><circle cx="3" cy="5" r="2"/><circle cx="3" cy="12" r="2"/><circle cx="3" cy="19" r="2"/></svg></button>');
  h.push('</div>'); // foot
  h.push('</div>'); // card
  return h.join("");
}

function libRenderRow(it, pfx) {
  var title = it.title;
  var isOpen = _libOpenKey === _libTitleKey(it);
  var menuKey = _libCardMenuKey(it, pfx);
  _libQueueCardProgress(it, pfx);

  var h = [];
  h.push('<div class="lib-title-row lib-hoverable' + (isOpen ? ' is-open' : '') + '" id="' + pfx + '" onclick="libOpenTitle(\'' + pfx + '\')">');
  h.push('<div class="lib-row-left">');
  // Title line and pills are split into their own wrappers so mobile CSS
  // can stack the pills below the title (see .lib-row-title-line /
  // .lib-row-pills in library.css) — on desktop both sit side by side,
  // same as before.
  h.push('<div class="lib-row-title-line">');
  h.push('<svg class="lib-arrow' + (isOpen ? ' lib-arrow-open' : '') + '" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>');
  h.push('<svg class="lib-icon lib-icon-folder" viewBox="0 0 24 24"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg>');
  h.push('<span class="lib-title-name" id="' + pfx + 'Name">' + libEsc(title.folder) + '</span>');
  h.push('</div>'); // row-title-line
  h.push('<div class="lib-row-pills">');
  h.push(typePillHtml(title.is_movie));
  h.push(volTagHtml(it.cpLabel));
  if (isNewTitle(title)) h.push('<span class="mf-poster-flag mf-poster-flag--new lib-row-new-flag">' + t('Neu', 'New') + '</span>');
  h.push('</div>'); // row-pills
  h.push('</div>'); // row-left
  h.push('<div class="lib-row-right">');
  h.push('<span class="lib-badge">' + title.total_episodes + (title.is_movie ? ' Film' : ' Ep.') + '</span>');
  h.push('<span class="lib-badge lib-badge-size">' + libFmtSize(title.total_size) + '</span>');
  h.push('<button class="lib-kebab-btn" data-libkey="' + menuKey + '" onclick="event.stopPropagation();libOpenMenu(this)" title="' + t('Mehr Optionen', 'More Options') + '"><svg viewBox="0 0 6 24"><circle cx="3" cy="5" r="2"/><circle cx="3" cy="12" r="2"/><circle cx="3" cy="19" r="2"/></svg></button>');
  h.push('</div>'); // row-right
  h.push('</div>'); // row
  return h.join("");
}

function libDetailHeaderHtml(it) {
  var title = it.title;
  var h = [];
  h.push('<div class="lib-detail-header">');
  h.push('<span class="lib-detail-title">' + libEsc(title.folder) + '</span>');
  h.push(typePillHtml(title.is_movie));
  h.push(volTagHtml(it.cpLabel));
  h.push('<button class="mf-icon-btn lib-detail-close" onclick="libCloseTitle()" title="' + t('Schließen', 'Close') + '">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg></button>');
  h.push('</div>');
  return h.join("");
}

function libRenderDetailShell(it, pfx) {
  return '<div class="lib-detail-row">' + libDetailHeaderHtml(it) +
    '<div class="lib-body lib-expanded" id="' + pfx + 'Body"></div></div>';
}

// Fills the (already inserted, empty) detail body for the currently open
// item. Movies reuse libRenderMovieFlat's own self-contained file rows
// (same per-file kebab/rename/delete semantics movies have always had —
// libFillTitleBody's is_movie branch is intentionally NOT used here since
// its libRenderEpisode()-based rows wire per-episode rename/delete to the
// backend with season="movies", which /api/library/{rename,delete} cannot
// parse as an int and would 500). Series use the unchanged season/episode
// pipeline via libFillTitleBody.
function libRenderDetailBody(bodyEl, item, pfx) {
  var title = item.title;
  if (title.is_movie) {
    bodyEl.innerHTML = libRenderMovieFlat(title, 0, item.langFolder, null, item.cpId);
    bodyEl.classList.remove("lib-lazy-body");
    _libScheduleProgressFlush();
  } else {
    libFillTitleBody(bodyEl, { title: title, li: 0, langFolder: item.langFolder, lfi: null, ti: 0, cpId: item.cpId, pfx: pfx });
  }
}

function libOpenTitle(pfx) {
  var params = _libLazy[pfx + "Body"];
  if (!params) return;
  var key = _libTitleKey(params);
  if (_libOpenKey === key) { libCloseTitle(); return; }
  _libOpenKey = key;
  _libOpenSeasons = new Set();
  libRepaint();
}

function libCloseTitle() {
  _libOpenKey = null;
  _libOpenSeasons = new Set();
  libRepaint();
}

// Renders a movie as one or more flat, non-expandable file rows — used
// both for direct rendering (kept for API compatibility) and as the
// detail-body content of an opened movie card/row.
var _libMovieFlatIdx = 0;
function libRenderMovieFlat(title, li, langFolder, lfi, cpId) {
  var pfx     = "libMFlat" + (_libMovieFlatIdx++);
  var cpIdStr = cpId !== null && cpId !== undefined ? String(cpId) : '';
  var lfStr   = langFolder || '';
  var files   = (title.seasons && title.seasons["movies"]) ? title.seasons["movies"] : [];

  var h = [];
  files.forEach(function(ep) {
    var _rnd    = Math.random().toString(36).slice(2, 10);
    var rowId   = pfx + "_" + _rnd;
    var _upscaleKey = "usc_" + rowId;
    _libUpscaleTitles[_upscaleKey] = title;

    var menuKey = libRegMenuCtx({ type:'movie', folder:title.folder, cpId:cpId, lf:langFolder, pfx:rowId, upscaleKey:_upscaleKey, epPath:ep.path||'' });
    h.push('<div class="lib-episode-row lib-movie-flat-row lib-hoverable" id="' + rowId + '" data-path="' + libEscAttr(ep.path || '') + '" data-title="' + libEscAttr(title.folder) + '">');
    h.push('<div class="lib-row-left">');
    h.push('<svg class="lib-icon lib-icon-film" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="2"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="2" y1="7" x2="7" y2="7"/><line x1="17" y1="7" x2="22" y2="7"/><line x1="17" y1="17" x2="22" y2="17"/><line x1="2" y1="17" x2="7" y2="17"/></svg>');
    h.push('<div class="lib-info-col">');
    h.push('<div class="lib-info-main">');
    h.push('<span class="lib-ep-title" id="' + rowId + 'Name">' + libEsc(title.folder) + '</span>');
    h.push('</div>');
    h.push('<div class="lib-info-meta">');
    h.push('<span class="lib-movie-pill">Film</span>');
    h.push(libCodecBadge(ep.file, ep.video_codec) + libResolutionBadge(ep.file, ep.resolution));
    h.push('<span class="lib-badge lib-badge-size lib-meta-size">' + libFmtSize(ep.size) + '</span>');
    h.push('</div>');
    h.push('</div>');
    h.push('</div>');
    h.push('<div class="lib-row-right">');
    h.push('<span class="lib-badge lib-badge-size">' + libFmtSize(ep.size) + '</span>');
    // Play button for films
    if (ep.path) {
      // Path and title come from disk and can contain quotes, so they go into
      // data-* attributes (HTML-escaped) instead of being interpolated into a
      // JS string inside an onclick attribute.
      h.push('<button class="lib-action-btn lib-btn-play" data-libplay="' + libEscAttr(ep.path) + '" data-libplaytitle="' + libEscAttr(title.folder) + '" onclick="event.stopPropagation();libPlayFromButton(event,this)" title="Film abspielen"><svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg></button>');
    }
    h.push('<button class="lib-kebab-btn" data-libkey="' + menuKey + '" onclick="event.stopPropagation();libOpenMenu(this)" title="Mehr Optionen"><svg viewBox="0 0 6 24"><circle cx="3" cy="5" r="2"/><circle cx="3" cy="12" r="2"/><circle cx="3" cy="19" r="2"/></svg></button>');
    h.push('</div>');
    if (ep.path) {
      h.push('<div class="lib-ep-progress-wrap" id="' + rowId + '_prog"><div class="lib-ep-progress-fill" style="width:0%"></div></div>');
      _libPendingProgress.push({ rowId: rowId, path: ep.path });
    }
    h.push('</div>');
  });
  return h.join("");
}

// Called lazily from libRenderDetailBody to populate a series' detail body.
function libFillTitleBody(bodyEl, params) {
  var title = params.title, pfx = params.pfx;
  var li = params.li, langFolder = params.langFolder, lfi = params.lfi, ti = params.ti, cpId = params.cpId;
  var html = [];

  if (title.is_movie && title.seasons["movies"]) {
    // Movies: render files directly, no "Staffel X" wrapper
    title.seasons["movies"].forEach(function(ep) {
      html.push(libRenderEpisode(ep, title, "movies", cpId, langFolder));
    });
  } else {
    var seasonKeys = Object.keys(title.seasons).filter(function(k){ return k !== "movies"; })
      .sort(function(a,b){ return parseInt(a)-parseInt(b); });
    seasonKeys.forEach(function(skey) {
      html.push(libRenderSeason(title, skey, pfx, li, langFolder, lfi, ti, cpId));
    });
  }

  bodyEl.innerHTML = html.join("");
  bodyEl.classList.remove("lib-lazy-body");
}

function libRenderSeason(title, skey, titlePfx, li, langFolder, lfi, ti, cpId) {
  var eps      = title.seasons[skey];
  var bodyId   = titlePfx + "_s" + skey + "Body";
  var videoEps = eps.filter(function(e){ return e.is_video !== false; });
  var watchedEps = eps.filter(function(e){ return e.watched; }).length;
  var seasonSize = eps.reduce(function(acc,e){ return acc + e.size; }, 0);
  var cpIdStr = cpId !== null && cpId !== undefined ? String(cpId) : '';
  var lfStr   = langFolder || '';
  var menuKey = libRegMenuCtx({ type:'season', folder:title.folder, cpId:cpId, lf:langFolder, skey:skey });
  var h = [];
  h.push('<div class="lib-season-section">');
  h.push('<div class="lib-season-row lib-hoverable" onclick="libToggle(\'' + bodyId + '\',this)">');
  h.push('<div class="lib-row-left">');
  h.push('<svg class="lib-arrow" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>');
  h.push('<svg class="lib-icon lib-icon-season" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>');
  h.push('<div style="display:flex;flex-direction:column;gap:2px;min-width:0">');
  h.push('<span style="font-weight:600;font-size:0.88rem">' + t('Staffel', 'Season') + ' ' + skey + '</span>');
  h.push('<span class="lib-season-sub">' + watchedEps + '/' + videoEps.length + ' ' + t('gesehen', 'watched') + '</span>');
  h.push('</div>');
  h.push('</div>');
  h.push('<div class="lib-row-right">');
  h.push('<span class="lib-badge">' + videoEps.length + ' Ep.</span>');
  h.push('<span class="lib-badge lib-badge-size">' + libFmtSize(seasonSize) + '</span>');
  if (libraryCanDelete) {
    h.push('<button class="lib-kebab-btn" data-libkey="' + menuKey + '" onclick="event.stopPropagation();libOpenMenu(this)" title='+t('Mehr Optionen', "More Options")+'><svg viewBox="0 0 6 24"><circle cx="3" cy="5" r="2"/><circle cx="3" cy="12" r="2"/><circle cx="3" cy="19" r="2"/></svg></button>');
  }
  h.push('</div>');
  h.push('</div>'); // season-row

  h.push('<div class="lib-body" id="' + bodyId + '">');
  eps.forEach(function(ep) {
    h.push(libRenderEpisode(ep, title, skey, cpId, langFolder));
  });
  h.push('</div>');
  h.push('</div>');
  return h.join("");
}

function libCodecBadge(filename, cachedCodec) {
  var f = (filename || "").toLowerCase();
  var badges = [];
  var ext = f.split('.').pop();
  var extMap = { mkv:'MKV', mp4:'MP4', avi:'AVI', mov:'MOV', ts:'TS', wmv:'WMV', flv:'FLV', webm:'WEBM', m4v:'M4V' };
  if (extMap[ext]) {
    var cls = (ext === 'mkv') ? 'lib-codec-mkv' : '';
    badges.push('<span class="lib-codec-badge ' + cls + '">' + extMap[ext] + '</span>');
  }
  var codec = cachedCodec;
  if (!codec) {
    if (f.includes('hevc') || f.includes('x265') || f.includes('h.265')) codec = 'HEVC';
    else if (f.includes('h264') || f.includes('x264') || f.includes('h.264') || f.includes('avc')) codec = 'H.264';
    else if (f.includes('av1')) codec = 'AV1';
  }
  if (codec) {
    var ckey = codec.toUpperCase();
    var cls = '';
    if (ckey.includes('HEVC') || ckey.includes('265')) cls = 'lib-codec-hevc';
    else if (ckey.includes('264') || ckey.includes('AVC')) cls = 'lib-codec-h264';
    else if (ckey.includes('AV1')) cls = 'lib-codec-av1';
    badges.push('<span class="lib-codec-badge ' + cls + '">' + codec + '</span>');
  }
  return badges.join('');
}

function libGetResolution(filename) {
  var f = (filename || "").toLowerCase();
  if (f.includes("4k") || f.includes("2160p") || f.includes("3840x2160")) return "4K";
  if (f.includes("2k") || f.includes("1440p") || f.includes("2560x1440")) return "2K";
  if (f.includes("1080p") || f.includes("1080i") || f.includes("1920x1080")) return "1080p";
  if (f.includes("720p") || f.includes("1280x720")) return "720p";
  if (f.includes("480p") || f.includes("854x480") || f.includes("640x480")) return "480p";
  if (f.includes("360p") || f.includes("640x360")) return "360p";

  var m = f.match(/\b(2160|1440|1080|720|480|360|240)p?\b/);
  if (m) {
    var val = m[1];
    if (val === "2160") return "4K";
    if (val === "1440") return "2K";
    return val + "p";
  }
  return null;
}

function libResolutionBadge(filename, resolution) {
  var res = resolution || libGetResolution(filename);
  if (!res) return '';
  var cls = '';
  if (res === '4K' || res === '2K') cls = 'lib-res-4k';
  else if (res === '1080p') cls = 'lib-res-1080';
  else if (res === '720p') cls = 'lib-res-720';
  return '<span class="lib-codec-badge ' + cls + '">' + res + '</span>';
}

function libRenderEpisode(ep, title, skey, cpId, langFolder) {
  var cpIdStr = cpId !== null && cpId !== undefined ? String(cpId) : '';
  var lfStr   = langFolder || '';
  var isVideo = ep.is_video !== false;
  var _rnd    = Math.random().toString(36).slice(2, 10);
  var rowId   = 'libEpRow_' + _rnd;
  var menuKey = libRegMenuCtx({ type:'ep', folder:title.folder, cpId:cpId, lf:langFolder,
    skey:skey, epNum:ep.episode, epFile:ep.file||'', epPath:ep.path||'' });
  var h = [];
  var _titleVal = (title.folder || "") + (ep.is_movie_file ? "" : (" E" + String(ep.episode).padStart(2,"0"))) + " – " + (ep.file || "");
  h.push('<div class="lib-episode-row lib-hoverable" id="' + rowId + '" data-path="' + libEscAttr(ep.path || '') + '" data-title="' + libEscAttr(_titleVal) + '">');
  h.push('<div class="lib-row-left">');
  if (isVideo) {
    h.push('<svg class="lib-icon lib-icon-video" viewBox="0 0 24 24"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>');
  } else {
    h.push('<svg class="lib-icon lib-icon-file" viewBox="0 0 24 24"><path d="M13 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V9z"/><polyline points="13 2 13 9 20 9"/></svg>');
  }
  h.push('<div class="lib-info-col">');
  h.push('<div class="lib-info-main">');
  if (!ep.is_movie_file) h.push('<span class="lib-ep-num">E' + String(ep.episode).padStart(2,"0") + '</span>');
  h.push('<span class="lib-ep-file" title="' + libEscAttr(ep.file) + '">' + libEscAttr(ep.file) + '</span>');
  h.push('</div>');
  h.push('<div class="lib-info-meta">');
  h.push(libCodecBadge(ep.file, ep.video_codec) + libResolutionBadge(ep.file, ep.resolution));
  h.push('<span class="lib-badge lib-badge-size lib-meta-size">' + libFmtSize(ep.size) + '</span>');
  h.push('</div>');
  h.push('</div>');
  h.push('</div>');
  h.push('<div class="lib-row-right">');
  h.push('<span class="lib-badge lib-badge-size">' + libFmtSize(ep.size) + '</span>');
  // Play button (always visible, not in kebab)
  if (isVideo && ep.path) {
    // See the film row above: data-* attributes, not an onclick string.
    h.push('<button class="lib-action-btn lib-btn-play" data-libplay="' + libEscAttr(ep.path) + '" data-libplaytitle="' + libEscAttr(_titleVal) + '" onclick="event.stopPropagation();libPlayFromButton(event,this)" title='+t("Abspielen","Play")+'><svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg></button>');
  }
  // Kebab for rename/upscale/delete
  h.push('<button class="lib-kebab-btn" data-libkey="' + menuKey + '" onclick="event.stopPropagation();libOpenMenu(this)" title='+t("Mehr Optionen", "More Options")+'><svg viewBox="0 0 6 24"><circle cx="3" cy="5" r="2"/><circle cx="3" cy="12" r="2"/><circle cx="3" cy="19" r="2"/></svg></button>');
  h.push('</div>');
  if (isVideo && ep.path) {
    h.push('<div class="lib-ep-progress-wrap" id="' + rowId + '_prog"><div class="lib-ep-progress-fill" style="width:0%"></div></div>');
    _libPendingProgress.push({ rowId: rowId, path: ep.path });
  }
  h.push('</div>');
  return h.join("");
}

// ================================================================
// ---- Kebab Menu System ----
// ================================================================

// Context registry (avoids null-byte attribute encoding issues)
var _libMenuContexts = {};
var _libMenuCtxIdx   = 0;
function libRegMenuCtx(data) {
  var key = 'lmc' + (_libMenuCtxIdx++);
  _libMenuContexts[key] = data;
  return key;
}

// Attribute-safe escaping. Same implementation as libEsc now -- the shared
// escaper (static/mf_escape.js) is safe in both text and attribute context,
// so the two names only survive to keep the call sites readable.
function libEscAttr(s) {
  return window.mfEscape(s);
}

var _libMenuEl     = null;
var _libMenuAnchor = null;

function _libBuildMenu() {
  _libMenuEl = document.createElement('div');
  _libMenuEl.className = 'lib-menu';
  document.body.appendChild(_libMenuEl);
  document.addEventListener('click', function(e) {
    if (!_libMenuEl || !_libMenuEl.classList.contains('lib-menu-show')) return;
    if (!e.target.closest('.lib-menu') && !e.target.closest('.lib-kebab-btn')) libCloseMenu();
  });
  window.addEventListener('scroll', libCloseMenu, true);
  window.addEventListener('resize',  libCloseMenu);
  document.addEventListener('keydown', function(e) { if (e.key === 'Escape') libCloseMenu(); });
}

function libCloseMenu() {
  if (_libMenuEl) _libMenuEl.classList.remove('lib-menu-show');
  _libMenuAnchor = null;
}

function libOpenMenu(btn) {
  if (!_libMenuEl) _libBuildMenu();
  if (_libMenuAnchor === btn && _libMenuEl.classList.contains('lib-menu-show')) { libCloseMenu(); return; }
  _libMenuAnchor = btn;

  // Read context from registry — avoids HTML attribute encoding issues
  var key = btn.getAttribute('data-libkey') || '';
  var ctx = _libMenuContexts[key];
  if (!ctx) { console.warn('[lib] No menu context for key:', key); return; }

  var type   = ctx.type   || '';
  var folder = ctx.folder || '';
  var cpId   = (ctx.cpId !== null && ctx.cpId !== undefined) ? ctx.cpId : null;
  var lf     = ctx.lf    || null;

  var ICO_RENAME  = '<svg viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>';
  var ICO_MOVE    = '<svg viewBox="0 0 24 24"><polyline points="5 9 2 12 5 15"/><polyline points="9 5 12 2 15 5"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="12" y1="2" x2="12" y2="22"/></svg>';
  var ICO_UPSCALE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>';
  var ICO_TRASH   = '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 011-1h4a1 1 0 011 1v2"/></svg>';
  var ICO_INFO    = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>';
  var ICO_SYNC    = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/></svg>';

  var items = [];

  if (type === 'title' || type === 'movie') {
    var pfx = ctx.pfx || '', upscaleKey = ctx.upscaleKey || '';
    var isMovieTitle = (type === 'movie') || !!ctx.isMovie;
    if (libraryCanDelete) {
      items.push({ label:t('Umbenennen', 'Rename'),  icon:ICO_RENAME,  fn:function(){ libStartRename(pfx, folder, cpId, lf); } });
      if (libAllTargets.length > 1)
        items.push({ label:t('Verschieben', 'Move'), icon:ICO_MOVE, fn:function(){ libOpenMove(folder, cpId, lf); } });
    }
    // Auto-Sync only applies to series (new episodes), not movies.
    if (!isMovieTitle) {
      items.push({ label:t('Zu Auto-Sync hinzufügen', 'Add to Auto-Sync'), icon:ICO_SYNC, fn:function(){ libAddToAutosync(folder); } });
    }
    items.push({ label:t('Upscalen', 'Upscale'), icon:ICO_UPSCALE, fn:function(){ libUpscaleTitle(null, upscaleKey, cpId, lf); } });
    if (type === 'movie' && ctx.epPath) {
      items.push({ label:t('Details', 'Details'), icon:ICO_INFO, fn:function(){ libOpenMediaInfo(ctx.epPath, folder); } });
    }
    if (libraryCanDelete) {
      items.push({ sep:true });
      items.push({ label:(type==='movie' ? t('Film löschen','Delete movie') : t('Titel löschen','Delete title')), icon:ICO_TRASH, danger:true,
        fn:function(){ libDeleteTitle(folder, cpId, lf); } });
    }

  } else if (type === 'season') {
    var sk = ctx.skey;
    if (libraryCanDelete)
      items.push({ label:t('Staffel löschen', 'Delete season'), icon:ICO_TRASH, danger:true,
        fn:function(){ libDeleteSeason(folder, parseInt(sk,10), cpId, lf); } });

  } else if (type === 'ep') {
    var esk = ctx.skey, eNum = ctx.epNum, eFile = ctx.epFile || '', ePath = ctx.epPath || '';
    if (libraryCanDelete)
      items.push({ label:t('Umbenennen', 'Rename'), icon:ICO_RENAME,
        fn:function(){ libStartEpRename(folder, esk, eNum, eFile, cpId, lf); } });
    if (ePath)
      items.push({ label:t('Folge upscalen', 'Upscale episode'), icon:ICO_UPSCALE,
        fn:function(){ libUpscaleEpisode(null, ePath, folder+' – '+eFile); } });
    if (ePath) {
      var displayTitle = folder + ' – E' + String(eNum).padStart(2,'0') + ' – ' + eFile;
      items.push({ label:t('Details', 'Details'), icon:ICO_INFO, fn:function(){ libOpenMediaInfo(ePath, displayTitle); } });
    }
    if (libraryCanDelete) {
      items.push({ sep:true });
      items.push({ label:t('Episode löschen', 'Delete episode'), icon:ICO_TRASH, danger:true,
        fn:function(){ libDeleteEpisode(folder, esk, eNum, cpId, lf); } });
    }
  }

  if (!items.length) return;

  var html = [], actionItems = [];
  items.forEach(function(it) {
    if (it.sep) { html.push('<div class="lib-menu-sep"></div>'); return; }
    actionItems.push(it);
    html.push('<button class="' + (it.danger ? 'lib-menu-danger' : '') + '">' +
      it.icon + '<span>' + libEsc(it.label) + '</span></button>');
  });
  _libMenuEl.innerHTML = html.join('');
  _libMenuEl.querySelectorAll('button').forEach(function(b, i) {
    b.addEventListener('click', function(e) {
      e.stopPropagation(); libCloseMenu();
      if (actionItems[i] && actionItems[i].fn) actionItems[i].fn();
    });
  });

  _libMenuEl.style.visibility = 'hidden';
  _libMenuEl.classList.add('lib-menu-show');
  var r  = btn.getBoundingClientRect();
  var mw = _libMenuEl.offsetWidth, mh = _libMenuEl.offsetHeight;
  var left = Math.max(8, Math.min(r.right - mw, window.innerWidth - mw - 8));
  var top  = r.bottom + 6;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 6);
  _libMenuEl.style.left = left + 'px';
  _libMenuEl.style.top  = top  + 'px';
  _libMenuEl.style.visibility = '';
}

// ---- Toggle (season expand/collapse within an open detail) ----

function libToggle(bodyId, headerEl) {
  var body = document.getElementById(bodyId);
  if (!body) return;

  var expanded = body.classList.toggle("lib-expanded");
  if (headerEl) {
    var arrow = headerEl.querySelector(".lib-arrow");
    if (arrow) arrow.classList.toggle("lib-arrow-open", expanded);
  }

  // Season body: "..._sNBody" suffix
  var m = bodyId.match(/_s(\d+)Body$/);
  if (m) {
    var sKey = "s" + m[1];
    if (expanded) _libOpenSeasons.add(sKey);
    else _libOpenSeasons.delete(sKey);
  }
}

// ---- Delete ----

async function libDeleteTitle(folder, cpId, langFolder) {
  if (!await showConfirm('<b class="lib-delete-title">' + libEsc(folder) + '</b>' + t(' vollständig <b>löschen</b>? Dieser Vorgang kann nicht rückgängig gemacht werden.', ' completely <b>delete</b>? This action cannot be undone.'))) return;
  var body = { folder: folder, season: null, episode: null, custom_path_id: cpId };
  if (langFolder) body.lang_folder = langFolder;
  await libApiPost("/api/library/delete", body, t("Erfolgreich gelöscht", "Successfully deleted"));
}

async function libDeleteSeason(folder, season, cpId, langFolder) {
  if (!await showConfirm(t('Alle Episoden von Staffel ', 'Delete all episodes of season ') + season + ' in "<b class="lib-delete-title">' + libEsc(folder) + '</b>"?')) return;
  var body = { folder: folder, season: season, episode: null, custom_path_id: cpId };
  if (langFolder) body.lang_folder = langFolder;
  await libApiPost("/api/library/delete", body, t("Staffel gelöscht", "Season deleted"));
}

async function libDeleteEpisode(folder, season, episode, cpId, langFolder) {
  if (!await showConfirm(t('Episode ', 'Episode ') + 'E' + String(episode).padStart(3,"0") + ' in "<b class="lib-delete-title">' + libEsc(folder) + '</b>"?')) return;
  var body = { folder: folder, season: season, episode: episode, custom_path_id: cpId };
  if (langFolder) body.lang_folder = langFolder;
  await libApiPost("/api/library/delete", body, t("Episode gelöscht", "Episode deleted"));
}

// ---- Rename ----

function libStartRename(pfx, currentName, cpId, langFolder) {
  var nameEl = document.getElementById(pfx + "Name");
  if (!nameEl) return;
  var section = document.getElementById(pfx);
  if (!section) return;

  // Build inline input
  var input = document.createElement("input");
  input.type = "text";
  input.value = currentName;
  input.className = "lib-inline-input";
  input.onclick = function(e) { e.stopPropagation(); };

  var confirmBtn = document.createElement("button");
  confirmBtn.className = "lib-inline-btn lib-inline-confirm";
  confirmBtn.innerHTML = '&#10003;';
  confirmBtn.title = t("Bestätigen", "Confirm");

  var cancelBtn = document.createElement("button");
  cancelBtn.className = "lib-inline-btn lib-inline-cancel";
  cancelBtn.innerHTML = '&#10005;';
  cancelBtn.title = t("Abbrechen", "Cancel");

  var origContent = nameEl.innerHTML;
  nameEl.innerHTML = "";
  nameEl.appendChild(input);
  nameEl.appendChild(confirmBtn);
  nameEl.appendChild(cancelBtn);
  input.select();

  var doRename = async function() {
    var newName = input.value.trim();
    if (!newName || newName === currentName) { restoreLib(); return; }
    try {
      var resp = await fetch("/api/library/rename", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder: currentName, new_name: newName, custom_path_id: cpId, lang_folder: langFolder })
      });
      var data = await resp.json();
      if (data.error) { showToast(data.error); restoreLib(); }
      else { showToast(t("Umbenannt", "Renamed")); libLoad(false); }
    } catch(e) { showToast(t("Umbenennen fehlgeschlagen", "Rename failed")); restoreLib(); }
  };

  var restoreLib = function() { nameEl.innerHTML = origContent; };

  confirmBtn.onclick = function(e) { e.stopPropagation(); doRename(); };
  cancelBtn.onclick  = function(e) { e.stopPropagation(); restoreLib(); };
  input.addEventListener("keydown", function(e) {
    if (e.key === "Enter")  doRename();
    if (e.key === "Escape") restoreLib();
  });
}

function libStartEpRename(folder, season, episode, oldFile, cpId, langFolder) {
  // Find the episode row and inject rename UI inline
  var rows = document.querySelectorAll(".lib-ep-file");
  var targetRow = null;
  rows.forEach(function(el) {
    if (el.getAttribute("title") === oldFile) targetRow = el;
  });
  if (!targetRow) return;

  var input = document.createElement("input");
  input.type = "text";
  input.value = oldFile;
  input.className = "lib-inline-input";
  input.onclick = function(e) { e.stopPropagation(); };

  var confirmBtn = document.createElement("button");
  confirmBtn.className = "lib-inline-btn lib-inline-confirm";
  confirmBtn.innerHTML = '&#10003;';

  var cancelBtn = document.createElement("button");
  cancelBtn.className = "lib-inline-btn lib-inline-cancel";
  cancelBtn.innerHTML = '&#10005;';

  var origTitle = targetRow.getAttribute("title");
  var origText  = targetRow.textContent;
  targetRow.textContent = "";
  targetRow.appendChild(input);
  targetRow.appendChild(confirmBtn);
  targetRow.appendChild(cancelBtn);
  input.select();

  var doRename = async function() {
    var newName = input.value.trim();
    if (!newName || newName === oldFile) { restore(); return; }
    try {
      var resp = await fetch("/api/library/rename", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder: folder, season: season, episode: episode, old_file: oldFile, new_name: newName, custom_path_id: cpId, lang_folder: langFolder })
      });
      var data = await resp.json();
      if (data.error) { showToast(data.error); restore(); }
      else { showToast(t("Umbenannt", "Renamed")); libLoad(false); }
    } catch(e) { showToast(t("Umbenennen fehlgeschlagen", "Rename failed")); restore(); }
  };

  var restore = function() {
    targetRow.textContent = origText;
    targetRow.setAttribute("title", origTitle);
  };

  confirmBtn.onclick = function(e) { e.stopPropagation(); doRename(); };
  cancelBtn.onclick  = function(e) { e.stopPropagation(); restore(); };
  input.addEventListener("keydown", function(e) {
    if (e.key === "Enter")  doRename();
    if (e.key === "Escape") restore();
  });
}

// ---- Move ----

// ── Move job state ──────────────────────────────────────────────
var _libMoveJobId    = null;
var _libMovePollTimer = null;

function libOpenMove(folder, cpId, langFolder) {
  // Don't open select view while a move is already running
  if (_libMoveJobId) { libOpenMoveProgress(); return; }

  var modal   = document.getElementById("libMoveModal");
  var titleEl = document.getElementById("libMoveTitleName");
  var select  = document.getElementById("libMoveTarget");
  if (!modal || !titleEl || !select) return;

  titleEl.textContent = folder;
  modal._folder     = folder;
  modal._cpId       = cpId;
  modal._langFolder = langFolder;

  // Show select view, hide progress view
  var sv = document.getElementById("libMoveSelectView");
  var pv = document.getElementById("libMoveProgressView");
  if (sv) sv.style.display = "";
  if (pv) pv.style.display = "none";

  // Populate target dropdown (exclude current location)
  select.innerHTML = "";
  libAllTargets.forEach(function(t) {
    var isCurrent = (cpId === null || cpId === undefined)
      ? (t.custom_path_id === null || t.custom_path_id === undefined)
      : (String(t.custom_path_id) === String(cpId));
    if (isCurrent) return;
    var opt = document.createElement("option");
    opt.value = t.custom_path_id !== null && t.custom_path_id !== undefined ? t.custom_path_id : "";
    opt.textContent = t.label;
    select.appendChild(opt);
  });

  modal.style.display = "block";
}

function libCloseMoveModal() {
  var modal = document.getElementById("libMoveModal");
  if (modal) modal.style.display = "none";
}

// Called when clicking the background overlay — only close if no active job
function libMoveModalBgClick() {
  if (_libMoveJobId) { libMinimizeMove(); return; }
  libCloseMoveModal();
}

// Minimize to pill — keep job running in background
function libMinimizeMove() {
  libCloseMoveModal();
}

// Reopen progress modal from pill
function libOpenMoveProgress() {
  if (!_libMoveJobId) return;
  var modal = document.getElementById("libMoveModal");
  var sv    = document.getElementById("libMoveSelectView");
  var pv    = document.getElementById("libMoveProgressView");
  if (!modal) return;
  if (sv) sv.style.display = "none";
  if (pv) pv.style.display = "";
  modal.style.display = "block";
}

function _libShowMovePill(pct) {
  var pill = document.getElementById("libMovePill");
  var pctEl = document.getElementById("libMovePillPct");
  if (pill) pill.style.display = "";
  if (pctEl) pctEl.textContent = pct + "%";
}

function _libHideMovePill() {
  var pill = document.getElementById("libMovePill");
  if (pill) pill.style.display = "none";
}

function _libMoveSetProgress(pct, file) {
  var fill  = document.getElementById("libMoveProgressBarFill");
  var pctEl = document.getElementById("libMoveProgressPct");
  var fileEl = document.getElementById("libMoveProgressFile");
  if (fill)  fill.style.width  = pct + "%";
  if (pctEl) pctEl.textContent = pct + "%";
  if (fileEl) fileEl.textContent = file || "";
  _libShowMovePill(pct);
}

function _libMoveFinish(folder) {
  _libMoveJobId = null;
  clearInterval(_libMovePollTimer);
  _libMovePollTimer = null;
  _libHideMovePill();
  libCloseMoveModal();
  if (window.showToast) showToast('"' + folder + t("wurde verschoben", "was moved") + '"');
  libLoad(false);
}

function _libMoveError(msg) {
  _libMoveJobId = null;
  clearInterval(_libMovePollTimer);
  _libMovePollTimer = null;
  _libHideMovePill();

  // Show error in modal
  var pv  = document.getElementById("libMoveProgressView");
  var err = document.getElementById("libMoveProgressError");
  var act = document.getElementById("libMoveProgressActions");
  var modal = document.getElementById("libMoveModal");
  if (modal) modal.style.display = "block";
  if (pv) pv.style.display = "";
  var sv = document.getElementById("libMoveSelectView");
  if (sv) sv.style.display = "none";
  if (err) { err.style.display = ""; err.textContent = "Fehler: " + msg; }
  if (act) act.innerHTML = '<button class="btn btn-secondary btn-sm" onclick="libCloseMoveModal()">Schließen</button>';
}

async function libConfirmMove() {
  var modal  = document.getElementById("libMoveModal");
  var select = document.getElementById("libMoveTarget");
  if (!modal || !select) return;

  var folder     = modal._folder;
  var fromCpId   = modal._cpId    !== undefined ? modal._cpId    : null;
  var langFolder = modal._langFolder !== undefined ? modal._langFolder : null;
  var toVal      = select.value;
  var toCpId     = toVal === "" ? null : parseInt(toVal, 10);

  // Switch to progress view
  var sv = document.getElementById("libMoveSelectView");
  var pv = document.getElementById("libMoveProgressView");
  var pt = document.getElementById("libMoveProgressTitle");
  var err = document.getElementById("libMoveProgressError");
  var act = document.getElementById("libMoveProgressActions");
  if (sv)  sv.style.display  = "none";
  if (pv)  pv.style.display  = "";
  if (pt)  pt.textContent    = folder;
  if (err) err.style.display = "none";
  if (act) act.innerHTML     = '<button class="btn btn-secondary btn-sm" id="libMoveMinimizeBtn" onclick="libMinimizeMove()">Im Hintergrund</button>';
  _libMoveSetProgress(0, t("Wird vorbereitet…", "Preparing…"));

  try {
    var resp = await fetch("/api/library/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        folder:               folder,
        from_custom_path_id:  fromCpId,
        to_custom_path_id:    isNaN(toCpId) ? null : toCpId,
        lang_folder:          langFolder || null
      })
    });
    var data = await resp.json();
    if (data.error) {
      _libMoveError(data.error);
      return;
    }
    _libMoveJobId = data.job_id;
    // Start polling
    _libMovePollTimer = setInterval(async function() {
      try {
        var r = await fetch("/api/library/move_status/" + _libMoveJobId);
        if (!r.ok) { _libMoveError(t("Server-Fehler " + r.status, "Server error " + r.status)); return; }
        var s = await r.json();
        if (s.error && s.status !== "done") { _libMoveError(s.error); return; }
        var pct = s.total_bytes > 0 ? Math.round(s.copied_bytes / s.total_bytes * 100) : 0;
        _libMoveSetProgress(pct, s.current_file || "");
        if (s.status === "done") { _libMoveFinish(folder); }
        else if (s.status === "error") { _libMoveError(s.error || t("Unbekannter Fehler", "Unknown error")); }
      } catch(e) { _libMoveError(t("Verbindung unterbrochen", "Connection interrupted")); }
    }, 400);
  } catch(e) {
    _libMoveError(t("Netzwerkfehler: ", "Network error: ") + e.message);
  }
}

// ---- Shared API helper ----

async function libApiPost(url, body, successMsg) {
  try {
    var resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    var data = await resp.json();
    if (data.error) showToast(data.error);
    else { showToast(successMsg); libLoad(false); }
  } catch(e) { showToast(t("Aktion fehlgeschlagen", "Action failed")); }
}

// ---- Utilities ----

function libFmtSize(bytes) {
  if (!bytes) return "—";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return Math.round(bytes / 1024) + " KB";
  if (bytes < 1073741824) return Math.round(bytes / 1048576) + " MB";
  var gb = bytes / 1073741824;
  var val = gb >= 10 ? Math.round(gb) : parseFloat(gb.toFixed(1));
  return String(val).replace('.', ',') + " GB";
}

// Both names now point at the same shared, quote-safe escaper
// (static/mf_escape.js). libEsc() used to escape & < > only, and libEscJs()
// escaped for a JS string but not for HTML -- a file name containing a double
// quote therefore closed the onclick attribute it was interpolated into and
// the rest was parsed as markup.
var libEsc = window.mfEscape;

// Click handlers for the buttons that carry their payload in data-*
// attributes rather than in an interpolated onclick string.
function libPlayFromButton(event, btn) {
  libPlayEpisode(event, btn.dataset.libplay || "", btn.dataset.libplaytitle || "");
}

function libCopyFromButton(btn) {
  libCopyToClipboard(btn.dataset.libcopy || "", btn);
}

// ---- Init ----

libLoad(false);

// ── Upscaling ────────────────────────────────────────────────────────
async function libUpscaleTitle(event, titleKey, cpId, langFolder) {
  if (event) event.stopPropagation();
  var title = _libUpscaleTitles[titleKey];
  if (!title) return;

  // Collect all episode file paths for this title.
  // title.seasons is an object: { "1": [{episode, file, size, is_video, path}, ...], ... }
  var files = [];
  function collectFiles(t) {
    if (!t || !t.seasons) return;
    Object.values(t.seasons).forEach(function(eps) {
      eps.forEach(function(ep) {
        if (ep.path && ep.is_video !== false) {
          files.push({title: (t.folder || "") + " – " + (ep.file || ""), path: ep.path});
        }
      });
    });
  }
  collectFiles(title);

  if (!files.length) {
    if (typeof showToast === "function") showToast(t("Keine Dateipfade gefunden.", "No file paths found."));
    return;
  }

  try {
    var r = await fetch("/api/upscale/add-library", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({files: files}),
    });
    var d = await r.json();
    if (d.ok) {
      if (typeof showToast === "function") showToast(t("✓ " + d.added + " Datei(en) zur Upscaling-Queue hinzugefügt", "✓ " + d.added + " file(s) added to upscaling queue"));
      // Update badge
      if (typeof _startUpscaleBadgePoll === "function") {
        fetch("/api/upscale/badge").then(function(br) { return br.json(); }).then(function(bd) {
          if (bd.ok && typeof _updateUpscaleBadges === "function") _updateUpscaleBadges(bd.count || 0);
        }).catch(function(){});
      }
    } else {
      if (typeof showToast === "function") showToast(t("Fehler: " + (d.error || "unbekannt"), "Error: " + (d.error || "unknown")));
    }
  } catch(e) {
    if (typeof showToast === "function") showToast(t("Netzwerkfehler", "Network error"));
  }
}

async function libUpscaleEpisode(event, filePath, fileTitle) {
  if (event) event.stopPropagation();
  if (!filePath) { if (typeof showToast === "function") showToast(t("Kein Dateipfad gefunden.", "No file paths found.")); return; }
  try {
    var r = await fetch("/api/upscale/add-library", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({files: [{title: fileTitle || filePath, path: filePath}]}),
    });
    var d = await r.json();
    if (d.ok) {
      if (typeof showToast === "function") showToast(t("✓ Folge zur Upscaling-Queue hinzugefügt", "✓ Episode added to upscaling queue"));
      if (typeof _startUpscaleBadgePoll === "function") {
        fetch("/api/upscale/badge").then(function(br) { return br.json(); }).then(function(bd) {
          if (bd.ok && typeof _updateUpscaleBadges === "function") _updateUpscaleBadges(bd.count || 0);
        }).catch(function(){});
      }
    } else {
      if (typeof showToast === "function") showToast(t("Fehler: " + (d.error || "unbekannt"), "Error: " + (d.error || "unknown")));
    }
  } catch(e) {
    if (typeof showToast === "function") showToast(t("Netzwerkfehler", "Network error"));
  }
}


// ── Watch Progress ───────────────────────────────────────────────────────
// Batch-load progress for all episodes rendered in the current view.

var _libPendingProgress = [];  // populated by libRenderEpisode / libRenderMovieFlat / card prefetch
var _libProgressCache   = {};  // path → {percent, watched, position}
var _libProgressFlush   = null;

// After every render, schedule a flush (debounced 120 ms)
var _origLibFillTitleBody = libFillTitleBody;
libFillTitleBody = function(bodyEl, params) {
  _origLibFillTitleBody(bodyEl, params);
  _libScheduleProgressFlush();
};

function _libScheduleProgressFlush() {
  if (_libProgressFlush) clearTimeout(_libProgressFlush);
  _libProgressFlush = setTimeout(_libFlushProgress, 120);
}

async function _libFlushProgress() {
  _libProgressFlush = null;
  var batch = _libPendingProgress.splice(0);
  if (!batch.length) return;

  var paths = batch.map(function(b){ return b.path; });
  try {
    var resp = await fetch("/api/progress/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: paths })
    });
    var data = await resp.json();
    // Cache results
    Object.assign(_libProgressCache, data);
    // Apply to DOM — episode/movie-file rows use the row renderer, card-level
    // (eager movie) prefetches use the poster-card renderer.
    batch.forEach(function(b) {
      var prog = data[b.path];
      if (!prog) return;
      if (b.kind === 'card') _libApplyProgressToCard(b.rowId, prog);
      else _libApplyProgressToRow(b.rowId, prog);
    });
  } catch(e) { /* ignore */ }
}

function _libApplyProgressToRow(rowId, prog) {
  var row     = document.getElementById(rowId);
  if (!row) return;
  var wrap    = document.getElementById(rowId + '_prog');
  if (!wrap) return;
  var fill    = wrap.querySelector('.lib-ep-progress-fill');
  var pct     = prog.percent || 0;
  var path    = row.getAttribute('data-path') || '';
  var title   = row.getAttribute('data-title') || '';

  if (prog.watched) {
    // Fully watched: green bar + checkmark
    if (fill) { fill.style.width = '100%'; fill.classList.add('lib-watched'); }
    // Add checkmark before ep-num or title if not already there
    var epNum = row.querySelector('.lib-ep-num');
    if (epNum && !row.querySelector('.lib-watched-dot')) {
      var dot = document.createElement('span');
      dot.className = 'lib-watched-dot';
      dot.innerHTML = '<svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>';
      dot.title = t('Gesehen', 'Watched');
      epNum.parentNode.insertBefore(dot, epNum);
    } else if (!epNum && !row.querySelector('.lib-watched-dot')) {
      var titleEl = row.querySelector('.lib-ep-title') || row.querySelector('.lib-ep-file');
      if (titleEl) {
        var dot = document.createElement('span');
        dot.className = 'lib-watched-dot';
        dot.style.marginRight = '6px';
        dot.innerHTML = '<svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>';
        dot.title = t('Gesehen', 'Watched');
        titleEl.parentNode.insertBefore(dot, titleEl);
      }
    }
  } else if (pct > 3) {
    // In-progress
    if (fill) fill.style.width = pct + '%';
    // Add "Weiterschauen" pill next to ep-file or ep-title
    var insertTarget = row.querySelector('.lib-ep-file') || row.querySelector('.lib-ep-title');
    if (insertTarget && !row.querySelector('.lib-continue-pill')) {
      var pill = document.createElement('span');
      pill.className = 'lib-continue-pill';
      pill.innerHTML = '<svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>' + Math.round(pct) + '%';
      pill.title = t('Weiterschauen (' + Math.round(pct) + '%)', 'Continue watching (' + Math.round(pct) + '%)');
      pill.onclick = function(e) {
        e.stopPropagation();
        libPlayEpisode(e, path, title);
      };
      insertTarget.parentNode.insertBefore(pill, insertTarget.nextSibling);
    }
  }
}

// Card-level counterpart of _libApplyProgressToRow — applies a movie's
// eagerly-fetched progress to its (still collapsed) poster card / list row
// instead of an episode row. Handles both markups: the grid card has a
// .mf-poster-art host for the watched badge, the list row falls back to
// appending next to the title text.
function _libApplyProgressToCard(pfx, prog) {
  var el = document.getElementById(pfx);
  if (!el) return;
  var pct = prog.percent || 0;
  var bar = el.querySelector('.mf-poster-progress-fill');

  if (prog.watched) {
    el.classList.add('is-watched');
    if (bar) bar.style.width = '100%';
    if (!el.querySelector('.mf-poster-watched')) {
      var host = el.querySelector('.mf-poster-art') || document.getElementById(pfx + 'Name');
      if (host) {
        var dot = document.createElement('span');
        dot.className = 'mf-poster-watched';
        dot.innerHTML = '<svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>';
        dot.title = t('Gesehen', 'Watched');
        host.appendChild(dot);
      }
    }
  } else if (pct > 3) {
    if (bar) bar.style.width = pct + '%';
    el.classList.add('is-progress');
  }
}

// Store path/title on row element for pill click (set in libRenderEpisode via dataset)

// ── Play button ──────────────────────────────────────────────────────────

function libPlayEpisode(event, filePath, fileTitle) {
  if (event) event.stopPropagation();
  if (typeof openPlayer === 'function') {
    // Get cached progress
    var prog = _libProgressCache[filePath];
    var startPos = prog && prog.percent > 3 && !prog.watched ? prog.position : 0;
    openPlayer(filePath, fileTitle, startPos);
  } else {
    if (typeof showToast === 'function') showToast(t('Player wird geladen…', 'Player loading…'));
  }
}

/* ── Media Info Modal Controller ── */
function libCloseMediaInfoModal() {
  var modal = document.getElementById('libMediaInfoModal');
  if (modal) modal.style.display = 'none';
}

function libCopyToClipboard(text, btn) {
  navigator.clipboard.writeText(text).then(function() {
    if (btn) {
      var oldHtml = btn.innerHTML;
      btn.innerHTML = '<span style="font-size:0.75rem;font-weight:bold;color:var(--text-success)">Kopiert!</span>';
      setTimeout(function() {
        btn.innerHTML = oldHtml;
      }, 1500);
    }
  }).catch(function() {
    if (typeof showToast === 'function') showToast(t("Kopieren fehlgeschlagen", "Copy failed"));
  });
}

async function libOpenMediaInfo(path, title) {
  var modal = document.getElementById('libMediaInfoModal');
  var loading = document.getElementById('libMediaInfoLoading');
  var content = document.getElementById('libMediaInfoContent');
  if (!modal) return;

  modal.style.display = 'block';
  if (loading) loading.style.display = 'flex';
  if (content) content.innerHTML = '';

  try {
    var resp = await fetch("/api/library/media_info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: path })
    });
    var data = await resp.json();
    if (loading) loading.style.display = 'none';

    if (data.error) {
      if (content) content.innerHTML = '<div style="color:var(--text-danger);text-align:center;padding:20px;">Fehler: ' + libEsc(data.error) + '</div>';
      return;
    }

    // Build the grid and section details
    var h = [];
    h.push('<div class="lib-media-info-title">' + libEsc(title) + '</div>');

    // Basic Info Grid
    h.push('<div class="lib-media-info-grid">');

    h.push('<div class="lib-media-info-label">' + t('Dateiname', 'Filename') + ':</div>');
    h.push('<div class="lib-media-info-value">' + libEsc(data.filename) + '</div>');

    h.push('<div class="lib-media-info-label">' + t('Pfad', 'Path') + ':</div>');
    h.push('<div class="lib-media-info-value" style="display:flex;align-items:center;gap:6px;">' +
           '<span>' + libEsc(data.path) + '</span>' +
           '<button class="lib-copy-btn" data-libcopy="' + libEscAttr(data.path) + '" onclick="libCopyFromButton(this)" title="' + t('Pfad kopieren', 'Copy path') + '">' +
           '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>' +
           '</button>' +
           '</div>');

    h.push('<div class="lib-media-info-label">' + t('Größe', 'Size') + ':</div>');
    h.push('<div class="lib-media-info-value">' + libFmtSize(data.size_bytes) + ' (' + data.size_bytes.toLocaleString() + ' Bytes)</div>');

    h.push('<div class="lib-media-info-label">' + t('Container', 'Container') + ':</div>');
    h.push('<div class="lib-media-info-value">' + libEsc(data.container).toUpperCase() + '</div>');

    h.push('</div>');

    // Video / Audio details sections
    h.push('<div class="lib-media-info-sections">');

    // Video Section
    h.push('<div class="lib-media-info-section">');
    h.push('<div class="lib-media-info-sec-title">' +
           '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:middle;margin-right:4px;"><rect x="2" y="2" width="20" height="20" rx="2.18" ry="2.18"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="2" y1="7" x2="7" y2="7"/><line x1="2" y1="17" x2="7" y2="17"/><line x1="17" y1="17" x2="22" y2="17"/><line x1="17" y1="7" x2="22" y2="7"/></svg>' +
           t('Video Stream', 'Video Stream') +
           '</div>');
    if (data.video) {
      h.push('<div class="lib-media-info-sec-grid">');

      h.push('<div class="lib-media-info-sec-label">' + t('Codec', 'Codec') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.video.codec) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Profil', 'Profile') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.video.profile) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Level', 'Level') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.video.level) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Auflösung', 'Resolution') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.video.resolution) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Format', 'Format') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.video.aspect_ratio) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Framerate', 'Framerate') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + (data.video.framerate ? libEsc(data.video.framerate) + ' fps' : '—') + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Farbtiefe', 'Bit Depth') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.video.bit_depth) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Bereich', 'Range') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.video.video_range) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Pixelformat', 'Pixel Format') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.video.pixel_format) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Bitrate', 'Bitrate') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + (data.video.bitrate ? libEsc(data.video.bitrate) : '—') + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('AVC', 'AVC') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.video.avc) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Ref Frames', 'Ref Frames') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + (data.video.refs ? libEsc(data.video.refs) : '—') + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('NAL', 'NAL') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + (data.video.nal ? libEsc(data.video.nal) : '—') + '</div>');

      h.push('</div>');
    } else {
      h.push('<div style="color:var(--text-muted);font-size:0.85rem">' + t('Kein Video-Stream gefunden', 'No video stream found') + '</div>');
    }
    h.push('</div>');

    // Audio Section
    h.push('<div class="lib-media-info-section">');
    h.push('<div class="lib-media-info-sec-title">' +
           '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:middle;margin-right:4px;"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>' +
           t('Audio Stream', 'Audio Stream') +
           '</div>');
    if (data.audio) {
      h.push('<div class="lib-media-info-sec-grid">');

      h.push('<div class="lib-media-info-sec-label">' + t('Codec', 'Codec') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.audio.codec) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Profil', 'Profile') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.audio.profile) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Kanäle', 'Channels') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.audio.channels) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Layout', 'Layout') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + (data.audio.layout ? libEsc(data.audio.layout) : '—') + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Sprache', 'Language') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.audio.language).toUpperCase() + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Bitrate', 'Bitrate') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + (data.audio.bitrate ? libEsc(data.audio.bitrate) : '—') + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Sample Rate', 'Sample Rate') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.audio.sample_rate) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Default', 'Default') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.audio.default) + '</div>');

      h.push('<div class="lib-media-info-sec-label">' + t('Forced', 'Forced') + ':</div>');
      h.push('<div class="lib-media-info-sec-val">' + libEsc(data.audio.forced) + '</div>');

      h.push('</div>');
    } else {
      h.push('<div style="color:var(--text-muted);font-size:0.85rem">' + t('Kein Audio-Stream gefunden', 'No audio stream found') + '</div>');
    }
    h.push('</div>');

    h.push('</div>'); // end sections

    if (content) content.innerHTML = h.join('');
  } catch (e) {
    if (loading) loading.style.display = 'none';
    if (content) content.innerHTML = '<div style="color:var(--text-danger);text-align:center;padding:20px;">' + t('Netzwerkfehler: ', 'Network error: ') + libEsc(e.message) + '</div>';
  }
}


// ── Add to Auto-Sync ─────────────────────────────────────────────────────
// The library only knows folder names; resolve them to a real series URL on
// AniWorld / S.TO first (the "is it findable" check). If several sites match,
// let the user pick which one to sync from, then hand off to the shared
// Auto-Sync filter dialog which creates the job.

// Minimal toast fallback so feedback (and AutosyncFilter's own toasts) work
// on the library page even when no page-level showToast is defined.
if (typeof window.showToast !== "function") {
  window.showToast = function(msg) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = msg;
    el.classList.add("show");
    el.style.display = "block";
    clearTimeout(el._t);
    el._t = setTimeout(function(){ el.classList.remove("show"); el.style.display = "none"; }, 3000);
  };
}

async function libAddToAutosync(folder) {
  if (!window.AutosyncFilter) {
    showToast(t("Auto-Sync ist nicht verfügbar", "Auto-Sync is unavailable"));
    return;
  }
  showToast(t("Suche „" + folder + "“ auf den Seiten…", 'Searching "' + folder + '" on the sites…'));
  var results = [];
  try {
    var r = await fetch("/api/autosync/site-search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: folder }),
    });
    var d = await r.json();
    results = d.results || [];
  } catch (e) {
    showToast(t("Suche fehlgeschlagen", "Search failed"));
    return;
  }
  if (!results.length) {
    showToast(t("„" + folder + "“ wurde auf keiner Seite gefunden", '"' + folder + '" was not found on any site'));
    return;
  }
  if (results.length === 1) {
    _libOpenAutosyncCreate(results[0].url, results[0].title, results[0].poster_url);
  } else {
    _libShowAutosyncPicker(folder, results);
  }
}

async function _libOpenAutosyncCreate(url, title, coverUrl) {
  if (!window.AutosyncFilter) { showToast(t("Auto-Sync ist nicht verfügbar", "Auto-Sync is unavailable")); return; }
  var customPaths = [], langSep = false, langGroups = [];
  try {
    var res = await Promise.all([
      fetch("/api/custom-paths").then(function(x){ return x.json(); }),
      fetch("/api/settings").then(function(x){ return x.json(); }),
    ]);
    customPaths = (res[0] && res[0].paths) || [];
    langSep = res[1] && res[1].lang_separation === "1";
    langGroups = (res[1] && res[1].language_groups) || [];
  } catch (e) { /* fall back to defaults */ }
  window.AutosyncFilter.openCreate({
    seriesUrl: url,
    title: title,
    coverUrl: coverUrl,
    customPaths: customPaths,
    langSepEnabled: langSep,
    languageGroups: langGroups,
    onSaved: function(r) {
      if (r && r.created) showToast(t("Auto-Sync eingerichtet", "Auto-Sync set up"));
    },
  });
}

function _libShowAutosyncPicker(folder, results) {
  var existing = document.getElementById("libAutosyncPicker");
  if (existing) existing.remove();
  var ov = document.createElement("div");
  ov.id = "libAutosyncPicker";
  ov.className = "modal-overlay";
  // display:block triggers the global ".modal-overlay[style*='block']" rule
  // which centres the card via grid and lets it size to its content.
  ov.style.display = "block";
  ov.addEventListener("click", function(e) { if (e.target === ov) ov.remove(); });

  var rows = results.map(function(r, i) {
    var pct = Math.round((r.score || 0) * 100);
    return '<button class="lib-asp-row" data-idx="' + i + '">' +
      '<span class="lib-asp-site">' + libEsc(r.site_label) + '</span>' +
      '<span class="lib-asp-title">' + libEsc(r.title) + '</span>' +
      '<span class="lib-asp-score">' + pct + '%</span>' +
    '</button>';
  }).join("");

  ov.innerHTML =
    '<div class="card loaded" style="max-width:540px;width:92%;max-height:85vh;overflow-y:auto;margin:0 auto">' +
      '<div class="card-header">' +
        '<h2 class="card-title">' + t('Quelle wählen', 'Choose source') + '</h2>' +
        '<button class="modal-close" id="libAspClose"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg></button>' +
      '</div>' +
      '<p class="stat-sub" style="margin:0 0 12px">' + t('Mehrere Treffer für', 'Multiple matches for') + ' „' + libEsc(folder) + '“:</p>' +
      '<div class="lib-asp-list">' + rows + '</div>' +
    '</div>';

  document.body.appendChild(ov);
  ov.querySelector("#libAspClose").addEventListener("click", function() { ov.remove(); });
  ov.querySelectorAll(".lib-asp-row").forEach(function(b) {
    b.addEventListener("click", function() {
      var r = results[parseInt(b.getAttribute("data-idx"), 10)];
      ov.remove();
      _libOpenAutosyncCreate(r.url, r.title, r.poster_url);
    });
  });
}
