/* Bouton « Générer le résumé » de la page résultat Portfolio AI. */
(function () {
  "use strict";

  function getCsrfToken() {
    var field = document.querySelector("[name=csrfmiddlewaretoken]");
    if (field && field.value) return field.value;
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function postJson(url, payload) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      body: JSON.stringify(payload || {}),
    }).then(function (response) {
      return response.json().catch(function () {
        return { ok: false, text: "", error: "Réponse inattendue du serveur." };
      });
    });
  }

  var button = document.getElementById("llm-summary-btn");
  if (!button) return;

  var text = document.getElementById("llm-summary-text");
  var status = document.getElementById("llm-summary-status");
  var errorBox = document.getElementById("llm-summary-error");
  var label = button.textContent;

  button.addEventListener("click", function () {
    errorBox.hidden = true;
    errorBox.textContent = "";
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "Génération en cours…";
    status.textContent = "Génération du résumé en cours…";

    postJson(button.dataset.url, {})
      .then(function (data) {
        if (data.ok) {
          text.textContent = data.text;
          status.textContent = data.fallback
            ? "Gemini est indisponible : l'explication de l'optimiseur est affichée à la place."
            : "Résumé généré.";
        } else {
          status.textContent = "";
          errorBox.textContent = data.error || "La génération a échoué.";
          errorBox.hidden = false;
        }
      })
      .catch(function () {
        status.textContent = "";
        errorBox.textContent = "Impossible de joindre le serveur. Réessayez.";
        errorBox.hidden = false;
      })
      .then(function () {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = label;
      });
  });
})();