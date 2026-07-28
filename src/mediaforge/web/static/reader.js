// ============================================================
// MediaForge — eBook reader
// ============================================================
// A full-screen overlay, built to the same rules as the video player
// (static/player.js): the markup lives in base.html so any page can open it,
// `openReader()` / `closeReader()` are the whole public surface, and the body
// gets a class while it is open so the page behind stops animating.
//
// Two engines behind one set of controls:
//   PDF          -> pdf.js, one canvas per page, rendered on demand
//   EPUB         -> epub.js, paginated or scrolled, reflows to the viewport
// MOBI/AZW3 are recognised but cannot be rendered in a browser; they are
// offered as a download until the server-side conversion exists, and the
// reader says so rather than opening an empty frame.
//
// Reading position is per account and keyed by the BOOK, not by the file, so
// starting a novel as EPUB and continuing in the PDF keeps your place.

(function () {
  "use strict";

  var SAVE_INTERVAL = 5000;     // how often a position is written back
  var PDF_RENDER_AHEAD = 2;     // pages kept rendered around the current one

  var _open = false;
  var _kind = "";               // "pdf" | "epub"
  var _bookKey = "";
  var _title = "";
  var _saveTimer = null;
  var _pdf = null;              // pdf.js document
  var _pdfPage = 1;
  var _pdfScale = 1.1;
  var _pdfRendering = {};
  var _book = null;             // epub.js Book
  var _rendition = null;
  var _epubLocation = "";
  var _percent = 0;
  var _prefs = null;

  function $id(id) { return document.getElementById(id); }
  function esc(s) { return (window.mfEscape || String)(s == null ? "" : s); }
  function tr(de, en) { return (typeof t === "function") ? t(de, en) : en; }

  // ---- preferences (per account, same channel as the library layout) ----

  var PREF_DEFAULTS = { reader_font: "100", reader_theme: "dark", reader_flow: "paginated" };

  function loadPrefs() {
    var prefs = window._USER_PREFS || {};
    _prefs = {};
    Object.keys(PREF_DEFAULTS).forEach(function (key) {
      var value = prefs[key];
      if (value === undefined || value === null || value === "") {
        try { value = localStorage.getItem("mf-" + key); } catch (e) { value = null; }
      }
      _prefs[key] = value || PREF_DEFAULTS[key];
    });
  }

  function savePref(key, value) {
    _prefs[key] = String(value);
    try { localStorage.setItem("mf-" + key, String(value)); } catch (e) { /* private mode */ }
    if (typeof window.mfSaveUserPref === "function") {
      var patch = {};
      patch[key] = String(value);
      window.mfSaveUserPref(patch);
    }
  }

  // ---- position ----

  function loadPosition(bookKey) {
    return fetch("/api/reading/get?book=" + encodeURIComponent(bookKey))
      .then(function (r) { return r.ok ? r.json() : {}; })
      .catch(function () { return {}; });
  }

  function savePosition() {
    if (!_open || !_bookKey) return Promise.resolve();
    var location = _kind === "pdf" ? String(_pdfPage) : _epubLocation;
    if (!location) return Promise.resolve();
    return fetch("/api/reading/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ book: _bookKey, location: location, percent: _percent, kind: _kind })
    }).catch(function () { /* a lost position must never interrupt reading */ });
  }

  function startSaveTimer() {
    stopSaveTimer();
    _saveTimer = setInterval(savePosition, SAVE_INTERVAL);
  }
  function stopSaveTimer() {
    if (_saveTimer) { clearInterval(_saveTimer); _saveTimer = null; }
  }

  // ---- shell ----

  function setStatus(html, isError) {
    var el = $id("readerStatus");
    if (!el) return;
    el.innerHTML = html || "";
    el.hidden = !html;
    el.classList.toggle("is-error", !!isError);
  }

  function applyTheme() {
    var overlay = $id("readerOverlay");
    if (overlay) overlay.setAttribute("data-reader-theme", _prefs.reader_theme);
    var buttons = document.querySelectorAll("#readerThemes [data-theme]");
    Array.prototype.forEach.call(buttons, function (b) {
      b.classList.toggle("active", b.getAttribute("data-theme") === _prefs.reader_theme);
    });
    if (_rendition) applyEpubStyles();
  }

  function applyEpubStyles() {
    if (!_rendition) return;
    var themes = {
      dark:  { color: "#e8e8f0", background: "#14141c" },
      sepia: { color: "#4a3f35", background: "#f4ecd8" },
      light: { color: "#1c1c22", background: "#ffffff" }
    };
    var palette = themes[_prefs.reader_theme] || themes.dark;
    // epub.js injects into the rendered document, which is a separate context:
    // the app's own stylesheet does not reach inside it, so every visual choice
    // has to be pushed in explicitly.
    _rendition.themes.override("color", palette.color, true);
    _rendition.themes.override("background", palette.background, true);
    _rendition.themes.fontSize(_prefs.reader_font + "%");
  }

  function updateProgressUi() {
    var bar = $id("readerProgressFill");
    var label = $id("readerProgressLabel");
    var pct = Math.max(0, Math.min(100, Math.round(_percent)));
    if (bar) bar.style.width = pct + "%";
    if (label) {
      label.textContent = _kind === "pdf" && _pdf
        ? tr("Seite " + _pdfPage + " von " + _pdf.numPages, "Page " + _pdfPage + " of " + _pdf.numPages)
        : pct + "%";
    }
  }

  // ---- PDF ----

  function pdfWorkerReady() {
    if (!window.pdfjsLib) return false;
    if (!window.pdfjsLib.GlobalWorkerOptions.workerSrc) {
      window.pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/pdf.worker.min.js";
    }
    return true;
  }

  function renderPdfPage(num) {
    if (!_pdf || num < 1 || num > _pdf.numPages) return;
    var host = $id("readerPdf");
    var canvas = host.querySelector('[data-page="' + num + '"]');
    if (!canvas || canvas.getAttribute("data-rendered") === "1" || _pdfRendering[num]) return;
    _pdfRendering[num] = true;
    _pdf.getPage(num).then(function (page) {
      var viewport = page.getViewport({ scale: _pdfScale * (window.devicePixelRatio || 1) });
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      // Leave the CSS box alone: it was sized once from page 1 so the document
      // has a stable height. Only the backing store follows the pixel ratio.
      return page.render({ canvasContext: canvas.getContext("2d"), viewport: viewport }).promise;
    }).then(function () {
      canvas.setAttribute("data-rendered", "1");
      delete _pdfRendering[num];
    }).catch(function () { delete _pdfRendering[num]; });
  }

  function renderPdfWindow() {
    for (var n = _pdfPage - PDF_RENDER_AHEAD; n <= _pdfPage + PDF_RENDER_AHEAD; n++) renderPdfPage(n);
  }

  // Scrolling programmatically fires the same scroll event a reader's own
  // scrolling does, and the handler would immediately recompute the page from
  // a position the browser has not finished animating to -- which is what sent
  // a restored position back to page 1.
  var _suppressScroll = 0;

  function pdfGoTo(num, smooth) {
    if (!_pdf) return;
    num = Math.max(1, Math.min(_pdf.numPages, num));
    _pdfPage = num;
    _percent = (num / _pdf.numPages) * 100;
    var canvas = $id("readerPdf").querySelector('[data-page="' + num + '"]');
    if (canvas) {
      _suppressScroll = Date.now() + 700;
      canvas.scrollIntoView({ behavior: smooth === false ? "auto" : "smooth", block: "start" });
    }
    renderPdfWindow();
    updateProgressUi();
  }

  // Give every page box its true size from the document itself, before a
  // single page is painted. Without it the boxes carry a placeholder height,
  // so the scroll offset of page N is wrong, and jumping to a saved position
  // lands somewhere else entirely -- and then the scrollbar jumps around as
  // real pages replace the placeholders.
  function sizePdfPlaceholders(page) {
    var host = $id("readerPdf");
    if (!host || !page) return;
    var viewport = page.getViewport({ scale: _pdfScale });
    var maxWidth = host.clientWidth - 24;
    var width = Math.min(viewport.width, maxWidth);
    var height = width * (viewport.height / viewport.width);
    Array.prototype.forEach.call(host.querySelectorAll(".mfr-pdf-page"), function (c) {
      c.style.width = Math.round(width) + "px";
      c.style.height = Math.round(height) + "px";
    });
  }

  function openPdf(url, startPage) {
    if (!pdfWorkerReady()) {
      setStatus(esc(tr("Der PDF-Reader konnte nicht geladen werden.",
                       "The PDF reader could not be loaded.")), true);
      return;
    }
    setStatus(esc(tr("PDF wird geladen…", "Loading PDF…")));
    window.pdfjsLib.getDocument({ url: url }).promise.then(function (doc) {
      _pdf = doc;
      var host = $id("readerPdf");
      var html = [];
      for (var n = 1; n <= doc.numPages; n++) {
        // One canvas per page up front, painted on demand: the page boxes give
        // the scrollbar its true length immediately, so a 400-page PDF does not
        // grow under the reader's thumb while it renders.
        html.push('<canvas class="mfr-pdf-page" data-page="' + n + '"></canvas>');
      }
      host.innerHTML = html.join("");
      host.hidden = false;
      setStatus("");
      // Size first, jump second: the target page has to have its real offset
      // before scrollIntoView is asked to find it.
      doc.getPage(1).then(function (first) {
        sizePdfPlaceholders(first);
        pdfGoTo(startPage || 1, false);
        host.addEventListener("scroll", onPdfScroll, { passive: true });
        startSaveTimer();
      });
    }).catch(function (err) {
      setStatus(esc(tr("PDF konnte nicht geöffnet werden: ", "Could not open the PDF: ") +
                    (err && err.message ? err.message : "?")), true);
    });
  }

  var _pdfScrollTimer = null;
  function onPdfScroll() {
    if (_suppressScroll && Date.now() < _suppressScroll) return;
    if (_pdfScrollTimer) return;
    _pdfScrollTimer = setTimeout(function () {
      _pdfScrollTimer = null;
      var host = $id("readerPdf");
      if (!host || !_pdf) return;
      var pages = host.querySelectorAll(".mfr-pdf-page");
      var mid = host.scrollTop + host.clientHeight / 2;
      for (var i = 0; i < pages.length; i++) {
        if (pages[i].offsetTop + pages[i].offsetHeight >= mid) {
          _pdfPage = i + 1;
          break;
        }
      }
      _percent = (_pdfPage / _pdf.numPages) * 100;
      renderPdfWindow();
      updateProgressUi();
    }, 120);
  }

  // ---- EPUB ----

  function openEpub(url, startLocation) {
    if (!window.ePub) {
      setStatus(esc(tr("Der EPUB-Reader konnte nicht geladen werden.",
                       "The EPUB reader could not be loaded.")), true);
      return;
    }
    setStatus(esc(tr("Buch wird geladen…", "Loading book…")));
    var host = $id("readerEpub");
    host.hidden = false;
    host.innerHTML = "";
    try {
      _book = window.ePub(url);
      _rendition = _book.renderTo(host, {
        width: "100%",
        height: "100%",
        spread: "none",
        flow: _prefs.reader_flow === "scrolled" ? "scrolled-doc" : "paginated",
        allowScriptedContent: false
      });
    } catch (e) {
      setStatus(esc(tr("Buch konnte nicht geöffnet werden.", "Could not open the book.")), true);
      return;
    }

    _rendition.display(startLocation || undefined).then(function () {
      setStatus("");
      applyEpubStyles();
      startSaveTimer();
      // Locations let epub.js report a real percentage instead of "chapter 3
      // of 12". It is a full pass over the text, so it happens in the
      // background and the reader stays usable while it runs.
      _book.locations.generate(1200).then(function () { updateEpubProgress(); }).catch(function () {});
      buildEpubToc();
    }).catch(function () {
      setStatus(esc(tr("Buch konnte nicht geöffnet werden.", "Could not open the book.")), true);
    });

    _rendition.on("relocated", function (location) {
      _epubLocation = (location && location.start && location.start.cfi) || "";
      updateEpubProgress(location);
    });
    // Keys pressed inside the rendered document belong to the reader too --
    // without this, arrow keys stop working the moment the text has focus.
    _rendition.on("keyup", onKey);
  }

  function updateEpubProgress(location) {
    if (!_book) return;
    try {
      if (_book.locations && _book.locations.length()) {
        var cfi = _epubLocation || (location && location.start && location.start.cfi);
        if (cfi) _percent = (_book.locations.percentageFromCfi(cfi) || 0) * 100;
      }
    } catch (e) { /* locations not ready yet */ }
    updateProgressUi();
  }

  function buildEpubToc() {
    var list = $id("readerTocList");
    if (!list || !_book) return;
    _book.loaded.navigation.then(function (nav) {
      var items = (nav && nav.toc) || [];
      if (!items.length) {
        list.innerHTML = '<p class="mfr-toc-empty">' +
          esc(tr("Dieses Buch hat kein Inhaltsverzeichnis.", "This book has no table of contents.")) + "</p>";
        return;
      }
      list.innerHTML = items.map(function (item) {
        return '<button type="button" class="mfr-toc-item" data-href="' + esc(item.href) + '">' +
          esc(item.label ? item.label.trim() : "?") + "</button>";
      }).join("");
      Array.prototype.forEach.call(list.querySelectorAll("[data-href]"), function (btn) {
        btn.addEventListener("click", function () {
          if (_rendition) _rendition.display(btn.getAttribute("data-href"));
          toggleToc(false);
        });
      });
    }).catch(function () { /* no navigation document */ });
  }

  // ---- navigation ----

  function next() {
    if (_kind === "epub" && _rendition) _rendition.next();
    else if (_kind === "pdf") pdfGoTo(_pdfPage + 1);
  }
  function prev() {
    if (_kind === "epub" && _rendition) _rendition.prev();
    else if (_kind === "pdf") pdfGoTo(_pdfPage - 1);
  }

  function toggleToc(force) {
    var panel = $id("readerToc");
    if (!panel) return;
    var show = force === undefined ? panel.hidden : force;
    panel.hidden = !show;
  }

  function setFont(delta) {
    var size = Math.max(70, Math.min(200, parseInt(_prefs.reader_font, 10) + delta));
    savePref("reader_font", size);
    var label = $id("readerFontLabel");
    if (label) label.textContent = size + "%";
    if (_kind === "epub") applyEpubStyles();
    else if (_kind === "pdf") { _pdfScale = 1.1 * (size / 100); rerenderPdf(); }
  }

  function rerenderPdf() {
    var host = $id("readerPdf");
    if (!host) return;
    Array.prototype.forEach.call(host.querySelectorAll(".mfr-pdf-page"), function (c) {
      c.removeAttribute("data-rendered");
    });
    _pdfRendering = {};
    if (_pdf) {
      _pdf.getPage(1).then(function (first) {
        sizePdfPlaceholders(first);
        renderPdfWindow();
      });
    } else {
      renderPdfWindow();
    }
  }

  function setTheme(theme) {
    savePref("reader_theme", theme);
    applyTheme();
  }

  function setFlow(flow) {
    savePref("reader_flow", flow);
    var buttons = document.querySelectorAll("#readerFlow [data-flow]");
    Array.prototype.forEach.call(buttons, function (b) {
      b.classList.toggle("active", b.getAttribute("data-flow") === flow);
    });
    if (_kind === "epub" && _rendition) {
      _rendition.flow(flow === "scrolled" ? "scrolled-doc" : "paginated");
      applyEpubStyles();
    }
  }

  function onKey(ev) {
    if (!_open) return;
    var target = ev.target;
    if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
    switch (ev.key) {
      case "Escape":
        if (!$id("readerToc").hidden) { toggleToc(false); break; }
        closeReader();
        break;
      case "ArrowRight": case "PageDown": next(); break;
      case "ArrowLeft":  case "PageUp":   prev(); break;
      case "+": case "=": setFont(10); break;
      case "-": setFont(-10); break;
      case "f": case "F": toggleFullscreen(); break;
      default: return;
    }
    ev.preventDefault();
  }

  function toggleFullscreen() {
    var el = $id("readerOverlay");
    if (!el) return;
    if (!document.fullscreenElement) {
      if (el.requestFullscreen) el.requestFullscreen().catch(function () {});
      else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
    } else if (document.exitFullscreen) {
      document.exitFullscreen().catch(function () {});
    }
  }

  // ---- open / close ----

  var _bound = false;
  function bindOnce() {
    if (_bound) return;
    _bound = true;
    document.addEventListener("keydown", onKey);
    var map = [
      ["readerClose", closeReader], ["readerPrev", prev], ["readerNext", next],
      ["readerFontMinus", function () { setFont(-10); }],
      ["readerFontPlus", function () { setFont(10); }],
      ["readerTocBtn", function () { toggleToc(); }],
      ["readerTocClose", function () { toggleToc(false); }],
      ["readerFullscreen", toggleFullscreen]
    ];
    map.forEach(function (pair) {
      var el = $id(pair[0]);
      if (el) el.addEventListener("click", pair[1]);
    });
    Array.prototype.forEach.call(document.querySelectorAll("#readerThemes [data-theme]"), function (b) {
      b.addEventListener("click", function () { setTheme(b.getAttribute("data-theme")); });
    });
    Array.prototype.forEach.call(document.querySelectorAll("#readerFlow [data-flow]"), function (b) {
      b.addEventListener("click", function () { setFlow(b.getAttribute("data-flow")); });
    });
    var overlay = $id("readerOverlay");
    if (overlay) {
      overlay.addEventListener("click", function (ev) {
        if (ev.target === overlay) closeReader();
      });
    }
  }

  function reset() {
    stopSaveTimer();
    if (_rendition) { try { _rendition.destroy(); } catch (e) {} }
    if (_book) { try { _book.destroy(); } catch (e) {} }
    if (_pdf) { try { _pdf.destroy(); } catch (e) {} }
    _rendition = null; _book = null; _pdf = null;
    _pdfPage = 1; _pdfRendering = {}; _epubLocation = ""; _percent = 0;
    var pdfHost = $id("readerPdf");
    if (pdfHost) {
      pdfHost.removeEventListener("scroll", onPdfScroll);
      pdfHost.innerHTML = ""; pdfHost.hidden = true; pdfHost.scrollTop = 0;
    }
    var epubHost = $id("readerEpub");
    if (epubHost) { epubHost.innerHTML = ""; epubHost.hidden = true; }
    var toc = $id("readerTocList");
    if (toc) toc.innerHTML = "";
    toggleToc(false);
    setStatus("");
  }

  /**
   * Open a book in the overlay.
   *   path     absolute path of the file, as the library reported it
   *   ext      "pdf" | "epub" | ...
   *   title    shown in the header
   *   bookKey  grouping key -- the position is stored against this, not the
   *            file, so switching format keeps your place
   */
  window.openReader = function (path, ext, title, bookKey) {
    ext = String(ext || "").toLowerCase().replace(/^\./, "");
    loadPrefs();
    bindOnce();
    reset();

    _kind = ext === "pdf" ? "pdf" : "epub";
    _title = title || "";
    _bookKey = bookKey || path;
    _open = true;

    var overlay = $id("readerOverlay");
    overlay.style.display = "flex";
    document.body.classList.add("reader-open");
    var titleEl = $id("readerTitle");
    if (titleEl) titleEl.textContent = _title;
    var label = $id("readerFontLabel");
    if (label) label.textContent = _prefs.reader_font + "%";
    applyTheme();
    setFlow(_prefs.reader_flow);
    // The chapter list and the flow switch only mean something for reflowable
    // text; a PDF has fixed pages and its own outline.
    var epubOnly = document.querySelectorAll("[data-reader-epub-only]");
    Array.prototype.forEach.call(epubOnly, function (el) { el.hidden = _kind !== "epub"; });

    if (ext !== "pdf" && ext !== "epub") {
      setStatus(
        esc(tr("Dieses Format (" + ext.toUpperCase() + ") lässt sich im Browser nicht anzeigen.",
               "This format (" + ext.toUpperCase() + ") cannot be displayed in a browser.")) +
        ' <a class="mfr-download" href="/api/library/book/file?path=' + encodeURIComponent(path) +
        '" download>' + esc(tr("Herunterladen", "Download")) + "</a>",
        true);
      return;
    }

    var url = "/api/library/book/file?path=" + encodeURIComponent(path);
    loadPosition(_bookKey).then(function (pos) {
      if (!_open) return;
      if (_kind === "pdf") openPdf(url, parseInt(pos.location, 10) || 1);
      else openEpub(url, pos.location || "");
    });
  };

  window.closeReader = function () {
    if (!_open) return;
    savePosition();
    _open = false;
    reset();
    var overlay = $id("readerOverlay");
    if (overlay) overlay.style.display = "none";
    document.body.classList.remove("reader-open");
    if (document.fullscreenElement && document.exitFullscreen) {
      document.exitFullscreen().catch(function () {});
    }
  };

  // Namespaced surface for modules, mirroring window.MFPlayer.
  window.MFReader = {
    open: function (o) { window.openReader(o.path, o.ext, o.title, o.bookKey); },
    close: function () { window.closeReader(); },
    isOpen: function () { return _open; }
  };
})();
