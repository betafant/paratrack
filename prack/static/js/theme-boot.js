// Runs before the first paint, so the page never flashes the wrong theme.
// Dark unless the visitor chose otherwise, or their system asks for light and they have not chosen.
(function () {
  var theme = 'dark';
  try {
    var saved = localStorage.getItem('prack.theme');
    if (saved === 'light' || saved === 'dark') theme = saved;
    else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) theme = 'light';
  } catch (e) {
    /* storage can be blocked; the default stands */
  }
  document.documentElement.setAttribute('data-theme', theme);
})();
