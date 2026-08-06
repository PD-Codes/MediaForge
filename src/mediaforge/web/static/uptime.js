// UpTime dashboard — polls /api/uptime/status and updates cards in place.
// Buckets the selected time range into fixed-width history bars; click a bar
// to expand a per-bucket detail view. No page reload, no full grid rebuild.
(function () {
  "use strict";

  const I = {
    online:    t("Online", "Online"),
    offline:   t("Offline", "Offline"),
    degraded:  t("Eingeschränkt", "Degraded"),
    pending:   t("Wartet…", "Pending"),
    untracked: t("Nicht überwacht", "Not tracked"),
    uptime:    t("Uptime", "Uptime"),
    response:  t("Ø Antwort", "Avg response"),
    checks:    t("Prüfungen", "Checks"),
    lastCheck: t("Letzte Prüfung", "Last check"),
    never:     t("noch nie", "never"),
    disabledSource: t("Quelle deaktiviert", "Source disabled"),
    allOperational: t("Alle überwachten Seiten sind online", "All monitored sites are operational"),
    someDown:     t("Einige Seiten sind offline", "Some sites are offline"),
    someDegraded: t("Einige Seiten sind eingeschränkt", "Some sites are degraded"),
    noneTracked:  t("Keine Quellen ausgewählt — in den Integrationen aktivieren", "No sources selected — enable them in Integrations"),
    updated:  t("Aktualisiert", "Updated"),
    checking: t("Prüfe…", "Checking…"),
    degradedMsg:   t("Erreichbar, aber Inhalt nicht verifiziert — evtl. Sperr-/Challenge-Seite", "Reachable, but content unverified — possibly a block/challenge page"),
    unreachableMsg: t("Nicht erreichbar", "Unreachable"),
    lastError: t("Letzter Fehler", "Last error"),
    blockedMsg: t("Sperr-/Blockseite erkannt — nicht die echte Seite", "Block/ISP page detected — not the real site"),
    secAgo: t("s", "s"), minAgo: t("min", "min"), hAgo: t("h", "h"), dAgo: t("d", "d"),
    justNow: t("gerade eben", "just now"),
    noData: t("Keine Daten vorhanden", "No data available"),
    clickDetails: t("Balken anklicken für Details", "Click a bar for details"),
    close: t("Schließen", "Close"),
    loading: t("Lädt…", "Loading…"),
    days: t("Tage", "d"),
  };

  const RANGE_PRESETS = [
    { sec: 3600,      label: t("1 Std", "1h") },
    { sec: 6 * 3600,  label: t("6 Std", "6h") },
    { sec: 24 * 3600, label: t("24 Std", "24h") },
    { sec: 3 * 86400, label: t("3 Tage", "3d") },
    { sec: 7 * 86400, label: t("7 Tage", "7d") },
  ];

  let timer = null;
  let sig = "";
  let curMode = "range";   // "range" | "custom"
  let curRange = 6 * 3600; // seconds (range mode)
  let curStart = null, curEnd = null; // custom mode (epoch seconds)
  let _rangeBuiltFor = -1;

  // Shared escaper (static/mf_escape.js). Matters here in particular because
  // the monitor renders error strings that come back from the watched
  // third-party endpoints into data-msg="..." attributes.
  const esc = window.mfEscape;
  function host(url) { return (url || "").replace(/^https?:\/\//, "").replace(/\/$/, ""); }

  function relTime(ts, now) {
    if (!ts) return I.never;
    const d = Math.max(0, (now || Math.floor(Date.now() / 1000)) - ts);
    if (d < 5) return I.justNow;
    if (d < 60) return d + " " + I.secAgo;
    if (d < 3600) return Math.floor(d / 60) + " " + I.minAgo;
    if (d < 86400) return Math.floor(d / 3600) + " " + I.hAgo;
    return Math.floor(d / 86400) + " " + I.dAgo;
  }

  // Day and month only -- the year is noise inside a span that is at most a
  // few days wide. The locale still comes from the app language (see
  // mfLocale in base.html): `undefined` here meant the browser's, which is a
  // different setting and disagreed with the rest of the app.
  function fmtSpanPart(d) {
    return d.toLocaleDateString(window.mfLocale ? window.mfLocale() : "en-US",
                                { day: "2-digit", month: "2-digit" });
  }

  function fmtSpan(s, e) {
    const ds = new Date(s * 1000), de = new Date(e * 1000);
    const sameDay = ds.toDateString() === de.toDateString();
    const a = fmtSpanPart(ds) + " " + (window.mfFormatTime ? window.mfFormatTime(ds) : "");
    const b = (sameDay ? "" : fmtSpanPart(de) + " ") +
              (window.mfFormatTime ? window.mfFormatTime(de) : "");
    return a + " – " + b;
  }

  function statusInfo(src) {
    if (!src.tracked) return { cls: "st-untracked", label: I.untracked };
    switch (src.current_status) {
      case "up":       return { cls: "st-up",       label: I.online };
      case "degraded": return { cls: "st-degraded", label: I.degraded };
      case "down":     return { cls: "st-down",     label: I.offline };
      default:         return { cls: "st-pending",  label: I.pending };
    }
  }

  function barStatusLabel(status) {
    if (status === "up") return I.online;
    if (status === "degraded") return I.degraded;
    if (status === "down") return I.offline;
    if (status === "nodata") return I.noData;
    return I.pending;
  }

  function barMessage(status, msg) {
    if (status === "degraded") return msg && msg !== "reachable, content unverified" ? msg : I.degradedMsg;
    if (status === "down") {
      if (msg === "blocked_page") return I.blockedMsg;
      if (!msg || msg === "unreachable") return I.unreachableMsg;
      return msg;
    }
    return "";
  }

  function errorText(src) {
    if (!src.tracked) return "";
    if (src.current_status === "degraded") return I.degradedMsg;
    if (src.current_status === "down") {
      if (src.last_message === "blocked_page") return I.blockedMsg;
      let m = src.last_message || I.unreachableMsg;
      if (m === "unreachable") m = I.unreachableMsg;
      if (src.last_http_status) m = "HTTP " + src.last_http_status + " · " + m;
      return m;
    }
    return "";
  }

  function bucketClass(st) {
    return st === "up" ? "hb-up" : st === "degraded" ? "hb-degraded" : st === "down" ? "hb-down" : "hb-nodata";
  }
  function bucketsSig(buckets) {
    return (buckets || []).map(function (b) { return (b.start || 0) + ":" + (b.status || "") + ":" + (b.total || 0); }).join(",");
  }
  function bucketsHtml(buckets) {
    if (!buckets || !buckets.length) return '<span class="uc-bars-empty"></span>';
    return buckets.map(function (b) {
      return '<i class="hb ' + bucketClass(b.status) + '" tabindex="0"' +
        ' data-start="' + (b.start || "") + '" data-end="' + (b.end || "") + '"' +
        ' data-status="' + esc(b.status || "") + '"' +
        ' data-total="' + (b.total || 0) + '"' +
        ' data-ms="' + (b.avg_ms == null ? "" : b.avg_ms) + '"' +
        ' data-msg="' + esc(b.msg || "") + '"></i>';
    }).join("");
  }

  // ── Hover tooltip ─────────────────────────────────────────────────────────
  let _tipEl = null;
  function _ensureTip() {
    if (!_tipEl) { _tipEl = document.createElement("div"); _tipEl.className = "hb-tip"; _tipEl.style.display = "none"; document.body.appendChild(_tipEl); }
    return _tipEl;
  }
  function showTip(hb) {
    if (!hb || !document.body.contains(hb)) { hideTip(); return; }
    const tip = _ensureTip();
    const status = hb.getAttribute("data-status");
    const start = parseInt(hb.getAttribute("data-start") || "0", 10);
    const end = parseInt(hb.getAttribute("data-end") || "0", 10);
    const total = parseInt(hb.getAttribute("data-total") || "0", 10);
    const ms = hb.getAttribute("data-ms");
    const msg = hb.getAttribute("data-msg");
    const stCls = status === "up" ? "st-up" : status === "degraded" ? "st-degraded" : status === "down" ? "st-down" : "st-pending";
    let html;
    if (status === "nodata") {
      html = '<div class="hb-tip-status st-pending">' + esc(I.noData) + "</div>";
      if (start) html += '<div class="hb-tip-time">' + esc(fmtSpan(start, end)) + "</div>";
    } else {
      html = '<div class="hb-tip-status ' + stCls + '">' + esc(barStatusLabel(status)) + "</div>";
      if (start) html += '<div class="hb-tip-time">' + esc(fmtSpan(start, end)) + "</div>";
      html += '<div class="hb-tip-rt">' + total + " " + esc(I.checks) + (ms !== "" && ms != null ? " · " + esc(ms) + " ms" : "") + "</div>";
      const em = barMessage(status, msg);
      if (em) html += '<div class="hb-tip-msg">' + esc(em) + "</div>";
    }
    tip.innerHTML = html;
    tip.style.display = "block";
    const r = hb.getBoundingClientRect();
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = r.left + r.width / 2 - tw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
    let top = r.top - th - 10, below = false;
    if (top < 8) { top = r.bottom + 10; below = true; }
    tip.style.left = left + "px";
    tip.style.top = top + "px";
    tip.setAttribute("data-below", below ? "1" : "0");
    tip.style.setProperty("--arrow-x", (r.left + r.width / 2 - left) + "px");
  }
  function hideTip() { if (_tipEl) _tipEl.style.display = "none"; }

  // ── Bucket detail panel ───────────────────────────────────────────────────
  function closeAllDetails() {
    document.querySelectorAll('.uptime-card [data-role="detail"]').forEach(function (d) {
      d.hidden = true; d.dataset.key = ""; d.innerHTML = "";
    });
  }
  function renderDetail(detailEl, start, end, hbs) {
    const span = fmtSpan(start, end);
    let rows;
    if (!hbs.length) {
      rows = '<div class="ucd-empty">' + esc(I.noData) + "</div>";
    } else {
      rows = hbs.map(function (h) {
        const stCls = h.status === "up" ? "st-up" : h.status === "degraded" ? "st-degraded" : h.status === "down" ? "st-down" : "st-pending";
        const tm = window.mfFormatDateTime ? window.mfFormatDateTime(h.ts) : "";
        const rt = h.response_ms != null ? h.response_ms + " ms" : "";
        const m = barMessage(h.status, h.message);
        return '<div class="ucd-row">' +
          '<span class="ucd-dot ' + stCls + '"></span>' +
          '<span class="ucd-time">' + esc(tm) + "</span>" +
          '<span class="ucd-status ' + stCls + '">' + esc(barStatusLabel(h.status)) + "</span>" +
          '<span class="ucd-rt">' + esc(rt) + "</span>" +
          (m ? '<span class="ucd-msg">' + esc(m) + "</span>" : "") +
          "</div>";
      }).join("");
    }
    detailEl.innerHTML =
      '<div class="ucd-head"><span>' + esc(span) + " · " + hbs.length + " " + esc(I.checks) + "</span>" +
      '<button class="uc-detail-close" type="button">✕ ' + esc(I.close) + "</button></div>" +
      '<div class="ucd-list">' + rows + "</div>";
    detailEl.hidden = false;
  }
  async function openDetail(card, srcId, start, end) {
    const detail = card.querySelector('[data-role="detail"]');
    if (!detail) return;
    const key = srcId + ":" + start + ":" + end;
    if (!detail.hidden && detail.dataset.key === key) { detail.hidden = true; detail.dataset.key = ""; detail.innerHTML = ""; return; }
    detail.dataset.key = key;
    detail.hidden = false;
    detail.innerHTML = '<div class="ucd-head"><span>' + esc(I.loading) + "</span></div>";
    try {
      const r = await fetch("/api/uptime/heartbeats?source=" + encodeURIComponent(srcId) + "&start=" + start + "&end=" + end);
      const d = await r.json();
      if (detail.dataset.key !== key) return;
      renderDetail(detail, start, end, d.heartbeats || []);
    } catch (e) {
      detail.innerHTML = '<div class="ucd-empty">' + esc(e.message) + "</div>";
    }
  }

  function initGridEvents() {
    const grid = document.getElementById("uptimeGrid");
    if (!grid) return;
    grid.addEventListener("mouseover", function (e) { const hb = e.target.closest(".hb"); if (hb) showTip(hb); });
    grid.addEventListener("mouseout", function (e) { const hb = e.target.closest(".hb"); if (hb) hideTip(); });
    grid.addEventListener("focusin", function (e) { const hb = e.target.closest(".hb"); if (hb) showTip(hb); });
    grid.addEventListener("focusout", function () { hideTip(); });
    grid.addEventListener("click", function (e) {
      const close = e.target.closest(".uc-detail-close");
      if (close) { const d = close.closest('[data-role="detail"]'); if (d) { d.hidden = true; d.dataset.key = ""; d.innerHTML = ""; } return; }
      const hb = e.target.closest(".hb");
      if (!hb) return;
      const card = hb.closest(".uptime-card");
      if (!card) return;
      hideTip();
      openDetail(card, card.dataset.id, hb.getAttribute("data-start"), hb.getAttribute("data-end"));
    });
    window.addEventListener("scroll", hideTip, true);
  }

  // ── Range selector ────────────────────────────────────────────────────────
  function saveSel() {
    try { localStorage.setItem("uptimeRangeSel", JSON.stringify({ mode: curMode, range: curRange, start: curStart, end: curEnd })); } catch (e) {}
  }
  function loadSel() {
    try {
      const o = JSON.parse(localStorage.getItem("uptimeRangeSel"));
      if (o) { curMode = o.mode === "custom" ? "custom" : "range"; curRange = o.range || 6 * 3600; curStart = o.start || null; curEnd = o.end || null; }
    } catch (e) {}
  }
  function setActivePreset() {
    const h = document.getElementById("uptimeRangePresets");
    if (h) h.querySelectorAll(".uptime-range-btn").forEach(function (b) {
      b.classList.toggle("active", curMode === "range" && parseInt(b.getAttribute("data-sec"), 10) === curRange);
    });
    const ct = document.getElementById("uptimeCustomToggle");
    if (ct) ct.classList.toggle("active", curMode === "custom");
  }
  function buildRangeBar(retentionSec) {
    if (_rangeBuiltFor === retentionSec) { setActivePreset(); return; }
    _rangeBuiltFor = retentionSec;
    const h = document.getElementById("uptimeRangePresets");
    if (!h) return;
    let opts = RANGE_PRESETS.filter(function (o) { return o.sec <= retentionSec; });
    if (!opts.length) opts = [RANGE_PRESETS[0]];
    if (opts[opts.length - 1].sec < retentionSec) {
      opts = opts.concat([{ sec: retentionSec, label: Math.round(retentionSec / 86400) + " " + I.days }]);
    }
    h.innerHTML = opts.map(function (o) {
      return '<button type="button" class="uptime-range-btn" data-sec="' + o.sec + '">' + esc(o.label) + "</button>";
    }).join("");
    setActivePreset();
  }
  function toLocalInput(d) {
    const p = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + "T" + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function prefillCustom() {
    const f = document.getElementById("uptimeCustomFrom"), tt = document.getElementById("uptimeCustomTo");
    if (curMode === "custom" && curStart && curEnd) {
      if (f) f.value = toLocalInput(new Date(curStart * 1000));
      if (tt) tt.value = toLocalInput(new Date(curEnd * 1000));
      return;
    }
    if (f && !f.value) f.value = toLocalInput(new Date(Date.now() - 24 * 3600 * 1000));
    if (tt && !tt.value) tt.value = toLocalInput(new Date());
  }
  window.uptimeToggleCustom = function () {
    const c = document.getElementById("uptimeCustomRange");
    if (!c) return;
    if (c.hidden) { prefillCustom(); c.hidden = false; } else c.hidden = true;
  };
  window.uptimeApplyCustom = function () {
    const f = document.getElementById("uptimeCustomFrom"), tt = document.getElementById("uptimeCustomTo");
    const err = document.getElementById("uptimeCustomErr");
    if (!f || !tt) return;
    const s = f.value ? Math.floor(new Date(f.value).getTime() / 1000) : null;
    const e = tt.value ? Math.floor(new Date(tt.value).getTime() / 1000) : null;
    if (!s || !e || s >= e) { if (err) err.textContent = t("Ungültiger Zeitraum", "Invalid range"); return; }
    if (err) err.textContent = "";
    curMode = "custom"; curStart = s; curEnd = e; curRange = null;
    saveSel(); setActivePreset(); closeAllDetails(); refresh({ force: true });
  };

  function statusUrl() {
    if (curMode === "custom" && curStart && curEnd) return "/api/uptime/status?start=" + curStart + "&end=" + curEnd;
    if (curRange) return "/api/uptime/status?range=" + curRange;
    return "/api/uptime/status";
  }

  // ── Cards ─────────────────────────────────────────────────────────────────
  function skeleton(src) {
    return '' +
      '<div class="uptime-card" id="uc-' + esc(src.id) + '" data-id="' + esc(src.id) + '">' +
        '<div class="uc-row">' +
          '<div class="uc-left">' +
            '<span class="uc-dot"></span>' +
            '<div class="uc-title">' +
              '<div class="uc-name-row">' +
                '<span class="uc-name">' + esc(src.label) + "</span>" +
                '<span class="uc-chip" data-role="chip" hidden></span>' +
              "</div>" +
              '<a class="uc-url" data-role="url" target="_blank" rel="noopener noreferrer"></a>' +
            "</div>" +
            '<span class="uc-pill" data-role="pill"></span>' +
          "</div>" +
          '<div class="uc-barswrap">' +
            '<div class="uc-bars" data-role="bars"></div>' +
            '<div class="uc-hint" data-role="hint" hidden></div>' +
          "</div>" +
          '<div class="uc-stats">' +
            '<div class="uc-stat"><b data-role="pct">—</b><span>' + esc(I.uptime) + "</span></div>" +
            '<div class="uc-stat"><b data-role="avg">—</b><span>' + esc(I.response) + "</span></div>" +
            '<div class="uc-stat"><b data-role="checks">—</b><span>' + esc(I.checks) + "</span></div>" +
            '<div class="uc-stat"><b data-role="last">—</b><span>' + esc(I.lastCheck) + "</span></div>" +
          "</div>" +
        "</div>" +
        '<div class="uc-error" data-role="error" hidden></div>' +
        '<div class="uc-detail" data-role="detail" hidden></div>' +
      "</div>";
  }

  function updateCard(src, now) {
    const card = document.getElementById("uc-" + src.id);
    if (!card) return;
    const st = statusInfo(src);
    card.className = "uptime-card " + st.cls + (src.tracked ? "" : " is-untracked");
    const q = function (r) { return card.querySelector('[data-role="' + r + '"]'); };

    const pill = q("pill");
    if (pill) { pill.textContent = st.label; pill.className = "uc-pill " + st.cls; }
    const url = q("url");
    if (url) { url.textContent = host(src.url); url.href = src.url; }
    const chip = q("chip");
    if (chip) { if (!src.enabled_source) { chip.textContent = I.disabledSource; chip.hidden = false; } else chip.hidden = true; }

    const err = q("error");
    if (err) {
      const msg = errorText(src);
      if (msg) {
        err.hidden = false;
        err.className = "uc-error " + (src.current_status === "down" ? "is-down" : "is-warn");
        err.innerHTML = '<span class="uc-error-ic">' + (src.current_status === "down" ? "✕" : "!") +
                        '</span><span class="uc-error-msg">' + esc(msg) + "</span>";
      } else { err.hidden = true; err.innerHTML = ""; }
    }

    const bars = q("bars");
    if (bars) {
      const bsig = bucketsSig(src.buckets);
      if (bars.getAttribute("data-sig") !== bsig) {
        bars.innerHTML = bucketsHtml(src.buckets);
        bars.setAttribute("data-sig", bsig);
      }
    }
    const hint = q("hint");
    if (hint) {
      const agg = (src.buckets || []).some(function (b) { return (b.total || 0) > 1; });
      if (src.tracked && agg) { hint.textContent = I.clickDetails; hint.hidden = false; }
      else hint.hidden = true;
    }

    const setv = function (r, v) { const el = q(r); if (el) el.textContent = v; };
    setv("pct", src.tracked && src.uptime_pct != null ? src.uptime_pct + "%" : "—");
    setv("avg", src.tracked && src.avg_ms != null ? src.avg_ms + " ms" : "—");
    setv("checks", src.tracked ? (src.total_checks || 0) : "—");
    setv("last", src.tracked ? relTime(src.last_ts, now) : "—");
  }

  function renderSummary(sources) {
    const el = document.getElementById("uptimeSummary");
    if (!el) return;
    const tracked = sources.filter(function (s) { return s.tracked; });
    if (!tracked.length) {
      el.className = "uptime-summary st-pending";
      el.innerHTML = '<span class="uptime-summary-dot"></span><span>' + esc(I.noneTracked) + "</span>";
      return;
    }
    const down = tracked.filter(function (s) { return s.current_status === "down"; }).length;
    const degraded = tracked.filter(function (s) { return s.current_status === "degraded"; }).length;
    let cls, msg;
    if (down) { cls = "st-down"; msg = I.someDown; }
    else if (degraded) { cls = "st-degraded"; msg = I.someDegraded; }
    else { cls = "st-up"; msg = I.allOperational; }
    el.className = "uptime-summary " + cls;
    let counts = tracked.length + " " + t("Quellen", "sources");
    if (down) counts += " · " + down + " " + I.offline;
    if (degraded) counts += " · " + degraded + " " + I.degraded;
    el.innerHTML = '<span class="uptime-summary-dot"></span><span class="uptime-summary-msg">' + esc(msg) +
                   '</span><span class="uptime-summary-count">' + esc(counts) + "</span>";
  }

  function sortSources(sources) {
    return sources.slice().sort(function (a, b) {
      if (!!a.tracked !== !!b.tracked) return a.tracked ? -1 : 1;
      return 0;
    });
  }

  // Guards the 10 s poll against itself. A status response that takes longer
  // than the poll interval used to let requests pile up, and because they can
  // complete out of order an older payload could overwrite a newer one — the
  // page then showed a status that had already been superseded. One request at
  // a time, and a range switch aborts the in-flight one instead of racing it.
  let _inFlight = null;
  let _reqSeq = 0;

  async function refresh(opts) {
    if (_inFlight) {
      // A user action (range change, check-now) must not be swallowed by a
      // slow poll, so it cancels it; a routine poll simply skips this tick.
      if (!(opts && opts.force)) return;
      try { _inFlight.abort(); } catch (e) {}
    }
    const ctrl = new AbortController();
    _inFlight = ctrl;
    const seq = ++_reqSeq;
    let data;
    try {
      const resp = await fetch(statusUrl(), { signal: ctrl.signal });
      data = await resp.json();
    } catch (e) {
      if (e && e.name === "AbortError") return;
      const grid = document.getElementById("uptimeGrid");
      if (grid && !grid.querySelector(".uptime-card")) grid.innerHTML = '<div class="uptime-empty">⚠ ' + esc(e.message) + "</div>";
      return;
    } finally {
      if (_inFlight === ctrl) _inFlight = null;
    }
    // A late answer from a superseded request must never repaint the page.
    if (seq !== _reqSeq) return;
    const now = data.now || Math.floor(Date.now() / 1000);
    const sources = sortSources(data.sources || []);
    const grid = document.getElementById("uptimeGrid");
    renderSummary(sources);
    buildRangeBar((data.retention_days || 7) * 86400);

    const newSig = sources.map(function (s) { return s.id + ":" + (s.tracked ? 1 : 0); }).join(",");
    if (grid && newSig !== sig) {
      grid.innerHTML = sources.length ? sources.map(skeleton).join("") : '<div class="uptime-empty">' + esc(I.noneTracked) + "</div>";
      sig = newSig;
    }
    sources.forEach(function (s) { updateCard(s, now); });

    const up = document.getElementById("uptimeUpdated");
    if (up) up.textContent = I.updated + " " + (window.mfFormatTime ? window.mfFormatTime(now) : "");
  }

  window.uptimeCheckNow = async function () {
    const btn = document.getElementById("uptimeCheckNow");
    const reset = function () {
      if (btn) { btn.disabled = false; btn.textContent = btn.getAttribute("data-label") || "Check now"; }
    };
    if (btn) { btn.disabled = true; btn.textContent = I.checking; }
    try { await fetch("/api/uptime/check-now", { method: "POST" }); } catch (e) {}
    // The button stayed disabled for 2.5 s while the last refresh fired at
    // 4 s, so it was clickable again before the result it triggered had even
    // landed — and every extra click used to start another probe round
    // server-side. Re-enable only after the final refresh (the server answers
    // 409 for a redundant click either way).
    setTimeout(function () { refresh({ force: true }); }, 1500);
    setTimeout(function () { refresh({ force: true }); reset(); }, 4000);
  };

  // setInterval hands the callback its own arguments, so refresh must not be
  // passed to it bare — it would arrive as refresh(<number>) and be read as an
  // options object.
  function startPolling() {
    if (timer) return;
    refresh({ force: true });
    timer = setInterval(function () { refresh(); }, 10000);
  }
  function stopPolling() { if (timer) { clearInterval(timer); timer = null; } }

  document.addEventListener("DOMContentLoaded", function () {
    loadSel();
    if (curMode !== "custom" && !curRange) curRange = 6 * 3600;
    const btn = document.getElementById("uptimeCheckNow");
    if (btn) btn.setAttribute("data-label", btn.textContent);
    const presets = document.getElementById("uptimeRangePresets");
    if (presets) presets.addEventListener("click", function (e) {
      const b = e.target.closest(".uptime-range-btn");
      if (!b) return;
      curMode = "range"; curRange = parseInt(b.getAttribute("data-sec"), 10); curStart = null; curEnd = null;
      saveSel(); setActivePreset();
      const c = document.getElementById("uptimeCustomRange"); if (c) c.hidden = true;
      closeAllDetails(); refresh({ force: true });
    });
    initGridEvents();
    startPolling();
    document.addEventListener("visibilitychange", function () { if (document.hidden) stopPolling(); else startPolling(); });
  });
})();
