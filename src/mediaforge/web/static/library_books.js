// ============================================================
// Library — eBooks
// ============================================================
// The book shelf. Depends on library_core.js (see the contract in its
// header) and is loaded BEFORE it, because the core's init runs on parse.
//
// Books used to be a filter mode on the video page, which meant every paint
// went through a branch asking "are we books right now?" and the toolbar had
// to hide the controls that made no sense (episode sort). As its own page it
// simply declares the sort keys it has and paints its own grid.

var LIB_KIND      = "book";
var LIB_SORT_KEYS = ["name", "size"];

var _libOpenBookKey = null;   // key of the currently expanded book

// True when books come from more than one library path. With a single path the
// volume tag is the same word on every row -- noise that costs the title space
// it needs, so it is only shown when it actually distinguishes anything.
function libBooksSpanLocations(locations) {
  var seen = 0;
  (locations || []).forEach(function(loc) { if ((loc.books || []).length) seen++; });
  return seen > 1;
}

function libFlattenBooks(locations) {
  var items = [];
  (locations || []).forEach(function(loc) {
    (loc.books || []).forEach(function(book) {
      items.push({ book: book, cpId: loc.custom_path_id, cpLabel: loc.label });
    });
  });
  return items;
}

function libBookMatchesQuery(book, q) {
  if ((book.title || "").toLowerCase().includes(q)) return true;
  if ((book.series || "").toLowerCase().includes(q)) return true;
  for (var i = 0; i < (book.authors || []).length; i++) {
    if ((book.authors[i] || "").toLowerCase().includes(q)) return true;
  }
  for (var f = 0; f < (book.formats || []).length; f++) {
    if ((book.formats[f].path || "").toLowerCase().includes(q)) return true;
  }
  return false;
}

function libSortBooks(items) {
  return items.slice().sort(function(a, b) {
    var v;
    if (libSortKey === "size") {
      v = (a.book.total_size || 0) - (b.book.total_size || 0);
    } else {
      // Inside a series, volume order beats alphabetical order: "Band 2"
      // must not sort between "Band 11" and "Band 12".
      var sa = (a.book.series || "").toLowerCase(), sb = (b.book.series || "").toLowerCase();
      if (sa && sa === sb) {
        v = (a.book.series_index || 0) - (b.book.series_index || 0);
      } else {
        v = (a.book.sort_title || a.book.title || "")
              .localeCompare(b.book.sort_title || b.book.title || "", "de", { sensitivity: "base" });
      }
    }
    return libSortAsc ? v : -v;
  });
}

// "all" | "series" | "single" -- a shelf's own question, not a media type.
// Someone with 400 books almost always wants either "show me the series I am
// collecting" or "show me the one-offs"; nothing else about a book is a
// useful top-level split. The variable itself lives in library_core.js, which
// both shelves share; only the vocabulary differs.
function libSetFilter(mode) {
  libFilterMode = mode;
  ["all", "series", "single"].forEach(function(k) {
    var btn = document.getElementById("libFilter-" + k);
    if (!btn) return;
    btn.classList.toggle("active", k === mode);
    btn.setAttribute("aria-pressed", k === mode ? "true" : "false");
  });
  libPage = 0;               // result set changed — start back on page 1
  libRender(libLocations);
}

function libFilterBooks(items) {
  if (libFilterMode === "all") return items;
  return items.filter(function(it) {
    var inSeries = !!(it.book.series || "").trim();
    return libFilterMode === "series" ? inSeries : !inSeries;
  });
}

// Contract with library_core.js: paint everything from `locations`.
function libRender(locations) {
  var items = libFilterBooks(libFlattenBooks(locations));
  if (libSearchQuery) {
    var q = libSearchQuery.toLowerCase();
    items = items.filter(function(it) { return libBookMatchesQuery(it.book, q); });
  }
  libPaintBooks(libSortBooks(items));
}

// Kept as the name the in-page handlers (libToggleBook/libCloseBook) call, so
// a re-paint after expanding a book does not have to know about locations.
function libRenderBooks() {
  libRender(libLocations);
}

