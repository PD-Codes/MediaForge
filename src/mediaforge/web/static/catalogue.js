// Catalogue page — the full A-Z list of a source, for bulk selection.
//
// Two things shape everything in here:
//
//   1. SIZE. SerienStream lists ~10.8k series in one response. Rendering that
//      as DOM freezes the browser for seconds on every keystroke, so the list
//      is virtualised: one absolutely-positioned slice of rows inside a spacer
//      that carries the full height. Only what fits on screen exists.
//
//   2. The SELECTION has to survive everything — filtering, switching source,
//      opening a details modal, reloading the page. It therefore lives in a
//      Set keyed by url (never by row index, which changes with every filter)
//      and is mirrored into localStorage.
//
// The download itself is not done here: the server expands the selection into
// episodes in a background job (web/catalogue_worker.py), because doing it in
// the browser would mean hundreds of requests and lose everything on a reload.

(function () {
  "use strict";

  const T = window.CAT_I18N || {};
  const t = (key, fallback) => T[key] || fallback || key;

  const ROW_HEIGHT_DESKTOP = 44;
  const ROW_HEIGHT_MOBILE = 54;
  const OVERSCAN = 8;             // rows rendered above/below the viewport
  const WARN_THRESHOLD = 25;      // selection size that earns the warning
  // Below this many rows the A-Z rail is hidden: a list you can flick through
  // in one gesture does not need an index, and an index over twelve entries
  // is mostly empty letters.
  const RAIL_MIN_ROWS = 60;
  const STORE_KEY = "mf-catalogue-selection";
  // View preferences (sort direction, which chips are off). A separate key
  // from the selection on purpose: clearing a selection is a frequent,
  // deliberate act and must not also reset how the page is set up.
  const PREFS_KEY = "mf-catalogue-prefs";

  const el = (id) => document.getElementById(id);
  const viewport = el("catViewport");
  if (!viewport) return;          // not this page

  const spacer = el("catSpacer");
  const rowsHost = el("catRows");
  const searchInput = el("catSearch");
  const rail = el("catRail");
  const railBubble = el("catRailBubble");

  let sources = [];               // [{id, label, enabled, color}]
  // ONE merged list across every catalogue, sorted by title. A title that
  // both sites carry stays TWO entries on purpose -- they are two different
  // pages with different languages and different episode counts, and folding
  // them together would silently pick one for the user. The source label on
  // each row is what tells them apart.
  let entries = [];               // [{title, url, alt, source}]
  let filtered = [];
  let selection = new Set();      // urls
  let queuedUrls = new Set();
  let syncUrls = new Set();
  let offSources = new Set();     // source ids the chips have narrowed away
  // Status chips follow the same model as the source chips: a chip is ON by
  // default and turning it off hides the rows in that category. A row in none
  // of the categories is always visible -- these narrow the list, they never
  // define it.
  let offStatus = new Set();      // subset of "library" | "queued" | "sync"
  let onlySelected = false;       // show nothing but what is marked
  let sortDir = "asc";            // "asc" | "desc"
  let letterFirst = {};           // letter -> first index into `filtered`
  let railLetters = [];           // letters in rail order, "#" for the rest
  let activeLetter = "";
  let jobPoll = null;
  let loadedCount = 0;            // catalogues that answered

  // Stable per-source colour for the row dot and the chip dot, taken from the
  // same palette the home feed uses so a source looks the same wherever it
  // appears. The SERVER's colour wins when it has one: built-ins carry theirs
  // in catalogue.py and a third-party module supplies its own through
  // register_catalogue(color=...) -- this map is only the fallback for a
  // source that predates that, plus a last-resort neutral.
  const SOURCE_COLORS = {
    aniworld: "#6aa9ff",
    sto: "#8b7dff",
    filmpalast: "#ffb454",
    megakino: "#4ade80",
    filmo: "#e8914a",
    nineanime: "#f0a020",
    aniwaves: "#38bdf8",
  };
  const colorOf = (id) => {
    const known = sources.find((s) => s.id === id);
    return (known && known.color) || SOURCE_COLORS[id] || "#8b8bff";
  };
  const labelOf = (id) => (sources.find((s) => s.id === id) || {}).label || id;

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  const rowHeight = () => (window.matchMedia("(max-width: 720px)").matches
    ? ROW_HEIGHT_MOBILE : ROW_HEIGHT_DESKTOP);

  /** Is this already in the library? One place, every index we have.
   *
   * Three answers in order of how much they can be trusted:
   *
   *   1. The entry's own TMDB/IMDb id against the Plex/Jellyfin index. An
   *      exact match -- this is the whole reason the ids are resolved (see
   *      web/catalogue_ids.py). Only some entries have one; the backfill
   *      fills them in over hours and the rest fall through.
   *   2. The normalised TITLE against that same index.
   *   3. The normalised title against the download-folder list.
   *
   * 2 and 3 are app.js's own functions, picked between exactly the way
   * addDownloadedBadge() picks between them -- this page must not grow a
   * second opinion about what "downloaded" means. All of it is behind
   * `typeof` guards because app.js is not guaranteed to be on the page.
   */
  function inLibrary(title, entry) {
    if (!title && !entry) return false;
    try {
      if (typeof mediascanActive !== "undefined" && mediascanActive) {
        if (entry && entry.tmdb_id &&
            typeof window._isDownloadedByTmdb === "function" &&
            window._isDownloadedByTmdb(entry.tmdb_id)) {
          return true;
        }
        if (entry && entry.imdb_id && typeof mediascanImdbIds !== "undefined" &&
            mediascanImdbIds.has(String(entry.imdb_id))) {
          return true;
        }
        if (typeof window._isDownloadedByTitle === "function") {
          return window._isDownloadedByTitle(title);
        }
      }
    } catch (e) { /* app.js absent -- fall through to the folder list */ }
    return typeof window.isDownloaded === "function" && window.isDownloaded(title);
  }

  // ── Selection persistence ────────────────────────────────────────────────
  function loadSelection() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (raw) selection = new Set(JSON.parse(raw) || []);
    } catch (e) { selection = new Set(); }
  }

  function saveSelection() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(Array.from(selection)));
    } catch (e) { /* private mode / quota — the in-memory Set still works */ }
  }

  // ── View preferences ─────────────────────────────────────────────────────
  // Sort direction and the chip state survive a reload, because they describe
  // how this user reads the catalogue rather than what they are doing right
  // now. `onlySelected` is the exception: it is restored only when there is
  // still a selection to show, otherwise the page would come up empty with no
  // obvious reason why.
  function loadPrefs() {
    try {
      const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") || {};
      if (raw.sort === "desc") sortDir = "desc";
      if (Array.isArray(raw.offSources)) offSources = new Set(raw.offSources);
      if (Array.isArray(raw.offStatus)) offStatus = new Set(raw.offStatus);
      onlySelected = !!raw.onlySelected && selection.size > 0;
    } catch (e) { /* the defaults above are a perfectly good page */ }
  }

  function savePrefs() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({
        sort: sortDir,
        offSources: Array.from(offSources),
        offStatus: Array.from(offStatus),
        onlySelected: onlySelected,
      }));
    } catch (e) { /* private mode / quota -- the in-memory state still works */ }
  }

  // ── Data ─────────────────────────────────────────────────────────────────
  async function loadSources() {
    const resp = await fetch("/api/catalogue/sources");
    const data = await resp.json();
    sources = data.sources || [];
    // Resolved after the list is in place: colorOf() reads it back out.
    sources.forEach((s) => { s.color = colorOf(s.id); });
    renderChips();
    await loadAll(false);
  }

  async function loadState() {
    try {
      const resp = await fetch("/api/catalogue/state");
      const data = await resp.json();
      queuedUrls = new Set(data.queued || []);
      syncUrls = new Set(data.autosync || []);
    } catch (e) { /* badges are a nicety, never a blocker */ }
  }

  /** Fetch every enabled catalogue and merge them into one sorted list. */
  async function loadAll(force) {
    entries = [];
    loadedCount = 0;
    applyFilter();
    showMessage(t("loading", "Loading…"));

    const enabled = sources.filter((s) => s.enabled);
    if (!enabled.length) { showMessage(t("disabled", "No source is enabled.")); return; }

    const results = await Promise.all(enabled.map((s) =>
      fetch("/api/catalogue/" + encodeURIComponent(s.id) + (force ? "?refresh=1" : ""))
        .then((r) => r.json().then((d) => ({ ok: r.ok, id: s.id, data: d })))
        .catch(() => ({ ok: false, id: s.id, data: {} }))));

    const merged = [];
    results.forEach((res) => {
      if (!res.ok || !res.data || !res.data.entries) return;
      loadedCount += 1;
      res.data.entries.forEach((e) => {
        // tmdb_id/imdb_id come straight from the stored catalogue and are what
        // makes the library check exact rather than a title guess. Empty for
        // any entry the backfill has not reached yet.
        merged.push({
          title: e.title, url: e.url, alt: e.alt || "", source: res.id,
          tmdb_id: e.tmdb_id || "", imdb_id: e.imdb_id || "",
        });
      });
    });

    if (!merged.length) { showRetry(); return; }

    // Sorted by title, then by source, so the two rows of a title both sites
    // carry sit next to each other instead of ~6000 apart.
    merged.sort((a, b) => {
      const cmp = a.title.localeCompare(b.title, undefined, { sensitivity: "base" });
      return cmp !== 0 ? cmp : a.source.localeCompare(b.source);
    });
    entries = merged;
    applyFilter();
  }

  function showMessage(text) {
    rowsHost.innerHTML = '<div class="cat-empty">' + esc(text) + "</div>";
    spacer.style.height = "auto";
    rowsHost.style.transform = "";
  }

  function showRetry() {
    rowsHost.innerHTML = '<div class="cat-empty">' + esc(t("failed", "Failed.")) +
      ' <button type="button" class="cat-tool-btn" id="catRetry">' +
      esc(t("retry", "Try again")) + "</button></div>";
    spacer.style.height = "auto";
    const btn = el("catRetry");
    if (btn) btn.addEventListener("click", () => loadAll(true));
  }

  // ── Source chips ─────────────────────────────────────────────────────────
  // These FILTER the merged list, they do not switch between lists. A chip is
  // on by default; turning one off hides that source's rows without touching
  // the selection, so a row that is marked and then hidden stays marked.
  function renderChips() {
    const host = el("catSourceChips");
    host.innerHTML = "";
    sources.forEach((s) => {
      const btn = document.createElement("button");
      btn.type = "button";
      const on = s.enabled && !offSources.has(s.id);
      btn.className = "cat-chip" + (on ? " is-on" : "") + (s.enabled ? "" : " is-off");
      btn.innerHTML = '<span class="cat-chip-dot" style="background:' + s.color + '"></span>';
      btn.appendChild(document.createTextNode(s.label));
      if (!s.enabled) {
        btn.title = t("disabled", "Switched off in Settings.");
      } else {
        btn.setAttribute("aria-pressed", on ? "true" : "false");
        btn.addEventListener("click", () => {
          if (offSources.has(s.id)) offSources.delete(s.id); else offSources.add(s.id);
          savePrefs();
          renderChips();
          viewport.scrollTop = 0;
          applyFilter();
        });
      }
      host.appendChild(btn);
    });
  }

  // ── Status chips ─────────────────────────────────────────────────────────
  // The three badges a row can carry, as filters. Hiding "In library" is what
  // turns an eleven-thousand-row list into the few hundred titles you do NOT
  // have yet, which is the state most bulk selections start from.
  const STATUS_CHIPS = [
    { id: "library", key: "in_library", fallback: "In library", cls: "cat-chip--library" },
    { id: "queued", key: "queued", fallback: "Queued", cls: "cat-chip--queued" },
    { id: "sync", key: "syncing", fallback: "Auto-Sync", cls: "cat-chip--sync" },
  ];

  function renderStatusChips() {
    const host = el("catStatusChips");
    if (!host) return;
    host.innerHTML = "";
    STATUS_CHIPS.forEach((s) => {
      const on = !offStatus.has(s.id);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cat-chip cat-chip--status " + s.cls + (on ? " is-on" : "");
      btn.textContent = t(s.key, s.fallback);
      btn.title = t("chip_hint", "Show or hide these entries");
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.addEventListener("click", () => {
        if (offStatus.has(s.id)) offStatus.delete(s.id); else offStatus.add(s.id);
        savePrefs();
        renderStatusChips();
        viewport.scrollTop = 0;
        applyFilter();
      });
      host.appendChild(btn);
    });

    const sel = document.createElement("button");
    sel.type = "button";
    sel.className = "cat-chip cat-chip--status cat-chip--only" + (onlySelected ? " is-on" : "");
    sel.textContent = t("only_selection", "Only selection");
    sel.title = t("only_selection_hint", "Show only what is marked");
    sel.setAttribute("aria-pressed", onlySelected ? "true" : "false");
    sel.addEventListener("click", () => {
      onlySelected = !onlySelected;
      savePrefs();
      renderStatusChips();
      viewport.scrollTop = 0;
      applyFilter();
    });
    host.appendChild(sel);
  }

  function renderSortButton() {
    const btn = el("catSort");
    if (!btn) return;
    btn.textContent = sortDir === "asc" ? t("sort_az", "A\u2013Z") : t("sort_za", "Z\u2013A");
    btn.setAttribute("aria-label", t("sort_label", "Sort order"));
  }

  // ── Filter ───────────────────────────────────────────────────────────────
  // Matches the visible title AND the site's own alternate titles, which the
  // catalogue carries for exactly this reason: "Shingeki no Kyojin" should
  // find "Attack on Titan".
  function applyFilter() {
    const term = (searchInput.value || "").trim().toLowerCase();
    const hideLibrary = offStatus.has("library");
    const hideQueued = offStatus.has("queued");
    const hideSync = offStatus.has("sync");
    // app.js owns the downloaded-folder list; on a page where it has not
    // loaded, "in library" is simply unknown and must not hide anything.
    const canCheckLibrary = hideLibrary && (
      typeof window.isDownloaded === "function" ||
      typeof window._isDownloadedByTitle === "function");

    filtered = entries.filter((e) => {
      if (offSources.has(e.source)) return false;
      if (onlySelected && !selection.has(e.url)) return false;
      if (hideQueued && queuedUrls.has(e.url)) return false;
      if (hideSync && syncUrls.has(e.url)) return false;
      if (canCheckLibrary && inLibrary(e.title, e)) return false;
      if (!term) return true;
      return e.title.toLowerCase().indexOf(term) !== -1 ||
        (e.alt && e.alt.indexOf(term) !== -1);
    });
    // `entries` is sorted A-Z once at load; reversing the filtered slice is
    // cheaper and keeps the two orders guaranteed to be exact mirrors.
    if (sortDir === "desc") filtered.reverse();
    buildLetterIndex();
    updateCount();
    render();
  }

  // ── A-Z index ────────────────────────────────────────────────────────────
  // Diacritics are folded, so "Ärzte" indexes under A rather than under "#" --
  // otherwise the bucket that is supposed to hold the odd numeric title also
  // collects every umlaut on the page.
  function letterOf(title) {
    let s = String(title || "").trim();
    if (s.normalize) s = s.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    const ch = (s.charAt(0) || "").toUpperCase();
    return (ch >= "A" && ch <= "Z") ? ch : "#";
  }

  function buildLetterIndex() {
    letterFirst = {};
    for (let i = 0; i < filtered.length; i++) {
      const letter = letterOf(filtered[i].title);
      if (letterFirst[letter] === undefined) letterFirst[letter] = i;
    }
    const alpha = [];
    for (let c = 65; c <= 90; c++) alpha.push(String.fromCharCode(c));
    // "#" leads in A-Z order and trails in Z-A order, matching where
    // localeCompare actually puts those titles in the list.
    railLetters = sortDir === "desc"
      ? alpha.slice().reverse().concat(["#"])
      : ["#"].concat(alpha);
    renderRail();
  }

  function renderRail() {
    if (!rail) return;
    if (filtered.length < RAIL_MIN_ROWS) {
      rail.innerHTML = "";
      rail.hidden = true;
      return;
    }
    rail.hidden = false;
    rail.innerHTML = railLetters.map((letter) =>
      '<span class="cat-rail-letter' +
      (letterFirst[letter] === undefined ? " is-empty" : "") +
      (letter === activeLetter ? " is-active" : "") +
      '" data-letter="' + esc(letter) + '">' + esc(letter) + "</span>").join("");
  }

  function setActiveLetter(letter) {
    if (letter === activeLetter) return;
    activeLetter = letter;
    if (!rail || rail.hidden) return;
    const nodes = rail.querySelectorAll(".cat-rail-letter");
    for (let i = 0; i < nodes.length; i++) {
      nodes[i].classList.toggle("is-active",
        nodes[i].getAttribute("data-letter") === letter);
    }
  }

  // A letter with no entries still gets a hit target: dragging past it jumps
  // to the next letter that DOES exist, so the rail never feels dead.
  function jumpToLetter(letter) {
    const start = railLetters.indexOf(letter);
    if (start < 0) return;
    for (let i = start; i < railLetters.length; i++) {
      const target = letterFirst[railLetters[i]];
      if (target !== undefined) {
        viewport.scrollTop = target * rowHeight();
        setActiveLetter(railLetters[i]);
        return;
      }
    }
  }

  // Geometry rather than elementFromPoint: the letters are evenly distributed,
  // and a finger dragging over the rail must resolve even when it strays a few
  // pixels outside it.
  function letterAtY(clientY) {
    const rect = rail.getBoundingClientRect();
    if (!rect.height || !railLetters.length) return "";
    const rel = Math.min(rect.height - 1, Math.max(0, clientY - rect.top));
    return railLetters[Math.floor(rel / (rect.height / railLetters.length))] || "";
  }

  function showRailBubble(letter) {
    if (!railBubble || !rail) return;
    // parentElement, NOT offsetParent: the bubble is display:none until it is
    // shown, and a display:none element has no offsetParent -- reading it
    // first meant the bubble never appeared on the first drag at all. Its
    // parent (.cat-list-wrap) is the positioned ancestor either way.
    const host = railBubble.parentElement;
    if (!host) return;
    railBubble.textContent = letter;
    railBubble.hidden = false;          // measure it laid out, not hidden
    const railRect = rail.getBoundingClientRect();
    const hostRect = host.getBoundingClientRect();
    const step = railRect.height / railLetters.length;
    const idx = railLetters.indexOf(letter);
    railBubble.style.top =
      (railRect.top - hostRect.top + step * (idx + 0.5)) + "px";
  }

  function hideRailBubble() {
    if (railBubble) railBubble.hidden = true;
  }

  if (rail) {
    let dragging = false;
    const handle = (clientY) => {
      const letter = letterAtY(clientY);
      if (!letter) return;
      showRailBubble(letter);
      jumpToLetter(letter);
    };
    rail.addEventListener("pointerdown", (ev) => {
      dragging = true;
      // Pointer capture is what makes the drag keep working once the finger
      // leaves the 22px-wide rail, which on a phone it immediately does.
      try { rail.setPointerCapture(ev.pointerId); } catch (e) { /* older engines */ }
      handle(ev.clientY);
      ev.preventDefault();
    });
    rail.addEventListener("pointermove", (ev) => {
      if (!dragging) return;
      handle(ev.clientY);
      ev.preventDefault();
    });
    const endDrag = () => { dragging = false; hideRailBubble(); };
    rail.addEventListener("pointerup", endDrag);
    rail.addEventListener("pointercancel", endDrag);
    rail.addEventListener("pointerleave", () => { if (!dragging) hideRailBubble(); });
  }

  function updateCount() {
    el("catCount").textContent = entries.length
      ? filtered.length + " " + t("of", "of") + " " + entries.length + " " + t("entries", "entries")
      : "";
    const n = selection.size;
    el("catSelCount").textContent = n + " " + t("selected", "selected");
    updateWarning(n);
    syncActionsBar(n);
  }

  // The warning the user asked for: informative, never blocking. There is no
  // upper limit on a selection -- marking the whole catalogue is a legitimate
  // thing to want, so this says what it will cost instead of refusing it.
  // The count is prefixed rather than interpolated into the translated string,
  // see the note in catalogue.html.
  function updateWarning(n) {
    const warn = el("catWarn");
    if (n < WARN_THRESHOLD) { warn.style.display = "none"; return; }
    warn.textContent = n + " " + t("warn_many", "series selected; this will take a while.");
    warn.style.display = "";
  }

  // ── Virtualised rendering ────────────────────────────────────────────────
  function render() {
    const h = rowHeight();
    if (!filtered.length) {
      showMessage(entries.length ? t("nothing", "Nothing matches.") : t("loading", "Loading…"));
      return;
    }
    spacer.style.height = (filtered.length * h) + "px";

    const start = Math.max(0, Math.floor(viewport.scrollTop / h) - OVERSCAN);
    const visible = Math.ceil(viewport.clientHeight / h) + OVERSCAN * 2;
    const end = Math.min(filtered.length, start + visible);

    let html = "";
    for (let i = start; i < end; i++) {
      const e = filtered[i];
      const url = e.url;
      const isSel = selection.has(url);
      let badges = "";
      // Decided client-side against the index app.js already holds; asking
      // the server per row would be eleven thousand requests.
      if (inLibrary(e.title, e)) {
        badges += '<span class="cat-badge cat-badge--library">' + esc(t("in_library", "In library")) + "</span>";
      }
      if (queuedUrls.has(url)) {
        badges += '<span class="cat-badge cat-badge--queued">' + esc(t("queued", "Queued")) + "</span>";
      }
      if (syncUrls.has(url)) {
        badges += '<span class="cat-badge cat-badge--sync">' + esc(t("syncing", "Auto-Sync")) + "</span>";
      }
      // Title and meta travel in one .cat-row-main box so a phone can stack
      // them without the grid-area gymnastics the first version needed.
      // The source label is not decoration: the list is merged, and a title
      // both sites carry is two rows that are otherwise identical.
      html += '<div class="cat-row' + (isSel ? " is-selected" : "") + '" data-url="' + esc(url) + '">' +
        '<input type="checkbox" class="chb-main" ' + (isSel ? "checked" : "") + ' tabindex="-1">' +
        '<div class="cat-row-main">' +
        '<span class="cat-row-title">' + esc(e.title) + "</span>" +
        '<span class="cat-row-meta">' +
        '<span class="cat-src"><span class="cat-src-dot" style="background:' +
        esc(colorOf(e.source)) + '"></span>' +
        '<span class="cat-src-label">' + esc(labelOf(e.source)) + "</span></span>" +
        '<span class="cat-badges">' + badges + "</span>" +
        "</span></div>" +
        '<button type="button" class="cat-details-btn" data-details="' + esc(url) + '">' +
        esc(t("details", "Details")) + "</button></div>";
    }
    rowsHost.innerHTML = html;
    rowsHost.style.transform = "translateY(" + (start * h) + "px)";

    // Keep the rail in step with where the list actually is. Rounded rather
    // than floored: at a boundary the letter the user reads at the top of the
    // viewport is the one that should light up.
    const anchor = filtered[Math.min(filtered.length - 1,
      Math.max(0, Math.round(viewport.scrollTop / h)))];
    setActiveLetter(anchor ? letterOf(anchor.title) : "");
  }

  // ── Interaction ──────────────────────────────────────────────────────────
  viewport.addEventListener("scroll", () => {
    // rAF rather than a debounce: the list has to keep up WITH the scroll, and
    // a trailing debounce shows blank rows while the finger is still moving.
    if (viewport._raf) return;
    viewport._raf = requestAnimationFrame(() => { viewport._raf = null; render(); });
  });

  // Throttled like the scroll handler: a resize fires in a burst (and on
  // phones on every address-bar nudge), and each render walks the slice.
  let resizeRaf = null;
  window.addEventListener("resize", () => {
    if (resizeRaf) return;
    resizeRaf = requestAnimationFrame(() => { resizeRaf = null; render(); });
  });

  rowsHost.addEventListener("click", (ev) => {
    const detailsBtn = ev.target.closest("[data-details]");
    if (detailsBtn) {
      ev.stopPropagation();
      openDetails(detailsBtn.getAttribute("data-details"));
      return;
    }
    const row = ev.target.closest(".cat-row");
    if (!row) return;
    toggle(row.getAttribute("data-url"));
  });

  function toggle(url) {
    if (!url) return;
    if (selection.has(url)) selection.delete(url); else selection.add(url);
    saveSelection();
    // With "Only selection" active the row that was just unmarked no longer
    // belongs in the list at all, so this is a filter change, not a repaint.
    if (onlySelected) { applyFilter(); return; }
    updateCount();
    render();
  }

  searchInput.addEventListener("input", () => {
    viewport.scrollTop = 0;
    applyFilter();
  });

  el("catSelectAll").addEventListener("click", () => {
    // "Shown", not "all": with a filter active this is the useful action, and
    // without one it is still bounded by what the source has.
    filtered.forEach((e) => selection.add(e.url));
    saveSelection();
    updateCount();
    render();
  });

  el("catClear").addEventListener("click", () => {
    selection.clear();
    saveSelection();
    // Leaving "Only selection" on here would answer "clear" with an empty
    // list and no visible reason, so clearing switches it back off.
    if (onlySelected) {
      onlySelected = false;
      savePrefs();
      renderStatusChips();
    }
    applyFilter();
  });

  el("catRefresh").addEventListener("click", () => {
    // Asks the STORE for a refetch and follows it through the status strip;
    // the list on screen stays usable the whole time. loadAll(true) alone
    // would only re-request the same stored rows.
    fetch("/api/catalogue/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    }).then(() => pollStatus()).catch(() => loadAll(true));
  });

  const sortBtn = el("catSort");
  if (sortBtn) {
    sortBtn.addEventListener("click", () => {
      sortDir = sortDir === "asc" ? "desc" : "asc";
      savePrefs();
      renderSortButton();
      viewport.scrollTop = 0;
      applyFilter();
    });
  }

  // ── Details modal ────────────────────────────────────────────────────────
  // Deliberately its own modal rather than the start page's download modal:
  // that one owns a language/provider selection of its own and would fight
  // with the selection bar. Opening this one never changes the selection.
  let modalUrl = "";

  window.catCloseModal = function () {
    el("catModal").style.display = "none";
    modalUrl = "";
    setModalBackdrop("");
    // The list behind the modal has its own scroll container, so closing has
    // to give the page AND the body their scrolling back.
    document.body.classList.remove("cat-modal-open");
  };

  // ── Modal backdrop ───────────────────────────────────────────────────────
  // Two sources, in order: TMDB's landscape still when CineInfo can supply one
  // (same option the download modal honours -- a user who switched backdrops
  // off there does not want them here either), and the title's own poster,
  // blurred, when it cannot. A 2:3 poster stretched across a 16:5 strip is
  // unreadable sharp and perfectly good out of focus.
  let backdropToken = 0;

  function setModalBackdrop(url, isPoster) {
    const node = el("catModalBackdrop");
    const card = el("catModalCard");
    if (!node || !card) return;
    const token = ++backdropToken;
    if (!url) {
      node.style.backgroundImage = "";
      node.classList.remove("is-on", "is-poster");
      card.classList.remove("has-backdrop");
      return;
    }
    // Preloaded: flipping the class before the image decodes shows an empty
    // band first and then pops the picture in.
    const img = new Image();
    img.onload = function () {
      if (token !== backdropToken) return;   // a newer modal won
      node.style.backgroundImage = 'url("' + String(url).replace(/"/g, "%22") + '")';
      node.classList.toggle("is-poster", !!isPoster);
      node.classList.add("is-on");
      card.classList.add("has-backdrop");
    };
    img.src = url;
  }

  // app.js owns the CineInfo settings and the image proxy; both are optional
  // here, and without them the poster fallback still gives the modal a header.
  async function upgradeBackdropFromTmdb(title, forUrl) {
    const settings = window.cineinfoSettings;
    if (!settings || !settings.tmdb_api_key) return;
    if ((settings.show_backdrop == null ? "1" : settings.show_backdrop) === "0") return;
    try {
      const resp = await fetch("/api/tmdb/info?title=" + encodeURIComponent(title));
      if (!resp.ok) return;
      const data = await resp.json();
      if (modalUrl !== forUrl) return;       // a different title was opened
      const path = (data && data.raw_details && data.raw_details.backdrop_path) || "";
      if (!path) return;
      const full = "https://image.tmdb.org/t/p/w780" + path;
      setModalBackdrop(typeof window.proxyImg === "function" ? window.proxyImg(full) : full, false);
    } catch (e) { /* the poster fallback is already on screen */ }
  }

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && el("catModal").style.display !== "none") window.catCloseModal();
  });

  el("catModalToggle").addEventListener("click", () => {
    toggle(modalUrl);
    syncModalToggle();
  });

  function syncModalToggle() {
    const btn = el("catModalToggle");
    const marked = selection.has(modalUrl);
    // A leading check / plus makes the CURRENT state readable at a glance;
    // the label alone reads as an instruction either way round.
    btn.textContent = (marked ? "\u2713 " : "+ ") + (marked
      ? t("remove_sel", "Remove from selection")
      : t("add_sel", "Add to selection"));
    btn.classList.toggle("btn-primary", !marked);
    btn.classList.toggle("cat-modal-toggle--on", marked);
    btn.setAttribute("aria-pressed", marked ? "true" : "false");

    // The action bar carries the running total, and it is behind the modal —
    // without this, marking something here has no visible consequence at all.
    const info = el("catModalSelInfo");
    if (info) {
      info.textContent = selection.size
        ? selection.size + " " + t("selected", "selected")
        : "";
    }
  }

  // ── Loading state ────────────────────────────────────────────────────────
  // Shaped like the answer, not like a spinner: poster block, genre chips,
  // four description lines, a season head and five episode rows. That is what
  // stops the dialog resizing under the cursor the moment the data lands --
  // and with the modal centred, a "Loading…" line would put the card at a
  // third of its final height and then jump.
  //
  // Gated on body.skeleton-loader exactly like the download modal (see
  // openSeries() in app.js): it is an appearance setting, and a user who
  // turned shimmer off gets the plain line instead.
  function loadingMarkup() {
    if (!document.body.classList.contains("skeleton-loader")) {
      return '<div class="cat-empty">' + esc(t("loading", "Loading…")) + "</div>";
    }
    const line = (w) => '<div class="cat-sk cat-sk-line" style="width:' + w + '"></div>';
    let chips = "";
    for (let i = 0; i < 3; i++) chips += '<span class="cat-sk cat-sk-chip"></span>';
    let eps = "";
    for (let i = 0; i < 5; i++) {
      eps += '<div class="cat-ep"><span class="cat-sk cat-sk-num"></span>' +
        '<span class="cat-sk cat-sk-line" style="width:' + (72 - i * 8) + '%"></span></div>';
    }
    return '<div class="cat-modal-body">' +
      '<div class="cat-modal-poster cat-sk"></div>' +
      '<div class="cat-modal-info">' +
      '<div class="cat-modal-genres">' + chips + "</div>" +
      line("100%") + line("97%") + line("92%") + line("58%") +
      "</div></div>" +
      '<div class="cat-eplist">' +
      '<div class="cat-sk cat-sk-head"></div>' + eps +
      "</div>";
  }

  async function openDetails(url) {
    modalUrl = url;
    const modal = el("catModal");
    const body = el("catModalBody");
    const entry = entries.find((e) => e.url === url);
    el("catModalTitle").textContent = entry ? entry.title : "…";
    el("catModalSub").textContent = "";
    setModalBackdrop("");
    body.innerHTML = loadingMarkup();
    syncModalToggle();
    modal.style.display = "flex";
    // Without this the thirteen-thousand-row list keeps scrolling under the
    // dialog, which on a phone means closing it lands you somewhere else
    // entirely. The overlay carries overscroll-behavior for the same reason.
    document.body.classList.add("cat-modal-open");

    // A title the user just opened jumps the backfill's queue -- it is the one
    // entry out of thirteen thousand they demonstrably care about. Fire and
    // forget: the answer only changes a badge, and an already-resolved entry
    // costs the server a database read.
    resolveIds(url);

    try {
      const [seriesResp, seasonsResp] = await Promise.all([
        fetch("/api/series?url=" + encodeURIComponent(url)),
        fetch("/api/seasons?url=" + encodeURIComponent(url)),
      ]);
      if (modalUrl !== url) return;      // a different one was opened meanwhile
      const series = await seriesResp.json();
      const seasons = (await seasonsResp.json()).seasons || [];
      renderDetails(series, seasons, url);
    } catch (e) {
      body.innerHTML = '<div class="cat-empty">' + esc(t("failed", "Failed.")) + "</div>";
    }
  }

  function renderDetails(series, seasons, url) {
    const body = el("catModalBody");
    const title = (series && series.title) || "";
    if (title) el("catModalTitle").textContent = title;

    // Year and source on one line under the title: on this page "which site
    // is this row from" is a real question, and the modal used to drop it.
    const entry = entries.find((e) => e.url === url);
    const sub = [];
    if (series && series.release_year) sub.push(String(series.release_year));
    if (entry) sub.push(labelOf(entry.source));
    el("catModalSub").textContent = sub.join(" · ");

    const posterUrl = (series && series.poster_url) || "";
    if (posterUrl) setModalBackdrop(posterUrl, true);
    if (title) upgradeBackdropFromTmdb(title, url);

    const poster = posterUrl
      ? '<img class="cat-modal-poster" src="' + esc(posterUrl) + '" alt="" loading="lazy">'
      : '<div class="cat-modal-poster"></div>';
    const genres = ((series && series.genres) || [])
      .map((g) => "<span>" + esc(g) + "</span>").join("");
    const description = (series && series.description) || "";

    body.innerHTML =
      '<div class="cat-modal-body">' + poster +
      '<div class="cat-modal-info">' +
      (genres ? '<div class="cat-modal-genres">' + genres + "</div>" : "") +
      (description ? "<p>" + esc(description) + "</p>" : "") +
      "</div></div>" +
      '<div class="cat-eplist" id="catEpList"></div>';

    loadEpisodes(seasons, url);
  }

  async function loadEpisodes(seasons, url) {
    const host = el("catEpList");
    if (!host) return;
    if (!seasons.length) {
      host.innerHTML = '<div class="cat-empty">' + esc(t("no_episodes", "No episodes.")) + "</div>";
      return;
    }
    // Same shape as the list that is about to replace it, so the dialog does
    // not grow a second time once the episodes arrive.
    if (document.body.classList.contains("skeleton-loader")) {
      let sk = '<div class="cat-sk cat-sk-head"></div>';
      for (let i = 0; i < 6; i++) {
        sk += '<div class="cat-ep"><span class="cat-sk cat-sk-num"></span>' +
          '<span class="cat-sk cat-sk-line" style="width:' + (76 - i * 7) + '%"></span></div>';
      }
      host.innerHTML = sk;
    } else {
      host.innerHTML = '<div class="cat-empty">' + esc(t("loading", "Loading…")) + "</div>";
    }
    try {
      const lists = await Promise.all(seasons.map((s) =>
        fetch("/api/episodes?url=" + encodeURIComponent(s.url))
          .then((r) => r.json()).catch(() => ({ episodes: [] }))));
      if (modalUrl !== url) return;
      // /api/episodes already reports `downloaded` per episode -- the same
      // filesystem check the bulk worker's "only missing episodes" runs. It
      // was simply thrown away here, so a title you have half of looked
      // identical to one you have none of.
      //
      // Rendered exactly the way the download modal renders it (see
      // renderSeasons() in app.js): a green check after the episode number,
      // and a second one on the season header when the whole season is
      // there. Same .ep-downloaded / .season-downloaded classes from
      // cards.css, so the two views cannot drift apart -- and, like the
      // download modal, NOTHING is said about episodes that are missing.
      // "Nothing on disk yet" is a sentence nobody needs; the absence of a
      // check already says it.
      let html = "";
      lists.forEach((data, idx) => {
        const eps = data.episodes || [];
        if (!eps.length) return;
        const season = seasons[idx] || {};
        const allDownloaded = eps.every((ep) => ep.downloaded);
        html += '<div class="cat-season-head"><span>' +
          esc(t("season", "Season")) + " " +
          esc(season.season_number != null ? season.season_number : idx + 1) +
          " · " + eps.length + " " + esc(t("episodes_n", "episodes")) + "</span>" +
          (allDownloaded
            ? '<span class="season-downloaded" title="' +
              esc(t("all_downloaded", "All episodes downloaded")) + '">\u2713</span>'
            : "") +
          "</div>";
        eps.forEach((ep) => {
          html += '<div class="cat-ep">' +
            '<span class="cat-ep-num">E' +
            esc(ep.episode_number != null ? ep.episode_number : "?") + "</span>" +
            (ep.downloaded
              ? '<span class="ep-downloaded" title="' +
                esc(t("downloaded", "Downloaded")) + '">\u2713</span>'
              : "") +
            "<span>" + esc(ep.title_de || ep.title_en || "") + "</span></div>";
        });
      });
      host.innerHTML = html || '<div class="cat-empty">' + esc(t("no_episodes", "No episodes.")) + "</div>";
    } catch (e) {
      host.innerHTML = '<div class="cat-empty">' + esc(t("failed", "Failed.")) + "</div>";
    }
  }

  // ── Language groups ──────────────────────────────────────────────────────
  // The plain languages and the hosters are rendered by the template (see
  // catalogue.html). Only the GROUPS are fetched, because they are user data
  // that changes without a page reload -- and for a bulk action a fallback
  // chain is not a nicety: every series has different languages, and one fixed
  // language fails on a good share of a large selection.
  async function loadLanguageGroups() {
    try {
      const resp = await fetch("/api/language-groups");
      if (!resp.ok) return;
      const data = await resp.json();
      const groups = (data && (data.groups || data)) || [];
      if (!Array.isArray(groups) || !groups.length) return;
      const sel = el("catLanguage");
      const optgroup = document.createElement("optgroup");
      optgroup.label = t("groups", "Language groups");
      groups.forEach((g) => {
        if (!g || g.id == null) return;
        const opt = new Option(g.name || ("#" + g.id), "group:" + g.id);
        opt.title = (g.languages || []).join(" → ");
        optgroup.appendChild(opt);
      });
      if (optgroup.children.length) sel.appendChild(optgroup);
    } catch (e) { /* groups are optional; the plain languages still work */ }
  }

  // ── Lazy id resolution ───────────────────────────────────────────────────
  async function resolveIds(url) {
    const entry = entries.find((e) => e.url === url);
    if (!entry || entry.tmdb_id || entry.imdb_id) return;   // nothing to gain
    try {
      const resp = await fetch("/api/catalogue/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url }),
      });
      if (!resp.ok) return;
      const data = await resp.json();
      if (!data.tmdb_id && !data.imdb_id) return;
      entry.tmdb_id = data.tmdb_id || "";
      entry.imdb_id = data.imdb_id || "";
      // The row's "In library" badge may be decidable now that there is an id.
      render();
    } catch (e) { /* the title path still answers */ }
  }

  // ── Status strip ─────────────────────────────────────────────────────────
  // The lists come from the database now (web/catalogue_store.py), so the page
  // answers instantly and the refetch happens behind it. That is only an
  // improvement if the page SAYS what it is showing and what is going on --
  // otherwise "instant" is indistinguishable from "stuck on an old copy".
  //
  // Polls only while something is running, and stops as soon as nothing is.
  let statusPoll = null;
  let lastIdsRunning = false;

  const num = (value) => (typeof window.mfFormatNumber === "function"
    ? window.mfFormatNumber(value)
    : String(value));

  function humanAge(seconds) {
    if (seconds < 90) return "";                       // "just now" instead
    const mins = Math.round(seconds / 60);
    if (mins < 60) return mins + " " + t("ago_minutes", "min ago");
    const hours = Math.round(mins / 60);
    if (hours < 48) return hours + " " + t("ago_hours", "h ago");
    return Math.round(hours / 24) + " " + t("ago_days", "d ago");
  }

  function renderStatus(data) {
    const host = el("catStatus");
    if (!host) return false;
    const text = el("catStatusText");
    const bar = el("catStatusBar");
    const spin = el("catStatusSpin");
    const sources = (data && data.sources) || [];
    const ids = (data && data.ids) || {};
    const busy = (data && data.refreshing) || [];

    let message = "";
    let showBar = false;
    let spinning = false;

    if (busy.length) {
      // Which list, by name: with several sources "updating" alone leaves the
      // user guessing which one they are waiting on.
      const names = sources.filter((s) => s.refreshing).map((s) => s.label);
      message = t("refreshing_one", "Updating") + " " + (names.join(", ") || busy.join(", ")) + "…";
      spinning = true;
    } else if (ids.running && ids.total) {
      // The id backfill runs for a long time over thousands of rows, so this
      // one gets a real bar and real numbers -- "working" for an hour is not
      // a progress report.
      const pct = Math.min(100, Math.round((ids.checked / ids.total) * 100));
      message = t("resolving_ids", "Matching titles against TMDB") + " · " +
        num(ids.checked) + " / " + num(ids.total) + " (" + pct + "%)";
      showBar = true;
      spinning = true;
      el("catStatusFill").style.width = pct + "%";
    } else if (sources.some((s) => s.failed)) {
      message = t("refresh_failed", "The last update failed.");
    } else {
      // Nothing running: report the age of the OLDEST list, because that is
      // the one that decides how much the page can be trusted.
      const stamps = sources.filter((s) => s.fetched_at).map((s) => s.fetched_at);
      if (stamps.length) {
        const age = (Date.now() / 1000) - Math.min.apply(null, stamps);
        const human = humanAge(age);
        message = human
          ? t("updated_ago", "List from") + " " + human
          : t("updated_just_now", "List updated just now");
      }
    }

    // A finished backfill is worth one line; a permanently visible "complete"
    // badge is not.
    if (lastIdsRunning && !ids.running && ids.total) {
      message = t("ids_done", "Title matching complete") + " · " +
        num(ids.resolved || 0) + " / " + num(ids.total);
    }
    lastIdsRunning = !!ids.running;

    text.textContent = message;
    host.hidden = !message;
    bar.hidden = !showBar;
    spin.hidden = !spinning;
    host.classList.toggle("is-busy", spinning);
    host.classList.toggle("is-failed", !spinning && sources.some((s) => s.failed));
    return spinning;
  }

  async function pollStatus() {
    try {
      const resp = await fetch("/api/catalogue/status");
      if (!resp.ok) return false;
      const data = await resp.json();
      const busy = renderStatus(data);
      if (busy && !statusPoll) {
        statusPoll = setInterval(pollStatus, 3000);
      } else if (!busy && statusPoll) {
        clearInterval(statusPoll);
        statusPoll = null;
        // A refresh that just finished replaced the rows underneath us.
        loadAll(false);
      }
      return busy;
    } catch (e) { return false; }
  }

  const statusRefreshBtn = el("catStatusRefresh");
  if (statusRefreshBtn) {
    statusRefreshBtn.addEventListener("click", () => {
      fetch("/api/catalogue/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      }).then(() => pollStatus()).catch(() => { /* the strip stays as it was */ });
    });
  }

  // ── Bulk submit ──────────────────────────────────────────────────────────
  async function submit(mode) {
    if (!selection.size) { toast(t("pick_one", "Mark something first.")); return; }

    // Only urls that are in a LOADED catalogue travel. The server resolves
    // each one against whichever catalogue holds it, so a selection spanning
    // several sites goes in one request -- but a url left over in
    // localStorage from a source that has since been switched off would only
    // come back as "unknown", so it is dropped here.
    const known = new Set(entries.map((e) => e.url));
    const urls = Array.from(selection).filter((u) => known.has(u));
    if (!urls.length) { toast(t("pick_one", "Mark something first.")); return; }

    if (mode === "autosync" && !confirm(t("warn_sync", "Auto-Sync keeps downloading."))) return;

    // No `source`: the list is merged, so a selection legitimately spans
    // several sites and the server resolves each url against whichever
    // catalogue holds it.
    const body = {
      urls: urls,
      mode: mode,
      language: el("catLanguage").value || "German Dub",
      provider: el("catProvider").value || "VOE",
      missing_only: el("catMissingOnly").checked,
    };
    try {
      const resp = await fetch("/api/catalogue/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (!resp.ok) {
        toast(data.code === "busy" ? t("busy", "Still running.")
          : (data.error || t("failed", "Failed.")));
        return;
      }
      // Only what was actually submitted is cleared: entries of another source
      // stay marked, which is the whole reason the selection is global.
      urls.forEach((u) => selection.delete(u));
      saveSelection();
      updateCount();
      render();
      watchJob(data.id, mode);
    } catch (e) {
      toast(t("failed", "Failed."));
    }
  }

  // ── Options panel (phones) ───────────────────────────────────────────────
  // The three option controls are collapsed on a phone (see catalogue.css).
  // The toggle carries their current values, so the panel stays closed
  // without hiding what is set -- which is the only reason collapsing them is
  // acceptable at all.
  function updateOptionsSummary() {
    const host = el("catOptionsSummary");
    if (!host) return;
    const lang = el("catLanguage");
    const prov = el("catProvider");
    const bits = [
      (lang.options[lang.selectedIndex] || {}).text || "",
      (prov.options[prov.selectedIndex] || {}).text || "",
      el("catMissingOnly").checked
        ? t("only_missing_short", "only missing")
        : t("all_episodes_short", "all episodes"),
    ].filter(Boolean);
    host.textContent = bits.join(" · ");
  }

  // On a phone this opens the whole sheet, not just the option panel: the
  // Auto-Sync button and the warning live behind it too (see .cat-actions
  // .is-open in catalogue.css), which is what keeps the collapsed bar to a
  // single line.
  const optionsToggle = el("catOptionsToggle");
  if (optionsToggle) {
    optionsToggle.addEventListener("click", () => {
      const actions = el("catActions");
      const open = el("catOptions").classList.toggle("is-open");
      if (actions) actions.classList.toggle("is-open", open);
      optionsToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // Empty selection means there is nothing the bar can do, so on a phone it
  // gets out of the way entirely -- that is the whole point of the rebuild,
  // and it is worth more than any amount of shrinking the controls.
  function syncActionsBar(count) {
    const actions = el("catActions");
    if (!actions) return;
    actions.classList.toggle("is-empty", count === 0);
    if (count === 0 && actions.classList.contains("is-open")) {
      actions.classList.remove("is-open");
      el("catOptions").classList.remove("is-open");
      if (optionsToggle) optionsToggle.setAttribute("aria-expanded", "false");
    }
  }
  ["catLanguage", "catProvider", "catMissingOnly"].forEach((id) => {
    const node = el(id);
    if (node) node.addEventListener("change", updateOptionsSummary);
  });

  el("catQueueBtn").addEventListener("click", () => submit("queue"));
  el("catSyncBtn").addEventListener("click", () => submit("autosync"));

  // ── Job progress ─────────────────────────────────────────────────────────
  function watchJob(jobId, mode) {
    const card = el("catJob");
    card.style.display = "";
    el("catJobTitle").textContent = mode === "autosync"
      ? t("job_sync", "Creating Auto-Sync jobs") : t("job_queue", "Adding to the queue");
    el("catJobCancel").style.display = "";
    el("catJobCancel").onclick = () => {
      fetch("/api/catalogue/bulk/" + encodeURIComponent(jobId) + "/cancel", { method: "POST" });
    };

    if (jobPoll) clearInterval(jobPoll);
    const tick = async () => {
      try {
        const resp = await fetch("/api/catalogue/bulk/" + encodeURIComponent(jobId));
        if (!resp.ok) { clearInterval(jobPoll); jobPoll = null; return; }
        const job = await resp.json();
        renderJob(job);
        if (job.status !== "running") {
          clearInterval(jobPoll);
          jobPoll = null;
          el("catJobCancel").style.display = "none";
          // Badges are stale now -- and with a status chip off they decide
          // which rows exist, so this is applyFilter(), not render().
          loadState().then(applyFilter);
        }
      } catch (e) { /* transient — the next tick tries again */ }
    };
    jobPoll = setInterval(tick, 1500);
    tick();
  }

  function renderJob(job) {
    const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
    el("catJobFill").style.width = pct + "%";
    const bits = [
      job.done + "/" + job.total,
      job.queued + " " + t("created", "created"),
    ];
    if (job.episodes) bits.push(job.episodes + " " + t("episodes_n", "episodes"));
    if (job.skipped) bits.push(job.skipped + " " + t("skipped", "skipped"));
    if (job.failed) bits.push(job.failed + " " + t("failed_n", "failed"));
    if (job.status === "finished") bits.push("✓ " + t("done", "Done"));
    if (job.status === "cancelled") bits.push(t("cancelled", "Stopped"));
    el("catJobStats").innerHTML = bits.map((b) => "<span>" + esc(b) + "</span>").join("");
  }

  function toast(message) {
    if (typeof window.showToast === "function") window.showToast(message);
    else alert(message);
  }

  // ── Boot ─────────────────────────────────────────────────────────────────
  // A bulk job keeps running on the server while you are somewhere else in the
  // app, so coming back to this page has to pick the progress card back up --
  // otherwise a job that is halfway through a hundred series looks like it
  // never started, and the obvious reaction is to submit it a second time.
  async function resumeJob() {
    try {
      const resp = await fetch("/api/catalogue/bulk");
      if (!resp.ok) return;
      const jobs = (await resp.json()).jobs || [];
      if (!jobs.length) return;
      const running = jobs.filter((j) => j.status === "running").pop();
      if (running) { watchJob(running.id, running.mode); return; }
      // Nothing running: still show the most recent result, so a job that
      // finished while you were away is not silently swallowed. Only briefly
      // relevant, so anything older than an hour is left alone.
      const last = jobs[jobs.length - 1];
      if (last && last.finished_at && (Date.now() / 1000 - last.finished_at) < 3600) {
        el("catJob").style.display = "";
        el("catJobTitle").textContent = last.mode === "autosync"
          ? t("job_sync", "Creating Auto-Sync jobs") : t("job_queue", "Adding to the queue");
        el("catJobCancel").style.display = "none";
        renderJob(last);
      }
    } catch (e) { /* the page works fine without it */ }
  }

  loadSelection();
  loadPrefs();              // after loadSelection: onlySelected depends on it
  syncActionsBar(selection.size);   // before the first fetch, so the bar never
                                    // flashes into view on an empty selection
  renderStatusChips();
  renderSortButton();
  updateOptionsSummary();
  loadLanguageGroups().then(updateOptionsSummary);
  // Populate app.js's library indexes for this page. Without it every
  // "In library" check on the Catalogue page answered no -- see inLibrary().
  if (typeof window.loadDownloadedFolders === "function") {
    window.loadDownloadedFolders().then(() => {
      // The list may already be on screen; re-run the filter so the badges
      // (and the "In library" chip) reflect what was just loaded.
      if (entries.length) applyFilter();
    }).catch(() => { /* best-effort, same as app.js */ });
  }
  loadState().then(loadSources);
  resumeJob();
  pollStatus();
})();
