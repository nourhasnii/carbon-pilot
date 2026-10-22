(function () {
  var el = document.getElementById("fc-data"), canvas = document.getElementById("fc");
  if (!el || !canvas || typeof Chart === "undefined" || !window.cpTheme) return;
  var d = JSON.parse(el.textContent), chart;
  var labels = []; [d.history, d.forecast].forEach(function (a) { a.forEach(function (p) { if (labels.indexOf(p[0]) < 0) labels.push(p[0]); }); });
  labels.sort();
  function series(arr) { var m = {}; arr.forEach(function (p) { m[p[0]] = p[1]; }); return labels.map(function (l) { return l in m ? m[l] : null; }); }
  // La prévision est reliée au dernier point mesuré pour que la courbe se lise d'un seul trait.
  var fcSeries = series(d.forecast);
  if (d.history.length && d.forecast.length) { var last = d.history[d.history.length - 1]; var i = labels.indexOf(last[0]); if (fcSeries[i] === null) fcSeries[i] = last[1]; }
  var iv = d.interval || [], lo = {}, hi = {};
  iv.forEach(function (r) { lo[r[0]] = r[1]; hi[r[0]] = r[2]; });
  // La fourchette part du dernier point mesuré (largeur nulle) pour que la bande s'ouvre en éventail.
  if (d.history.length && iv.length) { var l2 = d.history[d.history.length - 1]; lo[l2[0]] = l2[1]; hi[l2[0]] = l2[1]; }
  function band(m) { return labels.map(function (l) { return l in m ? m[l] : null; }); }
  function render() {
    var t = window.cpTheme();
    if (chart) chart.destroy();
    chart = new Chart(canvas, {
      type: "line",
      data: { labels: labels, datasets: [
        { label: "Bas de la fourchette", data: band(lo), borderWidth: 0, pointRadius: 0, fill: false, order: 5 },
        { label: "Fourchette à 80 %", data: band(hi), borderWidth: 0, pointRadius: 0, fill: "-1", backgroundColor: t.forecast + "33", order: 5 },
        { label: "Mesuré", data: series(d.history), borderColor: t.ink, borderWidth: 2, pointRadius: 0, tension: .15 },
        { label: "Prévu", data: fcSeries, borderColor: t.forecast, borderWidth: 2.5, borderDash: [7, 5], pointRadius: 2, pointBackgroundColor: t.forecast, tension: .15 },
        { label: "Mesuré sur les jours prévus", data: series(d.actual), borderColor: t.actual, backgroundColor: t.actual, pointStyle: "rectRot", pointRadius: 5, showLine: false }
      ] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: t.animation,
        plugins: { legend: { display: false }, tooltip: { callbacks: { title: function (items) { return t.fmtLong(items[0].label); } } } },
        scales: {
          x: { grid: { display: false }, ticks: { maxTicksLimit: 9, callback: function (v) { return t.fmtShort(this.getLabelForValue(v)); } } },
          y: { grid: { color: t.grid }, title: { display: true, text: "kWh par jour" } }
        }
      }
    });
  }
  render();
  document.addEventListener("cp-theme-change", render);
})();
