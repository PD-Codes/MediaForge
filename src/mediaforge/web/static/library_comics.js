// ============================================================
// Library — Comics
// ============================================================
// The comic shelf. Depends on library_core.js (see the contract in its
// header) and is loaded BEFORE it, because the core's init runs on parse.
//
// Unit of display is the SERIES, not the issue: a run of "Batman" is 200
// files that belong together, and 200 cards is a directory listing rather
// than a shelf. Clicking a series expands its issues inline, the same
// single-open accordion the video shelf uses for seasons.
//
// A comic can be in five container formats and two of them (CBR/CBA) cannot
// be read without an external unpacker, so "can this be opened right now" is
// a first-class state here in a way it never is for a book. It is shown on
// the card, not hidden until the reader fails.

var LIB_KIND      = "comic";
var LIB_SORT_KEYS = ["name", "size", "issues"];

var _libOpenSeriesKey = null;   // key of the currently expanded series

// "series" groups issues into one card per run; "issues" lists every issue as
// its own card. Grouping is the right default -- 5,000 loose cards is a
// directory listing, not a shelf -- but it is not the only way people look at
// a collection: "show me everything I have, newest first" is a different and
// equally reasonable question, and it was not answerable at all before.
// Stored per account like the grid/list choice next to it.
//
// Read LAZILY, not here. _libPref lives in library_core.js, which loads AFTER
// this file (the core runs its init on parse and needs LIB_KIND to exist
// first). Calling it at parse time throws a ReferenceError that aborts the
// rest of THIS file -- and because `var` initializers below it never run
// while function declarations are still hoisted, the failure looks like
// "LIB_KIND is undefined" three files away rather than like a load order
// problem here. Nothing in a page module may call into the core at parse time.
var libGroupMode = "series";
var _libGroupPrefRead = false;

function _libValidGroup(v) { return v === "series" || v === "issues"; }

function _libReadGroupPref() {
  if (_libGroupPrefRead || typeof _libPref !== "function") return;
  _libGroupPrefRead = true;
  libGroupMode = _libPref("comic_group", "mf-comic-group", _libValidGroup, "series");
}

// Cover preparation. A cover cannot be read out of a CBR until the server has
// repacked it, so on a library of those the shelf starts blank and fills in
// over the following seconds. Saying so beats showing placeholder tiles that
// look like the scan found nothing.
var _libCoverPrep  = { running: false, done: 0, total: 0, pending: 0, failed: 0 };
var _libCoverPoll  = null;
// Bumped whenever preparation makes progress; appended to the cover URLs so
// the browser refetches the ones that 404'd a moment ago instead of serving
// its cached miss.
var _libCoverBust  = 0;

// ---- Data ----

function libFlattenComics(locations) {
  var items = [];
  (locations || []).forEach(function (loc) {
    (loc.comics || []).forEach(function (series) {
      items.push({ series: series, cpId: loc.custom_path_id, cpLabel: loc.label });
    });
  });
  return items;
}

// Every issue across every series as its own item, carrying the series it
// came from so a card can still say what it belongs to.
function libFlattenIssues(locations) {
  var items = [];
  (locations || []).forEach(function (loc) {
    (loc.comics || []).forEach(function (series) {
      (series.issues || []).forEach(function (issue) {
        items.push({ issue: issue, series: series, cpId: loc.custom_path_id, cpLabel: loc.label });
      });
    });
  });
  return items;
}

function libIssueMatchesQuery(it, q) {
  if ((it.series.series || "").toLowerCase().includes(q)) return true;
  if ((it.issue.title || "").toLowerCase().includes(q)) return true;
  if ((it.issue.file || "").toLowerCase().includes(q)) return true;
  if (String(it.issue.number || "").toLowerCase().includes(q)) return true;
  return false;
}

