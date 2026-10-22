(function () {
  // Affiche/masque les blocs du formulaire de planification selon l'action et la récurrence ;
  // les champs masqués sont désactivés pour ne pas être envoyés.
  function toggle(form, attr, value) {
    form.querySelectorAll("[" + attr + "]").forEach(function (el) {
      var on = el.getAttribute(attr).split(" ").indexOf(value) >= 0;
      el.hidden = !on;
      el.querySelectorAll("input, select, textarea").forEach(function (i) { i.disabled = !on; });
    });
  }

  function sync(form) {
    var action = form.querySelector("[name=action]");
    var rec = form.querySelector("[data-role=recurrence]");
    if (action) toggle(form, "data-show-action", action.value);
    if (rec) toggle(form, "data-show-rec", rec.value);
  }

  function syncAll(root) {
    (root || document).querySelectorAll("form[data-planning-form]").forEach(sync);
  }

  document.addEventListener("change", function (e) {
    var form = e.target.closest && e.target.closest("form[data-planning-form]");
    if (form) sync(form);
  });
  document.addEventListener("htmx:afterSwap", function (e) { syncAll(e.target); });
  document.addEventListener("DOMContentLoaded", function () { syncAll(); });
})();
