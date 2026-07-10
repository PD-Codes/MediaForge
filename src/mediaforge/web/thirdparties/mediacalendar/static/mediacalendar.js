/* Media Calendar -- all client-side logic for the "Media Kalender" page
 * (three internal sections: My Calendars / My Lists / Settings). Vanilla
 * JS, no build step, no shared-core JS dependency -- everything here talks
 * only to this integration's own /api/media-calendar/* routes (routes.py)
 * plus the generic per-item settings API every thirdparty gets for free
 * (/api/settings/thirdparty/mediacalendar), so this file has zero coupling
 * to any other page's script.
 */

const MC_POSTER_BASE = "https://image.tmdb.org/t/p/w185";
const MC_GENRE_CACHE = {}; // media_type -> [{id,name}]
const MC_PROVIDER_CACHE = {}; // media_type -> [{provider_id,provider_name,logo_path}]

function mcApi(url, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  return fetch(url, opts).then(async (r) => {
    let data;
    try { data = await r.json(); } catch (e) { data = {}; }
    return data;
  });
}

function mcEsc(s) {
  return (s == null ? "" : String(s)).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function mcPoster(path) {
  return path ? MC_POSTER_BASE + path : "";
}

// --- Tabs --------------------------------------------------------------

window.toggleMcCalendarList = function(forceCollapse) {
  const group = document.getElementById("mcCalendarListGroup");
  const title = document.querySelector("#mcPanelCalendars .mc-collapsible-title");
  if (!group || !title) return;
  
  const collapsed = typeof forceCollapse === "boolean" ? forceCollapse : !group.classList.contains("collapsed");
  group.classList.toggle("collapsed", collapsed);
  title.classList.toggle("collapsed-title", collapsed);
};

window.toggleMcListList = function(forceCollapse) {
  const group = document.getElementById("mcListListGroup");
  const title = document.querySelector("#mcPanelLists .mc-collapsible-title");
  if (!group || !title) return;
  
  const collapsed = typeof forceCollapse === "boolean" ? forceCollapse : !group.classList.contains("collapsed");
  group.classList.toggle("collapsed", collapsed);
  title.classList.toggle("collapsed-title", collapsed);
};

function switchMcTab(tab) {
  document.querySelectorAll(".mc-tab").forEach((el) => {
    el.classList.toggle("mc-tab-active", el.dataset.tab === tab);
  });
  document.getElementById("mcPanelCalendars").classList.toggle("mc-panel-active", tab === "calendars");
  document.getElementById("mcPanelLists").classList.toggle("mc-panel-active", tab === "lists");
  document.getElementById("mcPanelPlanned").classList.toggle("mc-panel-active", tab === "planned");
  document.getElementById("mcPanelSettings").classList.toggle("mc-panel-active", tab === "settings");
  if (window.history && history.pushState) {
    history.pushState(null, "", "/media-calendar/" + tab);
  }
  if (tab === "calendars") {
    const detail = document.getElementById("mcCalendarDetail");
    if (!detail || detail.style.display === "none") {
      window.toggleMcCalendarList(false);
    }
    McCalendars.load();
  }
  else if (tab === "lists") {
    const detail = document.getElementById("mcListDetail");
    if (!detail || detail.style.display === "none") {
      window.toggleMcListList(false);
    }
    McLists.load();
  }
  else if (tab === "planned") McPlanned.load();
  else if (tab === "settings") McSettings.load();
}

window.addEventListener("popstate", () => {
  const parts = window.location.pathname.split("/");
  const tab = parts[parts.length - 1];
  switchMcTab(tab === "media-calendar" ? "calendars" : tab);
});

document.addEventListener("DOMContentLoaded", () => {
  switchMcTab(window.MC_ACTIVE_TAB || "calendars");
});

// --- Shared: TMDB search picker (used by both calendars & lists) -------

function mcBuildSearchPicker(containerId, onPick) {
  const el = document.getElementById(containerId);
  el.innerHTML = `
    <div class="mc-search-row">
      <input type="text" placeholder="${mcEsc(_mcT("Search movie or show..."))}" id="${containerId}_input">
    </div>
    <div class="mc-search-results" id="${containerId}_results"></div>`;
  const input = document.getElementById(containerId + "_input");
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) {
      document.getElementById(containerId + "_results").innerHTML = "";
      return;
    }
    timer = setTimeout(async () => {
      const data = await mcApi("/api/media-calendar/tmdb/search?q=" + encodeURIComponent(q));
      const results = data.results || [];
      const resEl = document.getElementById(containerId + "_results");
      resEl.innerHTML = results.slice(0, 12).map((r) => `
        <div class="mc-search-result" data-id="${r.id}" data-type="${r.media_type}">
          <img src="${mcPoster(r.poster_path)}" loading="lazy">
          <div>
            <div class="mc-search-result-title">${mcEsc(r.title || r.name)}</div>
            <div class="mc-search-result-meta">${r.media_type === "movie" ? _mcT("Movie") : _mcT("TV Show")} · ${(r.release_date || r.first_air_date || "").slice(0, 4)}</div>
          </div>
        </div>`).join("") || `<div class="mc-empty">${mcEsc(_mcT("No results"))}</div>`;
      resEl.querySelectorAll(".mc-search-result").forEach((rowEl, i) => {
        rowEl.addEventListener("click", () => {
          onPick(results[i]);
          input.value = "";
          resEl.innerHTML = "";
        });
      });
    }, 350);
  });
}

// Server-rendered page chrome (tabs, headers, banners) goes through
// Jinja's {{ _() }} and is translated via this integration's own
// translations/de catalog. Strings built dynamically in this file are
// deliberately left untranslated (English source only) -- the same
// approach anime_seasons_view.js takes for its client-built markup --
// since a client-side i18n catalog would be another moving part this
// single-file script doesn't need. _mcT() exists as the single seam
// where that could be added later without touching every call site.
function _mcT(s) {
  return (window.MC_I18N && window.MC_I18N[s]) || s;
}

// --- Accordion helper ----------------------------------------------------

function mcAccordion(items) {
  // items: [{id, title, bodyHtml}]
  return items.map((it) => `
    <div class="mc-accordion-item" id="acc_${it.id}">
      <div class="mc-accordion-header" onclick="mcToggleAccordion('${it.id}')">${mcEsc(it.title)}</div>
      <div class="mc-accordion-body">${it.bodyHtml}</div>
    </div>`).join("");
}

function mcToggleAccordion(id) {
  document.getElementById("acc_" + id).classList.toggle("mc-open");
}

async function mcLoadGenres(mediaType) {
  if (MC_GENRE_CACHE[mediaType]) return MC_GENRE_CACHE[mediaType];
  const data = await mcApi("/api/media-calendar/tmdb/genres?media_type=" + mediaType);
  MC_GENRE_CACHE[mediaType] = data.genres || [];
  return MC_GENRE_CACHE[mediaType];
}

async function mcLoadProviders(mediaType) {
  if (MC_PROVIDER_CACHE[mediaType]) return MC_PROVIDER_CACHE[mediaType];
  const data = await mcApi("/api/media-calendar/tmdb/providers?media_type=" + mediaType);
  MC_PROVIDER_CACHE[mediaType] = data.providers || [];
  return MC_PROVIDER_CACHE[mediaType];
}

// =========================================================================
// Calendars
// =========================================================================

