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
    cards.forEach(function (c) {
      var on = c.getAttribute('data-kind') === kind;
      c.classList.toggle('on', on);
    });
    var ok = cfg.hasCv && !cfg.running && valid();
    button.disabled = !ok;
    hint.textContent = !cfg.hasCv ? 'Upload your CV first.' : cfg.running ? 'Finish your current interview first.' : (!kind ? 'Choose one of the three.' : '');
    add.disabled = ownBoxes().length >= cfg.maxQuestions;
  }

  function choose(kind) { kindInput.value = kind; refresh(); }

  cards.forEach(function (c) {
    c.addEventListener('click', function () { if (kindInput.value !== c.getAttribute('data-kind')) { choose(c.getAttribute('data-kind')); } });
    c.addEventListener('focusin', function () { if (kindInput.value !== c.getAttribute('data-kind')) { choose(c.getAttribute('data-kind')); } });
  });

  function pick(li) {
    list.querySelectorAll('li').forEach(function (x) { x.classList.remove('on'); x.setAttribute('aria-selected', 'false'); });
    li.classList.add('on');
    li.setAttribute('aria-selected', 'true');
    roleInput.value = li.getAttribute('data-key');
    refresh();
  }

  list.addEventListener('click', function (e) {
    var li = e.target.closest('li');
    if (li) { pick(li); }
  });

  function showRoles(roles) {
    var current = roleInput.value;
    list.textContent = '';
    roles.forEach(function (r) {
      var li = document.createElement('li');
      li.setAttribute('role', 'option');
      li.setAttribute('data-key', r.key);
      var strong = document.createElement('strong');
      strong.textContent = r.title;
      var muted = document.createElement('span');
      muted.className = 'muted';
      muted.textContent = ' ' + r.company;
      li.appendChild(strong);
      li.appendChild(muted);
      if (r.key === current) { li.classList.add('on'); li.setAttribute('aria-selected', 'true'); }
      list.appendChild(li);
    });
    if (!roles.length) { list.textContent = 'No roles match.'; }
  }

  search.addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      fetch(cfg.rolesUrl + '?q=' + encodeURIComponent(search.value.trim()), {headers: {Accept: 'application/json'}})
        .then(function (r) { return r.json(); }).then(function (d) { showRoles(d.roles); }).catch(function () {});
    }, 250);
  });

  if (title) { title.addEventListener('change', refresh); }
  ownList.addEventListener('input', refresh);

  add.addEventListener('click', function () {
    var n = ownBoxes().length;
    if (n >= cfg.maxQuestions) { return; }
    var box = document.createElement('input');
    box.type = 'text';
    box.name = 'q';
    box.maxLength = 300;
    box.placeholder = 'Question ' + (n + 1);
    box.setAttribute('aria-label', 'Question ' + (n + 1));
    ownList.appendChild(box);
    box.focus();
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

  if (!list.children.length) {
    fetch(cfg.rolesUrl, {headers: {Accept: 'application/json'}}).then(function (r) { return r.json(); }).then(function (d) { showRoles(d.roles); }).catch(function () {});
  }
  refresh();
})();