// Cover preparation. Most book covers live INSIDE the file (an EPUB carries
// its own), and a MOBI has to be converted before one can be read at all, so
// on a fresh library the shelf starts with placeholder tiles and fills in over
// the following seconds. Saying so beats leaving grey cards that look like the
// scan found nothing -- which is exactly what this shelf did before.
var _libCoverPrep  = { running: false, done: 0, total: 0 };
var _libCoverPoll  = null;
var _libCoverBust  = 0;

// Contract with library_core.js: fill the header pills.
function libUpdateSummary(locations) {
  libPaintSummaryPills(locations);
  libCoverPollStart();
}

// Split out of libUpdateSummary so the progress pill can be repainted on its
// own while covers trickle in -- rebuilding the whole grid every two seconds
// would reset the scroll position under the reader's hands and collapse an
// open book.
var _libSummaryLocations = null;

function libPaintSummaryPills(locations) {
  if (locations) _libSummaryLocations = locations;
  var items = libFlattenBooks(_libSummaryLocations || []);
  var size = 0, series = {};
  items.forEach(function(it) {
    size += it.book.total_size || 0;
    var s = (it.book.series || "").trim();
    if (s) series[s.toLowerCase()] = 1;
  });
  var pillsEl = document.getElementById("libSummaryPills");
  if (!pillsEl) return;
  var seriesCount = Object.keys(series).length;
  var parts = [];
  if (items.length)  parts.push('<span class="lib-summary-pill"><b>' + items.length + '</b> ' +
                                libEsc(t("Bücher", "Books")) + '</span>');
  if (seriesCount)   parts.push('<span class="lib-summary-pill lib-summary-pill--series"><b>' + seriesCount + '</b> ' +
                                libEsc(t("Reihen", "Series")) + '</span>');
  if (size)          parts.push('<span class="lib-summary-pill"><b>' + libFmtSize(size) + '</b> ' +
                                libEsc(t("gesamt", "total")) + '</span>');
  if (_libCoverPrep.running) {
    parts.push('<span class="lib-summary-pill lib-summary-pill--prep">' +
      '<span class="lib-prep-spinner" aria-hidden="true"></span>' +
      libEsc(t("Cover werden vorbereitet", "Preparing covers")) +
      " <b>" + (_libCoverPrep.done || 0) + "/" + (_libCoverPrep.total || 0) + "</b></span>");
  }
  pillsEl.innerHTML = parts.join("");
}

async function libCoverPollTick() {
  try {
    var resp = await fetch("/api/library/book/covers/status");
    var st = await resp.json();
    var moved = (st.done !== _libCoverPrep.done) || (st.running !== _libCoverPrep.running);
    _libCoverPrep = st;
    if (moved) {
      // Same reasoning as the comic shelf: libRefreshMissingCovers() sets the
      // cache-buster, but only after an early return that fires whenever no
      // card is currently marked stale. A card that is offscreen is
      // loading="lazy", so it never requested its cover, never failed, and was
      // never marked -- and would then be repainted with the very URL the
      // browser has a cached 404 for.
      _libCoverBust = Date.now();
      libPaintSummaryPills();
      libRefreshMissingCovers();
    }
    if (!st.running && _libCoverPoll) {
      window.mfPollStop(_libCoverPoll);
      _libCoverPoll = null;
      _libCoverBust = Date.now();
      libPaintSummaryPills();
      libRefreshMissingCovers();      // one last sweep for the final few
    }
  } catch (e) { /* a failed status check is not worth telling anyone about */ }
}

// Started after every fetch, so a scan kicked off by the watcher starts the
// indicator too and not just the first page load.
function libCoverPollStart() {
  if (_libCoverPoll) return;
  if (typeof window.mfPoll !== "function") return;
  _libCoverPoll = window.mfPoll(libCoverPollTick, 2000);
  // mfPoll is setInterval underneath, so the first status would only arrive
  // two seconds from now -- long enough that a short run is over before the
  // indicator ever appears, which is how "it never shows me anything" happens.
  libCoverPollTick();
}

