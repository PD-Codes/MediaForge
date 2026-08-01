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

  // ==================================================================== go
  applyDensity();
  renderTools();
  wireTabStops();
  loadOnboarding();
})();