const McCalendars = (() => {
  let calendars = [];
  let draft = null; // editing state
  let editingId = null;
  let detailId = null;

  function emptyDraft() {
    return {
      name: "", color: "#7c3aed", media_types: ["movie", "tv"], source: "discover",
      combine_list_with_discover: false, provider_filter_mode: "include",
      library_filter: "any", seerr_filter: "any", sort_order: 0,
      genres: [], keywords: [], providers: [],
      manual: [], excluded: [],
      list_ids: { source: [], positive: [], negative: [] },
    };
  }

  async function load() {
    const data = await mcApi("/api/media-calendar/calendars");
    calendars = data.calendars || [];
    render();
  }

  function render() {
    const el = document.getElementById("mcCalendarList");
    if (!calendars.length) {
      el.innerHTML = `<div class="mc-empty">${mcEsc(_mcT("No calendars yet -- create one to start tracking upcoming releases."))}</div>`;
      return;
    }
    el.innerHTML = calendars.map((c) => `
      <div class="mc-card" data-id="${c.id}">
        <div class="mc-card-head">
          <span class="mc-color-dot" style="background:${mcEsc(c.color)}"></span>
          <span class="mc-card-title">${mcEsc(c.name)}</span>
        </div>
        <div class="mc-card-meta">
          ${_mcT(c.source === "discover" ? "Discover filter" : c.source === "list" ? "From lists" : "Library")}
          · ${c.media_types.map((t) => t === "movie" ? _mcT("Movies") : _mcT("TV Shows")).join(", ")}
        </div>
        <div class="mc-card-actions">
          <button class="mc-btn mc-btn-sm" data-act="open">${mcEsc(_mcT("Open"))}</button>
          <button class="mc-btn mc-btn-sm" data-act="edit">${mcEsc(_mcT("Edit"))}</button>
          <button class="mc-btn mc-btn-sm" data-act="dup">${mcEsc(_mcT("Duplicate"))}</button>
          <button class="mc-btn mc-btn-sm mc-btn-danger" data-act="del">${mcEsc(_mcT("Delete"))}</button>
        </div>
      </div>`).join("");
    el.querySelectorAll(".mc-card").forEach((card) => {
      const id = parseInt(card.dataset.id, 10);
      card.querySelector('[data-act="open"]').addEventListener("click", (e) => { e.stopPropagation(); openDetail(id); });
      card.querySelector('[data-act="edit"]').addEventListener("click", (e) => { e.stopPropagation(); openEditor(id); });
      card.querySelector('[data-act="dup"]').addEventListener("click", async (e) => {
        e.stopPropagation();
        await mcApi(`/api/media-calendar/calendars/${id}/duplicate`, { method: "POST" });
        load();
      });
      card.querySelector('[data-act="del"]').addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(_mcT("Delete this calendar?"))) return;
        await mcApi(`/api/media-calendar/calendars/${id}`, { method: "DELETE" });
        if (detailId === id) { document.getElementById("mcCalendarDetail").style.display = "none"; detailId = null; }
        load();
      });
      card.addEventListener("click", () => openDetail(id));
    });
  }

  // Release calendar (month/week view) -- adapted from web/static/calendar.js's
  // rendering patterns (indexEvents/renderMonth/renderWeek/pillHtml/tileHtml/
  // rowHtml/showDayPopover/navigate/setRange/setLayout) but bound to this
  // calendar's own resolved TMDB releases (is_new/in_library/in_autosync/
  // planned_download/watched/hidden -- see service.py's _postprocess) instead
  // of AutoSync/Seerr/Crunchyroll events, and with one accent color instead
  // of per-source colors. Persisted range/layout prefs are shared with every
  // calendar the user opens (same "mc-cal-*" localStorage keys as the app's
  // own /calendar page uses for "aw-cal-*", just namespaced separately).
  const calState = {
    anchor: new Date(),
    range: localStorage.getItem("mc-cal-range") || "month",
    layout: localStorage.getItem("mc-cal-layout") || "grid",
    releases: [],
    byDay: {},
  };

  async function openDetail(id, forceRefresh) {
    detailId = id;
    if (window.toggleMcCalendarList) {
      window.toggleMcCalendarList(true);
    }
    const cal = calendars.find((c) => c.id === id);
    const detailEl = document.getElementById("mcCalendarDetail");
    detailEl.style.display = "block";
    detailEl.innerHTML = `<div class="mc-empty">${mcEsc(_mcT("Loading..."))}</div>`;
    const url = `/api/media-calendar/calendars/${id}/releases` + (forceRefresh ? "?refresh=1" : "");
    const data = await mcApi(url);
    if (data.error) {
      detailEl.innerHTML = `
        <div class="mc-detail-head"><h2>${mcEsc(cal ? cal.name : "")}</h2></div>
        <div class="mc-banner mc-banner-error">${mcEsc(data.error)}</div>`;
      return;
    }
    calState.releases = data.releases || [];
    calState.byDay = mcCalIndexByDay(calState.releases);
    detailEl.innerHTML = `
      <div class="mc-detail-head">
        <h2>${mcEsc(cal ? cal.name : "")}</h2>
        <div class="mc-detail-actions">
          <button class="mc-btn mc-btn-sm" id="mcCalRefreshBtn">${mcEsc(_mcT("Refresh"))}</button>
          <a class="mc-btn mc-btn-sm" href="/api/media-calendar/calendars/${id}/ics">${mcEsc(_mcT("Export .ics"))}</a>
        </div>
      </div>
      <div class="mc-cal-toolbar">
        <div class="mc-cal-nav">
          <button class="mc-btn mc-cal-nav-btn" id="mcCalPrevBtn" title="${mcEsc(_mcT("Back"))}" aria-label="${mcEsc(_mcT("Back"))}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"></polyline></svg>
          </button>
          <button class="mc-btn mc-btn-sm" id="mcCalTodayBtn">${mcEsc(_mcT("Today"))}</button>
          <button class="mc-btn mc-cal-nav-btn" id="mcCalNextBtn" title="${mcEsc(_mcT("Next"))}" aria-label="${mcEsc(_mcT("Next"))}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>
          </button>
          <span class="mc-cal-period-label" id="mcCalPeriodLabel"></span>
        </div>
        <div class="mc-cal-toolbar-spacer"></div>
        <div class="mc-cal-segmented" id="mcCalRangeToggle" role="tablist">
          <button class="mc-cal-seg-btn" data-range="month" role="tab">${mcEsc(_mcT("Month"))}</button>
          <button class="mc-cal-seg-btn" data-range="week" role="tab">${mcEsc(_mcT("Week"))}</button>
        </div>
        <div class="mc-cal-segmented" id="mcCalLayoutToggle" role="tablist">
          <button class="mc-cal-seg-btn" data-layout="grid" role="tab" title="${mcEsc(_mcT("Grid view"))}" aria-label="${mcEsc(_mcT("Grid view"))}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect></svg>
          </button>
          <button class="mc-cal-seg-btn" data-layout="list" role="tab" title="${mcEsc(_mcT("List view"))}" aria-label="${mcEsc(_mcT("List view"))}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"></line><line x1="8" y1="12" x2="21" y2="12"></line><line x1="8" y1="18" x2="21" y2="18"></line><line x1="3" y1="6" x2="3.01" y2="6"></line><line x1="3" y1="12" x2="3.01" y2="12"></line><line x1="3" y1="18" x2="3.01" y2="18"></line></svg>
          </button>
        </div>
      </div>
      <div class="mc-cal-view" id="mcCalView"></div>`;
    document.getElementById("mcCalRefreshBtn").addEventListener("click", () => openDetail(id, true));
    document.getElementById("mcCalPrevBtn").addEventListener("click", () => mcCalNavigate(-1));
    document.getElementById("mcCalNextBtn").addEventListener("click", () => mcCalNavigate(1));
    document.getElementById("mcCalTodayBtn").addEventListener("click", () => { calState.anchor = new Date(); mcCalRender(); });
    document.querySelectorAll("#mcCalRangeToggle .mc-cal-seg-btn").forEach((b) => {
      b.addEventListener("click", () => mcCalSetRange(b.dataset.range));
    });
    document.querySelectorAll("#mcCalLayoutToggle .mc-cal-seg-btn").forEach((b) => {
      b.addEventListener("click", () => mcCalSetLayout(b.dataset.layout));
    });
    mcCalBindActions();
    mcCalSetRange(calState.range, true);
    mcCalSetLayout(calState.layout, true);
    mcCalRender();
  }

  // --- Calendar state helpers ---------------------------------------------

  function mcCalSetRange(range, skipRender) {
    calState.range = range;
    localStorage.setItem("mc-cal-range", range);
    document.querySelectorAll("#mcCalRangeToggle .mc-cal-seg-btn").forEach((b) => {
      b.classList.toggle("mc-active", b.dataset.range === range);
    });
    if (!skipRender) mcCalRender();
  }

  function mcCalSetLayout(layout, skipRender) {
    calState.layout = layout;
    localStorage.setItem("mc-cal-layout", layout);
    document.querySelectorAll("#mcCalLayoutToggle .mc-cal-seg-btn").forEach((b) => {
      b.classList.toggle("mc-active", b.dataset.layout === layout);
    });
    if (!skipRender) mcCalRender();
  }

  function mcCalNavigate(dir) {
    if (calState.range === "month") {
      calState.anchor = new Date(calState.anchor.getFullYear(), calState.anchor.getMonth() + dir, 1);
    } else {
      calState.anchor = mcCalAddDays(calState.anchor, dir * 7);
    }
    mcCalRender();
  }

  // --- Rendering -----------------------------------------------------------

  function mcCalRender() {
    const view = document.getElementById("mcCalView");
    if (!view) return;
    mcCalUpdatePeriodLabel();
    if (!calState.releases.length) {
      view.innerHTML = `<div class="mc-empty">${mcEsc(_mcT("Nothing found for the current filter."))}</div>`;
      return;
    }
    view.innerHTML = calState.range === "month" ? mcCalRenderMonth() : mcCalRenderWeek();
    mcCalWireDayInteractions();
  }

  function mcCalUpdatePeriodLabel() {
    const el = document.getElementById("mcCalPeriodLabel");
    if (!el) return;
    const locale = window.__LANG === "de" ? "de-DE" : "en-US";
    let label;
    if (calState.range === "month") {
      label = new Intl.DateTimeFormat(locale, { month: "long", year: "numeric" }).format(calState.anchor);
    } else {
      const s = mcCalStartOfWeek(calState.anchor), e = mcCalAddDays(s, 6);
      const f = new Intl.DateTimeFormat(locale, { day: "numeric", month: "short" });
      label = f.format(s) + " – " + f.format(e) + " " + e.getFullYear();
    }
    el.textContent = label.charAt(0).toUpperCase() + label.slice(1);
  }

  function mcCalWeekdays() {
    const locale = window.__LANG === "de" ? "de-DE" : "en-US";
    const ref = mcCalStartOfWeek(new Date());
    const fmt = new Intl.DateTimeFormat(locale, { weekday: "short" });
    const out = [];
    for (let i = 0; i < 7; i++) out.push(fmt.format(mcCalAddDays(ref, i)));
    return out;
  }

  function mcCalRenderMonth() {
    const first = new Date(calState.anchor.getFullYear(), calState.anchor.getMonth(), 1);
    const gridStart = mcCalStartOfWeek(first);
    const today = new Date();
    const month = calState.anchor.getMonth();

    let html = `<div class="mc-cal-month-grid${calState.layout === "grid" ? " mc-cal-layout-grid" : ""}">`;
    mcCalWeekdays().forEach((w) => { html += `<div class="mc-cal-weekday">${mcEsc(w)}</div>`; });

    for (let i = 0; i < 42; i++) {
      const d = mcCalAddDays(gridStart, i);
      const key = mcCalDayKey(d);
      const evs = calState.byDay[key] || [];
      let cls = "mc-cal-day";
      if (d.getMonth() !== month) cls += " mc-cal-day-muted";
      if (mcCalSameDay(d, today)) cls += " mc-cal-today";
      const hasEvents = evs.length > 0;
      if (hasEvents) cls += " mc-cal-day-clickable";
      const numCls = "mc-cal-day-num" + (hasEvents ? " mc-cal-has-events" : "");
      const dayAttr = hasEvents ? ` data-day="${key}"` : "";

      html += `<div class="${cls}"${dayAttr}>`;
      html += `<span class="${numCls}"${dayAttr}>${d.getDate()}</span>`;
      html += `<div class="mc-cal-day-events">`;
      const limit = calState.layout === "grid" ? 1 : 3;
      evs.slice(0, limit).forEach((r) => { html += mcCalPillHtml(r); });
      if (evs.length > limit) {
        html += `<span class="mc-cal-day-more" data-day="${key}">+${evs.length - limit} ${mcEsc(_mcT("more"))}</span>`;
      }
      html += "</div>";
      // Mobile-only "at a glance" fallback (see mediacalendar.css's
      // @media (max-width: 640px) rule hiding .mc-cal-day-events entirely --
      // full pills don't fit a ~70px-tall day cell there). Without this, a
      // day with releases looked identical to an empty one on a phone --
      // .mc-cal-has-events only changes on :hover, which touch devices
      // never trigger -- so the only way to discover anything was tapping
      // every single day. A handful of small dots (one per release, capped)
      // gives a real overview at a glance; tapping the day still opens the
      // full popover exactly as before (see mcCalWireDayInteractions()).
      if (hasEvents) {
        const dotCap = 4;
        html += `<div class="mc-cal-day-dots"${dayAttr} aria-hidden="true">`;
        evs.slice(0, dotCap).forEach(() => { html += `<span class="mc-cal-day-dot"></span>`; });
        if (evs.length > dotCap) html += `<span class="mc-cal-day-dot-more">+${evs.length - dotCap}</span>`;
        html += `</div>`;
      }
      html += "</div>";
    }
    html += "</div>";
    return html;
  }

  function mcCalRenderWeek() {
    const s = mcCalStartOfWeek(calState.anchor);
    const today = new Date();
    const locale = window.__LANG === "de" ? "de-DE" : "en-US";
    let html = `<div class="mc-cal-list">`;
    for (let i = 0; i < 7; i++) {
      const d = mcCalAddDays(s, i);
      const key = mcCalDayKey(d);
      const evs = calState.byDay[key] || [];
      const isToday = mcCalSameDay(d, today);
      html += `<div class="mc-cal-list-day${isToday ? " mc-cal-today" : ""}">`;
      html += `<div class="mc-cal-list-day-header">`;
      html += `<span class="mc-cal-list-day-weekday">${mcEsc(new Intl.DateTimeFormat(locale, { weekday: "long" }).format(d))}</span>`;
      html += `<span class="mc-cal-list-day-date">${mcEsc(new Intl.DateTimeFormat(locale, { day: "numeric", month: "long" }).format(d))}</span>`;
      if (evs.length) {
        html += `<span class="mc-cal-list-day-badge">${evs.length} ${mcEsc(_mcT(evs.length === 1 ? "release" : "releases"))}</span>`;
      }
      html += "</div>";
      if (!evs.length) {
        html += `<div class="mc-cal-list-empty">${mcEsc(_mcT("No releases"))}</div>`;
      } else if (calState.layout === "grid") {
        html += `<div class="mc-cal-tiles">`;
        evs.forEach((r) => { html += mcCalTileHtml(r); });
        html += "</div>";
      } else {
        html += `<div class="mc-cal-list-events">`;
        evs.forEach((r) => { html += mcCalRowHtml(r); });
        html += "</div>";
      }
      html += "</div>";
    }
    html += "</div>";
    return html;
  }

  // --- Item markup (pill / tile / row / popover) ---------------------------

  function mcCalPad(n) { return n < 10 ? "0" + n : "" + n; }
  function mcCalDayKey(d) { return d.getFullYear() + "-" + mcCalPad(d.getMonth() + 1) + "-" + mcCalPad(d.getDate()); }
  function mcCalParseDay(s) {
    const p = (s || "").split("-");
    return new Date(parseInt(p[0], 10), parseInt(p[1], 10) - 1, parseInt(p[2], 10));
  }
  function mcCalSameDay(a, b) { return mcCalDayKey(a) === mcCalDayKey(b); }
  function mcCalStartOfWeek(d) {
    const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const dow = (x.getDay() + 6) % 7; // Monday first
    x.setDate(x.getDate() - dow);
    return x;
  }
  function mcCalAddDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }

  function mcCalEpLabel(r) {
    if (r.media_type === "movie") return _mcT("Movie");
    if ((r.season_number ?? -1) < 0) return "";
    return "S" + mcCalPad(r.season_number) + "E" + mcCalPad(r.episode_number < 0 ? 0 : r.episode_number);
  }

  function mcCalIndexByDay(releases) {
    const byDay = {};
    releases.forEach((r) => {
      if (r.hidden || !r.release_date) return;
      (byDay[r.release_date] = byDay[r.release_date] || []).push(r);
    });
    return byDay;
  }

  function mcCalKeyAttrs(r) {
    return `data-t="${r.tmdb_id}" data-mt="${r.media_type}" data-s="${r.season_number ?? -1}" data-e="${r.episode_number ?? -1}"`;
  }

  function mcCalFindRelease(el) {
    const tmdbId = parseInt(el.dataset.t, 10);
    const mediaType = el.dataset.mt;
    const season = parseInt(el.dataset.s, 10);
    const episode = parseInt(el.dataset.e, 10);
    return calState.releases.find((r) => r.tmdb_id === tmdbId && r.media_type === mediaType
      && (r.season_number ?? -1) === season && (r.episode_number ?? -1) === episode);
  }

  function mcCalBadgesHtml(r) {
    const badges = [];
    if (r.is_new) badges.push(`<span class="mc-cal-badge mc-cal-badge-new">${mcEsc(_mcT("New"))}</span>`);
    if (r.in_library) badges.push(`<span class="mc-cal-badge mc-cal-badge-lib">${mcEsc(_mcT("In library"))}</span>`);
    if (r.in_autosync) badges.push(`<span class="mc-cal-badge mc-cal-badge-sync">${mcEsc(_mcT("In-Sync"))}</span>`);
    else if (r.planned_download === "queued") badges.push(`<span class="mc-cal-badge mc-cal-badge-sync">${mcEsc(_mcT("In-Sync"))}</span>`);
    if (r.planned_download === "pending") badges.push(`<span class="mc-cal-badge mc-cal-badge-planned">${mcEsc(_mcT("Planned Download"))}</span>`);
    if (r.planned_download === "failed") badges.push(`<span class="mc-cal-badge mc-cal-badge-failed">${mcEsc(_mcT("Failed"))}</span>`);
    if (r.requested) badges.push(`<span class="mc-cal-badge mc-cal-badge-req">${mcEsc(_mcT("Requested"))}</span>`);
    return badges.join("");
  }

  // Click-through actions offered on a release: search across configured
  // providers (opens the shared aniSearchModal from app.js), plan/edit/unplan
  // an automatic hourly re-search + auto-download once released (only
  // offered while it's not already in the library), plus the existing
  // watched/hide toggles. Wired via a single delegated document click
  // listener (see mcCalBindActions) instead of per-render listeners, since
  // these buttons get re-created on every mcCalRender() call. "Plan auto-
  // download" opens McPlanned's language/path config modal (see
  // routes.py's api_planned_download_add, which doubles as add+edit)
  // instead of flagging immediately -- see mcCalBindActions' "plan-config".
  //
  // "Add to Auto Sync" is a separate, more immediate alternative to
  // "Plan auto-download": planning a release just flags it and waits
  // (checked hourly from the release date on) for the moment it actually
  // shows up on a site -- for someone who doesn't want to wait for THIS
  // specific release and would rather set up regular, ongoing Auto-Sync
  // for the whole series right now, this searches immediately and creates
  // (or reuses) a normal Auto-Sync job on the spot, no planned_downloads
  // row involved at all. Hidden once the release is already covered by
  // Auto-Sync (r.in_autosync). Reuses the same language/path modal as
  // "Plan auto-download" -- see McPlanned.openConfig()'s mode parameter.
  function mcCalActionsHtml(r) {
    const attrs = mcCalKeyAttrs(r);
    const planned = !!r.planned_download;
    return `<div class="mc-cal-actions">
      <button data-cal-act="search" ${attrs}>${mcEsc(_mcT("Search in MediaForge"))}</button>
      ${!r.in_autosync ? `<button data-cal-act="add-autosync" ${attrs}>${mcEsc(_mcT("Add to Auto Sync"))}</button>` : ""}
      ${!r.in_library ? `<button data-cal-act="plan-config" ${attrs}>${mcEsc(planned ? _mcT("Edit planned download") : _mcT("Plan auto-download"))}</button>` : ""}
      ${!r.in_library && planned ? `<button data-cal-act="plan-remove" ${attrs} class="mc-active">${mcEsc(_mcT("Remove planned download"))}</button>` : ""}
      <button data-cal-act="watched" ${attrs} class="${r.watched ? "mc-active" : ""}">${mcEsc(_mcT("Watched"))}</button>
      <button data-cal-act="hidden" ${attrs} class="${r.hidden ? "mc-active" : ""}">${mcEsc(_mcT("Hide"))}</button>
    </div>`;
  }

  // Applies a language/path config change (or a removal) made via
  // McPlanned's modal directly to the currently-loaded release list, so the
  // calendar view updates instantly without a full server refresh (the
  // modal itself has no view into calState, which is private to this
  // module). No-op if this release isn't part of the currently open
  // calendar (e.g. the config was edited from the "Planned Downloads" tab
  // instead of from a calendar).
  function applyPlannedConfig(tmdbId, mediaType, seasonNumber, episodeNumber, patch) {
    const r = calState.releases.find((x) => x.tmdb_id === tmdbId && x.media_type === mediaType
      && (x.season_number ?? -1) === seasonNumber && (x.episode_number ?? -1) === episodeNumber);
    if (!r) return;
    Object.assign(r, patch);
    calState.byDay = mcCalIndexByDay(calState.releases);
    mcCalRender();
  }

  function mcCalPillHtml(r) {
    const img = mcPoster(r.poster_path);
    const poster = img ? `<img class="mc-cal-pill-poster" loading="lazy" src="${img}" alt="">` : "";
    return `<div class="mc-cal-pill" ${mcCalKeyAttrs(r)} title="${mcEsc(r.title)}">
      ${poster}
      <div class="mc-cal-pill-text">
        <span class="mc-cal-pill-title">${mcEsc(r.title)}</span>
        <span class="mc-cal-pill-ep">${mcEsc(mcCalEpLabel(r))}</span>
      </div>
    </div>`;
  }

  function mcCalTileHtml(r) {
    const img = mcPoster(r.poster_path);
    const poster = img ? `<img class="mc-cal-tile-poster" loading="lazy" src="${img}" alt="">` : `<div class="mc-cal-tile-poster"></div>`;
    return `<div class="mc-cal-tile">
      ${poster}
      <div class="mc-cal-tile-body">
        <div class="mc-cal-tile-title">${mcEsc(r.title)}</div>
        <div class="mc-cal-tile-sub">${mcEsc(mcCalEpLabel(r))}</div>
        <div class="mc-cal-tile-badges">${mcCalBadgesHtml(r)}</div>
        ${mcCalActionsHtml(r)}
      </div>
    </div>`;
  }

  function mcCalRowHtml(r) {
    const img = mcPoster(r.poster_path);
    const poster = img ? `<img class="mc-cal-row-poster" loading="lazy" src="${img}" alt="">` : `<div class="mc-cal-row-poster"></div>`;
    return `<div class="mc-cal-row">
      ${poster}
      <div class="mc-cal-row-info">
        <div class="mc-cal-row-title">${mcEsc(r.title)}</div>
        <div class="mc-cal-row-badges">${mcCalBadgesHtml(r)}</div>
        ${mcCalActionsHtml(r)}
      </div>
      <span class="mc-cal-row-ep-badge">${mcEsc(mcCalEpLabel(r))}</span>
    </div>`;
  }

  // --- Day popover (month view "+N more" / day click) -----------------------

  let _mcCalOutsideClickBound = false;
  function mcCalShowDayPopover(anchorEl, dateKey) {
    const existing = document.querySelector(".mc-cal-popover");
    if (existing) existing.remove();
    const evs = calState.byDay[dateKey] || [];
    if (!evs.length) return;

    const pop = document.createElement("div");
    pop.className = "mc-cal-popover";
    const locale = window.__LANG === "de" ? "de-DE" : "en-US";
    const d = mcCalParseDay(dateKey);
    const headerText = new Intl.DateTimeFormat(locale, { weekday: "long", day: "numeric", month: "long" }).format(d);

    let html = `<div class="mc-cal-popover-header">${mcEsc(headerText)}</div><div class="mc-cal-popover-list">`;
    evs.forEach((r) => {
      const img = mcPoster(r.poster_path);
      const posterHtml = img ? `<img class="mc-cal-popover-img" loading="lazy" src="${img}" alt="">` : `<div class="mc-cal-popover-img"></div>`;
      html += `<div class="mc-cal-popover-item">
        ${posterHtml}
        <div class="mc-cal-popover-info">
          <div class="mc-cal-popover-title">${mcEsc(r.title)}</div>
          <div class="mc-cal-popover-sub">${mcEsc(mcCalEpLabel(r))}</div>
          <div class="mc-cal-popover-badges">${mcCalBadgesHtml(r)}</div>
          ${mcCalActionsHtml(r)}
        </div>
      </div>`;
    });
    html += "</div>";
    pop.innerHTML = html;
    document.body.appendChild(pop);

    const rect = anchorEl.getBoundingClientRect();
    const popWidth = Math.min(320, window.innerWidth - 20);
    const popHeight = pop.offsetHeight;
    let left = rect.left + window.scrollX - (popWidth / 2) + (rect.width / 2);
    if (left < 10) left = 10;
    if (left + popWidth > window.innerWidth - 10) left = window.innerWidth - popWidth - 10;
    let top = rect.bottom + window.scrollY + 6;
    const spaceBelow = window.innerHeight - rect.bottom;
    const spaceAbove = rect.top;
    if (spaceBelow < popHeight + 12 && spaceAbove > spaceBelow) {
      top = rect.top + window.scrollY - popHeight - 6;
      if (top < window.scrollY + 10) top = window.scrollY + 10;
    }
    pop.style.top = top + "px";
    pop.style.left = left + "px";

    if (!_mcCalOutsideClickBound) {
      _mcCalOutsideClickBound = true;
      document.addEventListener("click", (e) => {
        const openPop = document.querySelector(".mc-cal-popover");
        if (openPop && !openPop.contains(e.target) && !e.target.classList.contains("mc-cal-day-more")) {
          openPop.remove();
        }
      });
    }
  }

  function mcCalWireDayInteractions() {
    document.querySelectorAll("#mcCalView .mc-cal-day-more, #mcCalView .mc-cal-day-num.mc-cal-has-events").forEach((el) => {
      el.addEventListener("click", (e) => { e.stopPropagation(); mcCalShowDayPopover(el, el.getAttribute("data-day")); });
    });
    document.querySelectorAll("#mcCalView .mc-cal-day.mc-cal-day-clickable").forEach((cell) => {
      cell.addEventListener("click", (e) => { e.stopPropagation(); mcCalShowDayPopover(cell, cell.getAttribute("data-day")); });
    });
  }

  // --- Actions (delegated; buttons are re-created on every render) ---------

  let _mcCalActionsBound = false;
  function mcCalBindActions() {
    if (_mcCalActionsBound) return;
    _mcCalActionsBound = true;
    document.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-cal-act]");
      if (!btn) return;
      e.stopPropagation();
      const r = mcCalFindRelease(btn);
      if (!r) return;
      const act = btn.dataset.calAct;
      if (act === "search") {
        if (window.openAniSearchModal) window.openAniSearchModal(r.title, r.tmdb_id, r.media_type, r.poster_path);
        return;
      }
      if (act === "plan-config") {
        // Opens McPlanned's language/path modal instead of flagging
        // immediately -- McPlanned.saveConfig() calls back into
        // applyPlannedConfig() above once the user confirms, it does not
        // POST from here.
        McPlanned.openConfig(r);
        return;
      }
      if (act === "add-autosync") {
        // Same modal as "plan-config", but mode="autosync" -- see
        // McPlanned.openConfig()/saveConfig(): searches right now and sets
        // up regular Auto-Sync immediately instead of flagging + waiting.
        McPlanned.openConfig(r, "autosync");
        return;
      }
      const pop = document.querySelector(".mc-cal-popover");
      if (act === "plan-remove") {
        await mcApi(`/api/media-calendar/planned/${r.media_type}/${r.tmdb_id}?season_number=${r.season_number ?? -1}&episode_number=${r.episode_number ?? -1}`, { method: "DELETE" });
        r.planned_download = null;
        r.planned_language = null;
        r.planned_custom_path_id = null;
      } else if (act === "watched") {
        await mcApi("/api/media-calendar/progress", {
          method: "POST",
          body: JSON.stringify({
            tmdb_id: r.tmdb_id, media_type: r.media_type,
            season_number: r.season_number ?? -1, episode_number: r.episode_number ?? -1,
            watched: !r.watched,
          }),
        });
        r.watched = !r.watched;
      } else if (act === "hidden") {
        await mcApi("/api/media-calendar/progress", {
          method: "POST",
          body: JSON.stringify({
            tmdb_id: r.tmdb_id, media_type: r.media_type,
            season_number: r.season_number ?? -1, episode_number: r.episode_number ?? -1,
            hidden: !r.hidden,
          }),
        });
        r.hidden = !r.hidden;
        calState.byDay = mcCalIndexByDay(calState.releases);
      }
      if (pop) pop.remove();
      mcCalRender();
    });
  }

  function openEditor(id) {
    editingId = id;
    draft = id ? JSON.parse(JSON.stringify(calendars.find((c) => c.id === id))) : emptyDraft();
    document.getElementById("mcCalendarModalTitle").textContent = id ? _mcT("Edit calendar") : _mcT("New calendar");
    renderEditorBody();
    document.getElementById("mcCalendarModal").style.display = "flex";
  }

  function closeEditor() {
    document.getElementById("mcCalendarModal").style.display = "none";
    draft = null; editingId = null;
  }

  function mediaTypeCheckboxes() {
    return `
      <label><input type="checkbox" class="chb-main" id="mc_mt_movie" ${draft.media_types.includes("movie") ? "checked" : ""}> ${mcEsc(_mcT("Movies"))}</label>
      &nbsp;&nbsp;
      <label><input type="checkbox" class="chb-main" id="mc_mt_tv" ${draft.media_types.includes("tv") ? "checked" : ""}> ${mcEsc(_mcT("TV Shows"))}</label>`;
  }

  async function renderEditorBody() {
    const body = document.getElementById("mcCalendarModalBody");
    body.innerHTML = `<div class="mc-empty">${mcEsc(_mcT("Loading..."))}</div>`;
    // Must not read McLists' cache before it has actually fetched at least
    // once -- see McLists.ensureLoaded()'s docstring for why getCached()
    // alone isn't safe here (this was the "Noch keine Listen vorhanden"
    // false negative when a calendar's editor was opened before ever
    // visiting the "My Lists" tab in this page load).
    await McLists.ensureLoaded();
    const lists = McLists.getCached();
    body.innerHTML = `
      <div class="mc-field">
        <label>${mcEsc(_mcT("Name"))}</label>
        <input type="text" id="mc_cal_name" value="${mcEsc(draft.name)}">
      </div>
      <div class="mc-field-row">
        <div class="mc-field">
          <label>${mcEsc(_mcT("Color"))}</label>
          <input type="color" id="mc_cal_color" value="${mcEsc(draft.color)}">
        </div>
        <div class="mc-field">
          <label>${mcEsc(_mcT("Media types"))}</label>
          ${mediaTypeCheckboxes()}
        </div>
      </div>
      <div class="mc-field">
        <label>${mcEsc(_mcT("Source"))}</label>
        <select id="mc_cal_source">
          <option value="discover" ${draft.source === "discover" ? "selected" : ""}>${mcEsc(_mcT("TMDB Discover filter"))}</option>
          <option value="list" ${draft.source === "list" ? "selected" : ""}>${mcEsc(_mcT("From my lists"))}</option>
          <option value="library" ${draft.source === "library" ? "selected" : ""}>${mcEsc(_mcT("My media library"))}</option>
        </select>
      </div>
      <div id="mc_cal_accordion"></div>`;

    document.getElementById("mc_cal_source").addEventListener("change", (e) => {
      draft.source = e.target.value;
      renderAccordion();
    });
    renderAccordion();

    function renderAccordion() {
      const accEl = document.getElementById("mc_cal_accordion");
      const items = [];
      if (draft.source === "discover" || draft.source === "list") {
        items.push({
          id: "genres", title: _mcT("Genres"),
          bodyHtml: `<div id="mc_cal_genres"><div class="mc-empty">${mcEsc(_mcT("Loading..."))}</div></div>`,
        });
        items.push({
          id: "keywords", title: _mcT("Keywords"),
          bodyHtml: `
            <input type="text" id="mc_cal_kw_input" placeholder="${mcEsc(_mcT("Search keyword..."))}">
            <div class="mc-search-results" id="mc_cal_kw_results"></div>
            <div class="mc-chip-list" id="mc_cal_kw_chips"></div>`,
        });
        items.push({
          id: "providers", title: _mcT("Streaming providers"),
          bodyHtml: `
            <div id="mc_cal_providers"><div class="mc-empty">${mcEsc(_mcT("Loading..."))}</div></div>
            <div class="mc-field" style="margin-top:8px;">
              <label>${mcEsc(_mcT("Provider filter mode"))}</label>
              <select id="mc_cal_provider_mode">
                <option value="include" ${draft.provider_filter_mode === "include" ? "selected" : ""}>${mcEsc(_mcT("Only these providers"))}</option>
                <option value="exclude" ${draft.provider_filter_mode === "exclude" ? "selected" : ""}>${mcEsc(_mcT("Exclude these providers"))}</option>
              </select>
            </div>`,
        });
      }
      if (draft.source === "list") {
        items.push({
          id: "lists", title: _mcT("Linked lists"),
          bodyHtml: renderListLinkPicker(lists),
        });
        items.push({
          id: "combine", title: _mcT("Combine with filter"),
          bodyHtml: `
            <div class="mc-toggle-row">
              <div>
                <div class="mc-toggle-row-label">${mcEsc(_mcT("Also include Discover results"))}</div>
                <div class="mc-toggle-row-desc">${mcEsc(_mcT("Adds TMDB Discover matches (genres/keywords/providers above) on top of the linked lists."))}</div>
              </div>
              <label class="mc-switch"><input type="checkbox" id="mc_cal_combine" ${draft.combine_list_with_discover ? "checked" : ""}><span class="mc-switch-track"></span></label>
            </div>`,
        });
      } else {
        items.push({
          id: "fold", title: _mcT("Fold in / subtract lists"),
          // Folding in a list ADDS to whatever the Discover filter above
          // already matches -- it never replaces it. With no genre/keyword/
          // provider set, that Discover filter is unrestricted (TMDB
          // returns its full popularity-sorted feed, not "nothing"), so a
          // calendar meant to show ONLY a hand-picked list will instead
          // show that list buried in a huge Discover feed. Spelling this
          // out here since it looks like a bug otherwise -- the fix is to
          // switch Source to "From my lists" for list-only calendars.
          bodyHtml: `<div class="mc-hint">${mcEsc(_mcT("Adds these lists on top of whatever the Discover filter above matches -- with no genre/keyword/provider set, that's an unrestricted (large) result set, not \"nothing\". To show only these lists, switch Source to \"From my lists\" instead."))}</div>` + renderListLinkPicker(lists),
        });
      }
      items.push({
        id: "manual", title: _mcT("Manually included / excluded titles"),
        bodyHtml: `
          <div class="mc-field"><label>${mcEsc(_mcT("Add title"))}</label><div id="mc_cal_manual_search"></div></div>
          <div class="mc-field-row">
            <div class="mc-field">
              <label>${mcEsc(_mcT("Always included"))}</label>
              <div class="mc-chip-list" id="mc_cal_manual_chips"></div>
            </div>
            <div class="mc-field">
              <label>${mcEsc(_mcT("Always excluded"))}</label>
              <div class="mc-chip-list" id="mc_cal_excluded_chips"></div>
            </div>
          </div>
          <div class="mc-hint">${mcEsc(_mcT("Pick a search result, then use the buttons that appear to add it to either list."))}</div>`,
      });
      items.push({
        id: "status", title: _mcT("Library / request status filter"),
        bodyHtml: `
          <div class="mc-field">
            <label>${mcEsc(_mcT("Library status"))}</label>
            <select id="mc_cal_library_filter">
              <option value="any" ${draft.library_filter === "any" ? "selected" : ""}>${mcEsc(_mcT("Any"))}</option>
              <option value="in_library" ${draft.library_filter === "in_library" ? "selected" : ""}>${mcEsc(_mcT("Only in my library"))}</option>
              <option value="missing" ${draft.library_filter === "missing" ? "selected" : ""}>${mcEsc(_mcT("Only missing from my library"))}</option>
            </select>
          </div>
          <div class="mc-field">
            <label>${mcEsc(_mcT("Request status (Seerr)"))}</label>
            <select id="mc_cal_seerr_filter">
              <option value="any" ${draft.seerr_filter === "any" ? "selected" : ""}>${mcEsc(_mcT("Any"))}</option>
              <option value="requested" ${draft.seerr_filter === "requested" ? "selected" : ""}>${mcEsc(_mcT("Only requested"))}</option>
              <option value="not_requested" ${draft.seerr_filter === "not_requested" ? "selected" : ""}>${mcEsc(_mcT("Only not requested"))}</option>
            </select>
          </div>`,
      });

      accEl.innerHTML = mcAccordion(items);

      if (draft.source === "discover" || draft.source === "list") {
        loadGenreCheckboxes();
        loadProviderCheckboxes();
        wireKeywordSearch();
      }
      wireManualSearch();
      renderChips("mc_cal_manual_chips", draft.manual, (item) => removeRef("manual", item));
      renderChips("mc_cal_excluded_chips", draft.excluded, (item) => removeRef("excluded", item));
      wireListLinkPicker();

      const combineEl = document.getElementById("mc_cal_combine");
      if (combineEl) combineEl.addEventListener("change", (e) => { draft.combine_list_with_discover = e.target.checked; });
      document.getElementById("mc_cal_library_filter").addEventListener("change", (e) => { draft.library_filter = e.target.value; });
      document.getElementById("mc_cal_seerr_filter").addEventListener("change", (e) => { draft.seerr_filter = e.target.value; });
      const modeEl = document.getElementById("mc_cal_provider_mode");
      if (modeEl) modeEl.addEventListener("change", (e) => { draft.provider_filter_mode = e.target.value; });
    }

    async function loadGenreCheckboxes() {
      const mt = draft.media_types[0] || "movie";
      const genres = await mcLoadGenres(mt);
      const el = document.getElementById("mc_cal_genres");
      if (!el) return;
      el.innerHTML = genres.map((g) => `
        <label style="display:inline-block;width:48%;font-size:0.85em;margin-bottom:4px;">
          <input type="checkbox" class="chb-main mc_genre_cb" value="${g.id}" ${draft.genres.includes(g.id) ? "checked" : ""}> ${mcEsc(g.name)}
        </label>`).join("");
      el.querySelectorAll(".mc_genre_cb").forEach((cb) => {
        cb.addEventListener("change", () => {
          const id = parseInt(cb.value, 10);
          draft.genres = cb.checked ? [...draft.genres, id] : draft.genres.filter((x) => x !== id);
        });
      });
    }

    async function loadProviderCheckboxes() {
      const mt = draft.media_types[0] || "movie";
      const providers = await mcLoadProviders(mt);
      const el = document.getElementById("mc_cal_providers");
      if (!el) return;
      el.innerHTML = providers.map((p) => `
        <label style="display:inline-block;width:48%;font-size:0.85em;margin-bottom:4px;">
          <input type="checkbox" class="chb-main mc_provider_cb" value="${p.provider_id}" ${draft.providers.includes(p.provider_id) ? "checked" : ""}> ${mcEsc(p.provider_name)}
        </label>`).join("");
      el.querySelectorAll(".mc_provider_cb").forEach((cb) => {
        cb.addEventListener("change", () => {
          const id = parseInt(cb.value, 10);
          draft.providers = cb.checked ? [...draft.providers, id] : draft.providers.filter((x) => x !== id);
        });
      });
    }

    function wireKeywordSearch() {
      const input = document.getElementById("mc_cal_kw_input");
      if (!input) return;
      let timer = null;
      input.addEventListener("input", () => {
        clearTimeout(timer);
        const q = input.value.trim();
        if (q.length < 2) { document.getElementById("mc_cal_kw_results").innerHTML = ""; return; }
        timer = setTimeout(async () => {
          const data = await mcApi("/api/media-calendar/tmdb/keywords?q=" + encodeURIComponent(q));
          const resEl = document.getElementById("mc_cal_kw_results");
          // data-id/data-name (not just name) -- TMDB discover's with_keywords
          // filter needs the numeric keyword id, not its display name (see
          // service.py's _keyword_ids()); the name is only kept for the chip.
          resEl.innerHTML = (data.keywords || []).slice(0, 10).map((k) => `
            <div class="mc-search-result" data-id="${k.id}" data-name="${mcEsc(k.name)}"><div class="mc-search-result-title">${mcEsc(k.name)}</div></div>`).join("");
          resEl.querySelectorAll(".mc-search-result").forEach((rowEl) => {
            rowEl.addEventListener("click", () => {
              const id = parseInt(rowEl.dataset.id, 10);
              const name = rowEl.dataset.name;
              if (!draft.keywords.some((k) => k.id === id)) draft.keywords.push({ id, name });
              renderKwChips();
              input.value = ""; resEl.innerHTML = "";
            });
          });
        }, 350);
      });
      renderKwChips();
    }

    function renderKwChips() {
      const el = document.getElementById("mc_cal_kw_chips");
      if (!el) return;
      // Keywords saved before schema version 3 (or any bad row) have
      // id=0/null -- silently dropped from the actual TMDB filter (see
      // service.py's _keyword_ids(), 0 isn't a valid keyword id) rather
      // than sent as an invalid param, which used to zero out the whole
      // discover result. Flagging it here instead of leaving the user to
      // discover "my filter is being ignored" the hard way -- remove and
      // re-pick from the suggestion list to get a real id.
      el.innerHTML = draft.keywords.map((k, i) => `
        <span class="mc-chip${!k.id ? " mc-chip-stale" : ""}"${!k.id ? ` title="${mcEsc(_mcT("Invalid -- not applied. Remove and re-add it from the suggestions below."))}"` : ""}>
          ${mcEsc(k.name)}${!k.id ? " ⚠" : ""} <button data-i="${i}">&times;</button>
        </span>`).join("");
      el.querySelectorAll("button").forEach((btn) => {
        btn.addEventListener("click", () => {
          draft.keywords.splice(parseInt(btn.dataset.i, 10), 1);
          renderKwChips();
        });
      });
    }

    function wireManualSearch() {
      mcBuildSearchPicker("mc_cal_manual_search", (result) => {
        const ref = { tmdb_id: result.id, media_type: result.media_type, title: result.title || result.name, poster_path: result.poster_path };
        // Ask include vs exclude via a tiny inline confirm (kept dependency-free).
        if (confirm(_mcT("Add as EXCLUDED? (Cancel = add as always-included)"))) {
          draft.excluded = draft.excluded.filter((r) => !(r.tmdb_id === ref.tmdb_id && r.media_type === ref.media_type));
          draft.excluded.push(ref);
        } else {
          draft.manual = draft.manual.filter((r) => !(r.tmdb_id === ref.tmdb_id && r.media_type === ref.media_type));
          draft.manual.push(ref);
        }
        renderChips("mc_cal_manual_chips", draft.manual, (item) => removeRef("manual", item));
        renderChips("mc_cal_excluded_chips", draft.excluded, (item) => removeRef("excluded", item));
      });
    }

    function removeRef(bucket, item) {
      draft[bucket] = draft[bucket].filter((r) => !(r.tmdb_id === item.tmdb_id && r.media_type === item.media_type));
      renderChips("mc_cal_manual_chips", draft.manual, (i) => removeRef("manual", i));
      renderChips("mc_cal_excluded_chips", draft.excluded, (i) => removeRef("excluded", i));
    }

    function renderChips(containerId, items, onRemove) {
      const el = document.getElementById(containerId);
      if (!el) return;
      el.innerHTML = items.map((it) => `
        <span class="mc-chip" data-id="${it.tmdb_id}" data-type="${it.media_type}">${mcEsc(it.title)} <button>&times;</button></span>`).join("")
        || `<span class="mc-hint">${mcEsc(_mcT("None"))}</span>`;
      el.querySelectorAll(".mc-chip button").forEach((btn, i) => {
        btn.addEventListener("click", () => onRemove(items[i]));
      });
    }

    function renderListLinkPicker(lists) {
      if (!lists.length) {
        return `<div class="mc-hint">${mcEsc(_mcT("No lists yet -- create one under \"My Lists\" first."))}</div>`;
      }
      const role1 = draft.source === "list" ? "source" : "positive";
      return `
        <div class="mc-field">
          <label>${mcEsc(draft.source === "list" ? _mcT("Use these lists") : _mcT("Fold in (positive)"))}</label>
          ${lists.map((l) => `
            <label style="display:block;font-size:0.85em;margin-bottom:3px;">
              <input type="checkbox" class="chb-main mc_list_${role1}" value="${l.id}" ${draft.list_ids[role1].includes(l.id) ? "checked" : ""}> ${mcEsc(l.name)}
            </label>`).join("")}
        </div>
        ${draft.source !== "list" ? `
        <div class="mc-field">
          <label>${mcEsc(_mcT("Subtract (negative)"))}</label>
          ${lists.map((l) => `
            <label style="display:block;font-size:0.85em;margin-bottom:3px;">
              <input type="checkbox" class="chb-main mc_list_negative" value="${l.id}" ${draft.list_ids.negative.includes(l.id) ? "checked" : ""}> ${mcEsc(l.name)}
            </label>`).join("")}
        </div>` : ""}`;
    }

    function wireListLinkPicker() {
      const role1 = draft.source === "list" ? "source" : "positive";
      document.querySelectorAll(`.mc_list_${role1}`).forEach((cb) => {
        cb.addEventListener("change", () => {
          const id = parseInt(cb.value, 10);
          draft.list_ids[role1] = cb.checked
            ? [...draft.list_ids[role1], id]
            : draft.list_ids[role1].filter((x) => x !== id);
        });
      });
      document.querySelectorAll(".mc_list_negative").forEach((cb) => {
        cb.addEventListener("change", () => {
          const id = parseInt(cb.value, 10);
          draft.list_ids.negative = cb.checked
            ? [...draft.list_ids.negative, id]
            : draft.list_ids.negative.filter((x) => x !== id);
        });
      });
    }
  }

  async function save() {
    draft.name = document.getElementById("mc_cal_name").value.trim();
    if (!draft.name) { alert(_mcT("Please enter a name.")); return; }
    draft.color = document.getElementById("mc_cal_color").value;
    draft.media_types = [
      document.getElementById("mc_mt_movie").checked ? "movie" : null,
      document.getElementById("mc_mt_tv").checked ? "tv" : null,
    ].filter(Boolean);
    if (!draft.media_types.length) draft.media_types = ["movie", "tv"];

    const url = editingId ? `/api/media-calendar/calendars/${editingId}` : "/api/media-calendar/calendars";
    await mcApi(url, { method: editingId ? "PUT" : "POST", body: JSON.stringify(draft) });
    closeEditor();
    load();
  }

  return { load, openDetail, openEditor, closeEditor, save, getCached: () => calendars, applyPlannedConfig };
})();

