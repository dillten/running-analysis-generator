(function () {
  document.documentElement.setAttribute('data-theme', localStorage.getItem('theme') || 'dark');
})();

function toggleTheme() {
  var next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  var btn = document.querySelector('.theme-toggle');
  if (btn) btn.textContent = next === 'light' ? 'Dark' : 'Light';
}

// Shared by any page-level chart code that needs to branch on the active theme.
function isLightTheme() {
  return document.documentElement.getAttribute('data-theme') === 'light';
}

// Registers cb to run whenever the user toggles light/dark theme (any tab/page).
function onThemeChange(cb) {
  new MutationObserver(cb).observe(document.documentElement, {
    attributes: true, attributeFilter: ['data-theme'],
  });
}

// Chart.js axis/grid/tooltip colors that adapt to the active theme. Used by
// pages with Chart.js line/bar charts (sleep, body, analysis, mile-splits,
// predictor) so grid lines and labels stay legible in light mode instead of
// the dark-only literals those charts used to hardcode.
function chartThemeColors() {
  var light = isLightTheme();
  return {
    grid:          light ? 'rgba(0,0,0,0.08)'  : 'rgba(255,255,255,0.04)',
    tick:          light ? '#6b6b63'            : '#64748b',
    legend:        light ? '#4b4b45'            : '#94a3b8',
    tooltipBg:     light ? '#ffffff'            : '#0f1c30',
    tooltipBorder: light ? 'rgba(0,0,0,0.15)'   : '#1e2d45',
    tooltipText:   light ? '#1a1a1a'            : '#e2e8f0',
  };
}

// Applies chartThemeColors() to a live Chart.js instance's axes/legend/tooltip
// and redraws it. Call from onThemeChange() with each chart the page keeps a
// reference to.
function applyChartTheme(chart) {
  if (!chart || !chart.canvas) return;
  var c = chartThemeColors();
  var scales = chart.options.scales || {};
  ['x', 'y'].forEach(function (axis) {
    var scale = scales[axis];
    if (!scale) return;
    if (scale.grid) scale.grid.color = c.grid;
    if (scale.ticks) scale.ticks.color = c.tick;
    if (scale.title) scale.title.color = c.tick;
  });
  var plugins = chart.options.plugins || {};
  if (plugins.legend && plugins.legend.labels) plugins.legend.labels.color = c.legend;
  if (plugins.tooltip) {
    plugins.tooltip.backgroundColor = c.tooltipBg;
    plugins.tooltip.borderColor = c.tooltipBorder;
    plugins.tooltip.titleColor = c.tooltipText;
    plugins.tooltip.bodyColor = c.tooltipText;
  }
  chart.update();
}

document.addEventListener('DOMContentLoaded', function () {
  var btn = document.querySelector('.theme-toggle');
  if (btn) {
    btn.textContent = document.documentElement.getAttribute('data-theme') === 'light' ? 'Dark' : 'Light';
  }
});
