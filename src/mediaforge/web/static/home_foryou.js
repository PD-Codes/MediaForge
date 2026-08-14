/* ===================================================================
   MediaForge — "Could be for you"

   The one row on the home page made of titles the user does NOT have.
   Everything else on this page either lists their own material or lists
   what a source published; this asks "given what is on the shelf, what is
   missing from it".

   The candidates come from the TMDB recommendations already cached for the
   titles in the library, so the row costs no extra traffic. Only the five
   hero entries are looked up in full (backdrop, plot, genres) and those are
   cached server-side for a day. See web/recommend.py:for_you().

   Two states, and which one applies is a SERVER answer:
     configured    hero + rail
     not           a setup prompt naming what is actually missing

   Guessing that client-side would flash a "set up CineInfo" panel at every
   user who already did. Hence both start hidden in the template.

   Every string comes from window.__HOME_I18N (rendered by index.html
   through Flask-Babel). mfEscape (mf_escape.js) is the project's only
   escaper -- several of these values end up in attributes, and it escapes
   quotes too.
   =================================================================== */

(function () {
  const block = document.getElementById("fyBlock");
  if (!block) return;                       // classic layout — nothing to do

  const I18N = window.__HOME_I18N || {};
  function T(key) { return I18N[key] || key; }
  function fill(key, a, b) {
    let out = T(key);
    [a, b].forEach(function (value) {
      if (value === undefined) return;
      out = out.replace("{}", String(value));
    });
    return out;
  }

  const hero = document.getElementById("fyHero");
  const dots = document.getElementById("fyDots");
  const gate = document.getElementById("fyGate");
  const section = document.getElementById("fySection");
  const grid = document.getElementById("fyGrid");
  const reasons = document.getElementById("fyReasons");
  const prevBtn = document.getElementById("fyPrev");
  const nextBtn = document.getElementById("fyNext");

  // 7 s: long enough to read a three-line plot, short enough that a page left
  // open cycles through all five before anybody comes back to it.
  const ROTATE_MS = 7000;
  let heroes = [];
  let at = 0;
  let timer = null;

  // Skipped titles are a per-session thing on purpose: the row is rebuilt
  // from the library every six hours anyway, and persisting a "never show me
  // this" list would need a settings page to ever undo it.
  const skipped = {};

  // ------------------------------------------------------------------ hero
  /** One background layer per hero, cross-faded. Layers rather than swapping
      one element's background-image: a swap shows the gap while the next
      image decodes, and on a slow connection that gap is the whole 7 s. */
  function buildLayers() {
    hero.querySelectorAll(".fy-bd").forEach(function (el) { el.remove(); });
    dots.innerHTML = "";
    // Nothing to step through with 0 or 1 slide -- an arrow that does nothing
    // is worse than no arrow.
    if (prevBtn) prevBtn.hidden = heroes.length <= 1;
    if (nextBtn) nextBtn.hidden = heroes.length <= 1;
    // Anchor once: inserting before hero.firstChild inside the loop would
    // reverse the layers, so layer n stopped matching heroes[n].
    const anchor = hero.firstChild;
    heroes.forEach(function (item, i) {
      const layer = document.createElement("div");
      layer.className = "fy-bd";
      if (item.backdrop_url) {
        layer.style.backgroundImage = "url(" + JSON.stringify(item.backdrop_url) + ")";
      }
      hero.insertBefore(layer, anchor);

      const dot = document.createElement("button");
      dot.type = "button";
      dot.className = "fy-dot";
      dot.setAttribute("role", "tab");
      dot.setAttribute("aria-label", item.title || "");
      dot.addEventListener("click", function () { show(i); start(); });
      dots.appendChild(dot);
    });
  }

  /** Why this title is being suggested, in the user's own inventory's terms.
      A recommendation whose reason you cannot see is a recommendation you
      cannot argue with -- and this one is a guess, so it had better say
      whose fault it is. */
  function reasonFor(item) {
    const seeds = item.reason_seeds || [];
    if (seeds.length >= 2) return fill("fy_because_two", seeds[0], seeds[1]);
    if (seeds.length === 1) return fill("fy_because_one", seeds[0]);
    if (item.genre) return fill("fy_because_genre", item.score || 1, item.genre);
    return "";
  }

  function show(i) {
    if (!heroes.length) return;
    at = ((i % heroes.length) + heroes.length) % heroes.length;
    const item = heroes[at];

    hero.querySelectorAll(".fy-bd").forEach(function (el, n) {
      el.classList.toggle("is-on", n === at);
    });
    Array.prototype.forEach.call(dots.children, function (el, n) {
      el.classList.toggle("is-on", n === at);
      el.setAttribute("aria-selected", n === at ? "true" : "false");
    });

    document.getElementById("fyTitle").textContent = item.title || "";
    document.getElementById("fyPlot").textContent = item.overview || "";
    document.getElementById("fyWhy").textContent = reasonFor(item);

    const score = document.getElementById("fyScore");
    const pct = matchPercent(item);
    score.hidden = !pct;
    if (pct) {
      score.innerHTML = mfEscape(T("fy_match_label")) + " <b>" +
        mfEscape(String(pct)) + " %</b>";
    }

    const bits = [];
    if (item.year) bits.push(String(item.year));
    if ((item.genres || []).length) bits.push(item.genres.slice(0, 3).join(" · "));
    if (item.vote_average) bits.push("★ " + item.vote_average);
    document.getElementById("fyMeta").innerHTML = bits.map(function (b) {
      return "<span>" + mfEscape(b) + "</span>";
    }).join('<span class="fy-sep">•</span>');

    renderButtons(item);
  }

  /** The three things you can do with a suggestion. "Download now" is a
      search, not a download: MediaForge has no idea yet which source carries
      this title, and pretending otherwise would queue a job that fails. */
  function renderButtons(item) {
    const box = document.getElementById("fyBtns");
    box.innerHTML = "";

    const load = document.createElement("button");
    load.type = "button";
    load.className = "fy-btn is-primary";
    load.textContent = T("fy_download");
    load.addEventListener("click", function () { searchFor(item.title); });
    box.appendChild(load);

    if (item.tmdb_id) {
      const details = document.createElement("button");
      details.type = "button";
      details.className = "fy-btn";
      details.textContent = T("fy_details");
      details.addEventListener("click", function () { openDetails(item); });
      box.appendChild(details);
    }

    const skip = document.createElement("button");
    skip.type = "button";
    skip.className = "fy-btn";
    skip.textContent = T("fy_skip");
    skip.addEventListener("click", function () { skip_(item); });
    box.appendChild(skip);
  }

  function matchPercent(item) {
    // The score is "how many library titles pointed here", which is a count
    // and not a percentage. Presenting a raw count as a percentage would be
    // a made-up number, so this maps it onto a band that stays honest: more
    // agreement means a higher figure, but it never claims certainty.
    const seeds = Math.max(1, item.score || 0);
    const vote = Math.min(10, Math.max(0, item.vote_average || 0));
    return Math.min(97, Math.round(55 + Math.min(seeds, 6) * 5 + vote));
  }

  function searchFor(title) {
    const input = document.getElementById("searchInput");
    if (input && typeof window.doSearch === "function") {
      input.value = title;
      window.doSearch();
      return;
    }
    window.location.href = "/?q=" + encodeURIComponent(title);
  }

  function openDetails(item) {
    // MFDetailModal is the app's shared detail sheet (templates/mf_detail_modal.html).
    // Missing means shared_modals.html has not loaded -- fall through to the
    // search rather than swallowing the click.
    if (window.MFDetailModal && typeof window.MFDetailModal.openTmdb === "function") {
      window.MFDetailModal.openTmdb(item.tmdb_id, item.media_type || "tv", item.title);
      return;
    }
    searchFor(item.title);
  }

  function skip_(item) {
    skipped[String(item.tmdb_id || item.title)] = true;
    heroes = heroes.filter(function (h) { return !skipped[String(h.tmdb_id || h.title)]; });
    if (typeof window.showToast === "function") window.showToast(T("fy_skipped"));
    if (!heroes.length) { stop(); hero.hidden = true; return; }
    buildLayers();
    show(at);
    start();
    renderRail(lastItems);
  }

  function start() {
    stop();
    if (heroes.length > 1) {
      timer = setInterval(function () { show(at + 1); }, ROTATE_MS);
    }
  }
  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  // A rotator that keeps running in a background tab burns work nobody sees,
  // and comes back mid-fade when the tab is restored.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stop(); else if (heroes.length) start();
  });
  hero.addEventListener("mouseenter", stop);
  hero.addEventListener("mouseleave", function () { if (heroes.length) start(); });

  // Same wraparound show() already does for a dot click -- no second copy of
  // the modulo math, just a different starting index.
  if (prevBtn) prevBtn.addEventListener("click", function () { show(at - 1); start(); });
  if (nextBtn) nextBtn.addEventListener("click", function () { show(at + 1); start(); });

  // --------------------------------------------------------------- reasons
  /** Chips that narrow the rail to one reason.

      The mockup also showed "same studio", "similar viewers" and "completes
      a series". None of the three has anything behind it: the candidates
      carry tmdb_id, title, poster, vote, score, reason_seeds and one genre —
      no production companies, no cross-household signal, no franchise graph.
      A chip that filters on data the server does not send is a button that
      lies, so only "all" and the genres that actually occur are rendered. */
  let activeGenre = "";

  function renderReasons(items) {
    const counts = {};
    (items || []).forEach(function (item) {
      if (item.genre) counts[item.genre] = (counts[item.genre] || 0) + 1;
    });
    const genres = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; });
    if (!genres.length) { reasons.hidden = true; return; }
    if (genres.indexOf(activeGenre) === -1) activeGenre = "";

    reasons.innerHTML = "";
    [""].concat(genres.slice(0, 5)).forEach(function (genre) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "fy-r" + (genre === activeGenre ? " is-on" : "");
      chip.setAttribute("aria-pressed", genre === activeGenre ? "true" : "false");
      chip.textContent = genre ? fill("fy_reason_genre", genre) : T("fy_reason_all");
      chip.addEventListener("click", function () {
        activeGenre = genre;
        renderReasons(lastAll);
        renderRail(lastAll);
      });
      reasons.appendChild(chip);
    });
    reasons.hidden = false;
  }

  // ------------------------------------------------------------------ rail
  let lastItems = [];
  // The unfiltered set, so the chips can be rebuilt after a genre filter has
  // already thinned lastItems down.
  let lastAll = [];

  function renderRail(items) {
    lastItems = items || [];
    const list = lastItems.filter(function (item) {
      if (activeGenre && item.genre !== activeGenre) return false;
      return !skipped[String(item.tmdb_id || item.title)];
    });
    if (!list.length) { section.hidden = true; return; }
    section.hidden = false;
    grid.innerHTML = "";
    list.forEach(function (item) {
      const card = document.createElement("div");
      card.className = "browse-card fy-card";
      card.tabIndex = 0;
      const sub = [];
      if (item.genre) sub.push(item.genre);
      // The "%" is added here, not in the catalogue: a bare percent sign in
      // a msgid is read as a format spec by newstyle gettext and takes the
      // whole page down with a ValueError.
      sub.push(fill("fy_match", matchPercent(item) + " %"));
      card.innerHTML =
        // TMDB knows plenty of titles it has no artwork for. The placeholder
        // keeps the card the same height as its neighbours instead of
        // collapsing it to two lines of text.
        (item.poster_url
          ? '<img src="' + mfEscape(item.poster_url) + '" alt="" loading="lazy"' +
            " onload=\"this.parentElement.classList.add('loaded')\"" +
            " onerror=\"this.parentElement.classList.add('loaded');" +
            " this.outerHTML='<div class=&quot;fy-card-art&quot;></div>'\">"
          : '<div class="fy-card-art"></div>') +
        '<span class="fy-card-add" aria-hidden="true">+</span>' +
        '<div class="browse-info">' +
        '<div class="browse-title">' + mfEscape(item.title || "") + "</div>" +
        '<div class="browse-genre">' + mfEscape(sub.join(" · ")) + "</div>" +
        "</div>";
      card.title = reasonFor(item);
      card.addEventListener("click", function () { searchFor(item.title); });
      card.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); searchFor(item.title); }
      });
      grid.appendChild(card);
    });
  }

  // ------------------------------------------------------------------ gate
  /** What is missing, named. "Not configured" tells somebody nothing about
      which of the two halves of CineInfo they still owe it. */
  function renderGate(data) {
    const missing = [];
    missing.push({ ok: true, text: T("fy_gate_ok_library") });
    missing.push({ ok: false, text: T("fy_gate_no_key") });
    if (data && data.has_sources === false) {
      missing.push({ ok: false, text: T("fy_gate_no_source") });
    }
    gate.innerHTML =
      '<div class="fy-gate-in">' +
      '<h2 class="fy-gate-title">' + mfEscape(T("fy_gate_title")) + "</h2>" +
      '<p class="fy-gate-text">' + mfEscape(T("fy_gate_text")) + "</p>" +
      '<div class="fy-gate-req">' +
      missing.map(function (m) {
        return '<span class="fy-gate-chip' + (m.ok ? " is-ok" : "") + '">' +
          (m.ok ? "✓" : "✗") + " " + mfEscape(m.text) + "</span>";
      }).join("") +
      "</div>" +
      '<div class="fy-gate-btns">' +
      '<a class="fy-btn is-primary" href="/settings#cineinfo">' +
      mfEscape(T("fy_gate_setup")) + "</a>" +
      '<a class="fy-btn" href="/settings#cineinfo">' +
      mfEscape(T("fy_gate_what")) + "</a>" +
      "</div></div>";
    gate.hidden = false;
    reasons.hidden = true;
    block.hidden = false;
  }

  // -------------------------------------------------------------------- go
  async function load(refresh) {
    let data;
    try {
      const res = await fetch("/api/home-feed/foryou" + (refresh ? "?refresh=1" : ""));
      data = await res.json();
    } catch (err) {
      // A failed recommendation row is not worth a banner: the rest of the
      // Discover tab is unaffected, and the section simply stays away.
      block.hidden = true;
      return;
    }

    if (!data || data.configured === false) {
      hero.hidden = true;
      section.hidden = true;
      stop();
      renderGate(data);
      return;
    }

    gate.hidden = true;
    const items = (data.items || []).filter(function (item) {
      return !skipped[String(item.tmdb_id || item.title)];
    });
    // No backdrop filter: the hero is the head of the rail and must stay in
    // that order. buildLayers copes with a missing backdrop_url.
    heroes = (data.hero || []).filter(function (item) {
      return !skipped[String(item.tmdb_id || item.title)];
    });

    if (!items.length && !heroes.length) {
      // Configured, and still nothing to show. That is common rather than
      // exotic: tmdb_cache rows written before the `recommendations` field
      // existed carry an empty list, so a fully set-up instance can answer
      // "configured: true" with no candidates at all. Hiding the block left
      // the user with a blank tab and no reason for it -- say so instead.
      // Rendered into the gate node because it is the block's one prose slot;
      // renderGate() overwrites it whenever the setup prompt applies.
      hero.hidden = true;
      section.hidden = true;
      reasons.hidden = true;
      stop();
      // The message itself says "...hit Shuffle" -- #fyReroll is real, but it
      // lives in #fySection's heading, which is hidden right above (nothing
      // to show yet, no rail to put a heading over). Without a button HERE
      // too, the text names a control the user cannot actually see.
      gate.innerHTML = '<div class="fy-gate-in"><p class="fy-gate-text">' +
        mfEscape(T("fy_empty")) + '</p>' +
        '<button type="button" class="fy-btn is-primary fy-gate-reroll">' +
        mfEscape(T("fy_reroll")) + "</button></div>";
      gate.hidden = false;
      block.hidden = false;
      return;
    }
    block.hidden = false;

    if (heroes.length) {
      hero.hidden = false;
      buildLayers();
      show(0);
      start();
    } else {
      hero.hidden = true;
      stop();
    }
    lastAll = items;
    renderReasons(items);
    renderRail(items);
  }

  const reroll = document.getElementById("fyReroll");
  if (reroll) {
    reroll.addEventListener("click", function () {
      reroll.disabled = true;
      load(true).finally(function () { reroll.disabled = false; });
    });
  }
  // The empty-state's own Shuffle button (see the gate.innerHTML branch in
  // load() above) is rebuilt from scratch on every render, so it is wired
  // through delegation on the stable #fyGate node rather than a direct
  // listener that would need re-attaching every time.
  gate.addEventListener("click", function (ev) {
    const btn = ev.target.closest(".fy-gate-reroll");
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    load(true).finally(function () { btn.disabled = false; });
  });

  window.mfReloadForYou = function () { return load(false); };
  load(false);
})();
