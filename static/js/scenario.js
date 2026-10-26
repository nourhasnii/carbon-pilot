(function () {
  var el = document.getElementById("sc-data"), canvas = document.getElementById("sc");
  if (!el || !canvas || typeof Chart === "undefined" || !window.cpTheme) return;
  var rows = JSON.parse(el.textContent).daily, chart;
  var labels = rows.map(function (r) { return r[0]; });
  function render() {
    var t = window.cpTheme();
    if (chart) chart.destroy();
    chart = new Chart(canvas, {
      type: "line",
      data: { labels: labels, datasets: [
        { label: "Sans action", data: rows.map(function (r) { return r[1]; }), borderColor: t.ink, borderWidth: 2, pointRadius: 0, tension: .15 },
        { label: "Avec action", data: rows.map(function (r) { return r[2]; }), borderColor: t.ok, backgroundColor: t.ok + "26", borderWidth: 2.5, pointRadius: 0, tension: .15, fill: "-1" }
      ] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: t.animation,
        interaction: { mode: "index", intersect: false },
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
