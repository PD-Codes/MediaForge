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
  // this list leaves them out entirely (see renderRows()) -- they are
  // arranged and hidden on the Dashboard itself. "new", "popular", "movies"
  // and "because" have no Dashboard-card equivalent and keep full reorder
  // controls. The list is still needed to know which rows to leave out, and
  // "All in one page" (no cards at all) puts every one of them back.
  const DASH_WIDGET_ROWS = ["continue", "library", "watchlist", "upcoming", "gaps"];
  function isDashWidget(row) { return DASH_WIDGET_ROWS.indexOf(row) !== -1; }

  // "All in one page" (home_dash_enabled="all") renders the classic
  // button-bar Dashboard instead of the drag/resize card grid (see
  // templates/index.html) -- there is no card to drag these rows onto, so
  // "position and size are set on the Dashboard tab" is not true there. In
  // that mode every row (including the ones that are ALSO a Dashboard card
  // elsewhere) gets the full reorder controls back, same as before the drag
  // grid existed. Two different elements carry the answer depending on
  // scope: the per-account select only rendered for scope="user", the
  // instance default only rendered for scope="global" (both in
  // _start_page_form.html).
  function dashModeEl(scope) {
    return document.getElementById(scope === "user" ? "spDashMode-user" : "dashModeDefault");
  }
  function isAllInOne(scope) {
    const el = dashModeEl(scope);
    return !!el && el.value === "all";
  }
  // "Discover only" (home_dash_enabled="0") -- there is no Dashboard tab at
  // all in this mode, so its five widget rows (continue/library/watchlist/
  // upcoming/gaps) have nowhere to appear. Listing them here anyway used to
  // show a "Dashboard widgets" group full of toggles for a tab the account
  // can never open.
  function isDiscoverOnly(scope) {
    const el = dashModeEl(scope);
    return !!el && el.value === "0";
  }
  // The list step()/rowHtml's up/down buttons move within: the Discover-only
  // subsequence normally, or every row once there is no Dashboard grid to
  // split them from.
  function reorderGroup(scope) {
    return isAllInOne(scope) ? state[scope].order.slice() : discoverOrder(scope);
  }

  // scope -> {order, hidden, limit, sourcesOff, typesOff, rowMeta}
  const state = {};
  let catalogue = null;                    // the one /api/home-feed/sources answer

  function txt(key, fallback) { return I18N[key] || fallback; }

  function toast(message) {
    if (typeof showToast === "function") showToast(message);
  }

  // -------------------------------------------- Dashboard columns and cards
  //
  // The Dashboard is 2 or 3 columns; a card picks a column and a position
  // inside it. This list is the keyboard/settings twin of dragging a card
  // directly on the Dashboard -- both write the SAME preference
  // (home_dash_card_layout, ordered "<card id>:<column>" pairs), so they are
  // two views onto one state rather than two competing ones.
  //
  // scope="user" always (see spCardOrderRow-user's scope=='user' guard in
  // _start_page_form.html): this is a per-account arrangement, there is no
  // instance-default equivalent to arrange for a fresh account.
  //
  // The rows are read from the LIVE Dashboard DOM, not from a catalogue --
  // whatever home_panels.js has rendered IS the current arrangement, and the
  // card's own rendered title is always the correctly translated one, module
  // cards included. The price is that this only works when opened from the
  // home page itself; renderCardOrderList() says so rather than showing an
  // empty list on Profile/Settings.
  let cardCols = null;                      // [[{key, label}], ...] per column
  let orderDragging = null;

  function columnCount() {
    const root = document.getElementById("homeDashColumns");
    const n = root ? parseInt(root.dataset.cols, 10) : 0;
    if (n >= 2 && n <= 3) return n;
    return String((window._USER_PREFS || {}).home_dash_columns || "") === "2" ? 2 : 3;
  }

  function orderRowHtml(key, label, index, total, colSelect) {
    return '<div class="mf-order-row" draggable="true" data-row="' + escapeHtml(key) + '">' +
      '<span class="mf-order-handle" title="' + escapeHtml(txt("drag", "Drag to reorder")) +
      '" aria-hidden="true"><svg viewBox="0 0 20 20" width="16" height="16" fill="currentColor">' +
      '<circle cx="7" cy="5" r="1.5"/><circle cx="13" cy="5" r="1.5"/><circle cx="7" cy="10" r="1.5"/>' +
      '<circle cx="13" cy="10" r="1.5"/><circle cx="7" cy="15" r="1.5"/><circle cx="13" cy="15" r="1.5"/>' +
      "</svg></span>" +
      '<span class="mf-order-label">' + escapeHtml(label) + "</span>" +
      (colSelect || "") +
      '<span class="mf-order-actions">' +
      '<button type="button" class="mf-order-btn" data-order-move="up" data-order-key="' + escapeHtml(key) + '"' +
      (index === 0 ? " disabled" : "") + ' title="' + escapeHtml(txt("move_up", "Move up")) + '">' +
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" ' +
      'stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg></button>' +
      '<button type="button" class="mf-order-btn" data-order-move="down" data-order-key="' + escapeHtml(key) + '"' +
      (index === total - 1 ? " disabled" : "") + ' title="' + escapeHtml(txt("move_down", "Move down")) + '">' +
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" ' +
      'stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></button>' +
      "</span></div>";
  }

  function colSelectHtml(key, col, total) {
    let opts = "";
    for (let c = 0; c < total; c++) {
      opts += '<option value="' + c + '"' + (c === col ? " selected" : "") + ">" +
        escapeHtml(txt("column_n", "Column {}").replace("{}", String(c + 1))) + "</option>";
    }
    return '<select class="mf-order-col" data-col-for="' + escapeHtml(key) + '" aria-label="' +
      escapeHtml(txt("column", "Column")) + '">' + opts + "</select>";
  }

  /** `group` is the live array backing the rows currently in the DOM --
      dragging mutates it in place. Dropping onto a row in ANOTHER column
      moves the card there, which is the same thing the column dropdown
      does, so the two never disagree. */
  function attachOrderDnd(el, group, onChange) {
    el.addEventListener("dragstart", function (ev) {
      orderDragging = { key: el.dataset.row, group: group };
      el.classList.add("dragging");
      try { ev.dataTransfer.effectAllowed = "move"; } catch (e) { /* older browsers */ }
    });
    el.addEventListener("dragend", function () {
      orderDragging = null;
      el.classList.remove("dragging");
      document.querySelectorAll(".mf-order-row.drag-over").forEach(function (r) {
        r.classList.remove("drag-over");
      });
    });
    el.addEventListener("dragover", function (ev) {
      ev.preventDefault();
      if (orderDragging && orderDragging.key !== el.dataset.row) el.classList.add("drag-over");
    });
    el.addEventListener("dragleave", function () { el.classList.remove("drag-over"); });
    el.addEventListener("drop", function (ev) {
      ev.preventDefault();
      el.classList.remove("drag-over");
      if (!orderDragging) return;
      const from = orderDragging.group.findIndex(function (it) { return it.key === orderDragging.key; });
      const to = group.findIndex(function (it) { return it.key === el.dataset.row; });
      if (from === -1 || to === -1) return;
      if (orderDragging.group === group && from === to) return;
      const moved = orderDragging.group.splice(from, 1)[0];
      group.splice(to, 0, moved);
      onChange();
    });
  }

  function moveOrderItem(group, key, delta, onChange) {
    const i = group.findIndex(function (it) { return it.key === key; });
    const j = i + delta;
    if (i === -1 || j < 0 || j >= group.length) return;
    const moved = group.splice(i, 1)[0];
    group.splice(j, 0, moved);
    onChange();
  }

  function currentCardCols() {
    const total = columnCount();
    const out = [];
    for (let c = 0; c < total; c++) {
      const colEl = document.getElementById("dashCol-" + c);
      out.push(colEl ? Array.prototype.map.call(
        colEl.querySelectorAll(".dash-card-flow[data-card]"),
        function (el) {
          const span = el.querySelector(".dash-card-head span");
          return { key: el.dataset.card, label: span ? span.textContent : el.dataset.card };
        }) : []);
    }
    return out;
  }

  function groupOf(key) {
    return cardCols.filter(function (g) {
      return g.some(function (it) { return it.key === key; });
    })[0];
  }

  function renderCardOrderList() {
    const list = document.getElementById("spCardOrder-user");
    if (!list) return;
    if (!cardCols) cardCols = currentCardCols();
    const total = cardCols.length;
    let html = "";
    let any = false;
    cardCols.forEach(function (group, c) {
      html += '<div class="sp-sublabel">' +
        escapeHtml(txt("column_n", "Column {}").replace("{}", String(c + 1))) + "</div>";
      if (!group.length) {
        html += '<div class="mf-order-hint">' + escapeHtml(txt("column_empty", "Empty")) + "</div>";
        return;
      }
      any = true;
      html += group.map(function (it, i) {
        return orderRowHtml(it.key, it.label, i, group.length, colSelectHtml(it.key, c, total));
      }).join("");
    });
    if (any) {
      list.innerHTML = html;
    } else if (document.getElementById("homeDashColumns")) {
      // Genuinely nothing on the board (every card hidden/removed).
      list.innerHTML = '<div class="mf-order-hint">' +
        escapeHtml(txt("dash_add_empty", "Everything is already on your board.")) + "</div>";
    } else {
      // This list is read straight off the live Dashboard DOM (see
      // currentCardCols() above) -- on any page other than the home page
      // itself (Profile, Settings) that DOM does not exist, so it always
      // came out empty here regardless of what is on the account's
      // Dashboard. Say so instead of implying there is nothing to arrange.
      list.innerHTML = '<div class="mf-order-hint">' +
        escapeHtml(txt("dash_order_home_only",
          "Open this from the home page itself (the \"Customise this page\" button) to reorder cards.")) +
        "</div>";
    }
    list.querySelectorAll(".mf-order-row").forEach(function (el) {
      const group = groupOf(el.dataset.row);
      if (group) attachOrderDnd(el, group, function () { renderCardOrderList(); saveCardOrder(); });
    });
    list.querySelectorAll("[data-order-move]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const group = groupOf(btn.dataset.orderKey);
        if (!group) return;
        moveOrderItem(group, btn.dataset.orderKey, btn.dataset.orderMove === "up" ? -1 : 1,
          function () { renderCardOrderList(); saveCardOrder(); });
      });
    });
    list.querySelectorAll(".mf-order-col").forEach(function (sel) {
      sel.addEventListener("change", function () {
        const key = sel.dataset.colFor;
        const from = groupOf(key);
        const to = cardCols[Number(sel.value)];
        if (!from || !to || from === to) return;
        to.push(from.splice(from.findIndex(function (it) { return it.key === key; }), 1)[0]);
        renderCardOrderList();
        saveCardOrder();
      });
    });
  }

  function saveCardOrder() {
    const pairs = [];
    cardCols.forEach(function (group, c) {
      group.forEach(function (it) { pairs.push(it.key + ":" + c); });
    });
    const value = pairs.join(",");
    fetch("/api/user/preferences", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ home_dash_card_layout: value }),
    }).then(function (r) {
      if (!r.ok) throw new Error("save failed");
      if (window._USER_PREFS) window._USER_PREFS.home_dash_card_layout = value;
      // Apply straight away when this was opened from the home page, so the
      // list and the board never show two different arrangements.
      if (typeof window.mfDashApplyCardLayout === "function") window.mfDashApplyCardLayout(value);
      toast(txt("layout_saved", "Saved"));
    }).catch(function () {
      toast(txt("save_failed", "Could not be saved"));
    });
  }

  // "Start on" only means anything with two SEPARATE tabs to choose a first
  // one from -- "Discover only" ("0") has no Dashboard tab, "All in one
  // page" ("all") stacks both with no tab switch at all. Top-level (not
  // nested in bind()) for the same reason as syncOrderRows() below: both
  // bind()'s dashMode change handler and load()'s initial populate call it,
  // and a copy nested inside bind()'s closure is invisible from load() --
  // that mismatch is what silently emptied the Rows/order lists before
  // (ReferenceError inside load()'s forEach, aborting the rest of that
  // scope's iteration before renderRows()/renderChecks() ever ran).
  function syncStartTabRow(scope) {
    const dashMode = document.getElementById("spDashMode-" + scope);
    const startTabRow = document.getElementById("spStartTabRow-" + scope);
    if (startTabRow) startTabRow.style.display = (dashMode && dashMode.value !== "") ? "none" : "";
  }

  // The column count and the card list only mean anything with a Dashboard
  // to arrange ("Discover only" has none, "All in one page" renders the
  // older button-bar dashboard instead) and for the "user" scope
  // (scope="global" never renders them, see _start_page_form.html).
  // Top-level (not nested in bind()) so both bind()'s change handlers and
  // load()'s initial populate can call it.
  function syncOrderRows(scope) {
    const dashMode = document.getElementById("spDashMode-" + scope);
    const hide = !!(dashMode && (dashMode.value === "0" || dashMode.value === "all"));
    const colRow = document.getElementById("spDashColumnsRow-" + scope);
    const cardRow = document.getElementById("spCardOrderRow-" + scope);
    if (colRow) colRow.style.display = hide ? "none" : "";
    if (cardRow) cardRow.style.display = hide ? "none" : "";
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
    let html;
    if (isAllInOne(scope)) {
      // No Dashboard grid to arrange these on in this layout -- every row
      // is a plain, fully reorderable item again, one flat list.
      const all = state[scope].order.slice();
      html = all.map(function (row, i) { return rowHtml(scope, row, i, all.length, true); }).join("");
    } else if (isDiscoverOnly(scope)) {
      // No Dashboard tab exists to show the widget rows on -- drop the
      // group entirely rather than listing toggles with no visible effect.
      const disc = discoverOrder(scope);
      html = disc.map(function (row, i) { return rowHtml(scope, row, i, disc.length, true); }).join("");
    } else {
      // Column Dashboard (anything that is neither "Discover only" nor
      // "All in one page"): the five widget rows are CARDS there, shown and
      // hidden on the Dashboard itself (a card's "x", and the Add-widget
      // menu to bring it back). Listing the same five as checkboxes here
      // was a second, competing switch for one thing. The server keeps
      // their data flowing in this mode whatever the stored hidden list
      // says -- see feed_effective_config() in routes/browse.py.
      const disc = discoverOrder(scope);
      html = disc.map(function (row, i) { return rowHtml(scope, row, i, disc.length, true); }).join("");
    }
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

  // Up/down buttons only render for reorderable rows (Discover rows always,
  // every row once isAllInOne() -- see rowHtml's reorderable flag), so the
  // swap target is the neighbour within that same reorderable group
  // (reorderGroup), not the physically adjacent id in the full order array
  // -- in the split view that could be an unrelated Dashboard-widget row
  // sitting between two Discover rows.
  function step(scope, row, delta) {
    const order = state[scope].order;
    const group = reorderGroup(scope);
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

    // Dashboard/Discover arrangement + which tab opens first (only relevant
    // for the "tabs" arrangement) -- both only exist for scope="user" (see
    // _start_page_form.html) and both take effect on the next load, same as
    // the Layout select above: home_dash_enabled decides what app.py even
    // renders into the page, and the start tab is only read once by
    // home_2_1.js's wireTabs() on load.
    const dashMode = document.getElementById("spDashMode-" + scope);
    const startTab = document.getElementById("spStartTab-" + scope);
    if (dashMode) {
      dashMode.addEventListener("change", function () {
        const value = ["", "0", "all"].indexOf(dashMode.value) === -1 ? "" : dashMode.value;
        syncStartTabRow(scope);
        renderRows(scope);          // the Discover/Dashboard-widget split depends on this
        fetch("/api/user/preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ home_dash_enabled: value }),
        }).then(function (r) {
          if (!r.ok) throw new Error("save failed");
          toast(txt("layout_saved", "Saved — reload the home page"));
        }).catch(function () {
          toast(txt("save_failed", "Could not be saved"));
        });
      });
    }
    // scope="global" never renders the order rows at all (see
    // _start_page_form.html's scope=='user' guard), so this is a no-op
    // there. syncOrderRows() itself lives at top level (see above bind())
    // so load() can call it too.
    if (dashMode) dashMode.addEventListener("change", function () { syncOrderRows(scope); });
    // Column count. Needs a reload to take effect: the columns themselves
    // are server-rendered (index.html) because they are the drop targets
    // home_panels.js places cards into.
    const dashCols = document.getElementById("spDashColumns-" + scope);
    if (dashCols) {
      dashCols.addEventListener("change", function () {
        const value = dashCols.value === "2" ? "2" : "3";
        fetch("/api/user/preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ home_dash_columns: value }),
        }).then(function (r) {
          if (!r.ok) throw new Error("save failed");
          if (window._USER_PREFS) window._USER_PREFS.home_dash_columns = value;
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

    // "Could be for you" visibility -- scope="user" only (see
    // _start_page_form.html), hero banner and rail as two independent
    // checkboxes. Applied immediately through window.mfForyouSetHeroHidden/
    // window.mfForyouSetHidden rather than "reload the home page": the row
    // is pure client-side rendering, so there is nothing a reload would do
    // that toggling it in place does not.
    const foryouHeroHidden = document.getElementById("spForyouHeroHidden-" + scope);
    if (foryouHeroHidden) {
      foryouHeroHidden.addEventListener("change", function () {
        const value = foryouHeroHidden.checked ? "0" : "1";
        fetch("/api/user/preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ foryou_hero_hidden: value }),
        }).then(function (r) {
          if (!r.ok) throw new Error("save failed");
          if (window._USER_PREFS) window._USER_PREFS.foryou_hero_hidden = value;
          if (typeof window.mfForyouSetHeroHidden === "function") {
            window.mfForyouSetHeroHidden(value === "1");
          }
          toast(txt("saved", "Saved"));
        }).catch(function () {
          foryouHeroHidden.checked = !foryouHeroHidden.checked;
          toast(txt("save_failed", "Could not be saved"));
        });
      });
    }
    const foryouHidden = document.getElementById("spForyouHidden-" + scope);
    if (foryouHidden) {
      foryouHidden.addEventListener("change", function () {
        const value = foryouHidden.checked ? "0" : "1";
        fetch("/api/user/preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ foryou_hidden: value }),
        }).then(function (r) {
          if (!r.ok) throw new Error("save failed");
          if (window._USER_PREFS) window._USER_PREFS.foryou_hidden = value;
          if (typeof window.mfForyouSetHidden === "function") {
            window.mfForyouSetHidden(value === "1");
          }
          toast(txt("saved", "Saved"));
        }).catch(function () {
          foryouHidden.checked = !foryouHidden.checked;
          toast(txt("save_failed", "Could not be saved"));
        });
      });
    }

    // The instance-default twin of the dashMode select above -- unscoped id
    // (#dashModeDefault, only rendered for scope="global"), saved through
    // static/settings.js's own saveDashModeDefault(). This listener only
    // adds the row-list re-render; it does not duplicate the save.
    const dashModeDefaultEl = document.getElementById("dashModeDefault");
    if (dashModeDefaultEl) {
      dashModeDefaultEl.addEventListener("change", function () { renderRows(scope); });
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
      const dashModeEl = document.getElementById("spDashMode-" + scope);
      if (dashModeEl) {
        const stored = String((window._USER_PREFS || {}).home_dash_enabled || "");
        dashModeEl.value = ["0", "all"].indexOf(stored) === -1 ? "" : stored;
        // Single source of truth for the row's visibility rule -- see
        // syncStartTabRow() above, also used by dashMode's own change
        // handler.
        syncStartTabRow(scope);
      }
      const startTabEl = document.getElementById("spStartTab-" + scope);
      if (startTabEl) {
        const stored = String((window._USER_PREFS || {}).home_tab || "");
        startTabEl.value = stored === "disc" ? "disc" : "";
      }
      const dashColsEl = document.getElementById("spDashColumns-" + scope);
      if (dashColsEl) {
        dashColsEl.value = String((window._USER_PREFS || {}).home_dash_columns || "") === "2" ? "2" : "3";
      }
      syncOrderRows(scope);
      if (scope === "user") {
        // Re-read fresh every time the modal opens -- the account may have
        // dragged a card directly on the Dashboard since the last time this
        // form was populated.
        cardCols = null;
        renderCardOrderList();
      }
      const foryouHeroHiddenEl = document.getElementById("spForyouHeroHidden-" + scope);
      if (foryouHeroHiddenEl) {
        foryouHeroHiddenEl.checked = String((window._USER_PREFS || {}).foryou_hero_hidden || "") !== "1";
      }
      const foryouHiddenEl = document.getElementById("spForyouHidden-" + scope);
      if (foryouHiddenEl) {
        foryouHiddenEl.checked = String((window._USER_PREFS || {}).foryou_hidden || "") !== "1";
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
