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

  function syncCount() {
    var n = targetIds().length;
    document.getElementById("target-count").textContent =
      n + (n > 1 ? " enceintes ciblées" : " enceinte ciblée");
    previewBtn.disabled = sendBtn.disabled = n === 0;
  }

  form.addEventListener("change", syncCount);
  form.addEventListener("reset", function () { setTimeout(syncCount); });
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
    if (fd.get("preset_id")) o.preset_id = fd.get("preset_id");
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
    form.dataset.previewed = "1";
    htmx.trigger(form, "submit");
  });

  // Déjà prévisualisée : pas de seconde confirmation.
  document.body.addEventListener("htmx:confirm", function (e) {
    if (e.target !== form || form.dataset.previewed !== "1") return;
    delete form.dataset.previewed;
    e.preventDefault();
    e.detail.issueRequest(true);
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
