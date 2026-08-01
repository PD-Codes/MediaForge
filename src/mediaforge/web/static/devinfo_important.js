// Dev Info "important" modal -- home page only.
//
// index.html renders the overlay (hidden) whenever app.py's index() found at
// least one *unread* Dev Info post of type "important". This script reveals it,
// keeps it non-dismissible, and on confirm marks every post it showed as read
// before sending the user to the Dev Infos page to read the whole thing.
//
// Deliberately different from devinfo_banner.js next to it: the banner is
// dismissed client-side and remembered in localStorage, which means "I clicked
// it away on this browser". An important notice is the one thing that must not
// be silenced that cheaply, so its only off-switch is the server-side read
// state -- clearing the browser or opening the page elsewhere shows it again
// until the post is genuinely marked read.

(function devInfoImportantModal() {
  const overlay = document.getElementById("devinfoImportantOverlay");
  if (!overlay) return; // no unread important post on this load

  const confirmBtn = document.getElementById("devinfoImportantConfirm");
  if (!confirmBtn) return;

  const devinfosUrl = overlay.getAttribute("data-devinfos-url") || "/devinfos";

  // Every post the dialog is showing -- confirming acknowledges all of them,
  // which is why they are shown together in one dialog rather than queued.
  const postIds = Array.prototype.map
    .call(overlay.querySelectorAll(".devinfo-important-post"), function (el) {
      return el.getAttribute("data-devinfo-id");
    })
    .filter(function (id) { return id; });

  // ---- show ---------------------------------------------------------------
  const previousFocus = document.activeElement;
  overlay.hidden = false;
  document.body.classList.add("modal-open");
  // Focus the only control, so keyboard and screen-reader users land on it
  // instead of somewhere behind the overlay.
  try { confirmBtn.focus({ preventScroll: true }); } catch (e) { confirmBtn.focus(); }

  // ---- keep focus inside --------------------------------------------------
  // One focusable element, so the trap is simply "Tab goes back to it".
  // Escape is swallowed on purpose: there is no dismiss path, and letting Esc
  // close the overlay while the post stays unread would leave the page in a
  // state the server still considers unacknowledged.
  function onKeydown(e) {
    if (e.key === "Tab") {
      e.preventDefault();
      confirmBtn.focus();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
    }
  }
  document.addEventListener("keydown", onKeydown, true);

  function close() {
    document.removeEventListener("keydown", onKeydown, true);
    overlay.hidden = true;
    document.body.classList.remove("modal-open");
    if (previousFocus && typeof previousFocus.focus === "function") {
      try { previousFocus.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
    }
  }

  // ---- confirm ------------------------------------------------------------
  // Bodiless POST with no Content-Type, exactly like devinfos.js's mark-as-read
  // button: app.py's CSRF guard allows those and rejects anything cross-site
  // via Sec-Fetch-Site, so no token plumbing is needed here.
  function markRead(id) {
    return fetch("/api/devinfos/" + encodeURIComponent(id) + "/read", { method: "POST" })
      .catch(function () { /* offline/transient -- see below */ });
  }

  confirmBtn.addEventListener("click", function () {
    if (confirmBtn.disabled) return;
    confirmBtn.disabled = true;

    // Navigate once every mark-as-read call has settled, successfully or not.
    // A failed call is not worth blocking on: the user still gets to the Dev
    // Infos page, the post simply stays unread and the modal returns on the
    // next home page load -- which is the correct outcome for "we could not
    // record that you saw this".
    Promise.all(postIds.map(markRead)).then(function () {
      if (typeof updateDevInfoBadge === "function") {
        // Best-effort: keeps the sidebar badge honest if the navigation below
        // is slow or gets cancelled.
        try { updateDevInfoBadge(); } catch (e) { /* ignore */ }
      }
      close();
      window.location.href = devinfosUrl;
    });
  });

  // Clicks on the backdrop do nothing on purpose (no handler) -- an accidental
  // click next to the dialog must not count as having read a critical notice.
})();
