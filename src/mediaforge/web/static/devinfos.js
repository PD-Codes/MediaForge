// Dev Infos -- sidebar badge poll (mirrors queue.js's seerrBadge IIFE) plus a
// light client-side refresh for the Dev Infos page itself.

// Sidebar badge -- fetch the cached *unread* post count (see db.py's
// get_devinfo_count()) on every page and keep it fresh. Loaded globally (see
// base.html) so the badge works no matter which page is open, same as the
// Seerr badge in queue.js -- always runs unconditionally, no enable/disable
// gate (Dev Infos is always-on). Defined at top level (not inside an IIFE)
// so the mark-as-read handler further down can call it directly for instant
// feedback instead of waiting up to 60s for the next scheduled poll.
async function updateDevInfoBadge() {
  const badge = document.getElementById("devinfoBadge");
  if (!badge) return;
  try {
    const resp = await fetch("/api/devinfos/status");
    if (!resp.ok) return;
    const data = await resp.json();
    const n = data.count || 0;
    badge.textContent = n;
    badge.style.display = n > 0 ? "" : "none";
  } catch (e) { /* ignore -- remote Dev Info server may be unreachable */ }
}

(function startDevInfoBadgePoll() {
  updateDevInfoBadge();
  setInterval(updateDevInfoBadge, 60000); // refresh every 60s
})();

// Dev Infos page -- keep the list itself reasonably fresh without a full
// reload, in case new posts arrive while the page is open.
(function devInfoPageRefresh() {
  const list = document.getElementById("devinfosList");
  if (!list) return; // not on the devinfos page

  function typeLabel(type) {
    if (type === "feature") return t("Feature", "Feature");
    if (type === "fix") return t("Fix", "Fix");
    if (type === "warning") return t("Warnung", "Warning");
    return t("Ankündigung", "Announcement");
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function markReadBtnHtml(post) {
    const isRead = !!post.is_read;
    return (
      '<button type="button" class="devinfo-mark-read-btn" data-id="' + escapeHtml(post.id) + '"' +
        (isRead ? ' disabled' : '') + '>' +
        (isRead ? '✓ ' + t("Gelesen", "Read") : t("Als gelesen markieren", "Mark as read")) +
      '</button>'
    );
  }

  function render(posts) {
    if (!posts || !posts.length) {
      list.innerHTML = '<div class="devinfos-empty">' + t("Noch keine Dev Infos.", "No dev infos yet.") + '</div>';
      return;
    }
    list.innerHTML = posts.map(function (post) {
      const type = post.type || "announcement";
      return (
        '<div class="devinfo-card' + (post.is_read ? ' devinfo-card-read' : '') + '" data-type="' + escapeHtml(type) + '" data-id="' + escapeHtml(post.id) + '">' +
          '<div class="devinfo-card-head">' +
            '<span class="devinfo-tag devinfo-tag-' + escapeHtml(type) + '">' + typeLabel(type) + '</span>' +
            '<span class="devinfo-meta">' +
              (post.author ? '<span class="devinfo-author">' + escapeHtml(post.author) + '</span>' : '') +
              '<span class="devinfo-time">' + escapeHtml(post.formatted_time || post.remote_created_at || "") + '</span>' +
              markReadBtnHtml(post) +
            '</span>' +
          '</div>' +
          '<h3 class="devinfo-title">' + escapeHtml(post.title) + '</h3>' +
          '<div class="devinfo-body devinfo-markdown">' + (post.body_html != null ? post.body_html : escapeHtml(post.body)) + '</div>' +
        '</div>'
      );
    }).join("");
  }

  async function refresh() {
    try {
      const resp = await fetch("/api/devinfos/status");
      if (!resp.ok) return;
      const data = await resp.json();
      render(data.posts || []);
    } catch (e) { /* keep the server-rendered list on failure */ }
  }

  // Mark-as-read button -- event delegation on the list container, so this
  // works both for the template's server-rendered cards on first load AND
  // for cards that render() above replaces wholesale on every refresh cycle
  // (a listener bound directly to a button would be thrown away the next
  // time render() runs).
  list.addEventListener("click", async function (e) {
    const btn = e.target.closest(".devinfo-mark-read-btn");
    if (!btn || btn.disabled) return;
    const id = btn.getAttribute("data-id");
    if (!id) return;
    btn.disabled = true;
    try {
      const resp = await fetch("/api/devinfos/" + encodeURIComponent(id) + "/read", { method: "POST" });
      if (!resp.ok) {
        btn.disabled = false; // let the user try again (e.g. transient network error)
        return;
      }
      const card = btn.closest(".devinfo-card");
      if (card) card.classList.add("devinfo-card-read");
      btn.textContent = "✓ " + t("Gelesen", "Read");
      updateDevInfoBadge(); // instant sidebar update instead of waiting up to 60s
    } catch (e) {
      btn.disabled = false;
    }
  });

  // The template already server-renders the initial list. The page's own
  // visit just asked the poller to refetch immediately (see
  // routes/devinfos.py's devinfos_page()), so poll a couple of times soon
  // after load to pick that up without a full reload, then settle into the
  // normal 60s background cadence for as long as the tab stays open.
  setTimeout(refresh, 2000);
  setTimeout(refresh, 5000);
  setInterval(refresh, 60000);
})();
