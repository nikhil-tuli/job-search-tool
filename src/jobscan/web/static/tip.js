// Tooltips for elements with a data-tip attribute. Shown on hover and keyboard focus, and
// positioned from the element so a scrolling table cannot clip them.
(function () {
  'use strict';
  var tip = null;

  function hide() {
    if (tip) { tip.hidden = true; }
  }

  function show(el) {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'tooltip';
      tip.setAttribute('role', 'tooltip');
      document.body.appendChild(tip);
    }
    tip.textContent = el.getAttribute('data-tip');
    tip.hidden = false;
    var box = el.getBoundingClientRect();
    var width = tip.offsetWidth;
    var left = Math.min(Math.max(8, box.left), window.innerWidth - width - 8);
    var below = box.bottom + 6;
    var top = below + tip.offsetHeight > window.innerHeight ? box.top - tip.offsetHeight - 6 : below;
    tip.style.left = left + 'px';
    tip.style.top = Math.max(8, top) + 'px';
  }

  function target(event) {
    return event.target.closest ? event.target.closest('[data-tip]') : null;
  }

  document.addEventListener('mouseover', function (e) { var el = target(e); if (el) { show(el); } });
  document.addEventListener('mouseout', function (e) { if (target(e)) { hide(); } });
  document.addEventListener('focusin', function (e) { var el = target(e); if (el) { show(el); } });
  document.addEventListener('focusout', function (e) { if (target(e)) { hide(); } });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { hide(); } });
  window.addEventListener('scroll', hide, true);
})();