// =========================================================================
// Lists
// =========================================================================

const McLists = (() => {
  let lists = [];
  let loaded = false;
  let draft = null;
  let editingId = null;
  let detailId = null;

  function emptyDraft() {
    return { name: "", dynamic_enabled: false, media_types: ["movie", "tv"], genres: [], keywords: [], providers: [], items: [] };
  }

  async function load() {
    const data = await mcApi("/api/media-calendar/lists");
    lists = data.lists || [];
    loaded = true;
    render();
  }

  // The "My Lists" tab only fetches its data when the user actually clicks
  // that tab (switchMcTab -> McLists.load()). Anything else that needs the
  // list of lists (the calendar editor's "Fold in / subtract lists" and
  // "Use these lists" pickers, via getCached()) must not assume that has
  // happened yet -- opening a calendar editor as the very first action on
  // the page previously read the still-empty initial `lists = []` and
  // showed "No lists yet" even though lists existed server-side. Call this
  // before getCached() anywhere outside the Lists tab itself.
  async function ensureLoaded() {
    if (!loaded) await load();
  }

  function render() {
    const el = document.getElementById("mcListList");
    if (!lists.length) {
      el.innerHTML = `<div class="mc-empty">${mcEsc(_mcT("No lists yet -- create one to curate titles or import from AniList."))}</div>`;
      return;
    }
    el.innerHTML = lists.map((l) => `
      <div class="mc-card" data-id="${l.id}">
        <div class="mc-card-head"><span class="mc-card-title">${mcEsc(l.name)}</span></div>
        <div class="mc-card-meta">
          ${l.items.length} ${mcEsc(_mcT("titles"))}
          ${l.dynamic_enabled ? `<span class="mc-badge mc-badge-accent">${mcEsc(_mcT("Dynamic"))}</span>` : ""}
        </div>
        <div class="mc-card-actions">
          <button class="mc-btn mc-btn-sm" data-act="open">${mcEsc(_mcT("Open"))}</button>
          <button class="mc-btn mc-btn-sm" data-act="edit">${mcEsc(_mcT("Edit"))}</button>
          <button class="mc-btn mc-btn-sm mc-btn-danger" data-act="del">${mcEsc(_mcT("Delete"))}</button>
        </div>
      </div>`).join("");
    el.querySelectorAll(".mc-card").forEach((card) => {
      const id = parseInt(card.dataset.id, 10);
      card.querySelector('[data-act="open"]').addEventListener("click", (e) => { e.stopPropagation(); openDetail(id); });
      card.querySelector('[data-act="edit"]').addEventListener("click", (e) => { e.stopPropagation(); openEditor(id); });
      card.querySelector('[data-act="del"]').addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(_mcT("Delete this list?"))) return;
        await mcApi(`/api/media-calendar/lists/${id}`, { method: "DELETE" });
        if (detailId === id) { document.getElementById("mcListDetail").style.display = "none"; detailId = null; }
        load();
      });
      card.addEventListener("click", () => openDetail(id));
    });
  }

  async function openDetail(id) {
    detailId = id;
    if (window.toggleMcListList) {
      window.toggleMcListList(true);
    }
    const data = await mcApi(`/api/media-calendar/lists/${id}`);
    const list = data.list;
    const detailEl = document.getElementById("mcListDetail");
    detailEl.style.display = "block";
    detailEl.innerHTML = `
      <div class="mc-detail-head"><h2>${mcEsc(list.name)}</h2></div>
      <div class="mc-release-grid">
        ${list.items.map((it) => `
          <div class="mc-release-card">
            <img class="mc-release-poster" src="${mcPoster(it.poster_path)}" loading="lazy" alt="">
            <div class="mc-release-body">
              <div class="mc-release-title">${mcEsc(it.title)}</div>
              <div class="mc-release-date">${mcEsc(it.release_date || "")}</div>
              <div class="mc-release-actions"><button data-t="${it.tmdb_id}" data-mt="${it.media_type}">${mcEsc(_mcT("Remove"))}</button></div>
            </div>
          </div>`).join("") || `<div class="mc-empty">${mcEsc(_mcT("No titles added yet."))}</div>`}
      </div>`;
    detailEl.querySelectorAll("[data-t]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await mcApi(`/api/media-calendar/lists/${id}/items/${btn.dataset.mt}/${btn.dataset.t}`, { method: "DELETE" });
        openDetail(id);
      });
    });
  }

  function openEditor(id) {
    editingId = id;
    draft = id ? JSON.parse(JSON.stringify(lists.find((l) => l.id === id))) : emptyDraft();
    document.getElementById("mcListModalTitle").textContent = id ? _mcT("Edit list") : _mcT("New list");
    renderEditorBody(id);
    document.getElementById("mcListModal").style.display = "flex";
  }

  function closeEditor() {
    document.getElementById("mcListModal").style.display = "none";
    draft = null; editingId = null;
  }

  async function renderEditorBody(id) {
    const body = document.getElementById("mcListModalBody");
    const items = [];
    items.push({
      id: "basics", title: _mcT("Name & items"),
      bodyHtml: `
        <div class="mc-field">
          <label>${mcEsc(_mcT("Name"))}</label>
          <input type="text" id="mc_list_name" value="${mcEsc(draft.name)}">
        </div>
        ${id ? `
        <div class="mc-field">
          <label>${mcEsc(_mcT("Titles in this list"))}</label>
          <div class="mc-chip-list" id="mc_list_items_chips"></div>
        </div>
        <div class="mc-field">
          <label>${mcEsc(_mcT("Add title"))}</label>
          <div id="mc_list_item_search"></div>
        </div>` : `<div class="mc-hint">${mcEsc(_mcT("Save first, then add titles."))}</div>`}`,
    });
    items.push({
      id: "dynamic", title: _mcT("Dynamic filter"),
      bodyHtml: `
        <div class="mc-toggle-row">
          <div>
            <div class="mc-toggle-row-label">${mcEsc(_mcT("Enable dynamic matching"))}</div>
            <div class="mc-toggle-row-desc">${mcEsc(_mcT("Also auto-match new TMDB releases against genres/keywords/providers below, in addition to manually added titles."))}</div>
          </div>
          <label class="mc-switch"><input type="checkbox" id="mc_list_dynamic" ${draft.dynamic_enabled ? "checked" : ""}><span class="mc-switch-track"></span></label>
        </div>
        <div id="mc_list_dynamic_fields" style="display:${draft.dynamic_enabled ? "block" : "none"};margin-top:10px;">
          <div class="mc-field">
            <label>${mcEsc(_mcT("Media types"))}</label>
            <label><input type="checkbox" class="chb-main" id="mc_list_mt_movie" ${draft.media_types.includes("movie") ? "checked" : ""}> ${mcEsc(_mcT("Movies"))}</label>
            &nbsp;&nbsp;
            <label><input type="checkbox" class="chb-main" id="mc_list_mt_tv" ${draft.media_types.includes("tv") ? "checked" : ""}> ${mcEsc(_mcT("TV Shows"))}</label>
          </div>
          <div class="mc-field"><label>${mcEsc(_mcT("Genres"))}</label><div id="mc_list_genres"></div></div>
        </div>`,
    });
    items.push({
      id: "anilist", title: _mcT("Import from AniList"),
      bodyHtml: id ? `
        <div class="mc-field">
          <label>${mcEsc(_mcT("AniList username"))}</label>
          <input type="text" id="mc_list_anilist_user" placeholder="e.g. myusername">
        </div>
        <button class="mc-btn mc-btn-sm" id="mc_list_anilist_go">${mcEsc(_mcT("Import watching + planning"))}</button>
        <div id="mc_list_anilist_result" class="mc-hint"></div>` : `<div class="mc-hint">${mcEsc(_mcT("Save the list first, then import from AniList."))}</div>`,
    });
    body.innerHTML = mcAccordion(items);

    if (id) {
      renderListItemsChips();
      mcBuildSearchPicker("mc_list_item_search", async (result) => {
        const data = await mcApi(`/api/media-calendar/lists/${id}/items`, {
          method: "POST",
          body: JSON.stringify({
            tmdb_id: result.id, media_type: result.media_type,
            title: result.title || result.name, poster_path: result.poster_path,
            release_date: result.release_date || result.first_air_date,
          }),
        });
        // The add-item route returns the full, freshly-saved list (see
        // routes.py's api_list_add_item) -- use it directly instead of a
        // second round-trip, and keep draft.items in sync so re-rendering
        // the chips (e.g. after a subsequent add/remove) doesn't need
        // another fetch either.
        if (data.list) {
          draft.items = data.list.items;
          renderListItemsChips();
        }
      });
      const goBtn = document.getElementById("mc_list_anilist_go");
      if (goBtn) goBtn.addEventListener("click", async () => {
        const username = document.getElementById("mc_list_anilist_user").value.trim();
        if (!username) return;
        const resultEl = document.getElementById("mc_list_anilist_result");
        resultEl.textContent = _mcT("Importing...");
        const result = await mcApi(`/api/media-calendar/lists/${id}/anilist-import`, {
          method: "POST", body: JSON.stringify({ username }),
        });
        if (result.error) { resultEl.textContent = result.error; return; }
        resultEl.textContent = `${_mcT("Added")}: ${result.added.length} · ${_mcT("Unmatched")}: ${result.unmatched.length}`;
      });
    }

    document.getElementById("mc_list_dynamic").addEventListener("change", (e) => {
      draft.dynamic_enabled = e.target.checked;
      document.getElementById("mc_list_dynamic_fields").style.display = e.target.checked ? "block" : "none";
      if (e.target.checked) loadListGenres();
    });
    if (draft.dynamic_enabled) loadListGenres();

    function renderListItemsChips() {
      const el = document.getElementById("mc_list_items_chips");
      if (!el) return;
      if (!draft.items.length) {
        el.innerHTML = `<span class="mc-hint">${mcEsc(_mcT("No titles added yet."))}</span>`;
        return;
      }
      el.innerHTML = draft.items.map((it) => `
        <span class="mc-chip" data-id="${it.tmdb_id}" data-type="${it.media_type}">
          ${it.poster_path ? `<img class="mc-chip-poster" src="${mcPoster(it.poster_path)}" loading="lazy">` : ""}
          ${mcEsc(it.title)} <button>&times;</button>
        </span>`).join("");
      el.querySelectorAll(".mc-chip").forEach((chip) => {
        chip.querySelector("button").addEventListener("click", async () => {
          const data = await mcApi(`/api/media-calendar/lists/${id}/items/${chip.dataset.type}/${chip.dataset.id}`, { method: "DELETE" });
          if (data.list) draft.items = data.list.items;
          renderListItemsChips();
        });
      });
    }

    async function loadListGenres() {
      const mt = draft.media_types[0] || "movie";
      const genres = await mcLoadGenres(mt);
      const el = document.getElementById("mc_list_genres");
      if (!el) return;
      el.innerHTML = genres.map((g) => `
        <label style="display:inline-block;width:48%;font-size:0.85em;margin-bottom:4px;">
          <input type="checkbox" class="chb-main mc_list_genre_cb" value="${g.id}" ${draft.genres.includes(g.id) ? "checked" : ""}> ${mcEsc(g.name)}
        </label>`).join("");
      el.querySelectorAll(".mc_list_genre_cb").forEach((cb) => {
        cb.addEventListener("change", () => {
          const gid = parseInt(cb.value, 10);
          draft.genres = cb.checked ? [...draft.genres, gid] : draft.genres.filter((x) => x !== gid);
        });
      });
    }
  }

  async function save() {
    draft.name = document.getElementById("mc_list_name").value.trim();
    if (!draft.name) { alert(_mcT("Please enter a name.")); return; }
    const mtMovie = document.getElementById("mc_list_mt_movie");
    if (mtMovie) {
      draft.media_types = [
        mtMovie.checked ? "movie" : null,
        document.getElementById("mc_list_mt_tv").checked ? "tv" : null,
      ].filter(Boolean);
      if (!draft.media_types.length) draft.media_types = ["movie", "tv"];
    }
    if (!editingId) {
      const created = await mcApi("/api/media-calendar/lists", { method: "POST", body: JSON.stringify({ name: draft.name }) });
      editingId = created.id;
    }
    await mcApi(`/api/media-calendar/lists/${editingId}`, { method: "PUT", body: JSON.stringify(draft) });
    closeEditor();
    load();
  }

  return { load, ensureLoaded, openDetail, openEditor, closeEditor, save, getCached: () => lists };
})();

