// ===== Download History =====
(function () {
  "use strict";

  var LOCALE = window.__LANG === "de" ? "de-DE" : "en-US";

  var PER_PAGE_OPTIONS = [10, 20, 50, 100];
  function _initialPerPage() {
    var saved = parseInt(localStorage.getItem("aw-hist-perpage"), 10);
    return PER_PAGE_OPTIONS.indexOf(saved) !== -1 ? saved : 10;
  }

  var state = {
    search: "",
    status: "all",
    source: "all",
    range: "all",
    page: 0,            // 0-based current page
    limit: _initialPerPage(),
    total: 0,
    loading: false,
    loaded: false,
  };
  var selectedIds = new Set();

  // ── Helpers ──
  function esc(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function pad(n) { return n < 10 ? "0" + n : "" + n; }

  // Stored timestamps are UTC "YYYY-MM-DD HH:MM:SS"
  function parseUTC(s) {
    if (!s) return null;
    var d = new Date(s.replace(" ", "T") + "Z");
    return isNaN(d.getTime()) ? null : d;
  }
  function fmtDateTime(s) {
    var d = parseUTC(s);
    if (!d) return "—";
    return d.toLocaleString(LOCALE, {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    });
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
    if (mb == null) return "—";
    if (mb >= 1024) return (mb / 1024).toFixed(2) + " GB";
    return mb.toFixed(1) + " MB";
  }
  function fmtSpeed(mbps) {
    if (mbps == null) return "—";
    return mbps.toFixed(2) + " MB/s";
  }
  function epLabel(e) {
    if (e.season == null && e.episode == null) return t("Film", "Movie");
    return "S" + pad(e.season || 0) + "E" + pad(e.episode || 0);
  }
  function statusBadge(st, error) {
    if (st === "failed") {
      var tip = error ? ' title="' + esc(error) + '"' : '';
      return '<span class="hist-status-badge hist-status-failed"' + tip + '>' + t("Fehlgeschlagen", "Failed") + '</span>';
    }
    if (st === "cancelled") {
      var tipc = ' title="' + esc(error || t("Abgebrochen", "Cancelled")) + '"';
      return '<span class="hist-status-badge hist-status-cancelled"' + tipc + '>' + t("Abgebrochen", "Cancelled") + '</span>';
    }
    if (st === "skipped") {
      var tips = ' title="' + esc(error || t("Übersprungen", "Skipped")) + '"';
      return '<span class="hist-status-badge hist-status-skipped"' + tips + '>' + t("Übersprungen", "Skipped") + '</span>';
    }
    return '<span class="hist-status-badge hist-status-completed">' + t("Fertig", "Done") + '</span>';
  }

  function toast(msg) {
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

  // ── State visibility ──
  function showOnly(which) {
    var ids = { loading: "histLoading", empty: "histEmpty", table: "histTableWrap" };
    Object.keys(ids).forEach(function (k) {
      var el = document.getElementById(ids[k]);
      if (el) el.style.display = (k === which) ? (k === "loading" ? "flex" : "block") : "none";
    });
  }

  // ── Fetch + render ──
  // reload() resets to the first page; goToPage(n) keeps search/filter/limit.
  function reload() {
    state.page = 0;
    state.loaded = false;
    selectedIds.clear();
    updateBulkBar();
    showOnly("loading");
    fetchPage();
  }

  function fetchPage() {
    if (state.loading) return;
    state.loading = true;
    var offset = state.page * state.limit;
    var params = new URLSearchParams({
      limit: state.limit, offset: offset, status: state.status,
      source: state.source, range: state.range,
    });
    if (state.search) params.set("search", state.search);

    fetch("/api/history?" + params.toString())
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.loading = false;
        state.loaded = true;
        state.total = data.total || 0;
        var entries = data.entries || [];
        if (entries.length === 0 && state.page === 0) {
          showOnly("empty");
          return;
        }
        showOnly("table");
        renderRows(entries);
        updatePagination();
      })
      .catch(function () {
        state.loading = false;
        if (state.page === 0) showOnly("empty");
      });
  }

  function renderRows(entries) {
    var html = "";
    entries.forEach(function (e) {
      var checked = selectedIds.has(String(e.id)) ? " checked" : "";
      html += '<tr data-id="' + e.id + '">' +
        '<td class="hist-col-cb"><input type="checkbox" class="hist-row-cb"' + checked + ' /></td>' +
        '<td class="hist-cell-title" title="' + esc(e.title) + '">' + esc(e.title) + '</td>' +
        '<td class="hist-col-ep"><span class="hist-ep-badge">' + epLabel(e) + '</span></td>' +
        '<td class="hist-col-time hist-time">' + fmtDateTime(e.started_at) + '</td>' +
        '<td class="hist-col-time hist-time">' + fmtDateTime(e.finished_at) + '</td>' +
        '<td class="hist-col-dur hist-time">' + fmtDuration(e.duration_sec) + '</td>' +
        '<td class="hist-col-size hist-time">' + (e.size_mb != null ? fmtSize(e.size_mb) : "—") + '</td>' +
        '<td class="hist-col-status">' + statusBadge(e.status, e.error) + '</td>' +
        '</tr>';
    });
    var body = document.getElementById("histTableBody");
    body.innerHTML = html;   // replace — page-based, not appended
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
    });
    updateBulkBar();
  }

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

  function totalPages() {
    return Math.max(1, Math.ceil(state.total / state.limit));
  }

  function updatePagination() {
    var pages = totalPages();
    document.getElementById("histCount").textContent =
      state.total + " " + t("Einträge", "entries");

    var pag = document.getElementById("histPagination");
    pag.style.display = pages > 1 ? "" : "none";

    document.getElementById("histPageIndicator").textContent =
      t("Seite", "Page") + " " + (state.page + 1) + " / " + pages;
    document.getElementById("histPrevBtn").disabled = state.page <= 0;
    document.getElementById("histNextBtn").disabled = state.page >= pages - 1;
  }

  function goToPage(n) {
    var pages = totalPages();
    n = Math.max(0, Math.min(n, pages - 1));
    if (n === state.page || state.loading) return;
    state.page = n;
    // Keep the current rows visible until the new page arrives (no loading flash)
    fetchPage();
    var wrap = document.getElementById("histTableWrap");
    if (wrap && wrap.scrollIntoView) wrap.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ── Detail modal ──
  function row(key, val, mono) {
    if (val == null || val === "") return "";
    return '<div class="hist-detail-row">' +
      '<div class="hist-detail-key">' + esc(key) + '</div>' +
      '<div class="hist-detail-val' + (mono ? " hist-mono" : "") + '">' + esc(val) + '</div>' +
      '</div>';
  }

  // Highlighted, wrapping row for the failure reason
  function errorRow(val) {
    if (val == null || val === "") return "";
    return '<div class="hist-detail-row hist-detail-error-row">' +
      '<div class="hist-detail-key">' + esc(t("Fehler", "Error")) + '</div>' +
      '<div class="hist-detail-val hist-detail-error">' + esc(val) + '</div>' +
      '</div>';
  }

  // Neutral, wrapping row for skip reasons (not an error)
  function infoRow(val) {
    if (val == null || val === "") return "";
    return '<div class="hist-detail-row">' +
      '<div class="hist-detail-key">' + esc(t("Info", "Info")) + '</div>' +
      '<div class="hist-detail-val">' + esc(val) + '</div>' +
      '</div>';
  }

  function openDetail(id) {
    fetch("/api/history/" + id)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.entry) { toast(t("Nicht gefunden", "Not found")); return; }
        var e = data.entry;
        document.getElementById("histDetailTitle").textContent = e.title + " · " + epLabel(e);
        var srcLabels = {
          manual: t("Manuell", "Manual"),
          autosync: "AutoSync",
          seerr: "Seerr",
        };
        var body =
          row(t("Titel", "Title"), e.title) +
          row(t("Episode", "Episode"), epLabel(e)) +
          row(t("Status", "Status"),
              e.status === "failed" ? t("Fehlgeschlagen", "Failed")
              : e.status === "cancelled" ? t("Abgebrochen", "Cancelled")
              : e.status === "skipped" ? t("Übersprungen", "Skipped")
              : t("Fertig", "Done")) +
          (e.status === "skipped" ? infoRow(e.error) : errorRow(e.error)) +
          row(t("Start", "Start"), fmtDateTime(e.started_at)) +
          row(t("Ende", "End"), fmtDateTime(e.finished_at)) +
          row(t("Dauer", "Duration"), fmtDuration(e.duration_sec)) +
          row(t("Größe", "Size"), e.size_mb != null ? fmtSize(e.size_mb) : null) +
          row(t("Ø Geschwindigkeit", "Avg. speed"), e.avg_speed_mbps != null ? fmtSpeed(e.avg_speed_mbps) : null) +
          row("Provider", e.provider) +
          row(t("Sprache", "Language"), e.language_label || e.language) +
          row(t("Quelle", "Source"), srcLabels[e.source] || e.source) +
          row(t("Hinzugefügt von", "Added by"), e.username) +
          row(t("Zielpfad", "Target path"), e.target_path, true) +
          row(t("Episoden-URL", "Episode URL"), e.episode_url, true);
        document.getElementById("histDetailBody").innerHTML = body;
        var delBtn = document.getElementById("histDeleteBtn");
        delBtn.onclick = function () { deleteEntry(e.id); };
        var retryBtn = document.getElementById("histRetryBtn");
        if (retryBtn) {
          var canRetry = (e.status === "failed" || e.status === "cancelled") && e.episode_url;
          retryBtn.style.display = canRetry ? "" : "none";
          retryBtn.onclick = function () { retryEntry(e.id); };
        }
        document.getElementById("histDetailModal").style.display = "block";
      })
      .catch(function () { toast(t("Fehler beim Laden", "Failed to load")); });
  }

  window.histCloseDetail = function () {
    document.getElementById("histDetailModal").style.display = "none";
  };

  function deleteEntry(id) {
    showConfirm(t("Diesen Eintrag aus dem Verlauf löschen?", "Delete this entry from the history?"),
      t("Löschen", "Delete")).then(function (ok) {
      if (!ok) return;
      fetch("/api/history/" + id, { method: "DELETE" })
        .then(function (r) { return r.json(); })
        .then(function () {
          histCloseDetail();
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
          histCloseDetail();
          toast(t("Erneut zur Warteschlange hinzugefügt", "Re-added to the queue"));
        } else {
          toast((d && d.error) || t("Erneut versuchen fehlgeschlagen", "Retry failed"));
        }
      })
      .catch(function () { toast(t("Erneut versuchen fehlgeschlagen", "Retry failed")); });
  }

  function _filtersActive() {
    return state.status !== "all" || state.source !== "all" || state.range !== "all" || !!state.search;
  }

  function clearAll() {
    var filtered = _filtersActive();
    var msg = filtered
      ? t("Die aktuell gefilterten Einträge löschen?", "Delete the currently filtered entries?")
      : t("Gesamten Download-Verlauf löschen?", "Clear the entire download history?");
    showConfirm(msg, t("Löschen", "Delete")).then(function (ok) {
      if (!ok) return;
      var body = { status: state.status, source: state.source, range: state.range };
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

  function exportHistory() {
    var params = new URLSearchParams({
      status: state.status, source: state.source, range: state.range, format: "csv",
    });
    if (state.search) params.set("search", state.search);
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

  function clearSelection() {
    selectedIds.clear();
    document.querySelectorAll(".hist-row-cb").forEach(function (cb) { cb.checked = false; });
    updateBulkBar();
  }

  // ── Init ──
  document.addEventListener("DOMContentLoaded", function () {
    var searchEl = document.getElementById("histSearch");
    var debounce;
    searchEl.addEventListener("input", function () {
      clearTimeout(debounce);
      debounce = setTimeout(function () {
        state.search = searchEl.value.trim();
        reload();
      }, 300);
    });

    document.querySelectorAll(".hist-filter-tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        document.querySelectorAll(".hist-filter-tab").forEach(function (b) { b.classList.remove("active"); });
        tab.classList.add("active");
        state.status = tab.getAttribute("data-status");
        reload();
      });
    });

    var perPageEl = document.getElementById("histPerPage");
    if (perPageEl) {
      perPageEl.value = String(state.limit);
      perPageEl.addEventListener("change", function () {
        var v = parseInt(perPageEl.value, 10);
        if (PER_PAGE_OPTIONS.indexOf(v) === -1) v = 10;
        state.limit = v;
        try { localStorage.setItem("aw-hist-perpage", String(v)); } catch (e) {}
        reload();
      });
    }

    document.getElementById("histPrevBtn").addEventListener("click", function () { goToPage(state.page - 1); });
    document.getElementById("histNextBtn").addEventListener("click", function () { goToPage(state.page + 1); });
    document.getElementById("histClearBtn").addEventListener("click", clearAll);

    var srcEl = document.getElementById("histSource");
    if (srcEl) srcEl.addEventListener("change", function () { state.source = srcEl.value; reload(); });
    var rngEl = document.getElementById("histRange");
    if (rngEl) rngEl.addEventListener("change", function () { state.range = rngEl.value; reload(); });

    var expBtn = document.getElementById("histExportBtn");
    if (expBtn) expBtn.addEventListener("click", exportHistory);
    var bulkDel = document.getElementById("histBulkDeleteBtn");
    if (bulkDel) bulkDel.addEventListener("click", bulkDelete);
    var bulkClr = document.getElementById("histBulkClearBtn");
    if (bulkClr) bulkClr.addEventListener("click", clearSelection);

    var selAll = document.getElementById("histSelectAll");
    if (selAll) selAll.addEventListener("change", function () {
      document.querySelectorAll("#histTableBody tr[data-id]").forEach(function (tr) {
        var id = tr.getAttribute("data-id");
        var cb = tr.querySelector(".hist-row-cb");
        if (selAll.checked) { selectedIds.add(String(id)); if (cb) cb.checked = true; }
        else { selectedIds.delete(String(id)); if (cb) cb.checked = false; }
      });
      updateBulkBar();
    });

    reload();
  });
})();
