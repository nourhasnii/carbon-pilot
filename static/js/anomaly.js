(function () {
  var el = document.getElementById("an-data"), canvas = document.getElementById("an");
  if (!el || !canvas || typeof Chart === "undefined" || !window.cpTheme) return;
  var d = JSON.parse(el.textContent), chart;
  var labels = d.series.map(function (p) { return p[0]; });
  function render() {
    var t = window.cpTheme(); if (chart) chart.destroy();
    var danger = getComputedStyle(document.documentElement).getPropertyValue("--danger").trim() || "#B42318";
    var at = labels.indexOf(d.date);
    chart = new Chart(canvas, {
      type: "line",
      data: { labels: labels, datasets: [
        { label: "Mesuré", data: d.series.map(function (p) { return p[1]; }), borderColor: t.ink, borderWidth: 2, pointRadius: 0, tension: .15 },
        { label: "Attendu ce jour-là", data: labels.map(function (l) { return l === d.date ? d.expected : null; }), borderColor: t.forecast, backgroundColor: t.forecast, pointStyle: "rectRot", pointRadius: 7, showLine: false },
        { label: "Anomalie", data: labels.map(function (l) { return l === d.date ? d.actual : null; }), borderColor: danger, backgroundColor: danger, pointStyle: "rectRot", pointRadius: 8, showLine: false }
      ] },
      options: { responsive: true, maintainAspectRatio: false, animation: t.animation, interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: true, position: "bottom" }, tooltip: { callbacks: { title: function (i) { return t.fmtLong(i[0].label); } } } },
        scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 9, callback: function (v) { return t.fmtShort(this.getLabelForValue(v)); } } },
                  y: { grid: { color: t.grid }, title: { display: true, text: "kWh par jour" } } } }
    });
  }
  render();
  document.addEventListener("cp-theme-change", render);
})();