// =========================================================================
// Planned Downloads (management list + language/path config modal)
// =========================================================================
// The "Planned Downloads" tab lists every release the user flagged via a
// calendar's "Plan auto-download" action (see McCalendars.mcCalActionsHtml),
// regardless of status (pending/queued/failed), and lets them edit the
// language/download-path config or remove the flag entirely. The same
// config modal (mcPlannedModal in mediacalendar.html) is opened from both
// places -- see openConfig()'s opts parameter, which accepts either a full
// planned_downloads row (from this tab's own list) or a release dict from
// McCalendars (which carries planned_language/planned_custom_path_id
// instead, see service.py's _postprocess()).

const McPlanned = (() => {
  let items = [];
  let customPaths = [];
  let pathsLoaded = false;
  let target = null; // release identity currently being configured

  // Same source-string list AutoSync's own job-creation modal uses (see
  // static/autosync_filter.js's DEFAULT_LANGS) -- these are the literal
  // `language` values web/db.py's add_autosync_job() stores, not translated
  // labels, so they must match exactly.
  const DEFAULT_LANGS = [
    "German Dub", "English Sub", "German Sub", "English Dub", "English Dub (German Sub)",
  ];

  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function epLabel(row) {
    if (row.media_type === "movie") return _mcT("Movie");
    if ((row.season_number ?? -1) < 0) return "";
    return "S" + pad(row.season_number) + "E" + pad(row.episode_number < 0 ? 0 : row.episode_number);
  }

  async function ensurePaths() {
    if (pathsLoaded) return customPaths;
    // /api/custom-paths is a core MediaForge route (not thirdparty-scoped,
    // see web/routes/settings.py's api_custom_paths) -- the same one
    // static/autosync.js, static/library.js, static/queue.js etc. already
    // read from, reused here as-is rather than duplicating a mediacalendar-
    // local copy of the same list.
    const data = await mcApi("/api/custom-paths");
    customPaths = data.paths || [];
    pathsLoaded = true;
    return customPaths;
  }

  function pathName(id) {
    if (id == null || id === "") return _mcT("Default path");
    const p = customPaths.find((x) => x.id === id || String(x.id) === String(id));
    return p ? p.name : _mcT("Default path");
  }

  function statusBadge(row) {
    if (row.status === "queued") return `<span class="mc-cal-badge mc-cal-badge-sync">${mcEsc(_mcT("In-Sync"))}</span>`;
    if (row.status === "failed") return `<span class="mc-cal-badge mc-cal-badge-failed">${mcEsc(_mcT("Failed"))}</span>`;
    return `<span class="mc-cal-badge mc-cal-badge-planned">${mcEsc(_mcT("Pending"))}</span>`;
  }

  async function load() {
    await ensurePaths();
    const data = await mcApi("/api/media-calendar/planned");
    items = data.planned || [];
    render();
  }

  function render() {
    const el = document.getElementById("mcPlannedList");
    if (!el) return;
    if (!items.length) {
      el.innerHTML = `<div class="mc-empty">${mcEsc(_mcT("No planned downloads yet."))}</div>`;
      return;
    }
    el.innerHTML = `<div class="mc-planned-list">` + items.map((row, i) => `
      <div class="mc-planned-row" data-i="${i}">
        ${row.poster_path ? `<img class="mc-planned-poster" src="${mcPoster(row.poster_path)}" loading="lazy" alt="">` : `<div class="mc-planned-poster"></div>`}
        <div class="mc-planned-info">
          <div class="mc-planned-title">${mcEsc(row.title || "")} <span class="mc-planned-ep">${mcEsc(epLabel(row))}</span></div>
          <div class="mc-planned-meta">
            ${statusBadge(row)}
            <span class="mc-planned-lang">${mcEsc(row.language || "German Dub")}</span>
            <span class="mc-planned-path">${mcEsc(pathName(row.custom_path_id))}</span>
          </div>
        </div>
        <div class="mc-planned-actions">
          <button class="mc-btn mc-btn-sm" data-act="edit">${mcEsc(_mcT("Edit"))}</button>
          <button class="mc-btn mc-btn-sm mc-btn-danger" data-act="del">${mcEsc(_mcT("Remove"))}</button>
        </div>
      </div>`).join("") + `</div>`;
    el.querySelectorAll(".mc-planned-row").forEach((rowEl) => {
      const row = items[parseInt(rowEl.dataset.i, 10)];
      rowEl.querySelector('[data-act="edit"]').addEventListener("click", () => openConfig(row));
      rowEl.querySelector('[data-act="del"]').addEventListener("click", async () => {
        if (!confirm(_mcT("Remove this planned download?"))) return;
        await mcApi(`/api/media-calendar/planned/${row.media_type}/${row.tmdb_id}?season_number=${row.season_number ?? -1}&episode_number=${row.episode_number ?? -1}`, { method: "DELETE" });
        McCalendars.applyPlannedConfig(row.tmdb_id, row.media_type, row.season_number ?? -1, row.episode_number ?? -1,
          { planned_download: null, planned_language: null, planned_custom_path_id: null });
        load();
      });
    });
  }

  // `mode` distinguishes the two things this same language/path modal is
  // used for: "plan" (default -- flag the release and wait, checked
  // hourly, until it actually shows up on a site) and "autosync" (search
  // right now and set up regular, ongoing Auto-Sync for the whole series
  // immediately -- see mcCalActionsHtml's "Add to Auto Sync" button in
  // McCalendars, an alternative to "Plan auto-download" for someone who
  // doesn't want to wait for this one specific release). Stored on
  // `target` so saveConfig() below knows which API call to make.
  async function openConfig(opts, mode) {
    mode = mode === "autosync" ? "autosync" : "plan";
    await ensurePaths();
    target = {
      tmdb_id: opts.tmdb_id, media_type: opts.media_type,
      season_number: opts.season_number ?? -1, episode_number: opts.episode_number ?? -1,
      title: opts.title, release_date: opts.release_date, poster_path: opts.poster_path,
      mode,
    };
    const language = opts.language || opts.planned_language || "German Dub";
    const pathId = opts.custom_path_id != null ? opts.custom_path_id
      : (opts.planned_custom_path_id != null ? opts.planned_custom_path_id : null);

    // The modal only ever asked for language/path -- with several
    // planned rows for the same show (different seasons/episodes) open
    // from the management list, there was nothing telling you *which*
    // one you were about to edit. Title + season/episode shown here too
    // now, not just in the list row (see McPlanned.render()'s epLabel()).
    const modalLabel = mode === "autosync" ? _mcT("Add to Auto Sync") : _mcT("Plan auto-download");
    document.getElementById("mcPlannedModalTitle").textContent = opts.title
      ? `${modalLabel} — ${opts.title}`
      : modalLabel;
    const epInfo = epLabel(target);
    document.getElementById("mcPlannedModalBody").innerHTML = `
      ${epInfo ? `<div class="mc-hint" style="margin-bottom:10px;">${mcEsc(epInfo)}</div>` : ""}
      <div class="mc-field">
        <label>${mcEsc(_mcT("Language"))}</label>
        <select id="mc_planned_language">
          ${DEFAULT_LANGS.map((l) => `<option value="${mcEsc(l)}" ${l === language ? "selected" : ""}>${mcEsc(l)}</option>`).join("")}
        </select>
      </div>
      <div class="mc-field">
        <label>${mcEsc(_mcT("Download path"))}</label>
        <select id="mc_planned_path">
          <option value="">${mcEsc(_mcT("Default path"))}</option>
          ${customPaths.map((p) => `<option value="${p.id}" ${String(pathId) === String(p.id) ? "selected" : ""}>${mcEsc(p.name)} (${mcEsc(p.path)})</option>`).join("")}
        </select>
      </div>
      <div class="mc-hint">${mcEsc(mode === "autosync"
        ? _mcT("Searches AniWorld/S.TO/MegaKino right now and sets up regular Auto-Sync for the whole series immediately if a match is found -- does not wait for this specific release.")
        : _mcT("Used once this release is found and turned into an Auto-Sync job -- checked hourly from the release date on."))}</div>`;
    document.getElementById("mcPlannedModal").style.display = "flex";
  }

  function closeConfig() {
    document.getElementById("mcPlannedModal").style.display = "none";
    target = null;
  }

  async function saveConfig() {
    if (!target) return;
    const language = document.getElementById("mc_planned_language").value;
    const pathVal = document.getElementById("mc_planned_path").value;
    const customPathId = pathVal ? parseInt(pathVal, 10) : null;

    if (target.mode === "autosync") {
      // Straight to regular Auto-Sync, no planned_downloads row at all --
      // see service.py's add_title_to_autosync()/_find_and_activate_autosync().
      // Capture the title before closeConfig() nulls out `target`.
      const title = target.title;
      closeConfig();
      const result = await mcApi("/api/media-calendar/autosync", {
        method: "POST",
        body: JSON.stringify({ title, language, custom_path_id: customPathId }),
      });
      if (result.ok) {
        if (window.showToast) window.showToast(_mcT("Added to Auto Sync."));
      } else {
        const msg = result.error === "no_match"
          ? _mcT("No matching site found for this title.")
          : _mcT("Could not add to Auto Sync.");
        if (window.showToast) window.showToast(msg);
        else alert(msg);
      }
      return;
    }

    await mcApi(`/api/media-calendar/planned/${target.media_type}/${target.tmdb_id}`, {
      method: "POST",
      body: JSON.stringify({
        season_number: target.season_number, episode_number: target.episode_number,
        title: target.title, release_date: target.release_date, poster_path: target.poster_path,
        language, custom_path_id: customPathId,
      }),
    });
    McCalendars.applyPlannedConfig(target.tmdb_id, target.media_type, target.season_number, target.episode_number, {
      planned_download: "pending", planned_language: language, planned_custom_path_id: customPathId,
    });
    closeConfig();
    if (document.getElementById("mcPanelPlanned").classList.contains("mc-panel-active")) load();
  }

  return { load, openConfig, closeConfig, saveConfig };
})();

