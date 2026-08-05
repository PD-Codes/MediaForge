/* Shared skeleton and empty-state helpers.
 *
 * Loaded from base.html next to mf_escape.js, so window.MFSkeleton is
 * available on every page including inline blocks.
 *
 * Why this is a helper rather than a convention: the markup is a handful of
 * divs, and every page that wrote its own drifted -- different shapes,
 * different counts, and in three places a bare "Loading…" string that made a
 * grid jump when the real cards arrived. One function is one shape.
 *
 * The rule this encodes: a waiting area must look like the thing that is
 * coming. A blank box cannot be told apart from "there is nothing here", and
 * the user cannot decide whether to wait or to leave.
 */
(function () {
  "use strict";

  var esc = window.mfEscape || function (s) { return String(s == null ? "" : s); };

  function cards(count) {
    var out = "";
    for (var i = 0; i < count; i++) {
      out += '<div class="mf-skeleton-card">' +
               '<div class="mf-skeleton mf-skeleton-art"></div>' +
               '<div class="mf-skeleton mf-skeleton-text is-medium"></div>' +
               '<div class="mf-skeleton mf-skeleton-text is-short"></div>' +
             '</div>';
    }
    return out;
  }

  function rows(count) {
    var out = "";
    for (var i = 0; i < count; i++) {
      out += '<div class="mf-skeleton-row">' +
               '<div class="mf-skeleton mf-skeleton-dot"></div>' +
               '<div class="mf-skeleton-lines">' +
                 '<div class="mf-skeleton mf-skeleton-text is-medium"></div>' +
                 '<div class="mf-skeleton mf-skeleton-text is-short"></div>' +
               '</div>' +
             '</div>';
    }
    return out;
  }

  var MFSkeleton = {
    /** Markup for a grid of poster cards. */
    cards: function (count) {
      return '<div class="mf-skeleton-grid">' + cards(count || 12) + '</div>';
    },

    /** Markup for a list of rows. */
    rows: rows,

    /**
     * Put a skeleton into a container.
     *
     * Guarded on the container already holding content: re-rendering a
     * skeleton over a list that is merely refreshing makes the page flash
     * and loses the user's scroll position. A skeleton is for the FIRST
     * load, when there is nothing to preserve.
     */
    show: function (target, kind, count) {
      var el = typeof target === "string" ? document.getElementById(target) : target;
      if (!el) { return false; }
      if (el.getAttribute("data-mf-loaded") === "1") { return false; }
      el.innerHTML = kind === "rows"
        ? MFSkeleton.rows(count || 6)
        : MFSkeleton.cards(count || 12);
      el.setAttribute("aria-busy", "true");
      return true;
    },

    /** Mark a container as loaded, so a later refresh does not re-skeleton it. */
    done: function (target) {
      var el = typeof target === "string" ? document.getElementById(target) : target;
      if (!el) { return; }
      el.setAttribute("data-mf-loaded", "1");
      el.removeAttribute("aria-busy");
    },

    /**
     * Markup for an empty state.
     *
     * `hint` is what to do about it. Optional, because sometimes there is
     * genuinely nothing to do -- but a section that can be empty for a
     * fixable reason should always say what the fix is.
     */
    empty: function (title, hint, action) {
      return '<div class="mf-empty">' +
        '<svg class="mf-empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
          'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
          '<circle cx="12" cy="12" r="9"/><line x1="9" y1="12" x2="15" y2="12"/></svg>' +
        '<div class="mf-empty-title">' + esc(title || "") + '</div>' +
        (hint ? '<div class="mf-empty-hint">' + esc(hint) + '</div>' : '') +
        (action ? '<div class="mf-empty-action">' + action + '</div>' : '') +
        '</div>';
    },
  };

  window.MFSkeleton = MFSkeleton;
})();
