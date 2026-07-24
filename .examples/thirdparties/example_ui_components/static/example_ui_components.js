// Tiny demo-only interactivity for the service-pill gallery on this page.
// A real integration attaching to Notifications gets this exact swap
// behaviour for free from notifications.html's own showService() -- this
// is just a local stand-in so the pills on *this* page do something.
function uicSwitchPill(id, el) {
  document.querySelectorAll("#uicPills .service-pill").forEach(function (btn) {
    btn.classList.remove("active");
  });
  el.classList.add("active");
  document.getElementById("uicPillResult").textContent = "Selected: " + id;
}

/* ── Demo wiring for the .mf-* composite controls (forms.css) ───────────
   Same pattern the Advanced Search uses in web/static/advanced_search.js:
   nothing is bound per element, every interaction is delegated on a
   container, and no value is ever interpolated into an onclick attribute.
   This is demo-only glue — the CSS is core, the behaviour is yours. */
(function () {
  "use strict";

  // Segmented buttons ---------------------------------------------------
  var segmented = document.getElementById("uicSegmented");
  if (segmented) {
    segmented.addEventListener("click", function (e) {
      var btn = e.target.closest(".mf-segmented-btn");
      if (!btn) return;
      segmented.querySelectorAll(".mf-segmented-btn").forEach(function (b) {
        b.classList.toggle("active", b === btn);
      });
      document.getElementById("uicSegmentedResult").textContent = btn.dataset.value;
    });
  }

  // Multi-select dropdown ----------------------------------------------
  var multiselect = document.getElementById("uicMultiselect");
  if (multiselect) {
    var trigger = multiselect.querySelector(".mf-multiselect-trigger");
    var label = multiselect.querySelector(".mf-multiselect-label");
    trigger.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = !multiselect.classList.contains("is-open");
      multiselect.classList.toggle("is-open", open);
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
    });
    multiselect.addEventListener("change", function () {
      var chosen = Array.prototype.map.call(
        multiselect.querySelectorAll('input[type="checkbox"]:checked'),
        function (cb) { return cb.value; }
      );
      label.textContent = chosen.length ? chosen.join(", ") : "Nothing selected";
    });
    document.addEventListener("click", function (e) {
      if (!multiselect.contains(e.target)) multiselect.classList.remove("is-open");
    });
  }

  // Token field ---------------------------------------------------------
  var tokenInput = document.getElementById("uicTokenInput");
  if (tokenInput) {
    var suggestionBox = document.getElementById("uicTokenSuggestions");
    var tokenList = document.getElementById("uicTokenList");
    var POOL = ["Alpha", "Beta", "Gamma", "Cinema", "Comedy"];
    var tokens = [];

    function renderTokens() {
      tokenList.innerHTML = tokens.map(function (name) {
        return '<span class="mf-token"><span>' + name + "</span>" +
          '<button type="button" class="mf-token-remove" data-token-id="' + name + '" aria-label="Remove">' +
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
          '<line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg></button></span>';
      }).join("");
    }

    tokenInput.addEventListener("input", function () {
      var q = tokenInput.value.trim().toLowerCase();
      if (!q) { suggestionBox.classList.remove("is-open"); return; }
      var matches = POOL.filter(function (name) {
        return name.toLowerCase().indexOf(q) !== -1 && tokens.indexOf(name) === -1;
      });
      suggestionBox.innerHTML = matches.length
        ? matches.map(function (name) { return '<div class="mf-token-suggestion" role="option">' + name + "</div>"; }).join("")
        : '<div class="mf-token-suggestion" data-empty="1">No results</div>';
      suggestionBox.classList.add("is-open");
      tokenInput.setAttribute("aria-expanded", "true");
    });

    suggestionBox.addEventListener("click", function (e) {
      var item = e.target.closest(".mf-token-suggestion");
      if (!item || item.dataset.empty) return;
      tokens.push(item.textContent);
      tokenInput.value = "";
      suggestionBox.classList.remove("is-open");
      tokenInput.setAttribute("aria-expanded", "false");
      renderTokens();
    });

    tokenList.addEventListener("click", function (e) {
      var btn = e.target.closest(".mf-token-remove");
      if (!btn) return;
      tokens = tokens.filter(function (name) { return name !== btn.dataset.tokenId; });
      renderTokens();
    });
  }

  // Range slider --------------------------------------------------------
  var range = document.getElementById("uicRange");
  if (range) {
    range.addEventListener("input", function () {
      document.getElementById("uicRangeValue").textContent = range.value;
    });
  }

  // Filter chips --------------------------------------------------------
  var chips = document.getElementById("uicChips");
  if (chips) {
    chips.addEventListener("click", function (e) {
      var btn = e.target.closest(".mf-chip-remove");
      if (!btn) return;
      btn.closest(".mf-chip").remove();
    });
  }

  // Pagination ----------------------------------------------------------
  var pager = document.getElementById("uicPagination");
  if (pager) {
    var TOTAL = 12;
    var current = 3;

    function pageNumbers() {
      var pages = [];
      for (var i = 1; i <= TOTAL; i++) {
        if (i === 1 || i === TOTAL || Math.abs(i - current) <= 2) {
          if (pages.length && i - pages[pages.length - 1] > 1) pages.push("…");
          pages.push(i);
        }
      }
      return pages;
    }

    function render() {
      var html = '<button type="button" class="mf-pagination-btn" data-page="1"' +
        (current === 1 ? " disabled" : "") + ">«</button>";
      html += '<button type="button" class="mf-pagination-btn" data-page="' + (current - 1) + '"' +
        (current === 1 ? " disabled" : "") + ">‹</button>";
      pageNumbers().forEach(function (entry) {
        if (entry === "…") { html += '<span class="mf-pagination-ellipsis">…</span>'; return; }
        html += '<button type="button" class="mf-pagination-page' + (entry === current ? " active" : "") +
          '" data-page="' + entry + '"' + (entry === current ? " disabled" : "") + ">" + entry + "</button>";
      });
      html += '<button type="button" class="mf-pagination-btn" data-page="' + (current + 1) + '"' +
        (current === TOTAL ? " disabled" : "") + ">›</button>";
      html += '<button type="button" class="mf-pagination-btn" data-page="' + TOTAL + '"' +
        (current === TOTAL ? " disabled" : "") + ">»</button>";
      html += '<span class="mf-pagination-jump"><label for="uicPageJump">Page</label>' +
        '<input type="text" id="uicPageJump" inputmode="numeric" value="' + current + '" />' +
        "<span>of " + TOTAL + "</span></span>";
      pager.innerHTML = html;
    }

    pager.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-page]");
      if (!btn || btn.disabled) return;
      current = Math.max(1, Math.min(parseInt(btn.dataset.page, 10) || 1, TOTAL));
      render();
    });

    pager.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" || e.target.id !== "uicPageJump") return;
      e.preventDefault();
      current = Math.max(1, Math.min(parseInt(e.target.value, 10) || 1, TOTAL));
      render();
    });

    render();
  }
})();