// Re-request the covers that were not there yet. Cards whose image 404'd are
// marked by libBookCoverFailed(); this puts a fresh <img> back with a
// cache-busting query, because the browser would otherwise keep serving the
// miss it cached a moment ago.
function libRefreshMissingCovers() {
  var stale = document.querySelectorAll(".mf-poster-art.mf-book-nocover[data-cover-src]");
  if (!stale.length) return;
  var bust = Date.now();
  _libCoverBust = bust;
  Array.prototype.forEach.call(stale, function (art) {
    art.classList.remove("mf-book-nocover");
    if (!_libCoverPrep.running) {
      var spin = art.querySelector(".mf-book-preparing");
      if (spin) spin.remove();
    }
    var faux = art.querySelector(".lib-fauxart");
    if (faux) faux.remove();
    var img = document.createElement("img");
    img.className = "mf-book-cover";
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    img.onerror = function () { libBookCoverFailed(img); };
    img.src = art.getAttribute("data-cover-src") + "&v=" + bust;
    art.insertBefore(img, art.firstChild);
  });
}

// Where a book's cover comes from. A sidecar image next to the file wins --
// it is the one the user (or Calibre) chose deliberately. Otherwise the cover
// is read out of the book itself, which is where all of them actually live.
function libBookCoverUrl(book) {
  var url;
  if (book.cover_path) {
    url = "/api/library/book/cover?path=" + encodeURIComponent(book.cover_path);
  } else {
    var src = libBookCoverSource(book);
    if (!src) return "";
    url = "/api/library/book/embedded-cover?path=" + encodeURIComponent(src);
  }
  // Without this the browser keeps serving the 404 it cached before the cover
  // existed, and the shelf stays blank until a hard reload.
  return _libCoverBust ? url + "&v=" + _libCoverBust : url;
}

// Which file to read the cover out of: the EPUB if there is one, because that
// is a zip member away, and only then a format that needs converting first.
// Mirrors the order _lib_queue_book_covers() queues them in -- the two must
// agree or the shelf asks for a cover nothing was ever asked to prepare.
function libBookCoverSource(book) {
  var formats = book.formats || [];
  var best = null, bestRank = 99;
  formats.forEach(function (f) {
    if (!f.path) return;
    var rank = String(f.ext || "").toLowerCase() === "epub" ? 0 : (f.readable ? 1 : 2);
    if (rank < bestRank) { bestRank = rank; best = f; }
  });
  return best ? best.path : "";
}

// The scanner normalises whatever the metadata carried ("deu", "de-DE") to a
// two-letter code; the shelf shows the language the way a person names it.
var LIB_LANGUAGE_NAMES = {
  de: ["Deutsch", "German"],   en: ["Englisch", "English"],
  fr: ["Französisch", "French"], es: ["Spanisch", "Spanish"],
  it: ["Italienisch", "Italian"], nl: ["Niederländisch", "Dutch"],
  pt: ["Portugiesisch", "Portuguese"], ru: ["Russisch", "Russian"],
  ja: ["Japanisch", "Japanese"], zh: ["Chinesisch", "Chinese"],
  ko: ["Koreanisch", "Korean"], pl: ["Polnisch", "Polish"],
  sv: ["Schwedisch", "Swedish"], da: ["Dänisch", "Danish"],
  no: ["Norwegisch", "Norwegian"], fi: ["Finnisch", "Finnish"],
  cs: ["Tschechisch", "Czech"], tr: ["Türkisch", "Turkish"],
  el: ["Griechisch", "Greek"], hu: ["Ungarisch", "Hungarian"],
  ro: ["Rumänisch", "Romanian"], uk: ["Ukrainisch", "Ukrainian"],
  ar: ["Arabisch", "Arabic"], he: ["Hebräisch", "Hebrew"], la: ["Latein", "Latin"]
};

// A book has a publication year. The day and month a publisher records are
// usually the day a file was made, and a full date invites the reader to
// believe a precision that is not there.
function libBookYear(raw) {
  var match = /(\d{4})/.exec(String(raw || ""));
  return match ? match[1] : String(raw || "");
}

function libLanguageName(code) {
  var pair = LIB_LANGUAGE_NAMES[(code || "").toLowerCase()];
  // An unknown code is shown as it came, in upper case: better an honest
  // "XYZ" than a wrong guess at what the file meant.
  return pair ? t(pair[0], pair[1]) : String(code || "").toUpperCase();
}

function libBookAuthorLine(book) {
  var authors = book.authors || [];
  if (!authors.length) return t("Unbekannter Autor", "Unknown author");
  return authors.slice(0, 2).join(", ") + (authors.length > 2 ? " …" : "");
}

