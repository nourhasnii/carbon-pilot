/* Comportements d'interface : thème clair/sombre, boutons en chargement, dépôt de fichier, export PNG des graphiques. */
(function () {
  "use strict";
  var root = document.documentElement;

  /* ---- Thème ---- */
  function currentTheme() { return root.getAttribute("data-theme") === "dark" ? "dark" : "light"; }
  function applyTheme(t, persist) {
    root.setAttribute("data-theme", t);
    root.setAttribute("data-bs-theme", t);
    if (persist) { try { localStorage.setItem("cp-theme", t); } catch (e) {} }
    syncSwitches();
    document.dispatchEvent(new CustomEvent("cp-theme-change", { detail: { theme: t } }));
  }
  function syncSwitches() {
    var dark = currentTheme() === "dark";
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) { b.setAttribute("aria-checked", dark ? "true" : "false"); });
  }
  document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
    b.addEventListener("click", function () { applyTheme(currentTheme() === "dark" ? "light" : "dark", true); });
  });
  syncSwitches();
  if (window.matchMedia) {  // suit le système tant que l'utilisateur n'a pas choisi
    var mq = matchMedia("(prefers-color-scheme: dark)");
    var follow = function (e) { var stored = null; try { stored = localStorage.getItem("cp-theme"); } catch (x) {} if (!stored) applyTheme(e.matches ? "dark" : "light", false); };
    if (mq.addEventListener) mq.addEventListener("change", follow);
  }

  /* ---- Contrôle de saisie dans le navigateur (le serveur revérifie toujours tout) ---- */
  function isField(el) { return /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) && el.type !== "hidden" && el.type !== "submit" && el.type !== "button" && el.willValidate; }
  function fieldWrap(el) { return el.closest(".mb-3") || el.parentNode; }
  function clearError(el) {
    var wrap = fieldWrap(el);
    wrap.querySelectorAll(".js-error").forEach(function (n) {
      var ds = (el.getAttribute("aria-describedby") || "").split(" ").filter(function (x) { return x && x !== n.id; });
      if (ds.length) el.setAttribute("aria-describedby", ds.join(" ")); else el.removeAttribute("aria-describedby");
      n.remove();
    });
    if (!wrap.querySelector(".field-error")) el.removeAttribute("aria-invalid");
  }
  function setError(el, msg) {
    clearError(el);
    var d = document.createElement("div");
    d.className = "field-error js-error"; d.id = (el.id || el.name) + "_err"; d.setAttribute("role", "alert"); d.textContent = msg;
    fieldWrap(el).appendChild(d);
    el.setAttribute("aria-invalid", "true");
    var ds = (el.getAttribute("aria-describedby") || "").split(" ").filter(Boolean); ds.push(d.id);
    el.setAttribute("aria-describedby", ds.join(" "));
  }
  function problem(el) {
    if (el.type === "file" && el.files && el.files.length) {
      var f = el.files[0], max = parseInt(el.getAttribute("data-max-bytes") || "0", 10);
      if (max && f.size > max) return "Fichier trop volumineux (" + Math.round(max / 1048576) + " Mo maximum).";
      if (f.size === 0) return "Le fichier est vide.";
      var acc = (el.getAttribute("accept") || "").split(",").filter(Boolean);
      if (acc.length && !acc.some(function (x) { return f.name.toLowerCase().endsWith(x.trim()); })) return "Format non supporté (" + acc.join(", ") + ").";
    }
    var v = el.validity; if (v.valid) return "";
    if (v.valueMissing) return el.tagName === "SELECT" ? "Choisissez une option." : el.type === "file" ? "Choisissez un fichier." : "Ce champ est obligatoire.";
    if (v.badInput || v.typeMismatch) return "Valeur non reconnue.";
    if (v.patternMismatch) return el.getAttribute("title") || "Format invalide.";
    if (v.tooShort) return "Au moins " + el.getAttribute("minlength") + " caractères.";
    if (v.tooLong) return "Au plus " + el.getAttribute("maxlength") + " caractères.";
    if ((v.rangeUnderflow || v.rangeOverflow) && /^(date|datetime-local)$/.test(el.type)) {
      var lim = new Date(el.getAttribute(v.rangeOverflow ? "max" : "min")), opts = el.type === "date" ? { dateStyle: "short" } : { dateStyle: "short", timeStyle: "short" };
      var txt = isNaN(lim) ? "" : lim.toLocaleString("fr-FR", opts);
      return v.rangeOverflow ? "La date ne peut pas être postérieure au " + txt + " (pas de date future)." : "La date ne peut pas être antérieure au " + txt + ".";
    }
    if (v.rangeUnderflow) return "La valeur doit être supérieure ou égale à " + el.getAttribute("min") + ".";
    if (v.rangeOverflow) return "La valeur doit être inférieure ou égale à " + el.getAttribute("max") + ".";
    if (v.stepMismatch) return "Trop de décimales (pas de " + el.getAttribute("step") + ").";
    return el.validationMessage || "Valeur invalide.";
  }
  // Erreurs déjà affichées par le serveur : on les relie à leur champ pour les lecteurs d'écran
  document.querySelectorAll("form .mb-3 .field-error").forEach(function (n, i) {
    var el = fieldWrap(n).querySelector("input, select, textarea"); if (!el) return;
    if (n.id && (el.getAttribute("aria-describedby") || "").indexOf(n.id) >= 0) return;  // déjà relié par Django
    if (!n.id) n.id = (el.id || "f" + i) + "_serr";
    el.setAttribute("aria-invalid", "true");
    var ds = (el.getAttribute("aria-describedby") || "").split(" ").filter(Boolean); if (ds.indexOf(n.id) < 0) ds.push(n.id);
    el.setAttribute("aria-describedby", ds.join(" "));
  });
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || form.hasAttribute("data-no-validate") || (form.method || "").toLowerCase() !== "post") return;
    var first = null;
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!isField(el) || el.disabled) return;
      var msg = problem(el);
      if (msg) { setError(el, msg); if (!first) first = el; } else { clearError(el); }
    });
    if (first) { e.preventDefault(); e.stopImmediatePropagation(); first.focus(); }
  }, true);
  ["input", "change", "blur"].forEach(function (ev) {
    document.addEventListener(ev, function (e) {
      var el = e.target;
      if (!(el instanceof HTMLElement) || !el.form || el.form.hasAttribute("data-no-validate") || (el.form.method || "").toLowerCase() !== "post" || !isField(el)) return;
      if (ev === "input" && !fieldWrap(el).querySelector(".js-error")) return;  // pas d'erreur tant qu'on tape sans avoir fauté
      var msg = problem(el); if (msg) setError(el, msg); else clearError(el);
    }, true);
  });

  /* ---- Boutons : indicateur de chargement, anti double envoi ---- */
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || form.hasAttribute("data-no-loading") || (form.method || "").toLowerCase() !== "post") return;
    var btn = form.querySelector('button[type="submit"]');
    if (!btn || btn.getAttribute("data-loading")) return;
    btn.setAttribute("data-loading", "1");
    btn.setAttribute("aria-busy", "true");
    btn.classList.add("btn-loading");
    var sp = document.createElement("span");
    sp.className = "spinner-border spinner-border-sm me-2"; sp.setAttribute("aria-hidden", "true");
    btn.insertBefore(sp, btn.firstChild);
    setTimeout(function () { btn.disabled = true; }, 0);
  });
  window.addEventListener("pageshow", function (e) {  // retour arrière : on rétablit les boutons
    if (!e.persisted) return;
    document.querySelectorAll("[data-loading]").forEach(function (b) {
      b.disabled = false; b.removeAttribute("data-loading"); b.removeAttribute("aria-busy"); b.classList.remove("btn-loading");
      var sp = b.querySelector(".spinner-border"); if (sp) sp.remove();
    });
  });

  /* ---- Dépôt de fichier (amélioration progressive) ---- */
  document.querySelectorAll('input[type="file"][data-dropzone]').forEach(function (input) {
    var zone = document.createElement("div"); zone.className = "dropzone";
    input.parentNode.insertBefore(zone, input); zone.appendChild(input);
    var hint = document.createElement("p"); hint.className = "dz-hint"; hint.setAttribute("aria-live", "polite");
    hint.textContent = "Vous pouvez aussi glisser-déposer un fichier ici."; zone.appendChild(hint);
    function show() { hint.textContent = input.files && input.files.length ? "Fichier sélectionné : " + input.files[0].name : "Vous pouvez aussi glisser-déposer un fichier ici."; }
    input.addEventListener("change", show);
    ["dragenter", "dragover"].forEach(function (ev) { zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add("is-over"); }); });
    ["dragleave", "drop"].forEach(function (ev) { zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.remove("is-over"); }); });
    zone.addEventListener("drop", function (e) { if (e.dataTransfer && e.dataTransfer.files.length) { input.files = e.dataTransfer.files; show(); } });
  });

  /* ---- Export PNG d'un graphique ---- */
  document.querySelectorAll("[data-chart-download]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var src = document.getElementById(btn.getAttribute("data-chart-download"));
      if (!src) return;
      var c = document.createElement("canvas"); c.width = src.width; c.height = src.height;
      var ctx = c.getContext("2d");
      ctx.fillStyle = getComputedStyle(root).getPropertyValue("--surface").trim() || "#fff";
      ctx.fillRect(0, 0, c.width, c.height); ctx.drawImage(src, 0, 0);
      var a = document.createElement("a"); a.download = (btn.getAttribute("data-filename") || "graphique") + ".png"; a.href = c.toDataURL("image/png");
      document.body.appendChild(a); a.click(); a.remove();
    });
  });
})();
