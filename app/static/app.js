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

  // Toasts en pile (max 3) : succès éphémères (role=status), erreurs persistantes jusqu'à fermeture (role=alert).
  // opts.action = { label, run }.
  var MAX_TOASTS = 3;
  function stack() { return document.getElementById("toast"); }
  function dismiss(item) {
    clearTimeout(item._timer);
    item.classList.remove("show");
    setTimeout(function () { if (item.parentNode) item.parentNode.removeChild(item); }, 200);
  }
  function hideToast() {
    var st = stack();
    if (st) Array.prototype.slice.call(st.children).forEach(dismiss);
  }
  function toast(msg, isError, opts) {
    var st = stack();
    if (!st) return;
    while (st.children.length >= MAX_TOASTS) {
      var old = Array.prototype.find.call(st.children, function (c) { return !c.classList.contains("is-error"); }) || st.children[0];
      if (old._timer) clearTimeout(old._timer);
      st.removeChild(old);
    }
    var item = document.createElement("div");
    item.className = "toast-item" + (isError ? " is-error" : "");
    item.setAttribute("role", isError ? "alert" : "status");
    var span = document.createElement("span");
    span.className = "toast-msg";
    span.textContent = msg;
    item.appendChild(span);
    var action = opts && opts.action;
    if (action) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn";
      b.textContent = action.label;
      b.addEventListener("click", function () { dismiss(item); action.run(); });
      item.appendChild(b);
    }
    if (isError) {
      var x = document.createElement("button");
      x.type = "button";
      x.className = "btn btn-icon";
      x.setAttribute("aria-label", "Fermer le message");
      x.innerHTML = '<span class="icon" aria-hidden="true">close</span>';
      x.addEventListener("click", function () { dismiss(item); });
      item.appendChild(x);
    }
    st.appendChild(item);
    requestAnimationFrame(function () { item.classList.add("show"); });
    if (!isError) item._timer = setTimeout(function () { dismiss(item); }, 4000);
  }

  // confirmDialog : remplace le confirm() natif de hx-confirm (une seule confirmation, stylée, au clavier).
  var confirmEl;
  function confirmDialog(message, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      if (!confirmEl) {
        confirmEl = document.createElement("dialog");
        confirmEl.className = "menu confirm-dialog";
        confirmEl.setAttribute("aria-labelledby", "cd-title");
        confirmEl.innerHTML =
          '<form method="dialog" class="stack"><h2 id="cd-title"></h2><p id="cd-body" class="muted"></p>' +
          '<div class="row"><button class="btn btn-text" value="cancel" id="cd-cancel">Annuler</button>' +
          '<button class="btn btn-filled" value="ok" id="cd-ok"></button></div></form>';
        document.body.appendChild(confirmEl);
      }
      var ok = confirmEl.querySelector("#cd-ok");
      confirmEl.querySelector("#cd-title").textContent = opts.title || "Confirmer";
      confirmEl.querySelector("#cd-body").textContent = message || "";
      ok.textContent = opts.confirmLabel || "Confirmer";
      ok.className = "btn " + (opts.danger === false ? "btn-filled" : "btn-filled btn-danger-solid");
      confirmEl.returnValue = "cancel";
      confirmEl.addEventListener("close", function () { resolve(confirmEl.returnValue === "ok"); }, { once: true });
      confirmEl.showModal();
      confirmEl.querySelector("#cd-cancel").focus();
    });
  }
  document.body.addEventListener("htmx:confirm", function (e) {
    if (!e.detail.question) return;
    e.preventDefault();
    var src = e.detail.elt;
    var danger = !!(src.closest && src.closest(".btn-danger"));
    var label = src.dataset && src.dataset.confirmLabel;
    if (!label && src.cloneNode) {
      var c = src.cloneNode(true);
      c.querySelectorAll(".icon").forEach(function (n) { n.remove(); });
      label = c.textContent.trim();
      if (label.length > 24) label = "";
    }
    confirmDialog(e.detail.question, { danger: danger, confirmLabel: label || (danger ? "Supprimer" : "Confirmer") })
      .then(function (yes) { if (yes) e.detail.issueRequest(true); });
  });

  // Filtre de liste côté client : <input data-filter-list="#id"> masque les <li data-filter-item> sans correspondance.
  document.addEventListener("input", function (e) {
    var sel = e.target.dataset && e.target.dataset.filterList;
    if (!sel) return;
    var q = e.target.value.trim().toLowerCase();
    document.querySelectorAll(sel + " [data-filter-item]").forEach(function (li) {
      var txt = li.textContent + " " + Array.prototype.map.call(li.querySelectorAll("input"), function (i) { return i.value; }).join(" ");
      li.hidden = !!q && txt.toLowerCase().indexOf(q) < 0;
    });
  });

  // Menu « Plus » (navigation mobile) : se ferme dès qu'on choisit une entrée ou un contrôle.
  document.addEventListener("click", function (e) {
    var more = document.getElementById("more-menu");
    if (!more) return;
    if (e.target.closest("[data-more]")) { more.showModal(); return; }
    if (more.open && (e.target === more || e.target.closest("[data-more-close]"))) more.close();
  });

  // Boutons occupés pendant une requête htmx (anti double envoi) ; jamais sur les contrôles d'alerte.
  function busyBtn(e, on) {
    var el = e.detail.elt;
    if (!el || !el.matches || el.closest("#alert-dialog")) return;
    var btns = el.matches("button, input[type=submit]") ? [el]
      : el.tagName === "FORM" ? Array.prototype.slice.call(el.querySelectorAll("button[type=submit], button:not([type])")) : [];
    btns.forEach(function (b) { b.toggleAttribute("aria-busy", on); b.disabled = on; });
  }
  document.body.addEventListener("htmx:beforeRequest", function (e) { busyBtn(e, true); });
  document.body.addEventListener("htmx:afterRequest", function (e) { busyBtn(e, false); });

  // Erreurs de champ : un 422 FastAPI est rattaché au champ (aria-invalid + message lié).
  function clearFieldErrors(form) {
    form.querySelectorAll("[aria-invalid]").forEach(function (f) { f.removeAttribute("aria-invalid"); f.removeAttribute("aria-describedby"); });
    form.querySelectorAll(".field-error").forEach(function (n) { n.remove(); });
  }
  function fieldErrors(xhr, el) {
    var form = el.tagName === "FORM" ? el : el.closest && el.closest("form");
    if (!form || xhr.status !== 422) return false;
    var detail;
    try { detail = JSON.parse(xhr.responseText).detail; } catch (x) { return false; }
    if (!Array.isArray(detail)) return false;
    clearFieldErrors(form);
    var n = 0;
    detail.forEach(function (d) {
      var name = d.loc && d.loc[d.loc.length - 1];
      var f = name && form.querySelector('[name="' + name + '"]');
      if (!f) return;
      var id = "err-" + name + "-" + (n++);
      var msg = document.createElement("span");
      msg.className = "field-error";
      msg.id = id;
      msg.textContent = d.msg;
      f.setAttribute("aria-invalid", "true");
      f.setAttribute("aria-describedby", id);
      f.insertAdjacentElement("afterend", msg);
    });
    return n > 0;
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
      var shown = fieldErrors(d.xhr, el);
      toast(shown ? "Certains champs sont invalides : voir le formulaire." : errorText(d.xhr), true);
      if (src.dataset.refresh) refresh(src.dataset.refresh);
    }
  });

  // Admin → Enceintes : copie d'un ID de lecteur MA et rattachement à une enceinte.
  document.addEventListener("click", function (e) {
    var copy = e.target.closest("[data-copy]");
    if (copy) {
      var done = function () { toast(copy.dataset.copyToast || "ID copié"); };
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
  function fmtTime(s) {
    s = Math.max(0, Math.floor(s));
    return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
  }
  // Horloge locale par titre : le poll remplace le fragment, l'état vit ici (clé = item_id).
  var npClock = { key: null, state: null, base: 0, t0: 0 };
  function npLocal() {
    if (npClock.state !== "playing") return npClock.base;
    return npClock.base + Math.floor((Date.now() - npClock.t0) / 1000);
  }
  function npSync() {
    var np = document.getElementById("now-playing");
    if (!np) { npClock.key = null; return; }
    var key = np.dataset.item || "", state = np.dataset.state || "";
    var srv = parseInt(np.dataset.elapsed, 10) || 0;
    var same = npClock.key === key && npClock.state === state;
    if (same && Math.abs(srv - npLocal()) <= 3) return;
    npClock = { key: key, state: state, base: srv, t0: Date.now() };
  }
  function npRender() {
    var np = document.getElementById("now-playing");
    if (!np) return;
    var el = npLocal();
    var dur = parseInt(np.dataset.duration, 10) || 0;
    if (dur) el = Math.min(el, dur);
    var e = np.querySelector(".np-elapsed");
    if (e) e.textContent = fmtTime(el);
    if (!dur) return;
    var bar = np.querySelector(".np-bar");
    if (bar) bar.setAttribute("aria-valuenow", el);
    var fill = np.querySelector(".np-fill");
    if (fill) fill.style.width = Math.floor((el * 100) / dur) + "%";
    var left = np.querySelector(".np-left");
    if (left) left.textContent = "-" + fmtTime(dur - el);
  }
  document.body.addEventListener("htmx:afterSettle", function (e) {
    if (e.target && e.target.id === "queue") { npSync(); npRender(); }
  });
  setInterval(function () { if (!document.hidden) npRender(); }, 1000);

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
    toast: toast, hideToast: hideToast, confirmDialog: confirmDialog, groupCall: groupCall, selected: selected, refresh: refresh,
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
