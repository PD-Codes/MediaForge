// ============================================================
// Library — shared core
// ============================================================
// MediaForge no longer has "a library page". It has one library PER MEDIA
// KIND (see web/media_kinds.py): /library/video, /library/books, and the
// placeholders behind them. This file holds everything those pages agree on
// -- fetching, scan/watcher status, search, sort, layout, pagination, the
// kebab-menu machinery and the small formatting helpers -- and nothing that
// knows what a title or a book actually looks like.
//
// The contract with a page module (library_video.js, library_books.js) is
// four globals it must define before this file's init runs:
//
//   LIB_KIND            "video" | "book" -- which library to ask the API for
//   LIB_SORT_KEYS       sort buttons this page offers, e.g. ["name","size"]
//   libRender(locations)   paint everything from the given locations
//   libUpdateSummary(locations)  fill the header pills
//
// Load order therefore matters: the page module first, this file second.
// That is the opposite of what looks natural, and it is why init lives at the
// bottom here rather than in each page.
//
// Why a split at all: this was one 2,555-line file that painted films and
// books from the same code paths, with the book half bolted onto the end and
// reached through filter-mode branches. Every change to
// one shelf risked the other, and the "coming soon" media types were fake
// filter buttons that had to short-circuit half the toolbar. Separate pages
// made all of that machinery unnecessary -- it is deleted, not moved.

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
var libViewMode      = "grid"; // "grid" | "list"  (restored below)

// Pagination — purely client-side: the whole filtered/sorted item list is
// already in memory (libFlattenTitles/libFilterTitles/libSortTitles), so
// paging is just a slice, no extra network round-trip. Mirrors the
// .mf-pagination usage pattern from history.js (numbered pager + a
// 10/20/50/100 "results per page" <select>, persisted in localStorage).
var LIB_PER_PAGE_OPTIONS = [10, 20, 50, 100];

// How the library is displayed is a per-ACCOUNT preference, not a per-browser
// one: window._USER_PREFS is rendered into <head> server-side (see base.html)
// and saved back through /api/user/preferences, so the layout a user picked on
// their desktop is the one they get on their phone. localStorage stays as the
// fallback for the logged-out / no-auth case and as the value used on the very
// first paint before anything is fetched.
function _libPref(key, lsKey, valid, fallback) {
  var prefs = window._USER_PREFS || {};
  if (valid(prefs[key])) return prefs[key];
  try {
    var saved = localStorage.getItem(lsKey);
    if (valid(saved)) return saved;
  } catch (e) { /* private mode */ }
  return fallback;
}

function _libSavePref(key, lsKey, value) {
  try { localStorage.setItem(lsKey, String(value)); } catch (e) { /* private mode */ }
  // Fire-and-forget, exactly like the appearance settings: the change has
  // already been applied locally, so a failed save (or a 401 with auth on and
  // the session expired) must not interrupt anything.
  if (typeof window.mfSaveUserPref === "function") {
    window.mfSaveUserPref(_libPrefPatch(key, String(value)));
  }
}
function _libPrefPatch(key, value) { var o = {}; o[key] = value; return o; }

function _libInitialPerPage() {
  var v = _libPref("library_per_page", "mf-lib-perpage",
                   function (x) { return LIB_PER_PAGE_OPTIONS.indexOf(parseInt(x, 10)) !== -1; },
                   "20");
  return parseInt(v, 10);
}
function _libInitialView() {
  return _libPref("library_view", "mf-lib-view",
                  function (x) { return x === "grid" || x === "list"; }, "grid");
}
var libPerPage = _libInitialPerPage();
var libPage    = 0; // 0-based
libViewMode = _libInitialView();

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
    var resp = await fetch("/api/library?kind=" + encodeURIComponent(LIB_KIND));
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
    libUpdateSummary(libLocations);

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
    var resp = await fetch("/api/library?kind=" + encodeURIComponent(LIB_KIND));
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
      libUpdateSummary(libLocations);
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
    if (tip) tip.title = t("watchdog nicht installiert (pip install watchdog)",
                           "watchdog not installed (pip install watchdog)");
    return;
  }
  if (watcher.active) {
    dot.className   = "lib-watcher-dot lib-watcher-on";
    label.textContent = t("Watcher aktiv", "Watcher active");
    if (tip && watcher.watched && watcher.watched.length) {
      tip.title = t("Überwacht: ", "Watching: ") + watcher.watched.map(function(w){ return w.path; }).join(", ");
    }
  } else {
    dot.className   = "lib-watcher-dot lib-watcher-starting";
    label.textContent = "Watcher startet…";
  }
}

