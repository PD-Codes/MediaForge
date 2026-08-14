/* ===================================================================
   MediaForge — Dashboard card grid

   The Dashboard tab answers "what is this instance doing right now": the
   queue, what is missing, what happened, how full the disks are, which
   sources answer. Two columns of cards -- the wide one for the things you
   read line by line, the narrow one for the things you glance at.

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
   below the grid are switched off in home_feed.js (DASH_CARD_ROWS), the same
   way gaps and today's calendar were.

   THE ARRANGEMENT ENGINE (v3): hand-rolled absolute positioning, the same
   idea gridstack.js uses, no library. A v2 (CSS Grid `dense` auto-flow)
   attempt shipped before this one and was rejected: `dense` repacks EVERY
   card's placement on any span change, so shrinking one widget visibly
   moved every other widget SIDEWAYS/into a different order on the board.
   Real pixel positions plus a collision resolver that only pushes the cards
   actually touched by a move fixes the sideways-jump part by construction --
   see resolveCollisions() below.

   Each card has {x, y, w, h} in grid units (COLS = 12 columns, ROW_H = 24px
   row unit, GAP = 18px). A card's X, W and H only ever change from a direct
   drag/resize/nudge on THAT card -- nothing here ever moves another card
   sideways or resizes it. Y is the one exception: after every commit,
   compactAll() lets every card float straight UP into any gap that opened in
   its own column span (never down, never sideways -- see its own comment for
   why that is a one-directional, order-preserving settle and not the kind of
   reflow `dense` did). That is deliberate: an empty column-wide gap under a
   widget you just shrank is exactly what should close, and it is the
   difference between "nothing moves" (the previous, rejected design) and
   "nothing jumps, but gaps do not survive either".

   The preference key is unchanged, `home_dash_layout`, now storing v3 rows
   "<card id>:<x>:<y>:<w>:<h>" (validated server-side in web/db/ui_prefs.py,
   which also still reads the old v1/v2 rows to migrate them once). A card
   with no stored v3 entry is placed once, the first time it is ever seen --
   see placeNewCard() -- and immediately settled by the same compactAll() as
   everything else, so a brand-new card never overlaps whatever else is on
   the board no matter which order two cards' data happened to arrive in.

   Nothing from either side is ever inserted as markup: every string goes
   through mfEscape().
   =================================================================== */

