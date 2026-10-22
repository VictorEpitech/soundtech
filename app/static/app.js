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

  var toastEl, toastTimer;
  function toast(msg, isError) {
    toastEl = toastEl || document.getElementById("toast");
    toastEl.textContent = msg;
    toastEl.classList.toggle("is-error", !!isError);
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 4000);
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
    toast: toast, groupCall: groupCall, selected: selected, refresh: refresh,
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
