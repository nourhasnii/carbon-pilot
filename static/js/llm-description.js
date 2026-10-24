/* Bouton « Générer la description » du formulaire de projet. */
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

  var button = document.getElementById("llm-description-btn");
  if (!button) return;

  var status = document.getElementById("llm-description-status");
  var errorBox = document.getElementById("llm-description-error");
  var titleField = document.getElementById(button.dataset.titleField);
  var targetField = document.getElementById(button.dataset.targetField);
  var label = button.textContent;

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  button.addEventListener("click", function () {
    errorBox.hidden = true;
    errorBox.textContent = "";
    var title = titleField ? titleField.value.trim() : "";
    if (title.length < 3 || title.length > 120) {
      showError("Saisissez d'abord un nom de projet de 3 à 120 caractères.");
      if (titleField) titleField.focus();
      return;
    }
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "Génération en cours…";
    status.textContent = "Génération de la description en cours…";

    postJson(button.dataset.url, { title: title })
      .then(function (data) {
        if (data.ok) {
          targetField.value = data.text;
          status.textContent = "Description proposée. Vous pouvez la modifier avant d'enregistrer.";
          targetField.focus();
        } else {
          status.textContent = "";
          showError(data.error || "La génération a échoué.");
        }
      })
      .catch(function () {
        status.textContent = "";
        showError("Impossible de joindre le serveur. Réessayez.");
      })
      .then(function () {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = label;
      });
  });
})();