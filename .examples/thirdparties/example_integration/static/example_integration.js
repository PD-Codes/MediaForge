// Example Integration — page script (see routes.py and service.py in this
// same folder). Fetches this integration's own cached item list and
// renders it as a small card grid.
//
// This deliberately does NOT reuse the shared app.js helpers (esc(),
// proxyImg(), enrichCardWithTmdb(), ...) even though it could — it's meant
// to be readable in isolation as the simplest possible working example.
// A real integration is free to load static/app.js the same way
// anime_seasons_view.html does and reuse those helpers, e.g. for image
// proxying or HTML-escaping user-facing text.

(function () {
  const loadingEl = document.getElementById("exintLoading");
  const emptyEl = document.getElementById("exintEmpty");
  const gridEl = document.getElementById("exintGrid");
  if (!loadingEl || !emptyEl || !gridEl) return;

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function renderItems(items) {
    gridEl.innerHTML = "";
    items.forEach((item) => {
      const card = document.createElement("div");
      card.className = "exint-card";
      card.innerHTML =
        `<div class="exint-card-title">${escapeHtml(item.title)}</div>` +
        `<div class="exint-card-desc">${escapeHtml(item.description)}</div>`;
      gridEl.appendChild(card);
    });
  }

  async function loadItems() {
    try {
      const resp = await fetch("/api/example-integration/items");
      const data = await resp.json();
      loadingEl.style.display = "none";
      if (!resp.ok || !data.items || !data.items.length) {
        emptyEl.style.display = "flex";
        return;
      }
      gridEl.style.display = "grid";
      renderItems(data.items);
    } catch (e) {
      loadingEl.style.display = "none";
      emptyEl.style.display = "flex";
    }
  }

  loadItems();
})();
