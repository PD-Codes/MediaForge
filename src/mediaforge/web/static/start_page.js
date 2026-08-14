/* ===================================================================
   MediaForge — Start Page settings (Settings → Start Page, and the
   "Customise" panel on the home page itself).

   Two scopes, one implementation:

     "user"    what I see              -> POST /api/user/preferences
                                          ("home_feed_layout")
     "global"  what a new account sees -> PUT  /api/settings (admin only)

   The user scope exists because the rows are personal -- one account's
   "Continue watching" says nothing to another. The global scope exists
   because the instance owner still needs to decide what a fresh account
   starts with (e.g. no calendar row on an install without a TMDB key).
   A user who never touches anything follows the global default forever;
   the moment they change something, only the parts they changed become
   their own -- see feed_effective_config() in routes/browse.py.

   The row list is built from /api/home-feed/sources rather than a
   hardcoded list, so a module's source and any future row show up here
   without this file knowing about them.
   =================================================================== */

(function () {
  const I18N = window.__STARTPAGE_I18N || {};
  const ROW_LABELS = I18N.rows || {};
  const HINTS = I18N.hints || {};
  const CARD_CHOICES = ["10", "20", "30", "40", "60"];
  // The project's single escaper (mf_escape.js, loaded globally from
  // base.html). app.js's escapeHtml is not available on the settings page.
  const escapeHtml = window.mfEscape;

  // Rows that are now ALSO a Dashboard card (static/home_panels.js's PLACE
  // table: continue/watchlist/newlib/gaps/upcoming -- "library" here is that
  // table's "newlib"). Their position is set by dragging the card itself, so
  // this list drops their drag handle/up-down controls and keeps only the
  // enable/disable checkbox, which is still the one true way to stop
  // MediaForge collecting that row's data (see the group hint below). "new",
  // "popular", "movies" and "because" have no Dashboard-card equivalent and
  // keep full reorder controls.
  const DASH_WIDGET_ROWS = ["continue", "library", "watchlist", "upcoming", "gaps"];
  function isDashWidget(row) { return DASH_WIDGET_ROWS.indexOf(row) !== -1; }

  // scope -> {order, hidden, limit, sourcesOff, typesOff, rowMeta}
  const state = {};
  let catalogue = null;                    // the one /api/home-feed/sources answer

  function txt(key, fallback) { return I18N[key] || fallback; }

  function toast(message) {
    if (typeof showToast === "function") showToast(message);
  }

  // ------------------------------------------------------------ rendering
  function rowHtml(scope, row, index, total, reorderable) {
    const meta = state[scope].rowMeta[row] || {};
    const on = state[scope].hidden.indexOf(row) === -1;
    const hint = HINTS[meta.hint] || "";
    const hintHtml = meta.link
      ? '<a href="' + escapeHtml(meta.link) + '">' + escapeHtml(hint) + "</a>"
      : escapeHtml(hint);
    const handle = reorderable
      ? '<span class="mf-order-handle" title="' + escapeHtml(txt("drag", "Drag to reorder")) +
        '" aria-hidden="true"><svg viewBox="0 0 20 20" width="16" height="16" fill="currentColor">' +
        '<circle cx="7" cy="5" r="1.5"/><circle cx="13" cy="5" r="1.5"/><circle cx="7" cy="10" r="1.5"/>' +
        '<circle cx="13" cy="10" r="1.5"/><circle cx="7" cy="15" r="1.5"/><circle cx="13" cy="15" r="1.5"/>' +
        "</svg></span>"
      : "";
    const actions = reorderable
      ? '<span class="mf-order-actions">' +
        '<button type="button" class="mf-order-btn" data-move="up" data-row="' + escapeHtml(row) + '"' +
        (index === 0 ? " disabled" : "") + ' title="' + escapeHtml(txt("move_up", "Move up")) + '">' +
        '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg></button>' +
        '<button type="button" class="mf-order-btn" data-move="down" data-row="' + escapeHtml(row) + '"' +
        (index === total - 1 ? " disabled" : "") + ' title="' + escapeHtml(txt("move_down", "Move down")) + '">' +
        '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></button>' +
        "</span>"
      : "";
    return '<div class="mf-order-row' + (on ? "" : " is-off") + '"' +
      (reorderable ? ' draggable="true"' : "") + ' data-row="' + escapeHtml(row) + '">' +
      handle +
      '<input type="checkbox" class="chb-main" data-toggle="' + escapeHtml(row) + '"' +
      (on ? " checked" : "") + ' aria-label="' + escapeHtml(txt("show_row", "Show this row")) + '">' +
      '<span class="mf-order-label">' + escapeHtml(ROW_LABELS[row] || row) +
      (hint ? '<small class="mf-order-hint">' + hintHtml + "</small>" : "") + "</span>" +
      actions + "</div>";
  }

  // Discover rows only (no Dashboard-card twin), preserving relative order --
  // this is the subset that still reorders in this list.
  function discoverOrder(scope) {
    return state[scope].order.filter(function (row) { return !isDashWidget(row); });
  }

  function renderRows(scope) {
    const list = document.getElementById("spRows-" + scope);
    if (!list) return;
    const disc = discoverOrder(scope);
    const dash = state[scope].order.filter(isDashWidget);
    let html = '<div class="sp-sublabel">' + escapeHtml(txt("group_discover", "Discover rows")) + "</div>";
    html += disc.map(function (row, i) { return rowHtml(scope, row, i, disc.length, true); }).join("");
    html += '<div class="sp-sublabel">' + escapeHtml(txt("group_dashboard", "Dashboard widgets")) + "</div>";
    html += '<div class="mf-order-hint mf-order-group-hint">' +
      escapeHtml(txt("group_dashboard_hint",
        "Position and size are set on the Dashboard tab. Turning one off here also stops collecting its data.")) +
      "</div>";
    html += dash.map(function (row, i) { return rowHtml(scope, row, i, dash.length, false); }).join("");
    list.innerHTML = html;
    list.querySelectorAll('.mf-order-row[draggable="true"]').forEach(function (el) { attachDnd(scope, el); });

    const note = document.getElementById("spState-" + scope);
    if (note) {
      note.textContent = state[scope].overridden && state[scope].overridden.length
        ? txt("using_own", "You changed this — the instance default no longer applies")
        : txt("using_default", "Following the instance default");
    }
  }

  function renderChecks(scope) {
    const sourceWrap = document.getElementById("spSources-" + scope);
    if (sourceWrap && catalogue) {
      sourceWrap.innerHTML = catalogue.sources.map(function (src) {
        const off = state[scope].sourcesOff.indexOf(src.id) !== -1;
        return '<label class="settings-checkbox-row"><input type="checkbox" class="chb-main" ' +
          'data-source="' + escapeHtml(src.id) + '"' + (off ? "" : " checked") +
          (src.enabled ? "" : " disabled") + "><span>" + escapeHtml(src.label) +
          (src.enabled ? "" : " · " + escapeHtml((window.__HOME_I18N || {}).off || "off")) +
          "</span></label>";
      }).join("");
    }
    const typeWrap = document.getElementById("spTypes-" + scope);
    if (typeWrap) {
      const home = window.__HOME_I18N || {};
      const types = [["series", home.series || "Series"], ["movies", home.movies || "Movies"],
                     ["adult", "18+"]];
      typeWrap.innerHTML = types.map(function (pair) {
        const off = state[scope].typesOff.indexOf(pair[0]) !== -1;
        return '<label class="settings-checkbox-row"><input type="checkbox" class="chb-main" ' +
          'data-type="' + pair[0] + '"' + (off ? "" : " checked") + "><span>" +
          escapeHtml(pair[1]) + "</span></label>";
      }).join("");
    }
  }

  // ------------------------------------------------------------ drag & drop
  let dragging = null;

  function attachDnd(scope, el) {
    el.addEventListener("dragstart", function (ev) {
      dragging = { scope: scope, row: el.dataset.row };
      el.classList.add("dragging");
      try { ev.dataTransfer.effectAllowed = "move"; ev.dataTransfer.setData("text/plain", el.dataset.row); }
      catch (e) { /* older browsers */ }
    });
    el.addEventListener("dragend", function () {
      dragging = null;
      el.classList.remove("dragging");
      document.querySelectorAll(".mf-order-row.drag-over").forEach(function (r) {
        r.classList.remove("drag-over");
      });
    });
    el.addEventListener("dragover", function (ev) {
      ev.preventDefault();
      if (dragging && dragging.scope === scope && dragging.row !== el.dataset.row) {
        el.classList.add("drag-over");
      }
    });
    el.addEventListener("dragleave", function () { el.classList.remove("drag-over"); });
    el.addEventListener("drop", function (ev) {
      ev.preventDefault();
      el.classList.remove("drag-over");
      if (!dragging || dragging.scope !== scope) return;
      move(scope, dragging.row, el.dataset.row);
    });
  }

  function move(scope, row, before) {
    const order = state[scope].order;
    const from = order.indexOf(row);
    const to = order.indexOf(before);
    if (from === -1 || to === -1 || from === to) return;
    order.splice(from, 1);
    order.splice(to, 0, row);
    renderRows(scope);
    save(scope);
  }

  // Up/down buttons only ever target Discover rows (Dashboard-widget rows
  // don't render them -- see rowHtml's reorderable flag), so the swap target
  // is the neighbour within the DISCOVER subsequence, not the physically
  // adjacent id in the full order array -- that could be an unrelated
  // Dashboard-widget row sitting between two Discover rows.
  function step(scope, row, delta) {
    const order = state[scope].order;
    const group = discoverOrder(scope);
    const gi = group.indexOf(row);
    const ti = gi + delta;
    if (gi === -1 || ti < 0 || ti >= group.length) return;
    const neighbour = group[ti];
    const from = order.indexOf(row);
    if (from === -1) return;
    order.splice(from, 1);
    const at = order.indexOf(neighbour);
    order.splice(delta < 0 ? at : at + 1, 0, row);
    renderRows(scope);
    save(scope);
  }

  function toggleRow(scope, row) {
    const hidden = state[scope].hidden;
    const at = hidden.indexOf(row);
    if (at === -1) hidden.push(row); else hidden.splice(at, 1);
    renderRows(scope);
    save(scope);
  }

  // ------------------------------------------------------------ saving
  async function save(scope) {
    const s = state[scope];
    try {
      let resp;
      if (scope === "user") {
        s.overridden = ["order", "hidden", "limit"];
        resp = await fetch("/api/user/preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            home_feed_layout: "o:" + s.order.join(",") + ";h:" + s.hidden.join(",") + ";n:" + s.limit,
          }),
        });
      } else {
        resp = await fetch("/api/settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            home_rows_order: s.order.join(","),
            home_rows_hidden: s.hidden.join(","),
            home_cards_per_row: String(s.limit),
            home_default_sources_off: s.sourcesOff.join(","),
            home_default_types_off: s.typesOff.join(","),
          }),
        });
      }
      const data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || resp.status);
      // The home page keeps its own hour-long copy of the feed; a layout it
      // has already drawn would otherwise stay wrong until that expires.
      if (typeof window.reloadHomeFeed === "function") window.reloadHomeFeed();
      const note = document.getElementById("spState-" + scope);
      if (note && scope === "user") note.textContent = txt("using_own", "You changed this");
    } catch (err) {
      toast(txt("save_failed", "Could not be saved") + ": " + err.message);
    }
  }

  async function reset(scope) {
    try {
      if (scope === "user") {
        await fetch("/api/user/preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ home_feed_layout: "", home_feed_filters: "" }),
        });
      } else {
        await fetch("/api/settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            home_rows_order: "", home_rows_hidden: "", home_cards_per_row: "30",
            home_default_sources_off: "", home_default_types_off: "adult",
          }),
        });
      }
      try { localStorage.removeItem("mf-home-filters"); } catch (e) { /* private mode */ }
      catalogue = null;
      await load();
      if (typeof window.reloadHomeFeed === "function") window.reloadHomeFeed();
      toast(txt("reset_done", "Reset"));
    } catch (err) {
      toast(txt("save_failed", "Could not be saved") + ": " + err.message);
    }
  }

  /** Show the stored kids-mode state. The PIN itself is never read back --
      it is a secret the server encrypts, and a form that renders it would
      hand it to anyone who can open the settings page. */
  async function loadKidsSettings(scope, kidsOn, kidsFsk, done) {
    try {
      const cfg = (await (await fetch("/api/home-feed/sources")).json()).config || {};
      if (kidsOn) kidsOn.checked = !!cfg.kids_switched_on;
      if (kidsFsk) kidsFsk.value = String(cfg.kids_max_fsk || "6");
    } catch (e) { /* leave the defaults */ }
    if (typeof done === "function") done();
  }

  // ------------------------------------------------------------ wiring
  function bind(scope) {
    const list = document.getElementById("spRows-" + scope);
    if (list) {
      list.addEventListener("click", function (ev) {
        const btn = ev.target.closest("[data-move]");
        if (btn) { step(scope, btn.dataset.row, btn.dataset.move === "up" ? -1 : 1); return; }
      });
      list.addEventListener("change", function (ev) {
        const box = ev.target.closest("[data-toggle]");
        if (box) toggleRow(scope, box.dataset.toggle);
      });
    }
    const cards = document.getElementById("spCards-" + scope);
    if (cards) {
      cards.addEventListener("change", function () {
        state[scope].limit = CARD_CHOICES.indexOf(cards.value) === -1 ? 30 : parseInt(cards.value, 10);
        save(scope);
      });
    }
    const sourceWrap = document.getElementById("spSources-" + scope);
    if (sourceWrap) {
      sourceWrap.addEventListener("change", function (ev) {
        const box = ev.target.closest("[data-source]");
        if (!box) return;
        const id = box.dataset.source;
        const at = state[scope].sourcesOff.indexOf(id);
        if (box.checked && at !== -1) state[scope].sourcesOff.splice(at, 1);
        if (!box.checked && at === -1) state[scope].sourcesOff.push(id);
        save(scope);
      });
    }
    const typeWrap = document.getElementById("spTypes-" + scope);
    if (typeWrap) {
      typeWrap.addEventListener("change", function (ev) {
        const box = ev.target.closest("[data-type]");
        if (!box) return;
        const id = box.dataset.type;
        const at = state[scope].typesOff.indexOf(id);
        if (box.checked && at !== -1) state[scope].typesOff.splice(at, 1);
        if (!box.checked && at === -1) state[scope].typesOff.push(id);
        save(scope);
      });
    }
    // Kids mode (admin scope only). The PIN saves on change rather than on
    // every keystroke: written character by character it would leave a trail
    // of half-PINs in the settings table, each of which would be the valid
    // one for a moment.
    const kidsOn = document.getElementById("spKidsOn-" + scope);
    const kidsFsk = document.getElementById("spKidsFsk-" + scope);
    const pin = document.getElementById("spKidsPin-" + scope);
    if (kidsOn || pin) {
      const state = document.getElementById("spKidsState-" + scope);

      function saveKids(patch) {
        fetch("/api/settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(patch),
        }).then(async function (r) {
          const body = await r.json().catch(function () { return {}; });
          if (!r.ok) throw new Error(body.error || String(r.status));
          toast(txt("saved", "Saved"));
          refreshKidsState();
        }).catch(function (err) {
          toast(txt("save_failed", "Could not be saved") + ": " + err.message);
        });
      }

      /** Say plainly whether the button will actually appear. "On" plus no
          PIN is the state people get stuck in, and without this line the
          home page simply shows nothing and looks broken. */
      async function refreshKidsState() {
        if (!state) return;
        try {
          const cfg = (await (await fetch("/api/home-feed/sources")).json()).config || {};
          state.textContent = cfg.kids_enabled
            ? txt("kids_ready", "Kids mode is available on the home page.")
            : txt("kids_needs_pin", "Switch it on and set a PIN — the button stays hidden until both are done.");
        } catch (e) { /* leave the hint empty rather than guess */ }
      }

      if (kidsOn) {
        kidsOn.addEventListener("change", function () {
          saveKids({ home_kids_enabled: kidsOn.checked ? "1" : "0" });
        });
      }
      if (kidsFsk) {
        kidsFsk.addEventListener("change", function () {
          saveKids({ home_kids_max_fsk: kidsFsk.value });
        });
      }
      if (pin) {
        pin.addEventListener("change", function () {
          saveKids({ home_kids_pin: pin.value || "" });
        });
      }
      loadKidsSettings(scope, kidsOn, kidsFsk, refreshKidsState);
    }
    // Which layout THIS account sees. Deliberately not part of save(scope):
    // the rows/filters live in one packed home_feed_layout string, while this
    // is its own preference key that the server reads before the page is even
    // built (app.py's index()). Changing it means a reload, not a repaint.
    const layout = document.getElementById("spLayout-" + scope);
    if (layout) {
      layout.addEventListener("change", function () {
        const value = ["", "0", "1"].indexOf(layout.value) === -1 ? "" : layout.value;
        layout.disabled = true;
        fetch("/api/user/preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ new_home: value }),
        }).then(function (r) {
          if (!r.ok) throw new Error("save failed");
          if (window._USER_PREFS) window._USER_PREFS.new_home = value;
          toast(txt("layout_saved", "Saved — reload the home page"));
        }).catch(function () {
          toast(txt("save_failed", "Could not be saved"));
        }).then(function () { layout.disabled = false; });
      });
    }

    // Dashboard tab on/off + which tab opens first -- both only exist for
    // scope="user" (see _start_page_form.html) and both take effect on the
    // next load, same as the Layout select above: dash_enabled decides what
    // app.py even renders into the page, and the start tab is only read once
    // by home_2_1.js's wireTabs() on load.
    const dashEnabled = document.getElementById("spDashEnabled-" + scope);
    const startTabRow = document.getElementById("spStartTabRow-" + scope);
    const startTab = document.getElementById("spStartTab-" + scope);
    function syncStartTabRow() {
      if (startTabRow) startTabRow.style.display = (dashEnabled && !dashEnabled.checked) ? "none" : "";
    }
    if (dashEnabled) {
      dashEnabled.addEventListener("change", function () {
        syncStartTabRow();
        fetch("/api/user/preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ home_dash_enabled: dashEnabled.checked ? "1" : "0" }),
        }).then(function (r) {
          if (!r.ok) throw new Error("save failed");
          toast(txt("layout_saved", "Saved — reload the home page"));
        }).catch(function () {
          toast(txt("save_failed", "Could not be saved"));
        });
      });
    }
    if (startTab) {
      startTab.addEventListener("change", function () {
        const value = ["", "disc"].indexOf(startTab.value) === -1 ? "" : startTab.value;
        fetch("/api/user/preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ home_tab: value }),
        }).then(function (r) {
          if (!r.ok) throw new Error("save failed");
          toast(txt("layout_saved", "Saved — reload the home page"));
        }).catch(function () {
          toast(txt("save_failed", "Could not be saved"));
        });
      });
    }

    const resetBtn = document.getElementById("spReset-" + scope);
    if (resetBtn) resetBtn.addEventListener("click", function () { reset(scope); });
  }

  async function load() {
    const scopes = [];
    document.querySelectorAll(".sp-form[data-sp-scope]").forEach(function (el) {
      scopes.push(el.dataset.spScope);
    });
    if (!scopes.length) return;

    if (!catalogue) {
      const resp = await fetch("/api/home-feed/sources");
      catalogue = await resp.json();
    }
    const cfg = catalogue.config || {};
    const def = catalogue.defaults || {};
    const meta = {};
    (catalogue.rows || []).forEach(function (r) { meta[r.id] = r; });

    scopes.forEach(function (scope) {
      const fromUser = scope === "user";
      state[scope] = {
        order: (fromUser ? cfg.order : def.order || cfg.order || []).slice(),
        hidden: (fromUser ? cfg.hidden : def.hidden || []).slice(),
        limit: fromUser ? (cfg.limit || 30) : (def.limit || 30),
        sourcesOff: (def.sources_off || []).slice(),
        typesOff: (def.types_off || []).slice(),
        overridden: fromUser ? (cfg.overridden || []) : [],
        rowMeta: meta,
      };
      const cards = document.getElementById("spCards-" + scope);
      if (cards) cards.value = String(state[scope].limit);
      // Straight from window._USER_PREFS rather than from the feed catalogue:
      // the layout is not part of the feed config, and "" (follow the
      // instance default) has to survive as its own value here -- reading a
      // missing key as "0" would silently pin every account to the classic
      // page the first time they opened this form.
      const layoutSel = document.getElementById("spLayout-" + scope);
      if (layoutSel) {
        const stored = String((window._USER_PREFS || {}).new_home || "");
        layoutSel.value = ["0", "1"].indexOf(stored) === -1 ? "" : stored;
      }
      // Same source of truth as the layout select above -- window._USER_PREFS,
      // not the feed catalogue, which knows nothing about either of these.
      const dashEnabledEl = document.getElementById("spDashEnabled-" + scope);
      if (dashEnabledEl) {
        dashEnabledEl.checked = String((window._USER_PREFS || {}).home_dash_enabled || "") !== "0";
        const row = document.getElementById("spStartTabRow-" + scope);
        if (row) row.style.display = dashEnabledEl.checked ? "" : "none";
      }
      const startTabEl = document.getElementById("spStartTab-" + scope);
      if (startTabEl) {
        const stored = String((window._USER_PREFS || {}).home_tab || "");
        startTabEl.value = stored === "disc" ? "disc" : "";
      }
      renderRows(scope);
      renderChecks(scope);
    });
  }

  function init() {
    const forms = document.querySelectorAll(".sp-form[data-sp-scope]");
    if (!forms.length) return;
    forms.forEach(function (el) { bind(el.dataset.spScope); });
    load();
  }

  window.MFStartPage = { init: init, reload: function () { catalogue = null; load(); } };

  // ------------------------------------------------------------- the modal
  // Opened from the "Customise" button on the new layout and from "Your home
  // page" on the classic one. The controls inside are the Settings tab's,
  // rendered into a modal here because /settings redirects a non-admin and
  // these settings are not admin business.
  //
  // Lives here rather than in home_feed.js (where it started) because that
  // file only loads on the new layout -- which left the classic page with a
  // button calling a function that did not exist.
  window.openStartPageModal = function () {
    const overlay = document.getElementById("startPageOverlay");
    if (!overlay) return;
    overlay.style.display = "block";
    if (window.MFScrollLock && typeof window.MFScrollLock.lock === "function") {
      window.MFScrollLock.lock();
    } else {
      document.body.style.overflow = "hidden";
    }
    if (window.MFStartPage) window.MFStartPage.reload();
  };

  window.closeStartPageModal = function () {
    const overlay = document.getElementById("startPageOverlay");
    if (!overlay) return;
    overlay.style.display = "none";
    if (window.MFScrollLock && typeof window.MFScrollLock.unlock === "function") {
      window.MFScrollLock.unlock();
    } else {
      document.body.style.overflow = "";
    }
  };

  window.closeStartPageModalOutside = function (ev) {
    if (ev.target === document.getElementById("startPageOverlay")) window.closeStartPageModal();
  };

  document.addEventListener("keydown", function (ev) {
    const overlay = document.getElementById("startPageOverlay");
    if (ev.key === "Escape" && overlay && overlay.style.display === "block") {
      window.closeStartPageModal();
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
