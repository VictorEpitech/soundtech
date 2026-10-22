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
})();
