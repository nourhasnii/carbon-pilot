/* Progressive enhancement for CarbonPilot AI actions.
 *
 * An AI action is any <form data-ai-action> whose handler runs a slow job
 * (local Qwen inference via Ollama, or a scikit-learn training run) before
 * redirecting. While the browser waits for that request we:
 *   - disable every submit button in the form,
 *   - swap the button label for a spinner + status text (data-ai-label),
 *   - mark the form aria-busy and announce the state to assistive tech.
 *
 * The server keeps doing all the real work and error handling; this script
 * only adds visible/announced feedback. With JavaScript disabled the form
 * submits exactly as before, so nothing depends on it.
 */
document.addEventListener('DOMContentLoaded', function () {
  var forms = document.querySelectorAll('form[data-ai-action]');
  if (!forms.length) return;

  function statusText(form) {
    return form.getAttribute('data-ai-label') || 'Working\u2026';
  }

  function announce(form, text) {
    var status = form.querySelector('[data-ai-status]');
    if (!status) {
      status = document.createElement('p');
      status.className = 'cp-sr-only';
      status.setAttribute('data-ai-status', '');
      status.setAttribute('role', 'status');
      status.setAttribute('aria-live', 'polite');
      form.appendChild(status);
    }
    status.textContent = text;
  }

  function setBusy(form, busy) {
    form.setAttribute('aria-busy', busy ? 'true' : 'false');
    form.querySelectorAll('button').forEach(function (button) {
      if (busy) {
        if (!button.hasAttribute('data-ai-original')) {
          button.setAttribute('data-ai-original', button.innerHTML);
        }
        button.disabled = true;
        button.classList.add('is-loading');
        button.innerHTML =
          '<span class="cp-spinner" aria-hidden="true"></span> ' + statusText(form);
      } else if (button.hasAttribute('data-ai-original')) {
        button.innerHTML = button.getAttribute('data-ai-original');
        button.removeAttribute('data-ai-original');
        button.disabled = false;
        button.classList.remove('is-loading');
      }
    });
    announce(form, busy ? statusText(form) : '');
  }

  forms.forEach(function (form) {
    form.addEventListener('submit', function () {
      // Guard against a double submit (double click / Enter) while in flight.
      if (form.getAttribute('aria-busy') === 'true') return;
      setBusy(form, true);
    });
  });

  // If the user returns via the back/forward cache the DOM is restored with
  // the buttons still disabled — reset them so the form stays usable.
  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    forms.forEach(function (form) {
      setBusy(form, false);
    });
  });
});
