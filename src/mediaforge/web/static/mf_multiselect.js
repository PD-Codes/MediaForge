/**
 * Shared behaviour for the .mf-multiselect dropdown (see static/forms.css).
 *
 * forms.css has shipped the styling for this component since the Advanced
 * Search rework, but every caller had to bring its own open/close/label JS —
 * advanced_search.js has a copy inside an IIFE, the example module has
 * another, and settings.js had a row of loose checkboxes instead. This file
 * is the one implementation, loaded once from templates/base.html, so a
 * page (or a third-party module) only has to render the markup.
 *
 * Opt in by putting `data-mf-multiselect` on the root element:
 *
 *   <div class="mf-multiselect" data-mf-multiselect
 *        data-none-label="No site" data-many-label="sites">
 *     <button type="button" class="mf-multiselect-trigger"
 *             aria-expanded="false" aria-haspopup="true">
 *       <span class="mf-multiselect-label">No site</span>
 *       <svg ...><polyline points="6 9 12 15 18 9"/></svg>
 *     </button>
 *     <div class="mf-multiselect-dropdown">
 *       <label class="mf-multiselect-item">
 *         <input type="checkbox" class="chb-main" value="aniworld"><span>AniWorld</span>
 *       </label>
 *     </div>
 *   </div>
 *
 * Everything is document-level delegation, so markup rendered later by
 * page JS (table rows, modals) works without an init call. Opting in
 * deliberately: roots without the attribute keep whatever hand-rolled
 * handlers they already have, so this cannot double-toggle them.
 *
 * The dropdown is switched to position:fixed while open whenever it sits
 * inside a scroll container (.user-table-wrapper is `overflow-x:auto`, which
 * would otherwise clip an absolutely positioned dropdown to the table) and
 * is flipped above the trigger when there is no room below — which is what
 * makes it usable on a phone.
 *
 * Events fired on the root:
 *   mf-multiselect-change  detail: { values, labels }
 *   mf-multiselect-close   detail: { values, labels }  (only after a change)
 *
 * Public helpers: window.mfMultiSelect.values(root) / .labels(root)
 *                 .refresh(root) / .close(root) / .closeAll()
 */
