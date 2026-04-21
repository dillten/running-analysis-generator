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

document.addEventListener('DOMContentLoaded', function () {
  var btn = document.querySelector('.theme-toggle');
  if (btn) {
    btn.textContent = document.documentElement.getAttribute('data-theme') === 'light' ? 'Dark' : 'Light';
  }
});