function libSortIssues(items) {
  return items.slice().sort(function (a, b) {
    var v;
    if (libSortKey === "size") {
      v = (a.issue.size || 0) - (b.issue.size || 0);
    } else if (libSortKey === "issues") {
      // "Issues" means "by date added" here: an issue has no issue COUNT, and
      // newest-first is what people actually want from a flat list.
      v = (a.issue.added_at || 0) - (b.issue.added_at || 0);
    } else {
      var s = (a.series.sort_series || "").localeCompare(b.series.sort_series || "", "de", { sensitivity: "base" });
      if (s !== 0) { v = s; }
      else {
        // Inside one series, reading order beats alphabetical: "10" after "9".
        var na = parseFloat(String(a.issue.number || "").replace(",", ".")) ;
        var nb = parseFloat(String(b.issue.number || "").replace(",", ".")) ;
        if (isNaN(na) && isNaN(nb)) v = (a.issue.file || "").localeCompare(b.issue.file || "");
        else if (isNaN(na)) v = 1;
        else if (isNaN(nb)) v = -1;
        else v = na - nb;
      }
    }
    return libSortAsc ? v : -v;
  });
}

function libSetGroup(mode) {
  if (!_libValidGroup(mode)) return;
  _libGroupPrefRead = true;          // an explicit choice outranks the stored one
  libGroupMode = mode;
  _libSavePref("comic_group", "mf-comic-group", mode);
  _libSyncGroupButtons();
  libPage = 0;                       // a different unit means different pages
  libRender(libLocations);
}

