// ===== Calendar View =====
// Shows upcoming episode air dates for AutoSync jobs based on TMDB data.
(function () {
  "use strict";

  var LOCALE = window.__LANG === "de" ? "de-DE" : "en-US";

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
    range: localStorage.getItem("aw-cal-range") || "month",   // 'month' | 'week'
    layout: localStorage.getItem("aw-cal-layout") || "list",  // 'list'  | 'grid'
    events: [],                       // [{title, season, episode, name, air_date, poster, still}]
    byDay: {},                        // 'YYYY-MM-DD' -> [events]
    loaded: false,
    // Source visibility filters (persisted). Crunchyroll adds a "watchlist only"
    // sub-filter that narrows Crunchyroll events to your watchlist entries.
    sourcesHidden: (function () {
      try { return JSON.parse(localStorage.getItem("aw-cal-hidden") || "{}"); }
      catch (e) { return {}; }
    })(),
    // Per-Crunchylist visibility (persisted) so custom lists can be shown separately.
    listsHidden: (function () {
      try { return JSON.parse(localStorage.getItem("aw-cal-lists-hidden") || "{}"); }
      catch (e) { return {}; }
    })(),
  };

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
  function crTag(ev) {
    // Show the CR badge for native CR events AND for other-source events that are
    // also on Crunchyroll (cr_member), so e.g. a Seerr item still shows its CR tag.
    if (!isCR(ev) && !ev.cr_member) return "";
    if (ev.cr_in_watchlist)
      return '<span class="cal-cr-tag cal-cr-wl">\u2605 Crunchyroll</span>';
    if (ev.cr_lists && ev.cr_lists.length)
      return '<span class="cal-cr-tag cal-cr-list">Crunchyroll \u00b7 ' + esc(ev.cr_lists.join(", ")) + '</span>';
    return '<span class="cal-cr-tag">Crunchyroll</span>';
  }
  function epLabel(ev) {
    if (ev.is_movie || ev.season == null) return t("Film", "Movie");
    return "S" + pad(ev.season || 0) + "E" + pad(ev.episode || 0);
  }
  function seerrTag(ev) {
    return ev.source === "seerr" ? '<span class="cal-seerr-tag">Seerr</span>' : "";
  }
  function esc(s) {
    // Single quotes included: attributes elsewhere in the codebase are written
    // with either quote character, and an escaper that only covers one of them
    // is a trap for whoever copies it next.
    return (s == null ? "" : String(s)).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c];
    });
  }

  var WEEKDAYS = (function () {
    var out = [];
    var ref = startOfWeek(new Date());
    var fmt = new Intl.DateTimeFormat(LOCALE, { weekday: "short" });
    for (var i = 0; i < 7; i++) out.push(fmt.format(addDays(ref, i)));
    return out;
  })();

  // ── Data ──
  function indexEvents() {
    state.byDay = {};
    state.events.forEach(function (ev) {
      if (!ev.air_date) return;
      if (isCR(ev) || ev.cr_member) {
        // Additive: a CR show can be in the watchlist AND lists AND/or simulcast —
        // and, for merged events, also a non-CR source (e.g. Seerr). Show it if
        // ANY enabled membership/source is visible.
        var vis = false;
        if (!isCR(ev) && !state.sourcesHidden[ev.source]) vis = true;  // own source
        if (!vis && ev.cr_in_watchlist && !state.sourcesHidden.watchlist) vis = true;
        if (!vis && ev.cr_lists && ev.cr_lists.length &&
            ev.cr_lists.some(function (n) { return !state.listsHidden[n]; })) vis = true;
        if (!vis && ev.cr_kind === "simulcast" && !state.sourcesHidden.crunchyroll) vis = true;
        if (!vis) return;
      } else if (state.sourcesHidden[ev.source]) {
        return;
      }
      (state.byDay[ev.air_date] = state.byDay[ev.air_date] || []).push(ev);
    });
  }

  // Source filter chips (rendered into the toolbar container).
  var SOURCE_LABELS = {
    autosync: ["AutoSync", "AutoSync"],
    seerr: ["Seerr", "Seerr"],
    mediathek: ["Mediathek", "Library"],
    crunchyroll: ["Crunchyroll", "Crunchyroll"],
  };
  function buildFilters() {
    var box = document.getElementById("calFilters");
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
    ["autosync", "seerr", "mediathek", "crunchyroll"].forEach(function (src) {
      if (!present[src]) return;
      var l = SOURCE_LABELS[src];
      var lbl = l ? t(l[0], l[1]) : src;
      var off = state.sourcesHidden[src] ? " cal-filter-off" : "";
      chips.push('<button class="cal-filter cal-filter-' + src + off +
        '" data-src="' + src + '">' + esc(lbl) + '</button>');
    });
    if (present.watchlist) {
      var woff = state.sourcesHidden.watchlist ? " cal-filter-off" : "";
      chips.push('<button class="cal-filter cal-filter-wl' + woff +
        '" data-src="watchlist">\u2605 ' + esc(t("Watchlist", "Watchlist")) + '</button>');
    }
    Object.keys(listNames).sort().forEach(function (name) {
      var loff = state.listsHidden[name] ? " cal-filter-off" : "";
      chips.push('<button class="cal-filter cal-filter-crlist' + loff +
        '" data-crlist="' + esc(name) + '">' + esc(name) + '</button>');
    });

    // Only worth a filter bar when there's more than one thing to toggle.
    if (chips.length < 2) { box.innerHTML = ""; return; }
    box.innerHTML = chips.join("");

    box.querySelectorAll(".cal-filter[data-src]").forEach(function (b) {
      b.addEventListener("click", function () {
        var src = b.getAttribute("data-src");
        state.sourcesHidden[src] = !state.sourcesHidden[src];
        localStorage.setItem("aw-cal-hidden", JSON.stringify(state.sourcesHidden));
        indexEvents();
        render();
      });
    });
    box.querySelectorAll(".cal-filter[data-crlist]").forEach(function (b) {
      b.addEventListener("click", function () {
        var name = b.getAttribute("data-crlist");
        state.listsHidden[name] = !state.listsHidden[name];
        localStorage.setItem("aw-cal-lists-hidden", JSON.stringify(state.listsHidden));
        indexEvents();
        render();
      });
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
    if (!isSilent && !state.loaded) { showState("loading"); }
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
    var dot = document.getElementById("calWatcherDot");
    var label = document.getElementById("calWatcherLabel");
    var scanBadge = document.getElementById("calScanBadge");
    if (!dot || !label) return;

    if (watcher.is_scanning) {
      if (scanBadge) scanBadge.style.display = "inline-flex";
    } else {
      if (scanBadge) scanBadge.style.display = "none";
    }

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
    var def = document.getElementById("calEmptyDefault");
    var syn = document.getElementById("calEmptySyncing");
    if (!def || !syn) return;
    var w = state.watcher || {};
    var syncing = !!w.active && (w.is_scanning || !w.last_sync);
    def.style.display = syncing ? "none" : "";
    syn.style.display = syncing ? "" : "none";

    // When showing the default "nothing here" state and Seerr is active with
    // open requests, clarify that those simply have no dated entries (not broken).
    var seerrHint = document.getElementById("calEmptySeerrHint");
    if (seerrHint) {
      var m = state.meta || {};
      seerrHint.style.display = (!syncing && m.seerr_active && m.seerr_count > 0) ? "" : "none";
    }
  }

  // ── State visibility ──
  function showState(which) {
    var ids = { loading: "calLoading", noKey: "calNoKey", empty: "calEmpty", view: "calView" };
    Object.keys(ids).forEach(function (k) {
      var el = document.getElementById(ids[k]);
      if (el) el.style.display = (k === which) ? (k === "view" ? "block" : "flex") : "none";
    });
  }

  // ── Rendering ──
  function render() {
    if (!state.loaded) { showState("loading"); return; }
    updatePeriodLabel();
    buildFilters();
    if (state.events.length === 0) { updateEmptyVariant(); showState("empty"); return; }
    showState("view");
    var view = document.getElementById("calView");
    view.innerHTML = state.range === "month" ? renderMonth() : renderWeek();
    wireMoreButtons();
  }

  function updatePeriodLabel() {
    var label = "";
    if (state.range === "month") {
      label = new Intl.DateTimeFormat(LOCALE, { month: "long", year: "numeric" }).format(state.anchor);
    } else {
      var s = startOfWeek(state.anchor), e = addDays(s, 6);
      var f = new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "short" });
      label = f.format(s) + " – " + f.format(e) + " " + e.getFullYear();
    }
    var el = document.getElementById("calPeriodLabel");
    if (el) el.textContent = label.charAt(0).toUpperCase() + label.slice(1);
  }

  function pillHtml(ev) {
    var imgSrc = evImg(ev, "w185");
    var poster = imgSrc ? '<img class="cal-pill-poster" loading="lazy" src="' + imgSrc + '" alt="">' : "";
    var cls = ev.source === "seerr" ? " cal-pill-seerr" : (isCR(ev) ? " cal-pill-cr" : "");
    return '<div class="cal-pill' + cls + '" title="' + esc(ev.title) + " · " + epLabel(ev) +
      (ev.name ? " · " + esc(ev.name) : "") + (ev.source === "seerr" ? " · Seerr" : "") +
      (isCR(ev) ? " · Crunchyroll" : "") + '">' +
      poster +
      '<div class="cal-pill-text">' +
      '<span class="cal-pill-title">' + esc(ev.title) + '</span>' +
      '<span class="cal-pill-ep">' + epLabel(ev) + seerrTag(ev) + crTag(ev) + '</span>' +
      '</div></div>';
  }

  function renderMonth() {
    var first = new Date(state.anchor.getFullYear(), state.anchor.getMonth(), 1);
    var gridStart = startOfWeek(first);
    var today = new Date();
    var month = state.anchor.getMonth();

    var html = '<div class="cal-month-grid ' + (state.layout === "grid" ? "cal-layout-grid" : "") + '">';
    WEEKDAYS.forEach(function (w) { html += '<div class="cal-weekday">' + esc(w) + '</div>'; });

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
      var numAttrs = hasEvents ? ' data-day="' + key + '"' : '';
      var cellAttrs = hasEvents ? ' data-day="' + key + '"' : '';

      html += '<div class="' + cls + '"' + cellAttrs + '>';
      html += '<span class="' + numCls + '"' + numAttrs + '>' + d.getDate() + '</span>';
      // Compact event-count dot — only visible on small screens (CSS-driven)
      if (hasEvents) {
        html += '<span class="cal-day-dot" aria-hidden="true">' + evs.length + '</span>';
      }
      html += '<div class="cal-day-events">';

      var limit = state.layout === "grid" ? 1 : 3;
      evs.slice(0, limit).forEach(function (ev) { html += pillHtml(ev); });
      if (evs.length > limit) {
        html += '<span class="cal-day-more" data-day="' + key + '">+' + (evs.length - limit) + " " +
          t("weitere", "more") + "</span>";
      }
      html += "</div></div>";
    }
    html += "</div>";
    return html;
  }

  function renderWeek() {
    var s = startOfWeek(state.anchor);
    var today = new Date();
    var html = '<div class="cal-list">';

    for (var i = 0; i < 7; i++) {
      var d = addDays(s, i);
      var key = dayKey(d);
      var evs = state.byDay[key] || [];
      var isToday = sameDay(d, today);

      html += '<div class="cal-list-day' + (isToday ? " cal-today" : "") + '">';
      html += '<div class="cal-list-day-header">';
      html += '<span class="cal-list-day-weekday">' +
        new Intl.DateTimeFormat(LOCALE, { weekday: "long" }).format(d) + "</span>";
      html += '<span class="cal-list-day-date">' +
        new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "long" }).format(d) + "</span>";
      html += '<span class="cal-list-day-badge">' + evs.length + " " +
        (evs.length === 1 ? t("Episode", "episode") : t("Episoden", "episodes")) + "</span>";
      html += "</div>";

      if (evs.length === 0) {
        html += '<div class="cal-row" style="color:var(--text-muted);font-size:0.82rem;">' +
          t("Keine Episoden", "No episodes") + "</div>";
      } else if (state.layout === "grid") {
        html += '<div class="cal-tiles">';
        evs.forEach(function (ev) { html += tileHtml(ev); });
        html += "</div>";
      } else {
        html += '<div class="cal-list-events">';
        evs.forEach(function (ev) { html += rowHtml(ev); });
        html += "</div>";
      }
      html += "</div>";
    }
    html += "</div>";
    return html;
  }

  function tileHtml(ev) {
    var img = evImg(ev, "w300");
    var poster = img
      ? '<img class="cal-tile-poster" loading="lazy" src="' + img + '" alt="">'
      : '<div class="cal-tile-poster"></div>';
    var cls = ev.source === "seerr" ? " cal-tile-seerr" : (isCR(ev) ? " cal-tile-cr" : "");
    return '<div class="cal-tile' + cls + '" title="' + esc(ev.title) + '">' + poster +
      '<div class="cal-tile-body">' +
      '<div class="cal-tile-title">' + esc(ev.title) + '</div>' +
      '<div class="cal-tile-sub">' + epLabel(ev) + seerrTag(ev) + crTag(ev) + '</div>' +
      (ev.name ? '<div class="cal-tile-name">' + esc(ev.name) + '</div>' : "") +
      '</div></div>';
  }

  // Row layout (list)
  function rowHtml(ev) {
    var img = evImg(ev, "w154");
    var poster = img
      ? '<img class="cal-row-poster" loading="lazy" src="' + img + '" alt="">'
      : '<div class="cal-row-poster"></div>';
    var cls = ev.source === "seerr" ? " cal-row-seerr" : (isCR(ev) ? " cal-row-cr" : "");
    return '<div class="cal-row' + cls + '">' + poster +
      '<div class="cal-row-info">' +
      '<div class="cal-row-title">' + esc(ev.title) + seerrTag(ev) + crTag(ev) + '</div>' +
      (ev.name ? '<div class="cal-row-sub">' + esc(ev.name) + '</div>' : "") +
      '</div>' +
      '<span class="cal-row-ep-badge">' + epLabel(ev) + '</span>' +
      '</div>';
  }

  // Close popover when clicking elsewhere
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
    if (evs.length === 0) return;

    var pop = document.createElement("div");
    pop.className = "cal-popover";

    var d = parseDay(dateKey);
    var fmt = new Intl.DateTimeFormat(LOCALE, { weekday: "long", day: "numeric", month: "long" });
    var headerText = fmt.format(d);

    var html = '<div class="cal-popover-header">' + esc(headerText) + '</div>';
    html += '<div class="cal-popover-list">';

    evs.forEach(function (ev) {
      var img = evImg(ev, "w154");
      var posterHtml = img
        ? '<img class="cal-popover-img" loading="lazy" src="' + img + '" alt="">'
        : '<div class="cal-popover-img"></div>';

      var badgeClass = ev.source === "seerr" ? " seerr-badge" : (isCR(ev) ? " cr-badge" : "");
      var badgeHtml = '<span class="cal-popover-ep-badge' + badgeClass + '">' + epLabel(ev) + seerrTag(ev) + crTag(ev) + '</span>';

      html += '<div class="cal-popover-item" title="' + esc(ev.title) + '">' +
        posterHtml +
        '<div class="cal-popover-info">' +
          '<div class="cal-popover-title">' + esc(ev.title) + '</div>' +
          (ev.name ? '<div class="cal-popover-sub">' + esc(ev.name) + '</div>' : "") +
        '</div>' +
        badgeHtml +
      '</div>';
    });

    html += '</div>';
    pop.innerHTML = html;
    document.body.appendChild(pop);

    var rect = btnEl.getBoundingClientRect();
    var popWidth = Math.min(300, window.innerWidth - 20); // matches CSS max-width: 90vw-ish
    var popHeight = pop.offsetHeight;

    // Horizontal: centre on the button, clamped to the viewport
    var left = rect.left + window.scrollX - (popWidth / 2) + (rect.width / 2);
    if (left < 10) left = 10;
    if (left + popWidth > window.innerWidth - 10) {
      left = window.innerWidth - popWidth - 10;
    }

    // Vertical: open below by default, but flip above if it would overflow the
    // viewport bottom and there is more room above.
    var top = rect.bottom + window.scrollY + 6;
    var spaceBelow = window.innerHeight - rect.bottom;
    var spaceAbove = rect.top;
    if (spaceBelow < popHeight + 12 && spaceAbove > spaceBelow) {
      top = rect.top + window.scrollY - popHeight - 6;
      if (top < window.scrollY + 10) top = window.scrollY + 10;
    }

    pop.style.top = top + "px";
    pop.style.left = left + "px";
  }

  // Show popover on "+N more", day number, or anywhere on a day cell with events
  // (the whole cell is the tap target — important on touch / small screens).
  function wireMoreButtons() {
    document.querySelectorAll(".cal-day-more, .cal-day-num.cal-has-events").forEach(function (el) {
      el.addEventListener("click", function (e) {
        e.stopPropagation();
        showDayPopover(el, el.getAttribute("data-day"));
      });
    });
    document.querySelectorAll(".cal-day.cal-day-clickable").forEach(function (cell) {
      cell.addEventListener("click", function (e) {
        e.stopPropagation();
        showDayPopover(cell, cell.getAttribute("data-day"));
      });
    });
  }

  // ── Navigation ──
  function navigate(dir) {
    if (state.range === "month") {
      state.anchor = new Date(state.anchor.getFullYear(), state.anchor.getMonth() + dir, 1);
    } else {
      state.anchor = addDays(state.anchor, dir * 7);
    }
    render();
  }

  function setRange(range) {
    state.range = range;
    localStorage.setItem("aw-cal-range", range);
    document.querySelectorAll("#calRangeToggle .cal-seg-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-range") === range);
    });
    render();
  }

  function setLayout(layout) {
    state.layout = layout;
    localStorage.setItem("aw-cal-layout", layout);
    document.querySelectorAll("#calLayoutToggle .cal-seg-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-layout") === layout);
    });
    render();
  }

  // ── Init ──
  document.addEventListener("DOMContentLoaded", function () {
    document.getElementById("calPrevBtn").addEventListener("click", function () { navigate(-1); });
    document.getElementById("calNextBtn").addEventListener("click", function () { navigate(1); });
    document.getElementById("calTodayBtn").addEventListener("click", function () {
      state.anchor = new Date();
      render();
    });
    document.querySelectorAll("#calRangeToggle .cal-seg-btn").forEach(function (b) {
      b.addEventListener("click", function () { setRange(b.getAttribute("data-range")); });
    });
    document.querySelectorAll("#calLayoutToggle .cal-seg-btn").forEach(function (b) {
      b.addEventListener("click", function () { setLayout(b.getAttribute("data-layout")); });
    });

    // Restore persisted toggle states
    setRange(state.range);
    setLayout(state.layout);

    showState("loading");
    load();

    // Poll calendar data & watcher status silently every 10 seconds
    setInterval(function () {
      load(true);
    }, 10000);
  });
})();
