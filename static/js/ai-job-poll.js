/* Poll the status of the asynchronous AI recommendation job.
 *
 * The server renders a <div data-ai-job> while a job is Pending/Running and
 * gives it the tenant-scoped JSON status URL. This script polls that endpoint
 * until the job reaches a terminal state, then shows the outcome and reloads
 * once so the freshly generated recommendations appear.
 *
 * Progressive enhancement: without JavaScript the form still queues the job and
 * the page still works — the banner simply stays as rendered and a manual
 * refresh reveals the results. The server remains the only source of truth; no
 * state is invented here.
 */
document.addEventListener('DOMContentLoaded', function () {
  var banner = document.querySelector('[data-ai-job]');
  if (!banner) return;

  var statusUrl = banner.getAttribute('data-status-url');
  if (!statusUrl) return;

  var jobId = banner.getAttribute('data-job-id') || '';
  var title = banner.querySelector('[data-ai-job-title]');
  var message = banner.querySelector('[data-ai-job-message]');
  var spinner = banner.querySelector('[data-ai-job-spinner]');
  var endpoint = statusUrl + '?job=' + encodeURIComponent(jobId);
  var timer = null;
  var stopped = false;

  function setMessage(text) {
    if (message) message.textContent = text;
  }

  function showOutcome(text, isError) {
    if (spinner) spinner.style.display = 'none';
    banner.classList.toggle('is-warning', !!isError);
    setMessage(text);
  }

  function finish(job) {
    stopped = true;
    if (timer) window.clearInterval(timer);
    var failed = job.status === 'FAILED';
    if (title) title.textContent = failed ? 'AI job failed' : 'AI job finished';
    showOutcome(
      job.message || (failed ? 'The AI job failed.' : 'The AI job finished.'),
      failed
    );
    // Reload once so the recommendation list reflects the new rows, after the
    // user has had a moment to read the result.
    window.setTimeout(function () {
      window.location.reload();
    }, 2500);
  }

  function poll() {
    if (stopped) return;
    fetch(endpoint, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (data) {
        var job = data && data.job;
        if (!job) {
          finish({ status: 'FAILED', message: 'The AI job is no longer available.' });
          return;
        }
        if (job.is_terminal) {
          finish(job);
          return;
        }
        if (title) title.textContent = job.status_label + '\u2026';
        if (job.status === 'RUNNING') {
          setMessage(
            'The local Qwen model is generating recommendations now. This can take a few minutes.'
          );
        }
      })
      .catch(function () {
        // Transient failure (server restart, network blip): stop polling so the
        // UI never spins forever. A manual refresh recovers the real state.
        stopped = true;
        if (timer) window.clearInterval(timer);
        if (title) title.textContent = 'AI job status unavailable';
        showOutcome(
          'The job status could not be checked. Refresh the page to see the latest state.',
          true
        );
      });
  }

  timer = window.setInterval(poll, 3000);
  poll();
});
