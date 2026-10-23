(function () {
  "use strict";

  var form = document.getElementById("announce-form");
  if (!form) return;
  var dlg = document.getElementById("preview-dialog");
  var previewBtn = document.getElementById("preview-btn");
  var sendBtn = document.getElementById("send-btn");

  // Union dédoublonnée : enceintes cochées + membres actifs des groupes cochés.
  function targetIds() {
    var ids = {};
    form.querySelectorAll("input[data-speaker]:checked").forEach(function (b) { ids[b.value] = 1; });
    form.querySelectorAll("input[data-members]:checked").forEach(function (b) {
      b.dataset.members.split(",").filter(Boolean).forEach(function (id) { ids[id] = 1; });
    });
    return Object.keys(ids);
  }

  var mode = form.querySelector("[data-mode]") ? "preset" : "free";
  var presetSel = document.getElementById("preset-select");
  var freeText = document.getElementById("free-text");
  var previewed = false;

  function hasMessage() {
    return mode === "preset" ? !!(presetSel && presetSel.value) : !!freeText.value.trim();
  }

  function syncCount() {
    var n = targetIds().length;
    document.getElementById("target-count").textContent =
      n + (n > 1 ? " enceintes ciblées" : " enceinte ciblée");
    document.getElementById("free-count").textContent = freeText.value.length + " / 500";
    var ready = n > 0 && hasMessage();
    previewBtn.disabled = sendBtn.disabled = !ready;
    document.getElementById("send-help").textContent = ready ? "" :
      n === 0 ? "Choisissez au moins un groupe ou une enceinte pour envoyer." :
      mode === "preset" ? "Choisissez un message pré-enregistré." : "Saisissez le message à diffuser.";
    var fold = document.getElementById("speakers-fold");
    if (fold) fold.querySelector("summary").textContent = "Choisir des enceintes précises";
  }

  function setMode(m) {
    mode = m;
    form.querySelectorAll("[data-mode]").forEach(function (b) {
      var on = b.dataset.mode === m;
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.className = "btn " + (on ? "btn-tonal" : "btn-text");
    });
    document.getElementById("preset-field").hidden = m !== "preset";
    document.getElementById("free-field").hidden = m !== "free";
    var pt = document.getElementById("preset-text");
    if (pt) pt.hidden = m !== "preset" || !pt.textContent;
    if (m === "free" && presetSel && presetSel.value) {
      presetSel.value = "";
      presetSel.dispatchEvent(new Event("change", { bubbles: true }));
    }
    syncCount();
  }
  form.querySelectorAll("[data-mode]").forEach(function (b) {
    b.addEventListener("click", function () { setMode(b.dataset.mode); });
  });
  if (presetSel) presetSel.addEventListener("change", function () {
    var pt = document.getElementById("preset-text");
    var opt = presetSel.options[presetSel.selectedIndex];
    pt.textContent = presetSel.value ? opt.title : "";
    pt.hidden = !presetSel.value;
  });

  form.addEventListener("change", syncCount);
  form.addEventListener("input", syncCount);
  form.addEventListener("reset", function () {
    setTimeout(function () { if (form.querySelector("[data-mode]")) setMode("preset"); syncCount(); });
  });
  syncCount();

  function payload() {
    var fd = new FormData(form);
    var o = {
      speaker_ids: fd.getAll("speaker_ids"),
      group_ids: fd.getAll("group_ids"),
      is_test: fd.get("is_test") === "true",
      voice: fd.get("voice") || null,
      lang: fd.get("lang") || null,
    };
    if (form.elements.force_volume) o.force_volume = fd.get("force_volume") === "true";
    if (mode === "preset") o.preset_id = fd.get("preset_id");
    else o.text = fd.get("text") || "";
    return o;
  }

  function errMsg(body) {
    var d = body && body.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map(function (e) { return e.msg; }).join(" ; ");
    return "Erreur";
  }

  previewBtn.addEventListener("click", async function () {
    previewBtn.disabled = true;
    try {
      var r = await fetch("/announcements/preview", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload()),
      });
      var body = await r.json().catch(function () { return null; });
      if (!r.ok) { SoundUI.toast(errMsg(body), true); return; }
      show(body);
    } catch (e) {
      SoundUI.toast("Serveur injoignable", true);
    } finally {
      syncCount();
    }
  });

  function show(p) {
    document.getElementById("pv-text").textContent = p.text;
    document.getElementById("pv-meta").textContent =
      "Voix : " + (p.voice || "par défaut") + " · Langue : " + (p.lang || "par défaut") +
      " · Durée estimée : ~" + p.estimated_seconds + " s" +
      (p.volume ? " · Volume forcé : " + p.volume + " %" : "");
    var names = p.speakers.map(function (s) { return s.name; });
    var txt = p.speakers.length + " enceinte(s) : " + names.join(", ");
    if (p.ignored.length) txt += " — ignorée(s) car inactive(s) : " + p.ignored.map(function (s) { return s.name; }).join(", ");
    document.getElementById("pv-speakers").textContent = txt;
    var ul = document.getElementById("pv-warnings");
    ul.textContent = "";
    p.warnings.forEach(function (w) {
      var li = document.createElement("li");
      li.textContent = "⚠ " + w;
      ul.appendChild(li);
    });
    ul.hidden = !p.warnings.length;
    dlg.showModal();
  }

  document.getElementById("pv-cancel").addEventListener("click", function () { dlg.close(); });
  document.getElementById("pv-send").addEventListener("click", function () {
    dlg.close();
    previewed = true;
    htmx.trigger(form, "submit");
  });

  // Un seul écran de confirmation : l'aperçu. Envoyer ouvre l'aperçu ; l'aperçu envoie.
  document.addEventListener("submit", function (e) {
    if (e.target !== form) return;
    if (previewed) { previewed = false; return; }
    e.preventDefault();
    e.stopPropagation();
    if (!previewBtn.disabled) previewBtn.click();
  }, true);

  // Suivi de l'envoi : état en direct (poll court) au lieu d'un simple toast.
  var STATUS_FR = { queued: "en attente", sending: "envoi en cours", done: "terminée", failed: "en échec" };
  var statusBox = document.getElementById("send-status");
  var statusText = document.getElementById("send-status-text");
  var retryBtn = document.getElementById("send-retry");
  var followTimer = null;
  var lastPayload = null;

  function showStatus(msg, failed) {
    statusBox.hidden = false;
    statusBox.className = "ad-status " + (failed ? "is-error" : "is-ok");
    statusBox.setAttribute("role", failed ? "alert" : "status");
    statusText.textContent = msg;
    retryBtn.hidden = !failed;
  }

  function follow(id, tries) {
    clearTimeout(followTimer);
    fetch("/announcements/" + encodeURIComponent(id), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (a) {
        if (!a) return;
        var failed = a.status === "failed";
        showStatus("Annonce " + (STATUS_FR[a.status] || a.status) + " (" + a.targets.length + " enceinte(s)).", failed);
        window.SoundUI.refresh("#history");
        if ((a.status === "queued" || a.status === "sending") && tries < 40) {
          followTimer = setTimeout(function () { follow(id, tries + 1); }, 3000);
        }
      })
      .catch(function () { /* le suivi est facultatif */ });
  }

  document.body.addEventListener("htmx:afterRequest", function (e) {
    if (e.detail.elt !== form) return;
    var xhr = e.detail.xhr;
    if (e.detail.successful) {
      var id = null;
      try { id = JSON.parse(xhr.responseText).id; } catch (x) { /* pas d'id */ }
      showStatus("Annonce envoyée.", false);
      if (id) follow(id, 0);
    } else {
      showStatus("Échec de l'envoi : l'annonce n'est pas partie.", true);
    }
    syncCount();
  });
  retryBtn.addEventListener("click", function () {
    previewed = true;
    htmx.trigger(form, "submit");
  });

  // Rediffuser depuis l'historique : recopie texte et cibles dans le formulaire (rien n'est envoyé).
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-replay]");
    if (!b) return;
    form.reset();
    setTimeout(function () {
      setMode("free");
      freeText.value = b.dataset.text;
      var ids = b.dataset.speakers.split(",").filter(Boolean);
      var found = 0;
      form.querySelectorAll("input[data-speaker]").forEach(function (c) {
        c.checked = ids.indexOf(c.value) >= 0;
        if (c.checked) found++;
      });
      document.getElementById("speakers-fold").open = true;
      syncCount();
      form.scrollIntoView({ block: "start" });
      freeText.focus();
      window.SoundUI.toast(found ? "Annonce recopiée : vérifiez puis envoyez." : "Texte recopié ; choisissez les enceintes.");
    });
  });

  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.target.id === "detail-body") document.getElementById("detail-dialog").showModal();
  });
  document.getElementById("dt-close").addEventListener("click", function () {
    document.getElementById("detail-dialog").close();
  });
  document.getElementById("hist-reset").addEventListener("click", function () {
    setTimeout(function () { htmx.trigger(document.getElementById("hist-filters"), "submit"); });
  });
})();
