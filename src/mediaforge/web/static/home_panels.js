/* ===================================================================
   MediaForge — Dashboard sections

   The Dashboard tab answers "what is this instance doing right now": the
   queue, what is missing, what happened, how full the disks are, which
   sources answer. Every card lives in one of four fixed, collapsible
   sections (MediaForge / System / Statistics / Modules).

   It used to be a segmented button bar with ONE panel below it, because six
   widgets meant six pollers (read the docstring at the top of
   routes/home_panels.py). The cards are all visible now, so that cost had to
   go somewhere else rather than away:

     * everything loads in ONE request -- GET /api/home-panels/all,
     * and ONE poll refreshes only the three panels whose data actually
       moves (queue, activity, system) with ?only=, and only while the
       Dashboard tab is the open tab and the browser tab is visible.

   ponytail: one interval for all three moving panels rather than a per-panel
   cadence. Queue progress therefore ticks at the same 20 s as the activity
   list, which is slower than the queue hub. Give the queue its own faster
   handle if the progress bar ever needs to feel live -- the batch endpoint
   already takes an ?only= list, so that is a second mfPoll and nothing else.

   Every panel arrives in the SAME shape ({stats, items, link, empty}) and
   goes through one renderer, whether it is built in or comes from a module.
   Built-in labels arrive as i18n KEYS and are resolved here against
   window.__HOME_I18N, which index.html renders through Flask-Babel: babel.cfg
   extracts Jinja templates only, so a string written in Python source would
   never reach a catalogue. A MODULE panel sends ready-made text instead -- it
   owns its own catalogue -- so every field accepts both and the key wins.

   Three cards are NOT panels: gaps, sources and today's calendar are built
   from data static/home_feed.js already fetched for the poster rows, so they
   cost no extra request. home_feed.js hands them over through
   window.mfHomeDashFeed().

   Three more cards are personal lists rather than instance state (continue
   watching, watchlist, new in the library). They come from the same
   window.mfHomeDashFeed() hand-over; the poster rows that used to show them
   below are switched off in home_feed.js (DASH_CARD_ROWS), the same way gaps
   and today's calendar were.

   ARRANGEMENT: no engine. The Dashboard is 2 or 3 columns (index.html
   renders them from home_dash_columns); a card picks a column and a
   position inside it, and one drag gesture sets both. That is the whole
   model -- there is nothing to resize, nothing to pack and nothing to
   recompute on a viewport change, which is what the two predecessors did
   and where their bugs lived:

     * a hand-rolled absolute-position grid (drag/resize/collision/
       compaction on a `home_dash_layout` preference), and
     * four named sections (`home_dash_section_layout`/`_order`), whose
       names decided what a card was "about" -- not a layout question.

   Neither preference is read any more; both are still accepted by
   web/db/ui_prefs.py so a module writing one does not start getting 400s.

   Nothing from either side is ever inserted as markup: every string goes
   through mfEscape().
   =================================================================== */

