/* Tick-box dropdowns: close one when you click elsewhere or press Escape. */
(function () {
  document.addEventListener('click', function (e) {
    Array.prototype.forEach.call(document.querySelectorAll('details.multi[open]'), function (d) {
      if (!d.contains(e.target)) { d.removeAttribute('open'); }
    });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') { return; }
    Array.prototype.forEach.call(document.querySelectorAll('details.multi[open]'), function (d) {
      d.removeAttribute('open');
    });
  });
})();
