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
  // Formats the server converts to EPUB before the reader can show them.
  var CONVERTIBLE = ["mobi", "azw3", "azw"];

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

  var PREF_DEFAULTS = {
    reader_font: "100",          // text size, percent
    reader_theme: "dark",
    reader_flow: "paginated",
    reader_face: "serif",        // serif | sans | original (the book's own)
    reader_lead: "1.65",         // line height
    reader_width: "44"           // reading measure, rem
  };

  var FACE_STACKS = {
    serif: '"Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Charter, ' +
           '"Bitstream Charter", Georgia, "Liberation Serif", "Nimbus Roman", "Times New Roman", serif',
    sans:  'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Liberation Sans", ' +
           '"Helvetica Neue", Arial, sans-serif'
  };

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

  // ---- chrome ----
  // The bars fade out while reading and return on any sign of a reader: a
  // mouse move, a tap, a key, the settings panel opening. The idle timer is
  // long enough that a slow reader is never left hunting for the controls.
  var IDLE_MS = 2800;
  var _idleTimer = null;

  function wakeChrome() {
    var shell = $id("readerShell");
    if (!shell) return;
    shell.classList.remove("is-idle");
    if (_idleTimer) clearTimeout(_idleTimer);
    _idleTimer = setTimeout(function () {
      // Never hide the chrome while a panel is open or the book is not there
      // yet -- both are moments where the reader is looking AT the controls.
      var sheet = $id("readerSheet"), toc = $id("readerToc"), status = $id("readerStatus");
      if ((sheet && !sheet.hidden) || (toc && !toc.hidden) || (status && !status.hidden)) return;
      var s2 = $id("readerShell");
      if (s2 && _open) s2.classList.add("is-idle");
    }, IDLE_MS);
  }

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

  var PALETTES = {
    dark:  { color: "#e8e8f0", background: "#14141c", link: "#a78bfa" },
    sepia: { color: "#453a2e", background: "#f4ecd8", link: "#7c5c2e" },
    light: { color: "#1b1b20", background: "#fdfdfb", link: "#5b3fd0" }
  };

  // Everything that carries running text. Naming them one by one rather than
  // using `*` is deliberate: `*` would also hit <html>, <head> and <img>, and
  // a font-size on an image container is a resizing bug waiting to happen.
  var TEXT_SELECTOR = "p, div, span, li, dd, dt, td, th, blockquote, section, " +
                      "article, figcaption, em, strong, i, b, small, sub, sup, a";

  var STYLE_ID = "mf-reader-style";

  /** The stylesheet pushed into the book's own document. */
  function readerRules() {
    var palette = PALETTES[_prefs.reader_theme] || PALETTES.dark;
    var stack = FACE_STACKS[_prefs.reader_face];
    var size = parseInt(_prefs.reader_font, 10) || 100;
    var lead = _prefs.reader_lead;
    var rules = [
      // Mobile Safari and Chrome on Android silently scale text in a frame
      // they consider too narrow, which fights every size the reader picks.
      "html { -webkit-text-size-adjust: none; text-size-adjust: none; }",
      "html, body { background: " + palette.background + " !important; " +
        "color: " + palette.color + " !important; }",
      // The size is set once, on <body>, and every descendant is pushed back to
      // "same as my parent". That is what makes the setting actually do
      // something: publishers routinely set `font-size: 11pt` on <p>, and an
      // absolute size on the paragraph ignores anything set further up -- which
      // is why raising the size appeared to change nothing at all.
      "body { font-size: " + size + "% !important; line-height: " + lead + " !important; }",
      TEXT_SELECTOR + " { font-size: 100% !important; line-height: " + lead + " !important; " +
        "color: " + palette.color + " !important; }",
      // Headings keep a hierarchy, but a relative one, so they scale with the
      // body instead of staying at whatever the publisher hard-coded.
      "h1 { font-size: 1.7em !important; } h2 { font-size: 1.45em !important; }",
      "h3 { font-size: 1.25em !important; } h4 { font-size: 1.1em !important; }",
      "h5, h6 { font-size: 1em !important; }",
      "h1, h2, h3, h4, h5, h6 { color: " + palette.color + " !important; line-height: 1.25 !important; }",
      "a, a * { color: " + palette.link + " !important; }",
      // A fixed-width image in a reflowed column is the one thing that can push
      // a page sideways and hide the text under it.
      "img, svg, video, table { max-width: 100% !important; height: auto !important; }"
    ];
    if (stack) {
      rules.push("body, " + TEXT_SELECTOR + ", h1, h2, h3, h4, h5, h6 { " +
                 "font-family: " + stack + " !important; }");
      // Code is the exception a reading font must not swallow: alignment in a
      // listing carries meaning that a proportional face destroys.
      rules.push("pre, code, kbd, samp, tt { font-family: ui-monospace, SFMono-Regular, " +
                 'Menlo, Consolas, "Liberation Mono", monospace !important; }');
    }
    return rules.join("\n");
  }

  /** Put (or refresh) that stylesheet inside one rendered section. */
  function injectStyles(contents) {
    try {
      var doc = contents && contents.document;
      if (!doc || !doc.head) return;
      var el = doc.getElementById(STYLE_ID);
      if (!el) {
        el = doc.createElement("style");
        el.id = STYLE_ID;
        // Last in <head> so it wins on equal specificity even before
        // !important is considered.
        doc.head.appendChild(el);
      }
      el.textContent = readerRules();
    } catch (e) { /* the document can go away mid-update */ }
  }

  function applyEpubStyles() {
    if (!_rendition) return;
    var palette = PALETTES[_prefs.reader_theme] || PALETTES.dark;
    // epub.js's own override still runs, but only for the two properties that
    // must be right in the very first painted frame -- it is applied while a
    // section is being set up, before there is a document to inject into, and
    // without it a dark book flashes white for one frame.
    try {
      _rendition.themes.override("color", palette.color, true);
      _rendition.themes.override("background", palette.background, true);
    } catch (e) { /* older epub.js */ }
    // The real work: a stylesheet inside the book's document. themes.override
    // only ever writes `body { ... }`, and a publisher rule on `p` beats it on
    // specificity -- which is exactly why typeface and size did nothing on the
    // books that name their own.
    var contents = [];
    try { contents = _rendition.getContents() || []; } catch (e) { contents = []; }
    if (contents && !contents.length && contents.document) contents = [contents];
    Array.prototype.forEach.call(contents, injectStyles);
  }

  function applyLayout() {
    var shell = $id("readerShell");
    if (shell) shell.style.setProperty("--mfr-measure", _prefs.reader_width + "rem");
  }

  var _chapterLabel = "";

  function updateProgressUi() {
    var bar = $id("readerProgressFill");
    var label = $id("readerProgressLabel");
    var chapter = $id("readerChapter");
    var pct = Math.max(0, Math.min(100, Math.round(_percent)));
    if (bar) bar.style.width = pct + "%";
    if (label) {
      label.textContent = _kind === "pdf" && _pdf
        ? tr(_pdfPage + " / " + _pdf.numPages, _pdfPage + " / " + _pdf.numPages)
        : pct + "%";
    }
    // Where you are beats how far you are: a chapter name is the answer to
    // "where was I", a percentage is only the answer to "how much is left".
    if (chapter) chapter.textContent = _chapterLabel;
    syncMarkState();
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

    // A CFI is only meaningful inside the exact rendering it came from. The
    // position is deliberately shared across a book's formats, so the CFI
    // saved while reading the EPUB is structurally meaningless in the EPUB
    // converted from the same book's MOBI -- display() rejects, and without
    // this fallback that surfaced as "could not open the book" on a book that
    // opens perfectly well. Losing the position is the right price; refusing
    // to open the book is not.
    var start = startLocation
      ? _rendition.display(startLocation).catch(function () {
          logLostPosition();
          return _rendition.display();
        })
      : _rendition.display();

    start.then(function () {
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
      updateChapterLabel(location);
      updateEpubProgress(location);
    });
    // Keys pressed inside the rendered document belong to the reader too --
    // without this, arrow keys stop working the moment the text has focus.
    _rendition.on("keyup", onKey);

    // The book renders inside an iframe, and an iframe is a separate document:
    // its events never reach a listener on the overlay. While reading, the
    // pointer is over exactly that iframe -- so without this hook, moving the
    // mouse or tapping the page could not bring the hidden chrome back, and
    // the only way out would be the keyboard.
    try {
      _rendition.hooks.content.register(function (contents) {
        var doc = contents && contents.document;
        if (!doc) return;
        // Every section is a fresh document and needs the reader's stylesheet
        // of its own; this is the hook that makes typeface, size, spacing and
        // paper survive turning the page into a new chapter.
        injectStyles(contents);
        ["mousemove", "pointerdown", "wheel", "touchstart"].forEach(function (evt) {
          doc.addEventListener(evt, wakeChrome, { passive: true });
        });
        // A tap in the middle of the page is the standard reader gesture for
        // "show me the controls"; the outer tap zones handle the edges.
        doc.addEventListener("click", function () { wakeChrome(); }, { passive: true });
      });
    } catch (e) { /* older epub.js without content hooks */ }
  }

  function logLostPosition() {
    // Not shown to the reader: they are at the start of the book, which is
    // self-evident, and a toast for it would be noise on every format switch.
    if (window.console && console.info) {
      console.info("[Reader] saved position does not apply to this file, starting at the beginning");
    }
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

  var _tocEntries = [];
  var _tocScanned = false;   // the spine fallback has already run (or is running)

  function updateChapterLabel(location) {
    if (!_tocEntries.length || !location || !location.start) return;
    var href = String(location.start.href || "").split("#")[0];
    var label = "";
    for (var i = 0; i < _tocEntries.length; i++) {
      if (chapterHref(_tocEntries[i].href) === href) { label = _tocEntries[i].label; break; }
    }
    if (label) _chapterLabel = label;
    markCurrentChapter(href);
  }

  function chapterHref(href) {
    return String(href || "").split("#")[0].replace(/^\.?\//, "");
  }

  function markCurrentChapter(href) {
    var list = $id("readerChapters");
    if (!list) return;
    Array.prototype.forEach.call(list.querySelectorAll("[data-href]"), function (btn) {
      btn.classList.toggle("is-current",
        chapterHref(btn.getAttribute("data-href")) === chapterHref(href));
    });
  }

  /** Flatten epub.js's nested navigation into rows that carry their depth. */
  function flattenNav(items, depth, out) {
    (items || []).forEach(function (item) {
      out.push({
        href: item.href,
        label: (item.label || "").trim() || "?",
        depth: Math.min(depth, 2)
      });
      if (item.subitems && item.subitems.length) flattenNav(item.subitems, depth + 1, out);
    });
    return out;
  }

  function renderChapters(entries, note) {
    var list = $id("readerChapters");
    var head = $id("readerChaptersHead");
    if (!list) return;
    if (head) head.hidden = false;
    if (!entries.length) {
      list.innerHTML = '<p class="mfr-toc-empty">' + esc(note ||
        tr("Dieses Buch hat kein Inhaltsverzeichnis.", "This book has no table of contents.")) + "</p>";
      return;
    }
    list.innerHTML = entries.map(function (item) {
      return '<button type="button" class="mfr-toc-item" data-depth="' + (item.depth || 0) +
        '" data-href="' + esc(item.href) + '">' + esc(item.label) + "</button>";
    }).join("");
    Array.prototype.forEach.call(list.querySelectorAll("[data-href]"), function (btn) {
      btn.addEventListener("click", function () {
        if (_rendition) _rendition.display(btn.getAttribute("data-href"));
        toggleToc(false);
      });
    });
    try {
      if (_rendition && _rendition.currentLocation) {
        updateChapterLabel(_rendition.currentLocation());
        updateProgressUi();
      }
    } catch (e) { /* location not settled yet */ }
  }

  function buildEpubToc() {
    if (!_book) return;
    _book.loaded.navigation.then(function (nav) {
      _tocEntries = flattenNav((nav && nav.toc) || [], 0, []);
      if (_tocEntries.length) {
        _tocScanned = true;
        renderChapters(_tocEntries);
      } else {
        // Nothing yet. The spine walk below can supply a list, but it opens
        // every section of the book to do it, so it waits until the reader
        // actually asks for the chapter list.
        renderChapters([], tr("Kapitel werden beim Öffnen der Liste ermittelt.",
                              "Chapters are worked out when you open the list."));
      }
    }).catch(function () {
      renderChapters([], tr("Kapitel werden beim Öffnen der Liste ermittelt.",
                            "Chapters are worked out when you open the list."));
    });
  }

  // A book with no navigation document is not a book without chapters -- it is
  // usually a converted Kindle file, where the chapter structure survives only
  // as the headings inside each section. Reading those back is what turns "no
  // table of contents" into a usable list.
  var _MAX_SPINE_SCAN = 400;

  function ensureSpineToc() {
    if (_tocScanned || !_book || !_book.spine) return;
    _tocScanned = true;
    var sections = [];
    try {
      _book.spine.each(function (section) { sections.push(section); });
    } catch (e) { return; }
    if (!sections.length) return;
    renderChapters([], tr("Kapitel werden gelesen…", "Reading chapters…"));

    var entries = [];
    var chain = Promise.resolve();
    sections.slice(0, _MAX_SPINE_SCAN).forEach(function (section, index) {
      chain = chain.then(function () {
        return section.load(_book.load.bind(_book)).then(function (doc) {
          var heading = null;
          try { heading = doc && doc.querySelector && doc.querySelector("h1, h2, h3, title"); }
          catch (e) { heading = null; }
          var label = heading ? String(heading.textContent || "").replace(/\s+/g, " ").trim() : "";
          if (label.length > 90) label = label.slice(0, 90) + "…";
          // A section with no heading still deserves a row: it is the only way
          // to reach the front matter or an unnamed interlude.
          entries.push({
            href: section.href,
            label: label || (tr("Abschnitt ", "Section ") + (index + 1)),
            depth: 0
          });
          try { section.unload(); } catch (e) { /* already gone */ }
        }).catch(function () { /* an unreadable section is simply skipped */ });
      });
    });
    chain.then(function () {
      if (!_open) return;
      _tocEntries = entries;
      renderChapters(entries);
    });
  }

  // ---- bookmarks ----
  // A position and a bookmark are different promises. The position is written
  // every few seconds and answers "where did I stop"; a bookmark is chosen and
  // answers "take me back here", so nothing but the reader may remove one.

  var _bookmarks = [];

  function currentLocation() {
    return _kind === "pdf" ? String(_pdfPage) : _epubLocation;
  }

  function bookmarkAt(location) {
    for (var i = 0; i < _bookmarks.length; i++) {
      if (_bookmarks[i].location === location) return _bookmarks[i];
    }
    return null;
  }

  /** Reflect "this page is bookmarked" in the header button and the ribbon. */
  function syncMarkState() {
    var marked = !!(currentLocation() && bookmarkAt(currentLocation()));
    var btn = $id("readerMarkBtn");
    if (btn) {
      btn.classList.toggle("is-marked", marked);
      btn.setAttribute("aria-pressed", marked ? "true" : "false");
    }
    var ribbon = $id("readerMark");
    if (ribbon) ribbon.classList.toggle("is-on", marked);
  }

  function loadBookmarks() {
    if (!_bookKey) return Promise.resolve();
    return fetch("/api/reading/bookmarks?book=" + encodeURIComponent(_bookKey))
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (data) {
        _bookmarks = (data && data.bookmarks) || [];
        renderBookmarks();
        syncMarkState();
      })
      .catch(function () { /* a missing list must not stop the book opening */ });
  }

  function toggleBookmark() {
    var location = currentLocation();
    wakeChrome();
    if (!location) return;
    if (bookmarkAt(location)) { removeBookmark(location); return; }

    var label = _chapterLabel ||
      (_kind === "pdf" ? tr("Seite ", "Page ") + _pdfPage : "");
    var entry = {
      location: location, kind: _kind, label: label,
      percent: Math.max(0, Math.min(100, _percent))
    };
    // Optimistic: the mark appears the moment it is asked for, and a failed
    // write takes it away again. Waiting for the round trip makes the button
    // feel broken on a slow connection.
    _bookmarks.push(entry);
    sortBookmarks();
    renderBookmarks();
    syncMarkState();

    fetch("/api/reading/bookmark", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        book: _bookKey, location: location, kind: _kind,
        label: label, percent: entry.percent
      })
    }).then(function (r) { return r.ok ? r.json() : { error: "http" }; })
      .then(function (res) {
        if (!res || !res.error) return;
        _bookmarks = _bookmarks.filter(function (b) { return b !== entry; });
        renderBookmarks();
        syncMarkState();
      })
      .catch(function () {
        _bookmarks = _bookmarks.filter(function (b) { return b !== entry; });
        renderBookmarks();
        syncMarkState();
      });
  }

  function removeBookmark(location) {
    _bookmarks = _bookmarks.filter(function (b) { return b.location !== location; });
    renderBookmarks();
    syncMarkState();
    fetch("/api/reading/bookmark/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ book: _bookKey, location: location })
    }).catch(function () { /* it is gone from the list either way */ });
  }

  function sortBookmarks() {
    _bookmarks.sort(function (a, b) { return (a.percent || 0) - (b.percent || 0); });
  }

  function goToBookmark(entry) {
    if (entry.kind === "pdf") pdfGoTo(parseInt(entry.location, 10) || 1);
    else if (_rendition) _rendition.display(entry.location).catch(function () {});
    toggleToc(false);
  }

  var _TRASH_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2m-9 0v14a2 2 0 002 2h6' +
    'a2 2 0 002-2V6"/></svg>';

  function renderBookmarks() {
    var host = $id("readerBookmarks");
    var head = $id("readerBookmarksHead");
    if (!host) return;
    // A CFI written by the EPUB means nothing to the PDF of the same book, so
    // only the ones this engine can actually jump to are offered.
    var usable = _bookmarks.filter(function (b) { return (b.kind || "epub") === _kind; });
    if (head) head.hidden = !usable.length;
    if (!usable.length) { host.innerHTML = ""; return; }

    host.innerHTML = usable.map(function (entry, index) {
      var pct = Math.round(entry.percent || 0);
      return '<div class="mfr-toc-row">' +
        '<button type="button" class="mfr-toc-item" data-mark="' + index + '">' +
          esc(entry.label || tr("Lesezeichen", "Bookmark")) +
          '<span class="mfr-toc-when">' + pct + "%</span>" +
        "</button>" +
        '<button type="button" class="mfr-toc-del" data-drop="' + index + '" ' +
          'aria-label="' + esc(tr("Lesezeichen entfernen", "Remove bookmark")) + '">' +
          _TRASH_SVG + "</button>" +
        "</div>";
    }).join("");

    Array.prototype.forEach.call(host.querySelectorAll("[data-mark]"), function (btn) {
      btn.addEventListener("click", function () {
        goToBookmark(usable[parseInt(btn.getAttribute("data-mark"), 10)]);
      });
    });
    Array.prototype.forEach.call(host.querySelectorAll("[data-drop]"), function (btn) {
      btn.addEventListener("click", function () {
        removeBookmark(usable[parseInt(btn.getAttribute("data-drop"), 10)].location);
      });
    });
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

  function toggleSheet(force) {
    var sheet = $id("readerSheet");
    var btn = $id("readerSheetBtn");
    if (!sheet) return;
    var show = force === undefined ? sheet.hidden : force;
    sheet.hidden = !show;
    if (btn) {
      btn.setAttribute("aria-expanded", show ? "true" : "false");
      btn.classList.toggle("active", show);
    }
    wakeChrome();
  }

  function syncSheet() {
    var pairs = [
      ["#readerFont [data-font]", "data-font", _prefs.reader_face],
      ["#readerLead [data-lead]", "data-lead", _prefs.reader_lead],
      ["#readerWidth [data-width]", "data-width", _prefs.reader_width],
      ["#readerThemes [data-theme]", "data-theme", _prefs.reader_theme],
      ["#readerFlow [data-flow]", "data-flow", _prefs.reader_flow]
    ];
    pairs.forEach(function (p) {
      Array.prototype.forEach.call(document.querySelectorAll(p[0]), function (b) {
        b.classList.toggle("active", b.getAttribute(p[1]) === String(p[2]));
      });
    });
    var label = $id("readerSizeLabel");
    if (label) label.textContent = _prefs.reader_font + "%";
  }

  function toggleToc(force) {
    var panel = $id("readerToc");
    if (!panel) return;
    var show = force === undefined ? panel.hidden : force;
    panel.hidden = !show;
    // Deriving a chapter list from the spine means opening every section of
    // the book, so it happens the first time someone asks to see the list --
    // never on the way to the first page.
    if (show && _kind === "epub") ensureSpineToc();
    if (show) wakeChrome();
  }

  function setFont(delta) {
    var size = delta === "reset"
      ? 100
      : Math.max(70, Math.min(220, parseInt(_prefs.reader_font, 10) + delta));
    savePref("reader_font", size);
    syncSheet();
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
    syncSheet();
  }

  function setFace(face) {
    savePref("reader_face", face);
    if (_kind === "epub") applyEpubStyles();
    syncSheet();
  }

  function setLead(lead) {
    savePref("reader_lead", lead);
    if (_kind === "epub") applyEpubStyles();
    syncSheet();
  }

  function setWidth(width) {
    savePref("reader_width", width);
    applyLayout();
    // epub.js lays the text out in columns sized to its container, so a new
    // measure only takes effect once it re-measures.
    if (_rendition && _rendition.resize) { try { _rendition.resize(); } catch (e) {} }
    syncSheet();
  }

  function setFlow(flow) {
    savePref("reader_flow", flow);
    syncSheet();
    if (_kind === "epub" && _rendition) {
      _rendition.flow(flow === "scrolled" ? "scrolled-doc" : "paginated");
      applyEpubStyles();
    }
  }

  function onKey(ev) {
    if (!_open) return;
    wakeChrome();
    var target = ev.target;
    if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
    switch (ev.key) {
      case "Escape":
        if (!$id("readerSheet").hidden) { toggleSheet(false); break; }
        if (!$id("readerToc").hidden) { toggleToc(false); break; }
        closeReader();
        break;
      case "ArrowRight": case "PageDown": next(); break;
      case "ArrowLeft":  case "PageUp":   prev(); break;
      case "+": case "=": setFont(10); break;
      case "-": setFont(-10); break;
      case "b": case "B": toggleBookmark(); break;
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

    var clicks = [
      ["readerClose", closeReader],
      ["readerPrev", prev], ["readerNext", next],
      ["readerPrevBtn", prev], ["readerNextBtn", next],
      ["readerTocBtn", function () { toggleSheet(false); toggleToc(); }],
      ["readerTocClose", function () { toggleToc(false); }],
      ["readerMarkBtn", toggleBookmark],
      ["readerSheetBtn", function () { toggleToc(false); toggleSheet(); }],
      ["readerFullscreen", toggleFullscreen]
    ];
    clicks.forEach(function (pair) {
      var el = $id(pair[0]);
      if (el) el.addEventListener("click", function (ev) { ev.stopPropagation(); pair[1](); });
    });

    // Every segmented control in the settings sheet, wired from its data
    // attribute so adding an option is a line of HTML and nothing else.
    var groups = [
      ["#readerFont [data-font]", "data-font", setFace],
      ["#readerLead [data-lead]", "data-lead", setLead],
      ["#readerWidth [data-width]", "data-width", setWidth],
      ["#readerThemes [data-theme]", "data-theme", setTheme],
      ["#readerFlow [data-flow]", "data-flow", setFlow]
    ];
    groups.forEach(function (g) {
      Array.prototype.forEach.call(document.querySelectorAll(g[0]), function (b) {
        b.addEventListener("click", function () { g[2](b.getAttribute(g[1])); });
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll("#readerSize [data-size]"), function (b) {
      var raw = b.getAttribute("data-size");
      b.addEventListener("click", function () {
        setFont(raw === "reset" ? "reset" : parseInt(raw, 10));
      });
    });

    var overlay = $id("readerOverlay");
    if (overlay) {
      overlay.addEventListener("click", function (ev) {
        // A click on the backdrop closes; a click anywhere inside first
        // dismisses an open panel, because that is what the reader means by
        // "somewhere else".
        if (ev.target === overlay) { closeReader(); return; }
        var sheet = $id("readerSheet");
        if (sheet && !sheet.hidden && !sheet.contains(ev.target)) toggleSheet(false);
      });
      ["mousemove", "pointerdown", "wheel"].forEach(function (evt) {
        overlay.addEventListener(evt, wakeChrome, { passive: true });
      });
    }
  }

  function reset() {
    stopSaveTimer();
    stopConvertPolling();
    if (_idleTimer) { clearTimeout(_idleTimer); _idleTimer = null; }
    _tocEntries = [];
    _tocScanned = false;
    _bookmarks = [];
    _chapterLabel = "";
    toggleSheet(false);
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
    var chapters = $id("readerChapters");
    if (chapters) chapters.innerHTML = "";
    var chaptersHead = $id("readerChaptersHead");
    if (chaptersHead) chaptersHead.hidden = true;
    renderBookmarks();
    syncMarkState();
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
    applyTheme();
    applyLayout();
    syncSheet();
    wakeChrome();
    loadBookmarks();
    // The chapter list and the flow switch only mean something for reflowable
    // text; a PDF has fixed pages and its own outline.
    var epubOnly = document.querySelectorAll("[data-reader-epub-only]");
    Array.prototype.forEach.call(epubOnly, function (el) { el.hidden = _kind !== "epub"; });

    if (CONVERTIBLE.indexOf(ext) !== -1) {
      // No browser renders Mobipocket. The server turns it into an EPUB once
      // and keeps it; until then the reader says what it is waiting for
      // instead of showing an empty frame.
      convertThenOpen(path, ext);
      return;
    }
    if (ext !== "pdf" && ext !== "epub") {
      setStatus(
        esc(tr("Dieses Format (" + ext.toUpperCase() + ") lässt sich nicht öffnen.",
               "This format (" + ext.toUpperCase() + ") cannot be opened.")) +
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

  // ---- MOBI / AZW3 / AZW: convert on the server, then read the EPUB ----

  var CONVERT_POLL_MS = 1200;
  var CONVERT_MAX_WAIT_MS = 180000;   // a 12 MB AZW3 takes under a second; this is the give-up line
  var _convertTimer = null;

  function stopConvertPolling() {
    if (_convertTimer) { clearTimeout(_convertTimer); _convertTimer = null; }
  }

  function convertThenOpen(path, ext) {
    var started = Date.now();
    setStatus(
      '<div class="mfr-spinner"></div>' +
      esc(tr(ext.toUpperCase() + " wird für den Reader vorbereitet…",
             "Preparing the " + ext.toUpperCase() + " for the reader…")) +
      '<div class="mfr-status-note">' +
      esc(tr("Das passiert nur beim ersten Öffnen.", "This happens only the first time.")) +
      "</div>");

    function fail(reason) {
      var why = reason === "too_large"
        ? tr("Die Datei ist zu groß für die Umwandlung.", "The file is too large to convert.")
        : tr("Die Datei konnte nicht umgewandelt werden.", "The file could not be converted.");
      setStatus(esc(why) +
        ' <a class="mfr-download" href="/api/library/book/file?path=' + encodeURIComponent(path) +
        '" download>' + esc(tr("Herunterladen", "Download")) + "</a>", true);
    }

    function poll() {
      if (!_open) return;
      fetch("/api/library/book/convert?path=" + encodeURIComponent(path))
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (!_open) return;
          if (res.ready && res.key) {
            _kind = "epub";
            loadPosition(_bookKey).then(function (pos) {
              if (!_open) return;
              openEpub("/api/library/book/converted/" + encodeURIComponent(res.key) + ".epub",
                       pos.location || "");
            });
            return;
          }
          if (res.failed) { fail(res.reason); return; }
          if (Date.now() - started > CONVERT_MAX_WAIT_MS) {
            fail("timeout");
            return;
          }
          _convertTimer = setTimeout(poll, CONVERT_POLL_MS);
        })
        .catch(function () { fail("network"); });
    }
    poll();
  }

  window.closeReader = function () {
    if (!_open) return;
    savePosition();
    _open = false;
    reset();
    var overlay = $id("readerOverlay");
    if (overlay) overlay.style.display = "none";
    var shell = $id("readerShell");
    if (shell) shell.classList.remove("is-idle");
    document.body.classList.remove("reader-open");
    if (document.fullscreenElement && document.exitFullscreen) {
      document.exitFullscreen().catch(function () {});
    }
  };

  // Namespaced surface for modules, mirroring window.MFPlayer.
  window.MFReader = {
    open: function (o) { window.openReader(o.path, o.ext, o.title, o.bookKey); },
    close: function () { window.closeReader(); },
    isOpen: function () { return _open; },
    // Everything a module needs to say "you are on page 40 of Dune" without
    // reaching into the reader's internals.
    getState: function () {
      return {
        open: _open, kind: _kind, bookKey: _bookKey, title: _title,
        location: currentLocation(), percent: _percent, chapter: _chapterLabel
      };
    },
    bookmarks: function () { return _bookmarks.slice(); },
    toggleBookmark: function () { toggleBookmark(); }
  };
})();
