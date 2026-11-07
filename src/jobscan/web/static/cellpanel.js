/* Eval: a click on a count shows its roles in one panel under the table. */
(function () {
  'use strict';
  var panels = document.querySelectorAll('.cell-panel');
  var buttons = document.querySelectorAll('.cell-btn');
  if (!panels.length) { return; }
  function show(id) {
    panels.forEach(function (p) { p.hidden = (p.id !== 'cell-' + id); });
    buttons.forEach(function (b) {
      var on = (b.getAttribute('data-cell') === id);
      b.classList.toggle('on', on);
      b.setAttribute('aria-expanded', on ? 'true' : 'false');
    });
  }
  show('');
  buttons.forEach(function (b) {
    b.addEventListener('click', function () {
      var id = b.getAttribute('data-cell');
      show(b.classList.contains('on') ? '' : id);
    });
  });
  document.querySelectorAll('.cell-close').forEach(function (c) {
    c.addEventListener('click', function () { show(''); });
  });
})();
