(function () {
  "use strict";

  // Bandeau informatif uniquement : jamais de blocage des alertes/annonces côté client.
  // navigator.onLine donne des faux négatifs (LAN sans Internet, VPN) : on confirme par un vrai fetch.
  var banner = document.getElementById("offline-banner");
  var seq = 0;
  function show(off) {
    document.body.classList.toggle("is-offline", off);
    if (banner) banner.hidden = !off;
  }
  async function probe() {
    var id = ++seq;
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, 3000);
    var off = false;
    try {
      await fetch("/offline", { cache: "no-store", credentials: "omit", signal: ctl.signal });
    } catch (e) { off = true; }
    clearTimeout(timer);
    if (id === seq) show(off);
  }
  window.addEventListener("online", probe);
  window.addEventListener("offline", probe);
  if (navigator.onLine === false) probe();

  // Arrivée depuis le bouton « Alerte » : la grille est directement visible et focalisée.
  if (location.hash === "#alert-grid") {
    var grid = document.getElementById("alert-grid");
    if (grid) {
      grid.scrollIntoView({ block: "start" });
      var first = grid.querySelector(".alert-btn");
      if (first) first.focus({ preventScroll: true });
    }
  }

  // Notifications Web Push (admin/staff) : facultatif, toute erreur est absorbée.
  var pushBtn = document.querySelector("[data-push-toggle]");
  if (pushBtn && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window) {
    var pushIcon = pushBtn.querySelector(".icon");
    var pushLabel = pushBtn.querySelector(".push-label");
    var say = function (msg) { if (window.SoundUI) window.SoundUI.toast(msg); };
    var render = function (on) {
      pushBtn.setAttribute("aria-pressed", on ? "true" : "false");
      pushBtn.title = on ? "Désactiver les notifications" : "Activer les notifications";
      if (pushIcon) pushIcon.textContent = on ? "notifications_active" : "notifications";
      if (pushLabel) pushLabel.textContent = pushBtn.title;
    };
    var b64ToBytes = function (b64) {
      var pad = "=".repeat((4 - (b64.length % 4)) % 4);
      var raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
      return Uint8Array.from(raw, function (c) { return c.charCodeAt(0); });
    };
    var jfetch = function (method, body) {
      return fetch(method === "GET" ? "/push/vapid-public-key" : "/push/subscribe", {
        method: method,
        credentials: "same-origin",
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
      });
    };
    var current = async function () {
      var reg = await navigator.serviceWorker.ready;
      return { reg: reg, sub: await reg.pushManager.getSubscription() };
    };
    (async function init() {
      try {
        var r = await jfetch("GET");
        if (!r.ok || !(await r.json()).enabled) return; // push non configuré côté serveur : bouton masqué
        var st = await current();
        render(!!st.sub && Notification.permission === "granted");
        pushBtn.hidden = false;
      } catch (e) { /* push facultatif */ }
    })();
    var busy = false;
    pushBtn.addEventListener("click", async function () {
      if (busy) return;
      busy = true;
      try {
        var st = await current();
        if (st.sub) {
          var endpoint = st.sub.endpoint;
          await st.sub.unsubscribe();
          await jfetch("DELETE", { endpoint: endpoint });
          render(false);
          say("Notifications désactivées");
          return;
        }
        var perm = await Notification.requestPermission();
        if (perm !== "granted") { say("Notifications refusées par le navigateur"); return; }
        var key = (await (await jfetch("GET")).json()).public_key;
        var sub = await st.reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: b64ToBytes(key),
        });
        var r = await jfetch("POST", sub.toJSON());
        if (!r.ok) { await sub.unsubscribe(); throw new Error("subscribe " + r.status); }
        render(true);
        say("Notifications activées");
      } catch (e) {
        say("Impossible de modifier les notifications");
      } finally {
        busy = false;
      }
    });
  }
})();
