/* Upload forms: choosing a file submits the form, so one click is enough. */
(function () {
  'use strict';
  document.querySelectorAll('form[data-autoupload]').forEach(function (form) {
    var input = form.querySelector('input[type=file]');
    if (!input) { return; }
    input.addEventListener('change', function () {
      if (input.files && input.files.length) { form.submit(); }
    });
  });
})();