// ---- Total size ----


// ---- Sort ----

function libSetSort(key) {
  if (libSortKey === key) {
    libSortAsc = !libSortAsc; // toggle direction on second click
  } else {
    libSortKey = key;
    libSortAsc = key === "name"; // name defaults A→Z, others default big→small
  }
  // Update button active state + direction arrows. Which buttons exist is the
  // page's decision (LIB_SORT_KEYS): sorting by episode count is meaningful on
  // the video shelf and meaningless on the book one, and the old page carried
  // the button on both and hid it again from script.
  LIB_SORT_KEYS.forEach(function(k) {
    var btn = document.getElementById("libSort-" + k);
    var dir = document.getElementById("libSortDir-" + k);
    if (!btn) return;
    btn.classList.toggle("active", k === libSortKey);
    if (dir) dir.textContent = (k === libSortKey) ? (libSortAsc ? "↑" : "↓") : "";
    if (k === libSortKey) {
      btn.title = (k === "name")
        ? (libSortAsc ? t("A–Z (klicken für Z–A)", "A–Z (click for Z–A)")
                      : t("Z–A (klicken für A–Z)", "Z–A (click for A–Z)"))
        : (libSortAsc ? t("Aufsteigend", "Ascending") : t("Absteigend", "Descending"));
    }
  });
  libPage = 0; // sort order changed — start back on page 1
  libRender(libLocations);
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

function _libSyncViewButtons() {
  var gBtn = document.getElementById("libViewGrid");
  var lBtn = document.getElementById("libViewList");
  var grid = libViewMode === "grid";
  if (gBtn) { gBtn.classList.toggle("active", grid); gBtn.setAttribute("aria-pressed", grid); }
  if (lBtn) { lBtn.classList.toggle("active", !grid); lBtn.setAttribute("aria-pressed", !grid); }
}

function libSetView(mode) {
  if (mode !== "grid" && mode !== "list") return;
  libViewMode = mode;
  _libSavePref("library_view", "mf-lib-view", mode);
  _libSyncViewButtons();
  libRender(libLocations);
}

// ---- Flatten ----
// Every title across every location/lang-folder becomes one flat item, so
// the whole library paints as a single poster grid / list regardless of
// how many volumes (custom paths) or language folders it spans.


function libRepaint() {
  libRender(libLocations);
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
  _libSavePref("library_per_page", "mf-lib-perpage", n);
  libPage = 0;
  libRepaint();
}

// ---- Shared card/row/detail markup helpers ----

// ---- Markup helpers shared by every shelf ----

function volTagHtml(cpLabel) {
  if (!cpLabel) return '';
  return '<span class="mf-vol-tag" title="' + libEscAttr(cpLabel) + '">' +
    '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg>' +
    '<span>' + libEsc(cpLabel) + '</span></span>';
}

// "Neu" flag: title was written to disk within the last N days.
// Backed by title.added_at (Unix seconds), populated server-side in
// _lib_scan_base() from the newest st_mtime among the title's files.

function _libFauxArt(name) {
  var hash = 0;
  for (var i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  var hue1 = hash % 360, hue2 = (hue1 + 48) % 360;
  var style = 'background:linear-gradient(155deg,hsl(' + hue1 + ',55%,22%),hsl(' + hue2 + ',55%,14%))';
  return '<div class="lib-fauxart" style="' + style + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg></div>';
}

// Primary/first file of a title — used for the card-level "Details" menu
// item (movies) and the eager card-level watch-progress prefetch.

// ---- Kebab menu system ----

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

  // What a menu offers depends entirely on what was clicked, and only the
  // page that painted the thing knows that. The core keeps the parts that are
  // the same everywhere: the context registry, the outside-click/Escape
  // handling and the viewport-aware positioning below.
  var items = (typeof window.libMenuItemsFor === "function")
    ? (window.libMenuItemsFor(ctx) || [])
    : [];

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


// ---- Init ----

// The template renders the grid button as the active one; move the highlight
// to the stored choice before the first fetch, so the toolbar and the layout
// agree even while the library is still loading. Only the buttons — calling
// libSetView() here would render an empty library and write the value it just
// read straight back.
_libSyncViewButtons();
libLoad(false);