function libBookSeriesLabel(book) {
  if (!book.series) return "";
  var idx = Number(book.series_index);
  // Volume 0 does not exist: where it appears the number came from something
  // that is not a volume ("Industrie 4.0"), and a "#0" badge advertises the
  // bad guess. Show the series without a number instead.
  if (!book.series_index || !isFinite(idx) || idx < 1) return book.series;
  // 2.0 reads as a volume number, 2.5 as a side story -- keep the decimal only
  // when it carries information.
  var shown = (idx % 1 === 0) ? String(Math.round(idx)) : String(idx);
  return book.series + " " + shown;
}

// One badge per FORMAT, not per file. A book kept as two EPUBs plus a MOBI
// reads as "EPUB ×2 · MOBI": three separate chips would re-introduce on the
// card exactly the duplication this whole grouping pass exists to remove, and
// they pushed the size badge onto a third line on a narrow card.
function libBookFormatBadges(book, limit, compact) {
  var order = [], byExt = {};
  (book.formats || []).forEach(function(f) {
    var ext = (f.ext || "").toUpperCase();
    if (!byExt[ext]) { byExt[ext] = { count: 0, readable: !!f.readable, size: 0, drm: false }; order.push(ext); }
    byExt[ext].count++;
    byExt[ext].size += f.size || 0;
    if (f.readable) byExt[ext].readable = true;
    if (f.drm) byExt[ext].drm = true;
  });
  var shown = (limit && order.length > limit) ? order.slice(0, limit) : order;
  var out = shown.map(function(ext) {
    var info = byExt[ext];
    // On a poster card the "×2" is what tips the badge row onto a second
    // line and pushes the size badge off it; the count survives in the
    // tooltip and in the detail panel, where there is room for it.
    var label = (info.count > 1 && !compact) ? (ext + " ×" + info.count) : ext;
    var hint = info.count > 1
      ? t(info.count + " Dateien", info.count + " files") + " · " + libFmtSize(info.size)
      : libFmtSize(info.size);
    // The chip is already greyed out via .is-locked; the tooltip is where the
    // reason fits without widening the badge row on a narrow card.
    if (info.drm && !info.readable) {
      hint += " · " + t("DRM-geschützt", "DRM-protected");
    }
    return '<span class="mf-format-badge' + (info.readable ? '' : ' is-locked') + '" title="' +
      libEscAttr(hint) + '">' + libEsc(label) + '</span>';
  });
  if (shown.length < order.length) {
    out.push('<span class="mf-format-badge">+' + (order.length - shown.length) + '</span>');
  }
  return out.join("");
}

// A cover that fails to load (file moved, unreadable image) must not leave a
// broken-image icon in the grid -- fall back to the same generated tile a book
// with no cover at all gets.
function libBookCoverFailed(img) {
  var art = img.parentNode;
  if (!art) return;
  var title = art.getAttribute("data-book-title") || "";
  img.remove();
  art.insertAdjacentHTML("afterbegin", _libFauxArt(title));
  // Marked, not forgotten: a 404 here usually means "not extracted yet", not
  // "there is none". libRefreshMissingCovers comes back for these.
  art.classList.add("mf-book-nocover");
  if (_libCoverPrep.running && !art.querySelector(".mf-book-preparing")) {
    art.insertAdjacentHTML("beforeend", '<span class="mf-book-preparing" aria-hidden="true"></span>');
  }
}

// Only the last two path segments. The folder is what tells two copies of the
// same book apart, and the full path is one hover away in the title attribute.
function libBookShortPath(path) {
  var parts = String(path || "").split(/[\\/]/).filter(Boolean);
  return parts.slice(-2).join("/");
}

