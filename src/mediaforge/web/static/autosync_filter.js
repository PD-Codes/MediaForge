/* ============================================================
   AutoSync Episode Filter — shared UI
   Provides a season/episode picker used by:
     - the search modal (create a sync job)   -> AutosyncFilter.openCreate()
     - the Auto-Sync page edit modal (embed)   -> AutosyncFilter.renderPicker()
   ============================================================ */
(function () {
  "use strict";

  // translation + escape fallbacks (global t/esc exist on both pages)
  const tr = (de, en) => (typeof t === "function" ? t(de, en) : de);
  const escape = (s) =>
    typeof esc === "function"
      ? esc(s)
      : String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
          ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]),
        );
  const toast = (m) => {
    if (typeof showToast === "function") showToast(m);
  };

  const DEFAULT_LANGS = [
    "German Dub",
    "English Sub",
    "German Sub",
    "English Dub",
    "English Dub (German Sub)",
  ];
  const DEFAULT_PROVIDERS = ["VOE", "Vidmoly", "Vidoza"];

  function el(tag, props, children) {
    const node = document.createElement(tag);
    if (props) {
      for (const k in props) {
        if (k === "class") node.className = props[k];
        else if (k === "html") node.innerHTML = props[k];
        else if (k === "text") node.textContent = props[k];
        else if (k.startsWith("on") && typeof props[k] === "function")
          node.addEventListener(k.slice(2), props[k]);
        else if (props[k] != null) node.setAttribute(k, props[k]);
      }
    }
    (children || []).forEach((c) => {
      if (c == null) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  // ---- range validation / parsing (mirror of server autosync_filter.py) ----
  function isValidRange(spec) {
    if (!spec) return true; // empty = whole season
    return /^\s*\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*\s*$/.test(spec);
  }

  // ---- compress a sorted list of ints into "1-12,15,20-22" ----
  function compressRanges(nums) {
    const a = Array.from(new Set(nums)).sort((x, y) => x - y);
    const out = [];
    let i = 0;
    while (i < a.length) {
      let j = i;
      while (j + 1 < a.length && a[j + 1] === a[j] + 1) j++;
      out.push(i === j ? String(a[i]) : a[i] + "-" + a[j]);
      i = j + 1;
    }
    return out.join(",");
  }
  function parseRangeJs(spec) {
    const set = new Set();
    if (!spec || typeof spec !== "string") return set;
    spec.split(",").forEach((part) => {
      part = part.trim();
      if (!part) return;
      if (part.indexOf("-") !== -1) {
        let [lo, hi] = part.split("-").map((x) => parseInt(x, 10));
        if (isNaN(lo) || isNaN(hi)) return;
        if (lo > hi) [lo, hi] = [hi, lo];
        for (let k = lo; k <= hi; k++) set.add(k);
      } else {
        const v = parseInt(part, 10);
        if (!isNaN(v)) set.add(v);
      }
    });
    return set;
  }

  // ---- build the picker into a container; returns a controller ----
  function renderPicker(container, opts) {
    opts = opts || {};
    const customPaths = opts.customPaths || [];
    const showMoviePath = opts.showMoviePath !== false;
    let mode = (opts.existingFilter && opts.existingFilter.mode) || "all";
    const existing = opts.existingFilter || null;

    container.innerHTML = "";

    // --- mode segmented control ---
    const modeAll = el("div", { class: "asf-mode" }, [
      el("div", { class: "asf-mode-title", text: tr("Alles synchronisieren", "Sync everything") }),
      el("div", {
        class: "asf-mode-desc",
        text: tr(
          "Alle Staffeln & Episoden, inkl. künftiger. Keine Auswahl nötig.",
          "All seasons & episodes, incl. future ones. No selection needed.",
        ),
      }),
    ]);
    const modeSel = el("div", { class: "asf-mode" }, [
      el("div", { class: "asf-mode-title", text: tr("Nur Ausgewähltes", "Only selected") }),
      el("div", {
        class: "asf-mode-desc",
        text: tr(
          "Du wählst gezielt Staffeln/Episoden. Nicht Gewähltes wird nicht gesynct.",
          "You pick specific seasons/episodes. Anything not selected is not synced.",
        ),
      }),
    ]);
    const modesWrap = el("div", { class: "asf-modes" }, [modeAll, modeSel]);

    const seasonsBox = el("div", { class: "asf-seasons" }, [
      el("div", { class: "asf-spinner", text: tr("Lade Staffeln…", "Loading seasons…") }),
    ]);
    const hint = el("div", {
      class: "asf-hint",
      text: tr(
        "Staffel aufklappen, um einzelne Episoden an-/abzuwählen. Häkchen an der Staffel = ganze Staffel (inkl. künftiger Folgen).",
        "Expand a season to pick individual episodes. Season checkbox = whole season (incl. future episodes).",
      ),
    });

    container.appendChild(el("div", { class: "asf-section" }, [
      el("span", { class: "asf-section-label", text: tr("Modus", "Mode") }),
      modesWrap,
    ]));

    // numbered-seasons section (hidden in "all" mode)
    const seasonsSection = el("div", { class: "asf-section" }, [
      el("span", { class: "asf-section-label", text: tr("Staffeln & Episoden", "Seasons & episodes") }),
      seasonsBox,
      hint,
    ]);
    container.appendChild(seasonsSection);

    function paintMode() {
      modeAll.classList.toggle("asf-active", mode === "all");
      modeSel.classList.toggle("asf-active", mode === "selected");
      // The season/episode picker is pointless in "all" mode -> hide it.
      seasonsSection.style.display = mode === "all" ? "none" : "";
    }
    modeAll.addEventListener("click", () => { mode = "all"; paintMode(); });
    modeSel.addEventListener("click", () => { mode = "selected"; ensureSeasonsLoaded(); paintMode(); });
    paintMode();

    // --- Movies / Specials section (own section, visible in both modes) ---
    const moviesCb = el("input", { type: "checkbox", class: "asf-season-cb chb-main" });
    moviesCb.checked = !!(existing && existing.include_movies);
    const moviePathSelect = el("select", { class: "asf-select" }, [
      el("option", { value: "", text: tr("Wie Serie (Standard)", "Same as series (default)") }),
    ]);
    customPaths.forEach((p) =>
      moviePathSelect.appendChild(el("option", { value: p.id, text: p.name + " (" + p.path + ")" })),
    );
    if (existing && existing.movie_custom_path_id != null)
      moviePathSelect.value = String(existing.movie_custom_path_id);
    const moviePathRow = el("div", { class: "asf-movie-path-row" }, [
      el("label", { text: tr("Filme-Pfad", "Movies path"), style: "min-width:92px;font-size:0.82rem;color:var(--text-secondary)" }),
      moviePathSelect,
    ]);
    const moviesToggleRow = el("label", { class: "asf-movies-toggle" }, [
      moviesCb,
      el("span", { text: tr("Filme / Specials mitsynchronisieren", "Also sync movies / specials") }),
    ]);
    const moviesSection = el("div", { class: "asf-section" }, [
      el("span", { class: "asf-section-label", text: tr("Filme & Specials", "Movies & specials") }),
      moviesToggleRow,
      showMoviePath ? moviePathRow : null,
    ]);
    moviesSection.style.display = "none"; // shown only if the series has a movies collection
    container.appendChild(moviesSection);
    moviesCb.addEventListener("change", () =>
      moviePathRow.classList.toggle("asf-show", moviesCb.checked && showMoviePath),
    );
    moviePathRow.classList.toggle("asf-show", moviesCb.checked && showMoviePath);

    const rows = []; // numbered-season row state objects
    let hasMovies = false;

    // ---- one numbered season row with lazy episode chips ----
    function makeSeasonRow(season) {
      const n = season.season_number;
      const cb = el("input", { type: "checkbox", class: "asf-season-cb chb-main" });
      const chevron = el("button", {
        class: "asf-expand",
        type: "button",
        title: tr("Episoden anzeigen", "Show episodes"),
        html: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>',
      });
      const summary = el("span", { class: "asf-season-sel", text: "" });
      const epWrap = el("div", { class: "asf-episodes" });

      const entry = existing && existing.seasons ? existing.seasons[String(n)] : undefined;
      const st = {
        season_number: n,
        url: season.url,
        episode_count: season.episode_count || 0,
        cb, epWrap, summary,
        episodes: null, selected: null, loaded: false, chips: {},
        btnAll: null,
        btnNone: null,
      };
      // New job: default whole-season (user deselects). Existing "selected"
      // filter: seasons not listed are off.
      if (entry === undefined) st.selected = (existing && existing.mode === "selected") ? null : "ALL";
      else if (entry === false) st.selected = null;
      else if (entry === true) st.selected = "ALL";
      else if (typeof entry === "string") st.selected = parseRangeJs(entry);
      else st.selected = entry ? "ALL" : null;

      const isAll = () => st.selected === "ALL";
      const isNone = () => st.selected === null || (st.selected instanceof Set && st.selected.size === 0);

      function paint() {
        if (isAll()) { cb.checked = true; cb.indeterminate = false; }
        else if (isNone()) { cb.checked = false; cb.indeterminate = false; }
        else { cb.checked = true; cb.indeterminate = true; }
        cb.classList.toggle("asf-cb-checked", isAll());
        cb.classList.toggle("asf-cb-indet", cb.indeterminate);
        if (isAll()) summary.textContent = tr("ganze Staffel", "whole season");
        else if (isNone()) summary.textContent = tr("aus", "off");
        else summary.textContent = compressRanges(Array.from(st.selected));
        if (st.loaded) {
          st.episodes.forEach((e) => {
            const on = isAll() || (st.selected instanceof Set && st.selected.has(e));
            if (st.chips[e]) st.chips[e].classList.toggle("asf-chip-on", on);
          });
        }
        if (st.btnAll) {
          st.btnAll.classList.toggle("asf-mini-on", isAll());
          st.btnAll.classList.toggle("asf-mini-off", !isAll());
        }
        if (st.btnNone) {
          st.btnNone.classList.toggle("asf-mini-on", isNone());
          st.btnNone.classList.toggle("asf-mini-off", !isNone());
        }
      }

      function renderChips() {
        epWrap.innerHTML = "";
        const btnAll = el("button", { type: "button", class: "asf-mini", text: tr("Alle", "All"), onclick: () => { st.selected = "ALL"; paint(); } });
        const btnNone = el("button", { type: "button", class: "asf-mini", text: tr("Keine", "None"), onclick: () => { st.selected = new Set(); paint(); } });
        st.btnAll = btnAll;
        st.btnNone = btnNone;
        epWrap.appendChild(el("div", { class: "asf-chip-bar" }, [btnAll, btnNone]));
        const grid = el("div", { class: "asf-chip-grid" });
        st.episodes.forEach((e) => {
          const chip = el("button", { type: "button", class: "asf-chip", text: "E" + e });
          chip.addEventListener("click", () => {
            if (st.selected === "ALL") st.selected = new Set(st.episodes);
            if (!(st.selected instanceof Set)) st.selected = new Set();
            if (st.selected.has(e)) st.selected.delete(e); else st.selected.add(e);
            if (st.episodes.every((x) => st.selected.has(x))) st.selected = "ALL";
            paint();
          });
          st.chips[e] = chip;
          grid.appendChild(chip);
        });
        epWrap.appendChild(grid);
        paint();
      }

      let loading = false;
      function loadEpisodes() {
        if (st.loaded || loading) return;
        loading = true;
        epWrap.innerHTML = "<div class='asf-spinner'>" + tr("Lade Episoden…", "Loading episodes…") + "</div>";
        fetch("/api/episodes?url=" + encodeURIComponent(st.url))
          .then((r) => r.json())
          .then((d) => {
            const eps = (d && d.episodes ? d.episodes : []).map((x) => x.episode_number).filter((x) => x != null);
            st.episodes = Array.from(new Set(eps)).sort((x, y) => x - y);
            st.loaded = true;
            renderChips();
          })
          .catch(() => {
            const known = st.selected instanceof Set ? Array.from(st.selected) : [];
            if (known.length) {
              st.episodes = known.sort((x, y) => x - y);
              st.loaded = true;
              epWrap.innerHTML = "";
              epWrap.appendChild(el("div", { class: "asf-warn", text: tr("Episoden konnten nicht geladen werden – gespeicherte Auswahl wird angezeigt.", "Could not load episodes – showing the saved selection.") }));
              renderChips();
            } else {
              epWrap.innerHTML = "<div class='asf-warn'>" + tr("Episoden konnten nicht geladen werden.", "Could not load episodes.") + "</div>";
            }
          })
          .finally(() => { loading = false; });
      }

      let open = false;
      chevron.addEventListener("click", () => {
        open = !open;
        chevron.classList.toggle("asf-open", open);
        epWrap.style.display = open ? "" : "none";
        if (open) loadEpisodes();
      });
      epWrap.style.display = "none";
      cb.addEventListener("change", () => { st.selected = cb.checked ? "ALL" : null; paint(); });

      paint();
      rows.push(st);

      const header = el("div", { class: "asf-season" }, [
        cb,
        el("span", { class: "asf-season-name", text: tr("Staffel ", "Season ") + n }),
        season.episode_count ? el("span", { class: "asf-season-count", text: season.episode_count + " " + tr("Folgen", "eps") }) : null,
        el("span", { class: "asf-season-spacer" }),
        summary,
        chevron,
      ]);
      return el("div", { class: "asf-season-block" }, [header, epWrap]);
    }

    function renderSeasonList(seasons) {
      seasonsBox.innerHTML = "";
      const numbered = seasons.filter((s) => !s.are_movies);
      const movies = seasons.filter((s) => s.are_movies);
      numbered.forEach((s) => seasonsBox.appendChild(makeSeasonRow(s)));
      if (!numbered.length)
        seasonsBox.appendChild(el("div", { class: "asf-spinner", text: tr("Keine Staffeln gefunden.", "No seasons found.") }));
      hasMovies = movies.length > 0;
      moviesSection.style.display = hasMovies ? "" : "none";
    }

    // Seasons are fetched lazily — only when the user actually opens the
    // "Only selected" picker. The common "Sync everything" flow (the default,
    // and what "Add to Auto-Sync" from the library uses) opens instantly with
    // no network call, so there is never a dangling "Loading seasons…" state.
    let _seasonsRequested = false;
    function ensureSeasonsLoaded() {
      if (_seasonsRequested) return;
      _seasonsRequested = true;
      if (!opts.seriesUrl) {
        renderSeasonList(buildFallbackSeasons(existing));
        return;
      }
      // Guard against a hanging request (e.g. a slow/offline source) so the
      // "Loading seasons…" spinner can't spin forever — abort after 20s.
      const _ac = typeof AbortController !== "undefined" ? new AbortController() : null;
      const _timer = setTimeout(() => { if (_ac) _ac.abort(); }, 20000);
      fetch("/api/seasons?url=" + encodeURIComponent(opts.seriesUrl),
            _ac ? { signal: _ac.signal } : undefined)
        .then((r) => r.json())
        .then((d) => {
          clearTimeout(_timer);
          if (d && Array.isArray(d.seasons) && d.seasons.length) renderSeasonList(d.seasons);
          else throw new Error("empty");
        })
        .catch(() => {
          clearTimeout(_timer);
          const fallback = buildFallbackSeasons(existing);
          seasonsSection.insertBefore(
            el("div", {
              class: "asf-warn",
              text: tr(
                "Staffelliste konnte nicht geladen werden (Serie evtl. offline). Es wird die gespeicherte Auswahl angezeigt.",
                "Could not load the season list (series may be offline). Showing the saved selection.",
              ),
            }),
            seasonsBox,
          );
          renderSeasonList(fallback);
        });
    }
    // If we open directly in "selected" mode (e.g. editing a filtered job),
    // load the seasons right away.
    if (mode === "selected") ensureSeasonsLoaded();

    const ready = Promise.resolve();

    function buildFallbackSeasons(flt) {
      const out = [];
      const keys = flt && flt.seasons ? Object.keys(flt.seasons).map(Number).sort((a, b) => a - b) : [];
      keys.forEach((n) => out.push({ season_number: n, episode_count: 0, are_movies: false }));
      if (flt && flt.include_movies) out.push({ season_number: 0, episode_count: 0, are_movies: true });
      return out;
    }

    return {
      ready,
      getMode: () => mode,
      validate() { return true; },
      getFilter() {
        const seasons = {};
        // In "all" mode the picker is hidden; everything syncs (no per-season keys).
        if (mode === "selected") {
          rows.forEach((r) => {
            const none = r.selected === null || (r.selected instanceof Set && r.selected.size === 0);
            const all = r.selected === "ALL";
            if (none) { /* omit = excluded */ }
            else if (all) seasons[String(r.season_number)] = true;
            else seasons[String(r.season_number)] = compressRanges(Array.from(r.selected));
          });
        }
        return { mode, seasons, include_movies: !!(moviesCb && moviesCb.checked) };
      },
      getMoviePathId() {
        return moviePathSelect.value ? parseInt(moviePathSelect.value, 10) : null;
      },
    };
  }

  // ---- full create/edit dialog (used by the search modal) ----
  let overlay = null;
  function ensureOverlay() {
    if (overlay) return overlay;
    overlay = el("div", { class: "asf-overlay", id: "asfOverlay" });
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
    return overlay;
  }
  function close() { if (overlay) overlay.classList.remove("asf-open"); }

  function openCreate(opts) {
    opts = opts || {};
    ensureOverlay();
    overlay.innerHTML = "";

    const existing = opts.existing || null;
    const coverUrl = opts.coverUrl || "";
    // "group:<id>" values may already be in opts.languages (the search modal
    // passes its dropdown verbatim); the group list is what turns them into
    // readable names, so drop them when it isn't available.
    // Groups need language separation (the backend refuses them otherwise); an
    // existing job that already uses one still gets its option so the dialog
    // shows the truth.
    const groups = (opts.langSepEnabled
      || String((existing && existing.language) || "").indexOf("group:") === 0)
      ? (opts.languageGroups || [])
      : [];
    const isGroup = (v) => String(v || "").indexOf("group:") === 0;
    const groupName = (v) => {
      const g = groups.find((x) => "group:" + x.id === v);
      return g ? g.name : null;
    };
    const langs = (opts.languages && opts.languages.length ? opts.languages : DEFAULT_LANGS)
      .filter((l) => !isGroup(l));
    if (opts.langSepEnabled && langs.indexOf("All Languages") === -1) langs.unshift("All Languages");
    const providers = opts.providers && opts.providers.length ? opts.providers : DEFAULT_PROVIDERS;
    const customPaths = opts.customPaths || [];

    const langSelect = el("select", { class: "asf-select" },
      langs.map((l) => el("option", { value: l, text: l === "All Languages" ? tr("Alle Sprachen", "All Languages") : l })));
    if (groups.length) {
      const optgroup = el("optgroup", { label: tr("Sprachgruppen", "Language groups") },
        groups.map((g) => el("option", {
          value: "group:" + g.id,
          text: g.name,
          title: (g.languages || []).join(" → "),
        })));
      langSelect.appendChild(optgroup);
    }
    const wantedLang = (existing && existing.language) || opts.currentLanguage || langs[0];
    // A job may point at a group this instance no longer has; don't silently
    // show the first language as if that were the job's setting.
    langSelect.value = (isGroup(wantedLang) && !groupName(wantedLang)) ? langs[0] : wantedLang;

    const provSelect = el("select", { class: "asf-select" },
      providers.map((p) => el("option", { value: p, text: p })));
    provSelect.value = (existing && existing.provider) || opts.currentProvider || providers[0];

    const pathSelect = el("select", { class: "asf-select" }, [
      el("option", { value: "", text: tr("Standard", "Default") }),
    ]);
    customPaths.forEach((p) => pathSelect.appendChild(el("option", { value: p.id, text: p.name + " (" + p.path + ")" })));
    if (existing && existing.custom_path_id != null) pathSelect.value = String(existing.custom_path_id);

    const pickerHost = el("div", {});

    const saveBtn = el("button", { class: "btn btn-primary", text: tr("Speichern", "Save") });
    const cancelBtn = el("button", { class: "btn btn-ghost", text: tr("Abbrechen", "Cancel"), onclick: close });
    const actions = [cancelBtn];
    if (existing && existing.id) {
      actions.push(el("button", {
        class: "btn btn-danger",
        text: tr("Auto-Sync entfernen", "Remove Auto-Sync"),
        onclick: async () => {
          const r = await fetch("/api/autosync/" + existing.id, { method: "DELETE" });
          const d = await r.json();
          if (d.ok) { toast(tr("Auto-Sync entfernt", "Auto-Sync removed")); close(); if (opts.onSaved) opts.onSaved({ removed: true }); }
          else toast(d.error || tr("Entfernen fehlgeschlagen", "Remove failed"));
        },
      }));
    }
    actions.push(saveBtn);

    const modal = el("div", { class: "asf-modal" }, [
      el("button", {
        class: "asf-close",
        "aria-label": tr("Schließen", "Close"),
        onclick: close,
        html: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
      }),
      el("h2", { class: "asf-title", text: existing && existing.id ? tr("Auto-Sync bearbeiten", "Edit Auto-Sync") : tr("Auto-Sync einrichten", "Set up Auto-Sync") }),
      el("div", { class: "asf-sub", text: opts.title || opts.seriesUrl || "" }),
      el("div", { class: "asf-row" }, [el("label", { text: tr("Sprache", "Language") }), langSelect]),
      el("div", { class: "asf-row" }, [el("label", { text: tr("Provider", "Provider") }), provSelect]),
      el("div", { class: "asf-row" }, [el("label", { text: tr("Pfad", "Path") }), pathSelect]),
      pickerHost,
      el("div", { class: "asf-actions" }, actions),
    ]);
    overlay.appendChild(modal);
    overlay.classList.add("asf-open");

    let existingFilter = null;
    if (existing && existing.episode_filter) {
      try { existingFilter = typeof existing.episode_filter === "string" ? JSON.parse(existing.episode_filter) : existing.episode_filter; }
      catch (e) { existingFilter = null; }
    }
    if (existingFilter) {
      existingFilter.movie_custom_path_id = existing.movie_custom_path_id;
    } else if (existing && existing.movie_custom_path_id != null) {
      existingFilter = { mode: "all", seasons: {}, include_movies: false, movie_custom_path_id: existing.movie_custom_path_id };
    }

    const picker = renderPicker(pickerHost, {
      seriesUrl: opts.seriesUrl,
      existingFilter,
      customPaths,
    });

    saveBtn.addEventListener("click", async () => {
      if (!picker.validate()) {
        toast(tr("Ungültiger Episodenbereich (z. B. „1-12“).", "Invalid episode range (e.g. \"1-12\")."));
        return;
      }
      const filter = picker.getFilter();
      const moviePathId = picker.getMoviePathId();
      const pathVal = pathSelect.value ? parseInt(pathSelect.value, 10) : null;
      saveBtn.disabled = true;
      try {
        if (existing && existing.id) {
          const r = await fetch("/api/autosync/" + existing.id, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              language: langSelect.value,
              provider: provSelect.value,
              custom_path_id: pathVal,
              episode_filter: filter,
              movie_custom_path_id: moviePathId,
            }),
          });
          const d = await r.json();
          if (d.ok) { toast(tr("Auto-Sync gespeichert", "Auto-Sync saved")); close(); if (opts.onSaved) opts.onSaved({ updated: true }); }
          else toast(d.error || tr("Speichern fehlgeschlagen", "Save failed"));
        } else {
          const r = await fetch("/api/autosync", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              title: opts.title,
              series_url: opts.seriesUrl,
              cover_url: coverUrl,
              language: langSelect.value,
              provider: provSelect.value,
              custom_path_id: pathVal,
              episode_filter: filter,
              movie_custom_path_id: moviePathId,
            }),
          });
          const d = await r.json();
          if (d.ok) { toast(tr("Auto-Sync eingerichtet", "Auto-Sync set up")); close(); if (opts.onSaved) opts.onSaved({ created: true, id: d.id }); }
          else if (r.status === 409 && d.job) {
            // already exists -> reopen as edit
            close();
            openCreate(Object.assign({}, opts, { existing: d.job }));
          } else toast(d.error || tr("Einrichten fehlgeschlagen", "Setup failed"));
        }
      } catch (e) {
        toast(tr("Anfrage fehlgeschlagen", "Request failed"));
      } finally {
        saveBtn.disabled = false;
      }
    });
  }

  // ---- short pill summary of a filter (for cards / buttons) ----
  function summarize(filter) {
    let flt = filter;
    if (typeof flt === "string") { try { flt = JSON.parse(flt); } catch (e) { return ""; } }
    if (!flt) return "";
    const parts = [];
    const seasons = flt.seasons || {};
    const keys = Object.keys(seasons);
    if (flt.mode === "selected") {
      const on = keys.filter((k) => seasons[k] !== false);
      if (on.length) parts.push("S" + on.map((k) => seasons[k] === true ? k : k + "(" + seasons[k] + ")").join(","));
      else parts.push(tr("Auswahl", "selected"));
    } else {
      const off = keys.filter((k) => seasons[k] === false);
      const ranges = keys.filter((k) => typeof seasons[k] === "string");
      if (off.length) parts.push(tr("ohne S", "no S") + off.join(","));
      if (ranges.length) parts.push("S" + ranges.map((k) => k + "(" + seasons[k] + ")").join(","));
      if (!parts.length) parts.push(tr("Alle", "All"));
    }
    if (flt.include_movies) parts.push(tr("+Filme", "+Movies"));
    return parts.join(" · ");
  }

  window.AutosyncFilter = { openCreate, renderPicker, summarize, close, isValidRange };
})();
