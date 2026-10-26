/* Thème commun des graphiques : couleurs lues à chaque appel depuis le CSS (donc valables en clair comme en sombre). */
window.cpTheme = function () {
  var css = getComputedStyle(document.documentElement);
  function v(n, f) { return (css.getPropertyValue(n) || "").trim() || f; }
  function rgba(hex, a) {
    var m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
    return m ? "rgba(" + parseInt(m[1], 16) + "," + parseInt(m[2], 16) + "," + parseInt(m[3], 16) + "," + a + ")" : hex;
  }
  function d(iso) { var p = iso.split("-"); return new Date(+p[0], +p[1] - 1, +p[2]); }
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var t = {
    ink: v("--ink", "#0F2A2E"), soft: v("--ink-soft", "#3C5558"), forecast: v("--forecast", "#B45309"), actual: v("--actual", "#1D4ED8"), ok: v("--ok", "#146C43"),
    grid: v("--rule", "#CBD6D8"), surface: v("--surface", "#FFFFFF"), animation: reduce ? false : { duration: 700 },
    fmtShort: function (iso) { return d(iso).toLocaleDateString("fr-FR", { day: "numeric", month: "short" }); },
    fmtLong: function (iso) { return d(iso).toLocaleDateString("fr-FR", { weekday: "short", day: "numeric", month: "long", year: "numeric" }); }
  };
  t.fill = rgba(t.ink, 0.1);
  if (window.Chart) { Chart.defaults.font.family = v("--font-ui", "sans-serif"); Chart.defaults.font.size = 13; Chart.defaults.color = t.soft; }
  return t;
};
