// ============================================================
// Library hub — the overview tiles
// ============================================================
// The tiles themselves are rendered server-side (templates/library_hub.html)
// so navigation works the instant the page paints, and so the labels stay in
// the template where pybabel can extract them. This file only fills in the
// counters, which need a cache read the navigation must not wait on.

// Same thresholds and the same comma decimal as libFmtSize() in
// library_core.js. Duplicated rather than shared because the hub deliberately
// does not load the shelf core -- one small function is a better trade than
// pulling 600 lines of grid/pagination machinery into a page with neither.
function libHubFmtSize(bytes) {
  if (!bytes) return "";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return Math.round(bytes / 1024) + " KB";
  if (bytes < 1073741824) return Math.round(bytes / 1048576) + " MB";
  var gb = bytes / 1073741824;
  if (gb >= 1024) {
    var tb = gb / 1024;
    return String(tb >= 10 ? Math.round(tb) : parseFloat(tb.toFixed(1))).replace(".", ",") + " TB";
  }
  var val = gb >= 10 ? Math.round(gb) : parseFloat(gb.toFixed(1));
  return String(val).replace(".", ",") + " GB";
}

function libHubNum(n) {
  return Number(n || 0).toLocaleString(window.__LANG === "de" ? "de-DE" : "en-US");
}

// One tile's stat line. Returns [] when the shelf is empty, so the caller can
// tell "nothing here yet" from "n items" and say so instead of printing "0".
function libHubStatParts(slug, c) {
  var parts = [];
  if (slug === "video") {
    if (c.primary)   parts.push(libHubNum(c.primary) + " " +
                                (c.primary === 1 ? t("Titel", "title") : t("Titel", "titles")));
    if (c.secondary) parts.push(libHubNum(c.secondary) + " " +
                                (c.secondary === 1 ? t("Episode", "episode") : t("Episoden", "episodes")));
  } else if (slug === "book") {
    if (c.primary)   parts.push(libHubNum(c.primary) + " " +
                                (c.primary === 1 ? t("Buch", "book") : t("Bücher", "books")));
  } else if (slug === "comic") {
    // Series first, issues second -- the same order the shelf groups them in,
    // so the tile and the page agree about what the headline number counts.
    if (c.primary)   parts.push(libHubNum(c.primary) + " " +
                                (c.primary === 1 ? t("Reihe", "series") : t("Reihen", "series")));
    if (c.secondary) parts.push(libHubNum(c.secondary) + " " +
                                (c.secondary === 1 ? t("Ausgabe", "issue") : t("Ausgaben", "issues")));
  }
  if (c.size) parts.push(libHubFmtSize(c.size));
  return parts;
}

function libHubPaint(counts, scanning) {
  Object.keys(counts || {}).forEach(function (slug) {
    var host = document.getElementById("libHubStats-" + slug);
    if (!host) return;                       // kind has no tile (not available)
    var parts = libHubStatParts(slug, counts[slug] || {});
    if (!parts.length) {
      // An empty shelf while a scan is running is almost certainly not empty,
      // it is unscanned -- saying "empty" there sends people to check their
      // paths for no reason.
      host.innerHTML = '<span class="lib-hub-stat lib-hub-stat--muted">' +
        window.mfEscape(scanning ? t("wird eingelesen…", "scanning…")
                                 : t("noch nichts hier", "nothing here yet")) + "</span>";
      return;
    }
    host.innerHTML = parts.map(function (p) {
      return '<span class="lib-hub-stat">' + window.mfEscape(p) + "</span>";
    }).join('<span class="lib-hub-dot" aria-hidden="true">·</span>');
  });
}

var _libHubPoll = null;

async function libHubLoad() {
  try {
    var resp = await fetch("/api/library/overview");
    var data = await resp.json();
    libHubPaint(data.counts || {}, !!data.is_scanning);

    // Keep refreshing only while something is actually being scanned. A first
    // run on a large library can take minutes, and a hub that stays on
    // "scanning…" until the user reloads looks broken.
    if (data.is_scanning && !_libHubPoll) {
      _libHubPoll = window.mfPoll(libHubLoad, 4000);
    } else if (!data.is_scanning && _libHubPoll) {
      window.mfPollStop(_libHubPoll);
      _libHubPoll = null;
    }
  } catch (e) {
    // The tiles are links and work without their counters -- a failed count
    // must not put an error banner over working navigation.
    document.querySelectorAll(".lib-hub-stat--placeholder").forEach(function (el) {
      el.remove();
    });
  }
}

libHubLoad();