(function () {
  "use strict";

  var GAP = 6; // px between trigger and dropdown
  var MARGIN = 8; // px kept free towards the viewport edges
  var MIN_WIDTH = 220; // matches .mf-multiselect-dropdown min-width
  var MAX_HEIGHT = 280; // matches .mf-multiselect-dropdown max-height

  function isGerman() {
    return window.__LANG === "de";
  }

  function managed(root) {
    return !!(root && root.hasAttribute && root.hasAttribute("data-mf-multiselect"));
  }

  function boxes(root) {
    return Array.prototype.slice.call(
      root.querySelectorAll('.mf-multiselect-dropdown input[type="checkbox"]')
    );
  }

  function checkedBoxes(root) {
    return boxes(root).filter(function (box) {
      return box.checked;
    });
  }

  function values(root) {
    return checkedBoxes(root).map(function (box) {
      return box.value;
    });
  }

  function labels(root) {
    return checkedBoxes(root).map(function (box) {
      var span = box.parentElement && box.parentElement.querySelector("span");
      return (span ? span.textContent : box.value).trim();
    });
  }

  // "nothing" / "A, B" / "3 sites" — kept short so the trigger does not have
  // to ellipsize in a narrow table cell.
  function refresh(root) {
    var target = root.querySelector(".mf-multiselect-label");
    if (!target) return;
    var picked = labels(root);
    var noneLabel = root.dataset.noneLabel || (isGerman() ? "Nichts ausgewählt" : "Nothing selected");
    var manyLabel = root.dataset.manyLabel || (isGerman() ? "ausgewählt" : "selected");
    var maxNames = parseInt(root.dataset.maxNames || "2", 10);
    if (!picked.length) target.textContent = noneLabel;
    else if (picked.length <= maxNames) target.textContent = picked.join(", ");
    else target.textContent = picked.length + " " + manyLabel;
  }

  function detail(root) {
    return { values: values(root), labels: labels(root) };
  }

  function emit(root, name) {
    root.dispatchEvent(
      new CustomEvent(name, { detail: detail(root), bubbles: true })
    );
  }

  // True when any ancestor clips overflow, in which case an absolutely
  // positioned dropdown would be cut off / trapped in that scroll box.
  function insideScrollContainer(el) {
    var node = el.parentElement;
    while (node && node !== document.body && node !== document.documentElement) {
      var style = window.getComputedStyle(node);
      if (/(auto|scroll|hidden)/.test(style.overflowX + style.overflowY)) return true;
      node = node.parentElement;
    }
    return false;
  }

  function resetPosition(dropdown) {
    dropdown.style.position = "";
    dropdown.style.top = "";
    dropdown.style.left = "";
    dropdown.style.width = "";
    dropdown.style.maxHeight = "";
  }

  function position(root) {
    var trigger = root.querySelector(".mf-multiselect-trigger");
    var dropdown = root.querySelector(".mf-multiselect-dropdown");
    if (!trigger || !dropdown) return;
    if (root.dataset.mfFixed !== "1") {
      resetPosition(dropdown);
      return;
    }

    var rect = trigger.getBoundingClientRect();
    var available = window.innerWidth - MARGIN * 2;
    var width = Math.min(Math.max(rect.width, MIN_WIDTH), available);
    var left = Math.max(MARGIN, Math.min(rect.left, window.innerWidth - width - MARGIN));

    dropdown.style.position = "fixed";
    dropdown.style.width = width + "px";
    dropdown.style.left = left + "px";

    // scrollHeight is only meaningful once the dropdown is displayed, which
    // it is: .is-open is set before position() runs.
    var wanted = Math.min(MAX_HEIGHT, dropdown.scrollHeight + 2);
    var below = window.innerHeight - rect.bottom - GAP - MARGIN;
    var above = rect.top - GAP - MARGIN;
    if (wanted > below && above > below) {
      var height = Math.min(wanted, above);
      dropdown.style.top = rect.top - GAP - height + "px";
      dropdown.style.maxHeight = height + "px";
    } else {
      dropdown.style.top = rect.bottom + GAP + "px";
      dropdown.style.maxHeight = Math.max(120, Math.min(wanted, below)) + "px";
    }
  }

  function open(root) {
    closeAll(root);
    var trigger = root.querySelector(".mf-multiselect-trigger");
    root.dataset.mfFixed = insideScrollContainer(root) ? "1" : "0";
    root.classList.add("is-open");
    if (trigger) trigger.setAttribute("aria-expanded", "true");
    position(root);
  }

  function close(root) {
    if (!root.classList.contains("is-open")) return;
    var trigger = root.querySelector(".mf-multiselect-trigger");
    var dropdown = root.querySelector(".mf-multiselect-dropdown");
    root.classList.remove("is-open");
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    if (dropdown) resetPosition(dropdown);
    if (root.dataset.mfDirty === "1") {
      delete root.dataset.mfDirty;
      emit(root, "mf-multiselect-close");
    }
  }

  function closeAll(except) {
    document.querySelectorAll(".mf-multiselect.is-open").forEach(function (root) {
      if (root !== except && managed(root)) close(root);
    });
  }

  document.addEventListener("click", function (ev) {
    var trigger = ev.target.closest && ev.target.closest(".mf-multiselect-trigger");
    var root = trigger && trigger.closest(".mf-multiselect");
    if (root && managed(root)) {
      ev.preventDefault();
      if (trigger.disabled) return;
      if (root.classList.contains("is-open")) close(root);
      else open(root);
      return;
    }
    // A click inside an open dropdown must not close it (multi-select), any
    // other click anywhere on the page does.
    var inside = ev.target.closest && ev.target.closest(".mf-multiselect");
    closeAll(inside && managed(inside) ? inside : null);
  });

  document.addEventListener("change", function (ev) {
    var box = ev.target;
    if (!box.matches || !box.matches('.mf-multiselect-dropdown input[type="checkbox"]')) return;
    var root = box.closest(".mf-multiselect");
    if (!root || !managed(root)) return;
    root.dataset.mfDirty = "1";
    refresh(root);
    emit(root, "mf-multiselect-change");
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    var open_ = document.querySelector(".mf-multiselect.is-open[data-mf-multiselect]");
    if (!open_) return;
    var trigger = open_.querySelector(".mf-multiselect-trigger");
    close(open_);
    if (trigger) trigger.focus();
  });

  // Keep an open dropdown glued to its trigger while the page (or the table
  // it lives in) scrolls. Capture phase so inner scroll containers are seen.
  var frame = null;
  function reposition() {
    if (frame) return;
    frame = window.requestAnimationFrame(function () {
      frame = null;
      document.querySelectorAll(".mf-multiselect.is-open[data-mf-multiselect]").forEach(position);
    });
  }
  window.addEventListener("scroll", reposition, true);
  window.addEventListener("resize", reposition);

  window.mfMultiSelect = {
    values: values,
    labels: labels,
    refresh: refresh,
    open: open,
    close: close,
    closeAll: function () {
      closeAll(null);
    },
  };
})();
