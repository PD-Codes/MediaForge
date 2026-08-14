/* ===================================================================
   MediaForge — Home 2.1

   Everything the new home page grew on top of its rows. Deliberately a
   separate file from home_feed.js: that one owns *what is in a row*, this
   one owns the page around the rows and must keep working when the feed
   itself fails.

     Density        comfortable / compact / list, stored on the account
     Keyboard       arrow keys through a row, Enter opens, Home/End jump
     Modes          the kids mode's age ceiling is enforced server-side and
                    needs a PIN to leave (/api/home/mode); the switch only
                    appears once an admin armed it under Settings
     Onboarding     what this instance still needs, dismissable for good

   The monthly recap is NOT here: it is a panel in the button bar (see
   routes/home_panels.py's _panel_wrapped). A recap is something you go and
   look at, not something that interrupts the page you opened to search on.

   Every string comes from window.__HOME_I18N (rendered by index.html
   through Flask-Babel) so this file holds no German/English pair.
   mfEscape (mf_escape.js) is the project's only escaper.
   =================================================================== */

(function () {
  const feed = document.getElementById("homeFeed");
  if (!feed) return;                        // classic layout — nothing to do

  const I18N = window.__HOME_I18N || {};
  function T(key) { return I18N[key] || key; }
  const prefs = window._USER_PREFS || {};

  function savePref(patch) {
    if (typeof window.mfSaveUserPref === "function") window.mfSaveUserPref(patch);
  }

  // ================================================================ density
  // A 4K screen fits about twice the cards a laptop does, and the row height
  // was a single hardcoded size for both. The class goes on <body> because
  // browse cards are styled from cards.css, which knows nothing about the
  // feed -- a body-level modifier is the one hook that reaches all of them.
  const DENSITIES = ["comfortable", "compact", "list"];
  let density = DENSITIES.indexOf(prefs.home_density) !== -1
    ? prefs.home_density : "comfortable";

  function applyDensity() {
    DENSITIES.forEach(function (name) {
      document.body.classList.toggle("home-density-" + name, name === density);
    });
  }

  function setDensity(value) {
    if (DENSITIES.indexOf(value) === -1 || value === density) return;
    density = value;
    applyDensity();
    renderTools();
    savePref({ home_density: density });
  }

  // ================================================================== modes
  // A mode is a saved answer to "what do I want to see right now". The only
  // part of it the server cares about is the age ceiling: everything else is
  // presentation and lives in the same per-account prefs the chips use.
  //
  // MODES is intentionally a fixed list rather than user-defined presets --
  // a custom-preset editor is a settings page, and this is a toolbar.
  const MODES = [
    { id: "", label: T("mode_default"), fsk: "" },
    { id: "kids", label: T("mode_kids"), fsk: "6" },
  ];
  let activeMode = String(prefs.home_mode || "");
  // Whether the mode switch is offered at all. False until an admin has BOTH
  // switched kids mode on and set a PIN (Settings -> Start Page), which is
  // why the toolbar starts without it rather than showing a button that
  // 409s. Set from the server's answer in mfHomeApplyMode().
  let kidsEnabled = false;
  // The ceiling currently in force. Comes from the server (the feed config),
  // not from the mode list, because the server is what enforces it -- reading
  // it back is also how a stale button state corrects itself.
  let maxFsk = "";

  async function setMode(mode, pin) {
    const entry = MODES.filter(function (m) { return m.id === mode; })[0];
    if (!entry || !kidsEnabled) return;
    const body = { mode: entry.id, max_fsk: entry.fsk };
    // Leaving a restricted mode is the only case that needs the PIN, and only
    // when a ceiling is actually in force -- the old code asked whenever the
    // target was less strict than the BUTTON's idea of the current mode, so
    // it prompted on a fresh page with no ceiling at all, and then accepted
    // anything because the server had nothing to compare against.
    if (maxFsk && !entry.fsk) {
      if (pin === undefined) { askPin(mode); return; }
      body.pin = pin;
    }
    try {
      const resp = await fetch("/api/home/mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      let data = {};
      try { data = await resp.json(); } catch (e) { /* empty/HTML error page */ }
      if (!resp.ok) {
        // Say WHICH refusal it was. One message for everything turned a wrong
        // PIN, an expired session and a validation slip into the same silent
        // non-event -- and a button that appears to do nothing is
        // indistinguishable from a broken one.
        if (data.error === "pin") { pinError(T("mode_locked")); return; }
        const why = data.error === "kids-disabled" ? T("mode_disabled")
          : (data.error === "no session" ? T("mode_no_session")
             : T("mode_failed") + " (" + resp.status + ")");
        closePin();
        if (typeof showToast === "function") showToast(why);
        return;
      }
      closePin();
      activeMode = entry.id;
      maxFsk = entry.fsk;
      renderTools();
      renderModeNotice();
      // The ceiling changes what the server is willing to send, so the rows
      // have to be asked again -- re-filtering what is already on the page
      // would only hide it.
      if (typeof window.reloadHomeFeed === "function") window.reloadHomeFeed();
    } catch (e) {
      if (typeof showToast === "function") showToast(T("mode_failed"));
    }
  }

  /** Say that the feed is limited, right above the rows.
      Without this, switching to Kids on a library TMDB has no certifications
      for looks exactly like nothing happening: the ceiling is real, but it
      had nothing to drop, so the page is unchanged and the only feedback was
      a subtly darker pill in the toolbar. */
  function renderModeNotice() {
    // home_feed.js reads this when it draws the chip row: with a ceiling in
    // force the 18+ chip must not look like a switch, because the source
    // behind it is never fetched.
    window.__HOME_MAX_FSK = maxFsk;
    const wrap = document.getElementById("feedAlerts");
    if (!wrap) return;
    const old = wrap.querySelector(".feed-mode-notice");
    if (old) old.remove();
    if (!maxFsk) return;
    const note = document.createElement("div");
    note.className = "feed-alert feed-mode-notice";
    note.innerHTML = '<span class="feed-alert-text">' +
      mfEscape(T("mode_notice").replace("{}", maxFsk)) + "</span>";
    wrap.prepend(note);
  }

  // ------------------------------------------------------------- PIN modal
  // window.prompt() was the wrong control for this: it is unstyled, it cannot
  // mask the digits, it blocks the whole tab, and on a phone it is a system
  // sheet that does not look like it belongs to MediaForge. This is the
  // ordinary overlay the rest of the app uses.
  let pinTarget = null;

  function askPin(mode) {
    const overlay = document.getElementById("homePinOverlay");
    const input = document.getElementById("homePinInput");
    const err = document.getElementById("homePinError");
    if (!overlay || !input) return;
    pinTarget = mode;
    input.value = "";
    if (err) err.textContent = "";
    overlay.style.display = "flex";
    if (typeof window.MFScrollLock === "object" && window.MFScrollLock.lock) {
      window.MFScrollLock.lock();
    }
    input.focus();
  }

  function closePin() {
    const overlay = document.getElementById("homePinOverlay");
    if (!overlay || overlay.style.display === "none") return;
    overlay.style.display = "none";
    pinTarget = null;
    if (typeof window.MFScrollLock === "object" && window.MFScrollLock.unlock) {
      window.MFScrollLock.unlock();
    }
  }

  function pinError(message) {
    const err = document.getElementById("homePinError");
    const input = document.getElementById("homePinInput");
    if (err) err.textContent = message;
    if (input) { input.value = ""; input.focus(); }
  }

  function submitPin() {
    const input = document.getElementById("homePinInput");
    if (!input || pinTarget === null) return;
    setMode(pinTarget, input.value.trim());
  }

  document.addEventListener("click", function (ev) {
    if (ev.target.closest("[data-pin-submit]")) { submitPin(); return; }
    if (ev.target.closest("[data-pin-cancel]") ||
        ev.target.id === "homePinOverlay") closePin();
  });

  document.addEventListener("keydown", function (ev) {
    const overlay = document.getElementById("homePinOverlay");
    if (!overlay || overlay.style.display !== "flex") return;
    if (ev.key === "Escape") closePin();
    else if (ev.key === "Enter") { ev.preventDefault(); submitPin(); }
  });

  /** Called by home_feed.js once the server has said what it is actually
      filtering by. Exported rather than polled: the feed already fetches the
      config, and a second request for the same answer would only be a second
      chance to disagree with it. */
  window.mfHomeApplyMode = function (mode, ceiling, available, kidsLimit) {
    activeMode = String(mode || "");
    maxFsk = String(ceiling || "");
    kidsEnabled = !!available;
    if (kidsLimit) MODES[1].fsk = String(kidsLimit);
    renderTools();
    renderModeNotice();
  };

  // ================================================================== tools
  function renderTools() {
    const wrap = document.getElementById("feedTools");
    if (!wrap) return;
    let html = '<div class="feed-tool-group" role="group" aria-label="' +
      mfEscape(T("density")) + '">';
    DENSITIES.forEach(function (name) {
      html += '<button type="button" class="feed-tool' +
        (name === density ? " is-on" : "") + '" data-density="' + name +
        '" aria-pressed="' + (name === density ? "true" : "false") + '">' +
        mfEscape(T("density_" + name)) + "</button>";
    });
    html += "</div>";

    // No mode group at all until an admin armed it. A switch that always
    // answers "not configured" is worse than no switch.
    if (kidsEnabled) {
      html += '<div class="feed-tool-group" role="group" aria-label="' +
        mfEscape(T("mode_switch")) + '">';
      MODES.forEach(function (mode) {
        html += '<button type="button" class="feed-tool' +
          (mode.id === activeMode ? " is-on" : "") + '" data-mode="' +
          mfEscape(mode.id) + '" aria-pressed="' +
          (mode.id === activeMode ? "true" : "false") + '">' +
          mfEscape(mode.label) + "</button>";
      });
      html += "</div>";
    }
    wrap.innerHTML = html;
  }

  document.addEventListener("click", function (ev) {
    const tool = ev.target.closest("#feedTools .feed-tool");
    if (!tool) return;
    if (tool.dataset.density) setDensity(tool.dataset.density);
    else if (typeof tool.dataset.mode === "string") setMode(tool.dataset.mode);
  });

  // =============================================================== keyboard
  // Rows were reachable by Tab only in the sense that every card is a tab
  // stop -- forty of them before the next row. Arrow keys move within a row,
  // up/down move between rows, and the focus ring is drawn by CSS so this is
  // also what makes the feed usable without a mouse at all.
  function cardsIn(section) {
    return Array.prototype.slice.call(
      section.querySelectorAll(".browse-card, .home-pcard-hit"));
  }

  function focusCard(card) {
    if (!card) return;
    card.setAttribute("tabindex", "0");
    card.focus({ preventScroll: true });
    card.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  feed.addEventListener("keydown", function (ev) {
    const keys = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"];
    if (keys.indexOf(ev.key) === -1) return;
    const card = ev.target.closest(".browse-card, .home-pcard-hit");
    if (!card) return;
    const section = card.closest(".browse-section");
    if (!section) return;
    const siblings = cardsIn(section);
    const at = siblings.indexOf(card);
    ev.preventDefault();

    if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
      const next = at + (ev.key === "ArrowRight" ? 1 : -1);
      if (next >= 0 && next < siblings.length) focusCard(siblings[next]);
      return;
    }
    if (ev.key === "Home" || ev.key === "End") {
      focusCard(siblings[ev.key === "Home" ? 0 : siblings.length - 1]);
      return;
    }
    // Between rows: keep the column where possible, so walking down a page
    // does not always dump you at the start of the next row.
    const sections = Array.prototype.slice.call(
      feed.querySelectorAll(".browse-section")).filter(function (s) {
        return s.style.display !== "none" && cardsIn(s).length;
      });
    const row = sections.indexOf(section);
    const target = sections[row + (ev.key === "ArrowDown" ? 1 : -1)];
    if (!target) return;
    const list = cardsIn(target);
    focusCard(list[Math.min(at, list.length - 1)]);
  });

  // Cards are divs with an onclick, so Enter/Space have to be wired by hand.
  feed.addEventListener("keydown", function (ev) {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const card = ev.target.closest(".browse-card");
    if (!card || card.tagName === "A" || card.tagName === "BUTTON") return;
    ev.preventDefault();
    card.click();
  });

  /** Make the first card of every row reachable with one Tab. The rest are
      reached with the arrow keys -- a roving tabindex, which is what the
      ARIA authoring practices call for and what stops a forty-card row from
      being forty tab stops. */
  function wireTabStops() {
    feed.querySelectorAll(".browse-section").forEach(function (section) {
      const list = cardsIn(section);
      list.forEach(function (el, i) {
        if (el.tagName === "A" || el.tagName === "BUTTON") return;
        el.setAttribute("tabindex", i === 0 ? "0" : "-1");
      });
    });
  }

  // The feed re-renders whenever a row arrives, so the tab stops are
  // re-applied by observation rather than by every caller remembering to.
  new MutationObserver(function () { wireTabStops(); })
    .observe(feed, { childList: true, subtree: true });

  // ============================================================= onboarding
  const ONBOARD_LABELS = {
    sources: "onboard_sources", tmdb: "onboard_tmdb", library: "onboard_library",
    modules: "onboard_modules", mediaplayer: "onboard_mediaplayer",
  };

  async function loadOnboarding() {
    const box = document.getElementById("homeOnboard");
    if (!box) return;
    if ((prefs.home_onboarding_done || "") === "1") return;
    let data;
    try {
      data = await (await fetch("/api/home/onboarding")).json();
    } catch (e) { return; }
    const steps = data.steps || [];
    // Nothing left to do — the checklist removes itself rather than sitting
    // there fully ticked. It comes back on its own if something breaks later.
    if (!steps.length || !data.open) return;
    const done = steps.length - data.open;
    box.innerHTML =
      '<div class="home-onboard-head"><b>' + mfEscape(T("onboard_title")) + "</b>" +
      '<span class="home-onboard-count">' +
      mfEscape(T("onboard_open").replace("{}", String(done)).replace("{}", String(steps.length))) +
      "</span>" +
      '<button type="button" class="home-onboard-hide" data-onboard-hide="1">' +
      mfEscape(T("onboard_dismiss")) + "</button></div>" +
      '<div class="home-onboard-steps">' +
      steps.map(function (step) {
        const label = T(ONBOARD_LABELS[step.key] || step.key);
        return '<a class="home-onboard-step' + (step.done ? " is-done" : "") +
          '" href="' + mfEscape(step.link) + '">' +
          '<span class="home-onboard-tick" aria-hidden="true">' +
          (step.done ? "✓" : "") + "</span>" + mfEscape(label) + "</a>";
      }).join("") + "</div>";
    box.hidden = false;
  }

  document.addEventListener("click", function (ev) {
    if (!ev.target.closest("[data-onboard-hide]")) return;
    const box = document.getElementById("homeOnboard");
    if (box) { box.hidden = true; box.innerHTML = ""; }
    savePref({ home_onboarding_done: "1" });
  });

  // =================================================================== tabs
  // Dashboard / Discover. Two panes rather than one long scroll because the
  // page answers two unrelated questions -- "what is my instance doing" and
  // "what is out there" -- and stacking them meant the second one only ever
  // existed below the fold.
  //
  // Dashboard is the landing tab: it renders from local state and is on
  // screen before a single source has answered. The discovery rows load
  // lazily anyway (home_feed.js observes them), so opening on Dashboard also
  // means a fresh page load fires no scraping requests at all until the user
  // asks for them.
  const TAB_PREF = "home_tab";
  const TABS = { dash: "homePaneDash", disc: "homePaneDisc" };

  function showTab(name, persist) {
    // No bar means no tab UI to represent a switch -- either there is only
    // one section (Dashboard off) or "All in one page" stacks both with
    // nothing to hide. A stray call (a module's deep link, say) must not
    // hide the Dashboard pane just because the pill it would have clicked
    // does not exist.
    if (!document.getElementById("homeTabs")) return;
    if (!TABS[name]) name = "dash";
    // Search results replace the feed (app.js sets body.is-searching and
    // hides #homeFeed). Picking a tab is a request to see that tab, so it
    // leaves the search first rather than switching a pane nobody can see.
    if (typeof window.exitSearch === "function") window.exitSearch();
    // Scoped to the bar on purpose: <body> carries the current tab as an
    // attribute too, and a document-wide [data-home-tab="…"] query matched
    // BODY first (document order) -- so the outgoing tab button never lost
    // its highlight and both tabs looked active at once.
    const bar = document.getElementById("homeTabs");
    Object.keys(TABS).forEach(function (key) {
      const pane = document.getElementById(TABS[key]);
      const btn = bar && bar.querySelector('[data-home-tab="' + key + '"]');
      const on = key === name;
      if (pane) pane.hidden = !on;
      if (btn) {
        btn.classList.toggle("active", on);
        btn.setAttribute("aria-selected", on ? "true" : "false");
      }
    });
    // Which tab is open, for CSS that needs to know (and for anything that
    // wants to read it back). Deliberately a DIFFERENT attribute name from
    // the buttons' data-home-tab -- see above.
    document.body.dataset.homeTabOpen = name;
    if (persist) savePref({ [TAB_PREF]: name });
  }

  function wireTabs() {
    const bar = document.getElementById("homeTabs");
    if (!bar) {
      // No tab bar at all -- two different reasons, and only one of them
      // means "Discover is the only thing there is":
      //   - Dashboard switched off server-side (index.html's dash_enabled)
      //     -- no #homePaneDash either, Discover really is alone.
      //   - "All in one page" (all_in_one) -- #homePaneDash is still there,
      //     just stacked above Discover instead of behind a tab. Leaving
      //     the attribute unset here keeps the dash-lock/add-widget buttons
      //     visible (see their `[data-home-tab-open="disc"]` CSS) and keeps
      //     home_panels.js's poll-pause-while-Discover-is-open check off,
      //     since the Dashboard is in fact on screen the whole time.
      if (!document.getElementById("homePaneDash")) {
        document.body.dataset.homeTabOpen = "disc";
      }
      return;
    }
    bar.addEventListener("click", function (ev) {
      const btn = ev.target.closest("[data-home-tab]");
      if (!btn) return;
      showTab(btn.dataset.homeTab, true);
    });
    // Left/right between the two tabs, the pattern the segmented control
    // already uses elsewhere in the app.
    bar.addEventListener("keydown", function (ev) {
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      const btns = Array.prototype.slice.call(bar.querySelectorAll("[data-home-tab]"));
      const at = btns.indexOf(document.activeElement);
      if (at === -1) return;
      const next = btns[(at + (ev.key === "ArrowRight" ? 1 : btns.length - 1)) % btns.length];
      next.focus();
      showTab(next.dataset.homeTab, true);
      ev.preventDefault();
    });
    // Own preference first, then the instance default from Settings -> Start
    // Page (window.__HOME_TAB_DEFAULT, set by index.html), then "dash".
    showTab(prefs[TAB_PREF] || window.__HOME_TAB_DEFAULT || "dash", false);
  }

  // Let other files (and the Discover-only "for you" block) ask for a tab
  // without importing this one -- home_foryou.js uses it for its deep link.
  window.mfHomeShowTab = function (name) { showTab(name, true); };

  /** The Dashboard is allowed to be empty on a fresh install: no playback
      history, no watchlist, no gaps. Saying so beats an unexplained blank
      pane, and it points at the tab that does have something in it. */
  window.mfHomeSyncDashEmpty = function () {
    const pane = document.getElementById("homePaneDash");
    const empty = document.getElementById("homeDashEmpty");
    if (!pane || !empty) return;
    const anyRow = pane.querySelector(
      '.feed-section:not([style*="display:none"]):not([style*="display: none"])');
    const grid = document.getElementById("homeDashGrid");
    const hasCards = grid && grid.querySelector(".dash-card");
    empty.style.display = (anyRow || hasCards) ? "none" : "";
  };

  // ======================================================= mobile topstrip
  // Phone-only: the search bar starts collapsed into #searchToggleBtn (see
  // index.html/index.css), and the strip shrinks to just the tab pill while
  // scrolling down so the cards below get the vertical space back. Both are
  // gated by CSS at the SAME breakpoint the topstrip already uses for its
  // own phone rules (768px, index.css) -- the classes below are harmless
  // above it since that CSS block only exists inside the query, so there is
  // no need to duplicate the breakpoint check in JS.
  (function wireMobileTopstrip() {
    const strip = document.getElementById("homeTopStrip");
    const toggle = document.getElementById("searchToggleBtn");
    if (!strip || !toggle) return;

    function setSearchOpen(open) {
      strip.classList.toggle("is-search-open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        const input = document.getElementById("searchInput");
        if (input) input.focus();
      }
    }

    toggle.addEventListener("click", function () {
      setSearchOpen(!strip.classList.contains("is-search-open"));
    });
    // Tapping outside the open field collapses it again -- the only way back
    // to the icon button once it has been expanded.
    document.addEventListener("click", function (ev) {
      if (!strip.classList.contains("is-search-open")) return;
      if (ev.target === toggle || toggle.contains(ev.target)) return;
      if (ev.target.closest(".home-searchbar")) return;
      setSearchOpen(false);
    });

    // rAF-throttled scroll direction, same pattern as catalogue.js's own
    // scroll handler: a raw scroll listener fires far more often than the
    // page can usefully repaint for.
    let lastY = window.scrollY;
    let raf = null;
    window.addEventListener("scroll", function () {
      if (raf) return;
      raf = requestAnimationFrame(function () {
        raf = null;
        const y = window.scrollY;
        const goingDown = y > lastY && y > 40;
        if (goingDown && !strip.classList.contains("is-scrolled-down")) {
          setSearchOpen(false);
        }
        strip.classList.toggle("is-scrolled-down", goingDown);
        lastY = y;
      });
    }, { passive: true });
  })();

  // ==================================================================== go
  applyDensity();
  renderTools();
  wireTabs();
  wireTabStops();
  loadOnboarding();
})();
