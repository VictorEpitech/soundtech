(function () {
  "use strict";

  // Recharge la page après une action réussie marquée data-reload (ex. clôture depuis le récap).
  document.body.addEventListener("htmx:afterRequest", function (e) {
    var el = e.detail.elt;
    if (e.detail.successful && el.hasAttribute && el.hasAttribute("data-reload")) location.reload();
  });

  var HOLD_MS = 1200;
  var dlg = document.getElementById("alert-dialog");
  if (!dlg) return;

  var $ = function (id) { return document.getElementById(id); };
  var ui = window.SoundUI;
  var state = { test: false };   // Mode exercice : volontairement non persisté (réel à chaque chargement).
  var current = null;            // { type, test, label } de la confirmation ouverte
  var sending = false;           // un seul envoi en vol : jamais de double envoi
  var done = false;              // alerte envoyée pour cette confirmation
  var sheetLoading = null;

  // --- mode exercice (page /alerts + feuille globale partagent le même état) -------------------

  function switches() {
    return Array.prototype.slice.call(document.querySelectorAll("#is-test, #sheet-is-test"));
  }
  function syncMode() {
    var text = state.test
      ? "ACTIVÉ : exercice, les messages sont marqués TEST"
      : "Désactivé : les alertes sont RÉELLES";
    switches().forEach(function (s) { s.checked = state.test; });
    ["mode-state", "sheet-mode-state"].forEach(function (id) { if ($(id)) $(id).textContent = text; });
    document.body.classList.toggle("mode-exercise", state.test);
    var banner = $("test-banner");
    if (banner) banner.hidden = !state.test;
  }
  document.addEventListener("change", function (e) {
    if (e.target.id === "is-test" || e.target.id === "sheet-is-test") {
      state.test = e.target.checked;
      syncMode();
    }
  });
  syncMode();

  // --- feuille de choix du type (fragment chargé à la demande, repli : page /alerts) ------------

  function sheet() { return $("alert-sheet"); }

  function loadSheet() {
    if (sheet()) return Promise.resolve(true);
    if (sheetLoading) return sheetLoading;
    sheetLoading = fetch("/ui/alerts/sheet", { credentials: "same-origin", headers: { "HX-Request": "true" } })
      .then(function (r) {
        if (!r.ok) return false;
        return r.text().then(function (html) {
          if (!sheet()) document.body.insertAdjacentHTML("beforeend", html);
          syncMode();
          return !!sheet();
        });
      })
      .catch(function () { return false; })
      .then(function (ok) { sheetLoading = null; return ok; });
    return sheetLoading;
  }

  function openSheet() {
    return loadSheet().then(function (ok) {
      if (!ok) { location.href = "/alerts"; return; }
      if (!dlg.open && !sheet().open) sheet().showModal();
    });
  }

  document.addEventListener("click", function (e) {
    var opener = e.target.closest("[data-alert-open]");
    if (opener) {
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button > 0) return;
      e.preventDefault();
      openSheet();
      return;
    }
    if (e.target.closest("[data-sheet-close]")) { sheet().close(); return; }
    var b = e.target.closest(".alert-btn");
    if (b) {
      if (sheet() && sheet().open) sheet().close();
      openConfirm(b.dataset);
    }
  });
  // Clic sur le fond de la feuille : fermeture.
  document.addEventListener("click", function (e) {
    if (e.target === sheet()) sheet().close();
  });

  // Préchargement discret : la feuille s'ouvre sans attendre le réseau.
  window.addEventListener("load", function () { setTimeout(loadSheet, 1500); });

  // --- confirmation ----------------------------------------------------------------------------

  function word() {
    return current.test ? dlg.dataset.testWord : dlg.dataset.alertWord;
  }

  function setStatus(msg, kind) {
    var s = $("ad-status");
    s.hidden = !msg;
    s.textContent = msg || "";
    s.className = "ad-status" + (kind ? " is-" + kind : "");
    s.setAttribute("role", kind === "error" || kind === "warn" ? "alert" : "status");
  }

  function validate() {
    var typed = $("ad-confirm").value.trim() === word();
    $("ad-send").disabled = sending || done || (!current.test && !typed);
  }

  function openConfirm(d) {
    current = { type: d.type, test: state.test, label: d.label || d.title };
    sending = false;
    done = false;
    cancelHold();
    var mode = $("ad-mode");
    mode.textContent = current.test ? "EXERCICE : aucune vraie alerte" : "ALERTE RÉELLE";
    mode.className = "badge mono ad-mode " + (current.test ? "mode-test" : "mode-real");
    dlg.classList.toggle("is-real", !current.test);
    $("ad-title").textContent = d.title;
    $("ad-message").textContent = d.message;
    $("ad-help").textContent = current.test
      ? "Le message sera préfixé TEST sur les enceintes et sur Teams."
      : "Ce message sera diffusé tel quel sur les enceintes et sur Teams, pour de vrai.";
    $("ad-confirm-zone").hidden = current.test;
    $("ad-confirm-label").textContent = "Ou tapez « " + dlg.dataset.alertWord + " » pour confirmer";
    $("ad-confirm").value = "";
    $("ad-cancel").textContent = "Annuler";
    $("ad-recap").hidden = true;
    $("ad-send").hidden = false;
    $("ad-send-label").textContent = current.test ? "Lancer l'exercice" : "Diffuser l'alerte réelle";
    setStatus("");
    validate();
    if (!dlg.open) dlg.showModal();
    $("ad-title").focus();
  }

  $("ad-confirm").addEventListener("input", validate);
  $("ad-cancel").addEventListener("click", function () { dlg.close(); });
  dlg.addEventListener("close", cancelHold);

  // --- appui long (réel uniquement) -------------------------------------------------------------

  var hold = null;
  var holdBtn = $("ad-hold");

  function paint(p) { holdBtn.style.setProperty("--p", String(p)); }

  function cancelHold(announce) {
    if (!hold) return;
    cancelAnimationFrame(hold.raf);
    hold = null;
    paint(0);
    holdBtn.classList.remove("is-holding");
    if (announce === true) setStatus("Appui relâché : alerte non envoyée.");
  }

  function tick() {
    if (!hold) return;
    var p = Math.min(1, (performance.now() - hold.t0) / HOLD_MS);
    paint(p);
    if (p >= 1) {
      var id = hold.id;
      cancelHold();
      try { holdBtn.releasePointerCapture(id); } catch (x) { /* déjà relâché */ }
      if (navigator.vibrate) navigator.vibrate(30);
      send();
      return;
    }
    hold.raf = requestAnimationFrame(tick);
  }

  holdBtn.addEventListener("pointerdown", function (e) {
    if (e.button > 0 || hold || sending || done) return;
    e.preventDefault();
    try { holdBtn.setPointerCapture(e.pointerId); } catch (x) { /* non supporté */ }
    hold = { t0: performance.now(), id: e.pointerId, raf: 0 };
    holdBtn.classList.add("is-holding");
    setStatus("Maintenez appuyé…");
    hold.raf = requestAnimationFrame(tick);
  });
  ["pointerup", "pointercancel", "lostpointercapture"].forEach(function (n) {
    holdBtn.addEventListener(n, function () { cancelHold(true); });
  });
  holdBtn.addEventListener("contextmenu", function (e) { e.preventDefault(); });
  // Au clavier / lecteur d'écran : pas d'appui long, on oriente vers la saisie du mot.
  holdBtn.addEventListener("click", function (e) {
    if (e.detail !== 0) return;
    setStatus("Au clavier, tapez le mot « " + dlg.dataset.alertWord + " » puis validez.");
    $("ad-confirm").focus();
  });

  // --- envoi -------------------------------------------------------------------------------------

  function clock() {
    return new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function failMsg(prefix) { return "Alerte NON envoyée : " + prefix; }

  async function post(payload) {
    var init = {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    };
    var r = await fetch("/incidents/from-template", init);
    if (r.status === 401) {
      var rr = await fetch("/auth/refresh", { method: "POST", credentials: "same-origin" });
      if (rr.ok) r = await fetch("/incidents/from-template", init);
    }
    return r;
  }

  // 409 : retrouve l'incident récent du même type/mode (meilleur effort, silencieux en cas d'échec).
  async function recentIncident(cur) {
    try {
      var r = await fetch("/incidents?limit=5", { credentials: "same-origin" });
      if (!r.ok) return null;
      var rows = await r.json();
      var hit = rows.find(function (i) { return i.type === cur.type && i.is_test === cur.test; });
      return hit ? hit.id : null;
    } catch (x) { return null; }
  }

  async function send() {
    if (sending || done || !current) return;
    var cur = current;
    var payload = { type: cur.type, is_test: cur.test, confirm: word() };
    sending = true;
    $("ad-send").disabled = true;
    $("ad-send").setAttribute("aria-busy", "true");
    setStatus("Envoi en cours…");
    var okId = null, msg = null, dup = false, recent = null;
    try {
      var r = await post(payload);
      if (r.ok) {
        var body = {};
        try { body = await r.json(); } catch (x) { /* corps non JSON */ }
        okId = body.id || "";
      } else if (r.status === 409) {
        dup = true;
        msg = "Une alerte identique vient peut-être de partir : vérifie le tableau de bord / les incidents avant de réessayer.";
        recent = await recentIncident(cur);
      } else {
        msg = failMsg("erreur " + r.status);
        try {
          var d = (await r.json()).detail;
          if (typeof d === "string") msg = failMsg(d);
        } catch (x) { /* non JSON */ }
      }
    } catch (err) {
      msg = failMsg("serveur injoignable (l'alerte n'est peut-être pas partie). Réessayer ou prévenir par téléphone.");
    }
    sending = false;
    $("ad-send").removeAttribute("aria-busy");
    if (current !== cur) return;
    if (okId !== null) {
      done = true;
      var what = cur.test ? "EXERCICE LANCÉ" : "ALERTE ENVOYÉE";
      setStatus(what + " à " + clock() + ". Le récap indique l'état de la diffusion.", "ok");
      $("ad-confirm-zone").hidden = true;
      $("ad-send").hidden = true;
      $("ad-cancel").textContent = "Fermer";
      if (okId) { $("ad-recap").href = "/alerts/" + encodeURIComponent(okId); $("ad-recap").hidden = false; }
      if (!dlg.open && ui) ui.toast(what.charAt(0) + what.slice(1).toLowerCase());
      if (ui) { ui.refresh("#incidents"); ui.refresh("#incident-banner"); }
      $("ad-cancel").focus();
    } else {
      setStatus(msg, dup ? "warn" : "error");
      if (dup) {
        $("ad-recap").href = recent ? "/alerts/" + encodeURIComponent(recent) : "/alerts";
        $("ad-recap").hidden = false;
      }
      $("ad-send").hidden = false;
      $("ad-send-label").textContent = "Réessayer";
      validate();
      if (!dlg.open && ui) ui.toast(msg, true);
    }
  }

  $("ad-form").addEventListener("submit", function (e) {
    e.preventDefault();
    if ($("ad-send").disabled) return;
    send();
  });

  // --- bandeau « incident en cours » : actualisation périodique ----------------------------------

  setInterval(function () {
    if (!document.hidden && ui) ui.refresh("#incident-banner");
  }, 15000);

  // --- lien profond /alerts?type=… ---------------------------------------------------------------

  var grid = $("alert-grid");
  var wanted = new URLSearchParams(location.search).get("type");
  if (grid && wanted) {
    var btn = Array.prototype.find.call(grid.querySelectorAll(".alert-btn"), function (b) { return b.dataset.type === wanted; });
    if (btn) openConfirm(btn.dataset);
  }
})();
