(function () {
  "use strict";

  function split(s) {
    return (s || "").split(",").map(function (x) { return x.trim(); }).filter(Boolean);
  }

  // Encode en JSON typé les requêtes des éléments marqués data-json.
  // data-arrays / data-bools / data-ints : noms de champs à convertir ; "" devient null.
  htmx.defineExtension("json-typed", {
    encodeParameters: function (xhr, params, elt) {
      var holder = elt.closest("[data-json]");
      if (!holder) return null;
      var arrays = split(holder.dataset.arrays);
      var bools = split(holder.dataset.bools);
      var ints = split(holder.dataset.ints);
      var out = {};
      arrays.forEach(function (k) { if (!(k in params)) out[k] = []; });
      Object.keys(params).forEach(function (k) {
        var v = params[k];
        if (arrays.indexOf(k) >= 0) v = Array.isArray(v) ? v : [v];
        else if (typeof v === "string") {
          if (v === "") v = null;
          else if (bools.indexOf(k) >= 0) v = v === "true" || v === "on";
          else if (ints.indexOf(k) >= 0) v = parseInt(v, 10);
        }
        out[k] = v;
      });
      xhr.overrideMimeType("text/json");
      return JSON.stringify(out);
    },
    onEvent: function (name, evt) {
      if (name === "htmx:configRequest" && evt.detail.elt.closest("[data-json]")) {
        evt.detail.headers["Content-Type"] = "application/json";
      }
      return true;
    },
  });

  // Toast : les erreurs restent affichées jusqu'à fermeture (ou action) ; opts.action = { label, run }.
  var toastEl, toastTimer;
  function hideToast() {
    clearTimeout(toastTimer);
    if (toastEl) toastEl.classList.remove("show");
  }
  function toast(msg, isError, opts) {
    toastEl = toastEl || document.getElementById("toast");
    clearTimeout(toastTimer);
    toastEl.setAttribute("role", isError ? "alert" : "status");
    toastEl.setAttribute("aria-live", isError ? "assertive" : "polite");
    toastEl.textContent = "";
    var span = document.createElement("span");
    span.className = "toast-msg";
    span.textContent = msg;
    toastEl.appendChild(span);
    var action = opts && opts.action;
    if (action) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn";
      b.textContent = action.label;
      b.addEventListener("click", function () { hideToast(); action.run(); });
      toastEl.appendChild(b);
    }
    if (isError) {
      var x = document.createElement("button");
      x.type = "button";
      x.className = "btn btn-icon";
      x.setAttribute("aria-label", "Fermer le message");
      x.innerHTML = '<span class="icon" aria-hidden="true">close</span>';
      x.addEventListener("click", hideToast);
      toastEl.appendChild(x);
    }
    toastEl.classList.toggle("is-error", !!isError);
    toastEl.classList.add("show");
    if (!isError) toastTimer = setTimeout(hideToast, 4000);
  }

  function errorText(xhr) {
    try {
      var d = JSON.parse(xhr.responseText).detail;
      if (typeof d === "string") return d;
      if (Array.isArray(d)) return d.map(function (e) { return e.msg; }).join(" ; ");
    } catch (e) { /* corps non JSON */ }
    return "Erreur " + xhr.status;
  }

  function refresh(sel) {
    var t = sel && document.querySelector(sel);
    if (t) htmx.trigger(t, "refresh");
  }

  async function renewSession() {
    var r = await fetch("/auth/refresh", { method: "POST", credentials: "same-origin" });
    return r.ok;
  }

  document.body.addEventListener("htmx:afterRequest", function (e) {
    var d = e.detail, el = d.elt;
    if (!el.closest) return;
    var src = el.closest("[data-refresh],[data-toast]") || el;
    if (d.successful) {
      if (src.dataset.toast) toast(src.dataset.toast);
      if (src.hasAttribute("data-reset") && el.tagName === "FORM") el.reset();
      if (src.dataset.refresh) refresh(src.dataset.refresh);
    } else if (d.xhr && d.xhr.status) {
      if (d.xhr.status === 401) {
        var cfg = d.requestConfig || {};
        var retried = cfg.headers && cfg.headers["X-Auth-Retry"];
        if (retried) { location.href = "/login"; return; }
        renewSession().then(function (ok) {
          if (!ok) { location.href = "/login"; return; }
          htmx.ajax(cfg.verb || "get", cfg.path, {
            source: el,
            target: d.target || el,
            swap: (el.getAttribute && el.getAttribute("hx-swap")) || undefined,
            headers: { "X-Auth-Retry": "1" },
          });
        });
        return;
      }
      toast(errorText(d.xhr), true);
      if (src.dataset.refresh) refresh(src.dataset.refresh);
    }
  });

  // Admin → Enceintes : copie d'un ID de lecteur MA et rattachement à une enceinte.
  document.addEventListener("click", function (e) {
    var copy = e.target.closest("[data-copy]");
    if (copy) {
      var done = function () { toast("ID copié"); };
      if (navigator.clipboard) navigator.clipboard.writeText(copy.dataset.copy).then(done, function () { toast("Copie impossible", true); });
      else toast("Copie impossible", true);
      return;
    }
    var use = e.target.closest("[data-use-player]");
    if (!use) return;
    var box = use.closest("[data-ma-use]");
    var sel = box.querySelector("select");
    fetch("/speakers/" + encodeURIComponent(sel.value), {
      method: "PATCH",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ma_player_id: box.dataset.playerId }),
    }).then(function (r) {
      if (r.ok) { toast("Enceinte mise à jour"); refresh("#speakers"); return; }
      return r.text().then(function (t) { toast(errorText({ status: r.status, responseText: t }), true); });
    }, function () { toast("Erreur réseau", true); });
  });

  // Rafraîchit la liste des incidents sans écraser un champ de confirmation en cours de saisie.
  setInterval(function () {
    var list = document.getElementById("incidents");
    if (!list || document.hidden) return;
    var busy = list.contains(document.activeElement) ||
      Array.prototype.some.call(list.querySelectorAll("input"), function (i) { return i.value; });
    if (!busy) htmx.trigger(list, "refresh");
  }, 5000);

  // Flux SSE : chaque évènement déclenche un rafraîchissement HTMX du fragment (auth/401 gérés par htmx).
  function openStream(el) {
    var es = new EventSource(el.dataset.sse, { withCredentials: true });
    es.addEventListener("update", function () { htmx.trigger(el, "refresh"); });
    es.onerror = function () {
      if (es.readyState !== EventSource.CLOSED) return;
      renewSession().then(function (ok) {
        if (ok && document.body.contains(el)) setTimeout(function () { openStream(el); }, 2000);
      });
    };
  }
  document.addEventListener("DOMContentLoaded", function () {
    var el = document.querySelector("[data-sse]");
    if (el) openStream(el);
  });


  // Validation de base : champ requis rempli d'espaces refusé ; message d'erreur lié au champ.
  function fieldError(input) {
    var id = (input.id || input.name || "f") + "-error";
    var err = input.parentNode.querySelector(".field-error");
    if (!err) {
      err = document.createElement("span");
      err.className = "field-error";
      err.setAttribute("role", "alert");
      input.insertAdjacentElement("afterend", err);
    }
    err.id = id;
    input.setAttribute("aria-describedby", id);
    return err;
  }
  function clearError(input) {
    input.removeAttribute("aria-invalid");
    var err = input.parentNode && input.parentNode.querySelector(".field-error");
    if (err) err.remove();
  }
  document.addEventListener("invalid", function (e) {
    var t = e.target;
    if (!t.form || t.type === "checkbox") return;
    t.setAttribute("aria-invalid", "true");
    fieldError(t).textContent = t.validationMessage;
  }, true);
  document.addEventListener("input", function (e) {
    var t = e.target;
    if (!t.matches || !t.matches("input[required], textarea[required]")) return;
    if (t.type === "text" || t.tagName === "TEXTAREA") {
      t.setCustomValidity(t.value && !t.value.trim() ? "Ce champ ne peut pas être vide." : "");
    }
    if (t.hasAttribute("aria-invalid") && t.checkValidity()) clearError(t);
  });

  document.body.addEventListener("htmx:sendError", function () {
    toast("Serveur injoignable", true);
  });

  document.addEventListener("click", async function (e) {
    var logout = e.target.closest("[data-logout]");
    if (logout) {
      await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
      location.href = "/login";
      return;
    }
    var grp = e.target.closest("[data-group]");
    if (grp) groupCall(grp.dataset.group, null, null);
  });

  // Sélection multi-lecteurs, mémorisée en localStorage.
  var SEL_KEY = "sound.music.selection";
  function loadSel() {
    try {
      var v = JSON.parse(localStorage.getItem(SEL_KEY) || "[]");
      return Array.isArray(v) ? v : [];
    } catch (e) { return []; }
  }
  function saveSel(ids) {
    try { localStorage.setItem(SEL_KEY, JSON.stringify(ids)); } catch (e) { /* stockage indisponible */ }
  }
  function boxes() { return document.querySelectorAll("#players [data-sel]"); }
  function selected() {
    return Array.prototype.filter.call(boxes(), function (b) { return b.checked; })
      .map(function (b) { return b.dataset.sel; });
  }
  function syncSel() {
    var bar = document.getElementById("group-bar");
    if (!bar) return;
    var n = selected().length;
    bar.hidden = n === 0;
    document.getElementById("sel-count").textContent = n + (n > 1 ? " lecteurs" : " lecteur");
  }
  function restoreSel() {
    var ids = loadSel();
    boxes().forEach(function (b) { b.checked = ids.indexOf(b.dataset.sel) >= 0; });
    syncSel();
  }
  document.addEventListener("DOMContentLoaded", function () {
    restoreSel();
    document.dispatchEvent(new CustomEvent("selection-changed"));
  });
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.target.id === "players") {
      restoreSel();
      document.dispatchEvent(new CustomEvent("selection-changed"));
    }
  });

  // Appelle l'endpoint existant sur chaque lecteur coché en parallèle ; un échec n'arrête pas les autres.
  async function groupCall(action, payload, okMsg) {
    var ids = selected();
    if (!ids.length) { toast("Aucun lecteur sélectionné", true); return; }
    var results = await Promise.all(ids.map(async function (id) {
      try {
        var r = await fetch("/music/players/" + encodeURIComponent(id) + "/" + action, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: payload ? JSON.stringify(payload) : undefined,
        });
        return r.ok ? null : errorText({ status: r.status, responseText: await r.text() });
      } catch (err) { return "Serveur injoignable"; }
    }));
    var failed = results.filter(Boolean).length;
    if (failed) toast(failed + "/" + ids.length + " lecteur(s) en échec : " + results.filter(Boolean)[0], true);
    else toast(okMsg || "Commande envoyée");
    setTimeout(function () { refresh("#players"); }, 800);
  }

  window.SoundUI = {
    toast: toast, hideToast: hideToast, groupCall: groupCall, selected: selected, refresh: refresh,
  };

  // Message pré-enregistré : désactive le texte libre.
  document.addEventListener("change", function (e) {
    if (e.target.dataset && e.target.dataset.sel) {
      saveSel(selected()); syncSel();
      document.dispatchEvent(new CustomEvent("selection-changed"));
      return;
    }
    if (e.target.id === "group-volume") {
      groupCall("volume", { level: parseInt(e.target.value, 10) }, "Volume appliqué");
      return;
    }
    if (e.target.id !== "preset-select") return;
    var txt = document.getElementById("free-text");
    txt.disabled = !!e.target.value;
    if (e.target.value) txt.value = "";
  });

  function syncTestBanner() {
    var cb = document.getElementById("is-test"), b = document.getElementById("test-banner");
    if (cb && b) b.hidden = !cb.checked;
  }
  document.addEventListener("change", function (e) {
    if (e.target.id === "is-test") syncTestBanner();
  });

  document.addEventListener("reset", function () {
    setTimeout(syncTestBanner);
    setTimeout(function () {
      var txt = document.getElementById("free-text");
      if (txt) txt.disabled = false;
    });
  });
})();
