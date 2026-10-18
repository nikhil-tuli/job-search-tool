/* Side panel for the roles list: opens a role's edit form without leaving the page. */
(function () {
  'use strict';
  var panel = document.getElementById('panel');
  var scrim = document.getElementById('scrim');
  var body = document.getElementById('panel-body');
  if (!panel) { return; }
  var rows = Array.prototype.slice.call(document.querySelectorAll('tr.row'));
  var current = -1;
  var returnFocus = null;
  var loadToken = 0;

  function form() { return body.querySelector('form.edit'); }
  function snapshot() { var f = form(); return f ? new URLSearchParams(new FormData(f)).toString() : ''; }
  var saved = '';
  function dirty() { return form() !== null && snapshot() !== saved; }
  function confirmDiscard() { return !dirty() || window.confirm('Discard unsaved changes?'); }

  function show(html) {
    body.innerHTML = html;
    saved = snapshot();
    var f = form();
    if (f) { f.addEventListener('submit', submit); }
    var first = body.querySelector('select, input[type=text], textarea');
    if (first) { first.focus(); }
  }

  function mark(index) {
    rows.forEach(function (row, i) { row.classList.toggle('sel', i === index); });
  }

  function open(index) {
    if (index < 0 || index >= rows.length || (current >= 0 && !confirmDiscard())) { return; }
    if (current < 0) { returnFocus = document.activeElement; }
    current = index;
    mark(index);
    panel.hidden = false;
    scrim.hidden = false;
    var token = ++loadToken;
    body.textContent = 'Loading…';
    fetch(rows[index].dataset.panel, { headers: { Accept: 'text/html' } })
      .then(function (r) { if (!r.ok) { throw new Error(r.status); } return r.text(); })
      .then(function (html) { if (token === loadToken) { show(html); } })
      .catch(function () {
        if (token === loadToken) { body.textContent = 'Could not load this role. Close the panel and try again.'; }
      });
  }

  function close() {
    if (current < 0 || !confirmDiscard()) { return; }
    loadToken++;
    current = -1;
    mark(-1);
    panel.hidden = true;
    scrim.hidden = true;
    body.textContent = '';
    if (returnFocus && document.contains(returnFocus)) { returnFocus.focus(); }
  }

  function submit(event) {
    event.preventDefault();
    var f = event.target;
    var token = loadToken;
    var button = f.querySelector('button[type=submit]');
    if (button) { button.disabled = true; }
    fetch(f.action || window.location.href, {
      method: 'POST',
      body: new URLSearchParams(new FormData(f)),
      headers: { 'X-Panel': '1' }
    }).then(function (r) {
      if (token !== loadToken) { return null; }
      var json = (r.headers.get('Content-Type') || '').indexOf('application/json') === 0;
      return (json ? r.json() : r.text()).then(function (data) { return { ok: r.ok, json: json, data: data }; });
    }).then(function (res) {
      if (!res) { return; }
      if (res.ok && res.json) { applySaved(res.data); }
      else if (!res.json) { show(res.data); }
      else { throw new Error('unexpected response'); }
    }).catch(function () {
      var f2 = form();
      if (f2) { f2.insertAdjacentHTML('beforeend', '<p class="error">Could not save. Try again.</p>'); }
      var b = f2 && f2.querySelector('button[type=submit]');
      if (b) { b.disabled = false; }
    });
  }

  function applySaved(data) {
    var row = rows[current];
    var select = row.querySelector('select[name=status]');
    if (select) { select.value = data.status; }
    row.querySelector('td.applied').textContent = data.applied_on;
    saved = snapshot();
    var f = form();
    var b = f && f.querySelector('button[type=submit]');
    if (b) { b.disabled = false; }
    var applied = f && f.querySelector('input[name=applied_on]');
    if (applied) { applied.value = data.applied_on; saved = snapshot(); }
    var note = body.querySelector('.saved');
    if (!note && f) { f.insertAdjacentHTML('beforeend', '<p class="saved" role="status">Saved</p>'); }
  }

  rows.forEach(function (row, index) {
    row.addEventListener('click', function (event) {
      var link = event.target.closest('a');
      if (link && !link.classList.contains('row-link')) { return; }
      if (!link && event.target.closest('input, select, button, label, form')) { return; }
      if (link && (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0)) { return; }
      event.preventDefault();
      open(index);
    });
  });
  scrim.addEventListener('click', close);
  document.getElementById('panel-close').addEventListener('click', close);
  document.addEventListener('keydown', function (event) {
    if (current < 0) { return; }
    if (event.key === 'Escape') { close(); return; }
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) { return; }
    if (event.key === 'ArrowDown') { event.preventDefault(); open(current + 1); }
    if (event.key === 'ArrowUp') { event.preventDefault(); open(current - 1); }
  });
})();
