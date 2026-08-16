/* ===================================================================
   MediaForge — Home panel bar (the button row under the search field)

   This is the pre-v3-grid Dashboard design (Cockpit variant 3): a row of
   buttons -- queue, activity, library, storage, system -- and ONE panel
   below whose content depends on the button pressed. home_panels.js
   replaced this with a card grid where every panel is visible at once
   (see that file's own docstring); this file was retired at the same time
   and is revived here, unchanged, for "All in one page" mode's Dashboard
   section (Customise this page -> "Home tabs" -> "All in one page") -- the
   button-bar shape is what that mode asks for, not the columns, and
   #homePanelBar/#homePanelBody never overlap with home_panels.js's
   #homeDashColumns, so both files coexist without either knowing about the
   other. Both still read the SAME backend (/api/home-panels,
   /api/home-panel/<id>) that home_panels.js gets its data from too (via
   /api/home-panels/all) -- routes/home_panels.py never dropped either
   endpoint.

   One panel at a time is the design, not a limitation:
     * the poster rows below keep their place and their scroll position,
     * a visit costs one request for the bar plus one for the open panel,
       instead of one per widget,
     * only the open panel polls, and mfPoll() stops it while the tab is
       hidden.

   Every built-in label arrives as an i18n KEY and is resolved here against
   window.__HOME_I18N, which index.html renders through Flask-Babel. That is
   not a style choice: babel.cfg extracts Jinja templates only, so a string
   written in Python source would never reach a catalogue (see the comment at
   the top of routes/home_panels.py). A MODULE panel sends ready-made text
   instead -- it owns its own catalogue -- so every field accepts both and the
   key wins when present.

   Nothing from either side is ever inserted as markup: every string goes
   through mfEscape().

   Deliberately does not touch the feed below. cards.css (.browse-card and its
   hover drawer) and app.js's overlay builder are untouched by this feature.
   =================================================================== */

