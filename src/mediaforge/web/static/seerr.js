/* ===================================================================
   MediaForge — Seerr (Jellyseerr/Overseerr) requests page
   -------------------------------------------------------------------
   Rewritten as a single IIFE module: no globals beyond the small
   `window.Seerr` surface the template and app.js need, no inline
   onclick handlers (everything is event delegation), and every value
   that reaches the DOM goes through esc() / escAttr() / safeUrl().

   Filtering, searching and sorting are done SERVER-side
   (/api/seerr/requests) so they cover the whole request set rather
   than only the pages infinite scroll happens to have loaded.

   Talks to:
     GET  /api/seerr/requests?take&skip&q&status&type&sort&dir
     POST /api/seerr/requests/batch          {action, ids, items}
     POST /api/seerr/requests/<id>/hide|unhide|decline
     GET  /api/seerr/hidden
     POST /api/search                        (search modal)
     GET  /api/series?url=...                (search-result posters)

   The series/movie detail modal itself is the shared implementation
   from templates/shared_modals.html — opened via app.js's
   openSeriesFromSeerr(). Nothing modal-related lives in this file.
   =================================================================== */
(function () {
  "use strict";

  var PAGE_SIZE = 20;
  var SEARCH_DEBOUNCE_MS = 320;
  var MAX_BATCH = 50;   // must match MAX_BATCH_IDS in routes/seerr.py

  // ── State ────────────────────────────────────────────────────────
  var S = {
    q: "",
    status: "all",           // all | pending | approved
    type: "all",             // all | tv | movie
    sort: "added",           // added | title | status
    dir: "desc",             // asc | desc
    layout: "grid",          // grid (poster) | list (compact row)
    skip: 0,
    total: null,
    facets: {},
    loading: false,
    seq: 0,                  // generation counter: stale responses are dropped
    searchSeq: 0,
    observer: null,
    items: {},               // id -> request payload (for batch metadata)
    selected: {},            // id -> true
    selectMode: false,
    truncated: false,
    // Context carried into the shared series/movie modal (app.js).
    ctxReqId: null,
    ctxStatus: null,
    ctxIsMovie: false,
  };

  var PREF_KEY = "mf-seerr-prefs-v1";

  function loadPrefs() {
    try {
      var raw = JSON.parse(localStorage.getItem(PREF_KEY) || "{}");
      ["status", "type", "sort", "dir", "layout"].forEach(function (k) {
        if (typeof raw[k] === "string") S[k] = raw[k];
      });
    } catch (e) { /* corrupt prefs are not worth a broken page */ }
  }

  function savePrefs() {
    try {
      localStorage.setItem(PREF_KEY, JSON.stringify({
        status: S.status, type: S.type, sort: S.sort, dir: S.dir, layout: S.layout,
      }));
    } catch (e) { /* private mode / quota — filters just do not persist */ }
  }

  // ── Escaping ─────────────────────────────────────────────────────
  // Single quotes included: attributes in this file are written with
  // either quote character, and an escaper covering only one of them is
  // a trap for whoever copies it next.
  function esc(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c];
    });
  }
  var escAttr = esc;

  // Only same-origin/relative and http(s) URLs may reach a src= or a CSS
  // url(). Everything else (javascript:, data:, vbscript:) is dropped.
  function safeUrl(u) {
    var s = String(u == null ? "" : u).trim();
    if (!s) return "";
    if (/^(https?:)?\/\//i.test(s) || s.charAt(0) === "/") return s;
    return "";
  }

  // A CSS url() lives inside a style attribute, so it needs BOTH the CSS
  // string escape (quote / backslash / parenthesis) and the HTML attribute
  // escape that esc() applies.
  function cssUrl(u) {
    var s = safeUrl(u);
    if (!s) return "";
    return escAttr("url('" + s.replace(/[\\'"()]/g, "\\$&").replace(/[\r\n]/g, "") + "')");
  }

  function $(id) { return document.getElementById(id); }

  // Attribute-selector-safe id (older browsers without CSS.escape fall back
  // to a numeric-only guard — request ids are integers server-side).
  function cssId(v) {
    var s = String(v);
    if (window.CSS && typeof CSS.escape === "function") return CSS.escape(s);
    return s.replace(/[^\w-]/g, "");
  }

  // ── Error codes → messages ───────────────────────────────────────
  // The backend never returns upstream text (it echoes the Seerr URL and
  // sometimes the API key); it returns these stable codes instead.
  function errorMessage(code) {
    switch (code) {
      case "not_configured":
        return t("Seerr ist noch nicht konfiguriert.", "Seerr is not configured yet.");
      case "unreachable":
        return t("Seerr ist nicht erreichbar.", "Seerr is unreachable.");
      case "upstream_error":
        return t("Seerr hat die Aktion abgelehnt.", "Seerr rejected the action.");
      case "forbidden":
        return t("Dafür fehlen dir die Rechte (nur Admins).", "You are not allowed to do that (admins only).");
      case "bad_action":
      case "bad_ids":
        return t("Ungültige Anfrage.", "Invalid request.");
      default:
        return t("Unbekannter Fehler.", "Unknown error.");
    }
  }

  function toast(msg) {
    if (typeof showToast === "function") showToast(msg);
    else console.warn("[Seerr]", msg);
  }

  // ── Dates ────────────────────────────────────────────────────────
  var LOCALE = window.mfLocale ? window.mfLocale() : (window.__LANG === "de" ? "de-DE" : "en-US");

  function formatCreated(iso) {
    if (!iso) return "";
    try {
      return window.mfFormatDate ? window.mfFormatDate(iso)
        : new Date(iso).toLocaleDateString(LOCALE,
            { day: "2-digit", month: "2-digit", year: "numeric" });
    } catch (e) { return ""; }
  }

  // Release dates arrive as a plain "YYYY-MM-DD" (no time, no zone). Feeding
  // that to new Date() would shift it by the local UTC offset and show the
  // previous day west of Greenwich, so anchor it in UTC and format in UTC.
  function formatRelease(ymd) {
    if (!ymd) return "";
    var p = String(ymd).split("-");
    if (p.length < 3) return String(ymd);
    try {
      var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
      return new Intl.DateTimeFormat(LOCALE, {
        day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
      }).format(d);
    } catch (e) { return String(ymd); }
  }

  // ── AutoSync "in sync" lookup ────────────────────────────────────
  // One /api/autosync call per page load, shared by every card. The old
  // implementation issued the fetch inside the card renderer and then
  // poked the DOM from a setTimeout(0) — one promise per card.
  var _autosyncTitles = null;

  function autosyncTitles() {
    if (!_autosyncTitles) {
      _autosyncTitles = fetch("/api/autosync")
        .then(function (r) { return r.ok ? r.json() : { jobs: [] }; })
        .then(function (d) {
          return (d.jobs || [])
            .map(function (j) { return String(j.title || "").toLowerCase(); })
            .filter(Boolean);
        })
        .catch(function () { return []; });
    }
    return _autosyncTitles;
  }

  function markAutosyncPills(requests) {
    var series = requests.filter(function (r) { return !r.isMovie && r.title; });
    if (!series.length) return;
    autosyncTitles().then(function (titles) {
      if (!titles.length) return;
      series.forEach(function (req) {
        var needle = String(req.title).toLowerCase();
        var hit = titles.some(function (jt) {
          return jt.indexOf(needle) !== -1 || needle.indexOf(jt) !== -1;
        });
        if (!hit) return;
        var pill = document.querySelector('.seerr-sync-pill[data-req-id="' + cssId(req.id) + '"]');
        if (!pill) return;
        pill.textContent = t("In Sync", "In sync");
        pill.classList.add("seerr-status-available");
        pill.hidden = false;
      });
    });
  }

  // ── Rendering ────────────────────────────────────────────────────

  // The corner flag answers "where does this stand" in one word. Download
  // progress wins over request status when there is any, because a request
  // that is already downloading has clearly been approved.
  function statusMeta(req) {
    if (req.downloadStatus === 4) {
      return { label: t("Teilweise", "Partial"), flag: "partial" };
    }
    if (req.downloadStatus === 5) {
      return { label: t("Geladen", "Downloaded"), flag: "done" };
    }
    if (req.status === 2) return { label: t("Angenommen", "Approved"), flag: "approved" };
    if (req.status === 1) return { label: t("Ausstehend", "Pending"), flag: "pending" };
    return { label: "", flag: "" };
  }

  // Request -> requested -> approved -> downloaded, as a track. Two status
  // pills side by side never said which of them comes first.
  function progressHtml(req) {
    var steps = [
      { label: t("Angefragt", "Requested"), state: "is-done" },
      {
        label: t("Angenommen", "Approved"),
        state: req.status === 2 ? "is-done" : "is-active",
      },
      {
        label: t("Geladen", "Downloaded"),
        state: req.downloadStatus === 5 ? "is-done"
             : req.downloadStatus === 4 ? "is-active" : "",
      },
    ];
    return '<span class="mf-progress">' + steps.map(function (s) {
      return '<span class="mf-progress-step ' + s.state + '">' +
        '<span class="mf-progress-bar"></span>' +
        '<span class="mf-progress-label">' + esc(s.label) + "</span></span>";
    }).join("") + "</span>";
  }

  function initials(name) {
    var parts = String(name || "").trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return "?";
    if (parts.length === 1) return parts[0].slice(0, 2);
    return parts[0][0] + parts[parts.length - 1][0];
  }

  // "3 days ago" reads faster than a date when you are scanning a backlog.
  function relTime(iso) {
    if (!iso) return "";
    var then = new Date(iso).getTime();
    if (isNaN(then)) return "";
    var days = Math.floor((Date.now() - then) / 86400000);
    if (days <= 0) return t("heute", "today");
    if (days === 1) return t("gestern", "yesterday");
    if (days < 30) return t("vor " + days + " Tagen", days + "d ago");
    var months = Math.round(days / 30);
    return months === 1
      ? t("vor 1 Monat", "1mo ago")
      : t("vor " + months + " Monaten", months + "mo ago");
  }

  function badgesHtml(req) {
    if (req.isMovie) {
      return '<span class="seerr-type-badge seerr-type-movie">' + esc(t("Film", "Movie")) + "</span>";
    }
    var out = '<span class="seerr-type-badge seerr-type-series">' + esc(t("Serie", "Series")) + "</span>";
    var seasons = Array.isArray(req.requestedSeasons) ? req.requestedSeasons : [];
    if (seasons.length) {
      out += '<span class="seerr-season-label">' + esc(t("Staffel", "Season")) +
        (window.__LANG === "de" && seasons.length !== 1 ? "n" : "") + "</span>";
      out += seasons.map(function (n) {
        return '<span class="seerr-season-badge">' + esc(n) + "</span>";
      }).join("");
    } else if (req.numberOfSeasons) {
      out += '<span class="seerr-season-label">' + esc(req.numberOfSeasons) + " " +
        esc(t("Staffel", "Season")) +
        (window.__LANG === "de"
          ? (req.numberOfSeasons !== 1 ? "n" : "")
          : (req.numberOfSeasons !== 1 ? "s" : "")) + "</span>";
    }
    return out;
  }

  function gridClass() {
    return S.layout === "list" ? "seerr-rows" : "mf-poster-grid";
  }

  function cardHtml(req) {
    return S.layout === "list" ? rowHtml(req) : posterHtml(req);
  }

  function selectBox(id, cls) {
    return '<label class="' + cls + '" title="' + escAttr(t("Auswählen", "Select")) + '">' +
      '<input type="checkbox" class="chb-main seerr-card-checkbox" data-req-id="' + escAttr(id) + '"' +
      (S.selected[id] ? " checked" : "") +
      ' aria-label="' + escAttr(t("Anfrage auswählen", "Select request")) + '">' +
      "</label>";
  }

  function hideBtn(id) {
    return '<button type="button" class="seerr-hide-btn" data-action="hide" data-req-id="' + escAttr(id) + '"' +
      ' title="' + escAttr(t("Verstecken", "Hide")) + '"' +
      ' aria-label="' + escAttr(t("Anfrage verstecken", "Hide request")) + '">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15" aria-hidden="true">' +
        '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>' +
        '<path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>' +
        '<line x1="1" y1="1" x2="23" y2="23"/>' +
      "</svg></button>";
  }

  function actionButtons(req, size) {
    var cls = "btn btn-sm" + (size === "block" ? "" : "");
    var out = '<button type="button" class="btn btn-primary btn-sm" data-action="search" data-req-id="' +
      escAttr(req.id) + '">' + esc(t("Suchen", "Search")) + "</button>";
    if (window.seerrCanDecline && req.status !== 2) {
      out += '<button type="button" class="' + cls + ' btn-reject" data-action="decline" data-req-id="' +
        escAttr(req.id) + '">' + esc(t("Ablehnen", "Decline")) + "</button>";
    }
    return out;
  }

  // Facts line over the artwork: type, season count, rating. Short enough
  // to stay on one line at the narrowest grid column.
  function factsLine(req) {
    var bits = [];
    bits.push(req.isMovie ? t("Film", "Movie") : t("Serie", "Series"));
    var seasons = Array.isArray(req.requestedSeasons) ? req.requestedSeasons : [];
    if (!req.isMovie) {
      if (seasons.length) {
        bits.push(seasons.length === 1
          ? t("Staffel " + seasons[0], "Season " + seasons[0])
          : t(seasons.length + " Staffeln", seasons.length + " seasons"));
      } else if (req.numberOfSeasons) {
        bits.push(req.numberOfSeasons === 1
          ? t("1 Staffel", "1 season")
          : t(req.numberOfSeasons + " Staffeln", req.numberOfSeasons + " seasons"));
      }
    }
    if (req.voteAverage) bits.push("★ " + Number(req.voteAverage).toFixed(1));
    return bits.join(" · ");
  }

  // Attribution stays in the card foot, outside the hover overlay: knowing
  // who asked is the thing you scan this page for, and touch has no hover.
  function footHtml(req) {
    if (!req.requestedBy && !req.createdAt) return "";
    var who = req.requestedBy
      ? '<span class="mf-avatar mf-avatar--sm" aria-hidden="true">' + esc(initials(req.requestedBy)) + "</span>" +
        '<span class="mf-poster-who">' + esc(t("von", "by")) + " <b>" + esc(req.requestedBy) + "</b></span>"
      : '<span class="mf-poster-who">' + esc(t("Angefragt", "Requested")) + "</span>";
    var when = req.createdAt
      ? '<span class="mf-poster-when">' + esc(relTime(req.createdAt)) + "</span>"
      : "";
    return '<div class="mf-poster-foot">' + who + when + "</div>";
  }

  // Air/release date, short enough for a poster column. Series get the
  // first-air date, movies their release date; the backend already picks
  // the right one and hands it over as `releaseDate`.
  function releaseLine(req) {
    if (!req.releaseDate) return "";
    var p = String(req.releaseDate).split("-");
    if (p.length < 3) return String(req.releaseDate);
    try {
      var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
      var text = new Intl.DateTimeFormat(LOCALE, {
        day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
      }).format(d);
      // Say what the date means — a bare date on a request card is ambiguous.
      var upcoming = d.getTime() > Date.now();
      var label = req.isMovie
        ? (upcoming ? t("Start", "Releases") : t("Start", "Released"))
        : (upcoming ? t("Start", "Premieres") : t("seit", "since"));
      return label + " " + text;
    } catch (e) { return String(req.releaseDate); }
  }

  function posterHtml(req) {
    var id = req.id;
    var st = statusMeta(req);
    var poster = safeUrl(req.posterUrl);
    var release = releaseLine(req);

    return '<article class="seerr-card mf-poster-card' + (S.selected[id] ? " is-selected" : "") +
      '" data-req-id="' + escAttr(id) + '" tabindex="0">' +

      '<div class="mf-poster-art">' +
        (st.flag
          ? '<span class="mf-poster-flag mf-poster-flag--' + escAttr(st.flag) + '">' +
            esc(st.label) + "</span>"
          : "") +
        selectBox(id, "mf-poster-select") +
        (poster
          ? '<img src="' + escAttr(poster) + '" alt="" loading="lazy" decoding="async">'
          : "") +
        '<div class="mf-poster-scrim">' +
          "<div>" +
            (release
              ? '<div class="seerr-poster-release">' + esc(release) + "</div>"
              : "") +
            '<div class="mf-poster-meta">' + esc(factsLine(req)) + "</div>" +
            '<h3 class="mf-poster-title">' + esc(req.title) + "</h3>" +
          "</div>" +
        "</div>" +
        hideBtn(id) +
      "</div>" +

      // Sibling of the artwork, not a child of it: on hover-capable pointers
      // the overlay lays itself back over the poster (see mf_components.css),
      // while on touch it becomes an ordinary row *below* the poster. Nested
      // inside the artwork the touch variant flowed on top of the image.
      '<div class="mf-poster-actions">' +
        (req.overview
          ? '<p class="seerr-poster-overview">' + esc(req.overview) + "</p>"
          : "") +
        actionButtons(req) +
      "</div>" +

      footHtml(req) +
      '<span class="seerr-sync-pill" data-req-id="' + escAttr(id) + '" hidden></span>' +
    "</article>";
  }

  // Compact row: same data, ordered for scanning rather than browsing.
  function rowHtml(req) {
    var id = req.id;
    var st = statusMeta(req);
    var poster = safeUrl(req.posterUrl);

    return '<article class="seerr-card seerr-row' + (S.selected[id] ? " is-selected" : "") +
      '" data-req-id="' + escAttr(id) + '" tabindex="0">' +
      selectBox(id, "seerr-row-select") +
      (poster
        ? '<img class="seerr-row-art" src="' + escAttr(poster) + '" alt="" loading="lazy" decoding="async">'
        : '<span class="seerr-row-art" aria-hidden="true"></span>') +
      '<div class="seerr-row-main">' +
        '<h3 class="seerr-row-title">' + esc(req.title) +
          (st.flag
            ? '<span class="seerr-row-flag seerr-row-flag--' + escAttr(st.flag) + '">' +
              esc(st.label) + "</span>"
            : "") +
        "</h3>" +
        '<div class="seerr-row-meta">' + esc(factsLine(req)) +
          (releaseLine(req) ? " · " + esc(releaseLine(req)) : "") +
          (req.requestedBy
            ? " · " + esc(t("von", "by")) + " " + esc(req.requestedBy)
            : "") +
          (req.createdAt ? " · " + esc(relTime(req.createdAt)) : "") +
          '<span class="seerr-sync-pill" data-req-id="' + escAttr(id) + '" hidden></span>' +
        "</div>" +
      "</div>" +
      '<div class="seerr-row-progress">' + progressHtml(req) + "</div>" +
      '<div class="seerr-row-actions">' + actionButtons(req) + hideBtn(id) + "</div>" +
    "</article>";
  }

  function emptyHtml(kind, detail) {
    var icon = '<svg class="seerr-empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18"/><path d="M8 2v4M16 2v4"/></svg>';
    var title = "";
    var hintHtml = "";
    if (kind === "not_configured") {
      title = t("Seerr ist noch nicht konfiguriert.", "Seerr is not configured yet.");
      // The only interpolated markup here is a fixed literal link.
      hintHtml = t('Trage URL und API-Key unter <a href="/integrations#seerr">Integrationen</a> ein.',
                   'Add the URL and API key under <a href="/integrations#seerr">Integrations</a>.');
    } else if (kind === "error") {
      title = detail || t("Fehler beim Laden.", "Failed to load.");
    } else if (kind === "filtered") {
      title = t("Keine Anfrage passt zu den Filtern.", "No request matches your filters.");
      hintHtml = esc(t("Setze Suche und Filter zurück, um alles zu sehen.",
                       "Reset the search and filters to see everything."));
    } else {
      title = t("Keine ausstehenden oder angenommenen Anfragen.",
                "No pending or approved requests.");
    }
    return '<div class="seerr-empty' + (kind === "error" ? " seerr-empty-error" : "") + '">' +
      icon +
      "<p>" + esc(title) + "</p>" +
      (hintHtml ? '<p class="seerr-empty-hint">' + hintHtml + "</p>" : "") +
      (kind === "filtered"
        ? '<button type="button" class="btn btn-secondary btn-sm" data-action="reset-filters">' +
          esc(t("Filter zurücksetzen", "Reset filters")) + "</button>"
        : "") +
      "</div>";
  }

  function skeletonHtml(n) {
    var out = "";
    for (var i = 0; i < n; i++) {
      out += S.layout === "list"
        ? '<div class="seerr-card seerr-row seerr-skeleton" aria-hidden="true">' +
            '<span class="seerr-row-art skeleton"></span>' +
            '<div class="seerr-row-main">' +
              '<div class="skeleton seerr-skeleton-line" style="width:42%;height:15px"></div>' +
              '<div class="skeleton seerr-skeleton-line" style="width:66%"></div>' +
            "</div></div>"
        : '<div class="mf-poster-card seerr-skeleton" aria-hidden="true">' +
            '<div class="mf-poster-art skeleton"></div>' +
            '<div class="mf-poster-foot">' +
              '<div class="skeleton seerr-skeleton-line" style="width:64%"></div>' +
            "</div></div>";
    }
    return out;
  }

  // ── Data loading ─────────────────────────────────────────────────

  function buildQuery() {
    var p = new URLSearchParams();
    p.set("take", String(PAGE_SIZE));
    p.set("skip", String(S.skip));
    if (S.q) p.set("q", S.q);
    if (S.status !== "all") p.set("status", S.status);
    if (S.type !== "all") p.set("type", S.type);
    p.set("sort", S.sort);
    p.set("dir", S.dir);
    return p.toString();
  }

  function reload() {
    S.skip = 0;
    S.total = null;
    S.items = {};
    S.loading = false;
    if (S.observer) { S.observer.disconnect(); S.observer = null; }
    var list = $("seerrList");
    if (list) list.innerHTML = '<div class="' + gridClass() + '">' + skeletonHtml(S.layout === "list" ? 4 : 8) + "</div>";
    setStatus("loading");
    return fetchPage(true);
  }

  function fetchPage(isFirst) {
    if (S.loading) return Promise.resolve();
    if (!isFirst && S.total !== null && S.skip >= S.total) return Promise.resolve();
    S.loading = true;
    var seq = ++S.seq;

    var sentinel = $("seerrSentinel");
    if (sentinel) sentinel.remove();

    return fetch("/api/seerr/requests?" + buildQuery(), { headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.json()
          .catch(function () { return {}; })
          .then(function (d) { return { ok: r.ok, data: d }; });
      })
      .then(function (res) {
        if (seq !== S.seq) return;             // a newer query already won
        var list = $("seerrList");
        if (!list) return;
        var data = res.data || {};

        if (!res.ok || data.error) {
          if (isFirst) {
            var code = data.error || "unreachable";
            list.innerHTML = code === "not_configured"
              ? emptyHtml("not_configured")
              : emptyHtml("error", errorMessage(code));
            setStatus("error");
            renderCount(0);
          }
          return;
        }

        var requests = data.requests || [];
        S.total = typeof data.total === "number" ? data.total : requests.length;
        S.facets = data.facets || {};
        S.truncated = !!data.truncated;
        S.skip += requests.length;

        if (isFirst) {
          if (!requests.length) {
            var filtering = !!S.q || S.status !== "all" || S.type !== "all";
            list.innerHTML = emptyHtml(filtering ? "filtered" : "empty");
            setStatus("ok", "0");
            renderCount(0);
            renderFacets();
            return;
          }
          list.innerHTML = '<div class="' + gridClass() + '" id="seerrGrid"></div>';
          setStatus("ok", String(S.total));
        }

        var grid = $("seerrGrid");
        if (!grid) return;
        var html = "";
        requests.forEach(function (req) {
          S.items[req.id] = req;
          html += cardHtml(req);
        });
        grid.insertAdjacentHTML("beforeend", html);

        renderCount(S.total);
        renderFacets();
        markAutosyncPills(requests);
        syncSelectionUi();

        if (S.skip < S.total) attachSentinel(list);
      })
      .catch(function (e) {
        if (seq !== S.seq) return;
        if (isFirst) {
          var list = $("seerrList");
          if (list) list.innerHTML = emptyHtml("error", errorMessage("unreachable"));
          setStatus("error");
        }
        console.error("[Seerr] load failed", e);
      })
      .then(function () {
        if (seq === S.seq) S.loading = false;
      });
  }

  function attachSentinel(list) {
    var el = document.createElement("div");
    el.id = "seerrSentinel";
    el.className = "seerr-sentinel";
    el.innerHTML = '<span class="seerr-spinner" aria-hidden="true"></span>' +
      "<span>" + esc(t("Lade weitere…", "Loading more…")) + "</span>";
    list.appendChild(el);
    if (!S.observer) {
      S.observer = new IntersectionObserver(function (entries) {
        if (entries[0].isIntersecting) fetchPage(false);
      }, { rootMargin: "300px" });
    }
    S.observer.observe(el);
  }

  // ── Toolbar / status chrome ──────────────────────────────────────

  function setStatus(state, label) {
    var wrap = $("seerrStatus");
    var dot = $("seerrStatusDot");
    var lbl = $("seerrStatusLabel");
    if (!wrap || !dot || !lbl) return;
    wrap.hidden = false;
    if (state === "loading") {
      dot.className = "seerr-conn-dot is-starting";
      lbl.textContent = t("Lädt…", "Loading…");
    } else if (state === "ok") {
      dot.className = "seerr-conn-dot is-on";
      lbl.textContent = t("Verbunden", "Connected") + (label ? " · " + label : "");
    } else {
      dot.className = "seerr-conn-dot is-off";
      lbl.textContent = t("Fehler", "Error");
    }
  }

  function renderCount(total) {
    var el = $("seerrCount");
    if (el) {
      el.textContent = total
        ? total + " " + (total === 1 ? t("Anfrage", "request") : t("Anfragen", "requests"))
        : "";
    }
    var warn = $("seerrTruncated");
    if (warn) warn.hidden = !S.truncated;

    // The sidebar badge mirrors the unfiltered total, not the filtered one.
    var badge = $("seerrBadge");
    if (badge) {
      var all = (S.facets && typeof S.facets.all === "number") ? S.facets.all : total;
      badge.textContent = all;
      badge.style.display = all > 0 ? "" : "none";
    }
  }

  function renderFacets() {
    document.querySelectorAll("[data-facet]").forEach(function (el) {
      var n = S.facets ? S.facets[el.getAttribute("data-facet")] : undefined;
      el.textContent = typeof n === "number" ? String(n) : "";
      el.hidden = typeof n !== "number";
    });
  }

  function syncSegmented(groupId, attr, value) {
    var group = $(groupId);
    if (!group) return;
    group.querySelectorAll(".mf-segmented-btn").forEach(function (b) {
      var on = b.getAttribute(attr) === value;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function syncToolbar() {
    syncSegmented("seerrStatusFilter", "data-status", S.status);
    syncSegmented("seerrTypeFilter", "data-type", S.type);
    syncSegmented("seerrLayoutToggle", "data-layout", S.layout);

    var sortSel = $("seerrSort");
    if (sortSel) sortSel.value = S.sort;

    var dirBtn = $("seerrSortDir");
    if (dirBtn) {
      dirBtn.classList.toggle("is-asc", S.dir === "asc");
      var dirLabel = S.dir === "asc"
        ? t("Aufsteigend sortiert", "Sorted ascending")
        : t("Absteigend sortiert", "Sorted descending");
      dirBtn.setAttribute("aria-label", dirLabel);
      dirBtn.title = dirLabel;
    }

    var selBtn = $("seerrSelectToggle");
    if (selBtn) {
      selBtn.classList.toggle("active", S.selectMode);
      selBtn.setAttribute("aria-pressed", S.selectMode ? "true" : "false");
    }
    document.body.classList.toggle("mf-select-mode", S.selectMode);

    var clear = $("seerrSearchClear");
    if (clear) clear.hidden = !S.q;
  }

  // ── Selection / batch ────────────────────────────────────────────

  function selectedIds() {
    return Object.keys(S.selected)
      .filter(function (id) { return S.selected[id]; })
      .map(Number);
  }

  function syncSelectionUi() {
    document.querySelectorAll(".seerr-card-checkbox").forEach(function (cb) {
      var on = !!S.selected[cb.getAttribute("data-req-id")];
      cb.checked = on;
      var card = cb.closest(".seerr-card");
      if (card) card.classList.toggle("is-selected", on);
    });

    var ids = selectedIds();
    var bar = $("seerrBulkBar");
    if (bar) bar.hidden = ids.length === 0;
    var info = $("seerrBulkCount");
    if (info) info.textContent = String(ids.length);
    var over = $("seerrBulkLimit");
    if (over) over.hidden = ids.length <= MAX_BATCH;

    var allBox = $("seerrSelectAll");
    if (allBox) {
      var boxes = document.querySelectorAll(".seerr-card-checkbox");
      allBox.checked = boxes.length > 0 && ids.length >= boxes.length;
      allBox.indeterminate = ids.length > 0 && ids.length < boxes.length;
    }
  }

  function setSelected(id, on) {
    if (on) S.selected[id] = true; else delete S.selected[id];
    syncSelectionUi();
  }

  function clearSelection() {
    S.selected = {};
    syncSelectionUi();
  }

  function runBatch(action) {
    var ids = selectedIds();
    if (!ids.length) return;
    // The server caps the batch too; slicing here keeps the confirm text and
    // the success message honest about what will actually happen.
    if (ids.length > MAX_BATCH) ids = ids.slice(0, MAX_BATCH);

    if (action === "decline") {
      var question = t(
        "Wirklich " + ids.length + " Anfrage(n) ablehnen? Das lässt sich nicht rückgängig machen.",
        "Really decline " + ids.length + " request(s)? This cannot be undone."
      );
      if (!window.confirm(question)) return;
    }

    var items = {};
    if (action === "hide") {
      ids.forEach(function (id) {
        var req = S.items[id];
        if (req) items[id] = { title: req.title || "", posterUrl: req.posterUrl || "" };
      });
    }

    setBulkBusy(true);
    fetch("/api/seerr/requests/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: action, ids: ids, items: items }),
    })
      .then(function (r) {
        return r.json()
          .catch(function () { return {}; })
          .then(function (d) { return { ok: r.ok, data: d }; });
      })
      .then(function (res) {
        var data = res.data || {};
        if (!res.ok || data.error) {
          toast(errorMessage(data.error));
          return;
        }
        var failed = (data.failed || []).length;
        toast(failed
          ? t(failed + " von " + ids.length + " fehlgeschlagen.",
              failed + " of " + ids.length + " failed.")
          : t("Aktion für " + ids.length + " Anfrage(n) ausgeführt.",
              "Applied to " + ids.length + " request(s)."));
        clearSelection();
        reload();
      })
      .catch(function (e) {
        console.error("[Seerr] batch failed", e);
        toast(errorMessage("unreachable"));
      })
      .then(function () { setBulkBusy(false); });
  }

  function setBulkBusy(busy) {
    var bar = $("seerrBulkBar");
    if (!bar) return;
    bar.classList.toggle("is-busy", busy);
    bar.querySelectorAll("button").forEach(function (b) { b.disabled = busy; });
  }

  // ── Single-request actions ───────────────────────────────────────

  function hideRequest(id) {
    var req = S.items[id] || {};
    fetch("/api/seerr/requests/" + encodeURIComponent(id) + "/hide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: req.title || "", posterUrl: req.posterUrl || "" }),
    })
      .then(function (r) {
        if (!r.ok) throw new Error("hide failed");
        var card = document.querySelector('.seerr-card[data-req-id="' + cssId(id) + '"]');
        if (card) card.remove();
        delete S.selected[id];
        if (S.total !== null) S.total = Math.max(0, S.total - 1);
        if (S.facets && typeof S.facets.all === "number") {
          S.facets.all = Math.max(0, S.facets.all - 1);
        }
        renderCount(S.total || 0);
        syncSelectionUi();
        // Hiding the last visible card would otherwise leave a blank page.
        if (!document.querySelector(".seerr-card")) reload();
      })
      .catch(function () {
        toast(t("Verstecken fehlgeschlagen.", "Could not hide the request."));
      });
  }

  function declineRequest(id) {
    var question = t("Anfrage wirklich ablehnen? Diese Aktion kann nicht rückgängig gemacht werden.",
                     "Really decline this request? This action cannot be undone.");
    if (!window.confirm(question)) return;
    fetch("/api/seerr/requests/" + encodeURIComponent(id) + "/decline", { method: "POST" })
      .then(function (r) {
        return r.json()
          .catch(function () { return {}; })
          .then(function (d) { return { ok: r.ok, data: d }; });
      })
      .then(function (res) {
        if (!res.ok || (res.data || {}).error) {
          toast(errorMessage((res.data || {}).error));
          return;
        }
        closeSearchModal();
        if (typeof closeModal === "function") closeModal();
        reload();
      })
      .catch(function () { toast(errorMessage("unreachable")); });
  }

  // ── Search modal (find streams for a request) ────────────────────

  var MOVIE_SOURCES = [
    { site: "filmpalast", label: "FilmPalast", keep: function () { return true; } },
    { site: "megakino", label: "MegaKino", keep: function (r) { return !r.is_series; } },
    // filmo.to is movie-only, so every hit belongs in this list.
    { site: "filmo", label: "filmo.to", keep: function () { return true; } },
  ];
  var SERIES_SOURCES = [
    { site: "aniworld", label: "AniWorld", keep: function () { return true; } },
    { site: "sto", label: "SerienStream", keep: function () { return true; } },
    { site: "megakino", label: "MegaKino", keep: function (r) { return !!r.is_series; } },
    // 9anime/Aniwaves are series-only and opt-in: the search route returns an
    // empty list for them unless the source is enabled in Settings, so no
    // extra gate is needed on this side (same arrangement as hanime below).
    { site: "nineanime", label: "9anime (EN)", keep: function () { return true; } },
    { site: "aniwaves", label: "Aniwaves (EN)", keep: function () { return true; } },
    // hanime (adult) comes last and stays empty unless enabled server-side.
    { site: "hanime", label: "hanime 18+", keep: function () { return true; } },
  ];

  // Sources an installed module registered. Appended to both lists above with
  // an unconditional keep(): the movie/series split is expressed by the
  // built-ins' own `keep` predicates (they know their result shape), and a
  // module source declares no such distinction -- so its hits are offered in
  // both contexts rather than silently dropped from one.
  var _extraSources = [];
  function _loadExtraSources() {
    if (typeof window.loadSearchSources !== "function") return Promise.resolve([]);
    return window.loadSearchSources().then(function (list) {
      _extraSources = (list || [])
        .filter(function (s) { return s.thirdparty; })
        .map(function (s) {
          return { site: s.id, label: s.label || s.id, keep: function () { return true; } };
        });
      return _extraSources;
    }).catch(function () { return []; });
  }

  function openSearchModal(reqId) {
    var req = S.items[reqId] || {};
    S.ctxReqId = reqId;
    S.ctxStatus = req.status;
    S.ctxIsMovie = !!req.isMovie;

    var titleEl = $("seerrSearchTitle");
    if (titleEl) {
      titleEl.textContent = req.isMovie
        ? t("Film suchen", "Search for movie")
        : t("Serie suchen", "Search for series");
    }
    var input = $("seerrSearchInput");
    if (input) input.value = req.title || "";
    var results = $("seerrSearchResults");
    if (results) results.innerHTML = "";

    var declineBtn = $("seerrSearchDeclineBtn");
    if (declineBtn) declineBtn.hidden = !(reqId && window.seerrCanDecline && req.status !== 2);

    openOverlay("seerrSearchOverlay");
    if (input) input.focus();
    // Resolved before the first fan-out so a module source is included in it;
    // loadSearchSources() caches, so this is one request per page load.
    _loadExtraSources().then(function () { if (req.title) doSearch(); });
  }

  function closeSearchModal() { closeOverlay("seerrSearchOverlay"); }

  function doSearch() {
    var input = $("seerrSearchInput");
    var container = $("seerrSearchResults");
    if (!input || !container) return;
    var q = input.value.trim();
    if (!q) return;

    container.innerHTML = '<div class="seerr-search-loading">' +
      '<span class="seerr-spinner" aria-hidden="true"></span>' +
      esc(t("Suche läuft…", "Searching…")) + "</div>";

    var sources = (S.ctxIsMovie ? MOVIE_SOURCES : SERIES_SOURCES).concat(_extraSources);
    var mySeq = ++S.searchSeq;

    Promise.all(sources.map(function (src) {
      return fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keyword: q, site: src.site }),
      })
        .then(function (r) { return r.ok ? r.json() : { results: [] }; })
        .then(function (d) {
          return (d.results || []).filter(src.keep).map(function (r) {
            return { url: r.url, title: r.title, source: src.label };
          });
        })
        .catch(function () { return []; });   // one dead source must not kill the search
    })).then(function (lists) {
      if (mySeq !== S.searchSeq) return;      // a newer search already won
      // Interleave the first two sources so both appear near the top, then
      // append the rest in declared order.
      var combined = [];
      var a = lists[0] || [], b = lists[1] || [];
      for (var i = 0; i < Math.max(a.length, b.length); i++) {
        if (i < a.length) combined.push(a[i]);
        if (i < b.length) combined.push(b[i]);
      }
      for (var j = 2; j < lists.length; j++) combined = combined.concat(lists[j] || []);
      renderSearchResults(combined);
    });
  }

  function renderSearchResults(results) {
    var container = $("seerrSearchResults");
    if (!container) return;
    if (!results.length) {
      container.innerHTML = '<div class="seerr-search-empty">' +
        esc(t("Keine Ergebnisse.", "No results.")) + "</div>";
      return;
    }
    container.innerHTML = results.map(function (r, i) {
      return '<button type="button" class="seerr-search-result" data-url="' + escAttr(safeUrl(r.url)) + '">' +
        '<span class="seerr-search-poster seerr-card-poster-placeholder" data-poster-slot="' + i + '"></span>' +
        '<span class="seerr-search-title">' + esc(r.title) + "</span>" +
        (r.source ? '<span class="seerr-source-pill">' + esc(r.source) + "</span>" : "") +
      "</button>";
    }).join("");

    // Posters load lazily in the background; a failure just leaves the
    // placeholder in place.
    results.forEach(function (r, i) {
      if (!r.url) return;
      fetch("/api/series?url=" + encodeURIComponent(r.url))
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (data) {
          if (!data || !data.poster_url) return;
          var slot = container.querySelector('[data-poster-slot="' + i + '"]');
          if (!slot) return;
          var src = safeUrl(typeof proxyImg === "function" ? proxyImg(data.poster_url) : data.poster_url);
          if (!src) return;
          slot.innerHTML = '<img src="' + escAttr(src) + '" alt="" loading="lazy" decoding="async">';
          slot.classList.remove("seerr-card-poster-placeholder");
        })
        .catch(function () { /* the poster is decoration */ });
    });
  }

  // ── Hidden-requests modal ────────────────────────────────────────

  function openHiddenModal() {
    var list = $("seerrHiddenList");
    if (list) {
      list.innerHTML = '<div class="seerr-search-loading">' +
        '<span class="seerr-spinner" aria-hidden="true"></span>' +
        esc(t("Lädt…", "Loading…")) + "</div>";
    }
    openOverlay("seerrHiddenOverlay");

    fetch("/api/seerr/hidden")
      .then(function (r) { return r.ok ? r.json() : { hidden: [] }; })
      .then(function (data) {
        if (!list) return;
        var items = data.hidden || [];
        if (!items.length) {
          list.innerHTML = '<div class="seerr-search-empty">' +
            esc(t("Keine versteckten Anfragen.", "No hidden requests.")) + "</div>";
          return;
        }
        list.innerHTML = items.map(function (item) {
          var poster = safeUrl(item.poster_url);
          return '<div class="seerr-hidden-row" data-req-id="' + escAttr(item.seerr_request_id) + '">' +
            (poster
              ? '<img class="seerr-hidden-poster" src="' + escAttr(poster) + '" alt="" loading="lazy">'
              : '<div class="seerr-hidden-poster seerr-hidden-poster-placeholder"></div>') +
            '<span class="seerr-hidden-title">' +
              esc(item.title || "#" + item.seerr_request_id) + "</span>" +
            '<button type="button" class="btn btn-sm btn-secondary" data-action="unhide" ' +
              'data-req-id="' + escAttr(item.seerr_request_id) + '">' +
              esc(t("Einblenden", "Show")) + "</button>" +
          "</div>";
        }).join("");
      })
      .catch(function () {
        if (list) {
          list.innerHTML = '<div class="seerr-search-empty">' +
            esc(errorMessage("unreachable")) + "</div>";
        }
      });
  }

  function unhide(id) {
    fetch("/api/seerr/requests/" + encodeURIComponent(id) + "/unhide", { method: "POST" })
      .then(function (r) {
        if (!r.ok) throw new Error("unhide failed");
        var row = document.querySelector(
          '#seerrHiddenList .seerr-hidden-row[data-req-id="' + cssId(id) + '"]');
        if (row) row.remove();
        var list = $("seerrHiddenList");
        if (list && !list.querySelector(".seerr-hidden-row")) {
          list.innerHTML = '<div class="seerr-search-empty">' +
            esc(t("Keine versteckten Anfragen.", "No hidden requests.")) + "</div>";
        }
        reload();
      })
      .catch(function () {
        toast(t("Einblenden fehlgeschlagen.", "Could not restore the request."));
      });
  }

  // ── Overlay helpers ──────────────────────────────────────────────
  // NOTE: modals.css makes an overlay visible via `.overlay[style*="block"]`,
  // i.e. it matches on the literal inline style string. Setting anything else
  // (flex, grid) leaves the overlay at `display:none` and the modal silently
  // never appears -- so "block" here is load-bearing, not a style choice.
  //
  // Body scroll is only released once NO overlay is open any more: closing the
  // search modal used to unlock the page while the shared series modal it had
  // just opened was still on screen.
  function openOverlay(id) {
    var el = $(id);
    if (!el) return;
    el.style.display = "block";
    document.body.style.overflow = "hidden";
  }

  function isOpen(id) {
    var el = $(id);
    return !!el && !!el.style.display && el.style.display !== "none";
  }

  function closeOverlay(id) {
    var el = $(id);
    if (el) el.style.display = "none";
    var stillOpen = Array.prototype.some.call(
      document.querySelectorAll(".overlay"),
      function (o) { return o.style.display && o.style.display !== "none"; }
    );
    if (!stillOpen) document.body.style.overflow = "";
  }

  // ── Wiring ───────────────────────────────────────────────────────

  var searchTimer = null;

  function applyFilterChange() {
    savePrefs();
    syncToolbar();
    clearSelection();
    reload();
  }

  function wireToolbar() {
    var input = $("seerrQuery");
    if (input) {
      input.addEventListener("input", function () {
        S.q = input.value.trim();
        var clear = $("seerrSearchClear");
        if (clear) clear.hidden = !S.q;
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function () { clearSelection(); reload(); }, SEARCH_DEBOUNCE_MS);
      });
      input.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && S.q) {
          e.preventDefault();
          input.value = "";
          S.q = "";
          clearTimeout(searchTimer);
          applyFilterChange();
        }
      });
    }

    var clearBtn = $("seerrSearchClear");
    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        if (input) input.value = "";
        S.q = "";
        clearTimeout(searchTimer);
        applyFilterChange();
        if (input) input.focus();
      });
    }

    var statusGroup = $("seerrStatusFilter");
    if (statusGroup) {
      statusGroup.addEventListener("click", function (e) {
        var b = e.target.closest(".mf-segmented-btn");
        if (!b) return;
        S.status = b.getAttribute("data-status");
        applyFilterChange();
      });
    }

    var typeGroup = $("seerrTypeFilter");
    if (typeGroup) {
      typeGroup.addEventListener("click", function (e) {
        var b = e.target.closest(".mf-segmented-btn");
        if (!b) return;
        S.type = b.getAttribute("data-type");
        applyFilterChange();
      });
    }

    var layoutGroup = $("seerrLayoutToggle");
    if (layoutGroup) {
      layoutGroup.addEventListener("click", function (e) {
        var b = e.target.closest(".mf-segmented-btn");
        if (!b) return;
        S.layout = b.getAttribute("data-layout");
        savePrefs();
        syncToolbar();
        // Poster and row are different markup, not just a different class,
        // so the list has to be rebuilt. Served from the same query.
        reload();
      });
    }

    var sortSel = $("seerrSort");
    if (sortSel) {
      sortSel.addEventListener("change", function () {
        S.sort = sortSel.value;
        // A fresh sort field gets the direction that reads naturally for it:
        // newest-first for dates, A→Z for titles and status.
        S.dir = S.sort === "added" ? "desc" : "asc";
        savePrefs();
        syncToolbar();
        reload();
      });
    }

    var dirBtn = $("seerrSortDir");
    if (dirBtn) {
      dirBtn.addEventListener("click", function () {
        S.dir = S.dir === "asc" ? "desc" : "asc";
        savePrefs();
        syncToolbar();
        reload();
      });
    }

    var selBtn = $("seerrSelectToggle");
    if (selBtn) {
      selBtn.addEventListener("click", function () {
        S.selectMode = !S.selectMode;
        if (!S.selectMode) clearSelection();
        syncToolbar();
      });
    }

    var refreshBtn = $("seerrRefreshBtn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", function () {
        _autosyncTitles = null;
        clearSelection();
        reload();
      });
    }

    var hiddenBtn = $("seerrHiddenBtn");
    if (hiddenBtn) hiddenBtn.addEventListener("click", openHiddenModal);

    var allBox = $("seerrSelectAll");
    if (allBox) {
      allBox.addEventListener("change", function () {
        if (allBox.checked) {
          document.querySelectorAll(".seerr-card-checkbox").forEach(function (cb) {
            S.selected[cb.getAttribute("data-req-id")] = true;
          });
        } else {
          S.selected = {};
        }
        syncSelectionUi();
      });
    }
  }

  function wireBulkBar() {
    var bar = $("seerrBulkBar");
    if (!bar) return;
    bar.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-bulk]");
      if (!btn) return;
      var action = btn.getAttribute("data-bulk");
      if (action === "clear") clearSelection();
      else runBatch(action);
    });
  }

  function wireList() {
    var list = $("seerrList");
    if (!list) return;

    list.addEventListener("click", function (e) {
      if (e.target.closest('[data-action="reset-filters"]')) {
        S.q = "";
        S.status = "all";
        S.type = "all";
        var input = $("seerrQuery");
        if (input) input.value = "";
        applyFilterChange();
        return;
      }
      var btn = e.target.closest("[data-action]");
      if (btn) {
        var id = Number(btn.getAttribute("data-req-id"));
        switch (btn.getAttribute("data-action")) {
          case "hide": hideRequest(id); break;
          case "decline": declineRequest(id); break;
          case "search": openSearchModal(id); break;
        }
        return;
      }
      activateCard(e.target);
    });

    // Keyboard equivalent of the card tap: the card carries tabindex="0", so
    // Enter/Space must do what a click does.
    list.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
      var card = e.target.closest && e.target.closest(".seerr-card");
      if (!card || card !== e.target) return;
      e.preventDefault();
      activateCard(card);
    });

    list.addEventListener("change", function (e) {
      var cb = e.target.closest(".seerr-card-checkbox");
      if (!cb) return;
      setSelected(cb.getAttribute("data-req-id"), cb.checked);
    });
  }

  // A click on the card body itself (not on one of its buttons). The poster
  // layout hides its action overlay behind :hover, which touch devices never
  // deliver, so without this a tap on a poster does nothing at all. In select
  // mode the same tap toggles the selection instead — that is the mode the
  // user is in, and hitting the small checkbox on a phone is fiddly.
  function activateCard(target) {
    if (!target || !target.closest) return;
    var card = target.closest(".seerr-card");
    if (!card || card.classList.contains("seerr-skeleton")) return;
    // Never hijack a real control that happens to live inside the card.
    if (target.closest("a, button, input, label, select, textarea")) return;

    var id = card.getAttribute("data-req-id");
    if (!id) return;
    if (S.selectMode || S.selected[id]) {
      setSelected(id, !S.selected[id]);
    } else {
      openSearchModal(Number(id));
    }
  }

  function wireModals() {
    var searchOverlay = $("seerrSearchOverlay");
    if (searchOverlay) {
      searchOverlay.addEventListener("click", function (e) {
        if (e.target === searchOverlay) closeSearchModal();
      });
    }
    ["seerrSearchClose", "seerrSearchCancel"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("click", closeSearchModal);
    });

    var goBtn = $("seerrSearchGo");
    if (goBtn) goBtn.addEventListener("click", doSearch);

    var modalInput = $("seerrSearchInput");
    if (modalInput) {
      modalInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); doSearch(); }
      });
    }

    var declineBtn = $("seerrSearchDeclineBtn");
    if (declineBtn) {
      declineBtn.addEventListener("click", function () {
        if (S.ctxReqId) declineRequest(S.ctxReqId);
      });
    }

    var results = $("seerrSearchResults");
    if (results) {
      results.addEventListener("click", function (e) {
        var row = e.target.closest(".seerr-search-result");
        if (!row) return;
        var url = row.getAttribute("data-url");
        if (!url) return;
        closeSearchModal();
        if (typeof openSeriesFromSeerr === "function") {
          openSeriesFromSeerr(url, S.ctxReqId, S.ctxStatus === 1, S.ctxIsMovie);
        } else {
          console.error("[Seerr] openSeriesFromSeerr() is missing — is app.js loaded before seerr.js?");
        }
      });
    }

    var hiddenOverlay = $("seerrHiddenOverlay");
    if (hiddenOverlay) {
      hiddenOverlay.addEventListener("click", function (e) {
        if (e.target === hiddenOverlay) { closeOverlay("seerrHiddenOverlay"); return; }
        var btn = e.target.closest('[data-action="unhide"]');
        if (btn) unhide(Number(btn.getAttribute("data-req-id")));
      });
    }
    ["seerrHiddenClose", "seerrHiddenCloseBtn"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("click", function () { closeOverlay("seerrHiddenOverlay"); });
    });

    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      // Topmost first: the hidden-requests modal can be opened over nothing,
      // but the search modal can have the shared series modal on top of it --
      // that one closes itself via app.js's own Escape handler.
      if (isOpen("seerrHiddenOverlay")) { closeOverlay("seerrHiddenOverlay"); return; }
      if (isOpen("seerrSearchOverlay")) closeSearchModal();
    });
  }

  function init() {
    loadPrefs();
    syncToolbar();
    wireToolbar();
    wireBulkBar();
    wireList();
    wireModals();
    reload();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Small public surface. app.js's shared-modal Seerr integration calls three
  // of these by name -- seerrLoad() and closeSeerrSearch() from the download
  // hook in _submitDownloadGroups(), and seerrDeclineRequest() from
  // _declineSeerrFromModal() -- so those names stay available as aliases.
  window.Seerr = {
    reload: reload,
    closeSearchModal: closeSearchModal,
    declineRequest: declineRequest,
    state: S,
  };
  window.seerrLoad = reload;
  window.closeSeerrSearch = closeSearchModal;
  window.seerrDeclineRequest = declineRequest;
})();