(function () {
  // The Dashboard is 2 or 3 columns (index.html renders them from the
  // account's home_dash_columns). A card picks a column and a position
  // inside it -- that is the whole arrangement model. Two predecessors were
  // removed: a free-position pixel grid (it fought every async refresh and
  // every viewport change) and four named sections (the names decided what a
  // card was "about", which is not a layout question).
  const colsRoot = document.getElementById("homeDashColumns");
  if (!colsRoot) return;                 // classic home page — nothing to do
  const COL_COUNT = Math.max(1, parseInt(colsRoot.dataset.cols, 10) || 3);

  const I18N = window.__HOME_I18N || {};
  function HT(key) { return I18N[key] || key; }
  const esc = window.mfEscape || function (s) { return String(s == null ? "" : s); };

  // key + args -> translated text, falling back to the plain text a module
  // sent. The placeholder is "{}" and is filled left to right: Flask-Babel
  // installs newstyle gettext, which turns a "%s" in a template into "{}" in
  // the rendered catalogue string -- so "{}" is what actually arrives here.
  function text(key, fallback, args) {
    let out = key ? HT(key) : (fallback || "");
    (args || []).forEach(function (arg) { out = out.replace("{}", arg); });
    return out;
  }

  // Named actions a card may trigger. The queue is a modal that base.html
  // ships on every page (openQueueHub in queue.js), not a route -- a link to
  // /queue 404s. The map is here and not in the payload so a module panel can
  // ask for "queue" and nothing else.
  const ACTIONS = {
    queue: function () {
      if (typeof window.openQueueHub === "function") window.openQueueHub("all");
      else if (typeof window.openQueueModal === "function") window.openQueueModal();
    },
  };

  // Built-in cards that get a loading skeleton before their data arrives
  // (see loadingBody() at the bottom). Module panels are not listed: their
  // ids are only known once /api/home-panels/all answers.
  const SKELETON_IDS = [
    "queue", "continue", "watchlist", "newlib", "gaps", "activity",
    "library", "storage", "sources", "upcoming", "system", "wrapped",
  ];

  // Card headings that read better than the panel's own button label did.
  // "Queue" was a button in a row of buttons; as a card heading next to a
  // progress bar it is "Running now".
  const TITLES = {
    queue: "dash_now",
    activity: "dash_activity",
    library: "dash_overview",
  };

  // Heading icons for the cards this file builds itself. The panel cards get
  // theirs from the payload; without these three the sections mixed cards with an
  // icon and cards without, which reads as two different kinds of card.
  const OWN_ICONS = {
    // antenna / broadcast
    sources: "M4.93 19.07a10 10 0 0 1 0-14.14M19.07 4.93a10 10 0 0 1 0 14.14"
             + "M7.76 16.24a6 6 0 0 1 0-8.48M16.24 7.76a6 6 0 0 1 0 8.48",
    // calendar
    upcoming: "M19 4H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2z"
              + "M16 2v4M8 2v4M3 10h18",
    // puzzle piece
    gaps: "M4 8h3a2 2 0 0 0 2-2V4a2 2 0 1 1 4 0v2a2 2 0 0 0 2 2h3v3a2 2 0 1 0 0 4v3"
          + "H4a1 1 0 0 1-1-1v-3a2 2 0 1 0 0-4z",
    // play in a circle
    "continue": "M10 8l6 4-6 4V8zM12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z",
    // bookmark
    watchlist: "M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z",
    // stacked discs
    newlib: "M4 7l8-4 8 4-8 4-8-4zM4 12l8 4 8-4M4 17l8 4 8-4",
  };

  // Only these three change while somebody looks at the page. Library,
  // storage and the monthly review are answers to slow questions -- polling
  // them would mean a library scan's worth of work every 20 s for a number
  // that moves once a day.
  const POLLED = ["queue", "activity", "system"];
  const POLL_MS = 20000;

  let poller = null;
  let feedSources = [];
  let feedPersonal = {};

  // --------------------------------------------------------------- rendering
  function iconSvg(path) {
    if (!path) return "";
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="' + esc(path) + '"/></svg>';
  }

  const PREFS = window._USER_PREFS || {};

  // Base ids the account closed with a card's own "x" (see card()'s close
  // button below). Only ever holds base ids -- an extra instance ("id.2") is
  // simply never auto-recreated by a poll/load in the first place, so
  // removing one needs no suppression, only a DOM remove.
  const HIDDEN = new Set(String(PREFS.home_dash_hidden || "").split(",").filter(Boolean));

  // Third-party dashboard-widget cards (register_thirdparty's
  // dashboard_widget_template, see index.html) are already server-rendered
  // into their section before this script runs. Soft-hide any the account
  // has closed. Their content is opaque, page-load-only Jinja markup with no
  // re-fetch story, so "closed" always means display:none, never DOM removal
  // -- see the .dash-close handler and addWidget() further down.
  document.querySelectorAll('.dash-card[data-widget-kind="thirdparty-template"]').forEach(function (el) {
    if (HIDDEN.has(el.dataset.card)) {
      el.style.display = "none";
      el.dataset.dashHidden = "1";
    }
  });

  let hiddenSaveTimer = null;
  function saveHidden() {
    // Debounced: closing several cards in a
    // row should not fire one request per click.
    if (hiddenSaveTimer) clearTimeout(hiddenSaveTimer);
    hiddenSaveTimer = setTimeout(function () {
      hiddenSaveTimer = null;
      if (typeof window.mfSaveUserPref === "function") {
        window.mfSaveUserPref({ home_dash_hidden: Array.from(HIDDEN).join(",") });
      }
    }, 400);
  }

  /** True (and removes any leftover DOM element) when `id` is a base id the
      account has closed -- called first thing by every renderer that can be
      invoked on a poll/load, so a hidden card is not just removed once but
      never comes back on its own. Never true for an instance id ("id.2"):
      only base ids are ever added to HIDDEN. */
  function isHidden(id) {
    if (!HIDDEN.has(id)) return false;
    const old = document.getElementById("dashCard-" + id);
    if (old) old.remove();
    return true;
  }

  // -------------------------------------------------------- section layout
  //
  // Which column a card STARTS in, before the account has dragged anything.
  // Base id only (an instance suffix like ".2" is stripped first), so every
  // instance of a multi:true card shares its base id's column. Grouped by
  // how a card is read, not by subsystem:
  //   0  what is happening / what wants doing -- read line by line
  //   1  your own material, and module widgets
  //   2  reference figures -- glanced at, not read
  // Anything not listed (a module panel, a thirdparty dashboard widget)
  // starts in the middle column: that is the one group this app cannot
  // enumerate in advance, so it is a correct default, not a special case.
  const COLUMN_OF = {
    queue: 0, gaps: 0, activity: 0,
    "continue": 1, watchlist: 1, newlib: 1, upcoming: 1,
    library: 2, storage: 2, system: 2, sources: 2, wrapped: 2,
  };
  const DEFAULT_COLUMN = 1;

  // ------------------------------------------------------- client pagination
  //
  // Sections mode only (a flowed card now grows with an unbounded list
  // instead of scrolling it -- see .dash-card-flow's dropped overflow:auto
  // in index.css -- so a long list needs pages instead of a scrollbar). Page
  // index is plain runtime state, not a preference -- nobody expects
  // "page 2" to survive a reload, only the current session.
  const PAGE_SIZE = 5;
  const _pageState = {};

  /** Slice `rows` (an array of already-rendered <li> strings) to one page and
      return {rowsHtml, pagerHtml} to splice into a card's body. `maxTotal`
      (optional) caps the list before paging, e.g. "5 per page, 10 total". A
      list that already fits on one page (after the cap) gets no pager at
      all -- nothing to page through. */
  function pagedRows(cardId, rows, maxTotal) {
    const list = maxTotal ? rows.slice(0, maxTotal) : rows;
    if (list.length <= PAGE_SIZE) {
      return { rowsHtml: list.join(""), pagerHtml: "" };
    }
    const totalPages = Math.ceil(list.length / PAGE_SIZE);
    const page = Math.max(0, Math.min(_pageState[cardId] || 0, totalPages - 1));
    const rowsHtml = list.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE).join("");
    const pagerHtml = '<div class="dash-pager" data-pager="' + esc(cardId) + '">' +
      '<button type="button" class="dash-pager-btn" data-page-dir="-1"' +
      (page === 0 ? " disabled" : "") + ' aria-label="' + esc(HT("dash_page_prev")) + '">‹</button>' +
      '<span class="dash-pager-count">' + (page + 1) + ' / ' + totalPages + '</span>' +
      '<button type="button" class="dash-pager-btn" data-page-dir="1"' +
      (page === totalPages - 1 ? " disabled" : "") + ' aria-label="' + esc(HT("dash_page_next")) + '">›</button>' +
      '</div>';
    return { rowsHtml: rowsHtml, pagerHtml: pagerHtml };
  }

  /** The account's own arrangement: ordered "<card id>:<column>" pairs from
      home_dash_card_layout. The LIST ORDER is the render order within a
      column, so one preference carries both facts. "" (nothing dragged yet)
      means every card sits in its COLUMN_OF default in the built-in order --
      the same "absent = built-in" convention home_dash_hidden uses, which is
      what lets a card added in a later release need no migration. Parsed
      once and cached; saveCardLayout() is the only thing that invalidates
      it. */
  let _cardLayout = null;
  function cardLayout() {
    if (_cardLayout) return _cardLayout;
    _cardLayout = new Map();
    String(PREFS.home_dash_card_layout || "").split(",").filter(Boolean).forEach(function (part, i) {
      const bits = part.split(":");
      const col = parseInt(bits[1], 10);
      if (bits.length !== 2 || !bits[0] || !isFinite(col) || col < 0) return;
      _cardLayout.set(bits[0], { col: col, index: i });
    });
    return _cardLayout;
  }

  /** Which column a card is in right now. A stored column beyond the current
      count folds into the LAST visible column rather than disappearing:
      switching 3 -> 2 columns must not lose a card, and switching back must
      find it where it was -- which is why the fold happens here, on read,
      and is never written back. */
  function colFor(id) {
    const base = id.split(".")[0];
    const saved = cardLayout().get(base);
    const want = saved ? saved.col : (COLUMN_OF.hasOwnProperty(base) ? COLUMN_OF[base] : DEFAULT_COLUMN);
    return Math.min(Math.max(0, want), COL_COUNT - 1);
  }

  function colEl(index) {
    return document.getElementById("dashCol-" + index);
  }

  /** Where a card belongs inside its column, respecting a saved order --
      called only when the card is first created (renderOneCard); a poll
      re-render never moves an existing element. A card with no saved
      position appends after everything that has one. */
  function colInsertRef(col, id) {
    const mine = cardLayout().get(id.split(".")[0]);
    if (!mine) return null;                // no saved position -> append
    let best = null;
    let bestIndex = Infinity;
    Array.prototype.forEach.call(col.querySelectorAll(".dash-card-flow[data-card]"), function (el) {
      const other = cardLayout().get(el.dataset.card.split(".")[0]);
      if (other && other.index > mine.index && other.index < bestIndex) {
        best = el;
        bestIndex = other.index;
      }
    });
    return best;
  }

  /** Persist the current DOM column/order of every card. Called after a drag
      settles. Walks the DOM rather than patching the cached map, so what is
      saved is always the arrangement the user is visibly looking at.

      ponytail: writes the FOLDED column (what is on screen) for every card,
      so dragging anything while in 2-column mode flattens a card that was
      parked in column 3. Acceptable: the fold is only visible because the
      account chose 2 columns. Store the pre-fold column alongside if parking
      a card "off screen" ever has to survive a drag. */
  function saveCardLayout() {
    const pairs = [];
    for (let c = 0; c < COL_COUNT; c++) {
      const col = colEl(c);
      if (!col) continue;
      Array.prototype.forEach.call(col.querySelectorAll(".dash-card-flow[data-card]"), function (el) {
        pairs.push(el.dataset.card + ":" + c);
      });
    }
    const value = pairs.join(",");
    _cardLayout = null;                    // re-derive from what we just saved
    if (typeof window.mfSaveUserPref === "function") {
      window.mfSaveUserPref({ home_dash_card_layout: value });
    }
    PREFS.home_dash_card_layout = value;
  }

  /** The Dashboard's "nothing needs your attention" line is owned by
      home_2_1.js and has to be re-checked whenever a card appears or
      disappears -- which is every render, every close and every drop. */
  function syncDashEmpty() {
    if (typeof window.mfHomeSyncDashEmpty === "function") window.mfHomeSyncDashEmpty();
  }

  /** Create a card on first sight, replace its contents on every later one.
      The whole card is the drag handle (see the colsRoot drag listeners
      further down) -- there is nothing to resize onto in a flow layout, so
      there is no grip and no resize corner, only the "x". */
  function renderOneCard(id, head, body, foot) {
    let el = document.getElementById("dashCard-" + id);
    let content;
    if (!el) {
      el = document.createElement("section");
      el.id = "dashCard-" + id;
      el.className = "dash-card dash-card-flow";
      el.dataset.card = id;
      el.draggable = !isLocked();

      content = document.createElement("div");
      content.className = "dash-card-content";
      el.appendChild(content);

      const close = document.createElement("button");
      close.type = "button";
      close.className = "dash-tool dash-close";
      close.title = HT("dash_remove");
      close.setAttribute("aria-label", HT("dash_remove"));
      close.textContent = "×";
      el.appendChild(close);

      const target = colEl(colFor(id));
      if (target) target.insertBefore(el, colInsertRef(target, id));
    } else {
      content = el.querySelector(".dash-card-content");
    }
    content.innerHTML = head +
      '<div class="dash-card-body">' + body + '</div>' +
      (foot ? '<div class="dash-card-foot">' + foot + '</div>' : '');
    syncDashEmpty();
    return el;
  }

  /** Public entry point every renderer (built-in or module) calls. `id` is
      always a base id (no ".") UNLESS the caller is materialising a fresh
      extra instance via its own `overrideId` (see renderPanel() and the feed
      renderers) -- in that case `id` already carries the ".N" suffix and this
      function behaves exactly like renderOneCard().

      For a base id, this ALSO copies the same head/body/foot into every
      sibling instance element already on the board (ids "id.2", "id.3", ...)
      so a normal data refresh -- a poll, a feed update -- keeps every
      instance in step without any renderer needing to know instances exist.

      ponytail: there is no per-instance configuration anywhere in this
      system -- a module's view() takes no parameters, so every instance of a
      type always shows IDENTICAL data, only its position/size on the board
      differs. Propagating the same render output to every sibling is
      therefore correct, not a shortcut. Upgrade path if a widget ever needs
      per-instance data: pass the instance id into view() and cache per-
      instance payloads instead of copying one shared render. */
  function card(id, head, body, foot) {
    const el = renderOneCard(id, head, body, foot);
    if (id.indexOf(".") === -1) {
      // document-scoped: a sibling instance lives in whichever container
      // renderOneCard() put it in.
      document.querySelectorAll('[data-card^="' + id + '."]').forEach(function (sib) {
        renderOneCard(sib.dataset.card, head, body, foot);
      });
    }
    return el;
  }

  /** A card heading: icon, title, and an optional quiet note or "open the
      list" link on the right. The move handle used to live inside this
      markup too; it is now built once by card() itself and laid out on top
      via CSS (see .dash-close in index.css) so it survives every re-render --
      see card()'s own comment. */
  function head(title, note, icon, link) {
    const right = link
      ? '<a class="dash-card-note" href="' + esc(link.href) + '">' +
        esc(link.label) + ' ›</a>'
      : (note ? '<span class="dash-card-note">' + esc(note) + '</span>' : '');
    return '<h3 class="dash-card-head">' + (icon ? iconSvg(icon) : "") +
      '<span>' + esc(title) + '</span>' + right + '</h3>';
  }

  /** The one renderer for every panel, built-in or module. Same payload
      shape, same markup -- a second renderer is a second thing to keep in
      step with routes/home_panels.py. Returns {body, foot} rather than one
      string: `foot` is the trailing "open the list" link, kept out of the
      scrollable body so it stays pinned to the card's bottom edge (see
      card()) instead of scrolling out of view with a long item list. */
  // Sections-mode-only pagination per built-in panel id -- see the design
  // note above pagedRows(). Absent id = not paginated, same as before (the
  // card keeps scrolling instead). "maxTotal: undefined" pages the full
  // server-sent list; a number caps it first ("5 per page, N total").
  const PANEL_PAGINATE = {
    activity: { maxTotal: 10 },
    library: { maxTotal: 10 },
    storage: {},
    wrapped: { maxTotal: 10 },
  };

  // ------------------------------------------------------------- charts
  //
  // Two panels answer a question that is a SHARE, not a number: how full the
  // fullest volume is, and how much of the queue is moving. Both are drawn
  // from the payload those panels already send -- no new endpoint, no new
  // field -- and the numbers stay underneath as the legend, because "87 %"
  // is still the thing you read out loud.
  //
  // Inline SVG rather than static/mf_charts.js: that module draws axed,
  // multi-series charts for the statistics page, which is a different job
  // from one ring and one bar.

  /** A stat out of a panel payload, by its i18n key. */
  function statBy(data, key) {
    return (data.stats || []).filter(function (s) { return s.label_key === key; })[0] || null;
  }
  function statNum(data, key) {
    const s = statBy(data, key);
    const n = s ? parseInt(String(s.value).replace(/[^0-9-]/g, ""), 10) : NaN;
    return isFinite(n) ? n : 0;
  }

  const TONE_COLOR = {
    err: "var(--error)", warn: "var(--warning)", ok: "var(--success)", "": "var(--accent)",
  };

  /** Progress ring. `pct` is clamped, `tone` picks the colour from the same
      three-tone vocabulary the payload already uses for stats and items. */
  function donut(pct, tone, caption) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    const r = 26, circ = 2 * Math.PI * r;
    const color = TONE_COLOR[tone] || TONE_COLOR[""];
    return '<div class="hp-donut"><svg viewBox="0 0 68 68" width="68" height="68" aria-hidden="true">' +
      '<circle cx="34" cy="34" r="' + r + '" fill="none" stroke="var(--bg-hover)" stroke-width="7"/>' +
      '<circle cx="34" cy="34" r="' + r + '" fill="none" stroke="' + color + '" stroke-width="7" ' +
      'stroke-linecap="round" stroke-dasharray="' + circ.toFixed(1) + '" stroke-dashoffset="' +
      (circ * (1 - p / 100)).toFixed(1) + '" transform="rotate(-90 34 34)"/>' +
      '<text x="34" y="34" text-anchor="middle" dominant-baseline="central" fill="var(--text-primary)" ' +
      'font-size="15" font-weight="700">' + esc(Math.round(p) + "%") + '</text></svg>' +
      '<span class="hp-donut-cap">' + esc(caption) + '</span></div>';
  }

  /** Proportional bar plus a legend. `parts` is [{value, label, color}];
      a part with value 0 keeps its legend entry (0 waiting is an answer)
      but takes no width. */
  function stackedBar(parts) {
    const total = parts.reduce(function (sum, p) { return sum + p.value; }, 0);
    const segs = total
      ? parts.filter(function (p) { return p.value > 0; }).map(function (p) {
          return '<i style="width:' + ((p.value / total) * 100).toFixed(2) + "%;background:" + p.color + '"></i>';
        }).join("")
      : "";
    return '<div class="hp-stack">' + segs + "</div>" +
      '<div class="hp-legend">' + parts.map(function (p) {
        return '<span><i style="background:' + p.color + '"></i>' + esc(p.value) + " " + esc(p.label) + "</span>";
      }).join("") + "</div>";
  }

  /** Which panels get a chart in place of their stat strip. A panel not
      listed here (every module panel, for one) renders exactly as before --
      this is an addition to two built-ins, not a new payload contract. */
  const PANEL_CHART = {
    storage: function (data) {
      const s = statBy(data, "hp_fullest");
      if (!s) return "";
      // WHICH volume is the fullest is the half of this answer the caption
      // used to leave out. Derived from the items the payload already
      // carries (one per volume, each with its own percent), so the server
      // needs no extra field for it.
      const worst = (data.items || []).reduce(function (best, it) {
        return (best && Number(best.percent) >= Number(it.percent)) ? best : it;
      }, null);
      const caption = text("hp_fullest", s.label) +
        (worst && worst.title ? ": " + worst.title : "");
      return '<div class="hp-chart">' +
        donut(parseInt(s.value, 10), s.tone, caption) + "</div>";
    },
    queue: function (data) {
      const running = statNum(data, "hp_running");
      const waiting = statNum(data, "hp_waiting");
      const failed = statNum(data, "hp_failed");
      const paused = statBy(data, "hp_paused");
      if (!running && !waiting && !failed) return "";
      const bar = stackedBar([
        { value: running, label: text("hp_running", ""), color: "var(--accent)" },
        { value: waiting, label: text("hp_waiting", ""), color: "var(--text-muted)" },
        { value: failed, label: text("hp_failed", ""), color: "var(--error)" },
      ]);
      // "Is the queue running?" is a state, not a share -- it stays a word,
      // next to the bar rather than inside it.
      const state = paused
        ? '<span class="hp-state' + (paused.tone ? " is-" + esc(paused.tone) : "") + '">' +
          esc(text(paused.label_key, paused.label)) + ": " +
          esc(text(paused.value_key, paused.value)) + "</span>"
        : "";
      return '<div class="hp-chart is-wide">' + bar + state + "</div>";
    },
  };

  function panelBody(data, cardId) {
    if (data.error) {
      return { body: '<div class="hp-error">' + esc(HT("panel_unavailable")) + '</div>', foot: "" };
    }
    let html = "";
    const chart = PANEL_CHART[data.id] ? PANEL_CHART[data.id](data) : "";
    // A charted panel drops its stat strip: the chart's own legend already
    // carries every figure that was in it, and showing both is the same
    // number twice.
    if (chart) {
      html += chart;
    } else if ((data.stats || []).length) {
      html += '<div class="hp-stats">' + data.stats.map(function (s) {
        return '<div class="hp-stat' + (s.tone ? " is-" + esc(s.tone) : "") + '">' +
          '<span class="hp-stat-value">' + esc(text(s.value_key, s.value)) + '</span>' +
          '<span class="hp-stat-label">' + esc(text(s.label_key, s.label)) + '</span></div>';
      }).join("") + '</div>';
    }
    if ((data.items || []).length) {
      const rows = data.items.map(function (item) {
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
      });
      const pageOpts = cardId && PANEL_PAGINATE.hasOwnProperty(data.id) ? PANEL_PAGINATE[data.id] : null;
      const paged = pageOpts ? pagedRows(cardId, rows, pageOpts.maxTotal) : { rowsHtml: rows.join(""), pagerHtml: "" };
      html += '<ul class="hp-items">' + paged.rowsHtml + '</ul>' + paged.pagerHtml;
    }
    if (!(data.stats || []).length && !(data.items || []).length) {
      html += '<div class="mf-empty hp-empty">' +
        esc(text(data.empty_key, data.empty) || HT("panel_empty")) + '</div>';
    }
    let foot = "";
    if (data.link && (data.link.href || data.link.action)) {
      const label = esc(text(data.link.label_key, data.link.label) || HT("panel_open")) + ' ›';
      foot = data.link.action
        ? '<button type="button" class="hp-more hp-more-btn" data-action="' +
          esc(data.link.action) + '">' + label + '</button>'
        : '<a class="hp-more" href="' + esc(data.link.href) + '">' + label + '</a>';
    }
    return { body: html, foot: foot };
  }

  function renderPanel(data, overrideId) {
    const id = overrideId || data.id;
    if (isHidden(id)) return;
    // Title/icon always key off the real registered type (data.id), even
    // when an extra instance is being materialised under a ".N" DOM id.
    const title = TITLES[data.id] ? HT(TITLES[data.id]) : text(data.label_key, data.label);
    const rendered = panelBody(data, id);
    card(id, head(title, "", data.icon), rendered.body, rendered.foot);
  }

  // ------------------------------------------------------------- feed cards
  //
  // Gaps, sources and today's calendar come from what home_feed.js already
  // fetched. They are cards like any other but not panels: nothing on the
  // server renders them, so they cannot go through /api/home-panels/all.

  function pill(label, tone) {
    return '<span class="dash-pill' + (tone ? " is-" + tone : "") + '">' +
      esc(label) + '</span>';
  }

  function renderGaps(overrideId) {
    const id = overrideId || "gaps";
    if (isHidden(id)) return;
    const gaps = feedPersonal.gaps || [];
    if (!gaps.length) {
      // Nothing missing is worth saying once, not worth a card every visit.
      const old = document.getElementById("dashCard-" + id);
      if (old) { old.remove(); syncDashEmpty(); }
      return;
    }
    const total = gaps.reduce(function (sum, g) {
      return sum + (parseInt(g.missing_count, 10) || 0);
    }, 0);
    const rows = gaps.map(function (it) {
      const slot = (it.missing || [])[0] || "";
      const art = it.poster_url
        ? '<img class="dash-gap-art" src="' + esc(it.poster_url) + '" alt="" loading="lazy">'
        : '<span class="dash-gap-art is-faux" style="' + esc(fauxArt(it.title)) + '"></span>';
      return '<li class="dash-gap">' + art +
        '<span class="dash-gap-m"><b>' + esc(it.title) + '</b>' +
        '<small>' + esc(slot) + '</small></span>' +
        '<button type="button" class="dash-btn" data-gap-search="' + esc(it.title) + '">' +
        esc(HT("dash_gap_fetch")) + '</button>' +
        '<button type="button" class="dash-gap-hide" data-gap-ignore="' +
        esc(it.folder || it.title) + '" title="' + esc(HT("gap_ignore")) +
        '" aria-label="' + esc(HT("gap_ignore")) + '">×</button></li>';
    });
    const paged = pagedRows(id, rows);
    card(id,
         head(HT("dash_gaps"), HT("dash_gap_count").replace("{}", String(total)),
              OWN_ICONS.gaps),
         '<ul class="dash-gaps">' + paged.rowsHtml + '</ul>' + paged.pagerHtml,
         '<a class="hp-more" href="/stats">' + esc(HT("dash_gaps_all")) + ' ›</a>');
  }

  /** Same hashing home_feed.js and the Library page use for placeholder art,
      so a title has the same colour everywhere. */
  function fauxArt(name) {
    let hash = 0;
    const value = String(name || "");
    for (let i = 0; i < value.length; i++) hash = (hash * 31 + value.charCodeAt(i)) >>> 0;
    const h1 = hash % 360, h2 = (h1 + 48) % 360;
    return "background:linear-gradient(155deg,hsl(" + h1 + ",55%,22%),hsl(" + h2 + ",55%,14%))";
  }

  function renderSources(overrideId) {
    const id = overrideId || "sources";
    if (isHidden(id)) return;
    const list = feedSources.filter(function (s) { return s.enabled; });
    if (!list.length) {
      card(id, head(HT("sources"), "", OWN_ICONS.sources),
           '<div class="mf-empty hp-empty">' + esc(HT("dash_empty_sources")) + '</div>');
      return;
    }
    const up = list.filter(function (s) { return !s.error; }).length;
    // One dot per source, before the list: a single red dot among green ones
    // is findable at a glance, six "online" pills are not.
    const dots = '<div class="dash-src-dots">' + list.map(function (s) {
      return '<span class="dash-src-dot' + (s.error ? " is-down" : "") + '" title="' +
        esc(s.label || s.id) + '"></span>';
    }).join("") + "</div>";
    const rows = list.map(function (s) {
      const down = !!s.error;
      return '<div class="dash-src"><span>' + esc(s.label || s.id) + '</span>' +
        pill(down ? HT("offline") : HT("dash_online"), down ? "warn" : "ok") + '</div>';
    }).join("");
    card(id,
         head(HT("sources"),
              HT("dash_sources_online").replace("{}", String(up)).replace("{}", String(list.length)),
              OWN_ICONS.sources),
         dots + '<div class="dash-srcs">' + rows + '</div>');
  }

  function renderUpcoming(overrideId) {
    const id = overrideId || "upcoming";
    if (isHidden(id)) return;
    // Today only: the card answers "is something on tonight", and the poster
    // row (and /calendar) still answer "what is coming".
    const today = new Date();
    const iso = today.getFullYear() + "-" +
      String(today.getMonth() + 1).padStart(2, "0") + "-" +
      String(today.getDate()).padStart(2, "0");
    const list = (feedPersonal.upcoming || []).filter(function (ev) {
      return String(ev.air_date || "").slice(0, 10) === iso;
    });
    let body;
    if (list.length) {
      const rows = list.map(function (ev) {
        const slot = ev.is_movie ? HT("movie")
          : (ev.season ? "S" + ev.season + "E" + (ev.episode || "") : "");
        return '<div class="dash-src"><span>' + esc(ev.title) + '</span>' +
          (slot ? pill(slot, "") : "") + '</div>';
      });
      const paged = pagedRows(id, rows, 10);
      body = '<div class="dash-srcs">' + paged.rowsHtml + '</div>' + paged.pagerHtml;
    } else {
      body = '<div class="mf-empty hp-empty">' + esc(HT("dash_empty_today")) + '</div>';
    }
    card(id, head(HT("dash_today"), "", OWN_ICONS.upcoming), body,
         '<a class="hp-more" href="/calendar">' + esc(HT("dash_open_calendar")) + ' ›</a>');
  }

  // ------------------------------------------------------ personal lists
  //
  // Continue watching / watchlist / new in the library. These used to be
  // poster RAILS below (home_feed.js renderPersonal), which meant
  // half a screen of artwork for three short lists. As cards they are the
  // same row shape the gaps card already uses: thumbnail, title, sub-line,
  // and one button on the right.

  function thumb(item) {
    return item.poster_url
      ? '<img class="dash-gap-art" src="' + esc(item.poster_url) + '" alt="" loading="lazy">'
      : '<span class="dash-gap-art is-faux" style="' + esc(fauxArt(item.title)) + '"></span>';
  }

  /** One list row. `percent` null = no progress bar. */
  function listRow(item, sub, percent, button) {
    return '<li class="dash-gap">' + thumb(item) +
      '<span class="dash-gap-m"><b>' + esc(item.title) + '</b>' +
      '<small>' + esc(sub) + '</small>' +
      (percent === null || percent === undefined ? '' :
        '<span class="dash-rowbar"><i style="width:' +
        Math.max(2, Math.min(100, Number(percent) || 0)) + '%"></i></span>') +
      '</span>' + button + '</li>';
  }

  function dropCard_(id) {
    const old = document.getElementById("dashCard-" + id);
    if (old) { old.remove(); syncDashEmpty(); }
  }

  function renderContinue(overrideId) {
    const id = overrideId || "continue";
    if (isHidden(id)) return;
    const list = feedPersonal["continue"] || [];
    if (!list.length) { dropCard_(id); return; }
    // A linked Jellyfin/Plex account means these positions came from THAT
    // server and MediaForge has no file to hand its own player -- exactly the
    // distinction the poster row made, kept here: a real <a> that leaves the
    // app, versus a button that opens the built-in player.
    const remoteServer = feedPersonal.continue_source &&
      feedPersonal.continue_source !== "local" ? feedPersonal.continue_source : "";
    const rows = list.map(function (it, n) {
      const sub = it.is_movie
        ? HT("movie")
        : (it.season
            ? "S" + it.season + (it.episode ? " · " + HT("episode") + " " + it.episode : "")
            : (it.episode ? HT("episode") + " " + it.episode : ""));
      const left = remainingText(it);
      const button = it.remote
        ? '<a class="dash-btn" href="' + esc(it.open_url || "#") +
          '" target="_blank" rel="noopener noreferrer">' +
          esc(remoteServer || HT("dash_resume")) + '</a>'
        : '<button type="button" class="dash-btn" data-play="' + n + '">' +
          esc(HT("dash_resume")) + '</button>';
      return listRow(it, left ? sub + " · " + left : sub, it.percent, button);
    });
    // n above indexes the FULL `list`, not the page -- pagedRows only
    // slices which pre-built rows are shown, it never reorders `list`
    // itself, so data-play="n" still resolves against feedPersonal on click
    // regardless of which page produced the click.
    const paged = pagedRows(id, rows);
    card(id,
         head(HT("continue_watching"), "", OWN_ICONS["continue"]),
         '<ul class="dash-gaps">' + paged.rowsHtml + '</ul>' + paged.pagerHtml);
  }

  /** Same wording the poster row used. */
  function remainingText(item) {
    const left = Math.max(0, (item.duration || 0) - (item.position || 0));
    if (!left) return "";
    const mins = Math.round(left / 60);
    if (mins < 60) return HT("minutes_left").replace("{}", String(mins));
    const hours = Math.floor(mins / 60);
    return HT("hours_left").replace("{}", hours + ":" + String(mins % 60).padStart(2, "0"));
  }

  function renderWatchlist(overrideId) {
    const id = overrideId || "watchlist";
    if (isHidden(id)) return;
    const list = feedPersonal.watchlist || [];
    if (!list.length) { dropCard_(id); return; }
    // No maxTotal: the whole list, paged 5 at a time.
    const rows = list.map(function (it) {
      return listRow(it, it.provider || "", null,
        '<button type="button" class="dash-btn" data-open-series="' +
        esc(it.url || "") + '">' + esc(HT("dash_open")) + '</button>');
    });
    const paged = pagedRows(id, rows);
    card(id,
         head(HT("your_watchlist"), "", OWN_ICONS.watchlist,
              { href: "/favourites", label: HT("dash_open_list") }),
         '<ul class="dash-gaps">' + paged.rowsHtml + '</ul>' + paged.pagerHtml);
  }

  function renderNewLibrary(overrideId) {
    const id = overrideId || "newlib";
    if (isHidden(id)) return;
    const list = feedPersonal.library || [];
    if (!list.length) { dropCard_(id); return; }
    const rows = list.map(function (it) {
      const sub = it.is_movie ? HT("movie")
        : (it.episodes || 0) + " " + HT("episodes_short");
      return listRow(it, sub, null,
        '<a class="dash-btn" href="/library">' + esc(HT("dash_open")) + '</a>');
    });
    const paged = pagedRows(id, rows, 10);
    card(id,
         head(HT("new_in_library"), "", OWN_ICONS.newlib,
              { href: "/library", label: HT("dash_open_list") }),
         '<ul class="dash-gaps">' + paged.rowsHtml + '</ul>' + paged.pagerHtml);
  }

  // Which renderer redraws a given card id after its pager page changes --
  // panel cards (queue/activity/library/storage/system/wrapped) all go
  // through the one PANEL_DATA_CACHE + renderPanel() path, feed cards each
  // keep their own renderer. A base id only; an extra instance never gets
  // its own pager state to page independently (same "one shared render"
  // reasoning card() already documents for multi:true widgets).
  const FEED_RERENDER = {
    gaps: renderGaps, upcoming: renderUpcoming, "continue": renderContinue,
    watchlist: renderWatchlist, newlib: renderNewLibrary,
  };
  function rerenderCard(id) {
    const base = id.split(".")[0];
    if (PANEL_DATA_CACHE[base]) { renderPanel(PANEL_DATA_CACHE[base], id); return; }
    const fn = FEED_RERENDER[base];
    if (fn) fn(id);
  }

  /** Called by home_feed.js whenever its own copy of either changes. */
  window.mfHomeDashFeed = function (sources, personal) {
    feedSources = sources || [];
    feedPersonal = personal || {};
    renderGaps();
    renderSources();
    renderUpcoming();
    renderContinue();
    renderWatchlist();
    renderNewLibrary();
    if (typeof window.mfHomeSyncDashEmpty === "function") window.mfHomeSyncDashEmpty();
  };

  // ------------------------------------------------------------------- data
  //
  // Every panel payload this account has ever received, keyed by its real
  // type id -- unconditionally, hidden or not. This is what lets "Add
  // widget" (see below) materialise a panel card immediately instead of
  // waiting for the next poll, and what supplies the Add menu's title/icon/
  // multi flag for panel types without a second endpoint.
  const PANEL_DATA_CACHE = {};

  function load(only) {
    const query = only ? "?only=" + encodeURIComponent(only.join(",")) : "";
    return fetch("/api/home-panels/all" + query)
      .then(function (r) { return r.ok ? r.json() : { panels: [] }; })
      .then(function (data) {
        ((data && data.panels) || []).forEach(function (panelData) {
          PANEL_DATA_CACHE[panelData.id] = panelData;
          renderPanel(panelData);
        });
        if (typeof window.mfHomeSyncDashEmpty === "function") window.mfHomeSyncDashEmpty();
      })
      .catch(function () { /* a dead endpoint leaves the last cards standing */ });
  }

  function startPoll() {
    if (!window.mfPoll) return;
    // mfPoll pauses itself while the browser tab is hidden; the tab check is
    // the in-page half of the same idea -- the Discover tab is a full screen
    // of posters, and refreshing cards nobody can see is the cost this whole
    // feature was rebuilt to avoid.
    poller = window.mfPoll(function () {
      if (document.body.dataset.homeTabOpen === "disc") return;
      load(POLLED);
    }, POLL_MS);
  }

  // ------------------------------------------------------------ interaction
  // Delegated on the sections root, not on each card: a card's content is
  // replaced wholesale on every refresh, a listener bound to it would not
  // survive that.
  colsRoot.addEventListener("click", function (ev) {
    const el = ev.target.closest(
      "[data-action],[data-gap-search],[data-gap-ignore],[data-play],[data-open-series]," +
      "[data-page-dir],.dash-close");
    if (!el) return;
    if (el.hasAttribute("data-page-dir")) {
      const pagerEl = el.closest("[data-pager]");
      if (!pagerEl) return;
      const cardId = pagerEl.dataset.pager;
      _pageState[cardId] = Math.max(0, (_pageState[cardId] || 0) + parseInt(el.dataset.pageDir, 10));
      rerenderCard(cardId);
      return;
    }
    if (el.classList.contains("dash-close")) {
      // Removing/adding is an arrangement choice, same as drag/resize --
      // a locked board leaves the "x" as a dead-looking control rather
      // than acting on a click, same treatment the grip/resize get.
      if (isLocked()) return;
      const cardEl = el.closest("[data-card]");
      if (!cardEl) return;
      ev.preventDefault();
      const id = cardEl.dataset.card;
      if (cardEl.dataset.widgetKind === "thirdparty-template") {
        // Opaque, server-rendered, page-load-only Jinja markup -- there is
        // nothing to reconstruct it from without a full page reload, so
        // "closing" it only ever toggles visibility, never DOM-removes it
        // (contrast with the JSON-driven panel/feed cards below, which are
        // fully re-renderable from cached data). See addWidget()'s
        // matching branch and the startup soft-hide pass above.
        cardEl.style.display = "none";
        cardEl.dataset.dashHidden = "1";
        HIDDEN.add(id);
        saveHidden();
        syncDashEmpty();
        return;
      }
      cardEl.remove();
      if (id.indexOf(".") === -1) {
        // A base id must not just vanish once -- see isHidden()'s callers.
        // An extra instance ("id.2") is never auto-recreated by a poll/
        // load in the first place, so it needs no suppression.
        HIDDEN.add(id);
        saveHidden();
      }
      syncDashEmpty();
      return;
    }
    if (el.hasAttribute("data-play")) {
      // Local playback: the same call the poster row made.
      const it = (feedPersonal["continue"] || [])[parseInt(el.dataset.play, 10)];
      if (!it) return;
      if (typeof window.openPlayer === "function") {
        window.openPlayer(it.path, it.title, it.position || 0);
      } else if (typeof window.showToast === "function") {
        window.showToast(HT("player_loading"));
      }
      return;
    }
    if (el.hasAttribute("data-open-series")) {
      if (typeof window.openSeries === "function") {
        window.openSeries(el.getAttribute("data-open-series"));
      }
      return;
    }
    if (el.hasAttribute("data-action")) {
      const fn = ACTIONS[el.getAttribute("data-action")];
      if (fn) { ev.preventDefault(); fn(); }
      return;
    }
    ev.preventDefault();
    if (el.hasAttribute("data-gap-search")) {
      // Hand the title to the ordinary search: it is the one place that
      // knows which of the enabled sources actually has this series.
      const input = document.getElementById("searchInput");
      if (input) input.value = el.getAttribute("data-gap-search");
      if (typeof window.doSearch === "function") window.doSearch();
      return;
    }
    ignoreGap(el);
  });

  /** "Not interested in this one." Writes the same media_ignored entry the
      statistics page uses (POST /api/media/ignore with the "__all__" slot),
      so a series dismissed here is also gone from /stats' incomplete list and
      can be taken back there. */
  function ignoreGap(btn) {
    const folder = btn.getAttribute("data-gap-ignore");
    btn.disabled = true;
    fetch("/api/media/ignore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: [{ folder: folder, title: folder, all: true }] }),
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok || data.error) throw new Error(data.error || ("HTTP " + resp.status));
      });
    }).then(function () {
      feedPersonal.gaps = (feedPersonal.gaps || []).filter(function (g) {
        return (g.folder || g.title) !== folder;
      });
      // Tell home_feed.js too: its copy feeds the poster row and the next
      // renderPersonal() would otherwise put the row straight back.
      if (typeof window.mfFeedDropGap === "function") window.mfFeedDropGap(folder);
      renderGaps();
      if (typeof window.showToast === "function") window.showToast(HT("gap_ignored"));
    }).catch(function () {
      btn.disabled = false;
      if (typeof window.showToast === "function") window.showToast(HT("gap_ignore_failed"));
    });
  }

  // Locking (below) is the only remaining arrangement gate: it flips the
  // draggable attribute on cards and section headers.
  function isLocked() {
    return colsRoot.classList.contains("is-dash-locked");
  }

  // ----------------------------------------------------------- locking
  //
  // Off by default (rearranging is the point of this feature) -- but if the
  // user locks the board, that choice is remembered on the account, same as
  // every other per-account UI preference here.
  const LOCK_PREF = "home_dash_locked";
  let locked = PREFS[LOCK_PREF] === "1";

  function applyLockUI() {
    if (colsRoot) {
      colsRoot.classList.toggle("is-dash-locked", locked);
      // Cards use the browser's native drag-and-drop (see renderOneCard()
      // and the colsRoot drag handlers below), which has nothing to
      // short-circuit the way a pointer handler would -- the draggable
      // attribute itself IS the gate.
      colsRoot.querySelectorAll(".dash-card-flow").forEach(function (el) { el.draggable = !locked; });
    }
    const btn = document.getElementById("dashLockBtn");
    if (btn) {
      btn.classList.toggle("is-locked", locked);
      btn.setAttribute("aria-pressed", locked ? "true" : "false");
      const label = locked ? HT("dash_unlock") : HT("dash_lock");
      btn.title = label;
      btn.setAttribute("aria-label", label);
    }
    // Adding/removing is an arrangement action, same as drag/resize -- a
    // locked board disables the button rather than leaving it clickable
    // with no effect.
    const addBtnEl = document.getElementById("dashAddBtn");
    if (addBtnEl) addBtnEl.disabled = locked;
    if (locked) closeAddMenu();
  }

  const lockBtn = document.getElementById("dashLockBtn");
  if (lockBtn) {
    lockBtn.addEventListener("click", function () {
      locked = !locked;
      applyLockUI();
      if (typeof window.mfSaveUserPref === "function") {
        window.mfSaveUserPref({ home_dash_locked: locked ? "1" : "0" });
      }
    });
  }

  // ------------------------------------------------------- "Add widget"
  //
  // Everything the menu needs is already sitting in memory: FEED_CATALOG for
  // the three feed-driven cards (gaps/sources/upcoming) and the three
  // personal lists (continue/watchlist/newlib), PANEL_DATA_CACHE for every
  // built-in panel and module panel this account has ever received a body
  // for (see load()). No new endpoint, no new request on open.
  const FEED_CATALOG = {
    gaps: { title: function () { return HT("dash_gaps"); }, icon: OWN_ICONS.gaps, render: renderGaps },
    sources: { title: function () { return HT("sources"); }, icon: OWN_ICONS.sources, render: renderSources },
    upcoming: { title: function () { return HT("dash_today"); }, icon: OWN_ICONS.upcoming, render: renderUpcoming },
    "continue": { title: function () { return HT("continue_watching"); }, icon: OWN_ICONS["continue"], render: renderContinue },
    watchlist: { title: function () { return HT("your_watchlist"); }, icon: OWN_ICONS.watchlist, render: renderWatchlist },
    newlib: { title: function () { return HT("new_in_library"); }, icon: OWN_ICONS.newlib, render: renderNewLibrary },
  };

  /** Whether the Add menu may offer more than one card of `typeId` -- a
      module panel's own `multi` field. No built-in sets it: every one of
      them shows instance-wide state, so a second copy would be a duplicate. */
  function isMulti(typeId) {
    const cached = PANEL_DATA_CACHE[typeId];
    return !!(cached && cached.multi);
  }

  /** Smallest N >= 2 with no "id.N" element on the board yet -- reuses a gap
      left by removing an earlier instance rather than always growing. */
  function nextInstanceId(typeId) {
    let n = 2;
    while (document.getElementById("dashCard-" + typeId + "." + n)) n++;
    return typeId + "." + n;
  }

  /** Every entry the Add menu may currently offer: a `multi` type always
      (adding another is always valid), a non-multi type only while it has
      no card on the board at all. */
  function addMenuEntries() {
    const out = [];
    Object.keys(FEED_CATALOG).forEach(function (id) {
      const entry = FEED_CATALOG[id];
      const onBoard = !!document.getElementById("dashCard-" + id);
      if (isMulti(id) || !onBoard) {
        out.push({ id: id, title: entry.title(), icon: entry.icon });
      }
    });
    Object.keys(PANEL_DATA_CACHE).forEach(function (id) {
      const data = PANEL_DATA_CACHE[id];
      const onBoard = !!document.getElementById("dashCard-" + id);
      if (isMulti(id) || !onBoard) {
        const title = TITLES[id] ? HT(TITLES[id]) : text(data.label_key, data.label);
        out.push({ id: id, title: title, icon: data.icon });
      }
    });
    // Third source: thirdparty-template cards currently soft-hidden. Never
    // multi (see resolve_dashboard_widgets()' own comment in registry.py --
    // there is no live re-render story for this opaque, page-load-only
    // markup) and, unlike a multi:true panel, only ever offered while
    // hidden -- there is exactly one of these on the board, always. No
    // separate JS-side title/icon cache: the already-rendered (just
    // invisible) card itself carries both.
    document.querySelectorAll('.dash-card[data-widget-kind="thirdparty-template"][data-dash-hidden="1"]')
      .forEach(function (cardEl) {
        const id = cardEl.dataset.card;
        const headEl = cardEl.querySelector(".dash-card-head");
        const titleEl = headEl && headEl.querySelector("span");
        const svgEl = headEl && headEl.querySelector("svg");
        out.push({
          id: id,
          title: titleEl ? titleEl.textContent : id,
          iconHtml: svgEl ? svgEl.outerHTML : "",
        });
      });
    return out;
  }

  function addWidget(typeId) {
    const thirdEl = document.getElementById("dashCard-" + typeId);
    if (thirdEl && thirdEl.dataset.widgetKind === "thirdparty-template") {
      // Un-hide only -- content is server-rendered once at page load and
      // never re-rendered from here.
      thirdEl.style.display = "";
      delete thirdEl.dataset.dashHidden;
      HIDDEN.delete(typeId);
      saveHidden();
      syncDashEmpty();
      return;
    }
    if (isMulti(typeId)) {
      const instanceId = nextInstanceId(typeId);
      const feedEntry = FEED_CATALOG[typeId];
      if (feedEntry) feedEntry.render(instanceId);
      else if (PANEL_DATA_CACHE[typeId]) renderPanel(PANEL_DATA_CACHE[typeId], instanceId);
      return;
    }
    // Not multi: this can only be the base id, and it was offered only
    // because it was not already on the board -- unhide it (in case it was
    // hidden) and render it immediately from whatever is already cached,
    // rather than waiting for the next poll.
    HIDDEN.delete(typeId);
    saveHidden();
    const feedEntry = FEED_CATALOG[typeId];
    if (feedEntry) feedEntry.render();
    else if (PANEL_DATA_CACHE[typeId]) renderPanel(PANEL_DATA_CACHE[typeId]);
  }

  const addWrap = document.getElementById("dashAddWrap");
  const addBtn = document.getElementById("dashAddBtn");
  const addMenu = document.getElementById("dashAddMenu");

  function closeAddMenu() {
    if (!addWrap || !addWrap.classList.contains("is-open")) return;
    addWrap.classList.remove("is-open");
    if (addBtn) addBtn.setAttribute("aria-expanded", "false");
    if (addMenu) addMenu.hidden = true;
  }

  function openAddMenu() {
    if (!addWrap || !addMenu || locked) return;
    const entries = addMenuEntries();
    addMenu.innerHTML = entries.length
      ? entries.map(function (e) {
          // e.iconHtml (thirdparty-template entries) is markup cloned straight
          // from this account's own already-rendered card head -- trusted,
          // not user input -- so it is used as-is, same as e.icon going
          // through iconSvg() below for every other entry kind.
          const icon = e.iconHtml || (e.icon ? iconSvg(e.icon) : "");
          return '<button type="button" class="dash-add-item" role="menuitem" data-add="' +
            esc(e.id) + '">' + icon +
            '<span>' + esc(e.title) + '</span></button>';
        }).join("")
      : '<div class="dash-add-empty">' + esc(HT("dash_add_empty")) + '</div>';
    addWrap.classList.add("is-open");
    if (addBtn) addBtn.setAttribute("aria-expanded", "true");
    addMenu.hidden = false;
    const first = addMenu.querySelector('[role="menuitem"]');
    if (first) first.focus();
  }

  if (addBtn) {
    addBtn.addEventListener("click", function (ev) {
      ev.preventDefault();
      if (addBtn.disabled) return;
      if (addWrap.classList.contains("is-open")) closeAddMenu();
      else openAddMenu();
    });
  }
  if (addMenu) {
    addMenu.addEventListener("click", function (ev) {
      const item = ev.target.closest("[data-add]");
      if (!item) return;
      addWidget(item.getAttribute("data-add"));
      closeAddMenu();
      if (addBtn) addBtn.focus();
    });
  }
  document.addEventListener("click", function (ev) {
    if (!addWrap || !addWrap.classList.contains("is-open")) return;
    if (!ev.target.closest("#dashAddWrap")) closeAddMenu();
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    if (!addWrap || !addWrap.classList.contains("is-open")) return;
    closeAddMenu();
    if (addBtn) addBtn.focus();
  });

  applyLockUI();

  // ---------------------------------------------------- column arrangement
  //
  // One drag gesture does everything the layout can express: pick a card up
  // anywhere on it, drop it at another position in its own column or in a
  // different column. Native HTML5 drag-and-drop, delegated on colsRoot (one
  // listener set, not one per card) so it keeps working for cards created
  // later by a poll or by the Add-widget menu.
  //
  // A dedicated marker element shows where the card would land, rather than
  // moving the card itself on every pointer move: a heavy card would
  // otherwise relayout its whole column continuously while dragging.

  /** Server-rendered module widget cards (index.html puts them in the middle
      column) move to their saved column once, before anything else is
      rendered -- otherwise a module widget the account dragged elsewhere
      would jump on every page load. */
  colsRoot.querySelectorAll('.dash-card[data-widget-kind="thirdparty-template"]').forEach(function (el) {
    const target = colEl(colFor(el.dataset.card));
    if (target && el.parentElement !== target) target.insertBefore(el, colInsertRef(target, el.dataset.card));
  });

  let draggingCard = null;
  let dragMarker = null;

  /** The card the marker goes BEFORE, or null to append. A column is a
      single vertical stack, so the cursor's Y against each card's vertical
      midpoint is the whole answer. */
  function markerRefFor(col, y) {
    const siblings = Array.prototype.filter.call(
      col.querySelectorAll(".dash-card-flow[data-card]"),
      function (el) { return el !== draggingCard; }
    );
    for (let i = 0; i < siblings.length; i++) {
      const rect = siblings[i].getBoundingClientRect();
      if (y < rect.top + rect.height / 2) return siblings[i];
    }
    return null;
  }

  function removeMarker() {
    if (dragMarker && dragMarker.parentElement) dragMarker.parentElement.removeChild(dragMarker);
    colsRoot.querySelectorAll(".dash-col.is-drop-target")
      .forEach(function (el) { el.classList.remove("is-drop-target"); });
  }

  colsRoot.addEventListener("dragstart", function (ev) {
    const cardEl = ev.target.closest(".dash-card-flow");
    if (!cardEl || isLocked()) return;
    draggingCard = cardEl;
    cardEl.classList.add("dragging");
    colsRoot.classList.add("is-dragging");
    if (!dragMarker) {
      dragMarker = document.createElement("div");
      dragMarker.className = "dash-card-drop-marker";
    }
    try { ev.dataTransfer.effectAllowed = "move"; } catch (e) { /* older browsers */ }
  });

  colsRoot.addEventListener("dragend", function () {
    if (draggingCard) draggingCard.classList.remove("dragging");
    removeMarker();
    colsRoot.classList.remove("is-dragging");
    draggingCard = null;
  });

  colsRoot.addEventListener("dragover", function (ev) {
    if (!draggingCard) return;
    const col = ev.target.closest(".dash-col");
    if (!col) { removeMarker(); return; }
    ev.preventDefault();
    colsRoot.querySelectorAll(".dash-col.is-drop-target")
      .forEach(function (el) { if (el !== col) el.classList.remove("is-drop-target"); });
    col.classList.add("is-drop-target");
    col.insertBefore(dragMarker, markerRefFor(col, ev.clientY));
  });

  colsRoot.addEventListener("drop", function (ev) {
    if (!draggingCard || !dragMarker || !dragMarker.parentElement) return;
    ev.preventDefault();
    dragMarker.parentElement.insertBefore(draggingCard, dragMarker);
    removeMarker();
    colsRoot.classList.remove("is-dragging");
    draggingCard.classList.remove("dragging");
    draggingCard = null;
    saveCardLayout();
    syncDashEmpty();
  });

  /** The Arrange form (static/start_page.js) writes the same preference from
      its own list; this lets it apply a column change without a reload. */
  window.mfDashApplyCardLayout = function (value) {
    PREFS.home_dash_card_layout = String(value || "");
    _cardLayout = null;
    // Snapshot first, then place: moving a card mid-query would otherwise
    // let a card that jumped forward be visited a second time.
    const all = Array.prototype.slice.call(colsRoot.querySelectorAll(".dash-card-flow[data-card]"));
    all.forEach(function (el) {
      const target = colEl(colFor(el.dataset.card));
      if (target) target.insertBefore(el, colInsertRef(target, el.dataset.card));
    });
    syncDashEmpty();
  };


  // --------------------------------------------------------- loading state
  //
  // Every built-in card waits on a fetch (this file's own /api/home-panels/
  // all, or home_feed.js's for the feed cards) before it has anything to
  // show, and until now that meant an empty stretch of grid until the first
  // response landed. A card is already placed here, in its real position and
  // size, with a spinner standing in for its content -- renderOneCard()
  // (called through card() the moment real data arrives) finds this same
  // element by id and simply overwrites `.dash-card-content`, the exact path
  // an ordinary refresh already takes. Module panels are not included: their
  // ids are not known until /api/home-panels/all answers, so there is
  // nothing to place a spinner for yet -- they pop in normally, no worse
  // than before this existed.
  function loadingBody() {
    return '<div class="dash-loading-body"><span class="dash-loader" role="status" aria-label="' +
      esc(HT("dash_loading")) + '"></span></div>';
  }
  SKELETON_IDS.forEach(function (id) {
    if (HIDDEN.has(id)) return;
    if (document.getElementById("dashCard-" + id)) return;   // server-rendered already, e.g. a thirdparty template card
    renderOneCard(id, "", loadingBody(), "");
  });

  load(null);
  startPoll();
})();