(function () {
  const grid = document.getElementById("homeDashGrid");
  if (!grid) return;                      // classic home page — nothing to do

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

  // Where a card sits, and how much room it gets by default (before the user
  // ever moves anything). `wide` cards -- the ones carrying a LIST (running
  // downloads, missing episodes, the activity log), which need the width for
  // a title plus a progress bar -- default to 8 of the grid's 12 columns and
  // 10 rows; the rest are reference figures that read fine at 4x8.
  //
  // `order` is only used to seed the FIRST-EVER placement pass (see
  // placeNewCard()) -- once a card has a stored x/y it never looks at this
  // again. Kept as a plain ascending number, same as before.
  // `multi: true` lets the Dashboard's "Add widget" menu offer a second (and
  // third, ...) card of that type -- see isMulti() and addWidget() below.
  // None of today's built-ins set it: every one of them shows instance-wide
  // state (the queue, the library, the disks), not per-instance data, so a
  // second copy would just be a duplicate. The key is read the same way the
  // module side (payload's `multi` field) is, so a future built-in COULD
  // opt in without any engine change.
  const PLACE = {
    queue:    { order: 10, wide: true },
    // The three personal lists sit right under the queue: they are the ones
    // the visitor is most likely to have come for.
    continue: { order: 12, wide: true },
    watchlist: { order: 14, wide: true },
    newlib:   { order: 16, wide: true },
    gaps:     { order: 20, wide: true },
    activity: { order: 30, wide: true },
    library:  { order: 40 },
    storage:  { order: 50 },
    sources:  { order: 60 },
    upcoming: { order: 70 },
    system:   { order: 80 },
    wrapped:  { order: 90 },
  };
  // A module panel has no place of its own: it is placed after the built-ins
  // (by order) the first time it is seen, and gets the full width because
  // nothing here knows what it will put inside.
  const MODULE_PLACE = { order: 100, wide: true };

  // Card headings that read better than the panel's own button label did.
  // "Queue" was a button in a row of buttons; as a card heading next to a
  // progress bar it is "Running now".
  const TITLES = {
    queue: "dash_now",
    activity: "dash_activity",
    library: "dash_overview",
  };

  // Heading icons for the cards this file builds itself. The panel cards get
  // theirs from the payload; without these three the grid mixed cards with an
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

  // ------------------------------------------------------- user arrangement
  //
  // One preference, `home_dash_layout`, holding "<id>:<x>:<y>:<w>:<h>" per
  // card (format v3). x/y/w/h are all in grid units -- see the pixel
  // conversion helpers below. A card the user has never touched has no v3
  // entry; placeNewCard() seeds one, once, the first time that card is
  // rendered (reusing its old v1/v2 size if it had one), and saves
  // immediately so the packer never runs twice for the same card.
  const COLS = 12;
  const ROW_H = 24;
  const GAP = 18;
  const PREFS = window._USER_PREFS || {};

  function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }

  /** Parses every generation this preference has ever been written in.
      Returns the valid v3 rows (the live layout, mutated from here on) plus
      a `legacy` map of best-effort sizes read from v1/v2 rows, used only to
      seed a first-ever placement so an account does not lose its old
      width/height the moment this engine ships. */
  function parseLayout(raw) {
    const v3 = {};
    const legacy = {};
    String(raw).split(",").forEach(function (part) {
      if (!part) return;
      const bits = part.split(":");
      if (bits.length === 5) {
        const id = bits[0];
        const x = parseInt(bits[1], 10), y = parseInt(bits[2], 10);
        const w = parseInt(bits[3], 10), h = parseInt(bits[4], 10);
        if (!id || !isFinite(x) || !isFinite(y) || !isFinite(w) || !isFinite(h)) return;
        if (x < 0 || x > COLS - 1 || y < 0 || y > 999) return;
        if (w < 2 || w > COLS || h < 3 || h > 80) return;
        v3[id] = { x: x, y: y, w: w, h: h };
        return;
      }
      const order = parseInt(bits[1], 10);
      if (!bits[0] || !isFinite(order)) return;
      if (bits.length === 3) {
        // v1: "id:order:span[1-3]" -- the oldest, 3-track format.
        const span = parseInt(bits[2], 10);
        if (span < 1 || span > 3) return;
        legacy[bits[0]] = { order: order, w: span * 4, h: null };
      } else if (bits.length === 4) {
        // v2: "id:order:colspan[1-12]:rowspan[1-40|a]" -- last session's
        // CSS-Grid format.
        const colSpan = parseInt(bits[2], 10);
        const rowRaw = bits[3];
        const rowSpan = rowRaw === "a" ? null : parseInt(rowRaw, 10);
        if (colSpan < 1 || colSpan > COLS) return;
        if (rowRaw !== "a" && (!isFinite(rowSpan) || rowSpan < 1 || rowSpan > 40)) return;
        legacy[bits[0]] = { order: order, w: colSpan, h: rowSpan };
      }
    });
    return { v3: v3, legacy: legacy };
  }

  const parsed = parseLayout(PREFS.home_dash_layout || "");
  const layout = parsed.v3;          // the live layout: id -> {x, y, w, h}
  const legacyMeta = parsed.legacy;  // id -> {order, w, h|null} from v1/v2

  function serializeLayout() {
    return Object.keys(layout).map(function (id) {
      const p = layout[id];
      return id + ":" + p.x + ":" + p.y + ":" + p.w + ":" + p.h;
    }).join(",");
  }

  let saveTimer = null;
  function saveLayout() {
    // Debounced: a live drag can call this indirectly many times a second
    // once collisions cascade through several cards; the pref only needs to
    // reach the server once the dust settles.
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      saveTimer = null;
      if (typeof window.mfSaveUserPref === "function") {
        window.mfSaveUserPref({ home_dash_layout: serializeLayout() });
      }
    }, 400);
  }

  // Base ids the account closed with a card's own "x" (see card()'s close
  // button below). Only ever holds base ids -- an extra instance ("id.2") is
  // simply never auto-recreated by a poll/load in the first place, so
  // removing one needs no suppression, only a DOM remove + forgetCard().
  const HIDDEN = new Set(String(PREFS.home_dash_hidden || "").split(",").filter(Boolean));

  // Third-party dashboard-widget cards (register_thirdparty's
  // dashboard_widget_template, see index.html) are already server-rendered
  // into the grid before this script runs. Soft-hide any the account has
  // closed BEFORE the first reflowAll() pass -- see reflowAll()'s own
  // "dashHidden" skip below -- so a hidden one never claims grid space or a
  // home_dash_layout entry on load. Their content is opaque, page-load-only
  // Jinja markup with no re-fetch story, so "closed" always means
  // display:none, never DOM removal -- see the .dash-close handler and
  // addWidget() further down.
  grid.querySelectorAll('.dash-card[data-widget-kind="thirdparty-template"]').forEach(function (el) {
    if (HIDDEN.has(el.dataset.card)) {
      el.style.display = "none";
      el.dataset.dashHidden = "1";
    }
  });

  let hiddenSaveTimer = null;
  function saveHidden() {
    // Same debounced-save shape as saveLayout(): closing several cards in a
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

  /** Pulls every card up to the lowest free row in its own column span, no
      exceptions -- this is what closes gaps ("keinen Platz lassen") and is
      also, on its own, what places a brand-new card, so there is exactly one
      packing algorithm instead of two that could disagree with each other
      (the previous version kept a second, incrementally-updated column-
      height tracker next to this one for new-card placement only, and the
      two could desync across async loads -- e.g. "continue watching" and the
      watchlist card landing on top of each other when their data arrived in
      an order the tracker had not seen yet. Recomputing from `layout` itself,
      every time, cannot desync).

      Cards are processed in ascending Y (ties by X, then id, for a
      deterministic order), so a card can only ever move UP here, never down:
      by the time card K is placed, every card sorted before it has already
      claimed its column space at a height no greater than K's own current Y
      (nothing overlapped before this ran), so K's new Y is bounded by that
      and can only shrink or stay put. That monotonic property is the whole
      reason this reads as "widgets climb into gaps" and not "widgets jump
      around" -- X, W and H are never touched here, only Y, and only upward. */
  function compactAll() {
    const heights = new Array(COLS).fill(0);
    Object.keys(layout).sort(function (a, b) {
      const pa = layout[a], pb = layout[b];
      return pa.y - pb.y || pa.x - pb.x || (a < b ? -1 : 1);
    }).forEach(function (id) {
      const p = layout[id];
      let y = 0;
      for (let c = p.x; c < p.x + p.w; c++) y = Math.max(y, heights[c]);
      p.y = y;
      for (let c = p.x; c < p.x + p.w; c++) heights[c] = y + p.h;
    });
  }

  /** Seeds a placeholder for a card the first time it is ever seen (no
      stored v3 entry) and lets compactAll() find its real resting slot.
      `order` (the built-in PLACE table, or a v1/v2 row's old order) is only
      ever used as compactAll()'s SORT key here -- it decides which cards are
      considered "above" which on a fresh account, it is never read as a
      literal row number. */
  function placeNewCard(id) {
    if (layout[id]) return layout[id];
    const meta = legacyMeta[id];
    const base = PLACE[id] || MODULE_PLACE;
    let w = meta ? meta.w : (base.wide ? 8 : 4);
    let h = meta && meta.h != null ? meta.h : (base.wide ? 10 : 8);
    w = clamp(w, 2, COLS);
    h = clamp(h, 3, 80);
    layout[id] = { x: 0, y: meta ? meta.order : base.order, w: w, h: h };
    compactAll();
    saveLayout();
    return layout[id];
  }

  /** A card that no longer has anything to show (no gaps left, an empty
      watchlist, ...) is removed from the DOM by its own renderer; this drops
      its layout entry too and lets everything below float up to close the
      hole -- without deleting the entry here, compactAll() would keep
      reserving its footprint forever for a card that no longer exists. */
  function forgetCard(id) {
    if (!layout[id]) return;
    delete layout[id];
    compactAll();
    saveLayout();
  }

  /** Where a card sits now -- the stored place, or a freshly seeded one. */
  function place(id) {
    return layout[id] || placeNewCard(id);
  }

  // --------------------------------------------------------- collisions
  //
  // Runs first, before compactAll(): after any single card's rect changes,
  // only the cards that rect now actually OVERLAPS get pushed -- straight
  // down, just clear of it -- never the whole board, and never sideways. A
  // shrink can never trigger a push at all: shrinking a rect can only shrink
  // the set of rects it overlaps, so resolveCollisions() after a shrink walks
  // a `queue` that starts and ends at [movedId] with zero pushes -- a
  // guaranteed no-op by construction, not by a special case. Any actual
  // upward movement other cards make after a shrink is compactAll() closing
  // the gap that left, not this function -- see the file header and
  // compactAll()'s own comment.
  function rectsOverlap(a, b) {
    return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  }

  function resolveCollisions(movedId) {
    const queue = [movedId];
    const seen = new Set();
    while (queue.length) {
      const id = queue.shift();
      if (seen.has(id)) continue;
      seen.add(id);
      const a = layout[id];
      if (!a) continue;
      Object.keys(layout).forEach(function (otherId) {
        if (otherId === id || seen.has(otherId)) return;
        const b = layout[otherId];
        if (rectsOverlap(a, b)) {
          b.y = a.y + a.h;      // push it straight down, just clear of a
          queue.push(otherId);   // that may now overlap a third card -- cascade
        }
      });
    }
  }

  // ------------------------------------------------------------- geometry
  //
  // Pixel conversion. colWidth is recomputed on every ResizeObserver tick
  // (and lazily the first time a card is placed, before the observer's
  // first callback has necessarily fired).
  let colWidth = 0;
  function recalcColWidth() {
    const contentWidth = grid.clientWidth;
    colWidth = Math.max(0, (contentWidth - (COLS - 1) * GAP) / COLS);
  }

  function pxX(x) { return x * (colWidth + GAP); }
  function pxY(y) { return y * (ROW_H + GAP); }
  function pxW(w) { return w * colWidth + (w - 1) * GAP; }
  function pxH(h) { return h * ROW_H + (h - 1) * GAP; }

  /** Applies one card's stored place to its element as absolute px
      left/top/width/height, plus a cheap `order` that is always correct so
      the mobile breakpoint (order-only flex stacking) never needs a drag to
      have happened first. */
  function applyPos(el, id) {
    if (!colWidth) recalcColWidth();
    const p = place(id);
    el.style.order = String(p.y * 1000 + p.x);
    el.style.left = pxX(p.x) + "px";
    el.style.top = pxY(p.y) + "px";
    el.style.width = pxW(p.w) + "px";
    el.style.height = pxH(p.h) + "px";
  }

  /** Absolutely positioned children do not contribute to a `position:
      relative` parent's height on their own -- the grid's own height is set
      explicitly here to the tallest bottom edge among the cards actually on
      the page right now. */
  function syncGridHeight() {
    let maxBottom = 0;
    Array.prototype.forEach.call(grid.children, function (el) {
      const id = el.dataset.card;
      const p = id && layout[id];
      if (!p) return;
      maxBottom = Math.max(maxBottom, pxY(p.y) + pxH(p.h));
    });
    grid.style.height = maxBottom + "px";
  }

  function reflowAll() {
    recalcColWidth();
    Array.prototype.forEach.call(grid.children, function (el) {
      // A soft-hidden thirdparty-template card (see dash-close/addWidget
      // below) stays out of the layout entirely -- no place(), no
      // placeNewCard(), no layout[id] entry -- while display:none, exactly
      // as if it were not on the board.
      if (el.dataset.card && el.dataset.dashHidden !== "1") applyPos(el, el.dataset.card);
    });
    syncGridHeight();
  }

  if (window.ResizeObserver) {
    new ResizeObserver(reflowAll).observe(grid);
  } else {
    // No polyfill needed for this app's supported browsers; a resize
    // listener is still a reasonable fallback for the rare miss.
    window.addEventListener("resize", reflowAll);
  }

  /** Put a card in its place, creating it on first sight and replacing its
      contents on every later one -- a poll must not make the grid jump.
      `body` is the scrollable middle section; `foot` (optional) is the one
      trailing link/button a few cards have ("Alle ansehen ›" etc.) -- kept as
      its own flex child so it always pins to the card's bottom edge instead
      of scrolling away with the list above it, and is simply absent from the
      DOM (not just hidden) when a card has nothing to link to.

      The grip and the resize handle are built ONCE, the first time a card is
      seen, into their own element outside `.dash-card-content` -- everything
      IN `.dash-card-content` (head text, stats, items, foot) is replaced
      wholesale on every call, same as before, but the two handles never are.
      A card whose data refreshes on its own schedule (a module's own poll, a
      linked media server's "continue watching" list, ...) used to lose an
      in-progress drag or resize the moment a refresh landed mid-gesture: the
      handle the pointer had captured was destroyed and rebuilt as a new,
      un-captured element. Every card goes through this one function, built-
      in or module, so fixing it here fixes it for both. */
  function renderOneCard(id, head, body, foot) {
    let el = document.getElementById("dashCard-" + id);
    let content;
    if (!el) {
      el = document.createElement("section");
      el.id = "dashCard-" + id;
      el.className = "dash-card";
      el.dataset.card = id;

      content = document.createElement("div");
      content.className = "dash-card-content";
      el.appendChild(content);

      const grip = document.createElement("button");
      grip.type = "button";
      grip.className = "dash-tool dash-grip";
      grip.title = HT("dash_move");
      grip.setAttribute("aria-label", HT("dash_move"));
      grip.textContent = "☰";
      el.appendChild(grip);

      // Pointer-drag resize (wired below, delegated on the grid). Hidden
      // below 901px via CSS -- the mobile grid is order-only. A real
      // <button>, not a bare span, so it is keyboard-reachable.
      const resize = document.createElement("button");
      resize.type = "button";
      resize.className = "dash-resize";
      resize.title = HT("dash_resize");
      resize.setAttribute("aria-label", HT("dash_resize"));
      el.appendChild(resize);

      // Close ("x"): same permanent-element treatment as the grip and the
      // resize handle -- built once here, never touched by a re-render, so a
      // click mid-poll cannot be swallowed by the element being replaced out
      // from under the pointer.
      const close = document.createElement("button");
      close.type = "button";
      close.className = "dash-tool dash-close";
      close.title = HT("dash_remove");
      close.setAttribute("aria-label", HT("dash_remove"));
      close.textContent = "×";
      el.appendChild(close);

      grid.appendChild(el);
    } else {
      content = el.querySelector(".dash-card-content");
    }
    content.innerHTML = head +
      '<div class="dash-card-body">' + body + '</div>' +
      (foot ? '<div class="dash-card-foot">' + foot + '</div>' : '');
    applyPos(el, id);
    syncGridHeight();
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
      grid.querySelectorAll('[data-card^="' + id + '."]').forEach(function (sib) {
        renderOneCard(sib.dataset.card, head, body, foot);
      });
    }
    return el;
  }

  /** A card heading: icon, title, and an optional quiet note or "open the
      list" link on the right. The move handle used to live inside this
      markup too; it is now built once by card() itself and laid out on top
      via CSS (see .dash-grip in index.css) so it survives every re-render --
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
  function panelBody(data) {
    if (data.error) {
      return { body: '<div class="hp-error">' + esc(HT("panel_unavailable")) + '</div>', foot: "" };
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
    const rendered = panelBody(data);
    card(id, head(title, "", data.icon), rendered.body, rendered.foot);
  }

  // ------------------------------------------------------------- feed cards
  //
  // Gaps, sources and today's calendar come from what home_feed.js already
  // fetched. They are cards on the same grid but not panels: nothing on the
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
      if (old) { old.remove(); forgetCard(id); reflowAll(); }
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
    }).join("");
    card(id,
         head(HT("dash_gaps"), HT("dash_gap_count").replace("{}", String(total)),
              OWN_ICONS.gaps),
         '<ul class="dash-gaps">' + rows + '</ul>',
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
    const rows = list.map(function (s) {
      const down = !!s.error;
      return '<div class="dash-src"><span>' + esc(s.label || s.id) + '</span>' +
        pill(down ? HT("offline") : HT("dash_online"), down ? "warn" : "ok") + '</div>';
    }).join("");
    card(id,
         head(HT("sources"),
              HT("dash_sources_online").replace("{}", String(up)).replace("{}", String(list.length)),
              OWN_ICONS.sources),
         '<div class="dash-srcs">' + rows + '</div>');
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
    const body = list.length
      ? '<div class="dash-srcs">' + list.map(function (ev) {
          const slot = ev.is_movie ? HT("movie")
            : (ev.season ? "S" + ev.season + "E" + (ev.episode || "") : "");
          return '<div class="dash-src"><span>' + esc(ev.title) + '</span>' +
            (slot ? pill(slot, "") : "") + '</div>';
        }).join("") + '</div>'
      : '<div class="mf-empty hp-empty">' + esc(HT("dash_empty_today")) + '</div>';
    card(id, head(HT("dash_today"), "", OWN_ICONS.upcoming), body,
         '<a class="hp-more" href="/calendar">' + esc(HT("dash_open_calendar")) + ' ›</a>');
  }

  // ------------------------------------------------------ personal lists
  //
  // Continue watching / watchlist / new in the library. These used to be
  // poster RAILS under the grid (home_feed.js renderPersonal), which meant
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
    if (old) { old.remove(); forgetCard(id); reflowAll(); }
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
    }).join("");
    card(id,
         head(HT("continue_watching"), "", OWN_ICONS["continue"]),
         '<ul class="dash-gaps">' + rows + '</ul>');
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
    const rows = list.slice(0, 8).map(function (it) {
      return listRow(it, it.provider || "", null,
        '<button type="button" class="dash-btn" data-open-series="' +
        esc(it.url || "") + '">' + esc(HT("dash_open")) + '</button>');
    }).join("");
    card(id,
         head(HT("your_watchlist"), "", OWN_ICONS.watchlist,
              { href: "/favourites", label: HT("dash_open_list") }),
         '<ul class="dash-gaps">' + rows + '</ul>');
  }

  function renderNewLibrary(overrideId) {
    const id = overrideId || "newlib";
    if (isHidden(id)) return;
    const list = feedPersonal.library || [];
    if (!list.length) { dropCard_(id); return; }
    const rows = list.slice(0, 8).map(function (it) {
      const sub = it.is_movie ? HT("movie")
        : (it.episodes || 0) + " " + HT("episodes_short");
      return listRow(it, sub, null,
        '<a class="dash-btn" href="/library">' + esc(HT("dash_open")) + '</a>');
    }).join("");
    card(id,
         head(HT("new_in_library"), "", OWN_ICONS.newlib,
              { href: "/library", label: HT("dash_open_list") }),
         '<ul class="dash-gaps">' + rows + '</ul>');
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
  // Delegated on the grid so it survives every re-render of every card.
  [grid].forEach(function (col) {
    col.addEventListener("click", function (ev) {
      const el = ev.target.closest(
        "[data-action],[data-gap-search],[data-gap-ignore],[data-play],[data-open-series],.dash-close");
      if (!el) return;
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
          forgetCard(id);
          HIDDEN.add(id);
          saveHidden();
          compactAll();
          reflowAll();
          return;
        }
        cardEl.remove();
        forgetCard(id);
        if (id.indexOf(".") === -1) {
          // A base id must not just vanish once -- see isHidden()'s callers.
          // An extra instance ("id.2") is never auto-recreated by a poll/
          // load in the first place, so it needs no suppression.
          HIDDEN.add(id);
          saveHidden();
        }
        reflowAll();
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

  // --------------------------------------------------------- arrange mode
  //
  // Pointer Events, not HTML5 drag-and-drop: dnd never fires on touch at
  // all (which is why the previous version needed a whole separate menu for
  // touch users) and has no notion of "how far did the pointer move", which
  // resize needs regardless. Pointer Events unify mouse/touch/pen, so one
  // pair of handlers below covers move AND resize on every input type.
  //
  // Desktop/tablet only: gated on window.innerWidth at the moment of every
  // pointerdown (rechecked live, not cached, since the window can resize) --
  // the mobile grid is order-only flex, where dragging or resizing a card
  // means nothing.
  const DESKTOP_MIN = 900;
  const MAX_Y = 400;
  // Below this many pixels of total pointer movement, a grip pointerdown ->
  // pointerup is treated as a CLICK, not a drag -- see endDrag()'s dragMove
  // branch. Generous enough that a real drag is never mistaken for a click,
  // small enough that an actual click never nudges the card first.
  const CARRY_CLICK_THRESHOLD = 6;

  let dragMove = null;
  let dragResize = null;
  // Click-to-carry state (see startCarry()/commitCarry() below): a card
  // picked up by a plain click on its grip, following the pointer with
  // nothing held down, until the next click drops it. Mutually exclusive
  // with dragMove/dragResize -- only ever one gesture live at a time.
  let carry = null;

  function isLocked() { return grid.classList.contains("is-dash-locked"); }

  /** Shared by every "a card just landed at x,y" path -- a real drag-end, a
      carry commit, and (indirectly, via the same shape) the Arrow-key nudge
      below. One place resolves the drop-point collision, settles the board
      and persists it, so drag and carry cannot drift out of step with each
      other's commit behaviour. */
  function commitMove(id, x, y, w, h) {
    layout[id] = { x: x, y: y, w: w, h: h };
    // Push whatever the drop point now overlaps out of the way first (the
    // spot the user chose wins), then let every card -- including the one
    // just dropped -- settle upward into any gap that leaves. See
    // compactAll()'s own comment for why that settle can only move things
    // up, never sideways or down.
    resolveCollisions(id);
    compactAll();
    reflowAll();
    saveLayout();
  }

  /** Pointermove handler while a card is being carried -- registered on
      `document`, not `grid`, since the cursor can leave the grid's bounds
      while carrying (see startCarry()). Keeps the same cursor-to-corner
      offset recorded at pickup, same rounding-to-grid-unit conversion the
      live drag preview above uses. */
  function onCarryMove(ev) {
    if (!carry) return;
    const gridRect = grid.getBoundingClientRect();
    const leftPx = ev.clientX - gridRect.left - carry.offsetX;
    const topPx = ev.clientY - gridRect.top - carry.offsetY;
    const nx = clamp(Math.round(leftPx / (colWidth + GAP)), 0, COLS - carry.w);
    const ny = clamp(Math.round(topPx / (ROW_H + GAP)), 0, MAX_Y);
    carry.liveX = nx;
    carry.liveY = ny;
    carry.el.style.left = pxX(nx) + "px";
    carry.el.style.top = pxY(ny) + "px";
  }

  /** Escape cancels a carry without moving the card -- the only way to
      abandon one without dropping it wherever the next click happens to
      land. */
  function onCarryKeydown(ev) {
    if (ev.key !== "Escape" || !carry) return;
    const c = carry;
    carry = null;
    document.removeEventListener("pointermove", onCarryMove);
    document.removeEventListener("keydown", onCarryKeydown);
    c.el.classList.remove("is-carrying");
    c.el.style.left = pxX(c.origX) + "px";
    c.el.style.top = pxY(c.origY) + "px";
    // The click that follows (if any) was already scheduled to commit the
    // carry -- commitCarry()'s own `if (!carry) return` guard makes that a
    // silent no-op now, so there is nothing else to unregister here.
  }

  /** Commits the card at its current carried position -- called by the
      next click anywhere in the document after startCarry(). */
  function commitCarry() {
    if (!carry) return;
    const c = carry;
    carry = null;
    document.removeEventListener("pointermove", onCarryMove);
    document.removeEventListener("keydown", onCarryKeydown);
    c.el.classList.remove("is-carrying");
    commitMove(c.id, c.liveX != null ? c.liveX : c.origX, c.liveY != null ? c.liveY : c.origY, c.w, c.h);
  }

  /** Enters carry mode for the card a grip click (not drag) just targeted.
      `d` is the dragMove record endDrag() decided was really a click; `ev`
      is that same pointerup event, used only for its clientX/Y to compute
      the pickup offset. */
  function startCarry(d, ev) {
    const rect = d.el.getBoundingClientRect();
    carry = {
      id: d.id, el: d.el, w: d.w, h: d.h,
      origX: d.x, origY: d.y,
      // Cursor-to-corner offset at pickup, kept for the whole carry so the
      // card does not jump to have the cursor at its corner.
      offsetX: ev.clientX - rect.left,
      offsetY: ev.clientY - rect.top,
    };
    d.el.classList.add("is-carrying");
    document.addEventListener("pointermove", onCarryMove);
    document.addEventListener("keydown", onCarryKeydown);
    // Registered next tick, not synchronously: the click event that is
    // still in flight from this very pointerup/click gesture would
    // otherwise be caught immediately and drop the card right back where
    // it was picked up.
    setTimeout(function () {
      document.addEventListener("click", commitCarry, { once: true });
    }, 0);
  }

  grid.addEventListener("pointerdown", function (ev) {
    if (window.innerWidth <= DESKTOP_MIN || isLocked()) return;
    const grip = ev.target.closest(".dash-grip");
    if (grip) {
      const cardEl = grip.closest("[data-card]");
      if (!cardEl) return;
      const id = cardEl.dataset.card;
      const p = place(id);
      dragMove = {
        id: id, el: cardEl, startClientX: ev.clientX, startClientY: ev.clientY,
        w: p.w, h: p.h, x: p.x, y: p.y,
      };
      cardEl.classList.add("is-interacting");
      try { grip.setPointerCapture(ev.pointerId); } catch (e) { /* fine without capture */ }
      ev.preventDefault();
      return;
    }
    const handle = ev.target.closest(".dash-resize");
    if (handle) {
      const cardEl = handle.closest("[data-card]");
      if (!cardEl) return;
      const id = cardEl.dataset.card;
      const p = place(id);
      dragResize = {
        id: id, el: cardEl, startClientX: ev.clientX, startClientY: ev.clientY,
        x: p.x, y: p.y, w: p.w, h: p.h,
      };
      cardEl.classList.add("is-interacting");
      try { handle.setPointerCapture(ev.pointerId); } catch (e) { /* fine without capture */ }
      ev.preventDefault();
    }
  });

  grid.addEventListener("pointermove", function (ev) {
    // No collision resolution during a live drag -- only on release, so the
    // preview does not itself jump other cards around mid-gesture.
    if (dragMove) {
      const dx = ev.clientX - dragMove.startClientX;
      const dy = ev.clientY - dragMove.startClientY;
      const nx = clamp(Math.round((pxX(dragMove.x) + dx) / (colWidth + GAP)), 0, COLS - dragMove.w);
      const ny = clamp(Math.round((pxY(dragMove.y) + dy) / (ROW_H + GAP)), 0, MAX_Y);
      dragMove.liveX = nx;
      dragMove.liveY = ny;
      dragMove.el.style.left = pxX(nx) + "px";
      dragMove.el.style.top = pxY(ny) + "px";
    } else if (dragResize) {
      // pxW(w) = w*(colWidth+GAP) - GAP, so w = (pxW(w) + GAP) / (colWidth+GAP)
      // exactly -- same identity for pxH/ROW_H -- no fudge factor needed.
      const dx = ev.clientX - dragResize.startClientX;
      const dy = ev.clientY - dragResize.startClientY;
      const nw = clamp(Math.round((pxW(dragResize.w) + dx + GAP) / (colWidth + GAP)), 2, COLS - dragResize.x);
      const nh = clamp(Math.round((pxH(dragResize.h) + dy + GAP) / (ROW_H + GAP)), 3, 80);
      dragResize.liveW = nw;
      dragResize.liveH = nh;
      dragResize.el.style.width = pxW(nw) + "px";
      dragResize.el.style.height = pxH(nh) + "px";
    }
  });

  function endDrag(ev) {
    if (dragMove) {
      const d = dragMove;
      dragMove = null;
      d.el.classList.remove("is-interacting");
      try { ev.target.releasePointerCapture(ev.pointerId); } catch (e) { /* already released */ }
      const dist = Math.hypot(ev.clientX - d.startClientX, ev.clientY - d.startClientY);
      if (dist < CARRY_CLICK_THRESHOLD) {
        // Effectively no drag happened -- treat this as the "pick up" half
        // of click-to-carry instead of committing a (non-)move. See
        // startCarry()'s own comment.
        startCarry(d, ev);
        return;
      }
      commitMove(d.id, d.liveX != null ? d.liveX : d.x, d.liveY != null ? d.liveY : d.y, d.w, d.h);
    } else if (dragResize) {
      const d = dragResize;
      dragResize = null;
      d.el.classList.remove("is-interacting");
      try { ev.target.releasePointerCapture(ev.pointerId); } catch (e) { /* already released */ }
      commitMove(d.id, d.x, d.y, d.liveW != null ? d.liveW : d.w, d.liveH != null ? d.liveH : d.h);
    }
  }
  grid.addEventListener("pointerup", endDrag);
  grid.addEventListener("pointercancel", endDrag);

  /** Keyboard route for both handles -- Arrow keys, one keypress = one
      committed nudge (no separate "drag mode" to toggle first). Replaces
      the old per-card menu's "move up/down"/width/height entries. */
  grid.addEventListener("keydown", function (ev) {
    if (window.innerWidth <= DESKTOP_MIN || isLocked()) return;
    const grip = ev.target.closest(".dash-grip");
    const handle = ev.target.closest(".dash-resize");
    if (!grip && !handle) return;
    let dx = 0, dy = 0;
    if (ev.key === "ArrowLeft") dx = -1;
    else if (ev.key === "ArrowRight") dx = 1;
    else if (ev.key === "ArrowUp") dy = -1;
    else if (ev.key === "ArrowDown") dy = 1;
    else return;
    const cardEl = (grip || handle).closest("[data-card]");
    if (!cardEl) return;
    const id = cardEl.dataset.card;
    const p = place(id);
    ev.preventDefault();
    if (grip) {
      layout[id] = { x: clamp(p.x + dx, 0, COLS - p.w), y: clamp(p.y + dy, 0, MAX_Y), w: p.w, h: p.h };
    } else {
      layout[id] = { x: p.x, y: p.y, w: clamp(p.w + dx, 2, COLS - p.x), h: clamp(p.h + dy, 3, 80) };
    }
    resolveCollisions(id);
    compactAll();
    reflowAll();
    saveLayout();
  });

  // ----------------------------------------------------------- locking
  //
  // Off by default (rearranging is the point of this feature) -- but if the
  // user locks the board, that choice is remembered on the account, same as
  // every other per-account UI preference here.
  const LOCK_PREF = "home_dash_locked";
  let locked = PREFS[LOCK_PREF] === "1";

  function applyLockUI() {
    grid.classList.toggle("is-dash-locked", locked);
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

  /** Whether the Add menu may offer more than one card of `typeId`. Checked
      the same way for a built-in (PLACE table) and a module panel (its
      cached payload's `multi` field) -- see PLACE's own comment. */
  function isMulti(typeId) {
    if (PLACE[typeId] && PLACE[typeId].multi) return true;
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
    grid.querySelectorAll('.dash-card[data-widget-kind="thirdparty-template"][data-dash-hidden="1"]')
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
      // never re-rendered from here. placeNewCard() (called from inside
      // reflowAll() -> applyPos() -> place()) picks a fresh slot since this
      // card has no stored layout entry, exactly like a brand-new card.
      thirdEl.style.display = "";
      delete thirdEl.dataset.dashHidden;
      HIDDEN.delete(typeId);
      saveHidden();
      reflowAll();
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
    if (!addWrap || !addMenu || window.innerWidth <= DESKTOP_MIN || locked) return;
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
  Object.keys(PLACE).forEach(function (id) {
    if (HIDDEN.has(id)) return;
    if (document.getElementById("dashCard-" + id)) return;   // server-rendered already, e.g. a thirdparty template card
    renderOneCard(id, "", loadingBody(), "");
  });

  load(null);
  startPoll();
})();
