(function () {
  'use strict';

  var topbar = document.getElementById('topbar');
  var button = document.getElementById('run-button');
  var panel = document.getElementById('run');
  var entrySelect = document.getElementById('run-entry');
  var problemEl = document.getElementById('run-problem');
  var fieldsEl = document.getElementById('run-fields');
  var commandEl = document.getElementById('run-command');
  var copyButton = document.getElementById('run-copy');
  var reviewEl = document.getElementById('run-review');
  var errorEl = document.getElementById('run-error');
  var checkButton = document.getElementById('run-check');
  var submitButton = document.getElementById('run-submit');
  var worktree = new URLSearchParams(location.search).get('worktree') || '';

  // gen changes whenever the form opens, closes or changes entry; readSeq, cmdSeq and checkSeq number
  // the newest dependent read, command and check request. An answer to an older one is dropped.
  var gen = 0;
  var readSeq = 0;
  var cmdSeq = 0;
  var checkSeq = 0;
  // The request the shown review checked and allows to submit, or null; submitting while one is sent.
  var reviewed = null;
  var refusal = '';
  var submitting = false;
  var entry = null;
  var form = null;
  var values = {};
  var extra = '';
  var targets = [];
  var runs = [];

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function url(path, params) {
    return path + '?' + new URLSearchParams(Object.assign({ worktree: worktree }, params || {}));
  }

  function answer(response) {
    return response.json().catch(function () {
      return null;
    }).then(function (body) {
      return { ok: response.ok, status: response.status, body: body };
    });
  }

  // Resolves the JSON body of a read, or rejects with the server's reason.
  function read(path, params) {
    return fetch(url(path, params), { cache: 'no-store' }).then(answer).then(function (r) {
      if (!r.ok) {
        throw new Error(r.body && r.body.error ? r.body.error : 'The Dashboard server answered HTTP ' + r.status);
      }
      return r.body;
    });
  }

  function show(el, text) {
    el.textContent = text;
    el.hidden = !text;
  }

  // The handler of a read that fails: it shows the reason unless the form moved on.
  function problemUnless(mine) {
    return function (err) {
      if (mine === gen) {
        show(problemEl, err.message);
      }
    };
  }

  // Only Starlette's unknown-route answer (404, not JSON) says the flag is off.
  fetch(url('/api/run/entries'), { cache: 'no-store' }).then(answer).catch(function () {
    return null;
  }).then(function (r) {
    if (!(r && r.status === 404 && r.body === null)) {
      topbar.hidden = false;
      button.hidden = false;
    }
  });

  function option(value, label, selected, attrs) {
    return '<option value="' + escapeHtml(value) + '"' + (selected ? ' selected' : '') + (attrs || '') +
      '>' + escapeHtml(label) + '</option>';
  }

  function showEntries(body) {
    function group(label, kind, items) {
      return '<optgroup label="' + label + '">' + items.map(function (item) {
        return option(kind + ':' + item.name, item.name + ' — ' + item.blurb, false);
      }).join('') + '</optgroup>';
    }
    entrySelect.innerHTML = option('', 'Choose a Flow or Specialist', true) +
      group('Booley Flows', 'flow', body.flows) + group('Specialists', 'specialist', body.specialists);
    show(problemEl, body.specialists_problem);
  }

  function openForm() {
    if (!panel.hidden) {
      return;
    }
    gen += 1;
    var mine = gen;
    panel.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    read('/api/run/entries').then(function (body) {
      if (mine === gen) {
        showEntries(body);
      }
    }).catch(problemUnless(mine));
    entrySelect.focus();
  }

  function reset() {
    gen += 1;
    entry = form = null;
    values = {};
    extra = '';
    targets = runs = [];
    fieldsEl.innerHTML = '';
    submitting = false;
    clearReview();
    show(problemEl, '');
    showCommand('', '');
  }

  function closeForm() {
    reset();
    panel.hidden = true;
    entrySelect.innerHTML = '';
    button.setAttribute('aria-expanded', 'false');
    button.focus();
  }

  function select(field, options, current, blank) {
    return '<select data-dest="' + escapeHtml(field.dest) + '">' +
      option('', blank, current === '', field.required ? ' disabled' : '') +
      options.map(function (o) {
        return option(o[0], o[1], o[0] === current);
      }).join('') + '</select>';
  }

  function same(item) {
    return [item, item];
  }

  function testsHtml() {
    return runs.length ? runs.map(function (run) {
      return '<label><input type="checkbox" data-dest="test" value="' + escapeHtml(run) + '"' +
        ((values.test || []).indexOf(run) === -1 ? '' : ' checked') + '> ' + escapeHtml(run) + '</label>';
    }).join(' ') : '<span class="muted">' + (values.target ? 'No Tests listed' : 'Choose a Target first') + '</span>';
  }

  function recordsHtml() {
    return '<datalist id="run-records">' + form.coverage_records.map(function (record) {
      return option(record.path, record.label, false);
    }).join('') + '</datalist>';
  }

  function control(field) {
    var dest = escapeHtml(field.dest);
    var now = (field.dest === 'extra' ? extra : values[field.dest]) || '';
    var text = '<input type="text" data-dest="' + dest + '" value="' + escapeHtml(now) + '"';
    switch (field.kind) {
      case 'toggle':
      case 'more':
        return '<input type="checkbox" data-dest="' + dest + '"' + (now ? ' checked' : '') + '>';
      case 'three-way':
        return select(field, [['on', field.flag], ['off', field.off_flag]], now, 'default');
      case 'list':
        return select(field, field.choices.map(same), now, field.required ? 'Choose ' + field.label : 'default');
      case 'target':
        return select(field, targets.map(same), now, field.required ? 'Choose a Target' : 'automatic');
      case 'tests':
        return testsHtml();
      case 'coverage_record':
        return text + ' list="run-records">' + recordsHtml();
      default:
        return text + '>';
    }
  }

  function rowHtml(field, hint) {
    var tag = field.kind === 'tests' ? 'div' : 'label';
    return '<' + tag + ' class="field"><span>' + escapeHtml(field.label) + '</span><span>' +
      control(field) + '</span><span class="muted">' + escapeHtml(hint) + '</span></' + tag + '>';
  }

  // A rebuilt form keeps the focus and caret of the field being edited.
  function render() {
    var active = document.activeElement;
    var dest = active && active.dataset && active.dataset.dest;
    var caret = active && active.selectionStart;
    var rows = form.fields.map(function (field) {
      return rowHtml(field, field.kind === 'more' ? '' : field.hint);
    });
    if (form.extra) {
      rows.push(rowHtml({ dest: 'extra', kind: 'text', label: 'Extra arguments' }, form.extra.hint));
    }
    fieldsEl.innerHTML = (form.note ? '<p class="muted">' + escapeHtml(form.note) + '</p>' : '') + rows.join('');
    updateButtons();
    var again = dest && fieldsEl.querySelector('[data-dest="' + dest + '"]');
    if (again) {
      again.focus();
      if (typeof caret === 'number') {
        again.setSelectionRange(caret, caret);
      }
    }
  }

  // The command line or the form's error: one shows at a time.
  function showCommand(command, error) {
    show(commandEl, command);
    copyButton.disabled = !command;
    show(errorEl, error);
  }

  function updateButtons() {
    checkButton.disabled = !form || submitting;
    submitButton.disabled = !reviewed || submitting;
  }

  function clearReview() {
    checkSeq += 1;
    reviewed = null;
    refusal = '';
    reviewEl.innerHTML = '';
    reviewEl.hidden = true;
    updateButtons();
  }

  function request() {
    return { kind: entry.kind, name: entry.name, values: values, extra: extra };
  }

  function post(path, body) {
    return fetch(url(path), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(answer);
  }

  function failure(r) {
    return (r.body && r.body.error) || 'HTTP ' + r.status;
  }

  function postCommand() {
    var mine = gen;
    var seq = cmdSeq += 1;
    copyButton.disabled = true;
    post('/api/run/command', request()).then(function (r) {
      if (mine !== gen || seq !== cmdSeq) {
        return;
      }
      showCommand(r.ok ? r.body.command : '', r.ok ? '' : failure(r));
    }).catch(function (err) {
      if (mine === gen && seq === cmdSeq) {
        showCommand('', err.message);
      }
    });
  }

  // Review checks the form as it is: the command, the facts to read, and a refusal that blocks Submit.
  function review() {
    if (!form || submitting) {
      return;
    }
    clearReview();
    cmdSeq += 1;
    var mine = gen;
    var seq = checkSeq;
    var sent = request();
    post('/api/run/check', sent).then(function (r) {
      if (mine !== gen || seq !== checkSeq) {
        return;
      }
      if (!r.ok) {
        showCommand('', failure(r));
        return;
      }
      showCommand(r.body.command, '');
      reviewEl.innerHTML = r.body.lines.map(function (line) {
        return '<li>' + escapeHtml(line) + '</li>';
      }).join('');
      reviewEl.hidden = false;
      refusal = r.body.refusal;
      show(errorEl, refusal);
      reviewed = refusal ? null : sent;
      updateButtons();
    }).catch(function (err) {
      if (mine === gen && seq === checkSeq) {
        showCommand('', err.message);
      }
    });
  }

  // Submit starts exactly the reviewed request. After a failed or lost answer only a new Review re-enables it.
  function submit() {
    if (!reviewed || submitting) {
      return;
    }
    var mine = gen;
    var sent = reviewed;
    clearReview();
    submitting = true;
    updateButtons();
    post('/api/run/submit', sent).then(function (r) {
      if (mine === gen) {
        return r.ok ? closeForm() : ended(failure(r));
      }
    }).catch(function (err) {
      if (mine === gen) {
        ended(err.message);
      }
    });
  }

  function ended(error) {
    submitting = false;
    show(errorEl, error);
    updateButtons();
  }

  // A form answer sets the list values: the server may drop a focus the category lacks.
  function adopt(body) {
    form = body;
    body.fields.forEach(function (field) {
      var now = body.values && body.values[field.dest];
      if (field.kind === 'list' && now !== undefined) {
        setValue(field.dest, now);
      }
    });
  }

  function setValue(dest, value) {
    if (value === '' || value === undefined || (Array.isArray(value) && !value.length)) {
      delete values[dest];
    } else {
      values[dest] = value;
    }
  }

  function chooseEntry(value) {
    reset();
    var at = value.indexOf(':');
    if (at === -1) {
      return;
    }
    entry = { kind: value.slice(0, at), name: value.slice(at + 1) };
    var mine = gen;
    var reads = [read('/api/run/form', entry)];
    if (entry.kind === 'flow') {
      reads.push(read('/api/run/targets', { flow: entry.name }));
    }
    Promise.all(reads).then(function (answers) {
      if (mine !== gen) {
        return;
      }
      if (answers[0].problem) {
        show(problemEl, answers[0].problem);
        return;
      }
      adopt(answers[0]);
      targets = answers[1] ? answers[1].targets : [];
      show(problemEl, answers[1] ? answers[1].problem : '');
      render();
      postCommand();
    }).catch(problemUnless(mine));
  }

  // A read that depends on the form's state: only the newest answer counts.
  function dependentRead(path, params, apply) {
    var mine = gen;
    var seq = readSeq += 1;
    read(path, params).then(function (body) {
      if (mine === gen && seq === readSeq) {
        apply(body);
        render();
      }
    }).catch(function (err) {
      if (seq === readSeq) {
        problemUnless(mine)(err);
      }
    });
  }

  function loadTests() {
    runs = [];
    if (!values.target) {
      readSeq += 1;
      return;
    }
    dependentRead('/api/run/tests', { target: values.target }, function (body) {
      runs = body.runs;
      show(problemEl, body.problem);
    });
  }

  // A Specialist's fields follow its list values and More, as the wizard's form does.
  function reloadForm() {
    dependentRead('/api/run/form', Object.assign({ kind: entry.kind, name: entry.name }, values), function (body) {
      clearReview(); // the answer may change the values a shown review checked
      adopt(body);
      postCommand();
    });
  }

  // A text input acts on `input`, every other control on `change`: browsers fire both for a select.
  function onEdit(event) {
    var target = event.target;
    var dest = target.dataset && target.dataset.dest;
    if (!entry || !dest || (target.type === 'text') !== (event.type === 'input')) {
      return;
    }
    clearReview();
    if (dest === 'extra') {
      extra = target.value;
    } else if (dest === 'test') {
      var kept = (values.test || []).filter(function (run) {
        return run !== target.value;
      });
      setValue(dest, target.checked ? kept.concat(target.value) : kept);
    } else {
      setValue(dest, target.type === 'checkbox' ? (target.checked ? 'on' : '') : target.value);
    }
    if (dest === 'target') {
      setValue('test', []);
      loadTests();
      render();
    } else if (entry.kind === 'specialist' && target.type !== 'text') {
      reloadForm();
    }
    postCommand();
  }

  function copy() {
    var text = commandEl.textContent;
    Promise.resolve().then(function () {
      return navigator.clipboard.writeText(text);
    }).catch(function () {
      show(errorEl, (refusal ? refusal + ' ' : '') + 'Copy failed: select the command');
    });
  }

  button.addEventListener('click', openForm);
  document.getElementById('run-cancel').addEventListener('click', closeForm);
  copyButton.addEventListener('click', copy);
  checkButton.addEventListener('click', review);
  submitButton.addEventListener('click', submit);
  entrySelect.addEventListener('change', function (event) {
    chooseEntry(event.target.value);
  });
  fieldsEl.addEventListener('change', onEdit);
  fieldsEl.addEventListener('input', onEdit);
  document.addEventListener('keydown', function (event) {
    if (!panel.hidden && event.key === 'Escape') {
      closeForm();
    }
  });
})();
