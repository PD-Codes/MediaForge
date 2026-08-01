/* ===================================================================
   MediaForge — Your profile (/profile)

   The three things on that page that need JavaScript of their own: the
   password change, the theme-pack choice, and the Jellyfin/Plex profile
   link. Everything else there (dark/light, accent colour) is driven by the
   helpers in base.html, which every page already loads — there is no second
   implementation of them here on purpose.

   All strings come from window.__PROFILE_I18N, rendered by profile.html
   through Flask-Babel.
   =================================================================== */

(function () {
  const I18N = window.__PROFILE_I18N || {};
  function T(key) { return I18N[key] || key; }
  function toast(message) {
    if (typeof showToast === "function") showToast(message);
  }

  // ============================================================== password
  const saveBtn = document.getElementById("pwSave");
  if (saveBtn) {
    const current = document.getElementById("pwCurrent");
    const next = document.getElementById("pwNew");
    const repeat = document.getElementById("pwRepeat");
    const state = document.getElementById("pwState");

    function say(message) { if (state) state.textContent = message; }

    saveBtn.addEventListener("click", async function () {
      // Checked here as well as on the server, but for a different reason:
      // the server never sees the repeat field, so "the two do not match" is
      // a question only this side can answer.
      if (next.value !== repeat.value) { say(T("pw_mismatch")); return; }
      if (next.value.length < 8) { say(T("pw_short")); return; }

      saveBtn.disabled = true;
      say("");
      try {
        const resp = await fetch("/api/user/password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ current: current.value, new: next.value }),
        });
        const data = await resp.json().catch(function () { return {}; });
        if (!resp.ok) {
          say(data.error === "wrong-password" ? T("pw_wrong")
            : (data.error === "unchanged" ? T("pw_unchanged")
               : T("save_failed") + " (" + resp.status + ")"));
          return;
        }
        // Cleared on success: leaving a password sitting in three fields on a
        // page somebody just walked away from is the same risk the current-
        // password prompt exists to cover.
        current.value = next.value = repeat.value = "";
        say(T("pw_changed"));
        toast(T("pw_changed"));
      } catch (err) {
        say(T("save_failed") + ": " + err.message);
      } finally {
        saveBtn.disabled = false;
      }
    });
  }

  // ============================================================ theme pack
  const pack = document.getElementById("profileThemePack");
  if (pack) {
    // The stored value comes from the server-rendered prefs, not from the
    // DOM: the <option> list is built by the template and carries no
    // "selected", so without this the dropdown would claim "instance
    // default" for someone who picked a pack.
    const prefs = window._USER_PREFS || {};
    pack.value = prefs.theme_pack || "";
    pack.addEventListener("change", function () {
      pack.disabled = true;
      fetch("/api/user/preferences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ theme_pack: pack.value }),
      }).then(function (resp) {
        if (!resp.ok) throw new Error(String(resp.status));
        // A theme pack is a stylesheet chosen while the page was built, so
        // it takes a reload rather than a repaint.
        window.location.reload();
      }).catch(function (err) {
        toast(T("save_failed") + ": " + err.message);
        pack.disabled = false;
      });
    });
  }

  // ============================================================== language
  document.querySelectorAll("[data-profile-lang]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      fetch("/api/user/language", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ language: btn.dataset.profileLang }),
      }).then(function () { window.location.reload(); })
        .catch(function (err) { toast(T("save_failed") + ": " + err.message); });
    });
  });

  // =========================================================== media server
  (async function loadPlayer() {
    const select = document.getElementById("profilePlayerUser");
    const section = document.getElementById("profilePlayerSection");
    const state = document.getElementById("profilePlayerState");
    if (!select || !section) return;

    let data;
    try {
      data = await (await fetch("/api/mediaplayer/users")).json();
    } catch (e) { return; }
    if (!data || !data.configured || !(data.users || []).length) return;

    // The menu entry appears with the panel, not before it: a tab that opens
    // an empty section is worse than no tab.
    const tab = document.getElementById("profileTabMediaplayer");
    if (tab) tab.hidden = false;

    // textContent, not innerHTML: these names come from someone else's server.
    (data.users || []).forEach(function (user) {
      const option = document.createElement("option");
      option.value = user.id;
      option.textContent = user.name;
      select.appendChild(option);
    });
    select.value = data.linked || "";
    if (state) state.textContent = data.server || "";
    section.hidden = false;

    select.addEventListener("change", function () {
      select.disabled = true;
      fetch("/api/user/preferences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mediaplayer_user: select.value || "" }),
      }).then(function (resp) {
        if (!resp.ok) throw new Error(String(resp.status));
        toast(T("saved"));
        // The home feed keeps its own copy of the rows, and "Continue
        // watching" is exactly what just changed source.
        if (typeof window.reloadHomeFeed === "function") window.reloadHomeFeed();
      }).catch(function (err) {
        toast(T("save_failed") + ": " + err.message);
      }).then(function () { select.disabled = false; });
    });
  })();
})();
