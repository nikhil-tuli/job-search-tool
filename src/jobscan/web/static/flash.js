/* Flash messages float above the page so they are visible wherever you are scrolled. */
(function () {
  'use strict';
  document.querySelectorAll('.flash').forEach(function (box) {
    box.title = 'Click to dismiss';
    box.addEventListener('click', function () { box.remove(); });
    if (box.classList.contains('ok')) { window.setTimeout(function () { box.remove(); }, 4000); }
  });
})();
