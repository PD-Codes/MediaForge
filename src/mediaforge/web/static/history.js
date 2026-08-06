/* ===================================================================
   MediaForge — Download history

   Server-driven: filtering, sorting and paging all happen in SQL
   (/api/history), the aggregate figures come from /api/history/summary
   for the *whole* filtered set rather than the visible page, and the
   filter dropdowns are filled from /api/history/facets so they only
   offer values that actually occur.

   Charts are MFCharts (static/mf-charts.js), the same module the
   Statistics page uses; the KPI cards and the modal shell reuse the
   classes from stats.css, so both pages read as one system.
   =================================================================== */

(function () {
  "use strict";

  // One source of truth for the locale: mfLocale() in base.html derives it
  // from the APP language, which is a different setting from the browser's.
  var LOCALE = window.mfLocale ? window.mfLocale() : (window.__LANG === "de" ? "de-DE" : "en-US");
  var PER_PAGE_OPTIONS = [10, 20, 50, 100];

  function _initialPerPage() {
    var saved = parseInt(localStorage.getItem("aw-hist-perpage"), 10);
    return PER_PAGE_OPTIONS.indexOf(saved) !== -1 ? saved : 20;
  }

  var state = {
    search: "", status: "all", source: "all", provider: "all", language: "all",
    range: "all",
    sort: "date", dir: "desc",
    page: 0,                       // 0-based
    limit: _initialPerPage(),
    total: 0,
    loading: false,
    seq: 0,                        // discards out-of-order responses
  };
  var selectedIds = new Set();
  var languageLabels = {};

  // ── Formatting ──────────────────────────────────────────────────

  function esc(s) {
    // Quotes included: every value below is interpolated into attributes
    // somewhere, and textContent-based escaping does not cover them.
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function fmtInt(n) {
    return n == null || isNaN(n) ? "—" : Number(n).toLocaleString(LOCALE);
  }
  function fmtFloat(n, d) {
    if (n == null || isNaN(n)) return "—";
    return Number(n).toLocaleString(LOCALE, { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  /** Stored timestamps are UTC "YYYY-MM-DD HH:MM:SS". */
  function parseUTC(s) {
    if (!s) return null;
    var d = new Date(String(s).replace(" ", "T") + "Z");
    return isNaN(d.getTime()) ? null : d;
  }
  // Formatting goes through the shared helpers so the history, the
  // Operations cards and the UpTime page cannot drift apart again; the UTC
  // parse above stays here because only this page stores its timestamps that
  // way, and handing "2026-08-06 18:16:25" to new Date() reads it as local.
  function fmtDateTime(s) {
    var d = parseUTC(s);
    if (!d) return "—";
    return window.mfFormatDateTime ? window.mfFormatDateTime(d) : d.toISOString();
  }
  function fmtTime(s) {
    var d = parseUTC(s);
    if (!d) return "—";
    return window.mfFormatTime ? window.mfFormatTime(d) : d.toISOString();
  }
  function fmtDuration(sec) {
    if (sec == null) return "—";
    sec = Math.round(sec);
    if (sec < 60) return sec + "s";
    var m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return m + "m " + pad(s) + "s";
    var h = Math.floor(m / 60); m = m % 60;
    return h + "h " + pad(m) + "m";
  }
  function fmtSize(mb) {
    if (mb == null || isNaN(mb)) return "—";
    var v = Number(mb);
    if (v >= 1024 * 1024) return fmtFloat(v / (1024 * 1024), 2) + " TB";
    if (v >= 1024) return fmtFloat(v / 1024, 2) + " GB";
    return fmtFloat(v, 1) + " MB";
  }
  function fmtSpeed(mbps) {
    return mbps == null ? "—" : fmtFloat(mbps, 2) + " MB/s";
  }
  function fmtDayLabel(iso) {
    var p = String(iso || "").split("-");
    if (p.length !== 3) return iso || "";
    if (window.__LANG === "de") return p[2] + "." + p[1] + ".";
    var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return (months[parseInt(p[1], 10) - 1] || p[1]) + " " + p[2];
  }

  function epLabel(e) {
    if (e.season == null && e.episode == null) return t("Film", "Movie");
    return "S" + pad(e.season || 0) + "E" + pad(e.episode || 0);
  }

  var STATUS_META = {
    completed: { label: function () { return t("Fertig", "Done"); }, color: "#22c55e" },
    failed: { label: function () { return t("Fehlgeschlagen", "Failed"); }, color: "#f87171" },
    cancelled: { label: function () { return t("Abgebrochen", "Cancelled"); }, color: "#a78bfa" },
    skipped: { label: function () { return t("Übersprungen", "Skipped"); }, color: "#f59e0b" },
  };
  function statusLabel(st) {
    var m = STATUS_META[st];
    return m ? m.label() : (st || "—");
  }
  function statusBadge(st, error) {
    var cls = STATUS_META[st] ? st : "completed";
    var tip = error ? ' title="' + esc(error) + '"' : "";
    return '<span class="hist-status-badge hist-status-' + cls + '"' + tip + ">" +
      esc(statusLabel(st)) + "</span>";
  }

  var SOURCE_LABELS = {
    manual: function () { return t("Manuell", "Manual"); },
    autosync: function () { return "AutoSync"; },
    seerr: function () { return "Seerr"; },
  };
  function sourceLabel(src) {
    return SOURCE_LABELS[src] ? SOURCE_LABELS[src]() : (src || "—");
  }

  function toast(msg) {
    if (typeof showToast === "function") { showToast(msg); return; }
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = msg;
    el.style.display = "";
    el.classList.remove("show");
    void el.offsetWidth;
    el.classList.add("show");
    clearTimeout(el._hideTimer);
    el._hideTimer = setTimeout(function () { el.classList.remove("show"); }, 4000);
  }

  function showOnly(which) {
    var ids = { loading: "histLoading", empty: "histEmpty", table: "histTableWrap" };
    Object.keys(ids).forEach(function (k) {
      var el = document.getElementById(ids[k]);
      if (el) el.style.display = (k === which) ? (k === "loading" ? "flex" : "block") : "none";
    });
  }

  function filtersActive() {
    return state.status !== "all" || state.source !== "all" || state.provider !== "all" ||
      state.language !== "all" || state.range !== "all" || !!state.search;
  }

  function filterParams() {
    var p = new URLSearchParams({
      status: state.status, source: state.source,
      provider: state.provider, language: state.language, range: state.range,
    });
    if (state.search) p.set("search", state.search);
    return p;
  }

  // ── Summary (breakdown charts) ─────────────────────────────────

  function loadSummary() {
    var host = document.getElementById("histSummary");
    if (!host) return;
    var seq = state.seq;
    fetch("/api/history/summary?days=30&" + filterParams().toString())
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (seq !== state.seq) return;      // a newer filter change won
        renderSummary(data);
      })
      .catch(function () { host.innerHTML = ""; });
  }

  function chartCard(o) {
    // <details> so the charts collapse on phones — same behaviour as the
    // Statistics page, where a stack of full-height charts buried the table.
    var open = window.matchMedia("(max-width: 640px)").matches ? "" : " open";
    return '<details class="chart-card' + (o.wide ? " span-2" : "") + '"' + open + ">" +
      '<summary class="chart-card-head"><div><h3 class="chart-card-title">' + esc(o.title) + "</h3>" +
      (o.sub ? '<p class="chart-card-sub">' + esc(o.sub) + "</p>" : "") + "</div>" +
      '<span class="chart-card-chevron" aria-hidden="true">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" ' +
      'stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>' +
      "</span></summary>" +
      '<div class="chart-card-body">' + o.body + "</div></details>";
  }

  // NB: chart ids are prefixed "histChart…". A bare "histProvider" would
  // collide with the filter <select> of the same name — two elements sharing
  // an id, and getElementById returning whichever came first, which silently
  // stopped that chart from ever mounting.
  function renderSummary(data) {
    var host = document.getElementById("histSummary");
    if (!host) return;
    var tot = data.totals || {};
    if (!tot.entries) { host.innerHTML = ""; return; }

    var hasCharts = typeof MFCharts !== "undefined";

    // The KPI card row (Entries / Success rate / Data volume / Avg. speed /
    // Total time) has been removed on request — this section now only
    // renders the breakdown charts below.
    var html = "";

    // No daily-trend chart here on purpose: the Statistics page already plots
    // downloads per day over a selectable range, and repeating it pushed the
    // actual list of downloads far below the fold.
    //
    // The guard is `hasCharts` alone — the remaining charts are breakdowns of
    // the whole filtered set, so they must still appear when every entry is
    // older than the 30-day window.
    if (hasCharts) {
      var specs = {};
      html += '<div class="stats-charts-grid">';

      var st = (data.by_status || []).filter(function (x) { return x.name; });
      if (st.length) {
        specs.histChartStatus = {
          type: "donut", height: 200,
          data: st.map(function (x) {
            return { label: statusLabel(x.name), value: x.count, color: (STATUS_META[x.name] || {}).color };
          }),
          centerValue: fmtInt(tot.entries), centerLabel: t("gesamt", "total"),
          valueFmt: function (v) { return fmtInt(v); },
        };
        html += chartCard({ title: t("Nach Status", "By status"), body: MFCharts.place("histChartStatus", specs.histChartStatus) });
      }
      var pv = (data.by_provider || []).filter(function (x) { return x.name; });
      if (pv.length) {
        specs.histChartProvider = {
          type: "bars", horizontal: true, color: "#6ea8fe",
          data: pv.map(function (x) { return { label: x.name, value: x.count }; }),
          valueFmt: function (v) { return fmtInt(v); },
        };
        html += chartCard({ title: t("Nach Provider", "By provider"), body: MFCharts.place("histChartProvider", specs.histChartProvider) });
      }
      var lg = (data.by_language || []).filter(function (x) { return x.name; });
      if (lg.length) {
        specs.histChartLang = {
          type: "bars", horizontal: true, color: "#f472b6",
          data: lg.map(function (x) { return { label: x.label || x.name, value: x.count }; }),
          valueFmt: function (v) { return fmtInt(v); },
        };
        html += chartCard({ title: t("Nach Sprache", "By language"), body: MFCharts.place("histChartLang", specs.histChartLang) });
      }
      html += "</div>";
      host.innerHTML = html;
      MFCharts.renderAll(host);
      host.querySelectorAll("details.chart-card").forEach(function (d) {
        d.addEventListener("toggle", function () { if (d.open) MFCharts.renderAll(d); });
      });
      return;
    }
    host.innerHTML = html;
  }

  // ── List ────────────────────────────────────────────────────────

  function reload() {
    state.page = 0;
    selectedIds.clear();
    updateBulkBar();
    showOnly("loading");
    state.seq++;
    loadSummary();
    fetchPage();
    var reset = document.getElementById("histResetBtn");
    if (reset) reset.style.display = filtersActive() ? "" : "none";
  }

  function fetchPage() {
    if (state.loading) return;
    state.loading = true;
    var seq = state.seq;
    var params = filterParams();
    params.set("limit", state.limit);
    params.set("offset", state.page * state.limit);
    params.set("sort", state.sort);
    params.set("dir", state.dir);

    fetch("/api/history?" + params.toString())
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.loading = false;
        if (seq !== state.seq) return;
        state.total = data.total || 0;
        var entries = data.entries || [];
        if (!entries.length) {
          var txt = document.getElementById("histEmptyText");
          if (txt) {
            txt.textContent = filtersActive()
              ? t("Keine Einträge für diese Filter.", "No entries for these filters.")
              : t("Noch keine Downloads aufgezeichnet.", "No downloads recorded yet.");
          }
          showOnly("empty");
          return;
        }
        showOnly("table");
        renderRows(entries);
        renderPagination();
        updateSortIndicators();
      })
      .catch(function () {
        state.loading = false;
        if (seq === state.seq) showOnly("empty");
      });
  }

  /** "Heute" / "Gestern" / a date — the group heading for a day. */
  function dayHeading(iso) {
    var d = parseUTC(iso);
    if (!d) return t("Ohne Datum", "No date");
    var today = new Date();
    var same = function (a, b) {
      return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() &&
        a.getDate() === b.getDate();
    };
    if (same(d, today)) return t("Heute", "Today");
    var yest = new Date(today.getTime() - 86400000);
    if (same(d, yest)) return t("Gestern", "Yesterday");
    return d.toLocaleDateString(LOCALE, { weekday: "long", year: "numeric", month: "long", day: "numeric" });
  }
  function dayKey(iso) {
    var d = parseUTC(iso);
    return d ? d.toDateString() : "none";
  }

  function renderRows(entries) {
    // Day separators only make sense while the list is in date order; with any
    // other sort the rows are not grouped by day at all.
    var grouped = state.sort === "date";
    var html = "";
    var lastDay = null;

    entries.forEach(function (e) {
      var when = e.finished_at || e.created_at;
      if (grouped) {
        var key = dayKey(when);
        if (key !== lastDay) {
          lastDay = key;
          html += '<tr class="hist-day-row"><td colspan="8">' + esc(dayHeading(when)) + "</td></tr>";
        }
      }
      var checked = selectedIds.has(String(e.id)) ? " checked" : "";
      html += '<tr data-id="' + esc(e.id) + '" tabindex="0">' +
        '<td class="hist-col-cb" data-label=""><input type="checkbox" class="hist-row-cb chb-main"' + checked +
        ' aria-label="' + esc(t("Auswählen", "Select")) + '" /></td>' +
        '<td class="hist-cell-title" data-label="' + esc(t("Titel", "Title")) + '" title="' + esc(e.title) + '">' +
        '<span class="hist-title-text">' + esc(e.title) + "</span>" +
        '<span class="hist-title-meta">' + esc(sourceLabel(e.source)) +
        (e.provider ? " · " + esc(e.provider) : "") + "</span></td>" +
        '<td class="hist-col-ep" data-label="' + esc(t("Episode", "Episode")) + '">' +
        '<span class="hist-ep-badge">' + esc(epLabel(e)) + "</span></td>" +
        '<td class="hist-col-time hist-time" data-label="' + esc(t("Beendet", "Finished")) + '">' +
        (grouped ? esc(fmtTime(when)) : esc(fmtDateTime(when))) + "</td>" +
        '<td class="hist-col-dur hist-time" data-label="' + esc(t("Dauer", "Duration")) + '">' +
        esc(fmtDuration(e.duration_sec)) + "</td>" +
        '<td class="hist-col-size hist-time" data-label="' + esc(t("Größe", "Size")) + '">' +
        esc(fmtSize(e.size_mb)) + "</td>" +
        '<td class="hist-col-size hist-time" data-label="' + esc(t("Geschwindigkeit", "Speed")) + '">' +
        esc(fmtSpeed(e.avg_speed_mbps)) + "</td>" +
        '<td class="hist-col-status" data-label="' + esc(t("Status", "Status")) + '">' +
        statusBadge(e.status, e.error) + "</td></tr>";
    });

    var body = document.getElementById("histTableBody");
    body.innerHTML = html;
    body.querySelectorAll("tr[data-id]").forEach(function (tr) {
      var id = tr.getAttribute("data-id");
      var cb = tr.querySelector(".hist-row-cb");
      if (cb) {
        cb.addEventListener("click", function (ev) { ev.stopPropagation(); });
        cb.addEventListener("change", function () {
          if (cb.checked) selectedIds.add(String(id)); else selectedIds.delete(String(id));
          updateBulkBar();
        });
      }
      tr.addEventListener("click", function () { openDetail(id); });
      tr.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openDetail(id); }
      });
    });
    updateBulkBar();
  }

  // ── Sorting ─────────────────────────────────────────────────────

  function setSort(col) {
    if (state.sort === col) {
      state.dir = state.dir === "asc" ? "desc" : "asc";
    } else {
      state.sort = col;
      // Dates read newest-first, everything else reads largest/A-Z first.
      state.dir = col === "title" ? "asc" : "desc";
    }
    state.page = 0;
    state.seq++;
    fetchPage();
  }

  function updateSortIndicators() {
    document.querySelectorAll(".hist-table th[data-sort]").forEach(function (th) {
      var active = th.getAttribute("data-sort") === state.sort;
      th.classList.toggle("is-sorted", active);
      th.classList.toggle("is-asc", active && state.dir === "asc");
      th.setAttribute("aria-sort", active ? (state.dir === "asc" ? "ascending" : "descending") : "none");
    });
  }

  // ── Pagination (shared .mf-pagination component) ─────────────────

  function totalPages() { return Math.max(1, Math.ceil(state.total / state.limit)); }

  function pageNumbers(current, total) {
    if (total <= 7) return Array.from({ length: total }, function (_, i) { return i + 1; });
    var out = [1];
    var from = Math.max(2, current - 1), to = Math.min(total - 1, current + 1);
    if (from > 2) out.push("…");
    for (var i = from; i <= to; i++) out.push(i);
    if (to < total - 1) out.push("…");
    out.push(total);
    return out;
  }

  function renderPagination() {
    var host = document.getElementById("histPagination");
    var cnt = document.getElementById("histCount");
    if (cnt) {
      var from = state.total ? state.page * state.limit + 1 : 0;
      var to = Math.min(state.total, (state.page + 1) * state.limit);
      cnt.textContent = t("Zeige " + fmtInt(from) + "–" + fmtInt(to) + " von " + fmtInt(state.total),
        "Showing " + fmtInt(from) + "–" + fmtInt(to) + " of " + fmtInt(state.total));
    }
    if (!host) return;
    var total = totalPages(), current = state.page + 1;
    if (total <= 1) { host.innerHTML = ""; return; }

    var btn = function (page, label, disabled, title) {
      return '<button type="button" class="mf-pagination-btn" data-page="' + page + '"' +
        (disabled ? " disabled" : "") + ' title="' + esc(title) + '">' + label + "</button>";
    };
    var html = '<div class="mf-pagination">';
    html += btn(1, "&laquo;", current === 1, t("Erste Seite", "First page"));
    html += btn(current - 1, "&lsaquo;", current === 1, t("Zurück", "Back"));
    pageNumbers(current, total).forEach(function (entry) {
      if (entry === "…") { html += '<span class="mf-pagination-ellipsis">…</span>'; return; }
      html += '<button type="button" class="mf-pagination-page' + (entry === current ? " active" : "") +
        '" data-page="' + entry + '"' + (entry === current ? " disabled" : "") + ">" + entry + "</button>";
    });
    html += btn(current + 1, "&rsaquo;", current === total, t("Weiter", "Next"));
    html += btn(total, "&raquo;", current === total, t("Letzte Seite", "Last page"));
    html += "</div>";
    host.innerHTML = html;
    // Delegated: the pager is rebuilt on every page change.
    host.querySelectorAll("[data-page]").forEach(function (b) {
      b.addEventListener("click", function () { goToPage(parseInt(b.getAttribute("data-page"), 10) - 1); });
    });
  }

  function goToPage(n) {
    n = Math.max(0, Math.min(n, totalPages() - 1));
    if (n === state.page || state.loading) return;
    state.page = n;
    state.seq++;
    fetchPage();
    var wrap = document.getElementById("histTableWrap");
    if (wrap && wrap.scrollIntoView) wrap.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ── Bulk selection ──────────────────────────────────────────────

  function updateBulkBar() {
    var bar = document.getElementById("histBulkBar");
    var cnt = document.getElementById("histBulkCount");
    if (!bar) return;
    var n = selectedIds.size;
    bar.style.display = n > 0 ? "flex" : "none";
    if (cnt) cnt.textContent = n + " " + t("ausgewählt", "selected");
    var selAll = document.getElementById("histSelectAll");
    if (selAll) {
      var rowCbs = document.querySelectorAll(".hist-row-cb");
      var checkedNow = document.querySelectorAll(".hist-row-cb:checked").length;
      selAll.checked = rowCbs.length > 0 && checkedNow === rowCbs.length;
      selAll.indeterminate = checkedNow > 0 && checkedNow < rowCbs.length;
    }
  }

  function clearSelection() {
    selectedIds.clear();
    document.querySelectorAll(".hist-row-cb").forEach(function (cb) { cb.checked = false; });
    updateBulkBar();
  }

  // ── Detail modal ────────────────────────────────────────────────

  function detailRow(key, val, opts) {
    if (val == null || val === "") return "";
    opts = opts || {};
    return '<div class="hist-detail-row' + (opts.rowClass ? " " + opts.rowClass : "") + '">' +
      '<div class="hist-detail-key">' + esc(key) + "</div>" +
      '<div class="hist-detail-val' + (opts.mono ? " hist-mono" : "") + '">' + esc(val) +
      (opts.copy ? ' <button type="button" class="hist-copy-btn" data-copy="' + esc(val) + '" title="' +
        esc(t("Kopieren", "Copy")) + '">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
        'stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/>' +
        '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>' : "") +
      "</div></div>";
  }

  function detailSection(title, rows) {
    var body = rows.filter(Boolean).join("");
    if (!body) return "";
    return '<section class="hist-detail-section"><h3 class="hist-detail-section-title">' +
      esc(title) + "</h3>" + body + "</section>";
  }

  function openDetail(id) {
    fetch("/api/history/" + id)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.entry) { toast(t("Nicht gefunden", "Not found")); return; }
        var e = data.entry;
        var meta = STATUS_META[e.status] || STATUS_META.completed;

        document.getElementById("histDetailTitle").textContent = e.title || "—";
        document.getElementById("histDetailSub").textContent =
          epLabel(e) + " · " + statusLabel(e.status) + " · " + fmtDateTime(e.finished_at || e.created_at);
        var iconEl = document.getElementById("histDetailIcon");
        if (iconEl) iconEl.style.setProperty("--icon-color", meta.color);

        var html = '<div class="stats-modal-summary">' +
          '<div class="stats-modal-summary-item"><span class="sms-value" style="color:' + meta.color + '">' +
          esc(statusLabel(e.status)) + '</span><span class="sms-label">' + esc(t("Status", "Status")) + "</span></div>" +
          '<div class="stats-modal-summary-item"><span class="sms-value">' + esc(fmtSize(e.size_mb)) +
          '</span><span class="sms-label">' + esc(t("Größe", "Size")) + "</span></div>" +
          '<div class="stats-modal-summary-item"><span class="sms-value">' + esc(fmtDuration(e.duration_sec)) +
          '</span><span class="sms-label">' + esc(t("Dauer", "Duration")) + "</span></div>" +
          '<div class="stats-modal-summary-item"><span class="sms-value">' + esc(fmtSpeed(e.avg_speed_mbps)) +
          '</span><span class="sms-label">' + esc(t("Ø Geschwindigkeit", "Avg. speed")) + "</span></div></div>";

        if (e.error) {
          // A skip reason is information, not a failure — don't paint it red.
          html += e.status === "skipped"
            ? '<div class="hist-detail-note">' + esc(e.error) + "</div>"
            : '<div class="hist-detail-note is-error">' + esc(e.error) + "</div>";
        }

        html += detailSection(t("Download", "Download"), [
          detailRow(t("Titel", "Title"), e.title),
          detailRow(t("Episode", "Episode"), epLabel(e)),
          detailRow(t("Start", "Start"), fmtDateTime(e.started_at)),
          detailRow(t("Ende", "End"), fmtDateTime(e.finished_at)),
        ]);
        html += detailSection(t("Herkunft", "Origin"), [
          detailRow("Provider", e.provider),
          detailRow(t("Sprache", "Language"), e.language_label || languageLabels[e.language] || e.language),
          detailRow(t("Quelle", "Source"), sourceLabel(e.source)),
          detailRow(t("Hinzugefügt von", "Added by"), e.username),
        ]);
        html += detailSection(t("Dateien", "Files"), [
          detailRow(t("Zielpfad", "Target path"), e.target_path, { mono: true, copy: true }),
          detailRow(t("Episoden-URL", "Episode URL"), e.episode_url, { mono: true, copy: true }),
        ]);

        var bodyEl = document.getElementById("histDetailBody");
        bodyEl.innerHTML = html;
        bodyEl.querySelectorAll(".hist-copy-btn").forEach(function (b) {
          b.addEventListener("click", function () {
            var text = b.getAttribute("data-copy");
            if (navigator.clipboard && navigator.clipboard.writeText) {
              navigator.clipboard.writeText(text)
                .then(function () { toast(t("Kopiert", "Copied")); })
                .catch(function () { toast(t("Kopieren fehlgeschlagen", "Copy failed")); });
            } else {
              toast(t("Kopieren nicht verfügbar", "Copy not available"));
            }
          });
        });

        document.getElementById("histDeleteBtn").onclick = function () { deleteEntry(e.id); };
        var retryBtn = document.getElementById("histRetryBtn");
        if (retryBtn) {
          var canRetry = (e.status === "failed" || e.status === "cancelled") && e.episode_url;
          retryBtn.style.display = canRetry ? "" : "none";
          retryBtn.onclick = function () { retryEntry(e.id); };
        }
        document.getElementById("histDetailModal").style.display = "flex";
        document.body.classList.add("modal-open");
      })
      .catch(function () { toast(t("Fehler beim Laden", "Failed to load")); });
  }

  window.histCloseDetail = function () {
    document.getElementById("histDetailModal").style.display = "none";
    document.body.classList.remove("modal-open");
  };

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var m = document.getElementById("histDetailModal");
    if (m && m.style.display === "flex") window.histCloseDetail();
  });

  // ── Actions ─────────────────────────────────────────────────────

  function deleteEntry(id) {
    showConfirm(t("Diesen Eintrag aus dem Verlauf löschen?", "Delete this entry from the history?"),
      t("Löschen", "Delete")).then(function (ok) {
      if (!ok) return;
      fetch("/api/history/" + id, { method: "DELETE" })
        .then(function (r) { return r.json(); })
        .then(function () {
          window.histCloseDetail();
          toast(t("Eintrag gelöscht", "Entry deleted"));
          reload();
        });
    });
  }

  function retryEntry(id) {
    fetch("/api/history/" + id + "/retry", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          window.histCloseDetail();
          toast(t("Erneut zur Warteschlange hinzugefügt", "Re-added to the queue"));
        } else {
          toast((d && d.error) || t("Erneut versuchen fehlgeschlagen", "Retry failed"));
        }
      })
      .catch(function () { toast(t("Erneut versuchen fehlgeschlagen", "Retry failed")); });
  }

  function clearAll() {
    var msg = filtersActive()
      ? t("Die aktuell gefilterten Einträge löschen?", "Delete the currently filtered entries?")
      : t("Gesamten Download-Verlauf löschen?", "Clear the entire download history?");
    showConfirm(msg, t("Löschen", "Delete")).then(function (ok) {
      if (!ok) return;
      var body = {
        status: state.status, source: state.source, provider: state.provider,
        language: state.language, range: state.range,
      };
      if (state.search) body.search = state.search;
      fetch("/api/history/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(function (r) { return r.json(); })
        .then(function (d) {
          toast(((d && d.deleted != null) ? d.deleted + " " : "") + t("Einträge gelöscht", "entries deleted"));
          reload();
        });
    });
  }

  function exportHistory(fmt) {
    var params = filterParams();
    params.set("format", fmt || "csv");
    window.location.href = "/api/history/export?" + params.toString();
  }

  function bulkDelete() {
    var ids = Array.from(selectedIds).map(Number);
    if (!ids.length) return;
    showConfirm(ids.length + " " + t("Einträge wirklich löschen?", "entries — really delete?"),
      t("Löschen", "Delete")).then(function (ok) {
      if (!ok) return;
      fetch("/api/history/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: ids }),
      }).then(function (r) { return r.json(); })
        .then(function () {
          selectedIds.clear();
          toast(t("Einträge gelöscht", "Entries deleted"));
          reload();
        });
    });
  }

  function resetFilters() {
    state.search = ""; state.status = "all"; state.source = "all";
    state.provider = "all"; state.language = "all"; state.range = "all";
    var el = document.getElementById("histSearch");
    if (el) el.value = "";
    ["histSource", "histProvider", "histLanguage", "histRange"].forEach(function (id) {
      var s = document.getElementById(id);
      if (s) s.value = "all";
    });
    document.querySelectorAll(".hist-filter-tab").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-status") === "all");
    });
    reload();
  }

  /** Fill the provider/language dropdowns from what the history contains. */
  function loadFacets() {
    fetch("/api/history/facets")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        languageLabels = data.language_labels || {};
        var fill = function (id, values, labels) {
          var sel = document.getElementById(id);
          if (!sel) return;
          values.forEach(function (v) {
            var o = document.createElement("option");
            o.value = v;
            o.textContent = (labels && labels[v]) || v;
            sel.appendChild(o);
          });
        };
        fill("histProvider", data.providers || []);
        fill("histLanguage", data.languages || [], languageLabels);
      })
      .catch(function () { /* dropdowns just stay at "all" */ });
  }

  // ── Init ────────────────────────────────────────────────────────

  document.addEventListener("DOMContentLoaded", function () {
    var searchEl = document.getElementById("histSearch");
    if (searchEl) {
      var debounce;
      searchEl.addEventListener("input", function () {
        clearTimeout(debounce);
        debounce = setTimeout(function () {
          state.search = searchEl.value.trim();
          reload();
        }, 300);
      });
    }

    document.querySelectorAll(".hist-filter-tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        document.querySelectorAll(".hist-filter-tab").forEach(function (b) { b.classList.remove("active"); });
        tab.classList.add("active");
        state.status = tab.getAttribute("data-status");
        reload();
      });
    });

    [["histSource", "source"], ["histProvider", "provider"],
     ["histLanguage", "language"], ["histRange", "range"]].forEach(function (pair) {
      var el = document.getElementById(pair[0]);
      if (el) el.addEventListener("change", function () { state[pair[1]] = el.value; reload(); });
    });

    var perPageEl = document.getElementById("histPerPage");
    if (perPageEl) {
      perPageEl.value = String(state.limit);
      perPageEl.addEventListener("change", function () {
        var v = parseInt(perPageEl.value, 10);
        if (PER_PAGE_OPTIONS.indexOf(v) === -1) v = 20;
        state.limit = v;
        try { localStorage.setItem("aw-hist-perpage", String(v)); } catch (e) { /* private mode */ }
        reload();
      });
    }

    document.querySelectorAll(".hist-table th[data-sort]").forEach(function (th) {
      th.addEventListener("click", function () { setSort(th.getAttribute("data-sort")); });
    });

    var byId = function (id, ev, fn) {
      var el = document.getElementById(id);
      if (el) el.addEventListener(ev, fn);
    };
    byId("histClearBtn", "click", clearAll);
    byId("histExportBtn", "click", function () { exportHistory("csv"); });
    byId("histBulkDeleteBtn", "click", bulkDelete);
    byId("histBulkClearBtn", "click", clearSelection);
    byId("histResetBtn", "click", resetFilters);

    var selAll = document.getElementById("histSelectAll");
    if (selAll) {
      selAll.addEventListener("change", function () {
        document.querySelectorAll("#histTableBody tr[data-id]").forEach(function (tr) {
          var id = tr.getAttribute("data-id");
          var cb = tr.querySelector(".hist-row-cb");
          if (selAll.checked) { selectedIds.add(String(id)); if (cb) cb.checked = true; }
          else { selectedIds.delete(String(id)); if (cb) cb.checked = false; }
        });
        updateBulkBar();
      });
    }

    loadFacets();
    reload();
  });
})();
