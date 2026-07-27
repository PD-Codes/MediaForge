/* ===================================================================
   MediaForge — Calendar
   -------------------------------------------------------------------
   Upcoming air/release dates for AutoSync jobs, Seerr requests, the
   Media Library and Crunchyroll, based on cached TMDB data.

   Three ranges (month / week / agenda) x two layouts (list / grid),
   free-text search, source chips, a media-type filter, a detail modal
   on every event, and an ICS subscription URL.

   Talks to:
     GET  /api/calendar                    (events + watcher state)
     GET  /api/calendar/feed               (ICS subscription URL)
     POST /api/calendar/feed/regenerate    (rotate the ICS token)
     GET  /api/tmdb/details?id&type        (detail modal)
   =================================================================== */
(function () {
  "use strict";

  var LOCALE = window.__LANG === "de" ? "de-DE" : "en-US";
  var POLL_MS = 10000;

  // One-time filter reset: the chip semantics changed (watchlist/lists are now
  // independent, additive chips). Stale saved "hidden" state from the old model
  // made watchlist + lists vanish, so clear it once per browser; everything then
  // defaults to visible.
  try {
    if (localStorage.getItem("aw-cal-filters-v2") !== "1") {
      localStorage.removeItem("aw-cal-hidden");
      localStorage.removeItem("aw-cal-lists-hidden");
      localStorage.removeItem("aw-cal-wl-only");
      localStorage.setItem("aw-cal-filters-v2", "1");
    }
  } catch (e) { /* ignore */ }

  // ── State ──
  var state = {
    anchor: new Date(),               // reference date for the visible period
    range: localStorage.getItem("aw-cal-range") || "month",   // month | week | agenda
    layout: localStorage.getItem("aw-cal-layout") || "list",  // list (timeline row) | grid (tiles)
    type: localStorage.getItem("aw-cal-type") || "all",       // all | tv | movie
    q: "",
    events: [],                       // raw events from the API
    byDay: {},                        // 'YYYY-MM-DD' -> [visible events]
    visible: [],                      // flat list of events passing all filters
    loaded: false,
    // Source visibility filters (persisted). Crunchyroll adds a "watchlist only"
    // sub-filter that narrows Crunchyroll events to your watchlist entries.
    sourcesHidden: readJson("aw-cal-hidden"),
    // Per-Crunchylist visibility (persisted) so custom lists can be shown separately.
    listsHidden: readJson("aw-cal-lists-hidden"),
    // Recomputed per render — see badgesWorthShowing().
    showBadges: { cr: true, seerr: true },
  };

  function readJson(key) {
    try { return JSON.parse(localStorage.getItem(key) || "{}") || {}; }
    catch (e) { return {}; }
  }

  function store(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* private mode */ }
  }

  // ── Helpers ──
  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function dayKey(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function parseDay(s) {
    var p = (s || "").split("-");
    return new Date(parseInt(p[0], 10), parseInt(p[1], 10) - 1, parseInt(p[2], 10));
  }
  function sameDay(a, b) { return dayKey(a) === dayKey(b); }
  function startOfWeek(d) {
    // Monday as the first day of the week
    var x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    var dow = (x.getDay() + 6) % 7; // 0 = Monday
    x.setDate(x.getDate() - dow);
    return x;
  }
  function addDays(d, n) { var x = new Date(d); x.setDate(x.getDate() + n); return x; }

  function tmdbImg(path, size) {
    if (!path) return "";
    return proxyImg("https://image.tmdb.org/t/p/" + (size || "w154") + path);
  }
  function isCR(ev) { return !!ev && ev.source === "crunchyroll"; }
  // Crunchyroll events carry an absolute image URL; everything else uses TMDB paths.
  function evImg(ev, size) {
    if (ev && ev.image_url) return proxyImg(ev.image_url);
    if (ev && ev.still) return tmdbImg(ev.still, size);
    if (ev && ev.poster) return tmdbImg(ev.poster, size);
    return "";
  }
  function isMovie(ev) { return !!(ev && (ev.is_movie || ev.season == null)); }

  // Source badges are only worth their space when they tell entries APART.
  // With a Crunchyroll-heavy calendar every single row said "★ CRUNCHYROLL",
  // which is a lot of ink for zero information — so the badge is suppressed
  // once a source accounts for nearly everything on screen. The coloured
  // spine on the left edge still identifies it, and the filter chips still
  // say which sources are in play.
  function badgesWorthShowing() {
    var total = state.visible.length;
    if (total < 6) return { cr: true, seerr: true };
    var cr = 0, seerr = 0;
    state.visible.forEach(function (ev) {
      if (isCR(ev) || ev.cr_member) cr++;
      if (ev.source === "seerr") seerr++;
    });
    // "Nearly everything" = 80%+. A mixed calendar keeps its badges.
    return { cr: cr / total < 0.8, seerr: seerr / total < 0.8 };
  }

  function crTag(ev) {
    // Show the CR badge for native CR events AND for other-source events that
    // are also on Crunchyroll (cr_member), so e.g. a Seerr item still shows it.
    if (!isCR(ev) && !ev.cr_member) return "";
    // A watchlist star is a personal mark, not a source label — always keep it.
    if (ev.cr_in_watchlist) return '<span class="cal-cr-tag cal-cr-wl" title="Crunchyroll">★</span>';
    if (!state.showBadges.cr) return "";
    if (ev.cr_lists && ev.cr_lists.length)
      return '<span class="cal-cr-tag cal-cr-list">' + esc(ev.cr_lists.join(", ")) + '</span>';
    return '<span class="cal-cr-tag">Crunchyroll</span>';
  }
  function epLabel(ev) {
    if (isMovie(ev)) return t("Film", "Movie");
    return "S" + pad(ev.season || 0) + "E" + pad(ev.episode || 0);
  }
  function seerrTag(ev) {
    if (ev.source !== "seerr" || !state.showBadges.seerr) return "";
    return '<span class="cal-seerr-tag">Seerr</span>';
  }
  function esc(s) {
    // Single quotes included: attributes elsewhere in the codebase are written
    // with either quote character, and an escaper that only covers one of them
    // is a trap for whoever copies it next.
    return (s == null ? "" : String(s)).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c];
    });
  }
  // Only relative and http(s) URLs may reach a src=; javascript:/data: are dropped.
  function safeUrl(u) {
    var s = String(u == null ? "" : u).trim();
    if (!s) return "";
    if (/^(https?:)?\/\//i.test(s) || s.charAt(0) === "/") return s;
    return "";
  }
  function $(id) { return document.getElementById(id); }

  var WEEKDAYS = (function () {
    var out = [];
    var ref = startOfWeek(new Date());
    var fmt = new Intl.DateTimeFormat(LOCALE, { weekday: "short" });
    for (var i = 0; i < 7; i++) out.push(fmt.format(addDays(ref, i)));
    return out;
  })();

  // ── Filtering ──

  function matchesSourceFilters(ev) {
    if (isCR(ev) || ev.cr_member) {
      // Additive: a CR show can be in the watchlist AND lists AND/or simulcast —
      // and, for merged events, also a non-CR source (e.g. Seerr). Show it if
      // ANY enabled membership/source is visible.
      if (!isCR(ev) && !state.sourcesHidden[ev.source]) return true;   // own source
      if (ev.cr_in_watchlist && !state.sourcesHidden.watchlist) return true;
      if (ev.cr_lists && ev.cr_lists.length &&
          ev.cr_lists.some(function (n) { return !state.listsHidden[n]; })) return true;
      if (ev.cr_kind === "simulcast" && !state.sourcesHidden.crunchyroll) return true;
      return false;
    }
    return !state.sourcesHidden[ev.source];
  }

  function matchesTypeFilter(ev) {
    if (state.type === "movie") return isMovie(ev);
    if (state.type === "tv") return !isMovie(ev);
    return true;
  }

  function matchesQuery(ev) {
    if (!state.q) return true;
    var needle = state.q;
    return String(ev.title || "").toLowerCase().indexOf(needle) !== -1 ||
           String(ev.name || "").toLowerCase().indexOf(needle) !== -1;
  }

  function indexEvents() {
    state.byDay = {};
    state.visible = [];
    state.events.forEach(function (ev) {
      if (!ev.air_date) return;
      if (!matchesSourceFilters(ev) || !matchesTypeFilter(ev) || !matchesQuery(ev)) return;
      state.visible.push(ev);
      (state.byDay[ev.air_date] = state.byDay[ev.air_date] || []).push(ev);
    });
    state.showBadges = badgesWorthShowing();
  }

  // Source filter chips (rendered into the toolbar container).
  // "mediathek" is the Jellyfin/Plex inventory comparison (mediascan_cache) and
  // is labelled MediaScan for that reason; "library" is the local MediaForge
  // library (library_cache). Two sources, two chips -- never the same word.
  var SOURCE_LABELS = {
    autosync: ["AutoSync", "AutoSync"],
    seerr: ["Seerr", "Seerr"],
    mediathek: ["MediaScan", "MediaScan"],
    library: ["Mediathek", "Library"],
    crunchyroll: ["Crunchyroll", "Crunchyroll"],
  };

  function buildFilters() {
    var box = $("calFilters");
    if (!box) return;
    // Categorise events. CR splits into simulcast / watchlist / per-list so each
    // is an independent show/hide chip (watchlist stays visible even when the
    // Crunchyroll/simulcast chip is off).
    var present = {};
    var listNames = {};
    state.events.forEach(function (ev) {
      if (!isCR(ev)) {
        present[ev.source] = true;
        if (!ev.cr_member) return;  // merged CR events also feed the CR chips below
      }
      // A show can belong to several categories at once -> feed every matching chip.
      if (ev.cr_in_watchlist) present.watchlist = true;
      if (ev.cr_lists) ev.cr_lists.forEach(function (n) { listNames[n] = true; });
      if (ev.cr_kind === "simulcast") present.crunchyroll = true;  // pure simulcast
    });

    var chips = [];
    ["autosync", "seerr", "mediathek", "library", "crunchyroll"].forEach(function (src) {
      if (!present[src]) return;
      var l = SOURCE_LABELS[src];
      var lbl = l ? t(l[0], l[1]) : src;
      var off = state.sourcesHidden[src] ? " cal-filter-off" : "";
      chips.push('<button type="button" class="cal-filter cal-filter-' + src + off +
        '" aria-pressed="' + (state.sourcesHidden[src] ? "false" : "true") +
        '" data-src="' + esc(src) + '">' + esc(lbl) + "</button>");
    });
    if (present.watchlist) {
      var woff = state.sourcesHidden.watchlist ? " cal-filter-off" : "";
      chips.push('<button type="button" class="cal-filter cal-filter-wl' + woff +
        '" aria-pressed="' + (state.sourcesHidden.watchlist ? "false" : "true") +
        '" data-src="watchlist">★ ' + esc(t("Watchlist", "Watchlist")) + "</button>");
    }
    Object.keys(listNames).sort().forEach(function (name) {
      var loff = state.listsHidden[name] ? " cal-filter-off" : "";
      chips.push('<button type="button" class="cal-filter cal-filter-crlist' + loff +
        '" aria-pressed="' + (state.listsHidden[name] ? "false" : "true") +
        '" data-crlist="' + esc(name) + '">' + esc(name) + "</button>");
    });

    // Only worth a filter bar when there's more than one thing to toggle.
    box.innerHTML = chips.length < 2 ? "" : chips.join("");
  }

  // The chip bar is rebuilt on every render, so the handlers are delegated once
  // instead of being re-bound per chip.
  function wireFilterBar() {
    var box = $("calFilters");
    if (!box) return;
    box.addEventListener("click", function (e) {
      var btn = e.target.closest(".cal-filter");
      if (!btn) return;
      var src = btn.getAttribute("data-src");
      if (src) {
        state.sourcesHidden[src] = !state.sourcesHidden[src];
        store("aw-cal-hidden", JSON.stringify(state.sourcesHidden));
      } else {
        var name = btn.getAttribute("data-crlist");
        if (!name) return;
        state.listsHidden[name] = !state.listsHidden[name];
        store("aw-cal-lists-hidden", JSON.stringify(state.listsHidden));
      }
      indexEvents();
      render();
    });
  }

  function _eventsSignature(events) {
    // Cheap fingerprint so silent polls only re-render when data actually changed
    var parts = [];
    for (var i = 0; i < events.length; i++) {
      var e = events[i];
      parts.push(e.tmdb_id + "|" + e.season + "|" + e.episode + "|" + e.air_date + "|" + e.source);
    }
    return parts.join(",");
  }

  function load(isSilent) {
    if (!isSilent && !state.loaded) showState("loading");
    fetch("/api/calendar")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.loaded = true;
        if (data && data.error === "no_key") { showState("noKey"); return; }
        var events = (data && data.events) || [];
        var sig = _eventsSignature(events);
        state.watcher = data.watcher || {};
        state.meta = data.meta || {};
        // On a silent poll, skip the (DOM-rebuilding) re-render when nothing
        // changed — avoids flicker and losing hover/popover state.
        if (!(isSilent && sig === state._sig)) {
          state._sig = sig;
          state.events = events;
          indexEvents();
          render();
        }
        updateWatcherStatus(state.watcher);
      })
      .catch(function () {
        state.loaded = true;
        if (!isSilent) {
          state.events = [];
          state._sig = "";
          indexEvents();
          render();
        }
      });
  }

  function updateWatcherStatus(watcher) {
    var dot = $("calWatcherDot");
    var label = $("calWatcherLabel");
    var scanBadge = $("calScanBadge");
    if (!dot || !label) return;

    if (scanBadge) scanBadge.style.display = watcher.is_scanning ? "inline-flex" : "none";

    if (watcher.active) {
      dot.className = "cal-watcher-dot cal-watcher-on";
      label.textContent = t("Watcher aktiv", "Watcher active");
    } else {
      dot.className = "cal-watcher-dot cal-watcher-off";
      label.textContent = t("Watcher inaktiv", "Watcher inactive");
    }
    // Keep the empty-state hint in sync even when the calendar isn't re-rendered
    updateEmptyVariant();
  }

  // When the calendar is empty, show a reassuring "first sync running" hint
  // while the watcher is still doing its initial population.
  function updateEmptyVariant() {
    var def = $("calEmptyDefault");
    var syn = $("calEmptySyncing");
    var filtered = $("calEmptyFiltered");
    if (!def || !syn) return;

    // "Nothing matches your filters" is a different situation from "nothing
    // scheduled at all" and deserves a different message plus a way out.
    var filteredOut = state.events.length > 0 && state.visible.length === 0;
    var w = state.watcher || {};
    var syncing = !filteredOut && !!w.active && (w.is_scanning || !w.last_sync);

    if (filtered) filtered.style.display = filteredOut ? "" : "none";
    def.style.display = (filteredOut || syncing) ? "none" : "";
    syn.style.display = syncing ? "" : "none";

    // When showing the default "nothing here" state and Seerr is active with
    // open requests, clarify that those simply have no dated entries (not broken).
    var seerrHint = $("calEmptySeerrHint");
    if (seerrHint) {
      var m = state.meta || {};
      seerrHint.style.display =
        (!syncing && !filteredOut && m.seerr_active && m.seerr_count > 0) ? "" : "none";
    }
  }

  // ── State visibility ──
  function showState(which) {
    var ids = { loading: "calLoading", noKey: "calNoKey", empty: "calEmpty", view: "calView" };
    Object.keys(ids).forEach(function (k) {
      var el = $(ids[k]);
      if (el) el.style.display = (k === which) ? (k === "view" ? "block" : "flex") : "none";
    });
  }

  // ── Rendering ──
  function render() {
    if (!state.loaded) { showState("loading"); return; }
    updatePeriodLabel();
    buildFilters();
    updateCount();

    if (state.visible.length === 0) { updateEmptyVariant(); showState("empty"); return; }
    showState("view");

    var view = $("calView");
    if (!view) return;
    if (state.range === "month") view.innerHTML = renderMonth();
    else if (state.range === "week") view.innerHTML = renderWeek();
    else view.innerHTML = renderAgenda();
  }

  function updateCount() {
    var el = $("calCount");
    if (!el) return;
    var n = state.visible.length;
    el.textContent = n
      ? n + " " + (n === 1 ? t("Eintrag", "entry") : t("Einträge", "entries"))
      : "";
  }

  function updatePeriodLabel() {
    var label = "";
    if (state.range === "month") {
      label = new Intl.DateTimeFormat(LOCALE, { month: "long", year: "numeric" }).format(state.anchor);
    } else if (state.range === "week") {
      var s = startOfWeek(state.anchor), e = addDays(s, 6);
      var f = new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "short" });
      label = f.format(s) + " – " + f.format(e) + " " + e.getFullYear();
    } else {
      // Agenda is anchored on "from today onwards" and does not page.
      label = t("Kommende Termine", "Upcoming");
    }
    var el = $("calPeriodLabel");
    if (el) el.textContent = label.charAt(0).toUpperCase() + label.slice(1);

    // Prev/next/today are meaningless in the agenda view.
    var isAgenda = state.range === "agenda";
    ["calPrevBtn", "calNextBtn", "calTodayBtn"].forEach(function (id) {
      var b = $(id);
      if (b) b.disabled = isAgenda;
    });
  }

  // Every event carries the data the detail modal needs, so a click anywhere
  // on it can open the modal without a lookup table.
  function evAttrs(ev) {
    return ' data-tmdb-id="' + esc(ev.tmdb_id || "") + '"' +
      ' data-media-type="' + esc(ev.media_type || (isMovie(ev) ? "movie" : "tv")) + '"' +
      ' data-title="' + esc(ev.title || "") + '"' +
      ' data-air="' + esc(ev.air_date || "") + '"' +
      ' data-ep="' + esc(epLabel(ev)) + '"' +
      ' data-name="' + esc(ev.name || "") + '"' +
      ' data-img="' + esc(safeUrl(evImg(ev, "w300"))) + '"';
  }

  function pillHtml(ev) {
    var imgSrc = safeUrl(evImg(ev, "w185"));
    var poster = imgSrc ? '<img class="cal-pill-poster" loading="lazy" src="' + esc(imgSrc) + '" alt="">' : "";
    var cls = ev.source === "seerr" ? " cal-pill-seerr" : (isCR(ev) ? " cal-pill-cr" : "");
    return '<div class="cal-pill cal-event' + cls + '"' + evAttrs(ev) +
      ' title="' + esc(ev.title) + " · " + esc(epLabel(ev)) +
      (ev.name ? " · " + esc(ev.name) : "") + (ev.source === "seerr" ? " · Seerr" : "") +
      (isCR(ev) ? " · Crunchyroll" : "") + '">' +
      poster +
      '<div class="cal-pill-text">' +
      '<span class="cal-pill-title">' + esc(ev.title) + "</span>" +
      '<span class="cal-pill-ep">' + esc(epLabel(ev)) + seerrTag(ev) + crTag(ev) + "</span>" +
      "</div></div>";
  }

  // Month cell, list layout: ONE LINE per title -- source as a 3px spine on the
  // left, the episode right-aligned and quiet. The old pill carried a poster, a
  // title, an episode badge and a source badge, which turned a full month into a
  // wall where every line looked identical (reported on a calendar with 876
  // entries, nearly all Crunchyroll). Five of these fit the height four pills
  // needed. The watchlist star survives because it is a personal mark, not a
  // source label -- see crTag().
  function monthRowHtml(ev) {
    return '<div class="cal-mrow cal-event"' + evAttrs(ev) +
      ' title="' + esc(ev.title) + " · " + esc(epLabel(ev)) +
      (ev.name ? " · " + esc(ev.name) : "") + '">' +
      '<span class="cal-mrow-spine ' + sourceClass(ev) + '" aria-hidden="true"></span>' +
      '<span class="cal-mrow-title">' + esc(ev.title) + "</span>" +
      (ev.cr_in_watchlist ? '<span class="cal-mrow-star" title="Crunchyroll">★</span>' : "") +
      '<span class="cal-mrow-ep">' + esc(epLabel(ev)) + "</span>" +
    "</div>";
  }

  function renderMonth() {
    var first = new Date(state.anchor.getFullYear(), state.anchor.getMonth(), 1);
    var gridStart = startOfWeek(first);
    var today = new Date();
    var month = state.anchor.getMonth();

    var html = '<div class="cal-month-grid ' + (state.layout === "grid" ? "cal-layout-grid" : "") + '">';
    WEEKDAYS.forEach(function (w) { html += '<div class="cal-weekday">' + esc(w) + "</div>"; });

    for (var i = 0; i < 42; i++) {
      var d = addDays(gridStart, i);
      var key = dayKey(d);
      var evs = state.byDay[key] || [];
      var cls = "cal-day";
      if (d.getMonth() !== month) cls += " cal-day-muted";
      if (sameDay(d, today)) cls += " cal-today";

      var hasEvents = evs.length > 0;
      if (hasEvents) cls += " cal-day-clickable";
      var numCls = "cal-day-num" + (hasEvents ? " cal-has-events" : "");
      var dayAttr = hasEvents ? ' data-day="' + esc(key) + '"' : "";

      html += '<div class="' + cls + '"' + dayAttr + ">";
      html += '<span class="' + numCls + '"' + dayAttr + ">" + d.getDate() + "</span>";
      // Compact event-count dot — only visible on small screens (CSS-driven)
      if (hasEvents) html += '<span class="cal-day-dot" aria-hidden="true">' + evs.length + "</span>";
      html += '<div class="cal-day-events">';

      // Tiles are tall, single lines are not — but one entry per day made the
      // month grid mostly "+N more". Two tiles still fit a cell at normal zoom,
      // and five of the flat rows fit where four pills used to.
      var limit = state.layout === "grid" ? 2 : 5;
      var itemHtml = state.layout === "grid" ? pillHtml : monthRowHtml;
      evs.slice(0, limit).forEach(function (ev) { html += itemHtml(ev); });
      if (evs.length > limit) {
        html += '<span class="cal-day-more" data-day="' + esc(key) + '">+' + (evs.length - limit) +
          " " + esc(t("weitere", "more")) + "</span>";
      }
      html += "</div></div>";
    }
    html += "</div>";
    return html;
  }

  function renderWeek() {
    var start = startOfWeek(state.anchor);
    var days = [];
    for (var i = 0; i < 7; i++) {
      var d = addDays(start, i);
      days.push({ date: d, evs: state.byDay[dayKey(d)] || [] });
    }
    // The week view keeps empty days — that is what makes it a week.
    return timelineHtml(days, new Date(), true);
  }

  // Agenda: every upcoming day that actually has entries, from today on. It
  // does not page; it answers "what is next" in one continuous run.
  function renderAgenda() {
    var todayKey = dayKey(new Date());
    var keys = Object.keys(state.byDay).filter(function (k) { return k >= todayKey; }).sort();
    if (!keys.length) {
      return '<div class="cal-agenda-empty">' +
        esc(t("Keine kommenden Termine — nur Vergangenes.",
              "Nothing upcoming — only past entries.")) + "</div>";
    }
    return timelineHtml(keys.map(function (k) {
      return { date: parseDay(k), evs: state.byDay[k] };
    }), new Date(), false);
  }

  // ── The timeline ───────────────────────────────────────────────────
  // Shared by the week and agenda views (.mf-timeline in mf_components.css).
  // `showEmpty` keeps days without entries (week) or drops them and states
  // the gap instead (agenda) — a fortnight with nothing scheduled should
  // read as a fortnight with nothing scheduled, not as two adjacent rows.
  function timelineHtml(days, today, showEmpty) {
    var html = '<div class="mf-timeline">';
    var prev = null;

    days.forEach(function (entry) {
      var d = entry.date;
      var evs = entry.evs || [];
      if (!evs.length && !showEmpty) return;

      if (!showEmpty && prev) {
        var skipped = Math.round((d - prev) / 86400000) - 1;
        if (skipped > 0) {
          html += '<div class="mf-timeline-gap">' + esc(gapLabel(skipped)) + "</div>";
        }
      }
      prev = d;

      html += '<div class="mf-stop' + (sameDay(d, today) ? " is-now" : "") + '">';
      html += '<span class="mf-stop-dot" aria-hidden="true"></span>';
      html += '<div class="mf-stop-when">' +
        '<span class="mf-stop-rel">' + esc(relativeDayLabel(d, today) ||
          new Intl.DateTimeFormat(LOCALE, { weekday: "short" }).format(d)) + "</span>" +
        '<span class="mf-stop-day">' + pad(d.getDate()) + "</span>" +
        '<span class="mf-stop-mon">' +
          esc(new Intl.DateTimeFormat(LOCALE, { month: "short" }).format(d)) + "</span>" +
        "</div>";

      if (evs.length > 2) {
        html += '<span class="mf-stop-count">' + evs.length + " " +
          esc(t("Einträge", "entries")) + "</span>";
      }

      if (!evs.length) {
        html += '<div class="mf-stop-item cal-stop-empty">' +
          esc(t("Nichts geplant", "Nothing scheduled")) + "</div>";
      } else if (state.layout === "grid") {
        html += '<div class="cal-tiles">';
        evs.forEach(function (ev) { html += tileHtml(ev); });
        html += "</div>";
      } else {
        evs.forEach(function (ev) { html += stopItemHtml(ev); });
      }
      html += "</div>";
    });

    return html + "</div>";
  }

  function gapLabel(days) {
    if (days === 1) return t("ein Tag ohne Termin", "one day with nothing");
    if (days < 7) return t(days + " Tage ohne Termin", days + " days with nothing");
    var weeks = Math.round(days / 7);
    return weeks === 1
      ? t("eine Woche ohne Termin", "a week with nothing")
      : t(weeks + " Wochen ohne Termin", weeks + " weeks with nothing");
  }

  // Source is a coloured spine on the left edge rather than another word
  // competing with the title.
  function sourceClass(ev) {
    if (isCR(ev) || ev.cr_member) return "cal-src-cr";
    if (ev.source === "seerr") return "cal-src-seerr";
    if (ev.source === "mediathek") return "cal-src-lib";
    if (ev.source === "library") return "cal-src-library";
    return "cal-src-auto";
  }

  function stopItemHtml(ev) {
    var img = safeUrl(evImg(ev, "w154"));
    return '<div class="mf-stop-item cal-event"' + evAttrs(ev) + ' tabindex="0"' +
      ' title="' + esc(ev.title) + " · " + esc(epLabel(ev)) + '">' +
      '<span class="mf-stop-source ' + sourceClass(ev) + '" aria-hidden="true"></span>' +
      (img
        ? '<img class="mf-stop-thumb" loading="lazy" src="' + esc(img) + '" alt="">'
        : '<span class="mf-stop-thumb" aria-hidden="true"></span>') +
      '<span class="mf-stop-text">' +
        '<span class="mf-stop-title">' + esc(ev.title) + seerrTag(ev) + crTag(ev) + "</span>" +
        (ev.name ? '<span class="mf-stop-sub">' + esc(ev.name) + "</span>" : "") +
      "</span>" +
      '<span class="mf-stop-badge">' + esc(epLabel(ev)) + "</span>" +
    "</div>";
  }

  function relativeDayLabel(d, today) {
    var days = Math.round((d - new Date(today.getFullYear(), today.getMonth(), today.getDate())) / 86400000);
    if (days === 0) return t("Heute", "Today");
    if (days === 1) return t("Morgen", "Tomorrow");
    if (days < 0) return "";
    if (days < 7) return t("in " + days + " Tagen", "in " + days + " days");
    var weeks = Math.round(days / 7);
    return weeks === 1
      ? t("in 1 Woche", "in 1 week")
      : t("in " + weeks + " Wochen", "in " + weeks + " weeks");
  }

  function tileHtml(ev) {
    var img = safeUrl(evImg(ev, "w300"));
    var poster = img
      ? '<img class="cal-tile-poster" loading="lazy" src="' + esc(img) + '" alt="">'
      : '<div class="cal-tile-poster"></div>';
    var cls = ev.source === "seerr" ? " cal-tile-seerr" : (isCR(ev) ? " cal-tile-cr" : "");
    return '<div class="cal-tile cal-event' + cls + '"' + evAttrs(ev) +
      ' title="' + esc(ev.title) + '">' + poster +
      '<div class="cal-tile-body">' +
      '<div class="cal-tile-title">' + esc(ev.title) + "</div>" +
      '<div class="cal-tile-sub">' + esc(epLabel(ev)) + seerrTag(ev) + crTag(ev) + "</div>" +
      (ev.name ? '<div class="cal-tile-name">' + esc(ev.name) + "</div>" : "") +
      "</div></div>";
  }

  // ── Day popover (month view) ──

  document.addEventListener("click", function (e) {
    var pop = document.querySelector(".cal-popover");
    if (pop && !pop.contains(e.target) && !e.target.classList.contains("cal-day-more")) {
      pop.remove();
    }
  });

  function showDayPopover(btnEl, dateKey) {
    var existing = document.querySelector(".cal-popover");
    if (existing) existing.remove();

    var evs = state.byDay[dateKey] || [];
    if (!evs.length) return;

    var pop = document.createElement("div");
    pop.className = "cal-popover";

    var d = parseDay(dateKey);
    var headerText = new Intl.DateTimeFormat(LOCALE, {
      weekday: "long", day: "numeric", month: "long",
    }).format(d);

    var html = '<div class="cal-popover-header">' + esc(headerText) + "</div>";
    html += '<div class="cal-popover-list">';

    evs.forEach(function (ev) {
      var img = safeUrl(evImg(ev, "w154"));
      var posterHtml = img
        ? '<img class="cal-popover-img" loading="lazy" src="' + esc(img) + '" alt="">'
        : '<div class="cal-popover-img"></div>';
      var badgeClass = ev.source === "seerr" ? " seerr-badge" : (isCR(ev) ? " cr-badge" : "");

      html += '<div class="cal-popover-item cal-event' + '"' + evAttrs(ev) +
        ' title="' + esc(ev.title) + '">' +
        posterHtml +
        '<div class="cal-popover-info">' +
          '<div class="cal-popover-title">' + esc(ev.title) + "</div>" +
          (ev.name ? '<div class="cal-popover-sub">' + esc(ev.name) + "</div>" : "") +
        "</div>" +
        '<span class="cal-popover-ep-badge' + badgeClass + '">' +
          esc(epLabel(ev)) + seerrTag(ev) + crTag(ev) + "</span>" +
      "</div>";
    });

    html += "</div>";
    pop.innerHTML = html;
    document.body.appendChild(pop);

    var rect = btnEl.getBoundingClientRect();
    var popWidth = Math.min(300, window.innerWidth - 20); // matches CSS max-width
    var popHeight = pop.offsetHeight;

    // Horizontal: centre on the button, clamped to the viewport
    var left = rect.left + window.scrollX - (popWidth / 2) + (rect.width / 2);
    if (left < 10) left = 10;
    if (left + popWidth > window.innerWidth - 10) left = window.innerWidth - popWidth - 10;

    // Vertical: open below by default, but flip above if it would overflow the
    // viewport bottom and there is more room above.
    var top = rect.bottom + window.scrollY + 6;
    var spaceBelow = window.innerHeight - rect.bottom;
    if (spaceBelow < popHeight + 12 && rect.top > spaceBelow) {
      top = rect.top + window.scrollY - popHeight - 6;
      if (top < window.scrollY + 10) top = window.scrollY + 10;
    }

    pop.style.top = top + "px";
    pop.style.left = left + "px";
  }

  // ── Detail modal ──
  // A calendar entry is a TMDB id, not a provider URL, so the big series modal
  // in shared_modals.html (which starts from a provider page and needs all of
  // app.js) does not fit. The calendar uses the standalone shared component
  // instead — templates/mf_detail_modal.html + static/mf_detail_modal.js —
  // which is the same modal any third-party module can embed.
  function openDetail(el) {
    if (!window.MFDetailModal) {
      console.error("[Calendar] MFDetailModal missing — is mf_detail_modal.js loaded?");
      return;
    }
    MFDetailModal.open({
      tmdbId: el.getAttribute("data-tmdb-id") || null,
      mediaType: el.getAttribute("data-media-type") === "movie" ? "movie" : "tv",
      title: el.getAttribute("data-title") || "",
      subtitle: el.getAttribute("data-ep") || "",
      caption: el.getAttribute("data-name") || "",
      date: el.getAttribute("data-air") || "",
      image: el.getAttribute("data-img") || "",
    });
  }

  // ── ICS subscription ──

  function openFeedModal() {
    var overlay = $("calFeedOverlay");
    if (!overlay) return;
    var input = $("calFeedUrl");
    if (input) input.value = t("Wird geladen…", "Loading…");
    overlay.style.display = "block";
    document.body.style.overflow = "hidden";
    fetchFeedUrl("/api/calendar/feed", "GET");
  }

  function fetchFeedUrl(url, method) {
    var input = $("calFeedUrl");
    fetch(url, method === "POST" ? { method: "POST" } : undefined)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (input) {
          input.value = (d && d.url) ||
            t("Konnte nicht geladen werden.", "Could not be loaded.");
        }
      })
      .catch(function () {
        if (input) input.value = t("Konnte nicht geladen werden.", "Could not be loaded.");
      });
  }

  function copyFeedUrl() {
    var input = $("calFeedUrl");
    if (!input || !input.value) return;
    var done = function () {
      var btn = $("calFeedCopy");
      if (!btn) return;
      var prev = btn.textContent;
      btn.textContent = t("Kopiert!", "Copied!");
      setTimeout(function () { btn.textContent = prev; }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(input.value).then(done, function () {
        input.select(); done();
      });
    } else {
      // Older browsers / non-secure contexts have no async clipboard API.
      input.select();
      try { document.execCommand("copy"); } catch (e) { /* leave it selected */ }
      done();
    }
  }

  // ── Navigation ──
  function navigate(dir) {
    if (state.range === "agenda") return;
    if (state.range === "month") {
      state.anchor = new Date(state.anchor.getFullYear(), state.anchor.getMonth() + dir, 1);
    } else {
      state.anchor = addDays(state.anchor, dir * 7);
    }
    render();
  }

  function setSegmented(groupId, attr, value) {
    var group = $(groupId);
    if (!group) return;
    group.querySelectorAll(".cal-seg-btn").forEach(function (b) {
      var on = b.getAttribute(attr) === value;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function setRange(range) {
    state.range = range;
    store("aw-cal-range", range);
    setSegmented("calRangeToggle", "data-range", range);
    render();
  }

  function setLayout(layout) {
    state.layout = layout;
    store("aw-cal-layout", layout);
    setSegmented("calLayoutToggle", "data-layout", layout);
    render();
  }

  function setType(type) {
    state.type = type;
    store("aw-cal-type", type);
    setSegmented("calTypeToggle", "data-type", type);
    indexEvents();
    render();
  }

  // ── Init ──
  function init() {
    var prev = $("calPrevBtn"), next = $("calNextBtn"), today = $("calTodayBtn");
    if (prev) prev.addEventListener("click", function () { navigate(-1); });
    if (next) next.addEventListener("click", function () { navigate(1); });
    if (today) {
      today.addEventListener("click", function () {
        state.anchor = new Date();
        render();
      });
    }

    var rangeToggle = $("calRangeToggle");
    if (rangeToggle) {
      rangeToggle.addEventListener("click", function (e) {
        var b = e.target.closest(".cal-seg-btn");
        if (b) setRange(b.getAttribute("data-range"));
      });
    }
    var layoutToggle = $("calLayoutToggle");
    if (layoutToggle) {
      layoutToggle.addEventListener("click", function (e) {
        var b = e.target.closest(".cal-seg-btn");
        if (b) setLayout(b.getAttribute("data-layout"));
      });
    }
    var typeToggle = $("calTypeToggle");
    if (typeToggle) {
      typeToggle.addEventListener("click", function (e) {
        var b = e.target.closest(".cal-seg-btn");
        if (b) setType(b.getAttribute("data-type"));
      });
    }

    var search = $("calSearch");
    var searchTimer = null;
    if (search) {
      search.addEventListener("input", function () {
        var clear = $("calSearchClear");
        if (clear) clear.hidden = !search.value;
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function () {
          state.q = search.value.trim().toLowerCase();
          indexEvents();
          render();
        }, 200);
      });
    }
    var searchClear = $("calSearchClear");
    if (searchClear) {
      searchClear.addEventListener("click", function () {
        if (search) { search.value = ""; search.focus(); }
        searchClear.hidden = true;
        state.q = "";
        indexEvents();
        render();
      });
    }

    wireFilterBar();

    // Day cells / "+N more" open the day popover; any event opens the detail
    // modal. Delegated once on the container, because the view is rebuilt on
    // every render and every poll.
    var view = $("calView");
    if (view) {
      view.addEventListener("click", function (e) {
        var ev = e.target.closest(".cal-event");
        if (ev) { e.stopPropagation(); openDetail(ev); return; }
        var more = e.target.closest(".cal-day-more, .cal-day-num.cal-has-events");
        if (more) { e.stopPropagation(); showDayPopover(more, more.getAttribute("data-day")); return; }
        var cell = e.target.closest(".cal-day.cal-day-clickable");
        if (cell) { e.stopPropagation(); showDayPopover(cell, cell.getAttribute("data-day")); }
      });
    }
    // The popover is appended to <body>, so its events need their own handler.
    document.addEventListener("click", function (e) {
      var pop = e.target.closest(".cal-popover .cal-event");
      if (!pop) return;
      e.stopPropagation();
      var open = document.querySelector(".cal-popover");
      if (open) open.remove();
      openDetail(pop);
    });

    // Empty-state "reset filters"
    var resetBtn = $("calResetFilters");
    if (resetBtn) {
      resetBtn.addEventListener("click", function () {
        state.q = "";
        state.type = "all";
        state.sourcesHidden = {};
        state.listsHidden = {};
        store("aw-cal-hidden", "{}");
        store("aw-cal-lists-hidden", "{}");
        store("aw-cal-type", "all");
        if (search) search.value = "";
        if (searchClear) searchClear.hidden = true;
        setSegmented("calTypeToggle", "data-type", "all");
        indexEvents();
        render();
      });
    }

    // The detail modal wires its own close/search/Escape handlers
    // (static/mf_detail_modal.js) — nothing to do here.

    // ICS feed modal
    var feedBtn = $("calFeedBtn");
    if (feedBtn) feedBtn.addEventListener("click", openFeedModal);
    var feedOverlay = $("calFeedOverlay");
    if (feedOverlay) {
      feedOverlay.addEventListener("click", function (e) {
        if (e.target === feedOverlay) closeFeed();
      });
    }
    ["calFeedClose", "calFeedCloseBtn"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("click", closeFeed);
    });
    var copyBtn = $("calFeedCopy");
    if (copyBtn) copyBtn.addEventListener("click", copyFeedUrl);
    var regenBtn = $("calFeedRegen");
    if (regenBtn) {
      regenBtn.addEventListener("click", function () {
        if (!window.confirm(t(
          "Neue Feed-Adresse erzeugen? Die bisherige hört sofort auf zu funktionieren.",
          "Generate a new feed URL? The previous one stops working immediately."
        ))) return;
        var input = $("calFeedUrl");
        if (input) input.value = t("Wird erzeugt…", "Generating…");
        fetchFeedUrl("/api/calendar/feed/regenerate", "POST");
      });
    }

    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      var pop = document.querySelector(".cal-popover");
      if (pop) { pop.remove(); return; }
      var feed = $("calFeedOverlay");
      if (feed && feed.style.display === "block") closeFeed();
      // The detail modal closes itself on Escape (mf_detail_modal.js).
    });

    // Restore persisted toggle states
    setSegmented("calRangeToggle", "data-range", state.range);
    setSegmented("calLayoutToggle", "data-layout", state.layout);
    setSegmented("calTypeToggle", "data-type", state.type);

    showState("loading");
    load();

    // Poll calendar data & watcher status silently
    window.mfPoll(function () { load(true); }, POLL_MS);
  }

  function closeFeed() {
    var overlay = $("calFeedOverlay");
    if (overlay) overlay.style.display = "none";
    var stillOpen = Array.prototype.some.call(
      document.querySelectorAll(".overlay"),
      function (o) { return o.style.display && o.style.display !== "none"; }
    );
    if (!stillOpen) document.body.style.overflow = "";
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
