/* Activity form: guard against oversize files and double submits. The
   server re-checks the file regardless — this only improves the
   experience. */
document.addEventListener('DOMContentLoaded', function () {
  var form = document.getElementById('activity-form');
  if (!form) return;

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
