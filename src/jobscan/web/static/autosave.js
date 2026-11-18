/* Saves a row's status dropdown in the background so the roles list never reloads. */
(function () {
  'use strict';
  var table = document.querySelector('table.roles');
  if (!table) { return; }
  var filterStatus = (table.dataset.filterStatus || '').split('|').filter(Boolean);

  function note(row, text, isError) {
    var el = row.querySelector('.note');
    if (!el) { return; }
    el.textContent = text;
    el.classList.toggle('error', !!isError);
    if (!isError && text) {
      window.setTimeout(function () { if (el.textContent === text) { el.textContent = ''; } }, 2500);
    }
  }

  // Rating buttons in a row: pressing the pressed one clears the rating.
  table.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('button[data-rate]') : null;
    if (!button) { return; }
    event.preventDefault();
    var form = button.form;
    var buttons = form.querySelectorAll('button[data-rate]');
    var body = new URLSearchParams(new FormData(form));
    body.set('rating', button.getAttribute('aria-pressed') === 'true' ? 'clear' : button.value);
    buttons.forEach(function (b) { b.disabled = true; });
    note(form, 'Saving…', false);
    fetch(form.action, { method: 'POST', body: body, headers: { 'X-Panel': '1' } })
      .then(function (response) {
        if (!response.ok) { throw new Error(response.status); }
        return response.json();
      })
      .then(function (data) {
        buttons.forEach(function (b) {
          b.disabled = false;
          b.setAttribute('aria-pressed', String(b.value === data.rating));
        });
        note(form, 'Saved ✓', false);
      })
      .catch(function () {
        buttons.forEach(function (b) { b.disabled = false; });
        note(form, 'Could not save. Try again.', true);
      });
  });

  table.addEventListener('focusin', function (event) {
    var select = event.target;
    if (select.matches && select.matches('select[data-autosave]')) { select.dataset.prev = select.value; }
  });

  table.addEventListener('change', function (event) {
    var select = event.target;
    if (!select.matches || !select.matches('select[data-autosave]')) { return; }
    var form = select.form;
    var row = select.closest('tr');
    var previous = select.dataset.prev !== undefined ? select.dataset.prev : select.value;
    // Build the body first: a disabled control is left out of FormData.
    var body = new URLSearchParams(new FormData(form));
    select.disabled = true;
    note(row, 'Saving…', false);
    fetch(form.action, {
      method: 'POST',
      body: body,
      headers: { 'X-Panel': '1' }
    }).then(function (response) {
      return response.json().then(function (data) { return { ok: response.ok, data: data }; });
    }).then(function (result) {
      select.disabled = false;
      if (!result.ok) {
        select.value = previous;
        note(row, result.data.error || 'Could not save', true);
        return;
      }
      select.dataset.prev = select.value;
      var key = form.querySelector('input[name=ids]').value;
      row.querySelector('td.applied').textContent = result.data.applied_on[key] || '';
      var fitCell = row.querySelector('td.fit');
      if (fitCell && result.data.fit && result.data.fit[key] !== undefined) {
        fitCell.innerHTML = result.data.fit[key];  // server-rendered, escaped by the template
      }
      var outside = filterStatus.length > 0 && filterStatus.indexOf(select.value) < 0;
      row.classList.toggle('stale', !!outside);
      note(row, 'Saved ✓', false);
    }).catch(function () {
      select.disabled = false;
      select.value = previous;
      note(row, 'Could not save. Try again.', true);
    });
  });
})();