(function () {
  const bar = document.getElementById("homePanelBar");
  const body = document.getElementById("homePanelBody");
  // Not "All in one page": no-op, same as it always did for the classic
  // layout before this file was retired.
  if (!bar || !body) return;

  const I18N = window.__HOME_I18N || {};
  function HT(key) { return I18N[key] || key; }
  const esc = window.mfEscape || function (s) { return String(s == null ? "" : s); };

  // key + args -> translated text, falling back to the plain text a module
  // sent. The placeholder is "{}" and is filled left to right: Flask-Babel
  // installs newstyle gettext, which turns a "%s" in a template into "{}" in
  // the rendered catalogue string (see mediaforge-home-2-0 notes) -- so "{}"
  // is what actually arrives here.
  function text(key, fallback, args) {
    let out = key ? HT(key) : (fallback || "");
    (args || []).forEach(function (arg) { out = out.replace("{}", arg); });
    return out;
  }

  // Named actions a panel may trigger. The queue is a modal that base.html
  // ships on every page (openQueueHub in queue.js), not a route -- a link to
  // /queue 404s. The map is here and not in the payload so a module panel can
  // ask for "queue" and nothing else.
  const ACTIONS = {
    queue: function () {
      if (typeof window.openQueueHub === "function") window.openQueueHub("all");
      else if (typeof window.openQueueModal === "function") window.openQueueModal();
    },
  };

  // Only kept to clear a value written by an earlier version -- see the
  // persistence note below. Nothing writes it any more.
  const LS_KEY = "mf-home-panel";
  const POLL_MS = 20000;

  let panels = [];                        // [{id,label,badge,icon,builtin}]
  let active = "";                        // "" = closed
  let poller = null;
  let inFlight = null;                    // AbortController of the open fetch

  // ------------------------------------------------------------- persistence
  //
  // There isn't any, deliberately. The cockpit panels used to remember which
  // one was open, in localStorage and as a server-side user preference, and
  // restore it on every page load. In practice that meant arriving at the home
  // page with a panel already expanded, pushing the actual content down, for a
  // choice made once days ago. A panel is a glance at something, not a mode --
  // so every load starts closed and the user opens what they want now.
  //
  // The stored key and preference are simply no longer read or written. Any
  // value left over from an earlier version is inert; nothing has to migrate.
  //
  // Tried once and reverted: restoring the panel on load. Reported straight
  // back as a bug -- "the cockpit menu still opens the same window on reload
  // instead of closing it". Leave it closed.
  function forgetStoredActive() {
    try { localStorage.removeItem(LS_KEY); } catch (e) { /* private mode */ }
  }

  // --------------------------------------------------------------- rendering
  function iconSvg(path) {
    if (!path) return "";
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="' + esc(path) + '"/></svg>';
  }

  /** A badge's colour class. "level" is the only tone the client decides
      rather than the server: the thresholds (amber at 90, red at 95) are the
      same ones the Storage panel already paints its bars with, and keeping
      them in one place means the button and the panel can never disagree
      about whether a disk is in trouble. */
  function badgeTone(p) {
    const tone = p.badge_tone || "info";
    if (tone !== "level") return tone;
    const value = parseInt(p.badge, 10) || 0;
    return value >= 95 ? "err" : (value >= 90 ? "warn" : "muted");
  }

  function renderBar() {
    if (!panels.length) { bar.style.display = "none"; return; }
    bar.style.display = "";
    bar.innerHTML = panels.map(function (p) {
      const on = p.id === active;
      const name = text(p.label_key, p.label);
      // A bare number next to a word explains nothing -- "System 58" was read
      // as a version and as an error code before anyone guessed "58 failed
      // downloads". The server sends what the badge counts as an i18n key;
      // it becomes the button's tooltip and its accessible name.
      let hint = "";
      if (p.badge > 0 && (p.badge_key || p.badge_label)) {
        hint = text(p.badge_key, p.badge_label, [String(p.badge)]);
        // A module that forgot the "{}" placeholder still gets a usable
        // tooltip rather than a sentence with the number missing from it.
        if (hint && hint.indexOf(String(p.badge)) === -1) hint = p.badge + " " + hint;
      }
      return '<button type="button" class="mf-segmented-btn hp-btn' + (on ? " active" : "") +
        '" data-panel="' + esc(p.id) + '" aria-expanded="' + (on ? "true" : "false") +
        '"' + (hint ? ' title="' + esc(hint) + '" aria-label="' + esc(name + " — " + hint) + '"' : '') +
        ' aria-controls="homePanelBody">' + iconSvg(p.icon) +
        '<span>' + esc(name) + '</span>' +
        (p.badge > 0 ? '<span class="mf-facet hp-badge hp-badge-' +
          esc(badgeTone(p)) + '">' + esc(String(p.badge) + (p.badge_suffix || "")) +
          '</span>' : '') +
        '</button>';
    }).join("");
  }

  function renderPanel(data) {
    if (!data) { body.innerHTML = ""; return; }
    if (data.error) {
      body.innerHTML = '<div class="hp-error">' + esc(HT("panel_unavailable")) + '</div>';
      return;
    }
    let html = "";
    if ((data.stats || []).length) {
      html += '<div class="hp-stats">' + data.stats.map(function (s) {
        return '<div class="hp-stat' + (s.tone ? " is-" + esc(s.tone) : "") + '">' +
          '<span class="hp-stat-value">' + esc(text(s.value_key, s.value)) + '</span>' +
          '<span class="hp-stat-label">' + esc(text(s.label_key, s.label)) + '</span></div>';
      }).join("") + '</div>';
    }
    if ((data.items || []).length) {
      html += '<ul class="hp-items">' + data.items.map(function (item) {
        // The whole row is the link when there is one, so the hit target is
        // the row and not just the title -- this list is used on touch too.
        const cls = 'hp-item' + (item.tone ? " is-" + esc(item.tone) : "");
        // An action row is a real <button>: it does something on the page
        // rather than going somewhere, and it has to be reachable by keyboard.
        const open = item.action
          ? '<button type="button" class="' + cls + ' hp-item-btn" data-action="' +
            esc(item.action) + '">'
          : (item.href ? '<a class="' + cls + '" href="' + esc(item.href) + '">'
                       : '<div class="' + cls + '">');
        const close = item.action ? '</button>' : (item.href ? '</a>' : '</div>');
        const sub = text(item.sub_key, item.sub, item.sub_args);
        return '<li>' + open +
          '<span class="hp-item-title">' + esc(item.title) + '</span>' +
          (sub ? '<span class="hp-item-sub">' + esc(sub) + '</span>' : '') +
          (item.percent === null || item.percent === undefined ? '' :
            '<span class="hp-item-bar"><i style="width:' + Number(item.percent) + '%"></i></span>') +
          close + '</li>';
      }).join("") + '</ul>';
    }
    if (!(data.stats || []).length && !(data.items || []).length) {
      html += '<div class="mf-empty hp-empty">' +
        esc(text(data.empty_key, data.empty) || HT("panel_empty")) + '</div>';
    }
    if (data.link && (data.link.href || data.link.action)) {
      const label = esc(text(data.link.label_key, data.link.label) || HT("panel_open")) + ' ›';
      html += data.link.action
        ? '<button type="button" class="hp-more hp-more-btn" data-action="' +
          esc(data.link.action) + '">' + label + '</button>'
        : '<a class="hp-more" href="' + esc(data.link.href) + '">' + label + '</a>';
    }
    body.innerHTML = html;
  }

  // ------------------------------------------------------------------- data
  function loadPanel(id, quiet) {
    if (!quiet) {
      body.innerHTML = '<div class="hp-loading" aria-live="polite">' +
        esc(HT("panel_loading")) + '</div>';
    }
    // One open panel means one in-flight request: clicking through the bar
    // quickly must not let an older answer overwrite a newer one.
    if (inFlight) inFlight.abort();
    const ctrl = ("AbortController" in window) ? new AbortController() : null;
    inFlight = ctrl;
    return fetch("/api/home-panel/" + encodeURIComponent(id),
      ctrl ? { signal: ctrl.signal } : {})
      .then(function (r) {
        if (r.status === 403 || r.status === 404) {
          // Rights changed (or a module was uninstalled) while the page was
          // open. Close rather than sit on an error nobody can act on.
          setActive("", true);
          return null;
        }
        return r.ok ? r.json() : Promise.reject(new Error(String(r.status)));
      })
      .then(function (data) {
        if (id !== active) return;         // the user moved on while we waited
        if (data) renderPanel(data);
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        if (id !== active) return;
        body.innerHTML = '<div class="hp-error">' + esc(HT("panel_failed")) + '</div>';
      });
  }

  function loadBar() {
    return fetch("/api/home-panels")
      .then(function (r) { return r.ok ? r.json() : { panels: [] }; })
      .then(function (data) {
        panels = (data && data.panels) || [];
        if (!panels.length) { renderBar(); return; }
        // Always closed on load -- see the persistence note above. The server
        // still sends `active` for older clients; it is ignored here.
        forgetStoredActive();
        setActive("", true);
      })
      .catch(function () { bar.style.display = "none"; });
  }

  // ------------------------------------------------------------ interaction
  // `silent` is vestigial: it used to mean "restore, don't persist". Nothing
  // persists now, so it is accepted and ignored rather than removed, because
  // both call sites still pass it and one of them is a module-facing path.
  function setActive(id, silent) {  // eslint-disable-line no-unused-vars
    active = id || "";
    renderBar();
    stopPoll();
    if (!active) {
      body.innerHTML = "";
      body.hidden = true;
      return;
    }
    body.hidden = false;
    loadPanel(active, false);
    startPoll();
  }

  function startPoll() {
    // mfPoll pauses itself while the tab is hidden, so an open panel on a
    // background tab costs nothing.
    if (window.mfPoll) {
      poller = window.mfPoll(function () {
        if (active) loadPanel(active, true);
      }, POLL_MS);
    }
  }

  function stopPoll() {
    if (poller && window.mfPollStop) window.mfPollStop(poller);
    poller = null;
  }

  // Delegated so it survives every re-render of the panel body.
  body.addEventListener("click", function (ev) {
    const el = ev.target.closest("[data-action]");
    if (!el) return;
    const fn = ACTIONS[el.getAttribute("data-action")];
    if (fn) { ev.preventDefault(); fn(); }
  });

  bar.addEventListener("click", function (ev) {
    const btn = ev.target.closest("[data-panel]");
    if (!btn) return;
    const id = btn.getAttribute("data-panel");
    // Clicking the open panel's own button closes it. The bar is a control,
    // not a tab strip: "I looked, thanks" needs a way out that is not a
    // second control nobody finds.
    setActive(id === active ? "" : id, false);
  });

  loadBar();
})();
