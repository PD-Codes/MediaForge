// MediaForge — Service Worker
//
// What changed and why
// --------------------
// This file used to cache two assets and never serve them: there was no
// `fetch` handler at all. MediaForge was therefore a PWA in name only —
// installable to the home screen, able to receive push notifications, and
// completely blank the moment the network hiccupped, because every request
// still went straight to a server that was not answering.
//
// It now handles fetches, with three deliberately different strategies:
//
//   * **Static assets** (/static/…) — cache first, revalidate in the
//     background. Serving a stylesheet from disk is the difference between a
//     page that renders instantly and one that flashes unstyled; the cache is
//     dropped wholesale on activate, so a deploy cannot leave stale assets.
//   * **Navigations** (HTML) — network first, cache as fallback, the offline
//     page if neither answers. A page served from cache while the server is
//     up would show yesterday's queue.
//   * **Everything else, including /api/** — network only, never cached.
//
// That last one is the important one. Caching API responses so the app
// "works offline" is tempting and would be actively harmful: a queue that
// claims three downloads are running while the server is unreachable, or a
// library listing full of files deleted this morning, is worse than an honest
// "you are offline". Stale operational data reads as truth.
//
// Offline *playback* — downloading episodes for a flight — is deliberately
// NOT attempted here. It needs Background Fetch (Chromium only), range-request
// handling and a storage-quota story; a half-built version that silently keeps
// the first 40 MB of a file is worse than not offering it at all.

const CACHE_VERSION = "mediaforge-v6";
const OFFLINE_URL = "/offline";

// The shell: enough to render something recognisable without the network.
// Deliberately short — every entry is fetched on install.
const SHELL = [
  OFFLINE_URL,
  "/static/style.css",
  "/static/mf_components.css",
  "/static/mf_escape.js",
  "/static/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) =>
      // Added one at a time, not with addAll(): addAll is atomic, so a single
      // renamed asset would reject the whole install — and a service worker
      // that never installs is one that never updates either. A miss here
      // simply falls through to the network in the fetch handler.
      Promise.all(SHELL.map((url) => cache.add(url).catch(() => null)))
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  );
});

function isStaticAsset(url) {
  return url.pathname.startsWith("/static/");
}

function isLiveData(url) {
  // Anything whose answer changes while you are looking at it.
  return (
    url.pathname.startsWith("/api/") ||
    url.pathname === "/healthz" ||
    url.pathname === "/readyz" ||
    url.pathname === "/sw.js"
  );
}

/** Cache first, refresh in the background. For assets. */
async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE_VERSION);
  const cached = await cache.match(request);

  const network = fetch(request)
    .then((response) => {
      // Only complete, same-origin, successful responses. A 206 or an opaque
      // cross-origin response stored here would later be served back as if it
      // were the whole file.
      if (response && response.status === 200 && response.type === "basic") {
        cache.put(request, response.clone());
      }
      return response;
    })
    .catch(() => null);

  if (cached) {
    // Do not await the revalidation: the point is to answer immediately.
    return cached;
  }
  const fresh = await network;
  return fresh || Response.error();
}

/** Network first, cache as fallback, offline page as a last resort. */
async function networkFirst(request) {
  const cache = await caches.open(CACHE_VERSION);
  try {
    const response = await fetch(request);
    if (response && response.status === 200 && response.type === "basic") {
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    const offline = await cache.match(OFFLINE_URL);
    if (offline) return offline;
    throw err;
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;

  // GET only. A POST replayed out of a cache would be a download queued twice.
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Never cached and never served stale — see the note at the top.
  if (isLiveData(url)) return;

  // Range requests are video. Passing them through untouched matters: the
  // player relies on the server's own 206 handling, and a service worker
  // "helping" here breaks seeking.
  if (request.headers.has("range")) return;

  if (isStaticAsset(url)) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request));
  }
});

// ── Push notifications ──────────────────────────────────────────────────────

self.addEventListener("push", (event) => {
  let data = { title: "MediaForge", body: "" };
  try {
    data = event.data ? event.data.json() : data;
  } catch (_) {
    data.body = event.data ? event.data.text() : "";
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "MediaForge", {
      body: data.body || "",
      icon: "/static/icon-192.png",
      badge: "/static/icon-192.png",
      tag: "aniworld-download",
      renotify: true,
    })
  );
});

// Click on notification → focus or open the app
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if (client.url.includes(self.location.origin) && "focus" in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow("/");
    })
  );
});