function libPaintBooks(items) {
  var gridEl  = document.getElementById("libGridView");
  var listEl  = document.getElementById("libListView");
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
        '<path d="M4 19.5A2.5 2.5 0 016.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z"/></svg>' +
        // While a scan runs the list is legitimately empty and saying "no
        // books found" is both wrong and alarming -- after a scanner-version
        // bump the cached list is emptied on purpose and re-read, which is
        // exactly when someone is looking at this page.
        '<p>' + libEsc(libSearchQuery
          ? t("Keine Bücher gefunden.", "No books found.")
          : (window.libIsScanning
             ? t("Bibliothek wird eingelesen …", "Reading the library …")
             : t("Keine Bücher gefunden. Lege EPUB-, MOBI-, AZW3- oder PDF-Dateien in einen deiner Bibliothekspfade.",
                 "No books found. Put EPUB, MOBI, AZW3 or PDF files into one of your library paths."))) + '</p>';
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
  var pageStart = libPage * libPerPage;
  var pageItems = items.slice(pageStart, pageStart + libPerPage);

  var html = [];
  var openItem = null, openPfx = null;
  pageItems.forEach(function(it, idx) {
    var pfx = "libBook" + idx;
    html.push(isGrid ? libRenderBookCard(it, pfx) : libRenderBookRow(it, pfx));
    if (_libOpenBookKey && it.book.key === _libOpenBookKey) {
      html.push(libRenderBookDetail(it, pfx));
      openItem = it;
      openPfx = pfx;
    }
  });
  target.innerHTML = html.join("");
  if (openItem && openPfx) { /* detail is rendered inline, nothing to hydrate */ }
  libRenderPagination(items.length);
}

function libRenderBookCard(it, pfx) {
  var book = it.book;
  var isOpen = _libOpenBookKey === book.key;
  var cover = libBookCoverUrl(book);
  var series = libBookSeriesLabel(book);

  var h = [];
  // Whole card, not just the cover -- see the note in library_comics.js.
  h.push('<div class="mf-poster-card mf-book-card' + (isOpen ? ' is-open' : '') + '" id="' + pfx + '"' +
         ' role="button" tabindex="0" aria-expanded="' + (isOpen ? "true" : "false") + '"' +
         ' onclick="libToggleBook(\'' + pfx + '\')"' +
         ' onkeydown="libBookCardKey(event, \'' + pfx + '\')">');
  // data-cover-src is what lets a cover arrive LATE: when the image 404s
  // (nothing extracted yet) the card keeps the URL, and libRefreshMissingCovers
  // swaps a fresh <img> back in as the background worker reports progress --
  // in place, so the grid is never repainted under the reader.
  h.push('<div class="mf-poster-art" data-book-title="' + libEscAttr(book.title || "") + '"' +
         (cover ? ' data-cover-src="' + libEscAttr(cover) + '"' : '') + '>');
  if (cover) {
    h.push('<img class="mf-book-cover" src="' + libEscAttr(cover) + '" alt="" loading="lazy" ' +
           'decoding="async" onerror="libBookCoverFailed(this)">');
  } else {
    h.push(_libFauxArt(book.title || ""));
  }
  // Only while something is actually being prepared. A book that simply has
  // no cover to find must not spin forever.
  if (!cover && _libCoverPrep.running) {
    h.push('<span class="mf-book-preparing" aria-hidden="true"></span>');
  }
  h.push('<div class="mf-poster-scrim">');
  h.push('<div class="mf-poster-meta">');
  if (series) h.push('<span class="mf-type-pill mf-type-pill--outline">' + libEsc(series) + '</span>');
  if (libBooksSpanLocations(libLocations)) h.push(volTagHtml(it.cpLabel));
  h.push('</div>');
  h.push('<p class="mf-poster-title">' + libEsc(book.title || "") + '</p>');
  h.push('<p class="mf-book-author">' + libEsc(libBookAuthorLine(book)) + '</p>');
  h.push('</div>'); // scrim
  h.push('</div>'); // art
  // No size badge here, deliberately. On a film card the file size stands in
  // for quality; on a book it is noise between 1 and 50 MB, and it was the one
  // chip too many that wrapped the format row onto a second line. It stays in
  // the list row and in the detail panel, where it is actually compared.
  h.push('<div class="mf-poster-foot mf-book-foot">');
  h.push('<span class="mf-format-badges">' + libBookFormatBadges(book, 2, true) + '</span>');
  h.push('</div>');
  h.push('</div>');
  return h.join("");
}

