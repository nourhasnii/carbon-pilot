(function () {
  const panel = document.querySelector("[data-status-url]");
  if (!panel) return;
  const url = panel.dataset.statusUrl;
  const started = Date.now();
  const timer = window.setInterval(function () {
    if (Date.now() - started >= 120000) {
      window.clearInterval(timer);
      panel.insertAdjacentHTML("beforeend", "<p role=\"alert\">Le délai d'attente est dépassé. Actualisez la page pour vérifier le résultat.</p>");
      return;
    }
    fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (response) {
        if (!response.ok) throw new Error("status");
        return response.json();
      })
      .then(function (data) {
        if (data.done) {
          window.clearInterval(timer);
          window.location.reload();
        }
      })
      .catch(function () {
        panel.insertAdjacentHTML("beforeend", "<p role=\"alert\">La vérification du calcul est momentanément indisponible.</p>");
      });
  }, 2000);
}());
