/* Module notes form: switch between "select from my list" and "find by code &
   name" (an existing module either way), and guard against oversize files /
   double submits. The server validates everything again — this only improves
   the experience. */
document.addEventListener('DOMContentLoaded', function () {
  var form = document.getElementById('note-form');
  if (!form) return;

  // "Select from my list" / "Find by code & name": only one of the two is ever sent.
  var paneExisting = document.getElementById('pane-existing');
  var paneFind = document.getElementById('pane-find');
  var modeExisting = document.getElementById('mode-existing');
  var modeFind = document.getElementById('mode-find');
  var select = form.querySelector('[name="module"]');
  var findFields = form.querySelectorAll('[name^="lookup_"]');
  var hasModules = form.dataset.hasModules === '1';

  function anyFindValue() {
    return Array.prototype.some.call(findFields, function (f) { return f.value.trim() !== ''; });
  }
  function hasFindErrors() {
    return !!paneFind.querySelector('.invalid-msg');
  }
  function show(mode) {
    var find = mode === 'find';
    paneExisting.hidden = find;
    paneFind.hidden = !find;
    (find ? modeFind : modeExisting).checked = true;
    if (find && select) select.value = '';
    if (!find) Array.prototype.forEach.call(findFields, function (f) { f.value = ''; });
  }

  if (paneExisting && paneFind && modeExisting && modeFind) {
    modeExisting.addEventListener('change', function () { show('existing'); });
    modeFind.addEventListener('change', function () { show('find'); });
    show((hasFindErrors() || (anyFindValue() && !(select && select.value))) ? 'find'
         : (!hasModules ? 'find' : 'existing'));
  }

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
