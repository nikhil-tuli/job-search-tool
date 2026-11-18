/* Closes a notice at once and tells the server, so it never shows again. */
(function () {
  'use strict';
  document.querySelectorAll('form[data-notice]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      fetch(form.action, {
        method: 'POST',
        body: new URLSearchParams(new FormData(form)),
        headers: { 'X-Panel': '1' }
      }).then(function (r) {
        if (r.ok) { form.remove(); } else { form.submit(); }
      }).catch(function () { form.submit(); });
    });
  });
})();
