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
  const I18N = window.__HOME_I18N || {};
  function HT(key) { return I18N[key] || key; }

  const DISCOVERY_ROWS = ["new", "popular", "movies"];
  const PERSONAL_ROWS = ["continue", "watchlist", "library", "upcoming"];
  const ROW_GRIDS = {
    continue: "feedContinueGrid",
    watchlist: "feedWatchlistGrid",
    library: "feedLibraryGrid",
    upcoming: "feedUpcomingGrid",
    new: "feedNewGrid",
    popular: "feedPopularGrid",
    movies: "feedMoviesGrid",
  };
  const ROW_MAX = 30;
  const PREF_KEY = "home_feed_filters";
  const LS_KEY = "mf-home-filters";
  const RELOAD_AFTER = 3600000;            // 1 h, same as the server-side cache

  let sources = [];                        // [{id,label,color,enabled,types,error}]
  let rows = {};                           // row -> [card]
  let personal = {};
  let offSources = {};                     // id -> true when switched off
  let offTypes = { adult: true };          // 18+ starts off
  let feedError = "";
  let downIds = [];                        // reported down by the UpTime monitor

  // ------------------------------------------------------------ preferences
  // Which chips are off is a per-ACCOUNT preference (same reasoning as the
  // library layout): a filter that resets on every device is a filter the
  // user sets again every day. localStorage stays as the fallback for the
  // logged-out / auth-disabled case.
  function loadFilters() {
    let raw = "";
    const prefs = window._USER_PREFS || {};
    if (typeof prefs[PREF_KEY] === "string") {
      raw = prefs[PREF_KEY];
    } else {
      try { raw = localStorage.getItem(LS_KEY) || ""; } catch (e) { raw = ""; }
    }
    if (!raw) return;
    offSources = {};
    offTypes = {};
    raw.split(";").forEach(function (part) {
      const bits = part.split(":");
      const target = bits[0] === "s" ? offSources : (bits[0] === "t" ? offTypes : null);
      if (!target) return;
      (bits[1] || "").split(",").forEach(function (id) { if (id) target[id] = true; });
    });
  }

  function saveFilters() {
    const value =
      "s:" + Object.keys(offSources).filter(function (k) { return offSources[k]; }).join(",") +
      ";t:" + Object.keys(offTypes).filter(function (k) { return offTypes[k]; }).join(",");
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
  function adultWanted() { return typeOn("adult"); }

  function activeSources() {
    return sources.filter(function (s) { return s.enabled; });
  }

  function availableTypes() {
    const seen = {};
    sources.forEach(function (s) {
      if (!s.enabled) return;
      (s.types || []).forEach(function (ty) { seen[ty] = true; });
    });
    return seen;
  }

  /** One chip row for both jobs. There used to be two rows stacked on top of
      each other -- a read-only status row and a clickable filter row, both
      listing the same five names, which is a puzzle rather than a control. */
  function renderFilters() {
    const wrap = document.getElementById("feedFilters");
    if (!wrap) return;
    const types = availableTypes();
    let html = '<span class="feed-chip-label">' + escapeHtml(HT("sources")) + "</span>";

    sources.forEach(function (s) {
      if (!s.enabled) {
        // Switched off in Settings → shown, but as a fact, not as a filter.
        html += '<span class="feed-chip is-disabled" title="' +
          escapeHtml(HT("disabled_in_settings")) + '">' +
          '<span class="feed-chip-dot"></span>' + escapeHtml(s.label) +
          ' · ' + escapeHtml(HT("off")) + "</span>";
        return;
      }
      const down = s.error || downIds.indexOf(s.id) !== -1;
      const on = sourceOn(s.id);
      const dot = s.color
        ? ' style="background:' + escapeHtml(s.color) + '"'
        : "";
      html += '<button type="button" class="feed-chip' + (on ? " is-on" : "") +
        (down ? " is-down" : "") + '" data-kind="source" data-value="' +
        escapeHtml(s.id) + '" aria-pressed="' + (on ? "true" : "false") + '">' +
        '<span class="feed-chip-dot"' + (down ? "" : dot) + "></span>" +
        escapeHtml(s.label) +
        (down ? ' · ' + escapeHtml(HT("offline")) : "") +
        "</button>";
    });

    html += '<span class="feed-chip-label feed-chip-label--split">' +
      escapeHtml(HT("type")) + "</span>";
    if (types.series) {
      html += typeChip("series", HT("series"));
    }
    if (types.movies) {
      html += typeChip("movies", HT("movies"));
    }
    if (types.adult) {
      html += typeChip("adult", "18+");
    }
    wrap.innerHTML = html;
  }

  function typeChip(key, label) {
    const on = typeOn(key);
    return '<button type="button" class="feed-chip' + (on ? " is-on" : "") +
      '" data-kind="type" data-value="' + escapeHtml(key) + '" aria-pressed="' +
      (on ? "true" : "false") + '">' + escapeHtml(label) + "</button>";
  }

  /** What the UpTime monitor knows, mirrored into the chips. app.js calls
      this through markSourceChipsDown() so there is still exactly one place
      that polls /api/uptime/status. */
  window.mfFeedMarkDown = function (ids) {
    downIds = Array.isArray(ids) ? ids.slice() : [];
    if (sources.length) { renderFilters(); renderAlerts(); }
  };

  feed.addEventListener("click", function (ev) {
    const retry = ev.target.closest(".feed-alert-retry");
    if (retry) { window.reloadHomeFeed(); return; }

    const btn = ev.target.closest(".feed-chip");
    if (!btn || btn.tagName !== "BUTTON") return;
    const kind = btn.dataset.kind;
    const value = btn.dataset.value;

    if (kind === "source") {
      const on = sourceOn(value);
      // Never leave every source off — that is an empty page, not a filter.
      if (on && activeSources().filter(function (s) {
        return s.id !== value && sourceOn(s.id);
      }).length === 0) return;
      if (on) offSources[value] = true; else delete offSources[value];
    } else if (kind === "type") {
      const on = typeOn(value);
      const others = Object.keys(availableTypes()).filter(function (k) {
        return k !== value && typeOn(k);
      });
      if (on && others.length === 0) return;
      if (on) offTypes[value] = true; else delete offTypes[value];
      // The 18+ source is only ever *fetched* when the chip is on, so turning
      // it on has to go back to the server once.
      if (value === "adult" && !offTypes.adult) { saveFilters(); renderFilters(); reload(); return; }
    } else {
      return;
    }
    saveFilters();
    renderFilters();
    renderRows();
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
      return '<button type="button" data-url="' + escapeHtml(e.url) + '">' +
        escapeHtml(e.label) + "</button>";
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

  // ------------------------------------------------------------ rendering
  function visibleCards(row) {
    return (rows[row] || []).filter(function (item) {
      return sourceOn(item.source) && typeOn(item.media_type);
    }).slice(0, ROW_MAX);
  }

  function showSection(row, visible) {
    const grid = document.getElementById(ROW_GRIDS[row]);
    const section = grid && grid.closest(".browse-section");
    if (section) section.style.display = visible ? "" : "none";
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
      grid.children[i].appendChild(pill);
    }
  }

  function sourceById(id) {
    for (let i = 0; i < sources.length; i++) if (sources[i].id === id) return sources[i];
    return null;
  }

  function renderRows() {
    DISCOVERY_ROWS.forEach(function (row) {
      const list = visibleCards(row);
      const grid = showSection(row, list.length > 0);
      if (!grid) return;
      if (!list.length) { grid.innerHTML = ""; return; }
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
    if (!broken.length && !feedError) { wrap.innerHTML = ""; return; }
    const names = broken.map(function (s) { return s.label; }).join(", ");
    const text = feedError || HT("source_down").replace("{}", names);
    wrap.innerHTML =
      '<div class="feed-alert">' +
      '<span class="feed-alert-text">' + escapeHtml(text) + "</span>" +
      '<button type="button" class="feed-alert-retry">' +
      escapeHtml(HT("try_again")) + "</button></div>";
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
    // Continue watching
    const cont = personal.continue || [];
    let grid = showSection("continue", cont.length > 0);
    if (grid && cont.length) {
      grid.innerHTML = cont.map(function (it, n) {
        const sub = it.is_movie
          ? HT("movie")
          : (it.season ? "S" + it.season + (it.episode ? " · " + HT("episode") + " " + it.episode : "")
                       : (it.episode ? HT("episode") + " " + it.episode : ""));
        return pcard(
          '<button type="button" class="home-pcard-hit" data-play="' + n + '">' +
          fauxArt(it.title) +
          '<span class="home-pcard-play" aria-hidden="true">' +
          '<svg viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20"/></svg></span>' +
          '<span class="home-pcard-bar"><i style="width:' +
          Math.max(2, Math.min(100, it.percent || 0)) + '%"></i></span>' +
          '<span class="home-pcard-title">' + escapeHtml(it.title) + "</span>" +
          '<span class="home-pcard-sub">' + escapeHtml(sub) + " · " +
          escapeHtml(remaining(it)) + "</span></button>");
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
        return pcard(
          '<a class="home-pcard-hit" href="/library">' +
          fauxArt(it.title) +
          '<span class="home-pcard-title">' + escapeHtml(it.title) + "</span>" +
          '<span class="home-pcard-sub">' + escapeHtml(sub) + "</span></a>");
      }).join("");
    }

    // Airing next
    const up = personal.upcoming || [];
    grid = showSection("upcoming", up.length > 0);
    if (grid && up.length) {
      grid.innerHTML = up.map(function (ev) {
        const art = ev.poster_url
          ? '<img src="' + escapeHtml(ev.poster_url) + '" alt="" loading="lazy">'
          : fauxArt(ev.title);
        const ep = ev.is_movie ? HT("movie")
          : (ev.season ? "S" + ev.season + "E" + (ev.episode || "") : "");
        return pcard(
          '<a class="home-pcard-hit" href="/calendar">' + art +
          '<span class="home-pcard-title">' + escapeHtml(ev.title) + "</span>" +
          '<span class="home-pcard-sub">' + escapeHtml(formatDate(ev.air_date)) +
          (ep ? " · " + escapeHtml(ep) : "") + "</span></a>", "has-art");
      }).join("");
    }
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
      return d.toLocaleDateString(window.__LANG === "de" ? "de-DE" : "en-US",
                                  { weekday: "short", day: "2-digit", month: "2-digit" });
    } catch (e) { return iso; }
  }

  // ------------------------------------------------------------ loading
  let loadedAt = 0;
  let inFlight = false;

  async function load() {
    if (inFlight) return;
    if (loadedAt && Date.now() - loadedAt < RELOAD_AFTER) return;
    inFlight = true;
    feedError = "";

    DISCOVERY_ROWS.forEach(function (row) {
      const grid = showSection(row, true);
      if (grid) renderSkeletons(grid, 12);
    });

    try {
      // The badges need their own data before any card is built — same order
      // loadAniworldBrowse() uses on the classic page.
      const [, , resp] = await Promise.all([
        loadDownloadedFolders(),
        loadAutoSyncJobs(),
        fetch("/api/home-feed?adult=" + (adultWanted() ? "1" : "0") +
              "&limit=" + ROW_MAX),
      ]);
      const data = await resp.json();
      sources = Array.isArray(data.sources) ? data.sources : [];
      rows = data.rows || {};
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
    renderRows();
    applyUptimeStatus();

    // Personal rows are a second, independent request: they read local data
    // (library, favourites, calendar) and must not hold up the discovery rows
    // if the calendar is slow.
    try {
      const presp = await fetch("/api/home-feed/personal");
      personal = await presp.json();
      renderPersonal();
    } catch (e) {
      personal = {};
    }
  }

  window.reloadHomeFeed = function () { loadedAt = 0; load(); };

  loadFilters();
  load();
})();
