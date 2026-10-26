(function () {
  var el = document.getElementById("chart-data"), canvas = document.getElementById("trend");
  if (!el || !canvas || typeof Chart === "undefined" || !window.cpTheme) return;
  var d = JSON.parse(el.textContent), chart;
  function render() {
    var t = window.cpTheme();
    if (chart) chart.destroy();
    chart = new Chart(canvas, {
      type: "line",
      data: { labels: d.labels, datasets: [{ label: "kWh", data: d.values, borderColor: t.ink, backgroundColor: t.fill, fill: true, tension: .15, pointRadius: d.values.length > 60 ? 0 : 2, borderWidth: 2 }] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: t.animation,
        plugins: { legend: { display: false }, tooltip: { callbacks: { title: function (items) { return t.fmtLong(items[0].label); } } } },
        scales: {
          x: { grid: { display: false }, ticks: { maxTicksLimit: 8, callback: function (v) { return t.fmtShort(this.getLabelForValue(v)); } } },
          y: { beginAtZero: true, grid: { color: t.grid }, title: { display: true, text: "kWh" } }
        }
      }
    });
  }
  render();
  document.addEventListener("cp-theme-change", render);
})();
