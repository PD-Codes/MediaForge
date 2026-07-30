/* ===================================================================
   MediaForge — Home panel bar (the button row under the search field)

   The home page answers "what do I want to watch". This row answers "what is
   this instance doing right now" -- queue, activity, library, storage,
   system -- without turning the page into a dashboard: a row of buttons, and
   ONE panel below whose content depends on the button.

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
  if (!bar || !body) return;              // classic home page — nothing to do

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

  const PREF_KEY = "home_panel";
  const LS_KEY = "mf-home-panel";         // fallback when nobody is logged in
  const POLL_MS = 20000;

  let panels = [];                        // [{id,label,badge,icon,builtin}]
  let active = "";                        // "" = closed
  let poller = null;
  let inFlight = null;                    // AbortController of the open fetch

  // ------------------------------------------------------------- persistence
  function storeActive(id) {
    try { localStorage.setItem(LS_KEY, id); } catch (e) { /* private mode */ }
    // Fire-and-forget, same as the appearance settings in base.html: the panel
    // is already open locally, so a failed save must never interrupt anything.
    // A 401 (auth on, session expired) is a normal outcome and stays silent.
    fetch("/api/user/preferences", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [PREF_KEY]: id }),
    }).then(function (r) {
      if (r.ok && window._USER_PREFS) window._USER_PREFS[PREF_KEY] = id;
    }).catch(function () { /* offline — the local copy is enough */ });
  }

  function storedActive() {
    const prefs = window._USER_PREFS || {};
    if (prefs[PREF_KEY]) return prefs[PREF_KEY];
    try { return localStorage.getItem(LS_KEY) || ""; } catch (e) { return ""; }
  }

  // --------------------------------------------------------------- rendering
  function iconSvg(path) {
    if (!path) return "";
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="' + esc(path) + '"/></svg>';
  }

  function renderBar() {
    if (!panels.length) { bar.style.display = "none"; return; }
    bar.style.display = "";
    bar.innerHTML = panels.map(function (p) {
      const on = p.id === active;
      return '<button type="button" class="mf-segmented-btn hp-btn' + (on ? " active" : "") +
        '" data-panel="' + esc(p.id) + '" aria-expanded="' + (on ? "true" : "false") +
        '" aria-controls="homePanelBody">' + iconSvg(p.icon) +
        '<span>' + esc(text(p.label_key, p.label)) + '</span>' +
        (p.badge > 0 ? '<span class="mf-facet">' + esc(String(p.badge)) + '</span>' : '') +
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
        // The server's answer wins over the local one: it already dropped any
        // panel this account may no longer see.
        const wanted = (data && data.active) || storedActive();
        const allowed = panels.some(function (p) { return p.id === wanted; });
        setActive(allowed ? wanted : "", true);
      })
      .catch(function () { bar.style.display = "none"; });
  }

  // ------------------------------------------------------------ interaction
  function setActive(id, silent) {
    active = id || "";
    renderBar();
    stopPoll();
    if (!active) {
      body.innerHTML = "";
      body.hidden = true;
      if (!silent) storeActive("");
      return;
    }
    body.hidden = false;
    if (!silent) storeActive(active);
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
