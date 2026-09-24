/* Activity form: narrow the Topic <select> down to whichever Module is
   currently selected (each <option> carries data-module from the server),
   and guard against oversize files / double submits. The server re-checks
   the module/topic pairing and the file regardless — this only improves
   the experience. */
document.addEventListener('DOMContentLoaded', function () {
  var form = document.getElementById('activity-form');
  if (!form) return;

  var moduleSelect = form.querySelector('[name="module"]');
  var topicSelect = form.querySelector('[name="topic"]');

  function filterTopics() {
    if (!moduleSelect || !topicSelect) return;
    var moduleId = moduleSelect.value;
    var options = topicSelect.querySelectorAll('option[data-module]');
    var selectedStillValid = false;

    Array.prototype.forEach.call(options, function (opt) {
      var matches = !moduleId || opt.dataset.module === moduleId;
      opt.hidden = !matches;
      opt.disabled = !matches;
      if (matches && opt.selected) selectedStillValid = true;
    });

    if (!selectedStillValid) {
      var placeholder = topicSelect.querySelector('option[value=""]');
      var firstVisible = topicSelect.querySelector('option[data-module]:not([hidden])');
      if (placeholder) {
        placeholder.selected = true;
      } else if (firstVisible) {
        firstVisible.selected = true;
      }
    }
  }

  if (moduleSelect) {
    filterTopics();
    moduleSelect.addEventListener('change', filterTopics);
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