function _libSyncGroupButtons() {
  ["series", "issues"].forEach(function (k) {
    var btn = document.getElementById("libGroup-" + k);
    if (!btn) return;
    var on = k === libGroupMode;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
  // Filtering by "runs vs one-shots" is a statement about series; in the flat
  // issue list there is nothing for it to mean, so it goes away rather than
  // sitting there doing nothing.
  var filters = document.getElementById("libFilterToggle");
  if (filters) filters.hidden = libGroupMode === "issues";
}

function libComicsSpanLocations(locations) {
  var seen = 0;
  (locations || []).forEach(function (loc) { if ((loc.comics || []).length) seen++; });
  return seen > 1;
}

function libSeriesMatchesQuery(series, q) {
  if ((series.series || "").toLowerCase().includes(q)) return true;
  if ((series.publisher || "").toLowerCase().includes(q)) return true;
  for (var w = 0; w < (series.writers || []).length; w++) {
    if ((series.writers[w] || "").toLowerCase().includes(q)) return true;
  }
  // Issue level too: searching a story title should find the run that holds it.
  for (var i = 0; i < (series.issues || []).length; i++) {
    var issue = series.issues[i];
    if ((issue.title || "").toLowerCase().includes(q)) return true;
    if ((issue.file || "").toLowerCase().includes(q)) return true;
  }
  return false;
}

function libFilterComics(items) {
  if (libFilterMode === "all") return items;
  return items.filter(function (it) {
    var many = (it.series.issue_count || 0) > 1;
    return libFilterMode === "series" ? many : !many;
  });
}

function libSortComics(items) {
  return items.slice().sort(function (a, b) {
    var v;
    if (libSortKey === "size") {
      v = (a.series.total_size || 0) - (b.series.total_size || 0);
    } else if (libSortKey === "issues") {
      v = (a.series.issue_count || 0) - (b.series.issue_count || 0);
    } else {
      v = (a.series.sort_series || a.series.series || "")
            .localeCompare(b.series.sort_series || b.series.series || "", "de", { sensitivity: "base" });
    }
    return libSortAsc ? v : -v;
  });
}

// ---- Contract with library_core.js ----

function libSetFilter(mode) {
  libFilterMode = mode;
  ["all", "series", "single"].forEach(function (k) {
    var btn = document.getElementById("libFilter-" + k);
    if (!btn) return;
    btn.classList.toggle("active", k === mode);
    btn.setAttribute("aria-pressed", k === mode ? "true" : "false");
  });
  libPage = 0;
  libRender(libLocations);
}

function libRender(locations) {
  _libReadGroupPref();
  _libSyncGroupButtons();
  if (libGroupMode === "issues") {
    var flat = libFlattenIssues(locations);
    if (libSearchQuery) {
      var fq = libSearchQuery.toLowerCase();
      flat = flat.filter(function (it) { return libIssueMatchesQuery(it, fq); });
    }
    libPaintIssues(libSortIssues(flat));
    return;
  }
  var items = libFilterComics(libFlattenComics(locations));
  if (libSearchQuery) {
    var q = libSearchQuery.toLowerCase();
    items = items.filter(function (it) { return libSeriesMatchesQuery(it.series, q); });
  }
  libPaintComics(libSortComics(items));
}

function libRenderComics() { libRender(libLocations); }

// Counting is done once per fetch; drawing happens again on every cover-poll
// tick, so the two are separate. Walking every series to redraw one spinner
// would be silly on a library with thousands of them.
var _libSummary = { series: 0, issues: 0, size: 0, pending: 0 };

function libUpdateSummary(locations) {
  var items = libFlattenComics(locations);
  var issues = 0, size = 0, pending = 0;
  items.forEach(function (it) {
    issues += it.series.issue_count || 0;
    size += it.series.total_size || 0;
    pending += it.series.needs_conversion_count || 0;
  });
  _libSummary = { series: items.length, issues: issues, size: size, pending: pending };
  libPaintSummaryPills();
  libCoverPollStart();
}

function libPaintSummaryPills() {
  var seriesCount = _libSummary.series, issues = _libSummary.issues;
  var size = _libSummary.size, pending = _libSummary.pending;
  var host = document.getElementById("libSummaryPills");
  if (!host) return;
  var parts = [];
  if (seriesCount) parts.push('<span class="lib-summary-pill"><b>' + seriesCount + '</b> ' +
                               libEsc(t("Reihen", "Series")) + "</span>");
  if (issues) parts.push('<span class="lib-summary-pill lib-summary-pill--issues"><b>' + issues + "</b> " +
                         libEsc(t("Ausgaben", "Issues")) + "</span>");
  // Only shown when there is something to act on -- a pill reading "0 need
  // preparing" is noise on every library that has no CBR at all.
  if (pending) parts.push('<span class="lib-summary-pill lib-summary-pill--pending"><b>' + pending + "</b> " +
                          libEsc(t("vorzubereiten", "to prepare")) + "</span>");
  if (size) parts.push('<span class="lib-summary-pill"><b>' + libFmtSize(size) + "</b> " +
                       libEsc(t("gesamt", "total")) + "</span>");
  if (_libCoverPrep.running) {
    var of = _libCoverPrep.total || 0;
    parts.push('<span class="lib-summary-pill lib-summary-pill--prep">' +
      '<span class="lib-prep-spinner" aria-hidden="true"></span>' +
      libEsc(t("Cover werden vorbereitet", "Preparing covers")) +
      " <b>" + (_libCoverPrep.done || 0) + "/" + of + "</b></span>");
  }
  host.innerHTML = parts.join("");
}

// ---- Paint ----

function libComicCoverUrl(series) {
  if (!series.cover_source) return "";
  var url = "/api/library/comic/cover?path=" + encodeURIComponent(series.cover_source);
  // Without this the browser keeps serving the 404 it cached before the cover
  // existed, and the shelf stays blank until a hard reload.
  return _libCoverBust ? url + "&v=" + _libCoverBust : url;
}

async function libCoverPollTick() {
  try {
    var resp = await fetch("/api/library/comic/covers/status");
    var st = await resp.json();
    var moved = (st.done !== _libCoverPrep.done) || (st.running !== _libCoverPrep.running);
    _libCoverPrep = st;
    if (moved) {
      // Only the pills and the pictures, NOT a full repaint. Rebuilding the
      // grid every couple of seconds while covers trickle in would reset the
      // scroll position under the reader's hands and collapse an open series.
      libPaintSummaryPills();
      libRefreshMissingCovers();
    }
    if (!st.running && _libCoverPoll) {
      window.mfPollStop(_libCoverPoll);
      _libCoverPoll = null;
      libPaintSummaryPills();
      libRefreshMissingCovers();      // one last sweep for the final few
    }
  } catch (e) { /* a failed status check is not worth telling anyone about */ }
}

// Started from libUpdateSummary, i.e. after every fetch, so a scan kicked off
// by the watcher starts the indicator too and not just the first page load.
function libCoverPollStart() {
  if (_libCoverPoll) return;
  _libCoverPoll = window.mfPoll(libCoverPollTick, 2000);
  // mfPoll is setInterval underneath, so the first status would only arrive
  // two seconds from now -- long enough that a short preparation run is over
  // before the indicator ever appears, which is exactly how "it never shows
  // me anything" happens. Ask once, straight away.
  libCoverPollTick();
}

// Re-request the covers that were not there yet. Cards whose image 404'd are
// marked by libComicCoverFailed(); this puts a fresh <img> back with a
// cache-busting query, because the browser would otherwise keep serving the
// miss it cached a moment ago.
function libRefreshMissingCovers() {
  var stale = document.querySelectorAll(".mf-poster-art.mf-comic-nocover[data-cover-src]");
  if (!stale.length) return;
  var bust = Date.now();
  Array.prototype.forEach.call(stale, function (art) {
    art.classList.remove("mf-comic-nocover");
    if (!_libCoverPrep.running) {
      var spin = art.querySelector(".mf-comic-preparing");
      if (spin) spin.remove();
    }
    var faux = art.querySelector(".lib-fauxart");
    if (faux) faux.remove();
    var img = document.createElement("img");
    img.className = "mf-comic-cover";
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    img.onerror = function () { libComicCoverFailed(img); };
    img.src = art.getAttribute("data-cover-src") + "&v=" + bust;
    art.insertBefore(img, art.firstChild);
  });
}

function libComicCoverFailed(img) {
  // No cover is normal, not an error: a CBR that has never been prepared has
  // nothing to extract one from yet. Fall back to the generated tile rather
  // than leaving a broken-image icon on the card.
  var art = img.parentNode;
  if (!art) return;
  img.remove();
  art.insertAdjacentHTML("afterbegin", _libFauxArt(art.getAttribute("data-comic-title") || ""));
  art.classList.add("mf-comic-nocover");
}

function libSeriesSubtitle(series) {
  var bits = [];
  if (series.volume) bits.push(t("Band ", "Vol. ") + series.volume);
  if (series.year) bits.push(String(series.year));
  if (series.publisher) bits.push(series.publisher);
  return bits.join(" · ");
}

function libPaintComics(items) {
  var gridEl = document.getElementById("libGridView");
  var listEl = document.getElementById("libListView");
  var emptyEl = document.getElementById("libEmptyState");
  if (!gridEl || !listEl) return;

  if (!items.length) {
    gridEl.hidden = true;
    listEl.hidden = true;
    if (emptyEl) {
      emptyEl.hidden = false;
      emptyEl.innerHTML =
        '<svg class="mf-empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" ' +
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 8h4v4H7z"/><path d="M14 8h3"/>' +
        '<path d="M14 12h3"/><path d="M7 16h10"/></svg>' +
        "<p>" + libEsc(libSearchQuery
          ? t("Keine Comics gefunden.", "No comics found.")
          : t("Keine Comics gefunden. Lege CBZ-, CBR-, CBT-, CB7-, CBA- oder PDF-Dateien in einen Pfad, der der Comic-Mediathek zugeordnet ist — am besten ein Ordner je Reihe.",
              "No comics found. Put CBZ, CBR, CBT, CB7, CBA or PDF files into a path assigned to the comic library — one folder per series works best.")) + "</p>";
    }
    libRenderPagination(0);
    return;
  }
  if (emptyEl) emptyEl.hidden = true;

  var isGrid = libViewMode === "grid";
  gridEl.hidden = !isGrid;
  listEl.hidden = isGrid;
  var target = isGrid ? gridEl : listEl;

  var totalP = libTotalPages(items.length);
  if (libPage >= totalP) libPage = totalP - 1;
  if (libPage < 0) libPage = 0;
  var start = libPage * libPerPage;
  var pageItems = items.slice(start, start + libPerPage);

  var showVol = libComicsSpanLocations(libLocations);
  var html = [];
  pageItems.forEach(function (it, idx) {
    var pfx = "libComic" + idx;
    html.push(isGrid ? libRenderComicCard(it, pfx, showVol) : libRenderComicRow(it, pfx, showVol));
    if (_libOpenSeriesKey && it.series.key === _libOpenSeriesKey) {
      html.push(libRenderComicDetail(it, pfx));
    }
  });
  target.innerHTML = html.join("");
  libRenderPagination(items.length);
}

// ---- Flat issue view ----

// One issue's own cover. Cheap for CBZ/CBT/CB7 (the route extracts it on the
// spot), and simply absent for a CBR nobody has prepared -- which is why the
// faux tile fallback matters more here than in the series view.
function libIssueCoverUrl(issue) {
  if (!issue.path) return "";
  var url = "/api/library/comic/cover?path=" + encodeURIComponent(issue.path);
  return _libCoverBust ? url + "&v=" + _libCoverBust : url;
}

function libIssueCardTitle(it) {
  var num = it.issue.number ? "#" + it.issue.number : "";
  var name = libIssueLabel(it.issue);
  return [num, name].filter(Boolean).join(" · ") || it.series.series || "";
}

function libPaintIssues(items) {
  var gridEl = document.getElementById("libGridView");
  var listEl = document.getElementById("libListView");
  var emptyEl = document.getElementById("libEmptyState");
  if (!gridEl || !listEl) return;

  if (!items.length) {
    gridEl.hidden = true;
    listEl.hidden = true;
    if (emptyEl) {
      emptyEl.hidden = false;
      emptyEl.innerHTML = "<p>" + libEsc(t("Keine Ausgaben gefunden.", "No issues found.")) + "</p>";
    }
    libRenderPagination(0);
    return;
  }
  if (emptyEl) emptyEl.hidden = true;

  var isGrid = libViewMode === "grid";
  gridEl.hidden = !isGrid;
  listEl.hidden = isGrid;
  var target = isGrid ? gridEl : listEl;

  var totalP = libTotalPages(items.length);
  if (libPage >= totalP) libPage = totalP - 1;
  if (libPage < 0) libPage = 0;
  var start = libPage * libPerPage;
  var pageItems = items.slice(start, start + libPerPage);

  var html = [];
  pageItems.forEach(function (it, idx) {
    html.push(isGrid ? libRenderIssueCard(it, "libIssue" + idx)
                     : libRenderIssueListRow(it, "libIssue" + idx));
  });
  target.innerHTML = html.join("");
  libRenderPagination(items.length);
}

function libRenderIssueCard(it, pfx) {
  var issue = it.issue;
  var cover = libIssueCoverUrl(issue);
  var h = [];
  h.push('<div class="mf-poster-card mf-comic-card mf-issue-card" id="' + pfx + '"' +
         ' role="button" tabindex="0"' +
         ' onclick="libReadIssue(event, \'' + pfx + '\')"' +
         ' onkeydown="libIssueCardKey(event, \'' + pfx + '\')"' +
         ' data-comic-path="' + libEscAttr(issue.path || "") + '"' +
         ' data-comic-ext="' + libEscAttr((issue.ext || "").replace(/^\./, "")) + '"' +
         ' data-comic-key="' + libEscAttr(issue.key || "") + '"' +
         ' data-comic-title="' + libEscAttr((it.series.series || "") + (issue.number ? " #" + issue.number : "")) + '">');
  h.push('<div class="mf-poster-art" data-comic-title="' + libEscAttr(it.series.series || "") + '"' +
         (cover ? ' data-cover-src="' + libEscAttr(cover.split("&v=")[0]) + '"' : "") + '>');
  if (cover) {
    h.push('<img class="mf-comic-cover" src="' + libEscAttr(cover) + '" alt="" loading="lazy" ' +
           'decoding="async" onerror="libComicCoverFailed(this)">');
  } else {
    h.push(_libFauxArt(it.series.series || ""));
  }
  // The flat view is where a missing cover shows most: every card is a single
  // issue, so there is nothing else on it to look at.
  if (_libCoverPrep.running) {
    h.push('<span class="mf-comic-preparing" title="' +
           libEscAttr(t("Cover wird vorbereitet", "Preparing cover")) + '">' +
           '<span class="lib-prep-spinner" aria-hidden="true"></span></span>');
  }
  if (issue.number) h.push('<span class="mf-comic-count">#' + libEsc(issue.number) + "</span>");
  if (issue.needs_conversion) {
    h.push('<span class="mf-issue-flag" title="' +
           libEscAttr(t("Muss einmalig vorbereitet werden", "Has to be prepared once")) + '">!</span>');
  }
  h.push("</div>");
  h.push('<div class="mf-poster-meta">');
  h.push('<div class="mf-poster-title" title="' + libEscAttr(libIssueCardTitle(it)) + '">' +
         libEsc(libIssueCardTitle(it)) + "</div>");
  h.push('<div class="mf-comic-sub">' + libEsc(it.series.series || "") + "</div>");
  h.push('<div class="mf-comic-facts">');
  h.push('<span class="mf-format-badge">' + libEsc(issue.format_label || "") + "</span>");
  h.push('<span class="lib-badge lib-badge-size">' + libEsc(libFmtSize(issue.size || 0)) + "</span>");
  h.push("</div></div></div>");
  return h.join("");
}

function libRenderIssueListRow(it, pfx) {
  var issue = it.issue;
  var h = [];
  h.push('<div class="lib-title-row mf-comic-row" id="' + pfx + '"' +
         ' role="button" tabindex="0"' +
         ' onclick="libReadIssue(event, \'' + pfx + '\')"' +
         ' onkeydown="libIssueCardKey(event, \'' + pfx + '\')"' +
         ' data-comic-path="' + libEscAttr(issue.path || "") + '"' +
         ' data-comic-ext="' + libEscAttr((issue.ext || "").replace(/^\./, "")) + '"' +
         ' data-comic-key="' + libEscAttr(issue.key || "") + '"' +
         ' data-comic-title="' + libEscAttr((it.series.series || "") + (issue.number ? " #" + issue.number : "")) + '">');
  h.push('<div class="lib-row-left">');
  h.push('<span class="mf-comic-issue-num">' + libEsc(issue.number ? "#" + issue.number : "—") + "</span>");
  h.push('<div class="lib-info-col"><div class="lib-info-main">');
  h.push('<span class="lib-title-name">' + libEsc(libIssueLabel(issue) || it.series.series || "") + "</span>");
  h.push('</div><div class="lib-info-meta"><span class="mf-comic-sub">' +
         libEsc(it.series.series || "") + "</span></div></div></div>");
  h.push('<div class="lib-row-right">');
  h.push('<span class="mf-format-badge">' + libEsc(issue.format_label || "") + "</span>");
  h.push('<span class="lib-badge lib-badge-size">' + libEsc(libFmtSize(issue.size || 0)) + "</span>");
  if (issue.needs_conversion) {
    h.push('<span class="lib-badge lib-badge-pending">!</span>');
  }
  h.push("</div></div>");
  return h.join("");
}

// A card in the flat view IS the issue, so activating it opens the reader
// straight away rather than expanding anything.
function libReadIssue(ev, pfx) {
  if (ev && ev.stopPropagation) ev.stopPropagation();
  var el = document.getElementById(pfx);
  if (!el || typeof window.openReader !== "function") return;
  window.openReader(el.getAttribute("data-comic-path") || "",
                    el.getAttribute("data-comic-ext") || "cbz",
                    el.getAttribute("data-comic-title") || "",
                    el.getAttribute("data-comic-key") || "");
}

function libIssueCardKey(ev, pfx) {
  if (!ev) return;
  if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
    ev.preventDefault();
    libReadIssue(ev, pfx);
  }
}

function libPendingBadge(series) {
  if (!series.needs_conversion_count) return "";
  return '<span class="lib-badge lib-badge-pending" title="' +
    libEscAttr(t("Diese Ausgaben müssen einmalig vorbereitet werden, bevor sie sich öffnen lassen.",
                 "These issues have to be prepared once before they can be opened.")) + '">' +
    series.needs_conversion_count + " " + libEsc(t("vorzubereiten", "to prepare")) + "</span>";
}

function libRenderComicCard(it, pfx, showVol) {
  var s = it.series;
  var isOpen = _libOpenSeriesKey === s.key;
  var cover = libComicCoverUrl(s);
  var h = [];
  // The click target is the WHOLE card, not just the cover. .mf-poster-card
  // already carries `cursor: pointer` (mf_components.css), so a handler on the
  // artwork alone means the title and the badges below it look clickable and
  // do nothing -- which reads as "the shelf is broken", not as "aim higher".
  h.push('<div class="mf-poster-card mf-comic-card' + (isOpen ? " is-open" : "") + '" id="' + pfx + '"' +
         ' role="button" tabindex="0" aria-expanded="' + (isOpen ? "true" : "false") + '"' +
         ' onclick="libToggleSeries(\'' + pfx + '\')"' +
         ' onkeydown="libCardKey(event, \'' + pfx + '\')">');
  h.push('<div class="mf-poster-art" data-comic-title="' + libEscAttr(s.series || "") + '"' +
         (cover ? ' data-cover-src="' + libEscAttr(cover.split("&v=")[0]) + '"' : "") + '>');
  if (cover) {
    h.push('<img class="mf-comic-cover" src="' + libEscAttr(cover) + '" alt="" loading="lazy" ' +
           'decoding="async" onerror="libComicCoverFailed(this)">');
  } else {
    h.push(_libFauxArt(s.series || ""));
  }
  // While the server is still extracting first pages, a card without one is
  // not a card without a cover -- it is a card whose cover is on its way.
  if (_libCoverPrep.running) {
    h.push('<span class="mf-comic-preparing" title="' +
           libEscAttr(t("Cover wird vorbereitet", "Preparing cover")) + '">' +
           '<span class="lib-prep-spinner" aria-hidden="true"></span></span>');
  }
  h.push('<span class="mf-comic-count">' + (s.issue_count || 0) + "</span>");
  h.push("</div>");
  h.push('<div class="mf-poster-meta">');
  h.push('<div class="mf-poster-title" title="' + libEscAttr(s.series || "") + '">' + libEsc(s.series || "") + "</div>");
  var sub = libSeriesSubtitle(s);
  if (sub) h.push('<div class="mf-comic-sub">' + libEsc(sub) + "</div>");
  h.push('<div class="mf-comic-facts">');
  h.push('<span class="lib-badge">' + (s.issue_count || 0) + " " +
         libEsc(s.issue_count === 1 ? t("Ausgabe", "issue") : t("Ausgaben", "issues")) + "</span>");
  h.push('<span class="lib-badge lib-badge-size">' + libEsc(libFmtSize(s.total_size || 0)) + "</span>");
  h.push(libPendingBadge(s));
  if (showVol) h.push(volTagHtml(it.cpLabel));
  h.push("</div></div></div>");
  return h.join("");
}

function libRenderComicRow(it, pfx, showVol) {
  var s = it.series;
  var isOpen = _libOpenSeriesKey === s.key;
  var sub = libSeriesSubtitle(s);
  var h = [];
  h.push('<div class="lib-title-row mf-comic-row" id="' + pfx + '" onclick="libToggleSeries(\'' + pfx + '\')">');
  h.push('<div class="lib-row-left">');
  h.push('<span class="lib-arrow' + (isOpen ? " lib-arrow-open" : "") + '">&rsaquo;</span>');
  h.push('<div class="lib-info-col"><div class="lib-info-main">');
  h.push('<span class="lib-title-name">' + libEsc(s.series || "") + "</span></div>");
  if (sub) h.push('<div class="lib-info-meta"><span class="mf-comic-sub">' + libEsc(sub) + "</span></div>");
  h.push("</div></div>");
  h.push('<div class="lib-row-right">');
  h.push('<span class="lib-badge">' + (s.issue_count || 0) + "</span>");
  h.push('<span class="lib-badge lib-badge-size">' + libEsc(libFmtSize(s.total_size || 0)) + "</span>");
  h.push(libPendingBadge(s));
  if (showVol) h.push(volTagHtml(it.cpLabel));
  h.push("</div></div>");
  return h.join("");
}

function libRenderComicDetail(it, pfx) {
  var s = it.series;
  var h = [];
  h.push('<div class="lib-detail-row" id="' + pfx + 'Detail">');
  h.push('<div class="lib-detail-header">');
  h.push('<span class="lib-detail-title">' + libEsc(s.series || "") + "</span>");
  h.push('<button class="lib-action-btn lib-detail-close" onclick="libCloseSeries()" aria-label="' +
         libEscAttr(t("Schließen", "Close")) + '">&times;</button>');
  h.push("</div>");

  if (s.summary) h.push('<p class="mf-comic-desc">' + libEsc(s.summary) + "</p>");
  if ((s.writers || []).length) {
    h.push('<p class="mf-comic-credits">' + libEsc(t("Text: ", "Writer: ") + s.writers.join(", ")) + "</p>");
  }

  h.push('<div class="mf-comic-issues">');
  (s.issues || []).forEach(function (issue) {
    h.push(libRenderIssueRow(issue, s));
  });
  h.push("</div></div>");
  return h.join("");
}

// What to write next to the issue number. A story title if the file carries
// one; otherwise the filename -- but NOT when the filename is only the number
// again, which is the common "Lucky Luke/001.cbr" layout. Printing "#001" and
// "001.cbr" side by side says the same thing twice and pushes everything else
// off a phone screen.
function libIssueLabel(issue) {
  if (issue.title) return issue.title;
  var file = issue.file || "";
  var stem = file.replace(/\.[^.]+$/, "");
  var num = String(issue.number || "");
  if (!num) return file;
  // Compare loosely: "01" and "1" are the same issue, and so are "#01" and "01".
  var norm = function (v) { return v.replace(/^#/, "").replace(/^0+(?=\d)/, "").toLowerCase(); };
  return norm(stem) === norm(num) ? "" : file;
}

function libRenderIssueRow(issue, series) {
  var label = issue.number ? "#" + issue.number : t("ohne Nummer", "no number");
  var name = libIssueLabel(issue);
  var h = [];
  h.push('<div class="mf-comic-issue">');
  h.push('<div class="lib-row-left">');
  h.push('<span class="mf-comic-issue-num">' + libEsc(label) + "</span>");
  h.push('<div class="lib-info-col"><div class="lib-info-main">');
  if (name) {
    h.push('<span class="mf-comic-issue-title">' + libEsc(name) + "</span>");
  } else {
    // Nothing to add -- show the series so the row is not a bare number.
    h.push('<span class="mf-comic-issue-title mf-comic-issue-muted">' +
           libEsc(series.series || "") + "</span>");
  }
  h.push("</div></div></div>");
  h.push('<div class="lib-row-right">');
  h.push('<span class="mf-format-badge">' + libEsc(issue.format_label || "") + "</span>");
  if (issue.page_count) {
    h.push('<span class="lib-badge">' + issue.page_count + " " + libEsc(t("S.", "pp.")) + "</span>");
  }
  h.push('<span class="lib-badge lib-badge-size">' + libEsc(libFmtSize(issue.size || 0)) + "</span>");
  // Every issue opens, readable or not: the reader is where the "prepare
  // this once" flow lives, so sending the user there is more useful than
  // disabling the button and leaving them without a next step.
  h.push('<button class="lib-action-btn lib-btn-play mf-comic-open" title="' +
         libEscAttr(t("Lesen", "Read")) + '" onclick="libReadComic(event, this)"' +
         ' data-comic-path="' + libEscAttr(issue.path) + '"' +
         ' data-comic-ext="' + libEscAttr((issue.ext || "").replace(/^\./, "")) + '"' +
         ' data-comic-key="' + libEscAttr(issue.key || "") + '"' +
         ' data-comic-title="' + libEscAttr((series.series || "") + (issue.number ? " #" + issue.number : "")) + '">' +
         '<svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg></button>');
  if (issue.needs_conversion) {
    h.push('<span class="lib-badge lib-badge-pending" title="' +
           libEscAttr(t("Muss einmalig vorbereitet werden.", "Has to be prepared once.")) + '">!</span>');
  }
  h.push("</div></div>");
  return h.join("");
}

// ---- Interaction ----

// Enter and Space activate a card, the way a real button would. Space is
// swallowed so the page does not scroll underneath the panel that just opened.
function libCardKey(ev, pfx) {
  if (!ev) return;
  if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
    ev.preventDefault();
    libToggleSeries(pfx);
  }
}

function libToggleSeries(pfx) {
  var el = document.getElementById(pfx);
  if (!el) return;
  var items = libSortComics(libFilterComics(libFlattenComics(libLocations)).filter(function (it) {
    if (!libSearchQuery) return true;
    return libSeriesMatchesQuery(it.series, libSearchQuery.toLowerCase());
  }));
  var idx = parseInt(pfx.replace("libComic", ""), 10);
  var item = items[libPage * libPerPage + idx];
  if (!item) return;
  _libOpenSeriesKey = (_libOpenSeriesKey === item.series.key) ? null : item.series.key;
  libRenderComics();
}

function libCloseSeries() {
  _libOpenSeriesKey = null;
  libRenderComics();
}

// Read straight off the button's dataset rather than through an inline
// argument list: a path or a title with a quote in it would otherwise break
// out of the onclick attribute.
function libReadComic(ev, btn) {
  // The event is passed in rather than read off window.event: the issue row
  // sits inside the series card, so without stopping propagation the click
  // would also collapse the series the user just opened.
  if (ev && ev.stopPropagation) ev.stopPropagation();
  var path = btn.getAttribute("data-comic-path") || "";
  var ext = btn.getAttribute("data-comic-ext") || "cbz";
  var title = btn.getAttribute("data-comic-title") || "";
  var key = btn.getAttribute("data-comic-key") || "";
  if (typeof window.openReader !== "function") return;
  window.openReader(path, ext, title, key);
}
