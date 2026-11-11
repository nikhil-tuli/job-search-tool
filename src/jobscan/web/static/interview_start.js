// Start screen: one card is active at a time; Start is enabled when the active card is valid.
(function () {
  'use strict';
  var cfg = window.INTERVIEW_START;
  var form = document.getElementById('start-form');
  var kindInput = document.getElementById('kind');
  var cards = form.querySelectorAll('.pick');
  var button = document.getElementById('start-button');
  var hint = document.getElementById('start-hint');
  var roleInput = document.getElementById('role');
  var search = document.getElementById('role-search');
  var list = document.getElementById('role-list');
  var title = document.getElementById('general-title');
  var ownList = document.getElementById('own-list');
  var add = document.getElementById('add-question');
  var timer = null;

  function ownBoxes() { return ownList.querySelectorAll('input[name=q]'); }

  function ownValid() {
    var seen = {}, count = 0;
    var boxes = ownBoxes();
    for (var i = 0; i < boxes.length; i++) {
      var v = boxes[i].value.trim();
      if (!v) { continue; }
      if (v.length < cfg.minChars) { return false; }
      var k = v.toLowerCase();
      if (!seen[k]) { seen[k] = 1; count++; }
    }
    return count >= 1;
  }

  function valid() {
    var kind = kindInput.value;
    if (kind === 'Company role') { return !!roleInput.value; }
    if (kind === 'General role') { return !!(title && title.value); }
    if (kind === 'My questions') { return ownValid(); }
    return false;
  }

  function refresh() {
    var kind = kindInput.value;
    cards.forEach(function (c) { c.classList.toggle('on', c.getAttribute('data-kind') === kind); });
    button.disabled = !(cfg.hasCv && !cfg.running && valid());
    hint.textContent = !cfg.hasCv ? 'Upload your CV first.' : cfg.running ? 'Finish your current interview first.' : (!kind ? 'Choose one of the three.' : '');
    add.disabled = ownBoxes().length >= cfg.maxQuestions;
  }

  function choose(kind) { kindInput.value = kind; refresh(); }

  cards.forEach(function (c) {
    function activate() { if (kindInput.value !== c.getAttribute('data-kind')) { choose(c.getAttribute('data-kind')); } }
    c.addEventListener('click', activate);
    c.addEventListener('focusin', activate);
  });

  function pick(el) {
    list.querySelectorAll('.choice').forEach(function (x) { x.classList.remove('on'); x.setAttribute('aria-selected', 'false'); });
    el.classList.add('on');
    el.setAttribute('aria-selected', 'true');
    roleInput.value = el.getAttribute('data-key');
    refresh();
  }

  list.addEventListener('click', function (e) {
    var el = e.target.closest('.choice');
    if (el) { pick(el); }
  });

  function showRoles(roles) {
    var current = roleInput.value;
    list.textContent = '';
    roles.forEach(function (r) {
      var el = document.createElement('div');
      el.className = 'choice';
      el.setAttribute('role', 'option');
      el.setAttribute('data-key', r.key);
      var strong = document.createElement('strong');
      strong.textContent = r.company;
      var small = document.createElement('small');
      small.textContent = r.title;
      el.appendChild(strong);
      el.appendChild(document.createElement('br'));
      el.appendChild(small);
      if (r.key === current) { el.classList.add('on'); el.setAttribute('aria-selected', 'true'); }
      list.appendChild(el);
    });
    if (!roles.length) {
      var none = document.createElement('small');
      none.textContent = 'No roles match.';
      list.appendChild(none);
    }
  }

  function loadRoles() {
    fetch(cfg.rolesUrl + '?q=' + encodeURIComponent(search.value.trim()), {headers: {Accept: 'application/json'}})
      .then(function (r) { return r.json(); }).then(function (d) { showRoles(d.roles); }).catch(function () {});
  }

  search.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(loadRoles, 250); });
  if (title) { title.addEventListener('change', refresh); }
  ownList.addEventListener('input', refresh);

  function renumber() {
    var boxes = ownBoxes();
    for (var i = 0; i < boxes.length; i++) {
      boxes[i].placeholder = 'Question ' + (i + 1);
      boxes[i].setAttribute('aria-label', 'Question ' + (i + 1));
    }
  }

  function newRow() {
    var row = document.createElement('div');
    row.className = 'irow own-row';
    row.style.marginTop = '6px';
    var box = document.createElement('input');
    box.type = 'text';
    box.name = 'q';
    box.maxLength = 300;
    var x = document.createElement('button');
    x.type = 'button';
    x.className = 'icon remove';
    x.setAttribute('aria-label', 'Remove question');
    x.textContent = '✕';
    row.appendChild(box);
    row.appendChild(x);
    return row;
  }

  add.addEventListener('click', function () {
    if (ownBoxes().length >= cfg.maxQuestions) { return; }
    var row = newRow();
    ownList.appendChild(row);
    renumber();
    row.querySelector('input').focus();
    refresh();
  });

  ownList.addEventListener('click', function (e) {
    var x = e.target.closest('.remove');
    if (!x) { return; }
    var row = x.closest('.own-row');
    if (ownBoxes().length > 1) { row.remove(); } else { row.querySelector('input').value = ''; }
    renumber();
    refresh();
  });

  // Fields of the cards that are not chosen are left out of the request. They stay enabled
  // until then so that clicking one chooses its card.
  function leaveOutInactive(leave) {
    cards.forEach(function (c) {
      var off = c.getAttribute('data-kind') !== kindInput.value;
      c.querySelectorAll('input:not([type=search]), select').forEach(function (el) { el.disabled = leave && off; });
    });
  }
  window.addEventListener('pageshow', function () { leaveOutInactive(false); button.textContent = 'Start interview'; refresh(); });

  form.addEventListener('submit', function (e) {
    if (button.disabled) { e.preventDefault(); return; }
    leaveOutInactive(true);
    button.disabled = true;
    button.textContent = 'Starting…';
  });

  if (!list.querySelector('.choice')) { loadRoles(); }
  refresh();
})();
