(function () {
  "use strict";

  // Recharge la page après une action réussie marquée data-reload (ex. clôture depuis le récap).
  document.body.addEventListener("htmx:afterRequest", function (e) {
    var el = e.detail.elt;
    if (e.detail.successful && el.hasAttribute && el.hasAttribute("data-reload")) location.reload();
  });

  var grid = document.getElementById("alert-grid");
  var dlg = document.getElementById("alert-dialog");
  if (!grid || !dlg) return;

  var toggle = document.getElementById("is-test");
  var stateEl = document.getElementById("mode-state");
  var el = function (id) { return document.getElementById(id); };
  var current = null;
  var busy = false;

  // Mode exercice : volontairement non persisté (désactivé à chaque chargement de page).
  toggle.checked = false;
  function syncMode() {
    stateEl.textContent = toggle.checked
      ? "ACTIVÉ : exercice, les messages sont marqués TEST"
      : "Désactivé : les alertes sont RÉELLES";
    document.body.classList.toggle("mode-exercise", toggle.checked);
  }
  toggle.addEventListener("change", syncMode);
  syncMode();

  function word() {
    return current.test ? grid.dataset.testWord : grid.dataset.alertWord;
  }

  function validate() {
    // Réel : saisie explicite du mot de confirmation ; exercice : le bouton suffit.
    el("ad-send").disabled = busy || (!current.test && el("ad-confirm").value.trim() !== word());
  }

  grid.addEventListener("click", function (e) {
    var b = e.target.closest(".alert-btn");
    if (!b) return;
    current = { type: b.dataset.type, test: toggle.checked };
    var mode = el("ad-mode");
    mode.textContent = current.test ? "EXERCICE : aucune vraie alerte" : "ALERTE RÉELLE";
    mode.className = "badge mono ad-mode " + (current.test ? "mode-test" : "mode-real");
    dlg.classList.toggle("is-real", !current.test);
    el("ad-title").textContent = b.dataset.title;
    el("ad-message").textContent = b.dataset.message;
    el("ad-help").textContent = current.test
      ? "Le message sera préfixé TEST sur les enceintes et sur Teams."
      : "Ce message sera diffusé tel quel sur les enceintes et sur Teams, pour de vrai.";
    el("ad-confirm-field").hidden = current.test;
    el("ad-confirm-label").textContent = "Tapez « " + grid.dataset.alertWord + " » pour confirmer";
    el("ad-confirm").value = "";
    el("ad-send-label").textContent = current.test ? "Lancer l'exercice" : "Diffuser l'alerte réelle";
    busy = false;
    validate();
    dlg.showModal();
  });

  el("ad-confirm").addEventListener("input", validate);
  el("ad-cancel").addEventListener("click", function () { dlg.close(); });

  el("ad-form").addEventListener("submit", async function (e) {
    e.preventDefault();
    if (busy || el("ad-send").disabled) return;
    busy = true;
    validate();
    var toast = window.SoundUI.toast;
    try {
      var r = await fetch("/incidents/from-template", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: current.type, is_test: current.test, confirm: word() }),
      });
      if (r.ok) toast(current.test ? "Exercice lancé" : "Alerte diffusée");
      else {
        var msg = "Erreur " + r.status;
        try { var d = (await r.json()).detail; if (typeof d === "string") msg = d; } catch (x) { /* non JSON */ }
        toast(msg, true);
      }
    } catch (err) {
      toast("Serveur injoignable", true);
    }
    busy = false;
    dlg.close();
    window.SoundUI.refresh("#incidents");
  });
})();