// =========================================================================
// Settings
// =========================================================================

const McSettings = (() => {
  const FIELDS = [
    { key: "mediacalendar_lookahead_weeks", type: "number", label: _mcTStatic("Lookahead (weeks)"), desc: _mcTStatic("How many weeks ahead calendars resolve releases for.") },
    { key: "mediacalendar_lookback_weeks", type: "number", label: _mcTStatic("Lookback (weeks)"), desc: _mcTStatic("How many weeks in the past calendars also resolve releases for, in addition to the forward-looking lookahead window. 0 keeps calendars showing today onward only.") },
    { key: "mediacalendar_cache_hours", type: "select", label: _mcTStatic("Cache duration"), desc: _mcTStatic("How long resolved calendar results are cached before being refreshed automatically."),
      options: [["6", "6h"], ["12", "12h"], ["24", "24h"], ["48", "48h"]] },
    { key: "mediacalendar_use_library", type: "toggle", label: _mcTStatic("Use my media library"), desc: _mcTStatic("Lets the \"My media library\" calendar source and library-status badges read your existing library data (a connected Jellyfin/Plex server via MediaScan, and/or MediaForge's own native library scan).") },
    { key: "mediacalendar_notify_enabled", type: "toggle", label: _mcTStatic("Reminder notifications"), desc: _mcTStatic("Sends a notification (via MediaForge's configured notification channels) shortly before a release date.") },
    { key: "mediacalendar_notify_lead_days", type: "number", label: _mcTStatic("Reminder lead time (days)"), desc: _mcTStatic("How many days in advance to send the reminder.") },
  ];

  function _mcTStatic(s) { return s; } // resolved at render time via _mcT()

  async function load() {
    const data = await mcApi("/api/settings/thirdparty/mediacalendar");
    const el = document.getElementById("mcSettingsForm");
    el.innerHTML = FIELDS.map((f) => {
      const value = (data.extra || {})[f.key];
      if (f.type === "toggle") {
        return `
          <div class="mc-toggle-row">
            <div>
              <div class="mc-toggle-row-label">${mcEsc(_mcT(f.label))}</div>
              <div class="mc-toggle-row-desc">${mcEsc(_mcT(f.desc))}</div>
            </div>
            <label class="mc-switch"><input type="checkbox" data-key="${f.key}" ${value === "1" ? "checked" : ""}><span class="mc-switch-track"></span></label>
          </div>`;
      }
      if (f.type === "select") {
        return `
          <div class="mc-field">
            <label>${mcEsc(_mcT(f.label))}</label>
            <select data-key="${f.key}">
              ${f.options.map(([v, l]) => `<option value="${v}" ${value === v ? "selected" : ""}>${mcEsc(l)}</option>`).join("")}
            </select>
            <div class="mc-hint">${mcEsc(_mcT(f.desc))}</div>
          </div>`;
      }
      return `
        <div class="mc-field">
          <label>${mcEsc(_mcT(f.label))}</label>
          <input type="number" data-key="${f.key}" value="${mcEsc(value)}">
          <div class="mc-hint">${mcEsc(_mcT(f.desc))}</div>
        </div>`;
    }).join("") + `<button class="mc-btn mc-btn-primary" id="mcSettingsSaveBtn" style="margin-top:10px;">${mcEsc(_mcT("Save"))}</button>`;

    document.getElementById("mcSettingsSaveBtn").addEventListener("click", save);
  }

  async function save() {
    const extra = {};
    document.querySelectorAll("#mcSettingsForm [data-key]").forEach((input) => {
      extra[input.dataset.key] = input.type === "checkbox" ? (input.checked ? "1" : "0") : input.value;
    });
    await mcApi("/api/settings/thirdparty/mediacalendar", { method: "PUT", body: JSON.stringify({ extra }) });
    load();
  }

  return { load };
})();
