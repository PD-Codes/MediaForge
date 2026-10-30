(function () {
  function text(de, en) { return window.t(de, en); }
  const cache = new Map();
  const cacheLifetime = 5 * 60 * 1000;
  let active = 0;
  const waiting = [];
  async function acquire(signal) {
    if (signal.aborted) throw new DOMException("Aborted", "AbortError");
    if (active < 3) { active++; return; }
    await new Promise(function (resolve, reject) {
      const entry = { resolve: resolve, reject: reject };
      entry.abort = function () {
        const index = waiting.indexOf(entry);
        if (index !== -1) waiting.splice(index, 1);
        reject(new DOMException("Aborted", "AbortError"));
      };
      signal.addEventListener("abort", entry.abort, { once: true });
      entry.signal = signal;
      waiting.push(entry);
    });
  }
  function release() {
    const next = waiting.shift();
    if (next) {
      next.signal.removeEventListener("abort", next.abort);
      next.resolve();
    } else active--;
  }
  async function request(url, ctx, body) {
    const cacheable = url === "/api/search" || /^\/api\/(series|seasons|episodes|providers)\?/.test(url);
    const key = url + (body ? JSON.stringify(body) : "");
    const cached = cacheable && cache.get(key);
    if (ctx.cancel.signal.aborted) throw new DOMException("Aborted", "AbortError");
    if (cached && cached.expires > Date.now()) return cached.data;
    if (cached) cache.delete(key);
    await acquire(ctx.cancel.signal);
    const controller = new AbortController();
    const abort = function () { controller.abort(); };
    ctx.cancel.signal.addEventListener("abort", abort, { once: true });
    if (ctx.cancel.signal.aborted) controller.abort();
    const timeout = setTimeout(abort, 30000);
    try {
      const response = await fetch(url, {
        method: body ? "POST" : "GET",
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
      if (!response.ok) {
        let message = "HTTP " + response.status;
        if ((response.headers.get("content-type") || "").includes("application/json")) {
          try { const data = await response.json(); if (data.error) message = data.error; } catch (_) {}
        }
        throw new Error(message);
      }
      const data = await response.json();
      if (data.error) throw new Error(data.error);
      if (cacheable) {
        if (cache.size >= 500) cache.delete(cache.keys().next().value);
        cache.set(key, { data: data, expires: Date.now() + cacheLifetime });
      }
      return data;
    } finally {
      clearTimeout(timeout);
      ctx.cancel.signal.removeEventListener("abort", abort);
      release();
    }
  }
  function normalTitle(value) {
    return String(value || "").trim().replace(/\s+/g, " ").toLocaleLowerCase();
  }
  function searchTitle(value) {
    let result = String(value || "").trim();
    const suffix = /\s*(?:[\[{](?:imdb|tmdb|tvdb)(?:id)?[-:=\s]+(?:tt)?\d+[\]}]|\(\d{4}(?:\s*[-–—]\s*(?:\d{4})?)?\))\s*$/i;
    while (suffix.test(result)) result = result.replace(suffix, "").trim();
    return result;
  }
  function imdbId(value) {
    const match = String(value || "").match(/\btt\d+\b/i);
    return match ? match[0].toLowerCase() : "";
  }
  async function sources(ctx) {
    const [catalogue, settings] = await Promise.all([
      request("/api/search/sources", ctx), request("/api/settings/client", ctx),
    ]);
    const enabled = ((settings || {}).sources || {}).enabled || {};
    const order = catalogue.order || [];
    return (catalogue.sources || []).filter(function (source) {
      const flag = enabled[source.id];
      const on = flag === undefined ? !!source.enabled : source.adult ? flag === "1" : flag !== "0";
      return source.searchable !== false && on;
    }).sort(function (a, b) {
      const ia = order.indexOf(a.id), ib = order.indexOf(b.id);
      return (ia < 0 ? order.length : ia) - (ib < 0 ? order.length : ib);
    });
  }
  async function find(item, ctx, catalogue) {
    const keyword = searchTitle(item.title);
    const results = await Promise.allSettled(catalogue.map(async function (source) {
      const data = await request("/api/search", ctx, { keyword: keyword, site: source.id });
      return (data.results || []).filter(function (hit) { return hit.url && !hit.is_movie; })
        .map(function (hit) { return { ...hit, sourceLabel: source.label }; });
    }));
    if (ctx.cancel.signal.aborted) throw new DOMException("Aborted", "AbortError");
    const hits = results.flatMap(function (r) { return r.status === "fulfilled" ? r.value : []; });
    function titleMatches(hit) {
      const key = normalTitle(hit.title);
      return key === normalTitle(item.title) || key === normalTitle(keyword);
    }
    hits.sort(function (a, b) { return Number(titleMatches(b)) - Number(titleMatches(a)); });
    const folderId = item.title.match(/[\[{]imdb(?:id)?[-:=\s]+(tt\d+)[\]}]/i);
    const expectedId = imdbId(item.imdb_id) || imdbId(folderId && folderId[1]);
    let match = expectedId ? -1 : hits.findIndex(titleMatches);
    const conflictingIds = new Set();
    // Check identities in small batches; provider titles can use another language.
    for (let i = 0; expectedId && match === -1 && i < hits.length; i += 3) {
      if (ctx.cancel.signal.aborted) throw new DOMException("Aborted", "AbortError");
      const matches = await Promise.all(hits.slice(i, i + 3).map(async function (hit) {
        try {
          const data = await request("/api/series?url=" + encodeURIComponent(hit.url), ctx);
          const foundId = imdbId(data.imdb_id);
          if (foundId && foundId !== expectedId) conflictingIds.add(hit);
          return !data.is_movie && foundId === expectedId;
        } catch (error) { return false; }
      }));
      const index = matches.indexOf(true);
      if (index !== -1) match = i + index;
    }
    if (ctx.cancel.signal.aborted) throw new DOMException("Aborted", "AbortError");
    if (match === -1) {
      const titleHits = hits.map(function (hit, index) { return titleMatches(hit) && !conflictingIds.has(hit) ? index : -1; }).filter(function (index) { return index !== -1; });
      if (titleHits.length === 1) match = titleHits[0];
    }
    return { hits: hits, match: match, failed: results.some(function (r) { return r.status === "rejected"; }) };
  }
  async function episodes(item, hit, ctx) {
    const missing = item.missing_slots || item.missing || [];
    const ignored = item.ignored_slots || [];
    const data = await request("/api/seasons?url=" + encodeURIComponent(hit.url), ctx);
    const seasons = (data.seasons || []).filter(function (season) {
      return !season.are_movies && missing.some(function (slot) {
        return slot === "S" + season.season_number || slot.startsWith("S" + season.season_number + "E");
      });
    });
    const out = [];
    for (let i = 0; i < seasons.length; i += 3) {
      const batch = await Promise.all(seasons.slice(i, i + 3).map(async function (season) {
        const result = await request("/api/episodes?url=" + encodeURIComponent(season.url), ctx);
        const seasonSlot = "S" + season.season_number;
        return (result.episodes || []).filter(function (ep) {
          const slot = seasonSlot + "E" + ep.episode_number;
          return ep.url && !ep.downloaded && !ignored.includes(slot) && !ignored.includes(seasonSlot) &&
            (missing.includes(slot) || missing.includes(seasonSlot));
        }).map(function (ep) { return { ...ep, slot: seasonSlot + "E" + ep.episode_number }; });
      }));
      out.push(...batch.flat());
    }
    return out;
  }
  function available(episodes, language) {
    return episodes.filter(function (ep) {
      return !ep.languages || !ep.languages.length || ep.languages.includes(language);
    });
  }
  async function providers(episode, ctx) {
    const data = await request("/api/providers?url=" + encodeURIComponent(episode.url), ctx);
    return data.providers || {};
  }
  async function prepare(item, ctx, catalogue) {
    // Try sources in priority order. Batch downloads need one match, not a
    // complete list of every provider that carries the series.
    let result = { match: -1 };
    for (const source of catalogue) {
      result = await find(item, ctx, [source]);
      if (result.match !== -1) break;
    }
    if (result.match === -1) throw new Error(text("Serie nicht eindeutig zugeordnet.", "Series could not be matched unambiguously."));
    const hit = result.hits[result.match];
    const found = await episodes(item, hit, ctx);
    if (!found.length) throw new Error(text("Keine nachladbaren Lücken gefunden.", "No downloadable gaps found."));
    const unavailable = (item.missing_slots || item.missing || []).filter(function (slot) {
      return /^S\d+E\d+$/.test(slot) && !found.some(function (ep) { return ep.slot === slot; });
    });
    return { item: item, hit: hit, episodes: found, unavailable: unavailable };
  }
  async function enqueue(entry, selected, language, provider, ctx) {
    const result = await request("/api/download", ctx, {
      series_url: entry.hit.url, title: entry.hit.title,
      episodes: selected.map(function (ep) { return ep.url; }), language: language, provider: provider,
    });
    for (const key of cache.keys()) {
      if (/^\/api\/(seasons|episodes|providers)\?/.test(key)) cache.delete(key);
    }
    return result;
  }
  window.MFGapDownloads = { request: request, sources: sources, find: find, episodes: episodes,
    available: available, providers: providers, prepare: prepare, enqueue: enqueue };
})();
