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
