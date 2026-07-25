/* ===================================================================
   MediaForge - Statistics page
   Renders the whole page from /api/stats plus a selectable trend
   window from /api/stats/trends. Charts are drawn by MFCharts
   (static/mf-charts.js) as inline SVG - no external chart library.

   Rendering model: build one HTML string, assign it once, then call
   MFCharts.renderAll() so every chart placeholder mounts in a single
   pass. That keeps the page to one reflow per refresh.
   =================================================================== */

// ---------------------------------------------------------------
// State
// ---------------------------------------------------------------

var MFStats = {
  days: 30,          // selected trend window
  data: null,        // last full /api/stats payload
  trends: null,      // trend series for the selected window
  loading: false,
};

// Kept on window for the ignore/restore handlers and the modals.
window._mediaStats = null;

// ---------------------------------------------------------------
// Formatting helpers (locale aware: DE uses comma decimals)
// ---------------------------------------------------------------

function statsLocale() {
  return window.__LANG === "de" ? "de-DE" : "en-US";
}

function fmtInt(n) {
  if (n == null || isNaN(n)) return "—";
  return Number(n).toLocaleString(statsLocale());
}

function fmtFloat(n, digits) {
  if (n == null || isNaN(n)) return "—";
  var d = digits == null ? 2 : digits;
  return Number(n).toLocaleString(statsLocale(), {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

/** Format a size given in megabytes, stepping up to GB/TB. */
function fmtSize(mb) {
  if (mb == null || isNaN(mb)) return "—";
  var v = Number(mb);
  if (v >= 1024 * 1024) return fmtFloat(v / (1024 * 1024), 2) + " TB";
  if (v >= 1024) return fmtFloat(v / 1024, 2) + " GB";
  return fmtFloat(v, 1) + " MB";
}

/**
 * Compact size for chart axis labels.
 *
 * The full fmtSize() renders "1.000,0 MB", which is wider than any sensible
 * left gutter and got clipped off the chart. Axis ticks do not need the
 * decimal place, and switching to GB at 1000 (not 1024) keeps the tick text
 * short at exactly the values a "nice" axis maximum produces.
 */
function fmtSizeAxis(mb) {
  if (mb == null || isNaN(mb)) return "—";
  var v = Number(mb);
  if (v >= 1000000) return fmtFloat(v / 1048576, 1) + " TB";
  if (v >= 1000) return fmtFloat(v / 1024, 1) + " GB";
  return fmtFloat(v, 0) + " MB";
}

function fmtSpeed(mbps) {
  if (mbps == null || isNaN(mbps)) return "—";
  return fmtFloat(mbps, 2) + " MB/s";
}

function fmtDuration(seconds) {
  if (!seconds) return "—";
  var h = Math.floor(seconds / 3600);
  var m = Math.floor((seconds % 3600) / 60);
  var s = Math.floor(seconds % 60);
  if (h > 0) return h + "h " + m + "m";
  if (m > 0) return m + "m " + s + "s";
  return s + "s";
}

/** "2026-07-25" -> short axis label, e.g. "25.07." (de) / "Jul 25" (en). */
function fmtDayLabel(iso) {
  var p = String(iso || "").split("-");
  if (p.length !== 3) return iso || "";
  if (window.__LANG === "de") return p[2] + "." + p[1] + ".";
  var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return (months[parseInt(p[1], 10) - 1] || p[1]) + " " + p[2];
}

function fmtDateTime(v) {
  if (!v) return "—";
  // SQLite gives "YYYY-MM-DD HH:MM:SS"; make it a valid ISO-ish string first.
  var d = new Date(String(v).replace(" ", "T") + (String(v).endsWith("Z") ? "" : "Z"));
  if (isNaN(d.getTime())) return String(v);
  return d.toLocaleString(statsLocale(), {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

/**
 * Escape a value for interpolation into HTML.
 *
 * Note the quote handling: the textContent/innerHTML trick alone escapes
 * `&`, `<` and `>` but NOT quotes, so a title containing a double quote
 * used to break out of attributes like title="..." and could inject an
 * event handler. Everything here is interpolated into attributes somewhere,
 * so quotes are escaped explicitly.
 */
function escHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Percentage change between two window totals, or null when undefined. */
function deltaPct(current, previous) {
  if (!previous) return null;
  return ((current - previous) / previous) * 100;
}

function deltaHtml(pct, invert) {
  if (pct == null || !isFinite(pct)) return "";
  var up = pct >= 0;
  // `invert` flips the color meaning for metrics where "more" is bad
  // (failures), so red/green always means bad/good rather than down/up.
  var good = invert ? !up : up;
  var cls = Math.abs(pct) < 0.5 ? "flat" : good ? "up" : "down";
  var arrow = Math.abs(pct) < 0.5 ? "→" : up ? "↑" : "↓";
  return '<span class="stat-delta ' + cls + '">' + arrow + " " +
    fmtFloat(Math.abs(pct), 0) + "%</span>";
}

// ---------------------------------------------------------------
// Card / section builders
// ---------------------------------------------------------------

var ICONS = {
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
  disk: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>',
  bolt: '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
  check: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
  clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  film: '<rect x="2" y="2" width="20" height="20" rx="2.18"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/>',
  tv: '<rect x="2" y="7" width="20" height="15" rx="2"/><polyline points="17 2 12 7 7 2"/>',
  warn: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  sync: '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><polyline points="21 3 21 9 15 9"/>',
};

function icon(name) {
  if (!ICONS[name]) return "";
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICONS[name] + "</svg>";
}

/**
 * Hero KPI card: big number, optional sparkline, optional delta badge.
 * `onClick` must be a literal call expression (kept for parity with the
 * rest of the page, which builds markup as strings).
 */
function heroCard(o) {
  // Clickable cards are real <button>s so they are keyboard reachable and
  // announced as actionable; static cards stay plain <div>s (a disabled
  // button would be dimmed and skipped by screen readers).
  var tag = o.onClick ? "button" : "div";
  var attrs = o.onClick ? ' type="button" onclick="' + o.onClick + '"' : "";
  return "<" + tag + ' class="stat-card hero-card' + (o.onClick ? " is-clickable" : "") +
    '" style="--kpi-color:' + escHtml(o.color || "var(--accent)") + '"' + attrs + ">" +
    '<span class="hero-head">' +
    '<span class="hero-icon">' + icon(o.icon) + "</span>" +
    (o.spark || "") +
    "</span>" +
    '<span class="stat-value">' + o.value + "</span>" +
    '<span class="stat-label">' + escHtml(o.label) + (o.delta || "") + "</span>" +
    (o.sub ? '<span class="stat-sub">' + o.sub + "</span>" : "") +
    "</" + tag + ">";
}

/** Compact KPI tile used by the secondary rows. */
function statCard(label, value, sub, color, onClick) {
  return heroCard({
    label: label, value: value, sub: sub, color: color, onClick: onClick,
  });
}

/** True while the viewport is at the app-wide mobile breakpoint. */
function isMobileView() {
  return window.matchMedia("(max-width: 640px)").matches;
}

/**
 * Card frame around a chart. `body` is raw HTML (usually MFCharts.place()).
 *
 * Built as <details>/<summary> so charts start collapsed on phones — a stack
 * of full-height charts otherwise buries the numbers and the tables below
 * them. On desktop the card is forced open and the summary is inert (see
 * stats.css), so it behaves exactly like a plain card there.
 */
function chartCard(o) {
  var open = isMobileView() ? "" : " open";
  return '<details class="chart-card' + (o.wide ? " span-2" : "") +
    (o.className ? " " + o.className : "") + '"' + open + ">" +
    '<summary class="chart-card-head"><div><h3 class="chart-card-title">' + escHtml(o.title) + "</h3>" +
    (o.sub ? '<p class="chart-card-sub">' + escHtml(o.sub) + "</p>" : "") + "</div>" +
    (o.aside ? '<div class="chart-card-aside">' + o.aside + "</div>" : "") +
    '<span class="chart-card-chevron" aria-hidden="true">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" ' +
    'stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></span>' +
    "</summary>" +
    '<div class="chart-card-body">' + o.body + "</div></details>";
}

/**
 * Mount every chart under `root` and make collapsed cards render on open.
 *
 * A chart inside a closed <details> has no layout, so it would paint at the
 * minimum width. MFCharts' ResizeObserver already repaints when the card
 * expands; the explicit toggle handler is the belt-and-braces path for
 * browsers that do not fire RO on a display change.
 */
function mountCharts(root) {
  if (!root || typeof MFCharts === "undefined") return;
  MFCharts.renderAll(root);
  root.querySelectorAll("details.chart-card").forEach(function (d) {
    if (d._mfcBound) return;
    d._mfcBound = true;
    d.addEventListener("toggle", function () {
      if (d.open) MFCharts.renderAll(d);
    });
  });
}

/** Keep the desktop layout expanded when the viewport crosses the breakpoint. */
(function watchChartCollapse() {
  var mq = window.matchMedia("(max-width: 640px)");
  var apply = function () {
    document.querySelectorAll("details.chart-card").forEach(function (d) {
      if (!mq.matches && !d.open) d.open = true;
    });
  };
  if (mq.addEventListener) mq.addEventListener("change", apply);
  else if (mq.addListener) mq.addListener(apply);
})();

function sectionTitle(text, sub) {
  return '<div class="stats-section-head"><h2 class="stats-section-title">' + escHtml(text) + "</h2>" +
    (sub ? '<span class="stats-section-sub">' + escHtml(sub) + "</span>" : "") + "</div>";
}

// ---------------------------------------------------------------
// Loading / skeletons
// ---------------------------------------------------------------

function renderSkeletons(container) {
  var html = '<div class="stats-kpi-row stats-kpi-main">';
  for (var i = 0; i < 4; i++) html += '<div class="stat-card skeleton hero-skeleton"></div>';
  html += "</div>";
  html += '<div class="stats-charts-grid">';
  for (var j = 0; j < 4; j++) html += '<div class="chart-card skeleton chart-skeleton"></div>';
  html += "</div>";
  container.innerHTML = html;
}

async function loadStats(showSkeleton) {
  var container = document.getElementById("statsContent");
  if (!container) return;
  if (showSkeleton !== false) renderSkeletons(container);
  MFStats.loading = true;
  document.getElementById("statsRefreshBtn") &&
    document.getElementById("statsRefreshBtn").classList.add("spinning");
  try {
    var resp = await fetch("/api/stats?days=" + encodeURIComponent(MFStats.days));
    var data = await resp.json();
    MFStats.data = data;
    MFStats.trends = data.trends || null;
    renderStats(data, container);
  } catch (e) {
    container.innerHTML = '<div class="stats-loading">' +
      t("Fehler beim Laden der Statistiken.", "Error loading statistics.") + "</div>";
    console.log(e);
  } finally {
    MFStats.loading = false;
    document.getElementById("statsRefreshBtn") &&
      document.getElementById("statsRefreshBtn").classList.remove("spinning");
  }
}

function refreshStats() {
  if (!MFStats.loading) loadStats(true);
}

/**
 * Switch the trend window. Only /api/stats/trends is refetched - the full
 * /api/stats payload also triggers the library-cache scan, which is far
 * more expensive and does not depend on the selected range.
 */
async function setStatsRange(days) {
  if (MFStats.loading || days === MFStats.days) return;
  MFStats.days = days;
  document.querySelectorAll("#statsRange .stats-range-btn").forEach(function (b) {
    b.classList.toggle("active", Number(b.dataset.days) === days);
  });
  MFStats.loading = true;
  var container = document.getElementById("statsContent");
  container.classList.add("is-updating");
  try {
    var resp = await fetch("/api/stats/trends?days=" + encodeURIComponent(days));
    MFStats.trends = await resp.json();
    if (MFStats.data) MFStats.data.trends = MFStats.trends;
    renderStats(MFStats.data || {}, container);
  } catch (e) {
    console.log(e);
  } finally {
    MFStats.loading = false;
    container.classList.remove("is-updating");
  }
}

// ---------------------------------------------------------------
// Modals
// ---------------------------------------------------------------

function _openModal(id) {
  var m = document.getElementById(id);
  if (!m) return;
  m.style.display = "flex";
  // Lock background scrolling so a long modal table doesn't scroll the page.
  document.body.classList.add("modal-open");
}

function _closeModal(id) {
  var m = document.getElementById(id);
  if (m) m.style.display = "none";
  if (!anyStatsModalOpen()) document.body.classList.remove("modal-open");
  // A refresh that arrived while the modal was open only updated the data,
  // not the modal DOM (see _renderModals). Rebuild the now-closed modals so
  // they are current the next time they are opened.
  _renderModals();
  // The scan poll suspends itself while a modal is open and does not
  // reschedule, so restart it here — otherwise opening a modal once during a
  // library scan would silently stop the Media counts from ever filling in.
  if (!anyStatsModalOpen() && window._mediaStats && window._mediaStats.scanning &&
      (window._mediaRescanTries || 0) <= 15) {
    clearTimeout(window._mediaRescanTimer);
    window._mediaRescanTimer = setTimeout(function () { loadStats(false); }, 1500);
  }
}

/** True while any of the three stats modals is visible. */
function anyStatsModalOpen() {
  return ["speedModal", "incompleteModal", "duplicatesModal"].some(function (id) {
    var m = document.getElementById(id);
    return m && m.style.display === "flex";
  });
}

function _isModalOpen(id) {
  var m = document.getElementById(id);
  return !!m && m.style.display === "flex";
}

/**
 * Rebuild the modal bodies — but never one that is currently open.
 *
 * Background refreshes fire every few seconds while the media library is
 * still scanning. Re-rendering an open modal underneath the user wiped the
 * search box, the checkbox selection and the scroll position, which read as
 * "the dialog reloads itself every few seconds". Open modals are left alone
 * and refreshed on close instead.
 */
function _renderModals() {
  if (!_isModalOpen("speedModal")) _renderSpeedModal();
  if (!_isModalOpen("incompleteModal")) _renderIncompleteModal();
  if (!_isModalOpen("duplicatesModal")) _renderDuplicatesModal();
}

function openSpeedModal() { _openModal("speedModal"); mountCharts(document.getElementById("speedModalContent")); }
function closeSpeedModal() { _closeModal("speedModal"); }
function openIncompleteModal() {
  _openModal("incompleteModal");
  // Rows are fetched lazily — opening the modal is what triggers the request.
  if (!_incState.loaded && !_incState.loading) _loadIncompletePage();
}
function closeIncompleteModal() { _closeModal("incompleteModal"); }
function openDuplicatesModal() {
  _openModal("duplicatesModal");
  // The rows are fetched lazily, so opening the modal is what triggers the
  // first request — the stats page itself never carries them.
  if (!_dupState.loaded && !_dupState.loading) _loadDuplicatesPage();
  else mountCharts(document.getElementById("duplicatesModalContent"));
}
function closeDuplicatesModal() { _closeModal("duplicatesModal"); }

document.addEventListener("keydown", function (e) {
  if (e.key !== "Escape") return;
  ["speedModal", "incompleteModal", "duplicatesModal"].forEach(function (id) {
    var m = document.getElementById(id);
    if (m && m.style.display === "flex") _closeModal(id);
  });
});

/** Summary strip shown at the top of every stats modal. */
function modalSummary(items) {
  return '<div class="stats-modal-summary">' + items.map(function (it) {
    return '<div class="stats-modal-summary-item"><span class="sms-value" style="color:' +
      escHtml(it.color || "var(--text-primary)") + '">' + it.value + "</span>" +
      '<span class="sms-label">' + escHtml(it.label) + "</span></div>";
  }).join("") + "</div>";
}

/** Search box wired to a filter function by id. */
function modalSearch(id, placeholder, handler) {
  return '<div class="stats-modal-tools"><div class="stats-search">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>' +
    '<input type="search" id="' + escHtml(id) + '" class="stats-search-input" placeholder="' +
    escHtml(placeholder) + '" oninput="' + handler + '" autocomplete="off"></div></div>';
}

// ---------------------------------------------------------------
// Modal pagination
//
// All three modals can hold thousands of rows (a large library easily
// produces hundreds of duplicate groups), which made them slow to render and
// impossible to scan. Each one keeps its full data set in memory and renders
// one page at a time.
//
// Layout contract: the modal shell (summary, chart, search box) is rendered
// once; only the element with id `<key>TableHost` is re-rendered on a filter
// or page change. That is what keeps the focus and caret in the search box
// while typing.
// ---------------------------------------------------------------

var MODAL_PAGE_SIZE = 20;

// Per-modal view state. Reset to page 1 whenever the filter changes, and
// clamped in _pageSlice() so deleting rows can never strand the user on a
// page that no longer exists.
var _modalView = {
  speed: { page: 1, q: "" },
  incomplete: { page: 1, q: "" },
  ignored: { page: 1, q: "" },
  duplicates: { page: 1, q: "" },
};

/** Case-insensitive title match against the modal's search box. */
function _modalFilter(list, key, titleOf) {
  var needle = String(_modalView[key].q || "").toLowerCase().trim();
  if (!needle) return list;
  return list.filter(function (x) {
    return String(titleOf(x) || "").toLowerCase().indexOf(needle) !== -1;
  });
}

function _pageSlice(list, key) {
  var total = Math.max(1, Math.ceil(list.length / MODAL_PAGE_SIZE));
  var page = Math.min(Math.max(1, _modalView[key].page), total);
  _modalView[key].page = page;
  var start = (page - 1) * MODAL_PAGE_SIZE;
  return {
    items: list.slice(start, start + MODAL_PAGE_SIZE),
    page: page, total: total, start: start, count: list.length,
  };
}

/**
 * Page numbers with ellipses: 1 … 4 5 [6] 7 8 … 20.
 * Mirrors the Advanced Search pager so both read the same.
 */
function _pageNumbers(current, total) {
  if (total <= 7) {
    return Array.from({ length: total }, function (_, i) { return i + 1; });
  }
  var out = [1];
  var from = Math.max(2, current - 1);
  var to = Math.min(total - 1, current + 1);
  if (from > 2) out.push("…");
  for (var i = from; i <= to; i++) out.push(i);
  if (to < total - 1) out.push("…");
  out.push(total);
  return out;
}

/**
 * Pager markup using the app-wide .mf-pagination component (forms.css, which
 * base.html loads globally). Returns just the "x-y of z" line when everything
 * fits on one page -- a pager with a single disabled page is noise.
 */
function _paginationHtml(key, info) {
  var from = info.count ? info.start + 1 : 0;
  var to = info.start + info.items.length;
  var range = '<div class="stats-page-info">' +
    escHtml(t("Zeige " + fmtInt(from) + "–" + fmtInt(to) + " von " + fmtInt(info.count),
      "Showing " + fmtInt(from) + "–" + fmtInt(to) + " of " + fmtInt(info.count))) + "</div>";
  if (info.total <= 1) return range;

  var btn = function (page, label, disabled, title) {
    return '<button type="button" class="mf-pagination-btn" data-page="' + page + '"' +
      (disabled ? " disabled" : "") + ' title="' + escHtml(title) + '" onclick="statsGoToPage(\'' +
      key + '\',' + page + ')">' + label + "</button>";
  };
  var html = range + '<div class="mf-pagination">';
  html += btn(1, "&laquo;", info.page === 1, t("Erste Seite", "First page"));
  html += btn(info.page - 1, "&lsaquo;", info.page === 1, t("Zurück", "Back"));
  _pageNumbers(info.page, info.total).forEach(function (entry) {
    if (entry === "…") {
      html += '<span class="mf-pagination-ellipsis">…</span>';
      return;
    }
    html += '<button type="button" class="mf-pagination-page' + (entry === info.page ? " active" : "") +
      '" data-page="' + entry + '"' + (entry === info.page ? " disabled" : "") +
      ' onclick="statsGoToPage(\'' + key + '\',' + entry + ')">' + entry + "</button>";
  });
  html += btn(info.page + 1, "&rsaquo;", info.page === info.total, t("Weiter", "Next"));
  html += btn(info.total, "&raquo;", info.page === info.total, t("Letzte Seite", "Last page"));
  html += "</div>";
  return html;
}

/** Re-render just the table host of one modal. */
var _MODAL_RENDERERS = {
  speed: function () { _renderSpeedTable(); },
  incomplete: function () { _renderIncompleteTable(); },
  ignored: function () { _renderIncompleteTable(); },
  duplicates: function () { _loadDuplicatesPage(); },
};

function statsGoToPage(key, page) {
  if (!_modalView[key]) return;
  _modalView[key].page = Math.max(1, parseInt(page, 10) || 1);
  _MODAL_RENDERERS[key]();
  // Jump back to the top of the list, otherwise page 2 opens mid-table.
  // Measured via getBoundingClientRect rather than offsetTop, which is
  // relative to the offsetParent and does not have to be the scroller.
  var host = document.getElementById(key + "TableHost");
  var scroller = host && host.closest(".stats-modal-body");
  if (scroller && host) {
    scroller.scrollTop += host.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 8;
  }
}

var _filterTimers = {};

function statsFilter(key, value) {
  if (!_modalView[key]) return;
  _modalView[key].q = value;
  _modalView[key].page = 1;   // a filtered list has different pages
  // The duplicates modal filters on the server, so debounce it — otherwise
  // every keystroke fires a request. The other two filter in memory and can
  // update immediately.
  if (key === "speed") {
    _MODAL_RENDERERS[key]();
    return;
  }
  clearTimeout(_filterTimers[key]);
  _filterTimers[key] = setTimeout(function () { _MODAL_RENDERERS[key](); }, 250);
}

// ---------------------------------------------------------------
// Speed modal
// ---------------------------------------------------------------

/** Rows currently in the speed modal, newest first. Set by _renderSpeedModal. */
var _speedRows = [];

function _renderSpeedModal() {
  var el = document.getElementById("speedModalContent");
  if (!el) return;
  var trends = MFStats.trends || {};
  var series = trends.speed_series || [];
  var g = (MFStats.data && MFStats.data.general) || {};

  // Fall back to the general payload's last_speeds when the history table is
  // empty (older installs recorded speeds on the queue row only).
  if (!series.length && g.last_speeds && g.last_speeds.length) {
    series = g.last_speeds.slice().reverse().map(function (x) {
      return { title: x.title, speed: x.speed, size_mb: x.size, finished_at: x.date };
    });
  }

  if (!series.length) {
    _speedRows = [];
    el.innerHTML = '<p class="stat-sub">' +
      t("Noch keine Geschwindigkeitsdaten vorhanden.", "No speed data recorded yet.") + "</p>";
    return;
  }

  var speeds = series.map(function (x) { return Number(x.speed) || 0; });
  var avg = speeds.reduce(function (a, b) { return a + b; }, 0) / speeds.length;
  var max = Math.max.apply(null, speeds);
  var min = Math.min.apply(null, speeds);
  var totalSize = series.reduce(function (a, x) { return a + (Number(x.size_mb) || 0); }, 0);

  // The chart runs oldest -> newest; the table reads newest first.
  _speedRows = series.slice().reverse();
  window._speedMaxSpeed = max;

  var html = modalSummary([
    { label: t("Ø Geschwindigkeit", "Avg. speed"), value: fmtSpeed(avg), color: "#06b6d4" },
    { label: t("Schnellster", "Fastest"), value: fmtSpeed(max), color: "#22c55e" },
    { label: t("Langsamster", "Slowest"), value: fmtSpeed(min), color: "#f59e0b" },
    { label: t("Volumen", "Volume"), value: fmtSize(totalSize) },
  ]);

  html += chartCard({
    title: t("Geschwindigkeitsverlauf", "Speed over time"),
    sub: t("Die letzten " + series.length + " abgeschlossenen Downloads",
      "The last " + series.length + " finished downloads"),
    body: MFCharts.place("speedTrendChart", {
      type: "area",
      height: 210,
      color: "#06b6d4",
      labels: series.map(function (x) { return fmtDayLabel((x.finished_at || "").split(" ")[0]); }),
      series: [{ name: t("MB/s", "MB/s"), values: speeds, color: "#06b6d4" }],
      valueFmt: function (v) { return fmtFloat(v, 1); },
      empty: t("Keine Daten", "No data"),
    }),
  });

  html += modalSearch("speedSearch", t("Titel suchen…", "Search title…"), "statsFilter('speed', this.value)");
  html += '<div id="speedTableHost"></div>';
  el.innerHTML = html;
  _renderSpeedTable();
  mountCharts(el);
}

function _renderSpeedTable() {
  var host = document.getElementById("speedTableHost");
  if (!host) return;
  var filtered = _modalFilter(_speedRows, "speed", function (x) { return x.title; });
  var info = _pageSlice(filtered, "speed");
  var max = window._speedMaxSpeed || 0;

  if (!info.count) {
    host.innerHTML = '<div class="stats-empty-state"><p>' +
      t("Kein Treffer für diese Suche.", "No match for this search.") + "</p></div>";
    return;
  }

  var html = '<div class="user-table-wrapper stats-table-wrap"><table class="user-table speed-modal-table">' +
    "<thead><tr>" +
    "<th>" + t("Titel", "Title") + "</th>" +
    "<th>" + t("Abgeschlossen", "Finished") + "</th>" +
    "<th>" + t("Größe", "Size") + "</th>" +
    "<th>" + t("Geschwindigkeit", "Speed") + "</th>" +
    '</tr></thead><tbody id="speedTableBody">';
  info.items.forEach(function (x) {
    var rel = max ? (Number(x.speed) || 0) / max * 100 : 0;
    // data-label feeds the mobile stacked-card layout (stats.css) — below
    // 640px the table collapses to one card per row, labelled from these.
    html += "<tr>" +
      '<td class="speed-modal-title" data-label="' + escHtml(t("Titel", "Title")) + '" title="' +
      escHtml(x.title) + '">' + escHtml(x.title) + "</td>" +
      '<td data-label="' + escHtml(t("Abgeschlossen", "Finished")) + '">' + escHtml(fmtDateTime(x.finished_at)) + "</td>" +
      '<td data-label="' + escHtml(t("Größe", "Size")) + '">' + escHtml(fmtSize(x.size_mb)) + "</td>" +
      '<td data-label="' + escHtml(t("Geschwindigkeit", "Speed")) + '"><div class="speed-cell"><span class="speed-bar"><span style="width:' +
      rel.toFixed(1) + '%"></span></span><b>' + escHtml(fmtSpeed(x.speed)) + "</b></div></td></tr>";
  });
  html += "</tbody></table></div>";
  html += _paginationHtml("speed", info);
  host.innerHTML = html;
}

// ---------------------------------------------------------------
// Incomplete-series modal
// ---------------------------------------------------------------

/**
 * Ignore-selection state, kept outside the DOM.
 *
 * With pagination the checkboxes of other pages simply do not exist, so
 * reading the selection off the DOM at submit time would silently drop
 * everything the user ticked on a page they have since left. The state is
 * keyed by folder and survives paging, filtering and tab switches.
 */
var _ignSel = { series: {}, slots: {}, titles: {} };

function _ignHasSelection() {
  return Object.keys(_ignSel.series).length > 0 ||
    Object.keys(_ignSel.slots).some(function (f) { return (_ignSel.slots[f] || []).length; });
}

function _ignSelectionCount() {
  var n = Object.keys(_ignSel.series).length;
  Object.keys(_ignSel.slots).forEach(function (f) {
    if (_ignSel.series[f]) return;   // whole series already covers its slots
    n += (_ignSel.slots[f] || []).length;
  });
  return n;
}

function statsIgnToggleSeries(folderEnc, checked, titleEnc) {
  var folder = decodeURIComponent(folderEnc);
  if (checked) {
    _ignSel.series[folder] = true;
    // Remember the title with the selection: the full series list no longer
    // lives in the browser, so it cannot be looked up at submit time.
    if (titleEnc) _ignSel.titles[folder] = decodeURIComponent(titleEnc);
  } else {
    delete _ignSel.series[folder];
  }
  _updateIgnActions();
}

function statsIgnToggleSlot(folderEnc, slotEnc, checked, titleEnc) {
  var folder = decodeURIComponent(folderEnc);
  var slot = decodeURIComponent(slotEnc);
  if (titleEnc) _ignSel.titles[folder] = decodeURIComponent(titleEnc);
  var list = _ignSel.slots[folder] || (_ignSel.slots[folder] = []);
  var at = list.indexOf(slot);
  if (checked && at === -1) list.push(slot);
  if (!checked && at !== -1) list.splice(at, 1);
  _updateIgnActions();
}

function statsIgnClearSelection() {
  _ignSel = { series: {}, slots: {}, titles: {} };
  _renderIncompleteTable();
}

/** Keep the action bar's counter/disabled state in sync without a re-render. */
function _updateIgnActions() {
  var btn = document.getElementById("ignApplyBtn");
  var info = document.getElementById("ignSelInfo");
  var n = _ignSelectionCount();
  if (btn) btn.disabled = !n;
  if (info) {
    info.textContent = n
      ? t(n + " ausgewählt", n + " selected")
      : t("Nichts ausgewählt", "Nothing selected");
  }
}

function toggleAllIncomplete(cb) {
  // Applies to the current page only — that is what the user can see, and
  // silently selecting hundreds of off-screen series would be a trap.
  document.querySelectorAll("#incompleteBody tr[data-folder]").forEach(function (tr) {
    var folder = tr.dataset.folder;
    if (cb.checked) {
      _ignSel.series[folder] = true;
      _ignSel.titles[folder] = tr.dataset.title || folder;
    } else {
      delete _ignSel.series[folder];
    }
    var box = tr.querySelector(".ign-series");
    if (box) box.checked = cb.checked;
  });
  _updateIgnActions();
}

/**
 * Incomplete-series modal state. Server-paginated for the same reason as the
 * duplicates modal: the gappy-series list grows with the library (875 of them
 * on a large install is ~620 KB of JSON) and was carried on every stats load
 * whether or not the modal was ever opened.
 */
var _incState = { loaded: false, loading: false, summary: null, seq: 0 };

function _incompleteTableHtml(info) {
  if (!info.count) {
    var empty = _modalView.incomplete.q
      ? t("Kein Treffer für diese Suche.", "No match for this search.")
      : t("Alle Serien sind vollständig.", "All series are complete.");
    return '<div class="stats-empty-state">' +
      (_modalView.incomplete.q ? "" : '<span class="stats-empty-emoji">🎉</span>') +
      "<p>" + empty + "</p></div>";
  }

  var mh = '<div class="user-table-wrapper stats-table-wrap"><table class="user-table stats-modal-table"><thead><tr>' +
    '<th class="td-check" style="width:36px"><input type="checkbox" class="chb-main" onchange="toggleAllIncomplete(this)" title="' +
    escHtml(t("Alle auf dieser Seite auswählen", "Select all on this page")) + '"></th>' +
    '<th style="width:36%">' + t("Serie", "Series") + "</th>" +
    '<th style="width:16%">' + t("Speicherort", "Location") + "</th>" +
    '<th style="width:auto">' + t("Fehlende Episoden", "Missing episodes") + "</th>" +
    '</tr></thead><tbody id="incompleteBody">';
  info.items.forEach(function (item) {
    var miss = item.missing || [];
    var folderEnc = encodeURIComponent(item.folder);
    var titleEnc = encodeURIComponent(item.title || item.folder);
    var seriesChecked = !!_ignSel.series[item.folder];
    var picked = _ignSel.slots[item.folder] || [];
    var chips = miss.map(function (sl) {
      var on = picked.indexOf(sl) !== -1 ? " checked" : "";
      return '<label class="ignore-slot-chip is-selectable"><input type="checkbox" class="ign-slot chb-main"' +
        on + ' onchange="statsIgnToggleSlot(\'' + folderEnc + "','" + encodeURIComponent(sl) +
        '\', this.checked, \'' + titleEnc + '\')"> ' + escHtml(sl) + "</label>";
    }).join(" ");
    mh += '<tr data-folder="' + escHtml(item.folder) + '" data-title="' + escHtml(item.title || "") + '">' +
      '<td class="td-check" data-label=""><input type="checkbox" class="ign-series chb-main"' +
      (seriesChecked ? " checked" : "") + ' onchange="statsIgnToggleSeries(\'' + folderEnc +
      '\', this.checked, \'' + titleEnc + '\')" title="' +
      escHtml(t("Ganze Serie ignorieren", "Ignore whole series")) + '"><span class="td-check-label">' +
      escHtml(t("Ganze Serie ignorieren", "Ignore whole series")) + "</span></td>" +
      '<td class="dup-title-cell" data-label="' + escHtml(t("Serie", "Series")) + '" title="' +
      escHtml(item.title) + '"><span class="dup-title-text">' + escHtml(item.title) + "</span>" +
      '<span class="miss-count">' + miss.length + " " + escHtml(t("fehlend", "missing")) + "</span></td>" +
      '<td data-label="' + escHtml(t("Speicherort", "Location")) + '">' + escHtml(item.location) + "</td>" +
      '<td data-label="' + escHtml(t("Fehlende Episoden", "Missing episodes")) +
      '"><div class="ignore-slot-wrap">' + chips + "</div></td></tr>";
  });
  mh += "</tbody></table></div>";
  mh += _paginationHtml("incomplete", info);
  mh += '<div class="ignore-actions">' +
    '<span class="ign-sel-info" id="ignSelInfo"></span>' +
    '<button type="button" class="btn btn-ghost ignore-restore-btn" onclick="statsIgnClearSelection()">' +
    escHtml(t("Auswahl leeren", "Clear selection")) + "</button>" +
    '<button type="button" class="btn-download-selected" id="ignApplyBtn" onclick="mediaIgnoreSelected()">' +
    escHtml(t("Auswahl ignorieren", "Ignore selected")) + "</button></div>";
  return mh;
}

function _ignoredTableHtml(info) {
  if (!info.count) {
    return '<div class="stats-empty-state"><p>' +
      (_modalView.ignored.q
        ? t("Kein Treffer für diese Suche.", "No match for this search.")
        : t("Keine ignorierten Einträge.", "No ignored entries.")) + "</p></div>";
  }
  var mh = '<div class="user-table-wrapper stats-table-wrap"><table class="user-table stats-modal-table"><thead><tr>' +
    '<th style="width:42%">' + t("Serie", "Series") + "</th>" +
    '<th style="width:auto">' + t("Ignoriert", "Ignored") + "</th>" +
    '<th style="width:150px"></th></tr></thead><tbody>';
  info.items.forEach(function (item) {
    var slots = item.slots || [];
    var isAll = slots.indexOf("__all__") !== -1;
    var folderEnc = encodeURIComponent(item.folder);
    var slotHtml;
    if (isAll) {
      slotHtml = '<span class="ignore-slot-chip ignore-all-chip">' +
        escHtml(t("Ganze Serie", "Whole series")) + "</span>";
    } else {
      slotHtml = slots.map(function (sl) {
        return '<span class="ignore-slot-chip">' + escHtml(sl) +
          ' <a href="#" class="ignore-remove-x" onclick="mediaUnignore(\'' + folderEnc + "','" +
          encodeURIComponent(sl) + '\');return false;" title="' +
          escHtml(t("Wiederherstellen", "Restore")) + '">×</a></span>';
      }).join(" ");
    }
    mh += "<tr>" +
      '<td class="speed-modal-title" data-label="' + escHtml(t("Serie", "Series")) + '" title="' +
      escHtml(item.title) + '">' + escHtml(item.title) + "</td>" +
      '<td data-label="' + escHtml(t("Ignoriert", "Ignored")) + '"><div class="ignore-slot-wrap">' + slotHtml + "</div></td>" +
      '<td data-label=""><button class="btn btn-ghost ignore-restore-btn" onclick="mediaUnignore(\'' + folderEnc +
      '\',null)">' + escHtml(t("Alle wiederherstellen", "Restore all")) + "</button></td></tr>";
  });
  mh += "</tbody></table></div>";
  mh += _paginationHtml("ignored", info);
  return mh;
}

/** Fetch and render the active tab's current page. */
async function _loadIncompletePage() {
  var host = document.getElementById("incompleteTableHost");
  if (!host) return;
  var view = window._incompleteView || "incomplete";
  var key = view === "ignored" ? "ignored" : "incomplete";
  var st = _modalView[key];
  var seq = ++_incState.seq;
  _incState.loading = true;

  try {
    var url = "/api/media/incomplete?view=" + key + "&page=" + encodeURIComponent(st.page) +
      "&per_page=" + MODAL_PAGE_SIZE + (st.q ? "&q=" + encodeURIComponent(st.q) : "");
    var resp = await fetch(url);
    var data = await resp.json();
    if (seq !== _incState.seq) return;   // superseded by a newer request

    _incState.loaded = true;
    _incState.summary = data.summary || _incState.summary;
    st.page = data.page || 1;
    var info = {
      items: data.items || [],
      page: data.page || 1,
      total: data.total_pages || 1,
      start: ((data.page || 1) - 1) * (data.per_page || MODAL_PAGE_SIZE),
      count: data.total || 0,
    };
    host.innerHTML = key === "ignored" ? _ignoredTableHtml(info) : _incompleteTableHtml(info);
    if (key === "incomplete") _updateIgnActions();
    _renderIncompleteSummary();
  } catch (e) {
    if (seq !== _incState.seq) return;
    host.innerHTML = '<div class="stats-empty-state"><p>' +
      t("Serien konnten nicht geladen werden.", "Could not load series.") + "</p></div>";
    console.log(e);
  } finally {
    if (seq === _incState.seq) _incState.loading = false;
  }
}

function _renderIncompleteSummary() {
  var host = document.getElementById("incSummaryHost");
  var sum = _incState.summary;
  if (!host || !sum) return;
  host.innerHTML = modalSummary([
    { label: t("Unvollständige Serien", "Incomplete series"), value: fmtInt(sum.incomplete), color: "#f59e0b" },
    { label: t("Fehlende Slots", "Missing slots"), value: fmtInt(sum.missing_slots), color: "#f87171" },
    { label: t("Vollständig", "Complete"), value: fmtInt(sum.complete), color: "#22c55e" },
    { label: t("Ignoriert", "Ignored"), value: fmtInt(sum.ignored) },
  ]);
  // Tab labels carry the counts too — keep them in step with the summary.
  var tabs = document.querySelectorAll("#incompleteModalContent .ignore-tab");
  if (tabs.length === 2) {
    tabs[0].textContent = t("Unvollständig", "Incomplete") + " (" + sum.incomplete + ")";
    tabs[1].textContent = t("Ignoriert", "Ignored") + " (" + sum.ignored + ")";
  }
}

/** Kept for the shared pager/filter plumbing (_MODAL_RENDERERS). */
function _renderIncompleteTable() {
  _loadIncompletePage();
}

function _renderIncompleteModal() {
  var m = window._mediaStats || {};
  var el = document.getElementById("incompleteModalContent");
  if (!el) return;
  var view = window._incompleteView || "incomplete";
  // Data changed underneath us — the next open refetches.
  _incState.loaded = false;
  _incState.summary = null;

  var html = '<div id="incSummaryHost"></div>';
  html += '<div class="ignore-tabs">' +
    '<button class="ignore-tab' + (view === "incomplete" ? " active" : "") +
    '" onclick="switchIncompleteView(\'incomplete\')">' + escHtml(t("Unvollständig", "Incomplete")) +
    " (" + fmtInt(m.incomplete_count || 0) + ")</button>" +
    '<button class="ignore-tab' + (view === "ignored" ? " active" : "") +
    '" onclick="switchIncompleteView(\'ignored\')">' + escHtml(t("Ignoriert", "Ignored")) +
    " (" + fmtInt(m.ignored_count || 0) + ")</button></div>";
  // One search box per tab, so switching tabs does not carry a stale filter.
  html += modalSearch(view === "ignored" ? "ignoredSearch" : "incompleteSearch",
    t("Serie suchen…", "Search series…"),
    "statsFilter('" + (view === "ignored" ? "ignored" : "incomplete") + "', this.value)");
  html += '<div id="incompleteTableHost"><div class="stats-loading">' +
    t("Lade Serien…", "Loading series…") + "</div></div>";
  el.innerHTML = html;
}

function switchIncompleteView(view) {
  window._incompleteView = view;
  _modalView[view === "ignored" ? "ignored" : "incomplete"].page = 1;
  _renderIncompleteModal();
  _loadIncompletePage();
}

// ---------------------------------------------------------------
// Duplicates modal
// ---------------------------------------------------------------

function _dupSlotLabel(item) {
  // Series episodes carry an "SxEy" slot; movies use the sentinel "movie".
  if (item.kind === "movie" || item.slot === "movie") return t("Film", "Movie");
  return item.slot;
}

/**
 * Duplicates modal state.
 *
 * Unlike the other two modals this one is paginated *server-side*: a large
 * library produces tens of thousands of groups, and shipping them all with
 * the stats payload cost ~28 MB of JSON per page load. Only the count travels
 * with /api/stats; the rows come from /api/media/duplicates one page at a
 * time, and the summary figures are aggregated server-side because they need
 * the whole set.
 */
var _dupState = { loaded: false, loading: false, summary: null, seq: 0 };

function _renderDuplicatesModal() {
  var m = window._mediaStats || {};
  var el = document.getElementById("duplicatesModalContent");
  if (!el) return;
  // Data changed underneath us — the next open refetches.
  _dupState.loaded = false;
  _dupState.summary = null;
  _modalView.duplicates.page = 1;

  if (!(m.duplicates_count || 0)) {
    el.innerHTML = '<div class="stats-empty-state"><span class="stats-empty-emoji">🎉</span><p>' +
      t("Keine Duplikate gefunden.", "No duplicates found.") + "</p></div>";
    return;
  }

  el.innerHTML = '<div id="dupSummaryHost"></div>' +
    modalSearch("dupSearch", t("Titel suchen…", "Search title…"), "statsFilter('duplicates', this.value)") +
    '<div id="duplicatesTableHost"><div class="stats-loading">' +
    t("Lade Duplikate…", "Loading duplicates…") + "</div></div>";
}

function _renderDupSummary() {
  var host = document.getElementById("dupSummaryHost");
  var sum = _dupState.summary;
  if (!host || !sum) return;
  var html = modalSummary([
    { label: t("Gruppen", "Groups"), value: fmtInt(sum.groups), color: "#f472b6" },
    { label: t("Dateien", "Files"), value: fmtInt(sum.files) },
    { label: t("Filme / Serien", "Movies / Series"), value: fmtInt(sum.movies) + " / " + fmtInt(sum.series) },
    { label: t("Freigebbar", "Reclaimable"), value: fmtSize(sum.reclaimable_mb), color: "#f59e0b" },
  ]);
  var res = (sum.resolutions || []).filter(function (x) { return x.name && x.name !== "?"; });
  if (res.length > 1) {
    html += chartCard({
      title: t("Auflösungen der Duplikate", "Resolutions among duplicates"),
      body: MFCharts.place("dupResChart", {
        type: "donut", height: 190,
        data: res.map(function (x) { return { label: x.name, value: x.value }; }),
        valueFmt: function (v) { return fmtInt(v); },
      }),
    });
  }
  host.innerHTML = html;
  mountCharts(host);
}

async function _loadDuplicatesPage() {
  var host = document.getElementById("duplicatesTableHost");
  if (!host) return;
  var view = _modalView.duplicates;
  // Guard against out-of-order responses: fast typing can leave an older
  // request landing after a newer one.
  var seq = ++_dupState.seq;
  _dupState.loading = true;
  host.setAttribute("aria-busy", "true");

  try {
    var url = "/api/media/duplicates?page=" + encodeURIComponent(view.page) +
      "&per_page=" + MODAL_PAGE_SIZE +
      (view.q ? "&q=" + encodeURIComponent(view.q) : "");
    var resp = await fetch(url);
    var data = await resp.json();
    if (seq !== _dupState.seq) return;   // superseded

    _dupState.loaded = true;
    if (!_dupState.summary && data.summary) {
      _dupState.summary = data.summary;
      _renderDupSummary();
    }
    view.page = data.page || 1;
    _renderDuplicatesTable({
      items: data.items || [],
      page: data.page || 1,
      total: data.total_pages || 1,
      start: ((data.page || 1) - 1) * (data.per_page || MODAL_PAGE_SIZE),
      count: data.total || 0,
    });
  } catch (e) {
    if (seq !== _dupState.seq) return;
    host.innerHTML = '<div class="stats-empty-state"><p>' +
      t("Duplikate konnten nicht geladen werden.", "Could not load duplicates.") + "</p></div>";
    console.log(e);
  } finally {
    if (seq === _dupState.seq) {
      _dupState.loading = false;
      host.removeAttribute("aria-busy");
    }
  }
}

function _renderDuplicatesTable(info) {
  var host = document.getElementById("duplicatesTableHost");
  if (!host) return;

  if (!info.count) {
    host.innerHTML = '<div class="stats-empty-state"><p>' +
      t("Kein Treffer für diese Suche.", "No match for this search.") + "</p></div>";
    return;
  }

  var html = '<div class="user-table-wrapper stats-table-wrap"><table class="user-table dup-table"><thead><tr>' +
    '<th style="width:24%">' + t("Serie / Film", "Series / Movie") + "</th>" +
    '<th style="width:9%">' + t("Episode", "Episode") + "</th>" +
    '<th style="width:13%">' + t("Speicherort", "Location") + "</th>" +
    '<th style="width:54%">' + t("Vorhandene Versionen", "Existing versions") + "</th>" +
    '</tr></thead><tbody id="dupBody">';
  info.items.forEach(function (item) {
    var files = item.files || [];
    // The badge goes on its own line below the title. Inline, it sat inside a
    // cell that truncates with text-overflow: ellipsis — and an inline-flex
    // chip ignores that, so it simply stuck out over the next column.
    var langBadge = item.language
      ? '<span class="dup-lang-badge ignore-slot-chip">' + escHtml(item.language) + "</span>" : "";
    // Only highlight a "keep this one" copy when the largest is unambiguous.
    // Two copies of the exact same size (a plain duplicate rather than a
    // quality difference) would otherwise both be marked green, which tells
    // the user nothing about which to delete.
    var sizes = files.map(function (f) { return Number(f.size) || 0; });
    var biggest = Math.max.apply(null, sizes);
    var biggestIsUnique = biggest > 0 &&
      sizes.filter(function (v) { return v === biggest; }).length === 1;
    // One block per copy: badge + size on the first line, path underneath.
    // These used to be two separate table columns kept in sync by a fixed
    // row height, which broke as soon as a badge or size was wider than its
    // column -- the size then bled into the path column.
    var fileRows = files.map(function (f) {
      var res = f.resolution || t("unbekannt", "unknown");
      var codec = f.video_codec ? " · " + escHtml(f.video_codec) : "";
      var ext = f.file && f.file.indexOf(".") !== -1 ? f.file.split(".").pop().toUpperCase() : "";
      var cont = ext ? " · " + escHtml(ext) : "";
      var best = biggestIsUnique && (Number(f.size) || 0) === biggest ? " is-best" : "";
      var sz = f.size ? '<span class="dup-size">' + escHtml(fmtSize(Number(f.size) / (1024 * 1024))) + "</span>" : "";
      var path = f.path || f.file || "";
      // Zero-width break opportunities after separators, so a long path wraps
      // at folder boundaries instead of one character per line.
      var pretty = escHtml(path).replace(/([\\/])/g, "$1<wbr>");
      return '<div class="dup-file">' +
        '<div class="dup-file-head"><span class="ignore-slot-chip' + best + '">' +
        escHtml(res) + codec + cont + "</span>" + sz + "</div>" +
        '<div class="dup-path-row" title="' + escHtml(path) + '">' + pretty + "</div></div>";
    }).join("");
    html += "<tr>" +
      '<td class="dup-title-cell" data-label="' + escHtml(t("Serie / Film", "Series / Movie")) +
      '" title="' + escHtml(item.title) + '"><span class="dup-title-text">' +
      escHtml(item.title) + "</span>" + langBadge + "</td>" +
      '<td data-label="' + escHtml(t("Episode", "Episode")) + '">' + escHtml(_dupSlotLabel(item)) + "</td>" +
      '<td data-label="' + escHtml(t("Speicherort", "Location")) + '">' + escHtml(item.location) + "</td>" +
      '<td class="dup-file-cell" data-label="' + escHtml(t("Vorhandene Versionen", "Existing versions")) +
      '">' + fileRows + "</td></tr>";
  });
  html += "</tbody></table></div>";
  html += _paginationHtml("duplicates", info);
  host.innerHTML = html;
}

// ---------------------------------------------------------------
// Ignore / restore actions
// ---------------------------------------------------------------

function mediaIgnoreSelected() {
  // Read from _ignSel, not the DOM: with pagination the checkboxes of every
  // other page do not exist, so a DOM scan would drop those selections.
  var items = {};
  Object.keys(_ignSel.series).forEach(function (folder) {
    items[folder] = { folder: folder, title: _ignSel.titles[folder] || folder, all: true };
  });
  Object.keys(_ignSel.slots).forEach(function (folder) {
    var slots = _ignSel.slots[folder] || [];
    if (!slots.length) return;
    if (items[folder] && items[folder].all) return;  // whole series covers it
    items[folder] = { folder: folder, title: _ignSel.titles[folder] || folder, slots: slots.slice() };
  });

  var payload = Object.values(items).filter(function (x) { return x.all || (x.slots && x.slots.length); });
  if (!payload.length) {
    if (typeof showToast === "function") showToast(t("Nichts ausgewählt.", "Nothing selected."));
    return;
  }
  fetch("/api/media/ignore", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: payload }),
  }).then(function (r) { return r.json(); }).then(function () {
    _ignSel = { series: {}, slots: {}, titles: {} };   // applied — start fresh
    loadStats(false);
  }).catch(function () { if (typeof showToast === "function") showToast(t("Fehler.", "Error.")); });
}

function mediaUnignore(folderEnc, slotEnc) {
  var body = { folder: decodeURIComponent(folderEnc) };
  if (slotEnc == null) body.all = true;
  else body.slot = decodeURIComponent(slotEnc);
  fetch("/api/media/unignore", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(function (r) { return r.json(); }).then(function () { loadStats(false); })
    .catch(function () { if (typeof showToast === "function") showToast(t("Fehler.", "Error.")); });
}

// ---------------------------------------------------------------
// Page sections
// ---------------------------------------------------------------

function renderHeroRow(g, trends) {
  var tot = (trends && trends.totals) || {};
  var daily = (trends && trends.daily) || [];
  var half = Math.floor(daily.length / 2);
  var sum = function (arr, key) {
    return arr.reduce(function (a, d) { return a + (Number(d[key]) || 0); }, 0);
  };
  // Compare the newer half of the window against the older half. Simple,
  // needs no extra query, and still answers "am I trending up or down".
  var prevDl = sum(daily.slice(0, half), "downloads");
  var currDl = sum(daily.slice(half), "downloads");
  var prevSz = sum(daily.slice(0, half), "size_mb");
  var currSz = sum(daily.slice(half), "size_mb");

  var dlSeries = daily.map(function (d) { return d.downloads; });
  var szSeries = daily.map(function (d) { return d.size_mb; });
  var spdSeries = (trends && trends.speed_series || []).map(function (x) { return x.speed; });

  var rangeLabel = t("letzte " + MFStats.days + " Tage", "last " + MFStats.days + " days");

  var html = '<div class="stats-kpi-row stats-kpi-main">';
  html += heroCard({
    label: t("Downloads", "Downloads"),
    value: fmtInt(tot.downloads != null ? tot.downloads : g.completed),
    sub: rangeLabel,
    color: "#7c3aed",
    icon: "download",
    spark: MFCharts.sparkline(dlSeries, { color: "#7c3aed" }),
    delta: deltaHtml(deltaPct(currDl, prevDl)),
  });
  html += heroCard({
    label: t("Datenvolumen", "Data volume"),
    value: fmtSize(tot.size_mb != null ? tot.size_mb : g.total_size_mb),
    sub: rangeLabel,
    color: "#e8914a",
    icon: "disk",
    spark: MFCharts.sparkline(szSeries, { color: "#e8914a" }),
    delta: deltaHtml(deltaPct(currSz, prevSz)),
  });
  html += heroCard({
    label: t("Ø Geschwindigkeit", "Avg. speed"),
    value: fmtSpeed(tot.avg_speed_mbps != null ? tot.avg_speed_mbps : g.average_speed_mbps),
    sub: (tot.max_speed_mbps ? t("Spitze", "Peak") + ": " + fmtSpeed(tot.max_speed_mbps) + " · " : "") +
      t("Klicken für Details", "Click for details"),
    color: "#06b6d4",
    icon: "bolt",
    spark: MFCharts.sparkline(spdSeries, { color: "#06b6d4" }),
    onClick: "openSpeedModal()",
  });
  html += heroCard({
    label: t("Erfolgsquote", "Success rate"),
    value: (tot.success_rate != null ? tot.success_rate : 0) + "%",
    sub: fmtInt(tot.failed || 0) + " " + t("fehlgeschlagen", "failed") + " · " +
      fmtInt(tot.completed || 0) + " " + t("erfolgreich", "successful"),
    color: (tot.success_rate || 0) >= 90 ? "#22c55e" : (tot.success_rate || 0) >= 70 ? "#f59e0b" : "#f87171",
    icon: "check",
    delta: deltaHtml(null),
  });
  html += "</div>";
  return html;
}

function renderTrendCharts(trends) {
  if (!trends || !(trends.daily || []).length) {
    return '<div class="stats-hint">' +
      t("Noch keine Verlaufsdaten im gewählten Zeitraum.",
        "No history data in the selected time range yet.") + "</div>";
  }
  var daily = trends.daily;
  var labels = daily.map(function (d) { return fmtDayLabel(d.date); });
  var html = sectionTitle(t("Verlauf", "Trends"),
    t(MFStats.days + " Tage", MFStats.days + " days"));
  html += '<div class="stats-charts-grid">';

  html += chartCard({
    title: t("Downloads pro Tag", "Downloads per day"),
    sub: t("Erfolgreich vs. fehlgeschlagen", "Successful vs. failed"),
    wide: true,
    body: MFCharts.place("dailyChart", {
      type: "area",
      height: 250,
      labels: labels,
      series: [
        { name: t("Erfolgreich", "Successful"), values: daily.map(function (d) { return d.completed; }), color: "#22c55e" },
        { name: t("Fehlgeschlagen", "Failed"), values: daily.map(function (d) { return d.failed; }), color: "#f87171" },
      ],
      valueFmt: function (v) { return fmtInt(Math.round(v)); },
      empty: t("Keine Daten", "No data"),
    }),
  });

  html += chartCard({
    title: t("Volumen pro Tag", "Volume per day"),
    sub: t("Heruntergeladene Datenmenge", "Downloaded data amount"),
    body: MFCharts.place("volumeChart", {
      type: "bars",
      height: 220,
      color: "#e8914a",
      data: daily.map(function (d) { return { label: fmtDayLabel(d.date), value: d.size_mb }; }),
      // Axis ticks use the compact size format and a roomier left gutter --
      // size labels are the widest in the app.
      valueFmt: function (v) { return fmtSizeAxis(v); },
      padL: 74,
      empty: t("Keine Daten", "No data"),
    }),
  });

  var spd = daily.filter(function (d) { return d.avg_speed_mbps != null; });
  html += chartCard({
    title: t("Geschwindigkeit im Zeitverlauf", "Speed over time"),
    sub: t("Tagesdurchschnitt in MB/s", "Daily average in MB/s"),
    body: spd.length
      ? MFCharts.place("speedDailyChart", {
        type: "line",
        height: 220,
        labels: spd.map(function (d) { return fmtDayLabel(d.date); }),
        series: [{ name: "MB/s", values: spd.map(function (d) { return d.avg_speed_mbps; }), color: "#06b6d4" }],
        valueFmt: function (v) { return fmtFloat(v, 1); },
        empty: t("Keine Daten", "No data"),
      })
      : '<div class="mfc-empty" style="height:200px">' + t("Keine Daten", "No data") + "</div>",
  });

  // Weekday x hour heatmap. SQLite's %w puts Sunday first; reorder to Mon-Sun.
  var wdNames = window.__LANG === "de"
    ? ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    : ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  var order = [1, 2, 3, 4, 5, 6, 0];
  var hm = trends.heatmap || [];
  var hours = [];
  for (var h = 0; h < 24; h++) hours.push(h < 10 ? "0" + h : String(h));
  html += chartCard({
    title: t("Aktivität nach Wochentag & Stunde", "Activity by weekday & hour"),
    sub: t("Wann laufen deine Downloads?", "When do your downloads run?"),
    wide: true,
    body: MFCharts.place("heatmapChart", {
      type: "heatmap",
      color: "#7c3aed",
      rows: order.map(function (d, i) {
        return { label: wdNames[i], values: hm[d] || [] };
      }),
      cols: hours,
      valueFmt: function (v) { return fmtInt(v) + " " + t("Downloads", "downloads"); },
      empty: t("Keine Daten", "No data"),
    }),
  });

  html += "</div>";
  return html;
}

function renderBreakdowns(trends, g) {
  var html = sectionTitle(t("Aufteilung", "Breakdown"));
  html += '<div class="stats-charts-grid">';

  var prov = (trends && trends.by_provider || []).filter(function (x) { return x.name; });
  html += chartCard({
    title: t("Nach Provider", "By provider"),
    sub: t("Downloads im Zeitraum", "Downloads in range"),
    body: prov.length
      ? MFCharts.place("providerChart", {
        type: "donut", height: 220,
        data: prov.map(function (x) { return { label: x.name, value: x.downloads }; }),
        centerValue: fmtInt(prov.reduce(function (a, x) { return a + x.downloads; }, 0)),
        centerLabel: t("gesamt", "total"),
        valueFmt: function (v) { return fmtInt(v); },
      })
      : '<div class="mfc-empty" style="height:200px">' + t("Keine Daten", "No data") + "</div>",
  });

  var src = (trends && trends.by_source || []).filter(function (x) { return x.name; });
  html += chartCard({
    title: t("Nach Auslöser", "By trigger"),
    sub: t("Manuell, Auto-Sync, …", "Manual, auto-sync, …"),
    body: src.length
      ? MFCharts.place("sourceChart", {
        type: "donut", height: 220,
        data: src.map(function (x) { return { label: x.name, value: x.downloads }; }),
        valueFmt: function (v) { return fmtInt(v); },
      })
      : '<div class="mfc-empty" style="height:200px">' + t("Keine Daten", "No data") + "</div>",
  });

  var langs = (trends && trends.by_language || []).filter(function (x) { return x.name; });
  if (!langs.length && g.by_language) {
    langs = g.by_language.map(function (x) { return { name: x.language, downloads: x.downloads }; });
  }
  html += chartCard({
    title: t("Nach Sprache", "By language"),
    body: langs.length
      ? MFCharts.place("langChart", {
        type: "bars", horizontal: true,
        data: langs.map(function (x) { return { label: x.name || "—", value: x.downloads }; }),
        valueFmt: function (v) { return fmtInt(v); },
      })
      : '<div class="mfc-empty" style="height:120px">' + t("Keine Daten", "No data") + "</div>",
  });

  var top = (trends && trends.top_titles || []);
  if (!top.length && g.top_titles) {
    top = g.top_titles.map(function (x) { return { title: x.title, downloads: x.count }; });
  }
  html += chartCard({
    title: t("Top-Titel", "Top titles"),
    sub: t("Meiste Downloads", "Most downloads"),
    body: top.length
      ? MFCharts.place("topTitlesChart", {
        type: "bars", horizontal: true,
        data: top.map(function (x) { return { label: x.title, value: x.downloads }; }),
        color: "#a78bfa",
        valueFmt: function (v) { return fmtInt(v); },
      })
      : '<div class="mfc-empty" style="height:120px">' + t("Keine Daten", "No data") + "</div>",
  });

  html += "</div>";
  return html;
}

function renderQueueSection(q, s) {
  var byStatus = q.by_status || {};
  var statusMeta = [
    { key: "completed", label: t("Abgeschlossen", "Completed"), color: "#22c55e" },
    { key: "running", label: t("Läuft", "Running"), color: "#06b6d4" },
    { key: "queued", label: t("Wartend", "Queued"), color: "#a78bfa" },
    { key: "partial", label: t("Teilweise", "Partial"), color: "#f59e0b" },
    { key: "failed", label: t("Fehlgeschlagen", "Failed"), color: "#f87171" },
    { key: "cancelled", label: t("Abgebrochen", "Cancelled"), color: "#55556a" },
  ];
  var data = statusMeta
    .map(function (m) { return { label: m.label, value: byStatus[m.key] || 0, color: m.color }; })
    .filter(function (d) { return d.value > 0; });

  var html = sectionTitle(t("Queue & Auto-Sync", "Queue & auto-sync"));
  html += '<div class="stats-charts-grid">';
  html += chartCard({
    title: t("Queue-Status", "Queue status"),
    sub: fmtInt(q.total || 0) + " " + t("Einträge gesamt", "entries total"),
    body: data.length
      ? MFCharts.place("queueChart", {
        type: "donut", height: 220, data: data,
        centerValue: fmtInt(q.total || 0),
        centerLabel: t("Einträge", "entries"),
        valueFmt: function (v) { return fmtInt(v); },
      })
      : '<div class="mfc-empty" style="height:200px">' + t("Queue ist leer", "Queue is empty") + "</div>",
  });

  var enabled = s.enabled || 0;
  var totalJobs = s.total_jobs || 0;
  html += chartCard({
    title: t("Auto-Sync", "Auto-sync"),
    sub: fmtInt(totalJobs) + " " + t("Jobs konfiguriert", "jobs configured"),
    body: MFCharts.place("syncGauge", {
      type: "gauge",
      height: 190,
      percent: totalJobs ? (enabled / totalJobs) * 100 : 0,
      label: fmtInt(enabled) + "/" + fmtInt(totalJobs),
      sub: t("aktiv", "active"),
      color: "#34d399",
    }) +
      '<div class="stats-inline-facts">' +
      '<span><b>' + fmtInt(s.total_episodes_found || 0) + "</b>" + t("Episoden gefunden", "episodes found") + "</span>" +
      "<span><b>" + escHtml(s.last_check ? fmtDateTime(s.last_check) : "—") + "</b>" +
      t("letzte Prüfung", "last check") + "</span>" +
      "</div>",
  });
  html += "</div>";
  return html;
}

function renderMediaSection(m) {
  window._mediaStats = m;
  if (!_isModalOpen("incompleteModal")) _renderIncompleteModal();
  if (!_isModalOpen("duplicatesModal")) _renderDuplicatesModal();

  var completeness = m.series_total ? (m.series_complete / m.series_total) * 100 : 0;
  var html = sectionTitle(t("Mediathek", "Media library"),
    m.scanning ? t("Scan läuft…", "Scanning…") : "");
  if (m.scanning) {
    html += '<div class="stats-hint">' +
      t("Mediathek wird gescannt… Werte aktualisieren sich gleich.",
        "Media library is being scanned… values will update shortly.") + "</div>";
  }

  html += '<div class="stats-kpi-row">';
  html += statCard(t("Filme", "Movies"), fmtInt(m.movies_total || 0), "", "#e8914a");
  html += statCard(t("Serien", "Series"), fmtInt(m.series_total || 0), "", "#a78bfa");
  html += statCard(t("Episoden", "Episodes"), fmtInt(m.episodes_total || 0), "", "#6ea8fe");
  html += statCard(t("Belegter Speicher", "Disk usage"), fmtSize(m.total_size_mb || 0),
    fmtInt(m.files_total || 0) + " " + t("Dateien", "files"), "#22c55e");
  html += statCard(t("Unvollständig", "Incomplete"), fmtInt(m.series_incomplete || 0),
    t("Klicken für Details", "Click for details"), "#f59e0b", "openIncompleteModal()");
  html += statCard(t("Duplikate", "Duplicates"), fmtInt(m.duplicates_count || 0),
    t("Klicken für Details", "Click for details"), "#f472b6", "openDuplicatesModal()");
  html += "</div>";

  html += '<div class="stats-charts-grid">';
  html += chartCard({
    title: t("Vollständigkeit", "Completeness"),
    sub: fmtInt(m.series_complete || 0) + " / " + fmtInt(m.series_total || 0) + " " +
      t("Serien komplett", "series complete"),
    body: MFCharts.place("completeGauge", {
      type: "gauge", height: 190, percent: completeness,
      color: completeness >= 90 ? "#22c55e" : completeness >= 60 ? "#f59e0b" : "#f87171",
      sub: t("komplett", "complete"),
    }),
  });

  var res = (m.resolutions || []).filter(function (x) { return x.name && x.name !== "?"; });
  html += chartCard({
    title: t("Auflösungen", "Resolutions"),
    sub: t("Dateien nach Auflösung", "Files by resolution"),
    body: res.length
      ? MFCharts.place("resChart", {
        type: "donut", height: 220,
        data: res.map(function (x) { return { label: x.name, value: x.value }; }),
        valueFmt: function (v) { return fmtInt(v); },
      })
      : '<div class="mfc-empty" style="height:200px">' + t("Keine Daten", "No data") + "</div>",
  });

  var cod = (m.codecs || []).filter(function (x) { return x.name && x.name !== "?"; });
  html += chartCard({
    title: t("Codecs", "Codecs"),
    body: cod.length
      ? MFCharts.place("codecChart", {
        type: "bars", horizontal: true,
        color: "#6ea8fe",
        data: cod.map(function (x) { return { label: x.name, value: x.value }; }),
        valueFmt: function (v) { return fmtInt(v); },
      })
      : '<div class="mfc-empty" style="height:120px">' + t("Keine Daten", "No data") + "</div>",
  });

  var big = m.largest_series || [];
  html += chartCard({
    title: t("Größte Serien", "Largest series"),
    sub: t("Nach belegtem Speicher", "By disk usage"),
    body: big.length
      ? MFCharts.place("largestChart", {
        type: "bars", horizontal: true,
        color: "#f472b6",
        data: big.map(function (x) { return { label: x.title, value: x.size_mb }; }),
        valueFmt: function (v) { return fmtSize(v); },
      })
      : '<div class="mfc-empty" style="height:120px">' + t("Keine Daten", "No data") + "</div>",
  });

  var loc = (m.by_location || []).filter(function (x) { return x.name; });
  if (loc.length > 1) {
    html += chartCard({
      title: t("Speicherorte", "Storage locations"),
      sub: t("Belegter Speicher je Ort", "Disk usage per location"),
      body: MFCharts.place("locChart", {
        type: "donut", height: 220,
        data: loc.map(function (x) { return { label: x.name, value: x.value }; }),
        valueFmt: function (v) { return fmtSize(v); },
      }),
    });
  }
  html += "</div>";
  return html;
}

function renderAllTime(g) {
  var items = [
    { label: t("Downloads gesamt", "Total downloads"), value: fmtInt(g.completed || 0), icon: "download" },
    { label: t("Episoden gesamt", "Total episodes"), value: fmtInt(g.total_episodes || 0), icon: "tv" },
    { label: t("Filme gesamt", "Total movies"), value: fmtInt(g.movie_files || 0), icon: "film" },
    { label: t("Fehlgeschlagen", "Failed"), value: fmtInt(g.failed || 0), icon: "warn" },
    { label: t("Letzte 24 h", "Last 24 h"), value: fmtInt(g.last_24h_completed || 0), icon: "clock" },
    { label: t("Ø Dauer", "Avg. duration"), value: fmtDuration(g.average_duration_seconds), icon: "clock" },
    { label: t("Volumen gesamt", "Total volume"), value: fmtSize(g.total_size_mb || 0), icon: "disk" },
  ];
  var html = sectionTitle(t("Gesamtzahlen", "All-time totals"),
    t("Unabhängig vom Zeitraum", "Independent of the selected range"));
  html += '<div class="stats-facts">' + items.map(function (it) {
    return '<div class="stats-fact"><span class="stats-fact-icon">' + icon(it.icon) + "</span>" +
      '<span class="stats-fact-body"><b>' + it.value + "</b><span>" + escHtml(it.label) + "</span></span></div>";
  }).join("") + "</div>";
  return html;
}

// ---------------------------------------------------------------
// Main render
// ---------------------------------------------------------------

function renderStats(data, container) {
  var g = data.general || {};
  var q = data.queue || {};
  var s = data.sync || {};
  var m = data.media || null;
  var trends = data.trends || MFStats.trends;

  var html = renderHeroRow(g, trends);
  html += renderTrendCharts(trends);
  html += renderBreakdowns(trends, g);
  if (m) html += renderMediaSection(m);
  html += renderQueueSection(q, s);
  html += renderAllTime(g);

  container.innerHTML = html;
  mountCharts(container);

  // Modal bodies live outside #statsContent, so they are rebuilt separately
  // (and never while the user has one open).
  if (!_isModalOpen("speedModal")) _renderSpeedModal();

  // While the library is still scanning, silently reload so the Media counts
  // fill in. Capped so a stuck scan can't poll forever, and suspended while a
  // modal is open — the numbers behind it are not worth interrupting the user
  // for, and the modal is refreshed on close anyway.
  clearTimeout(window._mediaRescanTimer);
  if (m && m.scanning) {
    window._mediaRescanTries = (window._mediaRescanTries || 0) + 1;
    if (window._mediaRescanTries <= 15) {
      // Back off gradually (6s, 8s, 10s, … capped at 30s) instead of hammering
      // a fixed 4s interval for a full minute on a slow library.
      var delay = Math.min(30000, 6000 + window._mediaRescanTries * 2000);
      window._mediaRescanTimer = setTimeout(function () {
        if (anyStatsModalOpen()) return; // retried on close via _closeModal()
        loadStats(false);
      }, delay);
    }
  } else {
    window._mediaRescanTries = 0;
  }
}

loadStats();
