(function () {
  'use strict';

  var POLL_MS = 3000;
  var STORAGE_KEY = 'booley.dashboard.results-open';
  var FAILED = ['FAILED', 'ERROR'];
  var PASSED = 'PASSED';
  var MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];

  var live = document.getElementById('live');
  var results = document.getElementById('results');
  var worktree = new URLSearchParams(location.search).get('worktree') || '';
  var lastResults = null;
  var resultsOpen = false;
  try {
    resultsOpen = localStorage.getItem(STORAGE_KEY) === 'true';
  } catch (e) {
    resultsOpen = false;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function pad(n) {
    return n < 10 ? '0' + n : String(n);
  }

  function formatBytes(n) {
    var gib = 1024 * 1024 * 1024;
    return n >= gib ? (n / gib).toFixed(1) + ' GiB' : (n / (1024 * 1024)).toFixed(1) + ' MiB';
  }

  // Mirrors the Dashboard's duration format; an unknown duration shows nothing.
  function formatDuration(seconds) {
    if (seconds === null) {
      return '';
    }
    if (seconds <= 0) {
      return '0s';
    }
    if (seconds < 1) {
      return seconds.toFixed(1) + 's';
    }
    var s = Math.floor(seconds);
    if (s >= 86400) {
      return Math.floor(s / 86400) + 'd ' + pad(Math.floor((s % 86400) / 3600)) + 'h';
    }
    if (s >= 3600) {
      return Math.floor(s / 3600) + 'h ' + pad(Math.floor((s % 3600) / 60)) + 'm';
    }
    if (s >= 60) {
      return Math.floor(s / 60) + 'm ' + pad(s % 60) + 's';
    }
    return s + 's';
  }

  function formatTime(stamp) {
    if (!stamp) {
      return '';
    }
    var d = new Date(stamp);
    if (isNaN(d.getTime())) {
      return '';
    }
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ' · ' + pad(d.getDate()) +
      ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear();
  }

  // A value the record does not have (no target, no end time) leaves its part out.
  function part(text) {
    if (text === null || text === '') {
      return '';
    }
    return '<span>' + escapeHtml(text) + '</span>';
  }

  function memoryText(r) {
    if (r.memory_bytes === null) {
      return 'unknown';
    }
    if (r.memory_limit_bytes === null) {
      return formatBytes(r.memory_bytes) + ' (no limit)';
    }
    return formatBytes(r.memory_bytes) + ' of ' + formatBytes(r.memory_limit_bytes);
  }

  function admissionText(a) {
    if (a.memory_budget_bytes === null) {
      return 'Memory Budget unavailable: ' + (a.memory_budget_unavailable || 'unknown');
    }
    var text = 'Memory Budget ' + formatBytes(a.memory_budget_bytes) + ' · ' +
      formatBytes(a.reserved_bytes) + ' reserved · ' + a.running_jobs + ' running · ' +
      a.queued_jobs + ' queued';
    return a.running_alone ? text + ' · a Job runs alone' : text;
  }

  function headerHtml(h) {
    var r = h.resources;
    var cpu = r.cpu_percent === null ? 'measuring' : Math.round(r.cpu_percent) + '%';
    var disk = r.disk_free_bytes === null ? 'unknown' : formatBytes(r.disk_free_bytes) + ' free';
    return '<div class="header"><div class="path">' + escapeHtml(h.worktree) + '</div>' +
      '<div class="muted">' + escapeHtml(h.branch === null ? 'detached HEAD' : h.branch) + '</div>' +
      '<div class="row">' + part('CPU ' + cpu) + ' ' + part('Memory ' + memoryText(r)) + ' ' +
      part('Disk ' + disk) + '</div>' +
      '<div class="muted">' + escapeHtml(admissionText(h.admission)) + '</div></div>';
  }

  function nowHtml(rows) {
    return '<h2>Now</h2>' + rows.map(function (row) {
      var position = row.queue_position === null ? '' : part('position ' + row.queue_position);
      return '<div class="card"><div class="row"><span class="handle">' + escapeHtml(row.handle) +
        '</span>' + part(row.flow) + part(row.target) + part(row.owner) + part(row.state) +
        part(row.stage) + part(formatDuration(row.elapsed_s)) + position + '</div></div>';
    }).join('');
  }

  function resultCardHtml(job) {
    var failed = FAILED.indexOf(job.result) !== -1;
    var passed = job.result.indexOf(PASSED) === 0;
    var cls = failed ? ' failed' : passed ? ' passed' : '';
    var criterion = job.criterion ? part(job.criterion.text) : '';
    var report = job.report_url ?
      '<a href="' + escapeHtml(job.report_url) + '">Report</a>' : '';
    return '<div class="card' + cls + '"><div class="row"><span class="handle">' +
      escapeHtml(job.handle) + '</span>' + part(job.flow) + part(job.target) + part(job.owner) +
      '<span class="result">' + escapeHtml(job.result) + '</span>' + criterion +
      part(formatTime(job.ended_at)) + part(formatDuration(job.duration_s)) + report +
      '</div></div>';
  }

  function resultsInner(r) {
    return '<summary>Results · ' + r.passed + ' passed · ' + r.failed + ' failed</summary>' +
      r.jobs.map(resultCardHtml).join('');
  }

  function writeResults(inner) {
    if (inner === lastResults) {
      return;
    }
    lastResults = inner;
    results.innerHTML = inner === '' ? '' :
      '<details id="results-section"' + (resultsOpen ? ' open' : '') + '>' + inner + '</details>';
  }

  function showSnapshot(s) {
    var empty = s.now.length === 0 && s.results.jobs.length === 0;
    live.innerHTML = headerHtml(s.header) + (empty ?
      '<p class="muted">No Jobs have run in this worktree yet.</p>' :
      (s.now.length ? nowHtml(s.now) : ''));
    writeResults(s.results.jobs.length ? resultsInner(s.results) : '');
  }

  function showError(message) {
    live.innerHTML = '<div class="error">' + escapeHtml(message) + '</div>';
    writeResults('');
  }

  results.addEventListener('toggle', function (event) {
    var target = event.target;
    if (!target || target.id !== 'results-section') {
      return;
    }
    resultsOpen = Boolean(target.open);
    try {
      localStorage.setItem(STORAGE_KEY, resultsOpen ? 'true' : 'false');
    } catch (e) {
      // Storage is blocked: the state lasts until the page closes.
    }
  }, true);

  function poll() {
    fetch('/api/snapshot?' + new URLSearchParams({ worktree: worktree }), { cache: 'no-store' })
      .then(function (response) {
        return response.json().then(function (body) {
          if (response.ok) {
            showSnapshot(body);
          } else {
            showError(body.error);
          }
        }, function () {
          showError('The Dashboard server answered HTTP ' + response.status + ' without an error message.');
        });
      })
      .catch(function (err) {
        showError('The Dashboard server did not answer: ' + (err && err.message ? err.message : err));
      })
      .then(function () {
        setTimeout(poll, POLL_MS);
      });
  }

  poll();
})();