function libRenderBookRow(it, pfx) {
  var book = it.book;
  var isOpen = _libOpenBookKey === book.key;
  var series = libBookSeriesLabel(book);
  var multi = libBooksSpanLocations(libLocations);

  // Author and series belong under the title, not at the far right of the row:
  // a book is identified by title AND author together, and separating them by
  // 600px of empty row makes the reader pair them by eye on every line.
  var sub = [];
  sub.push('<span class="mf-book-row-author">' + libEsc(libBookAuthorLine(book)) + '</span>');
  if (series) sub.push('<span class="mf-book-row-series">' + libEsc(series) + '</span>');
  if (multi && it.cpLabel) sub.push('<span class="mf-book-row-vol">' + libEsc(it.cpLabel) + '</span>');

  var h = [];
  h.push('<div class="lib-title-row mf-book-row' + (isOpen ? ' is-open' : '') + '" id="' + pfx +
         '" onclick="libToggleBook(\'' + pfx + '\')">');
  h.push('<div class="mf-book-row-main">');
  h.push('<span class="mf-book-row-title">' + libEsc(book.title || "") + '</span>');
  h.push('<span class="mf-book-row-sub">' + sub.join('<span class="mf-book-row-dot">·</span>') + '</span>');
  h.push('</div>');
  h.push('<div class="mf-book-row-facts">');
  h.push('<span class="mf-format-badges">' + libBookFormatBadges(book, 3, true) + '</span>');
  h.push('<span class="mf-book-row-size">' + libFmtSize(book.total_size) + '</span>');
  h.push('<svg class="mf-book-row-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
         'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
         '<path d="M9 18l6-6-6-6"/></svg>');
  h.push('</div>');
  h.push('</div>');
  return h.join("");
}

function libRenderBookDetail(it, pfx) {
  var book = it.book;
  var h = [];
  h.push('<div class="lib-detail-row mf-book-detail" id="' + pfx + 'Detail">');
  h.push('<div class="lib-detail-header">');
  h.push('<div>');
  h.push('<h3 class="lib-detail-title">' + libEsc(book.title || "") + '</h3>');
  h.push('<p class="mf-book-author">' + libEsc(libBookAuthorLine(book)) + '</p>');
  h.push('</div>');
  h.push('<button type="button" class="mf-icon-btn" onclick="libCloseBook()" aria-label="' +
         libEscAttr(t("Schließen", "Close")) + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
         'stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>');
  h.push('</div>');

  var facts = [];
  if (book.series) facts.push([t("Reihe", "Series"), libBookSeriesLabel(book)]);
  if (book.published) facts.push([t("Erschienen", "Published"), libBookYear(book.published)]);
  if (book.publisher) facts.push([t("Verlag", "Publisher"), book.publisher]);
  if (book.language) facts.push([t("Sprache", "Language"), libLanguageName(book.language)]);
  if (book.isbn) facts.push(["ISBN", book.isbn]);
  if (facts.length) {
    h.push('<dl class="mf-book-facts">');
    facts.forEach(function(pair) {
      h.push('<dt>' + libEsc(pair[0]) + '</dt><dd>' + libEsc(String(pair[1])) + '</dd>');
    });
    h.push('</dl>');
  }
  if (book.description) {
    h.push('<p class="mf-book-desc">' + libEsc(book.description) + '</p>');
  }
  if ((book.tags || []).length) {
    h.push('<div class="mf-book-tags">');
    book.tags.slice(0, 12).forEach(function(tag) {
      h.push('<span class="mf-chip-static">' + libEsc(tag) + '</span>');
    });
    h.push('</div>');
  }

  // Every file that belongs to this book. This list is the whole point of the
  // de-duplication being visible rather than silent: the shelf shows one book,
  // and here is the evidence for why -- including the two identical EPUBs a
  // Calibre import can leave behind. Nothing is ever deleted automatically.
  h.push('<h4 class="mf-book-files-title">' +
         libEsc(t("Dateien", "Files")) + ' <span class="lib-badge">' + (book.formats || []).length + '</span></h4>');
  h.push('<div class="mf-book-files">');
  (book.formats || []).forEach(function(f) {
    h.push('<div class="mf-book-file">');
    h.push('<span class="mf-format-badge' + (f.readable ? '' : ' is-locked') + '">' +
           libEsc((f.ext || "").toUpperCase()) + '</span>');
    h.push('<span class="mf-book-file-path" title="' + libEscAttr(f.path) + '">' +
           libEsc(libBookShortPath(f.path)) + '</span>');
    h.push('<span class="mf-book-file-size">' + libFmtSize(f.size) + '</span>');
    if (f.readable) {
      // Opens in the reader overlay, the same way a film opens in the player
      // instead of a browser tab. The position is stored against the BOOK, so
      // the format picked here does not start a separate bookmark.
      h.push('<button type="button" class="mf-book-open" ' +
             'onclick="libReadBook(\'' + libEscAttr(encodeURIComponent(f.path)) + '\', \'' +
             libEscAttr(f.ext) + '\', \'' + libEscAttr(encodeURIComponent(book.key)) + '\')">' +
             '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
             'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
             '<path d="M4 19.5A2.5 2.5 0 016.5 17H20"/>' +
             '<path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z"/></svg>' +
             '<span>' + libEsc(t("Lesen", "Read")) + '</span></button>');
      h.push('<a class="mf-book-dl" href="/api/library/book/file?path=' +
             encodeURIComponent(f.path) + '" download title="' +
             libEscAttr(t("Herunterladen", "Download")) + '" aria-label="' +
             libEscAttr(t("Herunterladen", "Download")) + '">' +
             '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
             'stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>' +
             '<polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></a>');
    } else {
      // DRM gets its own wording: "this format cannot be opened" reads like a
      // missing feature, while the actual reason is that the file is
      // encrypted -- typically a Kindle purchase, which no converter can undo.
      h.push('<span class="mf-book-locked" title="' +
             libEscAttr(f.drm
               ? t("Diese Datei ist DRM-geschützt (z. B. ein Kindle-Kauf) und lässt sich nicht öffnen.",
                   "This file is DRM-protected (e.g. a Kindle purchase) and cannot be opened.")
               : t("Dieses Format ist kopiergeschützt und lässt sich nicht öffnen.",
                   "This format is copy-protected and cannot be opened.")) + '">' +
             libEsc(f.drm ? t("DRM-geschützt", "DRM-protected") : t("Geschützt", "Protected")) + '</span>');
      // Still downloadable: the file is the user's own, it just cannot be
      // rendered here. Without this the row offered no action at all.
      h.push('<a class="mf-book-dl" href="/api/library/book/file?path=' +
             encodeURIComponent(f.path) + '" download title="' +
             libEscAttr(t("Herunterladen", "Download")) + '" aria-label="' +
             libEscAttr(t("Herunterladen", "Download")) + '">' +
             '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
             'stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>' +
             '<polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></a>');
    }
    h.push('</div>');
  });
  h.push('</div>');
  h.push('</div>');
  return h.join("");
}

