(function () {
  var el = document.getElementById("fx-data");
  if (!el || typeof Chart === "undefined" || !window.cpTheme) return;
  var d = JSON.parse(el.textContent), charts = {};
  function mk(id, cfg) { var c = document.getElementById(id); if (!c) return; if (charts[id]) charts[id].destroy(); charts[id] = new Chart(c, cfg); }
  function render() {
    var t = window.cpTheme();
    var base = { responsive: true, maintainAspectRatio: false, animation: t.animation };
    var v = d.validation || {};
    if (v.dates && v.dates.length) {
      mk("val", { type: "line", data: { labels: v.dates, datasets: [
        { label: "Réel", data: v.actual, borderColor: t.ink, borderWidth: 2, pointRadius: 0, tension: .15 },
        { label: "Prévu par le modèle retenu", data: v.best, borderColor: t.forecast, borderWidth: 2, borderDash: [7, 5], pointRadius: 0, tension: .15 },
        { label: "Baseline (J-7)", data: v.naive, borderColor: t.actual, borderWidth: 1.5, pointRadius: 0, tension: .15 }
      ] }, options: Object.assign({}, base, {
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: false }, tooltip: { callbacks: { title: function (i) { return t.fmtLong(i[0].label); } } } },
        scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 8, callback: function (x) { return t.fmtShort(this.getLabelForValue(x)); } } },
                  y: { grid: { color: t.grid }, title: { display: true, text: "kWh par jour" } } } }) });
    }
    if (d.candidates && d.candidates.length) {
      mk("cmp", { type: "bar", data: { labels: d.candidates.map(function (c) { return c.label + (c.best ? " (retenu)" : ""); }), datasets: [
        { label: "MAE", data: d.candidates.map(function (c) { return c.mae; }), backgroundColor: t.actual },
        { label: "RMSE", data: d.candidates.map(function (c) { return c.rmse; }), backgroundColor: t.forecast }
      ] }, options: Object.assign({}, base, { indexAxis: "y",
        plugins: { legend: { display: true, position: "bottom" } },
        scales: { x: { grid: { color: t.grid }, beginAtZero: true, title: { display: true, text: "Erreur en kWh par jour (plus petit = mieux)" } }, y: { grid: { display: false } } } }) });
    }
    if (d.importance && d.importance.length) {
      mk("imp", { type: "bar", data: { labels: d.importance.map(function (r) { return r.feature; }), datasets: [
        { label: "Part de l'influence (%)", data: d.importance.map(function (r) { return r.share; }), backgroundColor: t.ok }
      ] }, options: Object.assign({}, base, { indexAxis: "y",
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (c) { return c.parsed.x + " %"; } } } },
        scales: { x: { grid: { color: t.grid }, beginAtZero: true, title: { display: true, text: "Part de l'influence (%)" } }, y: { grid: { display: false } } } }) });
    }
  }
  render();
  document.addEventListener("cp-theme-change", render);
})();
