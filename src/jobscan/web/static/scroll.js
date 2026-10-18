/* Keeps the scroll position across a form submit that redirects back to the same page. */
(function () {
  'use strict';
  var KEY = 'jobscan.scroll';
  var MAX_AGE_MS = 30000;

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (event.defaultPrevented || !form || (form.method || '').toLowerCase() !== 'post') { return; }
    try {
      sessionStorage.setItem(KEY, JSON.stringify({
        path: window.location.pathname, y: window.scrollY, at: Date.now()
      }));
    } catch (e) { /* storage unavailable: the page just starts at the top */ }
  });

  try {
    var raw = sessionStorage.getItem(KEY);
    if (!raw) { return; }
    sessionStorage.removeItem(KEY);
    var saved = JSON.parse(raw);
    if (saved.path === window.location.pathname && Date.now() - saved.at < MAX_AGE_MS) {
      var restore = function () { window.scrollTo(0, saved.y); };
      restore();
      // Once more after load, in case the browser applied its own scroll position meanwhile.
      window.addEventListener('load', restore);
    }
  } catch (e) { /* ignore */ }
})();
