(function () {
  "use strict";

  function errorElement(field) {
    var id = field.getAttribute("data-error-id") || field.name + "-error";
    var error = document.getElementById(id);
    if (!error) {
      error = document.createElement("span");
      error.id = id;
      error.className = "cp-error";
      error.setAttribute("role", "alert");
      field.insertAdjacentElement("afterend", error);
    }
    return error;
  }

  function setError(field, message) {
    var error = errorElement(field);
    error.textContent = message || "";
    error.hidden = !message;
    field.setAttribute("aria-invalid", message ? "true" : "false");
  }

  function numberValue(field) {
    if (field.value.trim() === "") {
      return null;
    }
    var value = Number(field.value);
    return Number.isFinite(value) ? value : null;
  }

  function validateField(field) {
    var name = field.name;
    var value = field.value.trim();
    var number = numberValue(field);
    var message = "";

    if (field.required && !value) {
      message = "Ce champ est obligatoire.";
    } else if (number !== null && field.min !== "" && number < Number(field.min)) {
      message = "La valeur doit être supérieure ou égale à " + field.min + ".";
    } else if (number !== null && field.max !== "" && number > Number(field.max)) {
      message = "La valeur doit être inférieure ou égale à " + field.max + ".";
    } else if (name === "name" && value && !value.trim()) {
      message = "Le nom est obligatoire.";
    } else if (name === "currency" && value && !/^[A-Za-z]{3}$/.test(value)) {
      message = "La devise doit comporter exactement trois lettres.";
    }

    setError(field, message);
    return !message;
  }

  function validateDates(form) {
    var start = form.querySelector("[name='start_date']");
    var end = form.querySelector("[name='end_date']");
    if (!start || !end || !start.value || !end.value) {
      return true;
    }
    if (end.value < start.value) {
      setError(end, "La date de fin doit être postérieure ou égale à la date de début.");
      return false;
    }
    setError(end, "");
    return true;
  }

  function validateBudget(form) {
    var actual = form.querySelector("[name='actual_cost']");
    var committed = form.querySelector("[name='committed_spend']");
    if (!actual || !committed || !actual.value || !committed.value) {
      return true;
    }
    if (Number(committed.value) > Number(actual.value)) {
      setError(committed, "Les dépenses engagées ne peuvent pas dépasser le coût réel.");
      return false;
    }
    return true;
  }

  function attach(form) {
    var fields = form.querySelectorAll("input, select, textarea");
    fields.forEach(function (field) {
      field.addEventListener("blur", function () {
        validateField(field);
        validateDates(form);
        validateBudget(form);
      });
    });
    form.addEventListener("submit", function () {
      var valid = true;
      fields.forEach(function (field) {
        valid = validateField(field) && valid;
      });
      valid = validateDates(form) && valid;
      valid = validateBudget(form) && valid;
      if (!valid) {
        form.setAttribute("data-client-invalid", "true");
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form[data-investments-form]").forEach(attach);
  });
}());
