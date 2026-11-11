// Interview screen: microphone check, one recording per question, background uploads.
// Audio is held in this page's memory only until its upload succeeds. Nothing is saved locally.
(function () {
  'use strict';
  var cfg = window.INTERVIEW;
  var $ = function (id) { return document.getElementById(id); };
  var NEXT_AFTER_MS = 3000;
  var SPEAK_LEVEL = 0.04;
  var MAX_ERROR_RETRIES = 2;
  var MAX_WAIT_S = 60;

  var stream = null, audioCtx = null, analyser = null, meterTimer = null;
  var index = 0, recorder = null, chunks = [], startedAt = 0, tick = null, nextTimer = null;
  var current = null;        // {recorder, chunks, startedAt, mime}
  var queue = Promise.resolve();
  var recorded = 0;          // answers sent or waiting to be sent (not skipped)
  var finishing = false, finished = false;

  // ---- writing the questions ----------------------------------------------------------------

  function showOnly(id) {
    ['writing', 'mic', 'live', 'sending'].forEach(function (x) { $(x).hidden = x !== id; });
  }

  function waitForQuestions() {
    fetch(cfg.urls.status, {headers: {Accept: 'application/json'}}).then(function (r) { return r.json(); }).then(function (d) {
      if (d.state === 'ready') { location.reload(); return; }
      if (d.state === 'writing') { setTimeout(waitForQuestions, 1500); return; }
      $('writing').querySelector('.spinner').hidden = true;
      $('writing-error').textContent = d.error || 'Could not write questions. Try again.';
      $('writing-error').hidden = false;
      $('writing-back').hidden = false;
    }).catch(function () { setTimeout(waitForQuestions, 3000); });
  }

  // ---- level meter --------------------------------------------------------------------------

  function level() {
    var data = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(data);
    var sum = 0;
    for (var i = 0; i < data.length; i++) { var v = (data[i] - 128) / 128; sum += v * v; }
    return Math.sqrt(sum / data.length);
  }

  function startMeter(bars, onLevel) {
    stopMeter();
    meterTimer = setInterval(function () {
      var l = level();
      bars.forEach(function (b) { b.style.width = Math.min(100, Math.round(l * 400)) + '%'; });
      if (onLevel) { onLevel(l); }
    }, 80);
  }

  function stopMeter() { if (meterTimer) { clearInterval(meterTimer); meterTimer = null; } }

  // ---- microphone check ---------------------------------------------------------------------

  function micCheck() {
    showOnly('mic');
    var loud = 0;
    $('mic-allow').addEventListener('click', function () {
      $('mic-help').hidden = true;
      if (!navigator.mediaDevices || !window.MediaRecorder) {
        $('mic-help').textContent = 'This browser cannot record audio. Use a current Chrome, Edge, Firefox or Safari.';
        $('mic-help').hidden = false;
        return;
      }
      navigator.mediaDevices.getUserMedia({audio: true}).then(function (s) {
        stream = s;
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 1024;
        audioCtx.createMediaStreamSource(stream).connect(analyser);
        $('mic-allow').disabled = true;
        $('mic-start').disabled = false;
        $('mic-status').textContent = 'Say a few words.';
        startMeter([$('mic-level')], function (l) {
          loud = l > SPEAK_LEVEL ? loud + 80 : 0;
          if (loud >= 500) { $('mic-status').textContent = 'Voice detected.'; }
        });
      }).catch(function () { $('mic-help').hidden = false; });
    });
    $('mic-start').addEventListener('click', function () { stopMeter(); begin(); });
  }

  // ---- the interview ------------------------------------------------------------------------

  function mmss(ms) {
    var s = Math.floor(ms / 1000);
    return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
  }

  function pickMime() {
    var types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
    for (var i = 0; i < types.length; i++) {
      if (window.MediaRecorder.isTypeSupported && window.MediaRecorder.isTypeSupported(types[i])) { return types[i]; }
    }
    return '';
  }

  function extension(mime) {
    if (mime.indexOf('mp4') >= 0) { return 'm4a'; }
    if (mime.indexOf('ogg') >= 0) { return 'ogg'; }
    return 'webm';
  }

  function begin() {
    showOnly('live');
    window.addEventListener('beforeunload', function (e) {
      if (!finished) { e.preventDefault(); e.returnValue = ''; }
    });
    window.addEventListener('pagehide', function () {
      if (!finished && navigator.sendBeacon) { navigator.sendBeacon(cfg.urls.abandon); }
    });
    ask();
  }

  function setNext(enabled) { $('next').disabled = !enabled; }

  function ask() {
    var q = cfg.questions[index];
    var last = index === cfg.questions.length - 1;
    $('progress').textContent = 'Question ' + (index + 1) + ' of ' + cfg.questions.length;
    $('question').textContent = q.text;
    $('next').textContent = last ? 'Finish interview' : 'Next question';
    $('timer').textContent = '0:00';
    $('timer').className = 'timer';
    $('timer-note').textContent = '';
    $('rec-note').textContent = '';
    $('skip-reading').hidden = true;
    setNext(false);
    if (cfg.readAloud && 'speechSynthesis' in window && window.SpeechSynthesisUtterance) {
      speak(q.text);
    } else {
      if (cfg.readAloud) { $('read-note').hidden = false; }
      startRecording();
    }
  }

  function pickVoice() {
    var voices = window.speechSynthesis.getVoices();
    var prefer = ['en-IN', 'en-GB', 'en-US'];
    for (var i = 0; i < prefer.length; i++) {
      for (var j = 0; j < voices.length; j++) { if (voices[j].lang && voices[j].lang.replace('_', '-') === prefer[i]) { return voices[j]; } }
    }
    return null;
  }

  var reading = false;
  function stopReading() { reading = false; $('skip-reading').hidden = true; }

  function speak(text) {
    var u = new SpeechSynthesisUtterance(text);
    var v = pickVoice();
    if (v) { u.voice = v; }
    u.lang = (v && v.lang) || 'en-IN';
    var done = false;
    function after() {
      if (done) { return; }
      done = true;
      stopReading();
      startRecording();
    }
    u.onend = after;
    u.onerror = after;
    reading = true;
    $('skip-reading').hidden = false;
    $('skip-reading').onclick = function () { window.speechSynthesis.cancel(); after(); };
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
  }

  function startRecording() {
    var mime = pickMime();
    var rec = mime ? new MediaRecorder(stream, {mimeType: mime}) : new MediaRecorder(stream);
    var parts = [];
    current = {recorder: rec, chunks: parts, startedAt: performance.now(), mime: rec.mimeType || mime || 'audio/webm'};
    rec.ondataavailable = function (e) { if (e.data && e.data.size) { parts.push(e.data); } };
    rec.start(1000);
    $('rec-note').textContent = 'Recording';
    startMeter([$('live-level')]);
    nextTimer = setTimeout(function () { setNext(true); }, NEXT_AFTER_MS);
    tick = setInterval(onTick, 200);
  }

  function onTick() {
    var ms = performance.now() - current.startedAt;
    var s = ms / 1000;
    $('timer').textContent = mmss(ms);
    if (s >= cfg.stop) { advance('answer'); return; }
    if (s >= cfg.over) {
      $('timer').className = 'timer over';
      $('timer-note').textContent = 'Over ' + Math.round(cfg.over / 60 * 10) / 10 + ' minutes';
    } else if (s >= cfg.amber) {
      $('timer').className = 'timer amber';
      $('timer-note').textContent = 'Time to wrap up';
    }
  }

  // Stop the current recording and resolve with {blob, seconds, mime}.
  function stopRecording() {
    clearInterval(tick); tick = null;
    clearTimeout(nextTimer); nextTimer = null;
    stopMeter();
    var c = current;
    current = null;
    $('rec-note').textContent = '';
    if (!c) { return Promise.resolve(null); }
    return new Promise(function (resolve) {
      var seconds = (performance.now() - c.startedAt) / 1000;
      c.recorder.onstop = function () {
        resolve({blob: new Blob(c.chunks, {type: c.mime}), seconds: seconds, mime: c.mime});
      };
      try { c.recorder.stop(); } catch (e) { resolve(null); }
    });
  }

  // what: 'answer' | 'skip'
  function advance(what) {
    var idx = cfg.questions[index].idx;
    if (reading) { window.speechSynthesis.cancel(); stopReading(); }
    var taken = stopRecording();
    // Queue the job now, before the recording has finished, so that finish() waits for it.
    if (what === 'skip') {
      enqueue(function () { return taken.then(function () { return post(cfg.urls.skip, idx); }); });
    } else {
      recorded++;
      enqueue(function () { return taken.then(function (rec) { return sendAnswer(idx, rec); }); });
    }
    index++;
    if (index >= cfg.questions.length) { finish(); } else { ask(); }
  }

  function enqueue(job) {
    queue = queue.then(job).catch(function () {});
  }

  // ---- uploads ------------------------------------------------------------------------------

  function url(base, idx) { return base.replace(/\/0$/, '/' + idx); }

  function post(base, idx, body) {
    return fetch(url(base, idx), {method: 'POST', body: body || new FormData()});
  }

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function sendAnswer(idx, rec) {
    if (!rec || !rec.blob.size) { return notScored(idx, 'No recording was captured.'); }
    if (rec.blob.size > cfg.maxBytes) { return notScored(idx, 'The recording was too large to send.'); }
    var errors = 0, waited = false;
    function attempt() {
      var body = new FormData();
      body.append('audio', rec.blob, 'answer.' + extension(rec.mime));
      body.append('duration', String(rec.seconds));
      return fetch(url(cfg.urls.answer, idx), {method: 'POST', body: body}).then(function (r) {
        return r.json().catch(function () { return {error: 'The server sent an unexpected reply.'}; }).then(function (d) {
          if (d.status) { return d; }
          if (d.retry_after && !waited) {
            waited = true;
            return sleep(Math.min(MAX_WAIT_S, d.retry_after) * 1000).then(attempt);
          }
          if (d.error && errors < MAX_ERROR_RETRIES) {
            errors++;
            return sleep(1000 * errors).then(attempt);
          }
          return notScored(idx, d.error || 'The recording could not be turned into text.');
        });
      }).catch(function () {
        if (errors < MAX_ERROR_RETRIES) { errors++; return sleep(1000 * errors).then(attempt); }
        return notScored(idx, 'The recording could not be sent.');
      });
    }
    return attempt();
  }

  function notScored(idx, note) {
    var body = new FormData();
    body.append('note', note);
    return post(cfg.urls.notScored, idx, body);
  }

  // ---- ending -------------------------------------------------------------------------------

  function finish() {
    if (finishing) { return; }
    finishing = true;
    showOnly('sending');
    queue.then(function () {
      return fetch(cfg.urls.finish, {method: 'POST'}).then(function (r) { return r.json(); });
    }).then(function (d) {
      finished = true;
      location.href = d.next || cfg.urls.start;
    }).catch(function () {
      finishing = false;
      showOnly('live');
      $('rec-note').textContent = 'Could not finish. Try End interview again.';
    });
  }

  function endNow() {
    var n = recorded + (current && (performance.now() - current.startedAt) >= NEXT_AFTER_MS ? 1 : 0);
    var message = n > 0
      ? 'End now? You will get feedback on ' + n + ' answered question' + (n === 1 ? '' : 's') + '.'
      : 'Nothing to score. The interview will not be saved. End now?';
    if (!window.confirm(message)) { return; }
    var idx = cfg.questions[index].idx;
    if (reading) { window.speechSynthesis.cancel(); stopReading(); }
    var keep = current && (performance.now() - current.startedAt) >= NEXT_AFTER_MS;
    var taken = stopRecording();
    if (keep) {
      recorded++;
      enqueue(function () { return taken.then(function (rec) { return sendAnswer(idx, rec); }); });
    }
    index = cfg.questions.length;
    taken.then(finish);
  }

  // ---- start up -----------------------------------------------------------------------------

  if (!cfg.questions.length) {
    waitForQuestions();
    return;
  }
  $('next').addEventListener('click', function () { if (!$('next').disabled) { advance('answer'); } });
  $('skip').addEventListener('click', function () { advance('skip'); });
  $('end').addEventListener('click', endNow);
  micCheck();
})();
