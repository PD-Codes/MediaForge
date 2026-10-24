// PWA: Service Worker registration + Web Push subscription management

(function () {
  if (!("serviceWorker" in navigator)) return;

  let _swReg = null;
  let _vapidPublicKey = null;

  // Register Service Worker at root scope for full Push support
  navigator.serviceWorker
    .register("/sw.js")
    .then((reg) => {
      _swReg = reg;
      // The key is only needed by the push button (notifications page). This
      // file loads on every page, and togglePushSubscription() fetches the key
      // itself when it is missing -- so other pages skip the request.
      if (!document.getElementById("pushBtn")) return null;
      return fetch("/api/push/vapid-public-key").then((r) => r.json());
    })
    .then((data) => {
      if (!data) return;
      _vapidPublicKey = data.vapid_public_key || null;
      _updatePushButtonState();
    })
    .catch((err) => console.debug("[PWA] SW registration failed:", err));

  function _urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(base64);
    return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
  }

  async function _updatePushButtonState() {
    const btn         = document.getElementById("pushBtn");
    const prefsSection = document.getElementById("pushPrefsSection");
    if (!btn || !_swReg) return;
    const sub = await _swReg.pushManager.getSubscription().catch(() => null);
    if (sub) {
      btn.textContent = t("Web Push deaktivieren","Web Push deactivate");
      btn.classList.remove("btn-primary");
      btn.classList.add("push-active");
      if (prefsSection) prefsSection.classList.remove("push-prefs-disabled");
    } else {
      btn.textContent = t("Web Push aktivieren","Web Push activate");
      btn.classList.remove("push-active");
      btn.classList.add("btn-primary");
      if (prefsSection) prefsSection.classList.add("push-prefs-disabled");
    }
    // Also update the overridden handler from notifications.html if present
    if (typeof window._updatePushButtonState === "function" && window._updatePushButtonState !== _updatePushButtonState) {
      window._updatePushButtonState();
    }
  }

  window.togglePushSubscription = async function () {
    if (!_swReg) return;

    if (!_vapidPublicKey) {
      showToast(t("Push-Keys werden generiert, bitte kurz warten …","Push keys are being generated, please wait a moment…"));
      try {
        const data = await fetch("/api/push/vapid-public-key").then((r) => r.json());
        _vapidPublicKey = data.vapid_public_key || null;
      } catch (_) {}
      if (!_vapidPublicKey) {
        showToast(t("Push-Konfiguration fehlgeschlagen. Prüfe Server-Logs.","Push configuration failed. Check server logs."));
        return;
      }
    }

    const existing = await _swReg.pushManager.getSubscription();
    if (existing) {
      // Unsubscribe
      await fetch("/api/push/unsubscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: existing.endpoint }),
      });
      await existing.unsubscribe();
      _updatePushButtonState();
      showToast(t("Push-Benachrichtigungen deaktiviert","Push notifications disabled"));
    } else {
      // Subscribe
      const perm = await Notification.requestPermission();
      if (perm !== "granted") {
        showToast(t("Berechtigung verweigert","Permission denied"));
        return;
      }
      try {
        const sub = await _swReg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: _urlBase64ToUint8Array(_vapidPublicKey),
        });
        await fetch("/api/push/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(sub.toJSON()),
        });
        _updatePushButtonState();
        showToast(t("Push-Benachrichtigungen aktiviert 🔔","Push notifications activated 🔔"));
      } catch (err) {
        console.error("[PWA] Push subscribe failed:", err);
        showToast(t("Push-Abo fehlgeschlagen: " + err.message, "Push subscription failed: " + err.message));
      }
    }
  };
})();
