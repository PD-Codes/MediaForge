/* ===================================================================
   MediaForge — Home feed (the opt-in "new home page")

   Settings → General → "Use the new home page" swaps the classic home
   discovery block (one section per provider, two to four poster rows each,
   eleven rows in total) for this one: rows grouped by *question* instead of
   by source.

     Continue watching  — unfinished playback positions, from your library
     Your watchlist     — favourites
     New in your library— what the library scan picked up recently
     Airing next        — the calendar's next two weeks
     New this week      — every enabled source, interleaved
     Popular right now  — same
     Movies             — whatever did not already appear above

   The rows are built by GET /api/home-feed (one request, not eleven) and
   GET /api/home-feed/personal. The server owns *which* items end up in
   which row, because it is the only side that knows which sources exist --
   a module that registers a source through register_home_feed_source()
   shows up here without this file knowing its name. What this file owns is
   the chips, the filtering, and how a card looks.

   Discovery cards deliberately go through app.js's renderBrowseCards(), so
   the "already downloaded" badge, the Auto-Sync badge, TMDB/CineInfo
   enrichment and the click-through behave exactly as on the classic page.
   =================================================================== */

(function () {
  const feed = document.getElementById("homeFeed");
  if (!feed) return;                       // classic home page — nothing to do

  // Every visible string comes from the template (index.html renders them
  // through Flask-Babel), so the feed is translated by the same catalogue as
  // the rest of the app instead of a hardcoded German/English pair.
  // mfEscape (mf_escape.js) is the project's only escaper -- it also escapes
  // quotes, which matters here because several values end up in attributes.
  const I18N = window.__HOME_I18N || {};
  function HT(key) { return I18N[key] || key; }

  const DISCOVERY_ROWS = ["new", "popular", "movies"];
  const PERSONAL_ROWS = ["continue", "watchlist", "library", "upcoming", "gaps"];
  // Filled from /api/home-feed's `config`: which rows this account wants, in
  // which order (Settings -> Start Page, or the "Customise" button here).
  let layout = { order: [], hidden: [], limit: 30, rows: [] };
  const ROW_GRIDS = {
    continue: "feedContinueGrid",
    watchlist: "feedWatchlistGrid",
    library: "feedLibraryGrid",
    upcoming: "feedUpcomingGrid",
    gaps: "feedGapsGrid",
    because: "feedBecauseGrid",
    new: "feedNewGrid",
    popular: "feedPopularGrid",
    movies: "feedMoviesGrid",
  };
  let ROW_MAX = 30;
  const PREF_KEY = "home_feed_filters";
  const LS_KEY = "mf-home-filters";
  const RELOAD_AFTER = 3600000;            // 1 h, same as the server-side cache

  let sources = [];                        // [{id,label,color,enabled,types,error}]
  let rows = {};                           // row -> [card]
  let personal = {};
  let offSources = {};                     // id -> true when switched off
  let offTypes = { adult: true };          // 18+ starts off
  // "Vorhanden" / "Im Auto-Sync": both ON by default, because the default
  // discovery row is "what is new", not "what is new and not mine". Switching
  // one off hides the cards carrying that badge and the reserve pool moves the
  // next cards up (see visibleCards).
  const STATUS_KEYS = ["library", "autosync"];
  let offStatus = {};                      // key -> true when switched off
  let feedError = "";
  let downIds = [];                        // reported down by the UpTime monitor

  // ------------------------------------------------------------ preferences
  // Which chips are off is a per-ACCOUNT preference (same reasoning as the
  // library layout): a filter that resets on every device is a filter the
  // user sets again every day. localStorage stays as the fallback for the
  // logged-out / auth-disabled case.
  let hasStoredFilters = false;

  function loadFilters() {
    let raw = "";
    const prefs = window._USER_PREFS || {};
    if (typeof prefs[PREF_KEY] === "string") {
      raw = prefs[PREF_KEY];
    } else {
      try { raw = localStorage.getItem(LS_KEY) || ""; } catch (e) { raw = ""; }
    }
    if (!raw) return;
    hasStoredFilters = true;
    offSources = {};
    offTypes = {};
    // NOT reset from a stored value that predates the status filter: an
    // account that saved its filters before this existed has no "x:" part, and
    // clearing the defaults on that basis would silently switch both entries
    // on for everyone... which is what they already are. Kept explicit so the
    // next key added here does not get it wrong.
    offStatus = {};
    raw.split(";").forEach(function (part) {
      const bits = part.split(":");
      const target = bits[0] === "s" ? offSources
        : (bits[0] === "t" ? offTypes : (bits[0] === "x" ? offStatus : null));
      if (!target) return;
      (bits[1] || "").split(",").forEach(function (id) { if (id) target[id] = true; });
    });
  }

  function saveFilters() {
    const value =
      "s:" + Object.keys(offSources).filter(function (k) { return offSources[k]; }).join(",") +
      ";t:" + Object.keys(offTypes).filter(function (k) { return offTypes[k]; }).join(",") +
      ";x:" + STATUS_KEYS.filter(function (k) { return offStatus[k]; }).join(",");
    try { localStorage.setItem(LS_KEY, value); } catch (e) { /* private mode */ }
    if (typeof window.mfSaveUserPref === "function") {
      const patch = {};
      patch[PREF_KEY] = value;
      window.mfSaveUserPref(patch);        // fire-and-forget, like the appearance prefs
    }
  }

  // ------------------------------------------------------------ chip helpers
  function sourceOn(id) { return !offSources[id]; }
  function typeOn(key) { return !offTypes[key]; }
  function statusOn(key) { return !offStatus[key]; }
  function adultWanted() { return typeOn("adult"); }

  function activeSources() {
    return sources.filter(function (s) { return s.enabled; });
  }

  /** Is this source switched ON in Settings? Unknown ids answer true: a card
      whose source the chip list has not heard of yet (a module registered
      between two requests) should be shown, not silently swallowed. */
  function sourceEnabled(id) {
    const src = sourceById(id);
    return src ? !!src.enabled : true;
  }

  function availableTypes() {
    const seen = {};
    sources.forEach(function (s) {
      if (!s.enabled) return;
      (s.types || []).forEach(function (ty) { seen[ty] = true; });
    });
    return seen;
  }

  /** The filter row: two dropdowns, "Sources" and "Type".
   *
   *  It used to render TWO variants of the same state -- a row of clickable
   *  pills, and, below 640px, two .mf-multiselect dropdowns -- with CSS
   *  choosing between them by width. That is two sets of markup, two sets of
   *  event handling and two sets of guard rails for one control, and on an
   *  instance with nine sources plus three type pills the desktop variant was a
   *  twelve-pill ribbon that wrapped to two lines and pushed the rows down the
   *  page. The dropdowns say the same thing in two controls and leave room for
   *  the settings button beside them, so they are now the only variant at every
   *  width and the pills are gone.
   *
   *  What was lost with the pills and is deliberately not rebuilt: the "· off"
   *  and "· offline" suffixes were pill text. "off" is moot (a disabled source
   *  is not listed at all any more, see below) and "offline" is still carried
   *  in each dropdown entry's own label. */
  function renderFilters() {
    const wrap = document.getElementById("feedFilters");
    if (!wrap) return;
    // renderFilters() runs again on every row response. Replacing innerHTML
    // while a dropdown is open would make it disappear mid interaction, so the
    // render is deferred until it closes.
    if (multiselectOpen()) { filtersDirty = true; return; }
    filtersDirty = false;
    wrap.innerHTML = renderFilterMenus(availableTypes());
    syncMultiselects();
  }

  // ------------------------------------------------------- filter dropdowns
  // Same state, same guard rails as the old pill row, but as two
  // .mf-multiselect dropdowns
  // (static/mf_multiselect.js owns open/close/label; this file only renders
  // the markup and reacts to its events).
  let filtersDirty = false;

  function multiselectOpen() {
    return !!document.querySelector("#feedFilters .mf-multiselect.is-open");
  }

  function msItem(value, label, checked, disabled, title, color) {
    const dot = color
      ? '<span class="feed-chip-dot" style="background:' + mfEscape(color) + '"></span>'
      : "";
    return '<label class="mf-multiselect-item' + (disabled ? " is-disabled" : "") +
      '"' + (title ? ' title="' + mfEscape(title) + '"' : "") + ">" +
      '<input type="checkbox" class="chb-main" value="' + mfEscape(value) + '"' +
      (checked ? " checked" : "") + (disabled ? " disabled" : "") + ">" +
      "<span>" + dot + mfEscape(label) + "</span></label>";
  }

  /** *allLabel* is optional and only shows when every entry is checked. The
   *  status menu's default state IS "everything checked", and "2 states" there
   *  is a number the reader has to decode into "nothing is filtered out". */
  function msRoot(kind, manyLabel, items, allLabel) {
    return '<div class="mf-multiselect feed-ms" data-mf-multiselect data-feed-kind="' +
      kind + '" data-none-label="' + mfEscape(HT("filter_none")) +
      '" data-many-label="' + mfEscape(manyLabel) + '"' +
      (allLabel ? ' data-all-label="' + mfEscape(allLabel) + '"' : "") +
      ' data-max-names="1">' +
      '<button type="button" class="mf-multiselect-trigger" aria-expanded="false" ' +
      'aria-haspopup="true"><span class="mf-multiselect-label"></span>' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/>' +
      "</svg></button>" +
      '<div class="mf-multiselect-dropdown">' + items + "</div></div>";
  }

  /** " · EN" for an English-only source, "" otherwise. A dropdown entry is a
      plain text label (msItem escapes it), so the marker has to be part of the
      string rather than a badge element. */
  function enSuffix(s) {
    return s && s.english_only ? " · EN" : "";
  }

  /** The two dropdowns, each preceded by its own label so the row reads
      "SOURCES [ … ]  TYPE [ … ]" the way the pill row did. */
  function renderFilterMenus(types) {
    let src = "";
    sources.forEach(function (s) {
      // A source switched off in Settings is left out entirely rather than
      // listed as unpickable: an entry you have to open a menu to discover does
      // nothing is worse than no entry. Settings -> Sources is where the switch
      // is and where the absence is explained.
      if (!s.enabled) return;
      const down = s.error || downIds.indexOf(s.id) !== -1;
      src += msItem(s.id, s.label + enSuffix(s) + (down ? " · " + HT("offline") : ""),
        sourceOn(s.id), false, "", down ? "" : (s.color || ""));
    });

    let ty = "";
    if (types.series) ty += msItem("series", HT("series"), typeOn("series"), false, "", "");
    if (types.movies) ty += msItem("movies", HT("movies"), typeOn("movies"), false, "", "");
    if (types.adult) {
      // With an age ceiling in force the 18+ source is never fetched at all
      // (routes/browse.py drops it before the request goes out), so the entry
      // is a locked fact rather than a toggle that does nothing. This is the one
      // disabled entry that earns its place -- unlike a switched-off source, an
      // absent 18+ entry would look like a bug to the account that set the
      // limit.
      const ceiling = parseInt(window.__HOME_MAX_FSK || "", 10);
      ty += (ceiling >= 0 && ceiling < 18)
        ? msItem("adult", "18+ · " + HT("off"), false, true,
            HT("mode_notice").replace("{}", String(ceiling)), "")
        : msItem("adult", "18+", typeOn("adult"), false, "", "");
    }

    // "Have I already got this?" -- a different question from "what is it",
    // which is why it is its own dropdown rather than two more entries in the
    // Type menu. Both entries are ON by default; switching one off removes the
    // cards that carry that badge from the discovery rows.
    const st =
      msItem("library", HT("status_library"), statusOn("library"), false,
             HT("status_library_hint"), "") +
      msItem("autosync", HT("status_autosync"), statusOn("autosync"), false,
             HT("status_autosync_hint"), "");

    // A caption and its menu travel in ONE box. They used to be siblings in
    // the wrapping row, so with three controls the line break fell between
    // "TYP" and its dropdown and left the caption stranded at the end of the
    // first line, above a menu it no longer looked like it belonged to. The
    // row still wraps -- it has to -- but now only BETWEEN groups.
    return '<div class="feed-filter-menus">' +
      msGroup(HT("sources"), msRoot("source", HT("many_sources"), src)) +
      msGroup(HT("type"), msRoot("type", HT("many_types"), ty)) +
      msGroup(HT("status"),
              msRoot("status", HT("many_status"), st, HT("status_all"))) +
      "</div>";
  }

  function msGroup(label, menu) {
    return '<div class="feed-filter-group">' +
      '<span class="feed-chip-label">' + mfEscape(label) + "</span>" + menu +
      "</div>";
  }

  /** Push the current filter state back into the checkboxes and re-compute the
      trigger labels. Also the "undo" for a rejected toggle. */
  function syncMultiselects() {
    if (!window.mfMultiSelect) return;
    document.querySelectorAll("#feedFilters .mf-multiselect[data-feed-kind]")
      .forEach(function (root) {
        const kind = root.dataset.feedKind;
        const on = kind === "source" ? sourceOn : (kind === "status" ? statusOn : typeOn);
        root.querySelectorAll('.mf-multiselect-dropdown input[type="checkbox"]')
          .forEach(function (box) {
            if (!box.disabled) box.checked = on(box.value);
          });
        window.mfMultiSelect.refresh(root);
      });
  }

  // The component fires this on every checkbox change, so the filter is saved
  // immediately (chips do the same). Saving on mf-multiselect-close instead
  // would lose a change if the user navigates away with the dropdown open.
  feed.addEventListener("mf-multiselect-change", function (ev) {
    const root = ev.target.closest &&
      ev.target.closest(".mf-multiselect[data-feed-kind]");
    if (!root || !window.mfMultiSelect) return;
    const kind = root.dataset.feedKind;
    const picked = {};
    window.mfMultiSelect.values(root).forEach(function (v) { picked[v] = true; });

    // Guard rails, same as the chips: never all sources off, never all types
    // off. Unlike a chip the checkbox has already flipped, so the rejected
    // state has to be put back visibly.
    //
    // The status menu is deliberately exempt: "hide what I already have AND
    // what is already syncing" is a sensible thing to ask for -- it is the
    // whole point of the filter -- and unlike an empty source or type list it
    // cannot empty the page on its own.
    if (kind !== "status" && Object.keys(picked).length === 0) {
      syncMultiselects();
      return;
    }

    if (kind === "status") {
      STATUS_KEYS.forEach(function (key) {
        if (picked[key]) delete offStatus[key]; else offStatus[key] = true;
      });
    } else if (kind === "source") {
      activeSources().forEach(function (s) {
        if (picked[s.id]) delete offSources[s.id]; else offSources[s.id] = true;
      });
    } else if (kind === "type") {
      const wasAdult = typeOn("adult");
      Object.keys(availableTypes()).forEach(function (key) {
        if (picked[key]) delete offTypes[key]; else offTypes[key] = true;
      });
      // The 18+ source is only fetched while it is on, so switching it on has
      // to go back to the server once (same as the chip handler).
      if (!wasAdult && typeOn("adult")) {
        saveFilters(); renderFilters(); window.reloadHomeFeed(); return;
      }
    } else {
      return;
    }
    saveFilters();
    renderFilters();                         // deferred while the menu is open
    renderRows();
  });

  // A deferred render has to happen as soon as the menu is gone again. The
  // close event only fires when something changed, so a plain open/close is
  // caught by the click/Escape fallbacks below.
  function flushFilters() {
    if (filtersDirty && !multiselectOpen()) renderFilters();
  }
  feed.addEventListener("mf-multiselect-close", function () {
    window.setTimeout(flushFilters, 0);
  });
  document.addEventListener("click", function () {
    window.setTimeout(flushFilters, 0);
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") window.setTimeout(flushFilters, 0);
  });

  // typeChip() and the .feed-chip click handler that went with it are gone: the
  // filter row is two dropdowns at every width now (see renderFilters), and the
  // mf-multiselect-change listener above is the single place that applies a
  // filter change. Keeping a second, pill-shaped path around for a control that
  // is no longer rendered is how the two drift apart -- the pill handler had
  // already accumulated a fix (the 18+ reload) that the dropdown path needed
  // separately.

  /** What the UpTime monitor knows, mirrored into the filter dropdowns. app.js
      calls this through markSourceChipsDown() so there is still exactly one
      place that polls /api/uptime/status. */
  window.mfFeedMarkDown = function (ids) {
    downIds = Array.isArray(ids) ? ids.slice() : [];
    if (sources.length) { renderFilters(); renderAlerts(); }
  };

  feed.addEventListener("click", function (ev) {
    const retry = ev.target.closest(".feed-alert-retry");
    if (retry) { window.reloadHomeFeed(); return; }
  });

  // ------------------------------------------------------------ source pill
  /** The same title from two sources is one card. Clicking its pill says
      which sources have it and opens the one you pick -- before, the extra
      sources were a tooltip and the card always opened the first one. */
  function openSourcePicker(pill) {
    closeSourcePicker();
    let entries;
    try { entries = JSON.parse(pill.dataset.also); } catch (e) { return; }
    if (!entries || entries.length < 2) return;
    const menu = document.createElement("div");
    menu.className = "browse-src-menu";
    menu.innerHTML = entries.map(function (e) {
      return '<button type="button" data-url="' + mfEscape(e.url) + '">' +
        mfEscape(e.label) + "</button>";
    }).join("");
    menu.addEventListener("click", function (ev) {
      const b = ev.target.closest("button");
      if (!b) return;
      ev.stopPropagation();
      closeSourcePicker();
      if (typeof openSeries === "function") openSeries(b.dataset.url);
    });
    pill.appendChild(menu);
    setTimeout(function () {
      document.addEventListener("click", closeSourcePicker, { once: true });
    }, 0);
  }

  function closeSourcePicker() {
    const open = feed.querySelector(".browse-src-menu");
    if (open && open.parentNode) open.parentNode.removeChild(open);
  }


  // ------------------------------------------------------------ layout
  /** Put the sections in the configured order and drop the ones switched
      off. The DOM order is the template's default; this rewrites it once per
      load, which is cheaper and far less fragile than rendering seven
      sections from JavaScript. */
  function applyLayout() {
    if (!layout.order || !layout.order.length) return;
    const empty = document.getElementById("feedEmpty");
    layout.order.forEach(function (row) {
      const grid = document.getElementById(ROW_GRIDS[row]);
      const section = grid && grid.closest(".browse-section");
      if (!section) return;
      if (layout.hidden.indexOf(row) !== -1) {
        section.style.display = "none";
        section.dataset.feedHidden = "1";
        return;
      }
      delete section.dataset.feedHidden;
      // Re-append inside the section's OWN tab pane, not into #homeFeed:
      // the personal rows live under Dashboard and the discovery rows under
      // Discover, and appending to the feed root would have pulled every row
      // out of its pane and stacked them all under the last one.
      (section.closest(".home-pane") || feed).appendChild(section);
    });
    // The empty state belongs to the discovery rows, so it stays last inside
    // whichever pane it was rendered into.
    if (empty) (empty.closest(".home-pane") || feed).appendChild(empty);
    applyRowHints();
  }

  /** Say where a row's content comes from, right in its heading -- "from your
      favourites", linking to the page that owns it. A row whose origin you
      cannot see is a row you cannot fix. */
  function applyRowHints() {
    const strings = window.__STARTPAGE_I18N || {};
    (layout.rows || []).forEach(function (meta) {
      const grid = document.getElementById(ROW_GRIDS[meta.id]);
      const section = grid && grid.closest(".browse-section");
      if (!section) return;
      const heading = section.querySelector(".browse-heading");
      if (!heading || heading.querySelector(".browse-heading-src")) return;
      const label = (strings.hints || {})[meta.hint];
      if (!label) return;
      const hint = document.createElement(meta.link ? "a" : "span");
      hint.className = "browse-heading-src";
      hint.textContent = label;
      if (meta.link) hint.href = meta.link;
      heading.appendChild(hint);
    });
  }

  // Rows that the Dashboard now shows as a CARD (see static/home_panels.js).
  // Their entries stay in the Start Page settings -- the data still feeds the
  // dashboard -- but the poster row would be the same list a second time,
  // half a screen further down.
  const DASH_CARD_ROWS = {
    gaps: true, upcoming: true,
    // Personal lists, now cards on the same grid (renderContinue /
    // renderWatchlist / renderNewLibrary in static/home_panels.js).
    "continue": true, watchlist: true, library: true,
  };

  function rowVisible(row) {
    if (DASH_CARD_ROWS[row]) return false;
    return layout.hidden.indexOf(row) === -1;
  }

  // ------------------------------------------------------------ rendering
  /** The cards a row actually shows.
   *
   *  The list it filters is a POOL (loadRow asks with pool=1), so hiding a
   *  source does not shorten the row -- the cards behind the cut move up and
   *  the row keeps its configured length for as long as there is anything
   *  left to show.
   *
   *  sourceEnabled() is checked as well as sourceOn(): a source switched off
   *  in Settings is not fetched at all, but a card could still be sitting in
   *  the pool from before the setting changed (the browse cache outlives the
   *  toggle by up to an hour), and that card must not be shown. */
  function visibleCards(row) {
    return (rows[row] || []).filter(function (item) {
      return sourceEnabled(item.source) && sourceOn(item.source) &&
        typeOn(item.media_type) && statusAllows(item);
    }).slice(0, ROW_MAX);
  }

  /** The status filter, for DISCOVERY rows only.
   *
   *  Deliberately not applied to the personal rows: "New in your library" is
   *  a list of things you have, and "Continue watching" is a list of things
   *  you have started -- filtering "already in the library" out of those does
   *  not narrow them, it empties them. The personal rows answer a question
   *  the user already asked by name; the discovery rows are the ones where
   *  "and skip what I've got" is a useful thing to say.
   *
   *  The two answers come from app.js (mfCardInLibrary / mfCardOnAutoSync) --
   *  the exact functions behind the two badges on the card, so the filter and
   *  the badge cannot disagree. Missing helpers mean app.js has not finished
   *  loading; showing the card is the safe answer there, and renderRows() runs
   *  again once the library index lands. */
  function statusAllows(item) {
    if (!offStatus.library && !offStatus.autosync) return true;   // both on
    if (offStatus.library && typeof window.mfCardInLibrary === "function" &&
        window.mfCardInLibrary(item)) {
      return false;
    }
    if (offStatus.autosync && typeof window.mfCardOnAutoSync === "function" &&
        window.mfCardOnAutoSync(item)) {
      return false;
    }
    return true;
  }

  function showSection(row, visible) {
    const grid = document.getElementById(ROW_GRIDS[row]);
    const section = grid && grid.closest(".browse-section");
    if (section) section.style.display = (visible && rowVisible(row)) ? "" : "none";
    return grid;
  }

  /** The origin label the row no longer carries. Added after
      renderBrowseCards() so app.js stays the single owner of what a browse
      card looks like. */
  function addSourcePills(grid, list) {
    if (grid.children.length !== list.length) return;
    for (let i = 0; i < list.length; i++) {
      const item = list[i];
      const src = sourceById(item.source);
      const also = (item.also || []).filter(function (a) { return sourceOn(a.source); });
      const pill = document.createElement("span");
      pill.className = "browse-src-pill";
      pill.textContent = (src ? src.label : item.source) + (also.length ? " +" + also.length : "");
      if (also.length) {
        pill.classList.add("is-multi");
        pill.dataset.also = JSON.stringify(
          [{ label: src ? src.label : item.source, url: item.url || "" }].concat(
            also.map(function (a) { return { label: a.label, url: a.url }; })));
        pill.title = HT("also_on");
      }
      if (also.length) {
        // The pill sits inside the card, and the card's own onclick opens the
        // series. A delegated listener on #homeFeed would fire *after* that,
        // so the picker is bound here and stops the click where it happens.
        pill.addEventListener("click", function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          openSourcePicker(pill);
        });
      }
      // Into the text column when there is one. On a poster the pill is
      // absolutely positioned against .browse-card (which is the positioned
      // ancestor either way), so this changes nothing there -- but in a list
      // row it makes the pill a sibling of the title and genre, which is
      // where it belongs and where it lines up without a magic indent.
      const card = grid.children[i];
      (card.querySelector(".browse-info") || card).appendChild(pill);
    }
  }

  function sourceById(id) {
    for (let i = 0; i < sources.length; i++) if (sources[i].id === id) return sources[i];
    return null;
  }

  function renderRows() {
    DISCOVERY_ROWS.forEach(function (row) {
      const list = visibleCards(row);
      // A row that has not been fetched yet stays visible with its skeletons:
      // hiding it would pull it out of the IntersectionObserver's way and it
      // would never load at all.
      const waiting = rowState[row] === "pending" || rowState[row] === "loading";
      const grid = showSection(row, list.length > 0 || waiting);
      if (!grid) return;
      if (!list.length) { if (!waiting) grid.innerHTML = ""; return; }
      renderBrowseCards(grid, list, {
        skipTmdb: list.every(function (i) { return i.media_type === "adult"; }),
      });
      addSourcePills(grid, list);
    });
    renderPersonal();
    renderAlerts();

    const anyVisible = Object.keys(ROW_GRIDS).some(function (row) {
      const g = document.getElementById(ROW_GRIDS[row]);
      const s = g && g.closest(".browse-section");
      return s && s.style.display !== "none";
    });
    const empty = document.getElementById("feedEmpty");
    if (empty) {
      empty.style.display = anyVisible ? "none" : "";
      // "Nothing matches your filters" and "every source is down" are not the
      // same message, and only one of them is something the user can fix by
      // clicking a chip.
      empty.textContent = feedError ? feedError : HT("empty_filters");
    }
  }

  // ------------------------------------------------------------ alerts
  function renderAlerts() {
    const wrap = document.getElementById("feedAlerts");
    if (!wrap) return;
    const broken = sources.filter(function (s) {
      return s.enabled && (s.error || downIds.indexOf(s.id) !== -1);
    });
    // The Dashboard's Sources card says the same thing in a quieter place, so
    // it is told here -- "down" means the same in both, including the sources
    // that only failed on this page load (downIds).
    if (typeof window.mfHomeDashFeed === "function") {
      window.mfHomeDashFeed(sources.map(function (s) {
        return Object.assign({}, s, {
          error: s.error || (downIds.indexOf(s.id) !== -1 ? "down" : ""),
        });
      }), personal);
    }
    if (!broken.length && !feedError) { wrap.innerHTML = ""; return; }
    const names = broken.map(function (s) { return s.label; }).join(", ");
    const text = feedError || HT("source_down").replace("{}", names);
    wrap.innerHTML =
      '<div class="feed-alert">' +
      '<span class="feed-alert-text">' + mfEscape(text) + "</span>" +
      '<button type="button" class="feed-alert-retry">' +
      mfEscape(HT("try_again")) + "</button></div>";
  }

  // ------------------------------------------------------------ personal rows
  function fauxArt(name) {
    // Same hashing the Library page uses for its placeholder art, so a title
    // has the same colour in both places.
    let hash = 0;
    const text = String(name || "");
    for (let i = 0; i < text.length; i++) hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
    const h1 = hash % 360, h2 = (h1 + 48) % 360;
    return '<span class="home-pcard-faux" style="background:linear-gradient(155deg,hsl(' +
      h1 + ',55%,22%),hsl(' + h2 + ',55%,14%))"></span>';
  }

  function pcard(inner, cls) {
    return '<div class="home-pcard' + (cls ? " " + cls : "") + '">' + inner + "</div>";
  }

  function renderPersonal() {
    // Continue watching. When the account is linked to a Jellyfin/Plex user,
    // these entries came from that server (personal.continue_source says
    // which) and MediaForge has no file to hand its own player -- the card
    // opens the media server instead, and says so.
    const cont = personal.continue || [];
    const remoteServer = personal.continue_source && personal.continue_source !== "local"
      ? personal.continue_source : "";
    let grid = showSection("continue", cont.length > 0);
    if (grid && cont.length) {
      grid.innerHTML = cont.map(function (it, n) {
        const sub = it.is_movie
          ? HT("movie")
          : (it.season ? "S" + it.season + (it.episode ? " · " + HT("episode") + " " + it.episode : "")
                       : (it.episode ? HT("episode") + " " + it.episode : ""));
        const art = it.poster_url
          ? '<img src="' + mfEscape(it.poster_url) + '" alt="" loading="lazy">'
          : fauxArt(it.title);
        if (it.remote) {
          // A link, not a button: it leaves MediaForge, so it should behave
          // like every other link (middle-click, "open in new tab", the
          // status bar showing where it goes).
          return pcard(
            '<a class="home-pcard-hit" href="' + mfEscape(it.open_url || "#") +
            '" target="_blank" rel="noopener noreferrer">' + art +
            '<span class="home-pcard-play" aria-hidden="true">' +
            '<svg viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20"/></svg></span>' +
            '<span class="home-pcard-bar"><i style="width:' +
            Math.max(2, Math.min(100, it.percent || 0)) + '%"></i></span>' +
            '<span class="home-pcard-badge">' + mfEscape(remoteServer) + "</span>" +
            '<span class="home-pcard-title">' + mfEscape(it.title) + "</span>" +
            '<span class="home-pcard-sub">' + mfEscape(sub) + " · " +
            mfEscape(remaining(it)) + "</span></a>", it.poster_url ? "has-art" : "");
        }
        return pcard(
          '<button type="button" class="home-pcard-hit" data-play="' + n + '">' +
          art +
          '<span class="home-pcard-play" aria-hidden="true">' +
          '<svg viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20"/></svg></span>' +
          '<span class="home-pcard-bar"><i style="width:' +
          Math.max(2, Math.min(100, it.percent || 0)) + '%"></i></span>' +
          '<span class="home-pcard-title">' + mfEscape(it.title) + "</span>" +
          '<span class="home-pcard-sub">' + mfEscape(sub) + " · " +
          mfEscape(remaining(it)) + "</span></button>",
          it.poster_url ? "has-art" : "");
      }).join("");
      grid.querySelectorAll("[data-play]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          const it = cont[parseInt(btn.dataset.play, 10)];
          if (!it) return;
          if (typeof openPlayer === "function") {
            openPlayer(it.path, it.title, it.position || 0);
          } else if (typeof showToast === "function") {
            showToast(HT("player_loading"));
          }
        });
      });
    }

    // Watchlist — real posters, so these are ordinary browse cards.
    const wl = personal.watchlist || [];
    grid = showSection("watchlist", wl.length > 0);
    if (grid && wl.length) {
      renderBrowseCards(grid, wl.map(function (f) {
        return { title: f.title, url: f.url, poster_url: f.poster_url, genre: f.provider || "" };
      }), {});
    }

    // New in your library
    const lib = personal.library || [];
    grid = showSection("library", lib.length > 0);
    if (grid && lib.length) {
      grid.innerHTML = lib.map(function (it) {
        const sub = it.is_movie ? HT("movie")
          : it.episodes + " " + HT("episodes_short");
        // The server fills poster_url from the TMDB cache where it can
        // (_feed_library_posters); the generated colour block is the
        // fallback for titles TMDB has never been asked about.
        const art = it.poster_url
          ? '<img src="' + mfEscape(it.poster_url) + '" alt="" loading="lazy">'
          : fauxArt(it.title);
        return pcard(
          '<a class="home-pcard-hit" href="/library">' + art +
          '<span class="home-pcard-title">' + mfEscape(it.title) + "</span>" +
          '<span class="home-pcard-sub">' + mfEscape(sub) + "</span></a>",
          it.poster_url ? "has-art" : "");
      }).join("");
    }

    // Because you watched X. The only row on this page that GUESSES, so it
    // names its seed in the section title -- a suggestion nobody can trace
    // back to a reason is a suggestion nobody trusts, and one bad card should
    // read as "wrong guess" rather than "why is this here at all".
    const because = personal.because || [];
    grid = showSection("because", because.length > 0);
    if (grid && because.length) {
      // The heading ships with a {} placeholder (index.html); fill it with
      // the seed. Written via textContent, not innerHTML: the seed is a title
      // from the library and therefore not ours.
      const head = document.querySelector('[data-feed-heading="because"]');
      if (head && personal.because_seed) {
        const template = head.getAttribute("data-template") || head.textContent;
        head.setAttribute("data-template", template);
        head.textContent = template.replace("{}", personal.because_seed);
      }
      grid.innerHTML = because.map(function (it) {
        const art = it.poster_url
          ? '<img src="' + mfEscape(it.poster_url) + '" alt="" loading="lazy">'
          : fauxArt(it.title);
        const shared = (it.shared || []).join(", ");
        return pcard(
          '<a class="home-pcard-hit" href="/library">' + art +
          '<span class="home-pcard-title">' + mfEscape(it.title) + "</span>" +
          (shared ? '<span class="home-pcard-sub">' + mfEscape(shared) + "</span>" : "") +
          "</a>",
          it.poster_url ? "has-art" : "");
      }).join("");
    }

    // Gaps — the one row that asks something of you. Each card names what is
    // missing and links straight into the search for that title, so "3 of 12
    // missing" is one click from being fixed rather than a number to worry
    // about. The slot list is capped by the server at 12.
    const gaps = personal.gaps || [];
    grid = showSection("gaps", gaps.length > 0);
    if (grid && gaps.length) {
      grid.innerHTML = gaps.map(function (it) {
        const slots = (it.missing || []).slice(0, 4).join(", ");
        const more = (it.missing_count || 0) > 4
          ? " +" + ((it.missing_count || 0) - 4) : "";
        const art = it.poster_url
          ? '<img src="' + mfEscape(it.poster_url) + '" alt="" loading="lazy">'
          : fauxArt(it.title);
        // The ignore button is a SIBLING of the card button, not a child of
        // it: the card body is itself a <button>, so nesting would be invalid
        // markup and the outer click would fire on the way out anyway.
        return pcard(
          '<button type="button" class="home-pcard-hit" data-gap="' +
          mfEscape(it.title) + '">' + art +
          '<span class="home-pcard-flag">' +
          mfEscape(HT("missing_count").replace("{}", String(it.missing_count || 0))) +
          "</span>" +
          '<span class="home-pcard-title">' + mfEscape(it.title) + "</span>" +
          '<span class="home-pcard-sub">' + mfEscape(slots + more) + "</span></button>" +
          '<button type="button" class="home-pcard-dismiss" data-gap-ignore="' +
          mfEscape(it.folder || it.title) + '" title="' +
          mfEscape(HT("gap_ignore")) + '" aria-label="' +
          mfEscape(HT("gap_ignore")) + '">×</button>',
          it.poster_url ? "has-art is-gap" : "is-gap");
      }).join("");
      grid.querySelectorAll("[data-gap]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          // Hand the title to the ordinary search: it is the one place that
          // knows which of the enabled sources actually has this series, and
          // duplicating that decision here would be a second, worse answer.
          const input = document.getElementById("searchInput");
          if (input) input.value = btn.dataset.gap;
          if (typeof doSearch === "function") doSearch();
        });
      });
      // "Not interested in this one." Writes the same media_ignored entry the
      // statistics page uses (POST /api/media/ignore with the "__all__" slot),
      // so a series dismissed here is also gone from /stats' incomplete list
      // and can be taken back there -- one ignore list, two places that respect
      // it, rather than a home-page-only hide nobody can undo.
      grid.querySelectorAll("[data-gap-ignore]").forEach(function (btn) {
        btn.addEventListener("click", async function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          const folder = btn.dataset.gapIgnore;
          btn.disabled = true;
          try {
            const resp = await fetch("/api/media/ignore", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ items: [{ folder: folder, title: folder, all: true }] }),
            });
            const data = await resp.json().catch(function () { return {}; });
            if (!resp.ok || data.error) throw new Error(data.error || ("HTTP " + resp.status));
            // Drop it from the local copy and re-render rather than refetching:
            // the server already agreed, and /api/home-feed/personal reads the
            // whole library to rebuild this row.
            personal.gaps = (personal.gaps || []).filter(function (g) {
              return (g.folder || g.title) !== folder;
            });
            renderPersonal();
            if (typeof window.showToast === "function") window.showToast(HT("gap_ignored"));
          } catch (err) {
            btn.disabled = false;
            if (typeof window.showToast === "function") window.showToast(HT("gap_ignore_failed"));
          }
        });
      });
    }

    // Airing next
    const up = personal.upcoming || [];
    grid = showSection("upcoming", up.length > 0);
    if (grid && up.length) {
      grid.innerHTML = up.map(function (ev) {
        const art = ev.poster_url
          ? '<img src="' + mfEscape(ev.poster_url) + '" alt="" loading="lazy">'
          : fauxArt(ev.title);
        const ep = ev.is_movie ? HT("movie")
          : (ev.season ? "S" + ev.season + "E" + (ev.episode || "") : "");
        return pcard(
          '<a class="home-pcard-hit" href="/calendar">' + art +
          '<span class="home-pcard-title">' + mfEscape(ev.title) + "</span>" +
          '<span class="home-pcard-sub">' + mfEscape(formatDate(ev.air_date)) +
          (ep ? " · " + mfEscape(ep) : "") + "</span></a>", "has-art");
      }).join("");
    }

    // The Dashboard's gaps / sources / calendar cards are built from exactly
    // this data, so they are handed it here rather than fetching it again.
    if (typeof window.mfHomeDashFeed === "function") {
      window.mfHomeDashFeed(sources, personal);
    }

    // Every personal row lives in the Dashboard pane, so this is the moment
    // its "nothing here yet" state can be decided honestly.
    if (typeof window.mfHomeSyncDashEmpty === "function") window.mfHomeSyncDashEmpty();
  }

  function remaining(item) {
    const left = Math.max(0, (item.duration || 0) - (item.position || 0));
    if (!left) return "";
    const mins = Math.round(left / 60);
    if (mins < 60) return HT("minutes_left").replace("{}", String(mins));
    const hours = Math.floor(mins / 60);
    return HT("hours_left").replace("{}", hours + ":" + String(mins % 60).padStart(2, "0"));
  }

  function formatDate(iso) {
    try {
      const d = new Date(iso + "T00:00:00");
      return d.toLocaleDateString(window.mfLocale ? window.mfLocale() : "en-US",
                                  { weekday: "short", day: "2-digit", month: "2-digit" });
    } catch (e) { return iso; }
  }

  // ------------------------------------------------------------ loading
  //
  // One request per row, not one for the page.
  //
  // /api/home-feed still exists and still answers everything at once, but the
  // home page no longer uses it: the payload was only as fast as the slowest
  // site in it, so a single unresponsive source left the whole page on
  // skeletons -- and every row below the fold was scraped whether or not
  // anybody ever scrolled that far. The layout comes from
  // /api/home-feed/sources (a settings read, no scraping), the rows follow one
  // by one, and rows that start off-screen wait for an IntersectionObserver.
  let loadedAt = 0;
  let inFlight = false;
  const rowState = {};                     // row -> "pending"|"loading"|"done"
  let rowObserver = null;

  /** Cross-row dedupe, which the server can no longer do for us: with one
      request per row, no single response knows what another row already
      showed. Runs on the client because this is the side that ends up holding
      all of them. Earlier rows win -- "New this week" is a stronger statement
      about a title than "Movies". */
  function dedupeRows() {
    const seen = {};
    DISCOVERY_ROWS.forEach(function (row) {
      const list = rows[row] || [];
      rows[row] = list.filter(function (item) {
        const key = (item.key || item.title || "") + "|" + (item.media_type || "");
        if (seen[key]) return false;
        seen[key] = true;
        return true;
      });
    });
  }

  // See loadRow() below: how often and how fast a row that reported "still
  // warming up" is asked again.
  const ROW_RETRY_MS = 4000;
  const ROW_MAX_RETRIES = 6;
  const rowRetries = {};

  async function loadRow(row) {
    if (rowState[row] === "loading" || rowState[row] === "done") return;
    rowState[row] = "loading";
    const grid = showSection(row, true);
    if (grid && !grid.children.length) renderSkeletons(grid, 12);
    try {
      // pool=1: ask for MORE cards than the row shows. Switching a source chip
      // off filters client-side, and without a reserve the row simply got
      // shorter -- the configured cards-per-row is a promise about the layout,
      // not about how many sources happen to be on. visibleCards() slices back
      // down to ROW_MAX, so the surplus only ever moves up into gaps.
      const resp = await fetch("/api/home-feed/row/" + encodeURIComponent(row) +
                               "?adult=" + (adultWanted() ? "1" : "0") +
                               "&pool=1&limit=" + ROW_MAX);
      const data = await resp.json();
      // Every row response carries the full source list, including the
      // media types that row's sources publish -- the type chips cannot be
      // built from /api/home-feed/sources alone, which does not scrape and so
      // does not know them.
      if (Array.isArray(data.sources) && data.sources.length) {
        sources = data.sources;
        renderFilters();
      }
      // The server tells us how many of the pool to actually show, so the two
      // sides cannot disagree about the cards-per-row setting.
      if (typeof data.limit === "number" && data.limit > 0) ROW_MAX = data.limit;
      rows[row] = (data.rows || {})[row] || [];
      rowState[row] = "done";
      // A source whose server-side cache was cold is still being scraped; the
      // server says so instead of pretending the source is broken. Ask again
      // shortly so its cards appear on their own, rather than only after the
      // next full page load. Bounded, so a source that never finishes cannot
      // turn this into an endless poll.
      if (Array.isArray(data.pending) && data.pending.length &&
          (rowRetries[row] || 0) < ROW_MAX_RETRIES) {
        rowRetries[row] = (rowRetries[row] || 0) + 1;
        setTimeout(function () {
          if (rowState[row] === "done") { rowState[row] = "pending"; loadRow(row); }
        }, ROW_RETRY_MS);
      } else if (!Array.isArray(data.pending) || !data.pending.length) {
        rowRetries[row] = 0;
      }
    } catch (err) {
      rows[row] = [];
      rowState[row] = "pending";           // the retry button may try again
      feedError = HT("feed_failed");
    }
    dedupeRows();
    renderRows();
    applyUptimeStatus();
    prewarmRowMetadata(row);
  }

  /** Resolve CineInfo/TMDB data for the cards this row is HOLDING but not
   *  showing.
   *
   *  The row is backed by a reserve pool (loadRow asks with pool=1) so hiding a
   *  source refills it instead of shortening it. Those replacement cards used
   *  to arrive blank -- no genres, no rating, no age rating -- because the
   *  enrichment is driven by an IntersectionObserver on cards that are on the
   *  page, and a card in the reserve is not. So every filter switch showed a
   *  row of half-empty cards for as long as a batch took.
   *
   *  Asking for the reserve's metadata now means a filter switch is a
   *  re-render. Only the surplus is prewarmed: the visible cards are already
   *  being enriched by the observer, and asking for them here would double the
   *  requests to save nothing. Adult cards are skipped for the same reason
   *  renderRows() passes skipTmdb for them -- they are not in TMDB.
   */
  function prewarmRowMetadata(row) {
    if (typeof window.mfPrewarmTmdb !== "function") return;
    const pool = rows[row] || [];
    if (pool.length <= ROW_MAX) return;      // nothing held back
    const titles = [];
    pool.slice(ROW_MAX).forEach(function (item) {
      if (item.media_type === "adult") return;
      if (item.title) titles.push(item.title);
    });
    // Fire-and-forget: nothing on this page waits for it, and a failure just
    // leaves those cards to the ordinary lazy path.
    window.mfPrewarmTmdb(titles);
  }

  /** Rows that are already on screen load now; the rest load when they are
      scrolled to. A home page nobody scrolls should not scrape five sites. */
  function scheduleRows() {
    if (rowObserver) { rowObserver.disconnect(); rowObserver = null; }
    const pending = DISCOVERY_ROWS.filter(function (row) {
      return rowVisible(row) && rowState[row] !== "done";
    });
    if (!pending.length) return;
    if (!("IntersectionObserver" in window)) {
      pending.forEach(loadRow);             // old browser: behave as before
      return;
    }
    rowObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        const row = entry.target.dataset.feedRow;
        rowObserver.unobserve(entry.target);
        if (row) loadRow(row);
      });
    }, { rootMargin: "400px 0px" });        // start before it is actually seen
    pending.forEach(function (row) {
      const grid = document.getElementById(ROW_GRIDS[row]);
      const section = grid && grid.closest(".browse-section");
      if (!section) return;
      rowState[row] = "pending";
      if (grid && !grid.children.length) renderSkeletons(grid, 12);
      section.style.display = "";
      rowObserver.observe(section);
    });
  }

  async function load() {
    if (inFlight) return;
    if (loadedAt && Date.now() - loadedAt < RELOAD_AFTER) return;
    inFlight = true;
    feedError = "";
    DISCOVERY_ROWS.forEach(function (row) { rowState[row] = "pending"; });

    try {
      // The badges need their own data before any card is built — same order
      // loadAniworldBrowse() uses on the classic page. /api/home-feed/sources
      // reads settings only, so this first hop is fast even when every site
      // is down.
      const [, , resp] = await Promise.all([
        loadDownloadedFolders(),
        loadAutoSyncJobs(),
        fetch("/api/home-feed/sources"),
      ]);
      const data = await resp.json();
      sources = (Array.isArray(data.sources) ? data.sources : []).map(function (s) {
        // No types yet (that would need a scrape); the first row response
        // fills them in and re-renders the chips.
        return Object.assign({ types: [] }, s);
      });
      const config = data.config;
      if (config) {
        layout = {
          order: config.order || [],
          hidden: config.hidden || [],
          limit: config.limit || 30,
          rows: config.rows || [],
        };
        ROW_MAX = layout.limit;
        // A user who never touched a chip follows the instance default the
        // admin set under Settings -> Start Page.
        if (!hasStoredFilters) {
          offSources = {};
          offTypes = {};
          // No instance default for the status filter on purpose: "hide what
          // I already have" is a statement about ONE account's library, and an
          // admin cannot make it for everybody. Both entries start on.
          offStatus = {};
          (config.sources_off || []).forEach(function (id) { offSources[id] = true; });
          (config.types_off || []).forEach(function (ty) { offTypes[ty] = true; });
        }
        applyLayout();
        // The toolbar's mode buttons follow the SERVER's answer, not the
        // preference the page was rendered with: those two disagree for as
        // long as another device (or a failed write) says otherwise, and the
        // one that decides what is in the rows is the server.
        if (typeof window.mfHomeApplyMode === "function") {
          window.mfHomeApplyMode(config.mode || "", config.max_fsk || "",
                                 !!config.kids_enabled, config.kids_max_fsk || "");
        }
      }
      loadedAt = Date.now();
    } catch (err) {
      feedError = HT("feed_failed");
      sources = sources || [];
      rows = {};
      loadedAt = 0;                        // let the retry button try again
    } finally {
      inFlight = false;
    }

    renderFilters();
    scheduleRows();

    // Personal rows are one independent request: they read local data
    // (library, favourites, calendar) and must not wait for any site at all.
    try {
      const presp = await fetch("/api/home-feed/personal");
      personal = await presp.json();
      renderPersonal();
    } catch (e) {
      personal = {};
    }
  }

  /** A series the user dismissed on the Dashboard's gaps card. Kept in step
      here too, or the next renderPersonal() would put it back. */
  window.mfFeedDropGap = function (folder) {
    personal.gaps = (personal.gaps || []).filter(function (g) {
      return (g.folder || g.title) !== folder;
    });
  };

  window.reloadHomeFeed = function () {
    loadedAt = 0;
    DISCOVERY_ROWS.forEach(function (row) { delete rowState[row]; });
    load();
  };

  loadFilters();
  load();

  // The Start Page modal (openStartPageModal and friends) used to live here.
  // It moved to static/start_page.js, which is the file that owns the form
  // inside it -- and, unlike this one, is loaded on BOTH home page layouts.
  // The classic page needs the modal too: /settings redirects a non-admin, so
  // it is the only place a normal account can switch layouts at all.

})();
