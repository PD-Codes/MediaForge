/* ===================================================================
   MediaForge — shared TMDB detail modal
   -------------------------------------------------------------------
   Companion script for templates/mf_detail_modal.html. Exposes a single
   global, `MFDetailModal`, so any page or third-party module that knows
   a TMDB id can show a detail view without pulling in app.js.

       MFDetailModal.open({
         tmdbId: 1396,            // optional — without it, no TMDB fetch
         mediaType: "tv",         // "tv" | "movie" (default "tv")
         title: "Breaking Bad",
         subtitle: "S01E01",      // small badge next to the date
         caption: "Pilot",        // episode name / one-liner
         date: "2008-01-20",      // ISO day, rendered in the UI locale
         image: "/static/x.jpg",  // poster/still URL (already proxied)
         searchTitle: "…",        // what the search button looks for
       });
       MFDetailModal.close();

   Everything is escaped, the TMDB request is abortable (a second click
   must not let a slower first answer overwrite the newer entry), and
   the "search streams" button prefers app.js's openAniSearchModal()
   when the host page has it and otherwise falls back to the home
   search via ?q=.
   =================================================================== */
(function () {
  "use strict";

  var _abort = null;
  var _ctx = {};

  function $(id) { return document.getElementById(id); }

  function esc(s) {
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

  // base.html defines t(de, en) globally; guard anyway so the component also
  // works if it is ever embedded somewhere that does not.
  function tr(de, en) {
    return typeof t === "function" ? t(de, en) : en;
  }

  function locale() {
    return window.__LANG === "de" ? "de-DE" : "en-US";
  }

  // ISO days have no time-of-day. Anchoring them in UTC and formatting in UTC
  // keeps them from sliding to the previous day west of Greenwich.
  function formatDay(iso) {
    if (!iso) return "";
    var p = String(iso).split("-");
    if (p.length < 3) return String(iso);
    try {
      return new Intl.DateTimeFormat(locale(), {
        weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
      }).format(new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])));
    } catch (e) { return String(iso); }
  }

  function setText(id, value, hideWhenEmpty) {
    var el = $(id);
    if (!el) return;
    el.textContent = value || "";
    if (hideWhenEmpty) el.hidden = !value;
  }

  function anyOverlayOpen() {
    return Array.prototype.some.call(
      document.querySelectorAll(".overlay"),
      function (o) { return o.style.display && o.style.display !== "none"; }
    );
  }

  function open(opts) {
    var overlay = $("mfDetailOverlay");
    if (!overlay) {
      console.error("[MFDetailModal] markup missing — include mf_detail_modal.html");
      return;
    }
    _ctx = opts || {};

    setText("mfDetailTitle", _ctx.title || "");
    setText("mfDetailSubtitle", _ctx.subtitle || "", true);
    setText("mfDetailCaption", _ctx.caption || "", true);
    setText("mfDetailDate", formatDay(_ctx.date));

    var poster = $("mfDetailPoster");
    if (poster) {
      var img = safeUrl(_ctx.image);
      poster.innerHTML = img
        ? '<img src="' + esc(img) + '" alt="" loading="lazy" decoding="async">'
        : "";
      poster.classList.toggle("is-empty", !img);
    }

    var meta = $("mfDetailMeta");
    if (meta) meta.innerHTML = "";

    var overview = $("mfDetailOverview");
    if (overview) {
      overview.classList.toggle("is-loading", !!_ctx.tmdbId);
      overview.textContent = _ctx.tmdbId
        ? tr("Lade Details…", "Loading details…")
        : tr("Keine weiteren Details verfügbar.", "No further details available.");
    }

    // modals.css makes an overlay visible via `.overlay[style*="block"]`, i.e.
    // it matches on the literal inline style string — "block" is load-bearing.
    overlay.style.display = "block";
    document.body.style.overflow = "hidden";

    if (_ctx.tmdbId) loadDetails(_ctx.tmdbId, _ctx.mediaType === "movie" ? "movie" : "tv");
  }

  function loadDetails(tmdbId, mediaType) {
    if (_abort) _abort.abort();
    _abort = typeof AbortController === "function" ? new AbortController() : null;

    fetch("/api/tmdb/details?id=" + encodeURIComponent(tmdbId) + "&type=" + mediaType,
          _abort ? { signal: _abort.signal } : undefined)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { renderDetails(d); })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        renderDetails(null);
      });
  }

  function renderDetails(d) {
    var overview = $("mfDetailOverview");
    if (overview) overview.classList.remove("is-loading");

    if (!d || d.error) {
      if (overview) {
        overview.textContent = tr("Details konnten nicht geladen werden.",
                                  "Could not load details.");
      }
      return;
    }

    if (overview) {
      overview.textContent = d.overview ||
        tr("Keine Beschreibung hinterlegt.", "No description available.");
    }

    var chips = [];
    if (d.vote_average) chips.push("★ " + Number(d.vote_average).toFixed(1));
    if (d.number_of_seasons) {
      chips.push(d.number_of_seasons + " " +
        (d.number_of_seasons === 1 ? tr("Staffel", "season") : tr("Staffeln", "seasons")));
    }
    if (d.runtime) chips.push(d.runtime + " min");
    if (Array.isArray(d.genres) && d.genres.length) {
      chips.push(d.genres.slice(0, 3).map(function (g) { return g.name; }).join(", "));
    }
    if (d.status) chips.push(String(d.status));

    var meta = $("mfDetailMeta");
    if (meta) {
      meta.innerHTML = chips.map(function (c) {
        return '<span class="mf-detail-chip">' + esc(c) + "</span>";
      }).join("");
    }
  }

  function close() {
    var overlay = $("mfDetailOverlay");
    if (overlay) overlay.style.display = "none";
    if (_abort) { _abort.abort(); _abort = null; }
    // Only release the page scroll once nothing else is still open on top.
    if (!anyOverlayOpen()) document.body.style.overflow = "";
  }

  function searchStreams() {
    var title = _ctx.searchTitle || _ctx.title || "";
    if (!title) return;
    close();
    // Prefer the in-page cross-provider search when the host page loaded app.js.
    if (typeof openAniSearchModal === "function") {
      openAniSearchModal(title, _ctx.tmdbId || null,
                         _ctx.mediaType === "movie" ? "movie" : "tv");
      return;
    }
    window.location.href = "/?q=" + encodeURIComponent(title);
  }

  function wire() {
    var overlay = $("mfDetailOverlay");
    if (!overlay) return;   // component not included on this page

    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) close();
    });
    ["mfDetailClose", "mfDetailCloseBtn"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("click", close);
    });
    var searchBtn = $("mfDetailSearchBtn");
    if (searchBtn) searchBtn.addEventListener("click", searchStreams);

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && overlay.style.display === "block") close();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }

  window.MFDetailModal = { open: open, close: close };
})();
