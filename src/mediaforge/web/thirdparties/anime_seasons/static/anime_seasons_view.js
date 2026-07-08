// Anime Seasons — grid page for a single season (see routes.py and
// service.py in this same folder). Fetches the Jikan season list for the
// slug baked into the page container and renders it with the same
// `.browse-card` markup/enrichment pipeline the Home/Browse page uses
// (enrichCardWithTmdb, _crEnrichCard, renderBrowseHoverCards — all defined
// in the shared static/app.js, loaded before this file, see
// anime_seasons_view.html), so CineInfo (TMDB) and Crunchyroll/
// Fernsehserien.de pills "just work" for these titles too.
//
// Clicking a card does NOT try to open a local series (these titles come
// from MyAnimeList, not from a configured source site) — instead it opens
// the same cross-provider search modal Advanced Search uses
// (openAniSearchModal, defined in app.js), which searches AniWorld/S.to/
// FilmPalast/MegaKino for the title and lets the user open the normal
// download modal from a matching result.

(function () {
  const container = document.querySelector(".aniseason-page-container[data-slug]");
  if (!container) return;
  const slug = container.dataset.slug;

  const loadingEl = document.getElementById("aniSeasonLoading");
  const emptyEl = document.getElementById("aniSeasonEmpty");
  const gridEl = document.getElementById("aniSeasonGrid");

  function jikanTypeToTmdb(item) {
    return (item.type || "").toLowerCase() === "movie" ? "movie" : "tv";
  }

  function renderSeasonCards(items) {
    gridEl.innerHTML = "";
    items.forEach((item) => {
      const card = document.createElement("div");
      card.className = "browse-card";
      card.dataset.title = item.title || "";
      card.dataset.poster = item.poster || "";
      card.style.cursor = "pointer";
      // Search with the same sequel-marker-stripped title used for TMDB
      // (item.tmdb_query, e.g. "Re:Zero kara Hajimeru Break Time 4th Season"
      // -> "Re:Zero kara Hajimeru Break Time") rather than the raw MAL
      // title: AniWorld/S.to/FilmPalast/MegaKino host every season of a
      // show under one series page, so the shorter base title both matches
      // more reliably and reads better in the "Suche nach ..." modal title.
      //
      // Pass along the TMDB id too, if enrichCardWithTmdb (below) has
      // already resolved one for this card by the time it's clicked —
      // openAniSearchModal/runAniSearch (app.js) then also searches under
      // the localized (German) TMDB title, not just the MAL title, which
      // matters a lot here since AniWorld/S.to are German sites and MAL's
      // title is usually English/Romaji.
      //
      // Also pass item.title_localized as presetLocalizedTitle: our
      // self-hosted jikan-rest instance already resolves a German title
      // itself (see service.py's _normalize_entry), so this search variant
      // works even when MediaForge's own TMDB integration isn't configured
      // — unlike the tmdbId-based lookup above, which needs it.
      card.onclick = () => {
        const tmdbId = card.dataset.tmdbId ? parseInt(card.dataset.tmdbId, 10) : null;
        openAniSearchModal(item.tmdb_query || item.title || item.title_english || "", tmdbId, jikanTypeToTmdb(item), item.poster || "", item.title_localized || "");
      };

      const metaBits = [];
      if (item.score) metaBits.push("★ " + parseFloat(item.score).toFixed(1));
      if (item.episodes) metaBits.push(item.episodes + " " + t("Folgen", "eps"));
      else if (item.status) metaBits.push(esc(item.status));

      // "card-top-badge" on the Neu span is the same stacking marker
      // addDownloadedBadge/addSyncBadgeForTmdb (static/app.js) use for
      // Vorhanden/Sync -- all three share one top-right vertical stack, in
      // whatever order they get attached: Neu is already in the DOM (built
      // via innerHTML, right here) by the time those two run below, so it
      // always ends up on top, Vorhanden next, Sync last.
      card.innerHTML =
        `<img src="${esc(proxyImg(item.poster))}" alt="" loading="lazy" onload="this.parentElement.classList.add('loaded')" onerror="this.parentElement.classList.add('loaded'); this.style.display='none'">` +
        (item.is_new ? `<span class="aniseason-new-badge card-top-badge">${t("Neu", "New")}</span>` : "") +
        `<div class="browse-info">` +
        `<div class="browse-title" title="${esc(item.title)}">${esc(item.title)}</div>` +
        `<div class="browse-genre">${esc((item.genres || []).join(", "))}</div>` +
        (metaBits.length ? `<div class="browse-genre" style="opacity:0.65">${esc(metaBits.join(" · "))}</div>` : "") +
        `</div>`;

      gridEl.appendChild(card);

      // Vorhanden/Sync pills -- same functions/data sources the Home page
      // uses (see app.js). These items have no local source URL (they come
      // from MyAnimeList, not a configured source site), so the Tmdb-title
      // variant of the sync check is used instead of the url-based one.
      //
      // Downloaded folders and AutoSync jobs are named after whatever title
      // AniWorld/S.to actually showed when the user downloaded/synced them
      // -- usually the localized (German) title, NOT MyAnimeList's romaji/
      // English one. Checking only item.title missed already-downloaded
      // entries whenever the two titles differ (e.g. MAL "Yani Neko" vs. the
      // downloaded folder "Chainsmoker Cat"), so try every title variant we
      // have, most-likely-to-match first: title_localized, tmdb_query (the
      // sequel-marker-stripped MAL title), the raw MAL title, then English.
      const titleCandidates = [item.title_localized, item.tmdb_query, item.title, item.title_english];
      addDownloadedBadgeMulti(card, titleCandidates);
      addSyncBadgeForTmdbMulti(card, titleCandidates);

      // Same lazy CineInfo/Crunchyroll/Fernsehserien.de pipeline the Home
      // page cards use — no TMDB key just falls back to the CR/FS pill path.
      // Query with tmdb_query (the title minus any "II"/"2nd Season"/"Part 2"
      // sequel marker, see service.py's _split_sequel_marker) rather than the
      // raw MAL title — TMDB groups every season of an anime under one base
      // entry, so searching with the sequel suffix still attached often
      // returns zero results (e.g. "Youjo Senki II" finds nothing, "Youjo
      // Senki" does).
      enrichCardWithTmdb(card, item.tmdb_query || item.title || item.title_english || "");
    });
  }

  async function loadSeasonAnime() {
    try {
      // loadDownloadedFolders()/loadAutoSyncJobs() populate the globals
      // (downloadedFolders, mediascanTitles, autoSyncUrlMap) that
      // addDownloadedBadgeMulti/addSyncBadgeForTmdbMulti below read from --
      // every other page awaits these two before rendering cards (see
      // app.js's own page-init call sites), but this page previously only
      // waited for the CineInfo/general settings, so Vorhanden/Sync always
      // evaluated against still-empty data and never matched anything.
      await Promise.all([
        (typeof loadDownloadedFolders === "function") ? loadDownloadedFolders() : Promise.resolve(),
        (typeof loadAutoSyncJobs === "function") ? loadAutoSyncJobs() : Promise.resolve(),
        (typeof loadCineinfoSettings === "function") ? loadCineinfoSettings() : Promise.resolve(),
        (typeof loadGeneralSettings === "function") ? loadGeneralSettings() : Promise.resolve(),
      ]);
    } catch (e) { /* best-effort */ }

    try {
      const resp = await fetch("/api/anime-seasons/" + encodeURIComponent(slug));
      const data = await resp.json();
      loadingEl.style.display = "none";
      if (!resp.ok || !data.items || !data.items.length) {
        emptyEl.style.display = "flex";
        return;
      }
      gridEl.style.display = "grid";
      renderSeasonCards(data.items);
    } catch (e) {
      loadingEl.style.display = "none";
      emptyEl.style.display = "flex";
    }
  }

  loadSeasonAnime();
})();