function libBookCardKey(ev, pfx) {
  if (!ev) return;
  if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
    ev.preventDefault();
    libToggleBook(pfx);
  }
}

function libToggleBook(pfx) {
  var el = document.getElementById(pfx);
  if (!el) return;
  var items = libSortBooks(libFlattenBooks(libLocations).filter(function(it) {
    if (!libSearchQuery) return true;
    return libBookMatchesQuery(it.book, libSearchQuery.toLowerCase());
  }));
  var idx = parseInt(pfx.replace("libBook", ""), 10);
  var item = items[libPage * libPerPage + idx];
  if (!item) return;
  _libOpenBookKey = (_libOpenBookKey === item.book.key) ? null : item.book.key;
  libRenderBooks();
}

function libCloseBook() {
  _libOpenBookKey = null;
  libRenderBooks();
}

// Open a book in the reader overlay. Path and key arrive URI-encoded because
// they travel through an inline onclick attribute, where a quote or a backslash
// in a filename would otherwise break out of the handler.
function libReadBook(encodedPath, ext, encodedKey) {
  var path = decodeURIComponent(encodedPath);
  var key = decodeURIComponent(encodedKey || "");
  var title = "";
  var items = libFlattenBooks(libLocations);
  for (var i = 0; i < items.length; i++) {
    if (items[i].book.key === key) { title = items[i].book.title; break; }
  }
  if (typeof window.openReader !== "function") {
    // Should not happen -- reader.js loads from base.html on every page -- but
    // a missing reader must not swallow the click without explanation.
    window.open("/api/library/book/file?path=" + encodeURIComponent(path), "_blank", "noopener");
    return;
  }
  window.openReader(path, ext, title, key);
}

