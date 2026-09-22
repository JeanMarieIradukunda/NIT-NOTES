/* Module notes form: switch between "select existing module" and "enter new
   module", and guard against oversize files / double submits. The server
   validates everything again — this only improves the experience. */
document.addEventListener('DOMContentLoaded', function () {
  var form = document.getElementById('note-form');
  if (!form) return;

  var paneExisting = document.getElementById('pane-existing');
  var paneNew = document.getElementById('pane-new');
  var modeExisting = document.getElementById('mode-existing');
  var modeNew = document.getElementById('mode-new');
  var select = form.querySelector('[name="module"]');
  var newFields = form.querySelectorAll('[name^="new_module_"]');
  var hasModules = form.dataset.hasModules === '1';

  function anyNewValue() {
    return Array.prototype.some.call(newFields, function (f) { return f.value.trim() !== ''; });
  }
  function hasNewErrors() {
    return !!paneNew.querySelector('.invalid-msg');
  }
  function show(mode) {
    var isNew = mode === 'new';
    paneExisting.hidden = isNew;
    paneNew.hidden = !isNew;
    (isNew ? modeNew : modeExisting).checked = true;
    if (isNew && select) select.value = '';
    if (!isNew) Array.prototype.forEach.call(newFields, function (f) { f.value = ''; });
  }

  modeExisting.addEventListener('change', function () { show('existing'); });
  modeNew.addEventListener('change', function () { show('new'); });

  var start = (!hasModules || hasNewErrors() || (anyNewValue() && !(select && select.value))) ? 'new' : 'existing';
  show(start);
  if (!hasModules) modeExisting.disabled = true;

  var file = form.querySelector('input[type="file"]');
  var maxBytes = parseInt(form.dataset.maxMb || '25', 10) * 1024 * 1024;
  if (file) {
    file.addEventListener('change', function () {
      var old = file.parentNode.querySelector('.js-size-error');
      if (old) old.remove();
      file.classList.remove('is-invalid');
      if (file.files[0] && file.files[0].size > maxBytes) {
        var msg = document.createElement('div');
        msg.className = 'invalid-msg js-size-error';
        msg.textContent = 'That file is larger than the ' + form.dataset.maxMb + ' MB limit.';
        file.classList.add('is-invalid');
        file.insertAdjacentElement('afterend', msg);
        file.value = '';
      }
    });
  }

  form.addEventListener('submit', function (e) {
    var btn = e.submitter;
    if (form.dataset.submitted) { e.preventDefault(); return; }
    form.dataset.submitted = '1';
    if (btn) { btn.classList.add('disabled'); btn.setAttribute('aria-busy', 'true'); }
  });
});
