(function () {
  "use strict";

  var DEBOUNCE_MS = 300, MIN_CHARS = 2;
  var q = document.getElementById("search-q");
  var results = document.getElementById("results");
  var menu = document.getElementById("play-menu");
  var picker = document.getElementById("queue-player");
  var queue = document.getElementById("queue");
  if (!q || !results || !menu || !picker || !queue) return;

  // --- recherche : debounce, 2 caractères min, requête obsolète annulée ---
  var timer = null, ctrl = null;
  function runSearch() {
    var term = q.value.trim();
    if (ctrl) ctrl.abort();
    if (term.length < MIN_CHARS) { results.innerHTML = ""; return; }
    ctrl = new AbortController();
    var mine = ctrl;
    fetch("/ui/music/search?q=" + encodeURIComponent(term), {
      credentials: "same-origin", headers: { "HX-Request": "true" }, signal: mine.signal,
    }).then(function (r) {
      if (r.status === 401) { location.href = "/login"; return null; }
      return r.text();
    }).then(function (html) {
      if (html !== null && ctrl === mine) results.innerHTML = html;
    }).catch(function (err) {
      if (err.name !== "AbortError") window.SoundUI.toast("Recherche impossible", true);
    });
  }
  q.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(runSearch, DEBOUNCE_MS);
  });

  // --- menu de lecture sur les lecteurs cochés ---
  var pendingUri = null;
  results.addEventListener("click", function (e) {
    var b = e.target.closest("[data-uri]");
    if (!b) return;
    pendingUri = b.dataset.uri;
    document.getElementById("play-menu-title").textContent = b.dataset.name;
    menu.showModal();
  });
  var LABELS = { play: "Lecture lancée", next: "Ajouté en lecture suivante", add: "Ajouté à la file" };
  menu.addEventListener("click", function (e) {
    var b = e.target.closest("[data-opt]");
    if (b === null) { if (e.target === menu) menu.close(); return; }
    menu.close();
    var opt = b.dataset.opt;
    if (!opt || !pendingUri) return;
    window.SoundUI.groupCall("play", { uri: pendingUri, option: opt }, LABELS[opt]);
    setTimeout(function () { window.SoundUI.refresh("#queue"); }, 1200);
  });

  // --- scènes : un clic lance la scène côté serveur (le résultat partiel est signalé) ---
  var LAUNCH_ERRORS = { unreachable: "injoignable", refused: "refusé", error: "erreur" };
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-scene]");
    if (!b || b.disabled) return;
    b.disabled = true;
    fetch("/scenes/" + encodeURIComponent(b.dataset.scene) + "/launch", {
      method: "POST", credentials: "same-origin",
    }).then(function (r) {
      return r.text().then(function (t) {
        var d = {};
        try { d = JSON.parse(t); } catch (err) { /* corps non JSON */ }
        return { ok: r.ok, status: r.status, d: d };
      });
    }).then(function (res) {
      if (res.status === 401) { location.href = "/login"; return; }
      if (!res.ok) {
        window.SoundUI.toast(typeof res.d.detail === "string" ? res.d.detail : "Erreur " + res.status, true);
        return;
      }
      var d = res.d;
      if (d.partial) {
        window.SoundUI.toast(d.scene + " : " + d.failed.length + " lecteur(s) en échec ("
          + d.failed.map(function (f) { return f.player + " " + (LAUNCH_ERRORS[f.error] || f.error); }).join(", ")
          + "), " + d.ok.length + " lancé(s)", true);
      } else {
        window.SoundUI.toast("Scène « " + d.scene + " » lancée");
      }
      setTimeout(function () { window.SoundUI.refresh("#players"); window.SoundUI.refresh("#queue"); }, 1200);
    }).catch(function () {
      window.SoundUI.toast("Serveur injoignable", true);
    }).then(function () { b.disabled = false; });
  });

  // --- groupe d'enceintes : coche ses lecteurs (résolution serveur, enceintes actives) ---
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-pick-group]");
    if (!b) return;
    fetch("/speaker-groups/" + encodeURIComponent(b.dataset.pickGroup) + "/resolve", {
      credentials: "same-origin",
    }).then(function (r) {
      if (r.status === 401) { location.href = "/login"; return null; }
      return r.json().then(function (d) { return { ok: r.ok, d: d }; });
    }).then(function (res) {
      if (!res) return;
      if (!res.ok) {
        window.SoundUI.toast(typeof res.d.detail === "string" ? res.d.detail : "Groupe indisponible", true);
        return;
      }
      var wanted = res.d.player_ids || [], n = 0;
      document.querySelectorAll("#players [data-sel]").forEach(function (box) {
        var on = wanted.indexOf(box.dataset.sel) >= 0;
        box.checked = on;
        if (on) n++;
      });
      if (!n) { window.SoundUI.toast("Aucun lecteur musique dans ce groupe", true); }
      box_changed();
    }).catch(function () { window.SoundUI.toast("Serveur injoignable", true); });
  });
  function box_changed() {
    var first = document.querySelector("#players [data-sel]");
    if (first) first.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // --- file : lecteur choisi parmi les lecteurs cochés ---
  function syncPicker() {
    var ids = window.SoundUI.selected();
    var prev = picker.value;
    picker.innerHTML = "";
    ids.forEach(function (id) {
      var box = document.querySelector('#players [data-sel="' + id + '"]');
      var name = box ? box.getAttribute("aria-label").replace("Sélectionner ", "") : id;
      var o = document.createElement("option");
      o.value = id; o.textContent = name;
      picker.appendChild(o);
    });
    picker.disabled = !ids.length;
    if (ids.indexOf(prev) >= 0) picker.value = prev;
    loadQueue();
  }
  function loadQueue() {
    queue.setAttribute("hx-get", "/ui/music/queue?player_id=" + encodeURIComponent(picker.value));
    htmx.process(queue);
    htmx.trigger(queue, "refresh");
  }
  picker.addEventListener("change", loadQueue);
  document.addEventListener("selection-changed", syncPicker);
  syncPicker();
})();
