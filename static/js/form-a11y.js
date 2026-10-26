/* CarbonPilot — form accessibility helper.
   Associates server-rendered validation messages with their form controls so
   assistive technologies announce the error when the field is focused
   (WCAG 3.3.1 Error Identification, 3.3.3 Error Suggestion).
   Progressive enhancement only: the messages are already visible on screen and
   are also listed in the role="alert" summary at the top of each form, so the
   page stays fully usable when JavaScript is unavailable. */
(function () {
  "use strict";

  function appendDescribedBy(el, id) {
    var current = el.getAttribute("aria-describedby");
    if (!current || current.indexOf(id) === -1) {
      el.setAttribute("aria-describedby", current ? current + " " + id : id);
    }
  }

  function linkError(container, errorEl, index) {
    if (!container || !errorEl) { return; }
    var input = container.querySelector(
      'input:not([type="hidden"]), select, textarea'
    );
    if (!input) { return; }
    if (!errorEl.id) {
      errorEl.id = "cp-error-" + (input.id || "field" + index);
    }

    // Choice groups (radio/checkbox) are exposed through their wrapper.
    if (input.type === "radio" || input.type === "checkbox") {
      var group = container.querySelector('[role="radiogroup"]');
      if (group) {
        group.setAttribute("aria-invalid", "true");
        appendDescribedBy(group, errorEl.id);
        return;
      }
    }

    input.setAttribute("aria-invalid", "true");
    appendDescribedBy(input, errorEl.id);
  }

  // Workspace forms use a .cp-field wrapper.
  Array.prototype.forEach.call(
    document.querySelectorAll(".cp-field"),
    function (container, ci) {
      var errors = container.querySelectorAll(".cp-error, .field-error");
      Array.prototype.forEach.call(errors, function (el, i) {
        linkError(container, el, ci + "-" + i);
      });
    }
  );

  // Bootstrap-based forms (company profile) render .invalid-feedback blocks.
  Array.prototype.forEach.call(
    document.querySelectorAll(".invalid-feedback"),
    function (el, i) {
      var container =
        el.closest('.col-12, [class*="col-md-"], [class*="col-lg-"]') ||
        el.parentNode;
      linkError(container, el, "bs" + i);
    }
  );
})();
